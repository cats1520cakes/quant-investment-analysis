from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import pandas as pd
import pyarrow.parquet as pq

from quant_proof.network_guard import DirectHttpResponse, physical_http_get


CFFEX_ORIGIN = "http://www.cffex.com.cn"
CFFEX_PAGE_PATH = "/cn/jscs.html"
CFFEX_PAGE_NUMBERED_PATH = "/cn/jscs_{page}.html"
CFFEX_PAGE_NUMBERS: tuple[int, ...] = tuple(range(1, 37))
CFFEX_EXPECTED_UNIQUE_CSVS = 360
CFFEX_EXPECTED_FIRST_SNAPSHOT = "20100414"
CFFEX_EXPECTED_LAST_SNAPSHOT = "20260623"

EQUITY_FUTURE_PRODUCTS = frozenset({"IF", "IH", "IC", "IM"})
EQUITY_OPTION_PRODUCTS = frozenset({"IO", "HO", "MO"})
EQUITY_PRODUCTS = EQUITY_FUTURE_PRODUCTS | EQUITY_OPTION_PRODUCTS

FUTURES_HEADER = (
    "期货合约",
    "合约多头保证金标准",
    "合约空头保证金标准",
    "交易手续费标准",
    "交割手续费标准",
    "平今仓收取率",
)
OPTIONS_HEADER = (
    "合约系列",
    "保证金调整系数",
    "最低保障系数",
    "交易手续费标准",
    "行权（履约）手续费标准",
    "平今仓收取率",
)

NORMALIZED_COLUMNS: tuple[str, ...] = (
    "snapshot_date",
    "instrument_type",
    "parameter_scope",
    "contract_or_series",
    "product",
    "long_margin_rate",
    "short_margin_rate",
    "option_margin_adjustment_rate",
    "option_minimum_guarantee_coefficient",
    "trading_fee_value",
    "trading_fee_unit",
    "settlement_fee_value",
    "settlement_fee_unit",
    "settlement_fee_kind",
    "close_today_fee_multiplier",
    "close_today_fee_semantics",
    "option_shorting_enabled",
    "raw_contract_or_series",
    "raw_long_margin_standard",
    "raw_short_margin_standard",
    "raw_margin_adjustment_coefficient",
    "raw_minimum_guarantee_coefficient",
    "raw_trading_fee_standard",
    "raw_delivery_fee_standard",
    "raw_exercise_fee_standard",
    "raw_close_today_charge_rate",
    "raw_source_fields_json",
    "source_url",
    "source_official_path",
    "source_file",
    "source_section_title_date",
    "source_section_title_matches_snapshot",
    "source_sha256",
)

_STRING_COLUMNS = frozenset(NORMALIZED_COLUMNS) - {
    "long_margin_rate",
    "short_margin_rate",
    "option_margin_adjustment_rate",
    "option_minimum_guarantee_coefficient",
    "trading_fee_value",
    "settlement_fee_value",
    "close_today_fee_multiplier",
    "option_shorting_enabled",
    "source_section_title_matches_snapshot",
}
_FLOAT_COLUMNS = (
    "long_margin_rate",
    "short_margin_rate",
    "option_margin_adjustment_rate",
    "option_minimum_guarantee_coefficient",
    "trading_fee_value",
    "settlement_fee_value",
    "close_today_fee_multiplier",
)

_OFFICIAL_CSV_PATH = re.compile(
    r"^/sj/jscs/(?P<month>\d{6})/(?P<day>\d{2})/(?P<date>\d{8})_1\.csv$"
)
_FUTURE_CONTRACT = re.compile(r"^(?P<product>[A-Z]{1,3})\d{4}$")
_OPTION_SERIES = re.compile(r"^(?P<product>[A-Z]{1,3})\d{4}$")
_SECTION_TITLE = re.compile(r"^(?P<kind>期货|期权)合约结算业务参数表[（(](?P<date>\d{8})[）)]$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_SUCCESS_STATUSES = frozenset({"downloaded", "replaced_invalid_cache", "cached_valid"})


class CffexSettlementError(RuntimeError):
    """Raised when official settlement parameters fail a provenance gate."""


class CffexDiscoveryError(CffexSettlementError):
    """Raised when CFFEX pagination or official links are malformed."""


class CffexContentError(CffexSettlementError):
    """Raised when a settlement CSV cannot be parsed safely."""


class CffexArtifactError(CffexSettlementError):
    """Raised when a canonical artifact or its manifest is stale."""


class CffexLookupError(CffexSettlementError):
    """Raised when an exact causal lookup has no admissible record."""


class ShortOptionsDisabledError(CffexLookupError):
    """Raised when settlement data is asked to authorize a short option."""


class FeeUnit(str, Enum):
    NOTIONAL_RATE = "notional_rate"
    CURRENCY_PER_CONTRACT = "currency_per_contract"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ParsedFee:
    value: float | None
    unit: FeeUnit


@dataclass(frozen=True)
class ParsedCloseToday:
    multiplier: float | None
    semantics: str


@dataclass(frozen=True)
class DiscoveredSettlementCsv:
    official_path: str
    url: str
    snapshot_date: str
    discovery_pages: tuple[int, ...]


@dataclass(frozen=True)
class SettlementDownloadRecord:
    official_path: str
    url: str
    snapshot_date: str
    discovery_pages: tuple[int, ...]
    local_path: str
    bytes: int
    sha256: str
    local_ip: str
    remote_ip: str
    interface: str
    status: str
    checked_at_utc: str
    fetched_at_utc: str
    error: str = ""
    quarantine_path: str = ""

    @property
    def successful(self) -> bool:
        return self.status in _SUCCESS_STATUSES


@dataclass(frozen=True)
class SettlementArtifact:
    parquet_path: Path
    manifest_path: Path
    rows: int
    sources: int
    canonical: bool


class _DiscoveryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.current_pages: list[int] = []
        self.total_pages: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() == "a" and values.get("href"):
            self.hrefs.append(str(values["href"]))
        for attribute in ("onclick", "onkeyup"):
            expression = str(values.get(attribute, "")).strip()
            last_match = re.fullmatch(r"lastPage\(\s*(\d+)\s*\)", expression)
            next_match = re.fullmatch(r"nextPage\(\s*(\d+)\s*,\s*(\d+)\s*\)", expression)
            total_match = re.fullmatch(r"(?:toPage|jump)\(\s*['\"]?(\d+)['\"]?\s*\)", expression)
            if last_match:
                self.current_pages.append(int(last_match.group(1)))
            if next_match:
                self.current_pages.append(int(next_match.group(1)))
                self.total_pages.append(int(next_match.group(2)))
            if total_match:
                self.total_pages.append(int(total_match.group(1)))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parser_source_sha256() -> str:
    return sha256_file(Path(__file__))


def normalize_snapshot_date(value: object) -> str:
    if isinstance(value, (date, datetime, pd.Timestamp)):
        text = pd.Timestamp(value).strftime("%Y%m%d")
    else:
        text = str(value).strip().replace("-", "").replace("/", "")
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"invalid snapshot/trade date: {value!r}")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"invalid calendar date: {value!r}") from exc
    return text


def validate_page_numbers(
    page_numbers: Sequence[int],
    *,
    expected_total_pages: int = 36,
) -> tuple[int, ...]:
    pages = tuple(int(page) for page in page_numbers)
    if not pages:
        raise CffexDiscoveryError("at least one CFFEX pagination page is required")
    if pages != tuple(sorted(pages)) or len(set(pages)) != len(pages):
        raise CffexDiscoveryError("CFFEX page numbers must be unique and sorted")
    if pages != tuple(range(pages[0], pages[-1] + 1)):
        raise CffexDiscoveryError(f"CFFEX pagination is not continuous: {pages}")
    if pages[0] < 1 or pages[-1] > int(expected_total_pages):
        raise CffexDiscoveryError(
            f"CFFEX page selection {pages[0]}..{pages[-1]} exceeds 1..{expected_total_pages}"
        )
    return pages


def cffex_pagination_url(page: int) -> str:
    page_number = int(page)
    if page_number < 1:
        raise ValueError(f"invalid CFFEX page number: {page}")
    path = CFFEX_PAGE_PATH if page_number == 1 else CFFEX_PAGE_NUMBERED_PATH.format(page=page_number)
    return CFFEX_ORIGIN + path


def validate_official_csv_path(path: str) -> tuple[str, str]:
    match = _OFFICIAL_CSV_PATH.fullmatch(str(path))
    if not match:
        raise CffexDiscoveryError(f"malformed CFFEX settlement CSV path: {path}")
    snapshot_date = match.group("date")
    path_date = match.group("month") + match.group("day")
    if snapshot_date != path_date:
        raise CffexDiscoveryError(
            f"CFFEX path/filename date disagreement: path={path_date} filename={snapshot_date}"
        )
    normalize_snapshot_date(snapshot_date)
    return snapshot_date, Path(path).name


def _official_path_from_href(href: str) -> str | None:
    parsed = urlsplit(str(href).strip())
    path = parsed.path
    candidate = path.startswith("/sj/jscs/") and path.lower().endswith(".csv")
    if not candidate:
        return None
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise CffexDiscoveryError(f"malformed CFFEX settlement CSV link: {href}")
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.hostname != "www.cffex.com.cn":
            raise CffexDiscoveryError(f"non-official CFFEX settlement CSV link: {href}")
    validate_official_csv_path(path)
    return path


def _looks_like_html(payload: bytes) -> bool:
    prefix = payload[:8192].lstrip().lower()
    return any(marker in prefix for marker in (b"<!doctype html", b"<html", b"<body", b"<head"))


def _decode_discovery_html(payload: bytes, url: str) -> str:
    if not payload or not _looks_like_html(payload):
        raise CffexDiscoveryError(f"CFFEX pagination response is empty or not HTML: {url}")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CffexDiscoveryError(f"CFFEX pagination page is not decodable: {url}")


def _parse_discovery_page(payload: bytes, page: int, expected_total_pages: int) -> list[str]:
    url = cffex_pagination_url(page)
    parser = _DiscoveryParser()
    parser.feed(_decode_discovery_html(payload, url))
    currents = set(parser.current_pages)
    totals = set(parser.total_pages)
    if currents != {int(page)}:
        raise CffexDiscoveryError(
            f"CFFEX page continuity mismatch for page {page}: declared current pages={sorted(currents)}"
        )
    if totals != {int(expected_total_pages)}:
        observed = sorted(totals)
        if observed and max(observed) > int(expected_total_pages):
            raise CffexDiscoveryError(
                f"CFFEX pagination grew beyond configured page {expected_total_pages}: observed={observed}"
            )
        raise CffexDiscoveryError(
            f"CFFEX page-count continuity mismatch on page {page}: expected={expected_total_pages} observed={observed}"
        )
    paths: list[str] = []
    for href in parser.hrefs:
        official_path = _official_path_from_href(href)
        if official_path is not None:
            paths.append(official_path)
    if not paths:
        raise CffexDiscoveryError(f"CFFEX pagination page {page} has no official settlement CSV links")
    return paths


def discover_cffex_settlement_csvs(
    *,
    page_numbers: Sequence[int] = CFFEX_PAGE_NUMBERS,
    expected_total_pages: int = 36,
    expected_unique_csvs: int | None = CFFEX_EXPECTED_UNIQUE_CSVS,
    expected_first_snapshot: str | None = CFFEX_EXPECTED_FIRST_SNAPSHOT,
    expected_last_snapshot: str | None = CFFEX_EXPECTED_LAST_SNAPSHOT,
    interface: str = "en0",
    dns_server: str | None = None,
    timeout_seconds: float = 60.0,
    max_page_bytes: int = 2 * 1024 * 1024,
    fetcher: Callable[..., DirectHttpResponse] = physical_http_get,
) -> list[DiscoveredSettlementCsv]:
    pages = validate_page_numbers(page_numbers, expected_total_pages=expected_total_pages)
    path_pages: dict[str, set[int]] = {}
    for page in pages:
        url = cffex_pagination_url(page)
        response = fetcher(
            url,
            interface=interface,
            dns_server=dns_server,
            timeout_seconds=timeout_seconds,
            max_bytes=max_page_bytes,
        )
        if int(response.status) != 200:
            raise CffexDiscoveryError(
                f"CFFEX pagination page {page} returned HTTP {response.status} {response.reason}"
            )
        for path in _parse_discovery_page(response.body, page, expected_total_pages):
            path_pages.setdefault(path, set()).add(page)

    discovered: list[DiscoveredSettlementCsv] = []
    dates: dict[str, str] = {}
    for path, source_pages in path_pages.items():
        snapshot_date, _ = validate_official_csv_path(path)
        previous = dates.setdefault(snapshot_date, path)
        if previous != path:
            raise CffexDiscoveryError(
                f"multiple official settlement CSV paths claim snapshot {snapshot_date}: {previous}, {path}"
            )
        discovered.append(
            DiscoveredSettlementCsv(
                official_path=path,
                url=CFFEX_ORIGIN + path,
                snapshot_date=snapshot_date,
                discovery_pages=tuple(sorted(source_pages)),
            )
        )
    discovered.sort(key=lambda item: (item.snapshot_date, item.official_path))

    full_scope = pages == tuple(range(1, int(expected_total_pages) + 1))
    if full_scope and expected_unique_csvs is not None and len(discovered) != int(expected_unique_csvs):
        raise CffexDiscoveryError(
            f"canonical CFFEX discovery expected {expected_unique_csvs} unique CSVs, found {len(discovered)}"
        )
    if full_scope and discovered and expected_first_snapshot is not None:
        expected_first = normalize_snapshot_date(expected_first_snapshot)
        if discovered[0].snapshot_date != expected_first:
            raise CffexDiscoveryError(
                f"canonical CFFEX first snapshot drifted: expected={expected_first} found={discovered[0].snapshot_date}"
            )
    if full_scope and discovered and expected_last_snapshot is not None:
        expected_last = normalize_snapshot_date(expected_last_snapshot)
        if discovered[-1].snapshot_date != expected_last:
            raise CffexDiscoveryError(
                f"canonical CFFEX last snapshot drifted: expected={expected_last} found={discovered[-1].snapshot_date}"
            )
    return discovered


def decode_cffex_settlement_csv(payload: bytes) -> str:
    if not payload or not payload.strip():
        raise CffexContentError("CFFEX settlement CSV is empty")
    if _looks_like_html(payload):
        raise CffexContentError("CFFEX settlement CSV response is HTML")
    try:
        text = payload.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise CffexContentError("CFFEX settlement CSV is not decodable as GB18030") from exc
    if "\x00" in text:
        raise CffexContentError("CFFEX settlement CSV contains NUL bytes")
    return text.lstrip("\ufeff")


def _finite_number(text: str, field_name: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise CffexContentError(f"unsupported {field_name}: {text!r}") from exc
    if not math.isfinite(value):
        raise CffexContentError(f"non-finite {field_name}: {text!r}")
    return value


def parse_percentage(raw: str, field_name: str) -> float:
    compact = re.sub(r"\s+", "", str(raw)).replace("％", "%")
    percent = re.fullmatch(rf"({_NUMBER})%", compact)
    chinese = re.fullmatch(rf"百分之({_NUMBER})", compact)
    match = percent or chinese
    if not match:
        raise CffexContentError(f"unsupported percentage in {field_name}: {raw!r}")
    value = _finite_number(match.group(1), field_name) / 100.0
    if value <= 0.0:
        raise CffexContentError(f"{field_name} must be positive: {raw!r}")
    return value


def parse_positive_coefficient(raw: str, field_name: str) -> float:
    compact = re.sub(r"\s+", "", str(raw)).replace("％", "%")
    if compact.endswith("%") or compact.startswith("百分之"):
        return parse_percentage(compact, field_name)
    if not re.fullmatch(_NUMBER, compact):
        raise CffexContentError(f"unsupported coefficient in {field_name}: {raw!r}")
    value = _finite_number(compact, field_name)
    if value <= 0.0:
        raise CffexContentError(f"{field_name} must be positive: {raw!r}")
    return value


def parse_fee_standard(raw: str) -> ParsedFee:
    compact = re.sub(r"\s+", "", str(raw)).replace("％", "%").replace("／", "/")
    suffix = r"(?:（[^）]*）|\([^)]*\))?"
    denominators = {
        "百分之": 100.0,
        "千分之": 1_000.0,
        "万分之": 10_000.0,
        "十万分之": 100_000.0,
        "百万分之": 1_000_000.0,
    }
    for label, denominator in denominators.items():
        match = re.fullmatch(rf"(?:成交金额的|成交额的|交割金额的)?{label}({_NUMBER}){suffix}", compact)
        if match:
            return ParsedFee(_finite_number(match.group(1), "fee") / denominator, FeeUnit.NOTIONAL_RATE)
    percent = re.fullmatch(rf"(?:成交金额的|成交额的|交割金额的)?({_NUMBER})%{suffix}", compact)
    if percent:
        return ParsedFee(_finite_number(percent.group(1), "fee") / 100.0, FeeUnit.NOTIONAL_RATE)
    per_contract = re.fullmatch(rf"({_NUMBER})元/(?:手|张){suffix}", compact)
    if per_contract:
        return ParsedFee(_finite_number(per_contract.group(1), "fee"), FeeUnit.CURRENCY_PER_CONTRACT)
    alternate = re.fullmatch(rf"每(?:手|张)({_NUMBER})元{suffix}", compact)
    if alternate:
        return ParsedFee(_finite_number(alternate.group(1), "fee"), FeeUnit.CURRENCY_PER_CONTRACT)
    return ParsedFee(None, FeeUnit.UNSUPPORTED)


def parse_close_today_charge(raw: str) -> ParsedCloseToday:
    compact = re.sub(r"\s+", "", str(raw)).replace("％", "%")
    if compact in {"按交易手续费标准收取", "同交易手续费", "按交易手续费收取"}:
        return ParsedCloseToday(1.0, "fraction_of_trading_fee")
    if compact in {"免收", "不收取", "0"}:
        return ParsedCloseToday(0.0, "fraction_of_trading_fee")
    percent = re.fullmatch(rf"(?:交易手续费标准的)?({_NUMBER})%", compact)
    chinese = re.fullmatch(rf"(?:交易手续费标准的)?百分之({_NUMBER})", compact)
    match = percent or chinese
    if not match:
        return ParsedCloseToday(None, "unsupported")
    multiplier = _finite_number(match.group(1), "close-today charge") / 100.0
    if multiplier < 0.0:
        raise CffexContentError(f"close-today multiplier must be non-negative: {raw!r}")
    return ParsedCloseToday(multiplier, "fraction_of_trading_fee")


def _clean_cells(row: Sequence[str]) -> list[str]:
    cells = [str(cell).strip().lstrip("\ufeff") for cell in row]
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def _require_six_fields(raw_row: Sequence[str], section: str) -> tuple[list[str], list[str]]:
    raw = list(raw_row)
    while raw and str(raw[-1]) == "":
        raw.pop()
    clean = _clean_cells(raw)
    if len(clean) != 6:
        raise CffexContentError(f"{section} row must have exactly six fields: {clean!r}")
    return raw, clean


def _require_supported_positive_fee(parsed: ParsedFee, raw: str, field_name: str) -> None:
    if parsed.unit is FeeUnit.UNSUPPORTED or parsed.value is None:
        raise CffexContentError(f"unsupported fee unit in {field_name}: {raw!r}")
    if not math.isfinite(parsed.value) or parsed.value <= 0.0:
        raise CffexContentError(f"{field_name} must be positive and finite: {raw!r}")


def _parse_future_row(raw_row: Sequence[str]) -> tuple[str, dict[str, object]]:
    raw, clean = _require_six_fields(raw_row, "futures")
    contract = clean[0]
    match = _FUTURE_CONTRACT.fullmatch(contract)
    if not match:
        raise CffexContentError(f"malformed futures contract row: {contract!r}")
    product = match.group("product")
    if product not in EQUITY_FUTURE_PRODUCTS:
        return product, {"contract": contract, "raw": raw, "scope": "out_of_scope"}
    long_margin = parse_percentage(clean[1], "long margin")
    short_margin = parse_percentage(clean[2], "short margin")
    trading_fee = parse_fee_standard(clean[3])
    delivery_fee = parse_fee_standard(clean[4])
    close_today = parse_close_today_charge(clean[5])
    _require_supported_positive_fee(trading_fee, clean[3], "trading fee")
    _require_supported_positive_fee(delivery_fee, clean[4], "delivery fee")
    if close_today.multiplier is None or close_today.semantics == "unsupported":
        raise CffexContentError(f"unsupported close-today charge: {clean[5]!r}")
    fields = dict(zip(FUTURES_HEADER, raw, strict=True))
    return product, {
        "contract": contract,
        "long_margin": long_margin,
        "short_margin": short_margin,
        "trading_fee": trading_fee,
        "settlement_fee": delivery_fee,
        "close_today": close_today,
        "raw": raw,
        "raw_fields": fields,
    }


def _parse_option_row(raw_row: Sequence[str]) -> tuple[str, dict[str, object]]:
    raw, clean = _require_six_fields(raw_row, "options")
    series = clean[0]
    match = _OPTION_SERIES.fullmatch(series)
    if not match:
        raise CffexContentError(f"malformed option-series row: {series!r}")
    product = match.group("product")
    if product not in EQUITY_OPTION_PRODUCTS:
        return product, {"contract": series, "raw": raw, "scope": "out_of_scope"}
    margin_adjustment = parse_percentage(clean[1], "option margin adjustment")
    minimum_guarantee = parse_positive_coefficient(clean[2], "option minimum guarantee")
    trading_fee = parse_fee_standard(clean[3])
    exercise_fee = parse_fee_standard(clean[4])
    close_today = parse_close_today_charge(clean[5])
    _require_supported_positive_fee(trading_fee, clean[3], "option trading fee")
    _require_supported_positive_fee(exercise_fee, clean[4], "option exercise fee")
    if close_today.multiplier is None or close_today.semantics == "unsupported":
        raise CffexContentError(f"unsupported close-today charge: {clean[5]!r}")
    fields = dict(zip(OPTIONS_HEADER, raw, strict=True))
    return product, {
        "contract": series,
        "margin_adjustment": margin_adjustment,
        "minimum_guarantee": minimum_guarantee,
        "trading_fee": trading_fee,
        "settlement_fee": exercise_fee,
        "close_today": close_today,
        "raw": raw,
        "raw_fields": fields,
    }


def _base_normalized_row(
    *,
    snapshot_date: str,
    source_url: str,
    source_official_path: str,
    source_sha256: str,
) -> dict[str, object]:
    return {
        column: pd.NA for column in NORMALIZED_COLUMNS
    } | {
        "snapshot_date": snapshot_date,
        "option_shorting_enabled": False,
        "source_url": source_url,
        "source_official_path": source_official_path,
        "source_file": Path(source_official_path).name,
        "source_sha256": source_sha256,
    }


def _frame_with_stable_dtypes(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(NORMALIZED_COLUMNS))
    for column in _STRING_COLUMNS:
        frame[column] = frame[column].astype("string")
    for column in _FLOAT_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Float64")
    frame["option_shorting_enabled"] = frame["option_shorting_enabled"].astype("boolean")
    frame["source_section_title_matches_snapshot"] = frame[
        "source_section_title_matches_snapshot"
    ].astype("boolean")
    return frame


def _section_title_date_before(
    clean_rows: Sequence[Sequence[str]],
    header_index: int,
    expected_kind: str,
) -> str:
    cursor = header_index - 1
    while cursor >= 0 and not clean_rows[cursor]:
        cursor -= 1
    if cursor < 0 or len(clean_rows[cursor]) != 1:
        raise CffexContentError(f"missing dated {expected_kind} section title")
    match = _SECTION_TITLE.fullmatch(clean_rows[cursor][0])
    if not match or match.group("kind") != expected_kind:
        raise CffexContentError(f"malformed dated {expected_kind} section title: {clean_rows[cursor]!r}")
    return normalize_snapshot_date(match.group("date"))


def parse_cffex_settlement_csv(
    payload: bytes,
    snapshot_date: object,
    *,
    source_official_path: str | None = None,
    source_url: str | None = None,
) -> pd.DataFrame:
    normalized_date = normalize_snapshot_date(snapshot_date)
    official_path = source_official_path or (
        f"/sj/jscs/{normalized_date[:6]}/{normalized_date[6:]}/{normalized_date}_1.csv"
    )
    path_date, _ = validate_official_csv_path(official_path)
    if path_date != normalized_date:
        raise CffexContentError(
            f"source path snapshot {path_date} does not match requested snapshot {normalized_date}"
        )
    official_url = source_url or CFFEX_ORIGIN + official_path
    parsed_url = urlsplit(official_url)
    if parsed_url.hostname != "www.cffex.com.cn" or parsed_url.path != official_path:
        raise CffexContentError(f"source URL is not bound to the official path: {official_url}")

    text = decode_cffex_settlement_csv(payload)
    embedded_dates = set(re.findall(r"结算业务参数表[（(](\d{8})[）)]", text))
    if not embedded_dates or normalized_date not in embedded_dates:
        raise CffexContentError(
            f"CFFEX settlement title date mismatch: expected={normalized_date} found={sorted(embedded_dates)}"
        )
    for embedded_date in embedded_dates:
        title_date = normalize_snapshot_date(embedded_date)
        if title_date > normalized_date:
            raise CffexContentError(
                f"CFFEX settlement section title is future-dated: snapshot={normalized_date} title={title_date}"
            )
    raw_rows = list(csv.reader(StringIO(text, newline="")))
    clean_rows = [_clean_cells(row) for row in raw_rows]
    futures_indices = [index for index, row in enumerate(clean_rows) if tuple(row) == FUTURES_HEADER]
    options_indices = [index for index, row in enumerate(clean_rows) if tuple(row) == OPTIONS_HEADER]
    if len(futures_indices) != 1:
        raise CffexContentError(f"expected one futures header, found {len(futures_indices)}")
    if len(options_indices) > 1:
        raise CffexContentError(f"expected at most one options header, found {len(options_indices)}")
    futures_index = futures_indices[0]
    options_index = options_indices[0] if options_indices else None
    if options_index is not None and options_index <= futures_index:
        raise CffexContentError("options section precedes the futures section")
    futures_title_date = _section_title_date_before(clean_rows, futures_index, "期货")
    options_title_date = (
        _section_title_date_before(clean_rows, options_index, "期权")
        if options_index is not None
        else None
    )

    digest = sha256_bytes(payload)
    rows: list[dict[str, object]] = []
    future_end = options_index if options_index is not None else len(raw_rows)
    for raw_row in raw_rows[futures_index + 1 : future_end]:
        clean_row = _clean_cells(raw_row)
        if not clean_row:
            continue
        if len(clean_row) == 1 and _SECTION_TITLE.fullmatch(clean_row[0]):
            continue
        product, parsed = _parse_future_row(raw_row)
        if product not in EQUITY_FUTURE_PRODUCTS:
            continue
        raw = parsed["raw"]
        row = _base_normalized_row(
            snapshot_date=normalized_date,
            source_url=official_url,
            source_official_path=official_path,
            source_sha256=digest,
        )
        trading_fee = parsed["trading_fee"]
        settlement_fee = parsed["settlement_fee"]
        close_today = parsed["close_today"]
        assert isinstance(trading_fee, ParsedFee)
        assert isinstance(settlement_fee, ParsedFee)
        assert isinstance(close_today, ParsedCloseToday)
        row.update(
            {
                "instrument_type": "future",
                "parameter_scope": "contract",
                "contract_or_series": parsed["contract"],
                "product": product,
                "long_margin_rate": parsed["long_margin"],
                "short_margin_rate": parsed["short_margin"],
                "trading_fee_value": trading_fee.value,
                "trading_fee_unit": trading_fee.unit.value,
                "settlement_fee_value": settlement_fee.value,
                "settlement_fee_unit": settlement_fee.unit.value,
                "settlement_fee_kind": "delivery",
                "close_today_fee_multiplier": close_today.multiplier,
                "close_today_fee_semantics": close_today.semantics,
                "raw_contract_or_series": raw[0],
                "raw_long_margin_standard": raw[1],
                "raw_short_margin_standard": raw[2],
                "raw_trading_fee_standard": raw[3],
                "raw_delivery_fee_standard": raw[4],
                "raw_close_today_charge_rate": raw[5],
                "raw_source_fields_json": json.dumps(
                    parsed["raw_fields"], ensure_ascii=False, separators=(",", ":")
                ),
                "source_section_title_date": futures_title_date,
                "source_section_title_matches_snapshot": futures_title_date == normalized_date,
            }
        )
        rows.append(row)

    if options_index is not None:
        for raw_row in raw_rows[options_index + 1 :]:
            clean_row = _clean_cells(raw_row)
            if not clean_row:
                continue
            if len(clean_row) == 1 and _SECTION_TITLE.fullmatch(clean_row[0]):
                continue
            product, parsed = _parse_option_row(raw_row)
            if product not in EQUITY_OPTION_PRODUCTS:
                continue
            raw = parsed["raw"]
            row = _base_normalized_row(
                snapshot_date=normalized_date,
                source_url=official_url,
                source_official_path=official_path,
                source_sha256=digest,
            )
            trading_fee = parsed["trading_fee"]
            settlement_fee = parsed["settlement_fee"]
            close_today = parsed["close_today"]
            assert isinstance(trading_fee, ParsedFee)
            assert isinstance(settlement_fee, ParsedFee)
            assert isinstance(close_today, ParsedCloseToday)
            row.update(
                {
                    "instrument_type": "option",
                    "parameter_scope": "series",
                    "contract_or_series": parsed["contract"],
                    "product": product,
                    "option_margin_adjustment_rate": parsed["margin_adjustment"],
                    "option_minimum_guarantee_coefficient": parsed["minimum_guarantee"],
                    "trading_fee_value": trading_fee.value,
                    "trading_fee_unit": trading_fee.unit.value,
                    "settlement_fee_value": settlement_fee.value,
                    "settlement_fee_unit": settlement_fee.unit.value,
                    "settlement_fee_kind": "exercise",
                    "close_today_fee_multiplier": close_today.multiplier,
                    "close_today_fee_semantics": close_today.semantics,
                    "raw_contract_or_series": raw[0],
                    "raw_margin_adjustment_coefficient": raw[1],
                    "raw_minimum_guarantee_coefficient": raw[2],
                    "raw_trading_fee_standard": raw[3],
                    "raw_exercise_fee_standard": raw[4],
                    "raw_close_today_charge_rate": raw[5],
                    "raw_source_fields_json": json.dumps(
                        parsed["raw_fields"], ensure_ascii=False, separators=(",", ":")
                    ),
                    "source_section_title_date": options_title_date,
                    "source_section_title_matches_snapshot": options_title_date == normalized_date,
                }
            )
            rows.append(row)

    if not rows:
        raise CffexContentError(f"CFFEX settlement CSV has no equity-index parameter rows: {official_path}")
    frame = _frame_with_stable_dtypes(rows)
    validate_normalized_settlement_frame(frame, expected_source_hashes={digest})
    return frame.sort_values(
        ["snapshot_date", "instrument_type", "product", "contract_or_series"],
        kind="stable",
    ).reset_index(drop=True)


def validate_normalized_settlement_frame(
    frame: pd.DataFrame,
    *,
    expected_source_hashes: set[str] | None = None,
) -> None:
    missing = sorted(set(NORMALIZED_COLUMNS) - set(frame.columns))
    if missing:
        raise CffexContentError(f"normalized CFFEX frame missing columns: {','.join(missing)}")
    if frame.empty:
        raise CffexContentError("normalized CFFEX settlement frame is empty")
    keys = ["snapshot_date", "instrument_type", "contract_or_series"]
    if frame.duplicated(keys).any():
        raise CffexContentError("duplicate snapshot/instrument key in CFFEX settlement parameters")
    for value in frame["snapshot_date"].astype(str).unique():
        normalize_snapshot_date(value)
    title_dates = frame["source_section_title_date"].astype(str)
    snapshots = frame["snapshot_date"].astype(str)
    for value in title_dates.unique():
        normalize_snapshot_date(value)
    if bool(title_dates.gt(snapshots).any()):
        raise CffexContentError("source section title date may not be later than snapshot_date")
    expected_title_matches = title_dates.eq(snapshots)
    observed_title_matches = frame["source_section_title_matches_snapshot"].astype("boolean")
    if observed_title_matches.isna().any() or not bool(observed_title_matches.eq(expected_title_matches).all()):
        raise CffexContentError("source section title match flags are inconsistent")
    products = set(frame["product"].astype(str))
    if not products.issubset(EQUITY_PRODUCTS):
        raise CffexContentError(f"unknown equity-scope CFFEX products: {sorted(products - EQUITY_PRODUCTS)}")
    futures = frame["instrument_type"].astype(str).eq("future")
    options = frame["instrument_type"].astype(str).eq("option")
    if not bool((futures | options).all()):
        raise CffexContentError("unknown CFFEX instrument_type")
    if not set(frame.loc[futures, "product"].astype(str)).issubset(EQUITY_FUTURE_PRODUCTS):
        raise CffexContentError("future rows contain a non-future equity product")
    if not set(frame.loc[options, "product"].astype(str)).issubset(EQUITY_OPTION_PRODUCTS):
        raise CffexContentError("option rows contain a non-option equity product")
    if futures.any() and set(frame.loc[futures, "parameter_scope"].astype(str)) != {"contract"}:
        raise CffexContentError("future rows must use contract parameter scope")
    if options.any() and set(frame.loc[options, "parameter_scope"].astype(str)) != {"series"}:
        raise CffexContentError("option rows must use series parameter scope")
    if futures.any() and set(frame.loc[futures, "settlement_fee_kind"].astype(str)) != {"delivery"}:
        raise CffexContentError("future rows must label settlement fees as delivery fees")
    if options.any() and set(frame.loc[options, "settlement_fee_kind"].astype(str)) != {"exercise"}:
        raise CffexContentError("option rows must label settlement fees as exercise fees")
    for row in frame.loc[:, ["instrument_type", "contract_or_series", "product"]].itertuples(index=False):
        pattern = _FUTURE_CONTRACT if row.instrument_type == "future" else _OPTION_SERIES
        match = pattern.fullmatch(str(row.contract_or_series))
        if not match or match.group("product") != str(row.product):
            raise CffexContentError(
                f"CFFEX identifier/product disagreement: {row.contract_or_series}/{row.product}"
            )
    for mask, columns in (
        (futures, ("long_margin_rate", "short_margin_rate")),
        (options, ("option_margin_adjustment_rate", "option_minimum_guarantee_coefficient")),
    ):
        for column in columns:
            values = pd.to_numeric(frame.loc[mask, column], errors="coerce")
            if values.isna().any() or not bool(values.map(math.isfinite).all()) or not bool(values.gt(0.0).all()):
                raise CffexContentError(f"{column} must be positive and finite")
    valid_units = {FeeUnit.NOTIONAL_RATE.value, FeeUnit.CURRENCY_PER_CONTRACT.value}
    for prefix in ("trading_fee", "settlement_fee"):
        units = set(frame[f"{prefix}_unit"].astype(str))
        if not units.issubset(valid_units):
            raise CffexContentError(f"unsupported {prefix} units: {sorted(units - valid_units)}")
        values = pd.to_numeric(frame[f"{prefix}_value"], errors="coerce")
        if values.isna().any() or not bool(values.map(math.isfinite).all()) or not bool(values.gt(0.0).all()):
            raise CffexContentError(f"{prefix}_value must be positive and finite")
    close_values = pd.to_numeric(frame["close_today_fee_multiplier"], errors="coerce")
    if close_values.isna().any() or not bool(close_values.map(math.isfinite).all()) or not bool(close_values.ge(0.0).all()):
        raise CffexContentError("close_today_fee_multiplier must be non-negative and finite")
    if set(frame["close_today_fee_semantics"].astype(str)) != {"fraction_of_trading_fee"}:
        raise CffexContentError("unsupported close-today fee semantics")
    if bool(frame["option_shorting_enabled"].fillna(True).astype(bool).any()):
        raise CffexContentError("short options must remain disabled")
    hashes = set(frame["source_sha256"].astype(str))
    if not hashes or any(not _HASH.fullmatch(value) for value in hashes):
        raise CffexContentError("invalid or missing source SHA-256")
    if expected_source_hashes is not None and hashes != set(expected_source_hashes):
        raise CffexContentError(
            f"source hash set mismatch: expected={sorted(expected_source_hashes)} found={sorted(hashes)}"
        )
    for row in frame.loc[
        :, ["snapshot_date", "source_url", "source_official_path", "source_file"]
    ].drop_duplicates().itertuples(index=False):
        path_date, filename = validate_official_csv_path(str(row.source_official_path))
        if path_date != str(row.snapshot_date):
            raise CffexContentError("source official path date does not match snapshot_date")
        if str(row.source_url) != CFFEX_ORIGIN + str(row.source_official_path):
            raise CffexContentError("source URL is not the official CFFEX URL for its path")
        if str(row.source_file) != filename:
            raise CffexContentError("source filename does not match its official CFFEX path")


def validate_settlement_csv_payload(
    payload: bytes,
    snapshot_date: object,
    *,
    source_official_path: str | None = None,
    source_url: str | None = None,
) -> pd.DataFrame:
    return parse_cffex_settlement_csv(
        payload,
        snapshot_date,
        source_official_path=source_official_path,
        source_url=source_url,
    )


def settlement_local_path(raw_root: str | Path, source: DiscoveredSettlementCsv) -> Path:
    snapshot_date, filename = validate_official_csv_path(source.official_path)
    if snapshot_date != source.snapshot_date:
        raise CffexDiscoveryError("discovered record date no longer matches its official path")
    return Path(raw_root) / snapshot_date[:6] / snapshot_date[6:] / filename


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_bytes(path, encoded)


def _empty_download_manifest(
    expected_pages: Sequence[int], expected_unique_csvs: int
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact_type": "cffex_official_settlement_parameter_downloads",
        "official_origin": CFFEX_ORIGIN,
        "expected_pages": list(expected_pages),
        "expected_unique_csvs": int(expected_unique_csvs),
        "updated_at_utc": _utc_now(),
        "records": [],
    }


def load_settlement_download_manifest(path: str | Path) -> dict[str, object]:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CffexSettlementError(f"CFFEX download manifest is unreadable: {manifest_path}") from exc
    if int(payload.get("schema_version", 0)) != 1 or not isinstance(payload.get("records"), list):
        raise CffexSettlementError(f"unsupported CFFEX download manifest: {manifest_path}")
    return payload


def write_settlement_download_manifest(
    path: str | Path,
    records: Iterable[SettlementDownloadRecord],
    *,
    expected_pages: Sequence[int] = CFFEX_PAGE_NUMBERS,
    expected_unique_csvs: int = CFFEX_EXPECTED_UNIQUE_CSVS,
) -> Path:
    manifest_path = Path(path)
    expected = list(validate_page_numbers(expected_pages, expected_total_pages=max(expected_pages)))
    if manifest_path.exists():
        manifest = load_settlement_download_manifest(manifest_path)
        if manifest.get("expected_pages") != expected or int(manifest.get("expected_unique_csvs", -1)) != int(
            expected_unique_csvs
        ):
            raise CffexSettlementError("CFFEX download manifest completeness contract drifted")
    else:
        manifest = _empty_download_manifest(expected, expected_unique_csvs)
    indexed = {str(item["official_path"]): dict(item) for item in manifest["records"]}
    for record in records:
        validate_official_csv_path(record.official_path)
        indexed[record.official_path] = asdict(record)
    manifest["records"] = sorted(
        indexed.values(), key=lambda item: (str(item["snapshot_date"]), str(item["official_path"]))
    )
    manifest["updated_at_utc"] = _utc_now()
    _atomic_write_json(manifest_path, manifest)
    return manifest_path


def _record_from_mapping(value: Mapping[str, object]) -> SettlementDownloadRecord:
    return SettlementDownloadRecord(
        official_path=str(value.get("official_path", "")),
        url=str(value.get("url", "")),
        snapshot_date=str(value.get("snapshot_date", "")),
        discovery_pages=tuple(int(page) for page in value.get("discovery_pages", [])),
        local_path=str(value.get("local_path", "")),
        bytes=int(value.get("bytes", 0)),
        sha256=str(value.get("sha256", "")),
        local_ip=str(value.get("local_ip", "")),
        remote_ip=str(value.get("remote_ip", "")),
        interface=str(value.get("interface", "")),
        status=str(value.get("status", "")),
        checked_at_utc=str(value.get("checked_at_utc", "")),
        fetched_at_utc=str(value.get("fetched_at_utc", "")),
        error=str(value.get("error", "")),
        quarantine_path=str(value.get("quarantine_path", "")),
    )


def successful_download_records(path: str | Path) -> list[SettlementDownloadRecord]:
    manifest = load_settlement_download_manifest(path)
    return [
        record
        for record in (_record_from_mapping(item) for item in manifest["records"])
        if record.successful
    ]


def _route_fields(response: DirectHttpResponse, requested_interface: str) -> tuple[str, str, str]:
    route = getattr(response, "route", None)
    if route is None:
        return "", "", requested_interface
    connected_local = getattr(route, "connected_local", None)
    connected_peer = getattr(route, "connected_peer", None)
    local_ip = str(connected_local[0]) if connected_local else ""
    remote_ip = str(connected_peer[0]) if connected_peer else str(getattr(route, "resolved_ip", ""))
    return local_ip, remote_ip, str(getattr(route, "interface", requested_interface))


def _quarantine_path(path: Path, raw_root: Path, label: str) -> Path:
    try:
        relative = path.relative_to(raw_root)
    except ValueError:
        relative = Path(path.name)
    target = raw_root / "_quarantine" / relative.parent / f"{relative.name}.{label}.{time.time_ns()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _cached_record_matches(
    previous: SettlementDownloadRecord | None,
    source: DiscoveredSettlementCsv,
    local_path: Path,
    payload: bytes,
) -> bool:
    if previous is None or not previous.successful:
        return False
    return (
        previous.official_path == source.official_path
        and previous.url == source.url
        and previous.snapshot_date == source.snapshot_date
        and Path(previous.local_path) == local_path
        and previous.bytes == len(payload)
        and previous.sha256 == sha256_bytes(payload)
    )


def download_cffex_settlement_csv(
    source: DiscoveredSettlementCsv,
    *,
    raw_root: str | Path,
    manifest_path: str | Path,
    interface: str = "en0",
    dns_server: str | None = None,
    timeout_seconds: float = 60.0,
    max_csv_bytes: int = 5 * 1024 * 1024,
    expected_pages: Sequence[int] = CFFEX_PAGE_NUMBERS,
    expected_unique_csvs: int = CFFEX_EXPECTED_UNIQUE_CSVS,
    fetcher: Callable[..., DirectHttpResponse] = physical_http_get,
) -> SettlementDownloadRecord:
    root = Path(raw_root)
    target = settlement_local_path(root, source)
    previous: SettlementDownloadRecord | None = None
    manifest_file = Path(manifest_path)
    if manifest_file.exists():
        manifest = load_settlement_download_manifest(manifest_file)
        for item in manifest["records"]:
            if str(item.get("official_path")) == source.official_path:
                previous = _record_from_mapping(item)
                break

    quarantine = ""
    if target.exists():
        try:
            cached_payload = target.read_bytes()
            validate_settlement_csv_payload(
                cached_payload,
                source.snapshot_date,
                source_official_path=source.official_path,
                source_url=source.url,
            )
            cache_valid = _cached_record_matches(previous, source, target, cached_payload)
        except (OSError, CffexSettlementError, ValueError):
            cache_valid = False
            cached_payload = b""
        if cache_valid and previous is not None:
            record = SettlementDownloadRecord(
                official_path=source.official_path,
                url=source.url,
                snapshot_date=source.snapshot_date,
                discovery_pages=source.discovery_pages,
                local_path=str(target),
                bytes=len(cached_payload),
                sha256=sha256_bytes(cached_payload),
                local_ip=previous.local_ip,
                remote_ip=previous.remote_ip,
                interface=previous.interface or interface,
                status="cached_valid",
                checked_at_utc=_utc_now(),
                fetched_at_utc=previous.fetched_at_utc,
            )
            write_settlement_download_manifest(
                manifest_file,
                [record],
                expected_pages=expected_pages,
                expected_unique_csvs=expected_unique_csvs,
            )
            return record
        quarantined = _quarantine_path(target, root, "invalid")
        os.replace(target, quarantined)
        quarantine = str(quarantined)

    try:
        response = fetcher(
            source.url,
            interface=interface,
            dns_server=dns_server,
            timeout_seconds=timeout_seconds,
            max_bytes=max_csv_bytes,
        )
    except Exception as exc:
        checked_at = _utc_now()
        record = SettlementDownloadRecord(
            source.official_path,
            source.url,
            source.snapshot_date,
            source.discovery_pages,
            str(target),
            0,
            "",
            "",
            "",
            interface,
            "failed_network",
            checked_at,
            checked_at,
            str(exc),
            quarantine,
        )
        write_settlement_download_manifest(
            manifest_file,
            [record],
            expected_pages=expected_pages,
            expected_unique_csvs=expected_unique_csvs,
        )
        raise CffexSettlementError(
            f"physical-route CFFEX download failed for {source.snapshot_date}: {exc}"
        ) from exc
    local_ip, remote_ip, observed_interface = _route_fields(response, interface)
    checked_at = _utc_now()
    body = response.body
    body_hash = sha256_bytes(body)
    if int(response.status) != 200:
        record = SettlementDownloadRecord(
            source.official_path,
            source.url,
            source.snapshot_date,
            source.discovery_pages,
            str(target),
            len(body),
            body_hash,
            local_ip,
            remote_ip,
            observed_interface,
            "failed_http",
            checked_at,
            checked_at,
            f"HTTP {response.status} {response.reason}",
            quarantine,
        )
        write_settlement_download_manifest(
            manifest_file,
            [record],
            expected_pages=expected_pages,
            expected_unique_csvs=expected_unique_csvs,
        )
        raise CffexSettlementError(record.error)
    try:
        validate_settlement_csv_payload(
            body,
            source.snapshot_date,
            source_official_path=source.official_path,
            source_url=source.url,
        )
    except (CffexSettlementError, ValueError) as exc:
        invalid_response = _quarantine_path(target, root, "response-invalid")
        _atomic_write_bytes(invalid_response, body)
        record = SettlementDownloadRecord(
            source.official_path,
            source.url,
            source.snapshot_date,
            source.discovery_pages,
            str(target),
            len(body),
            body_hash,
            local_ip,
            remote_ip,
            observed_interface,
            "failed_validation",
            checked_at,
            checked_at,
            str(exc),
            str(invalid_response),
        )
        write_settlement_download_manifest(
            manifest_file,
            [record],
            expected_pages=expected_pages,
            expected_unique_csvs=expected_unique_csvs,
        )
        raise CffexContentError(f"invalid CFFEX settlement response for {source.snapshot_date}: {exc}") from exc

    _atomic_write_bytes(target, body)
    record = SettlementDownloadRecord(
        source.official_path,
        source.url,
        source.snapshot_date,
        source.discovery_pages,
        str(target),
        len(body),
        body_hash,
        local_ip,
        remote_ip,
        observed_interface,
        "replaced_invalid_cache" if quarantine else "downloaded",
        checked_at,
        checked_at,
        "",
        quarantine,
    )
    write_settlement_download_manifest(
        manifest_file,
        [record],
        expected_pages=expected_pages,
        expected_unique_csvs=expected_unique_csvs,
    )
    return record


def download_cffex_settlement_csvs(
    sources: Iterable[DiscoveredSettlementCsv],
    *,
    raw_root: str | Path,
    manifest_path: str | Path,
    interface: str = "en0",
    dns_server: str | None = None,
    timeout_seconds: float = 60.0,
    max_csv_bytes: int = 5 * 1024 * 1024,
    expected_pages: Sequence[int] = CFFEX_PAGE_NUMBERS,
    expected_unique_csvs: int = CFFEX_EXPECTED_UNIQUE_CSVS,
    fetcher: Callable[..., DirectHttpResponse] = physical_http_get,
) -> list[SettlementDownloadRecord]:
    ordered = sorted(sources, key=lambda item: (item.snapshot_date, item.official_path))
    if not ordered:
        raise CffexSettlementError("no CFFEX settlement CSVs were selected for download")
    records: list[SettlementDownloadRecord] = []
    for source in ordered:
        records.append(
            download_cffex_settlement_csv(
                source,
                raw_root=raw_root,
                manifest_path=manifest_path,
                interface=interface,
                dns_server=dns_server,
                timeout_seconds=timeout_seconds,
                max_csv_bytes=max_csv_bytes,
                expected_pages=expected_pages,
                expected_unique_csvs=expected_unique_csvs,
                fetcher=fetcher,
            )
        )
    return records


def settlement_artifact_manifest_path(parquet_path: str | Path) -> Path:
    path = Path(parquet_path)
    return path.with_suffix(path.suffix + ".manifest.json")


def resolve_settlement_output_path(
    canonical_parquet_path: str | Path,
    *,
    selected_pages: Sequence[int],
    expected_pages: Sequence[int] = CFFEX_PAGE_NUMBERS,
    start_date: object | None = None,
    end_date: object | None = None,
) -> Path:
    canonical = Path(canonical_parquet_path)
    expected = validate_page_numbers(expected_pages, expected_total_pages=max(expected_pages))
    selected = validate_page_numbers(selected_pages, expected_total_pages=max(expected))
    if not set(selected).issubset(expected):
        raise CffexArtifactError("selected CFFEX pages are outside the canonical page set")
    if selected == expected and start_date is None and end_date is None:
        return canonical
    parts: list[str] = []
    if selected != expected:
        parts.append(f"p{selected[0]:02d}-p{selected[-1]:02d}")
    if start_date is not None or end_date is not None:
        start = normalize_snapshot_date(start_date) if start_date is not None else "open"
        end = normalize_snapshot_date(end_date) if end_date is not None else "open"
        parts.append(f"d{start}-{end}")
    scope = ".scope-" + "-".join(parts or ["partial"])
    return canonical.with_name(f"{canonical.stem}{scope}{canonical.suffix}")


def _source_manifest_entries(records: Sequence[SettlementDownloadRecord]) -> list[dict[str, object]]:
    return [
        {
            "official_path": record.official_path,
            "url": record.url,
            "snapshot_date": record.snapshot_date,
            "discovery_pages": list(record.discovery_pages),
            "local_path": record.local_path,
            "bytes": record.bytes,
            "sha256": record.sha256,
            "local_ip": record.local_ip,
            "remote_ip": record.remote_ip,
            "interface": record.interface,
        }
        for record in records
    ]


def _stable_source_set_sha256(entries: Sequence[Mapping[str, object]]) -> str:
    stable = [
        {
            "official_path": entry["official_path"],
            "url": entry["url"],
            "snapshot_date": entry["snapshot_date"],
            "bytes": int(entry["bytes"]),
            "sha256": entry["sha256"],
        }
        for entry in entries
    ]
    return sha256_bytes(
        json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def build_cffex_settlement_artifact(
    records: Iterable[SettlementDownloadRecord | Mapping[str, object]],
    canonical_parquet_path: str | Path,
    *,
    selected_pages: Sequence[int] = CFFEX_PAGE_NUMBERS,
    expected_pages: Sequence[int] = CFFEX_PAGE_NUMBERS,
    expected_unique_csvs: int = CFFEX_EXPECTED_UNIQUE_CSVS,
    expected_first_snapshot: str | None = CFFEX_EXPECTED_FIRST_SNAPSHOT,
    expected_last_snapshot: str | None = CFFEX_EXPECTED_LAST_SNAPSHOT,
    start_date: object | None = None,
    end_date: object | None = None,
) -> SettlementArtifact:
    normalized_records = [
        record if isinstance(record, SettlementDownloadRecord) else _record_from_mapping(record)
        for record in records
    ]
    normalized_records.sort(key=lambda item: (item.snapshot_date, item.official_path))
    if not normalized_records or any(not record.successful for record in normalized_records):
        raise CffexArtifactError("artifact build requires successful CFFEX download records")
    if len({record.official_path for record in normalized_records}) != len(normalized_records):
        raise CffexArtifactError("artifact build has duplicate official source paths")
    if len({record.snapshot_date for record in normalized_records}) != len(normalized_records):
        raise CffexArtifactError("artifact build has duplicate snapshot dates")

    expected = validate_page_numbers(expected_pages, expected_total_pages=max(expected_pages))
    selected = validate_page_numbers(selected_pages, expected_total_pages=max(expected))
    output = resolve_settlement_output_path(
        canonical_parquet_path,
        selected_pages=selected,
        expected_pages=expected,
        start_date=start_date,
        end_date=end_date,
    )
    canonical = Path(canonical_parquet_path)
    is_canonical = output == canonical
    if not is_canonical and output == canonical:
        raise CffexArtifactError("partial CFFEX build may not overwrite the canonical artifact")
    if is_canonical:
        if len(normalized_records) != int(expected_unique_csvs):
            raise CffexArtifactError(
                f"canonical build requires {expected_unique_csvs} source CSVs, found {len(normalized_records)}"
            )
        discovery_coverage = set().union(*(set(record.discovery_pages) for record in normalized_records))
        if discovery_coverage != set(expected):
            raise CffexArtifactError(
                f"canonical build page coverage mismatch: expected={list(expected)} found={sorted(discovery_coverage)}"
            )
        if expected_first_snapshot is not None and normalized_records[0].snapshot_date != normalize_snapshot_date(
            expected_first_snapshot
        ):
            raise CffexArtifactError("canonical build first snapshot does not match the completeness contract")
        if expected_last_snapshot is not None and normalized_records[-1].snapshot_date != normalize_snapshot_date(
            expected_last_snapshot
        ):
            raise CffexArtifactError("canonical build last snapshot does not match the completeness contract")

    start = normalize_snapshot_date(start_date) if start_date is not None else None
    end = normalize_snapshot_date(end_date) if end_date is not None else None
    if start and end and start > end:
        raise CffexArtifactError("start_date must not exceed end_date")
    frames: list[pd.DataFrame] = []
    for record in normalized_records:
        if start and record.snapshot_date < start:
            raise CffexArtifactError("source record precedes the declared partial date scope")
        if end and record.snapshot_date > end:
            raise CffexArtifactError("source record exceeds the declared partial date scope")
        path = Path(record.local_path)
        if not path.is_file():
            raise CffexArtifactError(f"CFFEX source file is missing: {path}")
        payload = path.read_bytes()
        if len(payload) != record.bytes or sha256_bytes(payload) != record.sha256:
            raise CffexArtifactError(f"CFFEX source file hash/size drifted: {path}")
        frames.append(
            parse_cffex_settlement_csv(
                payload,
                record.snapshot_date,
                source_official_path=record.official_path,
                source_url=record.url,
            )
        )
    frame = pd.concat(frames, ignore_index=True, sort=False).loc[:, list(NORMALIZED_COLUMNS)]
    frame = _frame_with_stable_dtypes(frame.to_dict(orient="records"))
    expected_hashes = {record.sha256 for record in normalized_records}
    validate_normalized_settlement_frame(frame, expected_source_hashes=expected_hashes)
    frame = frame.sort_values(
        ["snapshot_date", "instrument_type", "product", "contract_or_series"], kind="stable"
    ).reset_index(drop=True)
    official_completeness_contract = (
        is_canonical
        and expected == CFFEX_PAGE_NUMBERS
        and int(expected_unique_csvs) == CFFEX_EXPECTED_UNIQUE_CSVS
    )
    if official_completeness_contract and set(frame["product"].astype(str)) != EQUITY_PRODUCTS:
        raise CffexArtifactError(
            f"canonical CFFEX build product coverage mismatch: found={sorted(frame['product'].astype(str).unique())}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = settlement_artifact_manifest_path(output)
    parquet_tmp = output.with_name(f".{output.stem}.{os.getpid()}.{time.time_ns()}.tmp.parquet")
    manifest_tmp = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    source_entries = _source_manifest_entries(normalized_records)
    try:
        frame.to_parquet(parquet_tmp, index=False, compression="zstd", engine="pyarrow")
        with parquet_tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        parquet_hash = sha256_file(parquet_tmp)
        manifest = {
            "schema_version": 1,
            "artifact_type": "cffex_official_settlement_parameters",
            "parquet_path": str(output),
            "parquet_bytes": int(parquet_tmp.stat().st_size),
            "parquet_sha256": parquet_hash,
            "rows": int(len(frame)),
            "columns": list(NORMALIZED_COLUMNS),
            "first_snapshot_date": str(frame["snapshot_date"].min()),
            "last_snapshot_date": str(frame["snapshot_date"].max()),
            "products": sorted(frame["product"].astype(str).unique()),
            "section_title_mismatch_rows": int(
                (~frame["source_section_title_matches_snapshot"].astype(bool)).sum()
            ),
            "section_title_mismatch_snapshots": sorted(
                frame.loc[
                    ~frame["source_section_title_matches_snapshot"].astype(bool), "snapshot_date"
                ].astype(str).unique()
            ),
            "parser_source_sha256": parser_source_sha256(),
            "source_set_sha256": _stable_source_set_sha256(source_entries),
            "source_count": len(source_entries),
            "sources": source_entries,
            "canonical_parquet_path": str(canonical),
            "is_canonical": is_canonical,
            "official_completeness_contract": official_completeness_contract,
            "scope": {
                "selected_pages": list(selected),
                "expected_pages": list(expected),
                "expected_unique_csvs": int(expected_unique_csvs),
                "start_date": start or "",
                "end_date": end or "",
            },
            "snapshot_semantics": "official publication snapshot; not legal effective_date",
            "close_today_fee_semantics": "decimal multiplier of the trading fee; values may exceed 1",
            "option_shorting_enabled": False,
            "created_at_utc": _utc_now(),
        }
        encoded = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with manifest_tmp.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(parquet_tmp, output)
        os.replace(manifest_tmp, manifest_path)
    finally:
        if parquet_tmp.exists():
            parquet_tmp.unlink()
        if manifest_tmp.exists():
            manifest_tmp.unlink()
    return SettlementArtifact(output, manifest_path, len(frame), len(source_entries), is_canonical)


def validate_cffex_settlement_artifact(
    parquet_path: str | Path,
    *,
    verify_sources: bool = True,
) -> dict[str, object]:
    output = Path(parquet_path)
    manifest_path = settlement_artifact_manifest_path(output)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CffexArtifactError(f"CFFEX settlement artifact manifest is unreadable: {manifest_path}") from exc
    if int(manifest.get("schema_version", 0)) != 1:
        raise CffexArtifactError("unsupported CFFEX settlement artifact manifest schema")
    if manifest.get("parser_source_sha256") != parser_source_sha256():
        raise CffexArtifactError("CFFEX settlement artifact was built by a stale parser")
    if not output.is_file() or int(manifest.get("parquet_bytes", -1)) != output.stat().st_size:
        raise CffexArtifactError("CFFEX settlement parquet is missing or its size drifted")
    if manifest.get("parquet_sha256") != sha256_file(output):
        raise CffexArtifactError("CFFEX settlement parquet hash does not match its manifest")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CffexArtifactError("CFFEX settlement artifact has no bound source list")
    if int(manifest.get("source_count", -1)) != len(sources):
        raise CffexArtifactError("CFFEX settlement source count does not match its manifest")
    if manifest.get("source_set_sha256") != _stable_source_set_sha256(sources):
        raise CffexArtifactError("CFFEX settlement source-set hash does not match its manifest")
    scope = manifest.get("scope")
    if not isinstance(scope, dict):
        raise CffexArtifactError("CFFEX settlement artifact scope is missing")
    if bool(manifest.get("is_canonical")):
        if str(output) != str(manifest.get("canonical_parquet_path")):
            raise CffexArtifactError("canonical CFFEX artifact path does not match its manifest")
        if len(sources) != int(scope.get("expected_unique_csvs", -1)):
            raise CffexArtifactError("canonical CFFEX artifact is incomplete")
        if scope.get("selected_pages") != scope.get("expected_pages"):
            raise CffexArtifactError("canonical CFFEX artifact has partial page scope")
        if scope.get("start_date") or scope.get("end_date"):
            raise CffexArtifactError("canonical CFFEX artifact has a partial date scope")
        if bool(manifest.get("official_completeness_contract")) and set(
            manifest.get("products", [])
        ) != EQUITY_PRODUCTS:
            raise CffexArtifactError("canonical CFFEX artifact does not cover all target equity products")
    elif output == Path(str(manifest.get("canonical_parquet_path"))):
        raise CffexArtifactError("partial CFFEX artifact overwrote the canonical path")

    expected_hashes: set[str] = set()
    if verify_sources:
        for source in sources:
            source_path = Path(str(source.get("local_path", "")))
            expected_hash = str(source.get("sha256", ""))
            if not source_path.is_file():
                raise CffexArtifactError(f"bound CFFEX source is missing: {source_path}")
            payload = source_path.read_bytes()
            if len(payload) != int(source.get("bytes", -1)) or sha256_bytes(payload) != expected_hash:
                raise CffexArtifactError(f"bound CFFEX source hash/size drifted: {source_path}")
            validate_settlement_csv_payload(
                payload,
                str(source["snapshot_date"]),
                source_official_path=str(source["official_path"]),
                source_url=str(source["url"]),
            )
            expected_hashes.add(expected_hash)
    else:
        expected_hashes = {str(source.get("sha256", "")) for source in sources}

    parquet = pq.ParquetFile(output)
    if list(parquet.schema.names) != list(NORMALIZED_COLUMNS):
        raise CffexArtifactError("CFFEX settlement parquet schema does not match the canonical schema")
    if int(manifest.get("rows", -1)) != int(parquet.metadata.num_rows):
        raise CffexArtifactError("CFFEX settlement parquet row count does not match its manifest")
    frame = pd.read_parquet(output)
    validate_normalized_settlement_frame(frame, expected_source_hashes=expected_hashes)
    return manifest


def latest_exact_settlement_record(
    data: pd.DataFrame | str | Path,
    contract_or_series: str,
    trade_date: object,
    *,
    instrument_type: str | None = None,
    position_side: str | None = None,
    verify_artifact_sources: bool = True,
) -> pd.Series:
    identifier = str(contract_or_series).strip().upper()
    if not identifier or identifier in EQUITY_PRODUCTS:
        raise CffexLookupError("an exact contract or option-series identifier is required; product substitution is forbidden")
    cutoff = normalize_snapshot_date(trade_date)
    if isinstance(data, pd.DataFrame):
        frame = data
    else:
        validate_cffex_settlement_artifact(data, verify_sources=verify_artifact_sources)
        frame = pd.read_parquet(data)
    required = {"snapshot_date", "instrument_type", "contract_or_series", "product"}
    if not required.issubset(frame.columns):
        raise CffexLookupError(f"settlement parameter data is missing lookup columns: {sorted(required - set(frame.columns))}")
    matches = frame.loc[frame["contract_or_series"].astype(str).eq(identifier)].copy()
    if instrument_type is not None:
        requested_type = str(instrument_type).strip().lower()
        matches = matches.loc[matches["instrument_type"].astype(str).eq(requested_type)]
    matches = matches.loc[matches["snapshot_date"].astype(str).le(cutoff)]
    if matches.empty:
        raise CffexLookupError(
            f"no exact settlement parameter record for {identifier} on or before {cutoff}; future backfill and product substitution are forbidden"
        )
    latest_date = str(matches["snapshot_date"].astype(str).max())
    latest = matches.loc[matches["snapshot_date"].astype(str).eq(latest_date)]
    if len(latest) != 1:
        raise CffexLookupError(f"ambiguous exact settlement parameter record for {identifier} at {latest_date}")
    record = latest.iloc[0].copy()
    side = str(position_side or "").strip().lower()
    if str(record["instrument_type"]) == "option" and side in {"short", "sell_to_open", "short_open"}:
        raise ShortOptionsDisabledError("option short-margin parameters are retained for provenance, but short options are disabled")
    return record

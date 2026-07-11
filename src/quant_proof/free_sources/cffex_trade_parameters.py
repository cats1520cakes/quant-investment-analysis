from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from quant_proof.free_sources.cffex_adapter import (
    CFFEX_PRODUCTS,
    CffexDataError,
    validate_cffex_contract_master_manifest,
    validate_cffex_panel_manifest,
)
from quant_proof.network_guard import DirectHttpResponse, physical_http_get


CFFEX_TRADE_PARAMETERS_URL = (
    "http://www.cffex.com.cn/sj/jycs/{yyyymm}/{dd}/{yyyymmdd}_1.csv"
)
DEFAULT_RAW_RELATIVE_ROOT = "raw/cffex/trade_parameters"
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

SOURCE_COLUMNS: tuple[str, ...] = (
    "合约代码",
    "合约月份",
    "挂盘基准价",
    "上市日",
    "最后交易日",
    "涨停板幅度（%）",
    "跌停板幅度（%）",
    "涨停板价位",
    "跌停板价位",
    "持仓限额",
)
RAW_COLUMN_NAMES: Mapping[str, str] = {
    "合约代码": "raw_contract",
    "合约月份": "raw_contract_month",
    "挂盘基准价": "raw_basis_price",
    "上市日": "raw_open_date",
    "最后交易日": "raw_official_last_trade_date",
    "涨停板幅度（%）": "raw_upper_limit_percentage",
    "跌停板幅度（%）": "raw_lower_limit_percentage",
    "涨停板价位": "raw_upper_limit_price",
    "跌停板价位": "raw_lower_limit_price",
    "持仓限额": "position_limit_raw",
}
TRADE_PARAMETER_COLUMNS: tuple[str, ...] = (
    "snapshot_date",
    "contract",
    "product",
    "contract_month",
    "basis_price",
    "open_date",
    "official_last_trade_date",
    "upper_limit_percentage",
    "lower_limit_percentage",
    "upper_limit_price",
    "lower_limit_price",
    "position_limit_raw",
    "position_limit",
    "source_sha256",
    "source_url",
    "source_file",
    "raw_contract",
    "raw_contract_month",
    "raw_basis_price",
    "raw_open_date",
    "raw_official_last_trade_date",
    "raw_upper_limit_percentage",
    "raw_lower_limit_percentage",
    "raw_upper_limit_price",
    "raw_lower_limit_price",
    "raw_record_json",
)
STATIC_METADATA_COLUMNS: tuple[str, ...] = (
    "product",
    "contract_month",
    "basis_price",
    "open_date",
)
EXPIRY_AUDIT_COLUMNS: tuple[str, ...] = (
    "expiry_first",
    "expiry_latest",
    "expiry_revision_count",
    "expiry_changed",
)
CONTRACT_METADATA_COLUMNS: tuple[str, ...] = (
    *TRADE_PARAMETER_COLUMNS,
    *EXPIRY_AUDIT_COLUMNS,
)
DOWNLOAD_MANIFEST_COLUMNS: tuple[str, ...] = (
    "snapshot_date",
    "url",
    "path",
    "status",
    "bytes",
    "sha256",
    "rows",
    "contracts",
    "products",
    "title_date",
    "local_ip",
    "remote_ip",
    "resolved_ip",
    "interface",
    "interface_ipv4",
    "dns_server",
    "route_interface",
)

_TITLE_PATTERN = re.compile(r"^合约交易业务参数表[（(](?P<date>\d{8})[）)]$")
_SOURCE_FILE_PATTERN = re.compile(r"^(?P<date>\d{8})_1\.csv$")
_FUTURE_PATTERN = re.compile(r"^(?P<product>IF|IH|IC|IM)(?P<month>\d{4})$")
_OPTION_PATTERN = re.compile(
    r"^(?P<product>IO|HO|MO)(?P<month>\d{4})-(?P<option_type>[CP])-(?P<strike>\d+(?:\.\d+)?)$"
)
_NON_TARGET_CFFEX_FUTURE_PATTERN = re.compile(r"^(?:T|TF|TS|TL)\d{4}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MISSING_MARKERS = frozenset({"", "--", "-", "—", "－"})
_ROUTE_COLUMNS = (
    "local_ip",
    "remote_ip",
    "resolved_ip",
    "interface_ipv4",
    "dns_server",
    "route_interface",
)


class CffexTradeParameterError(CffexDataError):
    """Raised when official CFFEX trade parameters fail an exactness gate."""


@dataclass(frozen=True)
class CffexTradeParameterDownload:
    snapshot_date: str
    url: str
    path: str
    status: str
    bytes: int
    sha256: str
    rows: int
    contracts: int
    products: str
    title_date: str
    local_ip: str
    remote_ip: str
    resolved_ip: str
    interface: str
    interface_ipv4: str
    dns_server: str
    route_interface: str


@dataclass(frozen=True)
class CffexTradeParameterReconciliation:
    details: pd.DataFrame
    summary: Mapping[str, int]

    @property
    def historical_differences(self) -> pd.DataFrame:
        return self.details.loc[
            self.details["classification"].eq("historical_difference")
        ].copy()

    @property
    def right_censored_corrections(self) -> pd.DataFrame:
        return self.details.loc[
            self.details["classification"].eq("right_censored_correction")
        ].copy()

    @property
    def missing_contracts(self) -> pd.DataFrame:
        return self.details.loc[
            self.details["classification"].eq("missing_official")
        ].copy()

    @property
    def extra_contracts(self) -> pd.DataFrame:
        return self.details.loc[
            self.details["classification"].eq("extra_official")
        ].copy()

    @property
    def is_complete(self) -> bool:
        blocking = {
            "historical_difference",
            "right_censored_conflict",
            "missing_official",
            "extra_official",
        }
        return not self.details["classification"].isin(blocking).any()

    def to_frame(self) -> pd.DataFrame:
        return self.details.copy()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _normalize_yyyymmdd(value: object, field: str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"{field} must use YYYYMMDD")
    try:
        parsed = datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid YYYYMMDD date") from exc
    if parsed.strftime("%Y%m%d") != text:
        raise ValueError(f"{field} must be a valid YYYYMMDD date")
    return text


def cffex_trade_parameters_url(snapshot_date: object) -> str:
    normalized = _normalize_yyyymmdd(snapshot_date, "snapshot_date")
    return CFFEX_TRADE_PARAMETERS_URL.format(
        yyyymm=normalized[:6],
        dd=normalized[6:],
        yyyymmdd=normalized,
    )


def cffex_trade_parameter_path(
    data_root: str | Path,
    snapshot_date: object,
    raw_relative_root: str | Path = DEFAULT_RAW_RELATIVE_ROOT,
) -> Path:
    normalized = _normalize_yyyymmdd(snapshot_date, "snapshot_date")
    raw_root = Path(raw_relative_root)
    if raw_root.is_absolute():
        raise ValueError("raw_relative_root must be relative to data_root")
    return (
        Path(data_root).expanduser()
        / raw_root
        / normalized[:6]
        / normalized[6:]
        / f"{normalized}_1.csv"
    )


def _decode_gb18030(payload: bytes) -> str:
    if not payload:
        raise CffexTradeParameterError("CFFEX trade-parameter CSV is empty")
    try:
        text = payload.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise CffexTradeParameterError(
            "CFFEX trade-parameter CSV is not valid GB18030"
        ) from exc
    return text.lstrip("\ufeff")


def _title_and_csv_text(payload: bytes, expected_date: str) -> tuple[str, str]:
    text = _decode_gb18030(payload)
    probe = text.lstrip().lower()[:4096]
    if any(marker in probe for marker in ("<!doctype", "<html", "<head", "<body")):
        raise CffexTradeParameterError("CFFEX trade-parameter response is HTML")
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if len(lines) < 2:
        raise CffexTradeParameterError(
            "CFFEX trade-parameter CSV is missing its title or header"
        )
    title = lines[0].strip()
    match = _TITLE_PATTERN.fullmatch(title)
    if not match:
        raise CffexTradeParameterError(
            "CFFEX trade-parameter CSV has an invalid title"
        )
    title_date = _normalize_yyyymmdd(match.group("date"), "title_date")
    if title_date != expected_date:
        raise CffexTradeParameterError(
            f"CFFEX title date mismatch: expected {expected_date}, found {title_date}"
        )
    return title_date, "\n".join(lines[1:])


def _validate_source_filename(source_file: str | Path | None, expected_date: str) -> None:
    if source_file is None or not str(source_file).strip():
        return
    name = Path(str(source_file)).name
    match = _SOURCE_FILE_PATTERN.fullmatch(name)
    if not match:
        raise CffexTradeParameterError(
            f"CFFEX trade-parameter source path has an invalid filename: {name}"
        )
    path_date = _normalize_yyyymmdd(match.group("date"), "source_path_date")
    if path_date != expected_date:
        raise CffexTradeParameterError(
            f"CFFEX source path date mismatch: expected {expected_date}, found {path_date}"
        )


def _parse_number_series(
    raw: pd.Series,
    field: str,
    *,
    allow_missing: bool,
    percentage: bool = False,
) -> pd.Series:
    stripped = raw.astype("string").fillna("").str.strip()
    missing = stripped.isin(_MISSING_MARKERS)
    cleaned = stripped.copy()
    if percentage:
        cleaned = cleaned.str.replace("％", "%", regex=False).str.removesuffix("%").str.strip()
    values = pd.to_numeric(cleaned.mask(missing), errors="coerce")
    invalid = ~missing & values.isna()
    if invalid.any():
        samples = ",".join(stripped.loc[invalid].astype(str).head(3))
        raise CffexTradeParameterError(
            f"CFFEX {field} contains non-numeric values: {samples}"
        )
    finite_nonnegative = values.isna() | (np.isfinite(values) & values.ge(0.0))
    if not finite_nonnegative.all():
        raise CffexTradeParameterError(
            f"CFFEX {field} must be finite and non-negative"
        )
    if not allow_missing and values.isna().any():
        raise CffexTradeParameterError(f"CFFEX {field} must not be missing")
    return values.astype(float)


def parse_position_limit(value: object) -> int | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    if text in _MISSING_MARKERS:
        return None
    tokens = re.findall(r"(?<![\d.])\d[\d,]*(?![\d.])", text)
    if len(tokens) != 1:
        return None
    normalized = tokens[0].replace(",", "")
    try:
        parsed = int(normalized)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _parse_contracts(raw_contracts: pd.Series) -> pd.DataFrame:
    contracts = raw_contracts.astype("string").fillna("").str.strip()
    futures = contracts.str.extract(_FUTURE_PATTERN)
    options = contracts.str.extract(_OPTION_PATTERN)
    valid_future = futures["product"].notna()
    valid_option = options["product"].notna()
    invalid = ~(valid_future | valid_option)
    if invalid.any():
        samples = ",".join(contracts.loc[invalid].astype(str).head(5))
        raise CffexTradeParameterError(
            f"CFFEX trade parameters contain invalid exact contract codes: {samples}"
        )
    return pd.DataFrame(
        {
            "contract": contracts,
            "product": futures["product"].fillna(options["product"]).astype("string"),
            "contract_month": futures["month"].fillna(options["month"]).astype("string"),
        },
        index=raw_contracts.index,
    )


def parse_cffex_trade_parameters_csv(
    payload: bytes,
    snapshot_date: object,
    *,
    source_file: str | Path | None = None,
    source_url: str | None = None,
    source_sha256: str | None = None,
) -> pd.DataFrame:
    normalized_date = _normalize_yyyymmdd(snapshot_date, "snapshot_date")
    _validate_source_filename(source_file, normalized_date)
    _, csv_text = _title_and_csv_text(payload, normalized_date)
    try:
        raw = pd.read_csv(
            StringIO(csv_text),
            dtype="string",
            keep_default_na=False,
        )
    except (pd.errors.ParserError, UnicodeError, ValueError) as exc:
        raise CffexTradeParameterError(
            "CFFEX trade-parameter CSV body is not parseable"
        ) from exc
    raw.columns = [str(column).lstrip("\ufeff").strip() for column in raw.columns]
    if raw.columns.duplicated().any():
        raise CffexTradeParameterError(
            "CFFEX trade-parameter CSV has duplicate headers"
        )
    missing = [column for column in SOURCE_COLUMNS if column not in raw.columns]
    if missing:
        raise CffexTradeParameterError(
            f"CFFEX trade-parameter CSV missing columns: {','.join(missing)}"
        )
    if tuple(raw.columns[: len(SOURCE_COLUMNS)]) != SOURCE_COLUMNS:
        raise CffexTradeParameterError(
            "CFFEX trade-parameter CSV header order has changed"
        )
    if raw.empty:
        raise CffexTradeParameterError("CFFEX trade-parameter CSV has no rows")

    raw_contracts = raw["合约代码"].astype("string").fillna("").str.strip()
    target_contract = raw_contracts.str.match(_FUTURE_PATTERN) | raw_contracts.str.match(
        _OPTION_PATTERN
    )
    known_non_target = raw_contracts.str.match(_NON_TARGET_CFFEX_FUTURE_PATTERN)
    unknown_contract = ~(target_contract | known_non_target)
    if unknown_contract.any():
        samples = ",".join(raw_contracts.loc[unknown_contract].astype(str).head(5))
        raise CffexTradeParameterError(
            f"CFFEX trade parameters contain invalid exact contract codes: {samples}"
        )
    raw = raw.loc[target_contract].copy()
    if raw.empty:
        raise CffexTradeParameterError(
            "CFFEX trade-parameter CSV has no target index futures/options rows"
        )

    actual_hash = _sha256_bytes(payload)
    if source_sha256 is not None:
        supplied_hash = str(source_sha256).strip().lower()
        if supplied_hash != actual_hash:
            raise CffexTradeParameterError(
                "CFFEX trade-parameter payload does not match source_sha256"
            )

    parsed_contracts = _parse_contracts(raw["合约代码"])
    frame = parsed_contracts.copy()
    raw_month = raw["合约月份"].astype("string").fillna("").str.strip()
    if not raw_month.str.fullmatch(r"\d{4}").all():
        raise CffexTradeParameterError(
            "CFFEX trade parameters contain invalid contract months"
        )
    if not raw_month.eq(frame["contract_month"]).all():
        raise CffexTradeParameterError(
            "CFFEX contract code and contract month disagree"
        )

    frame["snapshot_date"] = normalized_date
    frame["basis_price"] = _parse_number_series(
        raw["挂盘基准价"], "basis_price", allow_missing=False
    )
    for source_column, output_column in (
        ("上市日", "open_date"),
        ("最后交易日", "official_last_trade_date"),
    ):
        try:
            frame[output_column] = raw[source_column].map(
                lambda value: _normalize_yyyymmdd(value, output_column)
            )
        except ValueError as exc:
            raise CffexTradeParameterError(str(exc)) from exc
    frame["upper_limit_percentage"] = _parse_number_series(
        raw["涨停板幅度（%）"],
        "upper_limit_percentage",
        allow_missing=True,
        percentage=True,
    )
    frame["lower_limit_percentage"] = _parse_number_series(
        raw["跌停板幅度（%）"],
        "lower_limit_percentage",
        allow_missing=True,
        percentage=True,
    )
    frame["upper_limit_price"] = _parse_number_series(
        raw["涨停板价位"], "upper_limit_price", allow_missing=False
    )
    frame["lower_limit_price"] = _parse_number_series(
        raw["跌停板价位"], "lower_limit_price", allow_missing=False
    )
    frame["position_limit_raw"] = (
        raw["持仓限额"].astype("string").fillna("").str.strip()
    )
    frame["position_limit"] = pd.array(
        frame["position_limit_raw"].map(parse_position_limit), dtype="Int64"
    )
    frame["source_sha256"] = actual_hash
    frame["source_url"] = source_url or cffex_trade_parameters_url(normalized_date)
    frame["source_file"] = "" if source_file is None else str(source_file)

    for source_column, raw_name in RAW_COLUMN_NAMES.items():
        if raw_name == "position_limit_raw":
            continue
        frame[raw_name] = raw[source_column].astype("string").fillna("")
    raw_columns = list(raw.columns)
    frame["raw_record_json"] = raw.loc[:, raw_columns].apply(
        lambda row: json.dumps(
            {column: str(row[column]) for column in raw_columns},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        axis=1,
    )
    frame = frame.loc[:, list(TRADE_PARAMETER_COLUMNS)].reset_index(drop=True)
    validate_cffex_trade_parameter_rows(frame)
    return frame


def validate_cffex_trade_parameter_rows(frame: pd.DataFrame) -> None:
    missing = sorted(set(TRADE_PARAMETER_COLUMNS) - set(frame.columns))
    if missing:
        raise CffexTradeParameterError(
            f"CFFEX trade-parameter rows missing columns: {','.join(missing)}"
        )
    if frame.empty:
        raise CffexTradeParameterError("CFFEX trade-parameter rows are empty")
    if frame.duplicated(["snapshot_date", "contract"]).any():
        raise CffexTradeParameterError(
            "duplicate CFFEX snapshot_date/contract rows"
        )

    normalized_dates: dict[str, pd.Series] = {}
    for column in ("snapshot_date", "open_date", "official_last_trade_date"):
        try:
            normalized_dates[column] = frame[column].map(
                lambda value: _normalize_yyyymmdd(value, column)
            )
        except ValueError as exc:
            raise CffexTradeParameterError(str(exc)) from exc
    if not (
        normalized_dates["open_date"].le(normalized_dates["snapshot_date"])
        & normalized_dates["snapshot_date"].le(
            normalized_dates["official_last_trade_date"]
        )
    ).all():
        raise CffexTradeParameterError(
            "CFFEX rows must satisfy open_date <= snapshot_date <= official_last_trade_date"
        )

    parsed_contracts = _parse_contracts(frame["contract"])
    if not parsed_contracts["product"].eq(frame["product"].astype("string")).all():
        raise CffexTradeParameterError("CFFEX contract and product disagree")
    if not parsed_contracts["contract_month"].eq(
        frame["contract_month"].astype("string")
    ).all():
        raise CffexTradeParameterError("CFFEX contract and contract_month disagree")
    if not frame["product"].isin(CFFEX_PRODUCTS).all():
        raise CffexTradeParameterError("CFFEX rows contain unsupported products")

    for column in (
        "basis_price",
        "upper_limit_price",
        "lower_limit_price",
    ):
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or (~np.isfinite(values) | values.lt(0.0)).any():
            raise CffexTradeParameterError(
                f"CFFEX {column} must be finite and non-negative"
            )
    for column in ("upper_limit_percentage", "lower_limit_percentage"):
        original = frame[column]
        values = pd.to_numeric(original, errors="coerce")
        invalid = (original.notna() & values.isna()) | (
            values.notna() & (~np.isfinite(values) | values.lt(0.0))
        )
        if invalid.any():
            raise CffexTradeParameterError(
                f"CFFEX {column} must be missing or finite and non-negative"
            )
    raw_position_limits = frame["position_limit"]
    position_limits = pd.to_numeric(raw_position_limits, errors="coerce")
    invalid_limits = (raw_position_limits.notna() & position_limits.isna()) | (
        position_limits.notna()
        & (
            ~np.isfinite(position_limits)
            | position_limits.lt(0.0)
            | position_limits.ne(np.floor(position_limits))
        )
    )
    if invalid_limits.any():
        raise CffexTradeParameterError(
            "CFFEX position_limit must be a non-negative integer when parsed"
        )
    hashes = frame["source_sha256"].astype(str).str.lower()
    if not hashes.str.fullmatch(_SHA256_PATTERN).all():
        raise CffexTradeParameterError("CFFEX rows contain invalid source hashes")
    if frame.groupby("snapshot_date", sort=False)["source_sha256"].nunique().gt(1).any():
        raise CffexTradeParameterError(
            "a CFFEX snapshot is bound to multiple source hashes"
        )

    static_counts = frame.groupby("contract", sort=True, observed=True)[
        list(STATIC_METADATA_COLUMNS)
    ].nunique(dropna=False)
    changed = static_counts.gt(1)
    if changed.any().any():
        contracts = changed.index[changed.any(axis=1)].astype(str).tolist()
        fields = changed.columns[changed.any(axis=0)].astype(str).tolist()
        raise CffexTradeParameterError(
            "CFFEX static contract metadata changes across snapshots: "
            f"contracts={','.join(contracts[:5])} fields={','.join(fields)}"
        )


def validate_cffex_trade_parameter_csv(
    path: str | Path,
    snapshot_date: object,
) -> pd.DataFrame:
    source_path = Path(path)
    normalized = _normalize_yyyymmdd(snapshot_date, "snapshot_date")
    _validate_source_filename(source_path, normalized)
    try:
        payload = source_path.read_bytes()
    except OSError as exc:
        raise CffexTradeParameterError(
            f"CFFEX trade-parameter cache is unreadable: {source_path}"
        ) from exc
    return parse_cffex_trade_parameters_csv(
        payload,
        normalized,
        source_file=source_path,
        source_url=cffex_trade_parameters_url(normalized),
    )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _download_summary(
    frame: pd.DataFrame,
    *,
    snapshot_date: str,
    url: str,
    path: Path,
    status: str,
    interface: str,
    dns_server: str | None,
    response: DirectHttpResponse | None,
) -> CffexTradeParameterDownload:
    local_ip = ""
    remote_ip = ""
    resolved_ip = ""
    interface_ipv4 = ""
    route_interface = ""
    selected_dns = dns_server or ""
    selected_interface = interface
    if response is not None:
        route = response.route
        local_ip = route.connected_local[0] if route.connected_local else ""
        remote_ip = (
            route.connected_peer[0]
            if route.connected_peer
            else route.resolved_ip
        )
        resolved_ip = route.resolved_ip
        interface_ipv4 = route.interface_ipv4
        route_interface = route.route_interface
        selected_dns = route.dns_server
        selected_interface = route.interface
    return CffexTradeParameterDownload(
        snapshot_date=snapshot_date,
        url=url,
        path=str(path),
        status=status,
        bytes=int(path.stat().st_size),
        sha256=_sha256(path),
        rows=int(len(frame)),
        contracts=int(frame["contract"].nunique()),
        products=",".join(sorted(frame["product"].astype(str).unique())),
        title_date=snapshot_date,
        local_ip=local_ip,
        remote_ip=remote_ip,
        resolved_ip=resolved_ip,
        interface=selected_interface,
        interface_ipv4=interface_ipv4,
        dns_server=selected_dns,
        route_interface=route_interface,
    )


def download_cffex_trade_parameters(
    data_root: str | Path,
    snapshot_date: object,
    *,
    raw_relative_root: str | Path = DEFAULT_RAW_RELATIVE_ROOT,
    interface: str = "en0",
    dns_server: str | None = None,
    timeout_seconds: float = 60.0,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    fetcher: Callable[..., DirectHttpResponse] = physical_http_get,
) -> CffexTradeParameterDownload:
    normalized = _normalize_yyyymmdd(snapshot_date, "snapshot_date")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    url = cffex_trade_parameters_url(normalized)
    path = cffex_trade_parameter_path(
        data_root, normalized, raw_relative_root=raw_relative_root
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            cached = validate_cffex_trade_parameter_csv(path, normalized)
        except (CffexTradeParameterError, OSError, ValueError):
            quarantine = path.with_name(f"{path.name}.invalid.{time.time_ns()}")
            path.replace(quarantine)
        else:
            return _download_summary(
                cached,
                snapshot_date=normalized,
                url=url,
                path=path,
                status="cached_valid",
                interface=interface,
                dns_server=dns_server,
                response=None,
            )

    response = fetcher(
        url,
        interface=interface,
        dns_server=dns_server,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )
    if response.status != 200:
        raise CffexTradeParameterError(
            f"CFFEX trade parameters {normalized} returned HTTP "
            f"{response.status} {response.reason}"
        )
    content_type = str(response.headers.get("content-type", "")).lower()
    if "html" in content_type:
        raise CffexTradeParameterError(
            f"CFFEX trade parameters {normalized} returned HTML content"
        )
    frame = parse_cffex_trade_parameters_csv(
        response.body,
        normalized,
        source_file=f"{normalized}_1.csv",
        source_url=url,
    )
    _atomic_write_bytes(path, response.body)
    stored = validate_cffex_trade_parameter_csv(path, normalized)
    if not stored["source_sha256"].iloc[0] == frame["source_sha256"].iloc[0]:
        raise CffexTradeParameterError(
            "stored CFFEX trade-parameter cache changed during atomic write"
        )
    return _download_summary(
        stored,
        snapshot_date=normalized,
        url=url,
        path=path,
        status="downloaded",
        interface=interface,
        dns_server=dns_server,
        response=response,
    )


def _atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        frame.to_csv(tmp_path, index=False, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(path, encoded)


def write_cffex_trade_parameter_download_manifest(
    path: str | Path,
    records: Iterable[CffexTradeParameterDownload],
) -> Path:
    manifest_path = Path(path)
    incoming = pd.DataFrame([asdict(record) for record in records])
    if incoming.empty:
        return manifest_path
    missing = sorted(set(DOWNLOAD_MANIFEST_COLUMNS) - set(incoming.columns))
    if missing:
        raise CffexTradeParameterError(
            f"CFFEX download records missing fields: {','.join(missing)}"
        )
    incoming = incoming.loc[:, list(DOWNLOAD_MANIFEST_COLUMNS)]
    if manifest_path.exists():
        try:
            current = pd.read_csv(
                manifest_path,
                dtype={"snapshot_date": "string"},
                keep_default_na=False,
            )
        except (OSError, pd.errors.ParserError, ValueError) as exc:
            raise CffexTradeParameterError(
                f"CFFEX download manifest is unreadable: {manifest_path}"
            ) from exc
        missing_current = sorted(
            set(DOWNLOAD_MANIFEST_COLUMNS) - set(current.columns)
        )
        if missing_current:
            raise CffexTradeParameterError(
                "existing CFFEX download manifest is missing fields: "
                + ",".join(missing_current)
            )
        current = current.loc[:, list(DOWNLOAD_MANIFEST_COLUMNS)]
        by_date = current.set_index("snapshot_date", drop=False)
        for index, row in incoming.iterrows():
            date_value = str(row["snapshot_date"])
            if date_value not in by_date.index or row["status"] != "cached_valid":
                continue
            old = by_date.loc[date_value]
            if isinstance(old, pd.DataFrame):
                old = old.iloc[-1]
            for column in _ROUTE_COLUMNS:
                if not str(row[column]).strip():
                    incoming.at[index, column] = old[column]
        incoming = pd.concat([current, incoming], ignore_index=True, sort=False)
    incoming["snapshot_date"] = incoming["snapshot_date"].map(
        lambda value: _normalize_yyyymmdd(value, "snapshot_date")
    )
    incoming = (
        incoming.drop_duplicates("snapshot_date", keep="last")
        .sort_values("snapshot_date")
        .reset_index(drop=True)
    )
    _atomic_write_csv(manifest_path, incoming)
    return manifest_path


def validate_cffex_trade_parameter_download_manifest(
    path: str | Path,
    *,
    required_snapshot_dates: Iterable[object] | None = None,
) -> pd.DataFrame:
    manifest_path = Path(path)
    try:
        manifest = pd.read_csv(
            manifest_path,
            dtype={"snapshot_date": "string"},
            keep_default_na=False,
        )
    except (OSError, pd.errors.ParserError, ValueError) as exc:
        raise CffexTradeParameterError(
            f"CFFEX download manifest is unreadable: {manifest_path}"
        ) from exc
    missing = sorted(set(DOWNLOAD_MANIFEST_COLUMNS) - set(manifest.columns))
    if missing:
        raise CffexTradeParameterError(
            f"CFFEX download manifest missing fields: {','.join(missing)}"
        )
    manifest = manifest.loc[:, list(DOWNLOAD_MANIFEST_COLUMNS)].copy()
    try:
        manifest["snapshot_date"] = manifest["snapshot_date"].map(
            lambda value: _normalize_yyyymmdd(value, "snapshot_date")
        )
    except ValueError as exc:
        raise CffexTradeParameterError(str(exc)) from exc
    if manifest["snapshot_date"].duplicated().any():
        raise CffexTradeParameterError(
            "CFFEX download manifest has duplicate snapshot dates"
        )
    allowed_statuses = {"downloaded", "cached_valid"}
    invalid_statuses = sorted(set(manifest["status"].astype(str)) - allowed_statuses)
    if invalid_statuses:
        raise CffexTradeParameterError(
            "CFFEX download manifest has invalid statuses: "
            + ",".join(invalid_statuses)
        )
    if required_snapshot_dates is not None:
        expected = {
            _normalize_yyyymmdd(value, "required_snapshot_date")
            for value in required_snapshot_dates
        }
        found = set(manifest["snapshot_date"])
        missing_dates = sorted(expected - found)
        extra_dates = sorted(found - expected)
        if missing_dates or extra_dates:
            raise CffexTradeParameterError(
                "CFFEX download manifest date scope mismatch: "
                f"missing={','.join(missing_dates[:5])} "
                f"extra={','.join(extra_dates[:5])}"
            )
    for row in manifest.itertuples(index=False):
        date_value = str(row.snapshot_date)
        expected_url = cffex_trade_parameters_url(date_value)
        if str(row.url) != expected_url:
            raise CffexTradeParameterError(
                f"CFFEX manifest URL mismatch for {date_value}"
            )
        source_path = Path(str(row.path))
        _validate_source_filename(source_path, date_value)
        if not source_path.is_file():
            raise CffexTradeParameterError(
                f"CFFEX manifest source is missing: {source_path}"
            )
        if int(row.bytes) != int(source_path.stat().st_size):
            raise CffexTradeParameterError(
                f"CFFEX manifest byte count is stale for {date_value}"
            )
        source_hash = _sha256(source_path)
        if str(row.sha256).lower() != source_hash:
            raise CffexTradeParameterError(
                f"CFFEX manifest source hash is stale for {date_value}"
            )
        parsed = validate_cffex_trade_parameter_csv(source_path, date_value)
        if int(row.rows) != len(parsed) or int(row.contracts) != parsed["contract"].nunique():
            raise CffexTradeParameterError(
                f"CFFEX manifest row metadata is stale for {date_value}"
            )
        products = ",".join(sorted(parsed["product"].astype(str).unique()))
        if str(row.products) != products or str(row.title_date) != date_value:
            raise CffexTradeParameterError(
                f"CFFEX manifest content metadata is stale for {date_value}"
            )
        if not str(row.interface).strip():
            raise CffexTradeParameterError(
                f"CFFEX manifest is missing physical interface details for {date_value}"
            )
    return manifest.sort_values("snapshot_date").reset_index(drop=True)


def derive_required_snapshot_dates_from_frames(
    contract_master: pd.DataFrame,
    panel_calendar: Iterable[object],
    panel_last_date: object,
    *,
    scoped_dates: Iterable[object] | None = None,
    max_date: object | None = None,
    max_snapshots: int | None = None,
) -> list[str]:
    if "first_observation_date" not in contract_master.columns:
        raise CffexTradeParameterError(
            "CFFEX contract master is missing first_observation_date"
        )
    if contract_master.empty:
        raise CffexTradeParameterError("CFFEX contract master is empty")
    try:
        first_dates = contract_master["first_observation_date"].map(
            lambda value: _normalize_yyyymmdd(value, "first_observation_date")
        )
        horizon = _normalize_yyyymmdd(panel_last_date, "panel_last_date")
        calendar = {
            _normalize_yyyymmdd(value, "panel_trade_date")
            for value in panel_calendar
        }
    except ValueError as exc:
        raise CffexTradeParameterError(str(exc)) from exc
    if not calendar:
        raise CffexTradeParameterError("CFFEX panel calendar is empty")
    canonical = sorted(set(first_dates) | {horizon})
    absent = sorted(set(canonical) - calendar)
    if absent:
        raise CffexTradeParameterError(
            "required CFFEX snapshots are absent from the panel calendar: "
            + ",".join(absent[:5])
        )

    selected = canonical
    if scoped_dates is not None:
        try:
            scope = sorted(
                {
                    _normalize_yyyymmdd(value, "scoped_snapshot_date")
                    for value in scoped_dates
                }
            )
        except ValueError as exc:
            raise CffexTradeParameterError(str(exc)) from exc
        if not scope:
            raise CffexTradeParameterError("scoped_dates must not be empty")
        arbitrary = sorted(set(scope) - set(canonical))
        if arbitrary:
            raise CffexTradeParameterError(
                "scoped CFFEX dates must come from the canonical requirement set: "
                + ",".join(arbitrary[:5])
            )
        selected = scope
    if max_date is not None:
        try:
            maximum = _normalize_yyyymmdd(max_date, "max_date")
        except ValueError as exc:
            raise CffexTradeParameterError(str(exc)) from exc
        if maximum not in calendar:
            raise CffexTradeParameterError(
                "max_date must occur in the validated CFFEX panel calendar"
            )
        selected = [date_value for date_value in selected if date_value <= maximum]
    if max_snapshots is not None:
        if isinstance(max_snapshots, bool) or int(max_snapshots) <= 0:
            raise ValueError("max_snapshots must be positive")
        selected = selected[: int(max_snapshots)]
    if not selected:
        raise CffexTradeParameterError(
            "CFFEX snapshot scope contains no canonical required dates"
        )
    return selected


def derive_required_snapshot_dates(
    panel_path: str | Path,
    master_path: str | Path,
    *,
    scoped_dates: Iterable[object] | None = None,
    max_date: object | None = None,
    max_snapshots: int | None = None,
) -> list[str]:
    panel = Path(panel_path)
    master = Path(master_path)
    panel_manifest = validate_cffex_panel_manifest(panel)
    master_manifest = validate_cffex_contract_master_manifest(master, panel)
    try:
        contracts = pd.read_parquet(
            master, columns=["contract", "first_observation_date"]
        )
        calendar = pd.read_parquet(panel, columns=["trade_date"])[
            "trade_date"
        ].unique()
    except (OSError, ValueError, TypeError) as exc:
        raise CffexTradeParameterError(
            "validated CFFEX panel/master could not be read"
        ) from exc
    if len(contracts) != int(master_manifest["rows"]):
        raise CffexTradeParameterError(
            "CFFEX contract master row count changed after validation"
        )
    if contracts["contract"].astype(str).duplicated().any():
        raise CffexTradeParameterError(
            "CFFEX contract master contains duplicate contracts"
        )
    return derive_required_snapshot_dates_from_frames(
        contracts,
        calendar,
        panel_manifest["last_date"],
        scoped_dates=scoped_dates,
        max_date=max_date,
        max_snapshots=max_snapshots,
    )


def scoped_artifact_path(
    canonical_path: str | Path,
    snapshot_dates: Sequence[object],
) -> Path:
    normalized = sorted(
        {_normalize_yyyymmdd(value, "snapshot_date") for value in snapshot_dates}
    )
    if not normalized:
        raise ValueError("snapshot_dates must not be empty")
    signature = hashlib.sha256(",".join(normalized).encode("ascii")).hexdigest()[:10]
    canonical = Path(canonical_path)
    scope = f"scoped_{normalized[0]}_{normalized[-1]}_{len(normalized)}d_{signature}"
    return canonical.with_name(f"{canonical.stem}_{scope}{canonical.suffix}")


def cffex_trade_parameter_metadata_manifest_path(
    metadata_path: str | Path,
) -> Path:
    path = Path(metadata_path)
    return path.with_suffix(path.suffix + ".manifest.json")


def _source_manifest_entries(manifest: pd.DataFrame) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for row in manifest.sort_values("snapshot_date").itertuples(index=False):
        entries.append(
            {
                "snapshot_date": str(row.snapshot_date),
                "url": str(row.url),
                "path": str(row.path),
                "bytes": int(row.bytes),
                "sha256": str(row.sha256),
            }
        )
    return entries


def _source_set_hash(entries: Sequence[Mapping[str, object]]) -> str:
    encoded = json.dumps(
        list(entries), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_source_rows(manifest: pd.DataFrame) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for row in manifest.sort_values("snapshot_date").itertuples(index=False):
        source_path = Path(str(row.path))
        payload = source_path.read_bytes()
        frames.append(
            parse_cffex_trade_parameters_csv(
                payload,
                str(row.snapshot_date),
                source_file=source_path,
                source_url=str(row.url),
                source_sha256=str(row.sha256),
            )
        )
    if not frames:
        raise CffexTradeParameterError(
            "no CFFEX trade-parameter sources were supplied"
        )
    combined = pd.concat(frames, ignore_index=True, sort=False)
    validate_cffex_trade_parameter_rows(combined)
    return combined


def _default_history_path(metadata_path: str | Path) -> Path:
    metadata = Path(metadata_path)
    return metadata.with_name(f"{metadata.stem}_history{metadata.suffix}")


def _expiry_revision_counts(rows: pd.DataFrame) -> pd.Series:
    ordered = rows.sort_values(["contract", "snapshot_date"])
    changed = ordered.groupby("contract", sort=False, observed=True)[
        "official_last_trade_date"
    ].transform(lambda values: values.ne(values.shift()))
    return (
        changed.groupby(ordered["contract"], sort=False)
        .sum()
        .sub(1)
        .clip(lower=0)
        .astype(int)
    )


def _expiry_revision_summary(rows: pd.DataFrame) -> dict[str, int]:
    counts = _expiry_revision_counts(rows)
    return {
        "revised_contracts": int(counts.gt(0).sum()),
        "revision_events": int(counts.sum()),
    }


def _select_contract_rows_asof(
    history: pd.DataFrame,
    master: pd.DataFrame,
    panel_last_date: str,
) -> tuple[pd.DataFrame, list[str]]:
    selection = master.loc[:, ["contract", "last_trade_date"]].copy()
    selection["contract"] = selection["contract"].astype("string").str.strip()
    selection["last_trade_date"] = selection["last_trade_date"].map(
        lambda value: _normalize_yyyymmdd(value, "last_trade_date")
    )
    selection["metadata_cutoff_date"] = selection["last_trade_date"].map(
        lambda value: min(value, panel_last_date)
    )
    eligible = history.merge(
        selection.loc[:, ["contract", "metadata_cutoff_date"]],
        on="contract",
        how="inner",
        validate="many_to_one",
    )
    eligible = eligible.loc[
        eligible["snapshot_date"].astype(str).le(eligible["metadata_cutoff_date"])
    ].copy()
    eligible = eligible.sort_values(["contract", "snapshot_date"]).reset_index(
        drop=True
    )
    missing = sorted(set(selection["contract"]) - set(eligible["contract"]))
    if eligible.empty:
        return eligible, missing

    grouped = eligible.groupby("contract", sort=True, observed=True)
    audit = grouped["official_last_trade_date"].agg(
        expiry_first="first",
        expiry_latest="last",
    )
    audit["expiry_revision_count"] = _expiry_revision_counts(eligible)
    audit["expiry_changed"] = audit["expiry_revision_count"].gt(0)
    selected = grouped.tail(1).set_index("contract")
    selected = selected.join(audit, how="left").reset_index()
    selected = selected.drop(columns="metadata_cutoff_date")
    if not selected["official_last_trade_date"].eq(selected["expiry_latest"]).all():
        raise CffexTradeParameterError(
            "CFFEX as-of metadata expiry audit does not match selected rows"
        )
    if selected["contract"].duplicated().any():
        raise CffexTradeParameterError(
            "CFFEX as-of metadata selection produced duplicate contracts"
        )
    return selected.loc[:, list(CONTRACT_METADATA_COLUMNS)].sort_values(
        "contract"
    ).reset_index(drop=True), missing


def build_cffex_trade_parameter_metadata(
    panel_path: str | Path,
    master_path: str | Path,
    download_manifest_path: str | Path,
    metadata_path: str | Path,
    *,
    history_path: str | Path | None = None,
    required_snapshot_dates: Iterable[object] | None = None,
    canonical_output_path: str | Path | None = None,
    canonical_history_path: str | Path | None = None,
) -> Path:
    panel = Path(panel_path)
    master_path_obj = Path(master_path)
    output = Path(metadata_path)
    history_output = (
        _default_history_path(output) if history_path is None else Path(history_path)
    )
    canonical_output = (
        output if canonical_output_path is None else Path(canonical_output_path)
    )
    canonical_history = (
        _default_history_path(canonical_output)
        if canonical_history_path is None
        else Path(canonical_history_path)
    )
    if _canonical_path(output) == _canonical_path(history_output):
        raise CffexTradeParameterError(
            "CFFEX metadata and history must use different paths"
        )
    panel_manifest = validate_cffex_panel_manifest(panel)
    master_manifest = validate_cffex_contract_master_manifest(master_path_obj, panel)
    canonical_dates = derive_required_snapshot_dates(panel, master_path_obj)
    selected_dates = (
        canonical_dates
        if required_snapshot_dates is None
        else sorted(
            {
                _normalize_yyyymmdd(value, "required_snapshot_date")
                for value in required_snapshot_dates
            }
        )
    )
    if not selected_dates:
        raise CffexTradeParameterError("metadata build date scope is empty")
    arbitrary = sorted(set(selected_dates) - set(canonical_dates))
    if arbitrary:
        raise CffexTradeParameterError(
            "metadata build dates must come from the canonical requirement set: "
            + ",".join(arbitrary[:5])
        )
    full_scope = selected_dates == canonical_dates
    canonical_metadata_target = _canonical_path(output) == _canonical_path(
        canonical_output
    )
    canonical_history_target = _canonical_path(history_output) == _canonical_path(
        canonical_history
    )
    if (canonical_metadata_target or canonical_history_target) and not full_scope:
        raise CffexTradeParameterError(
            "partial CFFEX metadata/history cannot overwrite canonical outputs"
        )
    if not full_scope and canonical_output_path is None:
        raise CffexTradeParameterError(
            "partial CFFEX metadata requires an explicit distinct canonical_output_path"
        )

    source_manifest = validate_cffex_trade_parameter_download_manifest(
        download_manifest_path,
        required_snapshot_dates=selected_dates,
    )
    source_rows = _load_source_rows(source_manifest)
    try:
        master = pd.read_parquet(
            master_path_obj,
            columns=[
                "contract",
                "product",
                "first_observation_date",
                "last_trade_date",
            ],
        )
    except (OSError, ValueError, TypeError) as exc:
        raise CffexTradeParameterError(
            "validated CFFEX contract master could not be read"
        ) from exc
    master["contract"] = master["contract"].astype("string").str.strip()
    if master["contract"].duplicated().any():
        raise CffexTradeParameterError(
            "CFFEX contract master contains duplicate contracts"
        )

    master_contracts = set(master["contract"])
    source_extra_contracts = sorted(set(source_rows["contract"]) - master_contracts)
    history = source_rows.loc[
        source_rows["contract"].isin(master_contracts)
    ].sort_values(["snapshot_date", "contract"]).reset_index(drop=True)
    if history.empty:
        raise CffexTradeParameterError("CFFEX history build produced no rows")
    validate_cffex_trade_parameter_rows(history)
    history_contracts = set(history["contract"])
    history_missing_contracts = sorted(master_contracts - history_contracts)
    metadata, missing_contracts = _select_contract_rows_asof(
        history, master, str(panel_manifest["last_date"])
    )
    if full_scope and (history_missing_contracts or missing_contracts):
        missing = sorted(set(history_missing_contracts) | set(missing_contracts))
        raise CffexTradeParameterError(
            "canonical CFFEX history/metadata is missing exact master contracts: "
            + ",".join(missing[:10])
        )
    if full_scope and set(metadata["contract"]) != master_contracts:
        raise CffexTradeParameterError(
            "canonical CFFEX as-of metadata contract coverage is incomplete"
        )
    if metadata.empty:
        raise CffexTradeParameterError("CFFEX metadata build produced no rows")
    validate_cffex_trade_parameter_rows(metadata)
    history_revision_summary = _expiry_revision_summary(history)
    asof_revision_summary = {
        "revised_contracts": int(metadata["expiry_changed"].sum()),
        "revision_events": int(metadata["expiry_revision_count"].sum()),
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    history_output.parent.mkdir(parents=True, exist_ok=True)
    nonce = f"{os.getpid()}.{time.time_ns()}"
    metadata_tmp_path = output.with_name(
        f".{output.name}.{nonce}.tmp"
    )
    history_tmp_path = history_output.with_name(
        f".{history_output.name}.{nonce}.tmp"
    )
    try:
        history.to_parquet(history_tmp_path, index=False, compression="zstd")
        metadata.to_parquet(metadata_tmp_path, index=False, compression="zstd")
        history_sha256 = _sha256(history_tmp_path)
        metadata_sha256 = _sha256(metadata_tmp_path)
        history_tmp_path.replace(history_output)
        metadata_tmp_path.replace(output)
    finally:
        for tmp_path in (metadata_tmp_path, history_tmp_path):
            if tmp_path.exists():
                tmp_path.unlink()

    source_entries = _source_manifest_entries(source_manifest)
    manifest_payload: dict[str, object] = {
        "schema_version": 2,
        "metadata_path": str(output),
        "metadata_sha256": metadata_sha256,
        "rows": int(len(metadata)),
        "contracts": int(metadata["contract"].nunique()),
        "history_path": str(history_output),
        "history_sha256": history_sha256,
        "history_rows": int(len(history)),
        "history_contracts": int(history["contract"].nunique()),
        "history_snapshot_dates": int(history["snapshot_date"].nunique()),
        "products": sorted(metadata["product"].astype(str).unique()),
        "snapshot_dates": len(selected_dates),
        "first_snapshot_date": min(selected_dates),
        "last_snapshot_date": max(selected_dates),
        "canonical": bool(
            full_scope and canonical_metadata_target and canonical_history_target
        ),
        "complete_master_coverage": not missing_contracts,
        "missing_master_contracts": len(missing_contracts),
        "history_missing_master_contracts": len(history_missing_contracts),
        "source_extra_contracts": len(source_extra_contracts),
        "selection_rule": "latest_official_snapshot_not_after_min_derived_last_observation_and_panel_horizon",
        "expiry_revised_contracts": asof_revision_summary["revised_contracts"],
        "expiry_revision_events": asof_revision_summary["revision_events"],
        "history_expiry_revised_contracts": history_revision_summary[
            "revised_contracts"
        ],
        "history_expiry_revision_events": history_revision_summary[
            "revision_events"
        ],
        "source_panel_path": str(panel),
        "source_panel_sha256": str(panel_manifest["panel_sha256"]),
        "source_master_path": str(master_path_obj),
        "source_master_sha256": str(master_manifest["master_sha256"]),
        "source_download_manifest_path": str(download_manifest_path),
        "source_download_manifest_sha256": _sha256(download_manifest_path),
        "source_set_sha256": _source_set_hash(source_entries),
        "parser_source_sha256": _sha256(Path(__file__)),
        "sources": source_entries,
    }
    _atomic_write_json(
        cffex_trade_parameter_metadata_manifest_path(output), manifest_payload
    )
    return output


def validate_cffex_trade_parameter_metadata_manifest(
    metadata_path: str | Path,
    panel_path: str | Path,
    master_path: str | Path,
    download_manifest_path: str | Path,
) -> dict[str, object]:
    metadata = Path(metadata_path)
    panel_manifest = validate_cffex_panel_manifest(panel_path)
    master_manifest = validate_cffex_contract_master_manifest(
        master_path, panel_path
    )
    manifest_path = cffex_trade_parameter_metadata_manifest_path(metadata)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CffexTradeParameterError(
            f"CFFEX metadata manifest is unreadable: {manifest_path}"
        ) from exc
    if int(manifest.get("schema_version", 0)) != 2:
        raise CffexTradeParameterError(
            "unsupported CFFEX metadata manifest schema"
        )
    if not metadata.is_file() or manifest.get("metadata_sha256") != _sha256(metadata):
        raise CffexTradeParameterError(
            "CFFEX metadata hash does not match its manifest"
        )
    history = Path(str(manifest.get("history_path", "")))
    if not history.is_file() or manifest.get("history_sha256") != _sha256(history):
        raise CffexTradeParameterError(
            "CFFEX history hash does not match the metadata manifest"
        )
    if manifest.get("parser_source_sha256") != _sha256(Path(__file__)):
        raise CffexTradeParameterError(
            "CFFEX metadata was built by a stale parser"
        )
    if manifest.get("source_panel_sha256") != panel_manifest.get("panel_sha256"):
        raise CffexTradeParameterError(
            "CFFEX metadata is bound to a different daily panel"
        )
    if manifest.get("source_master_sha256") != master_manifest.get("master_sha256"):
        raise CffexTradeParameterError(
            "CFFEX metadata is bound to a different contract master"
        )
    if manifest.get("source_download_manifest_sha256") != _sha256(
        download_manifest_path
    ):
        raise CffexTradeParameterError(
            "CFFEX metadata source manifest is stale"
        )

    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CffexTradeParameterError(
            "CFFEX metadata manifest has no source hashes"
        )
    if manifest.get("source_set_sha256") != _source_set_hash(sources):
        raise CffexTradeParameterError(
            "CFFEX metadata source-set hash is stale"
        )
    expected_dates: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            raise CffexTradeParameterError(
                "CFFEX metadata manifest has an invalid source entry"
            )
        date_value = _normalize_yyyymmdd(
            source.get("snapshot_date"), "source_snapshot_date"
        )
        expected_dates.append(date_value)
        path = Path(str(source.get("path", "")))
        if not path.is_file() or str(source.get("sha256", "")) != _sha256(path):
            raise CffexTradeParameterError(
                f"CFFEX metadata source is stale for {date_value}"
            )
    source_manifest = validate_cffex_trade_parameter_download_manifest(
        download_manifest_path,
        required_snapshot_dates=expected_dates,
    )
    source_hashes = source_manifest.set_index("snapshot_date")["sha256"].astype(str)

    metadata_parquet = pq.ParquetFile(metadata)
    missing_columns = sorted(
        set(CONTRACT_METADATA_COLUMNS) - set(metadata_parquet.schema.names)
    )
    if missing_columns:
        raise CffexTradeParameterError(
            f"CFFEX metadata parquet missing columns: {','.join(missing_columns)}"
        )
    if int(manifest.get("rows", -1)) != int(metadata_parquet.metadata.num_rows):
        raise CffexTradeParameterError(
            "CFFEX metadata row count does not match its manifest"
        )
    history_parquet = pq.ParquetFile(history)
    history_missing_columns = sorted(
        set(TRADE_PARAMETER_COLUMNS) - set(history_parquet.schema.names)
    )
    if history_missing_columns:
        raise CffexTradeParameterError(
            "CFFEX history parquet missing columns: "
            + ",".join(history_missing_columns)
        )
    if int(manifest.get("history_rows", -1)) != int(
        history_parquet.metadata.num_rows
    ):
        raise CffexTradeParameterError(
            "CFFEX history row count does not match its manifest"
        )
    frame = pd.read_parquet(metadata)
    history_frame = pd.read_parquet(history)
    validate_cffex_trade_parameter_rows(frame)
    validate_cffex_trade_parameter_rows(history_frame)
    if frame["contract"].duplicated().any():
        raise CffexTradeParameterError(
            "CFFEX contract metadata contains duplicate contracts"
        )
    if int(manifest.get("contracts", -1)) != frame["contract"].nunique():
        raise CffexTradeParameterError(
            "CFFEX metadata contract count does not match its manifest"
        )
    if int(manifest.get("history_contracts", -1)) != int(
        history_frame["contract"].nunique()
    ):
        raise CffexTradeParameterError(
            "CFFEX history contract count does not match its manifest"
        )
    if int(manifest.get("history_snapshot_dates", -1)) != int(
        history_frame["snapshot_date"].nunique()
    ):
        raise CffexTradeParameterError(
            "CFFEX history snapshot count does not match its manifest"
        )
    expected_history_hashes = history_frame["snapshot_date"].astype(str).map(
        source_hashes
    )
    if expected_history_hashes.isna().any() or not history_frame[
        "source_sha256"
    ].astype(str).eq(expected_history_hashes).all():
        raise CffexTradeParameterError(
            "CFFEX history rows do not match source manifest hashes"
        )

    for column in ("expiry_first", "expiry_latest"):
        try:
            frame[column] = frame[column].map(
                lambda value: _normalize_yyyymmdd(value, column)
            )
        except ValueError as exc:
            raise CffexTradeParameterError(str(exc)) from exc
    revision_counts = pd.to_numeric(
        frame["expiry_revision_count"], errors="coerce"
    )
    if (
        revision_counts.isna().any()
        or revision_counts.lt(0).any()
        or revision_counts.ne(np.floor(revision_counts)).any()
    ):
        raise CffexTradeParameterError(
            "CFFEX expiry_revision_count must be a non-negative integer"
        )
    expiry_changed = frame["expiry_changed"]
    if not expiry_changed.isin([True, False]).all():
        raise CffexTradeParameterError("CFFEX expiry_changed must be boolean")
    if not expiry_changed.astype(bool).eq(revision_counts.gt(0)).all():
        raise CffexTradeParameterError(
            "CFFEX expiry_changed disagrees with expiry_revision_count"
        )
    if not frame["official_last_trade_date"].eq(frame["expiry_latest"]).all():
        raise CffexTradeParameterError(
            "CFFEX metadata official expiry disagrees with expiry_latest"
        )

    master_frame = pd.read_parquet(
        master_path,
        columns=["contract", "product", "first_observation_date", "last_trade_date"],
    )
    master_frame["contract"] = master_frame["contract"].astype("string").str.strip()
    expected_metadata, missing_asof = _select_contract_rows_asof(
        history_frame, master_frame, str(panel_manifest["last_date"])
    )
    compare_columns = [
        "contract",
        "snapshot_date",
        "source_sha256",
        "official_last_trade_date",
        *EXPIRY_AUDIT_COLUMNS,
    ]
    try:
        pd.testing.assert_frame_equal(
            frame.loc[:, compare_columns]
            .sort_values("contract")
            .reset_index(drop=True),
            expected_metadata.loc[:, compare_columns]
            .sort_values("contract")
            .reset_index(drop=True),
            check_dtype=False,
        )
    except AssertionError as exc:
        raise CffexTradeParameterError(
            "CFFEX metadata is not the latest eligible as-of history selection"
        ) from exc

    history_revision_summary = _expiry_revision_summary(history_frame)
    if int(manifest.get("history_expiry_revised_contracts", -1)) != int(
        history_revision_summary["revised_contracts"]
    ) or int(manifest.get("history_expiry_revision_events", -1)) != int(
        history_revision_summary["revision_events"]
    ):
        raise CffexTradeParameterError(
            "CFFEX history expiry revision counts do not match the manifest"
        )
    if int(manifest.get("expiry_revised_contracts", -1)) != int(
        frame["expiry_changed"].sum()
    ) or int(manifest.get("expiry_revision_events", -1)) != int(
        frame["expiry_revision_count"].sum()
    ):
        raise CffexTradeParameterError(
            "CFFEX metadata expiry revision counts do not match the manifest"
        )
    if bool(manifest.get("canonical")):
        master_contracts = set(master_frame["contract"].astype(str))
        if missing_asof or set(frame["contract"].astype(str)) != master_contracts:
            raise CffexTradeParameterError(
                "canonical CFFEX metadata no longer covers the exact master"
            )
        if set(history_frame["contract"].astype(str)) != master_contracts:
            raise CffexTradeParameterError(
                "canonical CFFEX history no longer covers the exact master"
            )
        if set(history_frame["snapshot_date"].astype(str)) != set(expected_dates):
            raise CffexTradeParameterError(
                "canonical CFFEX history does not cover every source snapshot"
            )
    return manifest


def _metadata_frame(metadata: pd.DataFrame | str | Path) -> pd.DataFrame:
    if isinstance(metadata, pd.DataFrame):
        return metadata.copy()
    try:
        return pd.read_parquet(metadata)
    except (OSError, ValueError, TypeError) as exc:
        raise CffexTradeParameterError(
            f"CFFEX contract metadata is unreadable: {metadata}"
        ) from exc


def query_exact_contract(
    metadata: pd.DataFrame | str | Path,
    contract: object,
    *,
    snapshot_date: object | None = None,
) -> pd.Series:
    exact = str(contract).strip()
    if not (_FUTURE_PATTERN.fullmatch(exact) or _OPTION_PATTERN.fullmatch(exact)):
        raise ValueError("contract must be an exact supported CFFEX contract code")
    frame = _metadata_frame(metadata)
    required = {"contract", "snapshot_date"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise CffexTradeParameterError(
            f"CFFEX metadata query missing columns: {','.join(missing)}"
        )
    rows = frame.loc[frame["contract"].astype(str).eq(exact)].copy()
    if snapshot_date is not None:
        normalized = _normalize_yyyymmdd(snapshot_date, "snapshot_date")
        rows = rows.loc[rows["snapshot_date"].astype(str).eq(normalized)]
    if rows.empty:
        suffix = "" if snapshot_date is None else f" at {normalized}"
        raise CffexTradeParameterError(
            f"exact CFFEX contract metadata is unavailable for {exact}{suffix}"
        )
    if len(rows) != 1:
        raise CffexTradeParameterError(
            f"exact CFFEX contract query is ambiguous for {exact}"
        )
    return rows.iloc[0].copy()


def query_exact_contract_asof(
    history: pd.DataFrame | str | Path,
    contract: object,
    trade_date: object,
) -> pd.Series:
    exact = str(contract).strip()
    if not (_FUTURE_PATTERN.fullmatch(exact) or _OPTION_PATTERN.fullmatch(exact)):
        raise ValueError("contract must be an exact supported CFFEX contract code")
    normalized_date = _normalize_yyyymmdd(trade_date, "trade_date")
    frame = _metadata_frame(history)
    missing = sorted({"contract", "snapshot_date"} - set(frame.columns))
    if missing:
        raise CffexTradeParameterError(
            f"CFFEX history query missing columns: {','.join(missing)}"
        )
    if frame.duplicated(["snapshot_date", "contract"]).any():
        raise CffexTradeParameterError(
            "CFFEX history query has duplicate snapshot_date/contract keys"
        )
    rows = frame.loc[
        frame["contract"].astype(str).eq(exact)
        & frame["snapshot_date"].astype(str).le(normalized_date)
    ].copy()
    if rows.empty:
        raise CffexTradeParameterError(
            f"exact CFFEX contract history is unavailable for {exact} "
            f"on or before {normalized_date}"
        )
    return rows.sort_values("snapshot_date").iloc[-1].copy()


def reconcile_cffex_last_trade_dates(
    contract_master: pd.DataFrame | str | Path,
    official_metadata: pd.DataFrame | str | Path,
    *,
    panel_last_date: object,
) -> CffexTradeParameterReconciliation:
    if isinstance(contract_master, pd.DataFrame):
        master = contract_master.copy()
    else:
        try:
            master = pd.read_parquet(contract_master)
        except (OSError, ValueError, TypeError) as exc:
            raise CffexTradeParameterError(
                f"CFFEX contract master is unreadable: {contract_master}"
            ) from exc
    official = _metadata_frame(official_metadata)
    master_required = {"contract", "last_trade_date"}
    official_required = {"contract", "official_last_trade_date"}
    missing_master = sorted(master_required - set(master.columns))
    missing_official = sorted(official_required - set(official.columns))
    if missing_master or missing_official:
        raise CffexTradeParameterError(
            "CFFEX reconciliation inputs are missing columns: "
            + ",".join(missing_master + missing_official)
        )
    horizon = _normalize_yyyymmdd(panel_last_date, "panel_last_date")
    master = master.loc[
        :, [column for column in ("contract", "product", "last_trade_date") if column in master.columns]
    ].copy()
    official = official.loc[
        :,
        [
            column
            for column in ("contract", "product", "official_last_trade_date")
            if column in official.columns
        ],
    ].copy()
    master["contract"] = master["contract"].astype("string").str.strip()
    official["contract"] = official["contract"].astype("string").str.strip()
    if master["contract"].duplicated().any():
        raise CffexTradeParameterError(
            "CFFEX reconciliation master has duplicate contracts"
        )
    official_static = official.groupby("contract", sort=True, observed=True)[
        "official_last_trade_date"
    ].nunique(dropna=False)
    if official_static.gt(1).any():
        raise CffexTradeParameterError(
            "official last-trade dates change across exact contract rows"
        )
    official = official.drop_duplicates("contract", keep="first")
    try:
        master["derived_last_trade_date"] = master["last_trade_date"].map(
            lambda value: _normalize_yyyymmdd(value, "last_trade_date")
        )
        official["official_last_trade_date"] = official[
            "official_last_trade_date"
        ].map(
            lambda value: _normalize_yyyymmdd(
                value, "official_last_trade_date"
            )
        )
    except ValueError as exc:
        raise CffexTradeParameterError(str(exc)) from exc
    master = master.drop(columns="last_trade_date")
    if "product" in master.columns:
        master = master.rename(columns={"product": "master_product"})
    if "product" in official.columns:
        official = official.rename(columns={"product": "official_product"})
    details = master.merge(official, on="contract", how="outer", indicator=True)
    details["panel_last_date"] = horizon
    details["derived_right_censored"] = details[
        "derived_last_trade_date"
    ].eq(horizon)
    details["difference_days"] = (
        pd.to_datetime(details["official_last_trade_date"], format="%Y%m%d", errors="coerce")
        - pd.to_datetime(details["derived_last_trade_date"], format="%Y%m%d", errors="coerce")
    ).dt.days.astype("Int64")

    def classify(row: pd.Series) -> str:
        if row["_merge"] == "left_only":
            return "missing_official"
        if row["_merge"] == "right_only":
            return "extra_official"
        if row["derived_last_trade_date"] == row["official_last_trade_date"]:
            return "exact_match"
        if bool(row["derived_right_censored"]):
            if row["official_last_trade_date"] > row["derived_last_trade_date"]:
                return "right_censored_correction"
            return "right_censored_conflict"
        return "historical_difference"

    details["classification"] = details.apply(classify, axis=1)
    details = details.drop(columns="_merge").sort_values("contract").reset_index(drop=True)
    counts = details["classification"].value_counts()
    summary = {
        "master_contracts": int(len(master)),
        "official_contracts": int(len(official)),
        "exact_matches": int(counts.get("exact_match", 0)),
        "historical_differences": int(counts.get("historical_difference", 0)),
        "right_censored_corrections": int(
            counts.get("right_censored_correction", 0)
        ),
        "right_censored_conflicts": int(counts.get("right_censored_conflict", 0)),
        "missing_contracts": int(counts.get("missing_official", 0)),
        "extra_contracts": int(counts.get("extra_official", 0)),
    }
    return CffexTradeParameterReconciliation(details=details, summary=summary)


# Singular aliases keep call sites readable when operating on one snapshot.
cffex_trade_parameter_url = cffex_trade_parameters_url
download_cffex_trade_parameter_snapshot = download_cffex_trade_parameters

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from io import StringIO
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quant_proof.network_guard import DirectHttpResponse, physical_http_get


CFFEX_HOST = "www.cffex.com.cn"
CFFEX_MONTH_URL = "http://www.cffex.com.cn/sj/historysj/{month}/zip/{month}.zip"
CFFEX_PRODUCTS = {
    "IF": {"instrument_type": "future", "underlying_index": "CSI300", "multiplier": 300.0},
    "IH": {"instrument_type": "future", "underlying_index": "SSE50", "multiplier": 300.0},
    "IC": {"instrument_type": "future", "underlying_index": "CSI500", "multiplier": 200.0},
    "IM": {"instrument_type": "future", "underlying_index": "CSI1000", "multiplier": 200.0},
    "IO": {"instrument_type": "option", "underlying_index": "CSI300", "multiplier": 100.0},
    "HO": {"instrument_type": "option", "underlying_index": "SSE50", "multiplier": 100.0},
    "MO": {"instrument_type": "option", "underlying_index": "CSI1000", "multiplier": 100.0},
}
CFFEX_PANEL_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "contract",
    "product",
    "instrument_type",
    "underlying_index",
    "contract_month",
    "option_type",
    "strike",
    "multiplier",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "pre_settle",
    "volume",
    "turnover_10k_cny",
    "open_interest",
    "open_interest_change",
    "delta",
    "open_executable",
    "settlement_mark_valid",
    "source",
    "source_file",
    "source_tier",
    "execution_tier",
)
CFFEX_CONTRACT_MASTER_COLUMNS: tuple[str, ...] = (
    "contract",
    "product",
    "instrument_type",
    "underlying_index",
    "contract_month",
    "option_type",
    "strike",
    "multiplier",
    "first_observation_date",
    "last_trade_date",
    "trading_days",
    "total_volume",
    "open_price_days",
    "exercise_style",
    "settlement_style",
    "expiry_source",
    "source_tier",
)
_SOURCE_COLUMNS = {
    "合约代码": "contract",
    "今开盘": "open",
    "最高价": "high",
    "最低价": "low",
    "成交量": "volume",
    "成交金额": "turnover_10k_cny",
    "持仓量": "open_interest",
    "持仓变化": "open_interest_change",
    "今收盘": "close",
    "今结算": "settle",
    "前结算": "pre_settle",
    "Delta": "delta",
}
_REQUIRED_SOURCE_COLUMNS = set(_SOURCE_COLUMNS) - {"持仓变化", "Delta"}
_NUMERIC_COLUMNS = (
    "strike",
    "multiplier",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "pre_settle",
    "volume",
    "turnover_10k_cny",
    "open_interest",
    "open_interest_change",
    "delta",
)
_DAILY_MEMBER_PATTERN = re.compile(r"^(?P<date>\d{8})_1\.csv$")
_FUTURE_PATTERN = re.compile(r"^(IF|IH|IC|IM)(\d{4})$")
_OPTION_PATTERN = re.compile(r"^(IO|HO|MO)(\d{4})-([CP])-(\d+(?:\.\d+)?)$")


class CffexDataError(RuntimeError):
    """Raised when official CFFEX data cannot pass content or route gates."""


@dataclass(frozen=True)
class CffexMonthDownload:
    month: str
    url: str
    path: str
    status: str
    bytes: int
    sha256: str
    entries: int
    rows: int
    first_date: str
    last_date: str
    products: str
    local_ip: str
    remote_ip: str
    interface: str


def cffex_month_url(month: str) -> str:
    normalized = str(month).replace("-", "")
    if not re.fullmatch(r"\d{6}", normalized):
        raise ValueError(f"invalid CFFEX month: {month}")
    return CFFEX_MONTH_URL.format(month=normalized)


def cffex_months(start_date: str, end_date: str) -> list[str]:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    if start > end:
        raise ValueError("start_date must not exceed end_date")
    return [str(period).replace("-", "") for period in pd.period_range(start, end, freq="M")]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode_cffex_csv(payload: bytes) -> str:
    for encoding in ("gb18030", "gbk", "utf-8-sig"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CffexDataError("CFFEX daily CSV is not decodable as GB18030/GBK/UTF-8")


def parse_cffex_daily_csv(payload: bytes, trade_date: str, source_file: str) -> pd.DataFrame:
    normalized_date = str(trade_date).replace("-", "")
    if not re.fullmatch(r"\d{8}", normalized_date):
        raise ValueError(f"invalid CFFEX trade_date: {trade_date}")
    frame = pd.read_csv(StringIO(_decode_cffex_csv(payload)), dtype="string", keep_default_na=False)
    frame.columns = [str(column).strip() for column in frame.columns]
    missing = sorted(_REQUIRED_SOURCE_COLUMNS - set(frame.columns))
    if missing:
        raise CffexDataError(f"CFFEX CSV missing required columns: {','.join(missing)}")
    for optional in ("持仓变化", "Delta"):
        if optional not in frame.columns:
            frame[optional] = ""
    frame = frame.rename(columns=_SOURCE_COLUMNS)
    frame["contract"] = frame["contract"].astype("string").str.strip()

    future_parts = frame["contract"].str.extract(_FUTURE_PATTERN)
    option_parts = frame["contract"].str.extract(_OPTION_PATTERN)
    valid_future = future_parts[0].notna()
    valid_option = option_parts[0].notna()
    frame = frame.loc[valid_future | valid_option].copy()
    future_parts = future_parts.loc[frame.index]
    option_parts = option_parts.loc[frame.index]
    if frame.empty:
        raise CffexDataError(f"CFFEX CSV has no supported equity-index contracts: {source_file}")

    frame["product"] = future_parts[0].fillna(option_parts[0]).astype("string")
    frame["contract_month"] = future_parts[1].fillna(option_parts[1]).astype("string")
    frame["instrument_type"] = frame["product"].map(
        {product: metadata["instrument_type"] for product, metadata in CFFEX_PRODUCTS.items()}
    )
    frame["underlying_index"] = frame["product"].map(
        {product: metadata["underlying_index"] for product, metadata in CFFEX_PRODUCTS.items()}
    )
    frame["multiplier"] = frame["product"].map(
        {product: metadata["multiplier"] for product, metadata in CFFEX_PRODUCTS.items()}
    )
    frame["option_type"] = option_parts[2].map({"C": "call", "P": "put"}).fillna("").astype("string")
    frame["strike"] = pd.to_numeric(option_parts[3], errors="coerce")
    for column in _NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column].replace({"": pd.NA, "--": pd.NA}), errors="coerce")
    frame["open_executable"] = frame["open"].notna() & frame["open"].gt(0.0) & frame["volume"].gt(0.0)
    frame["settlement_mark_valid"] = frame["settle"].notna() & frame["settle"].ge(0.0)
    frame["trade_date"] = normalized_date
    frame["source"] = "cffex_official"
    frame["source_file"] = str(source_file)
    frame["source_tier"] = "official_exchange_daily"
    frame["execution_tier"] = "daily_settlement_no_quotes"
    frame = frame.loc[:, list(CFFEX_PANEL_COLUMNS)].reset_index(drop=True)
    if frame.duplicated(["trade_date", "contract"]).any():
        raise CffexDataError(f"duplicate CFFEX contract rows in {source_file}")
    if frame[["close", "settle", "pre_settle"]].isna().all(axis=1).any():
        raise CffexDataError(f"CFFEX rows missing close and settlement prices in {source_file}")
    return frame


def parse_cffex_month_zip(path: str | Path) -> pd.DataFrame:
    archive_path = Path(path)
    frames: list[pd.DataFrame] = []
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise CffexDataError(f"CFFEX ZIP failed CRC validation: {archive_path}")
        for member in sorted(archive.namelist()):
            match = _DAILY_MEMBER_PATTERN.fullmatch(Path(member).name)
            if not match:
                continue
            frames.append(
                parse_cffex_daily_csv(
                    archive.read(member),
                    trade_date=match.group("date"),
                    source_file=f"{archive_path.name}:{member}",
                )
            )
    if not frames:
        raise CffexDataError(f"CFFEX ZIP has no daily CSV members: {archive_path}")
    frame = pd.concat(frames, ignore_index=True, sort=False)
    if frame.duplicated(["trade_date", "contract"]).any():
        raise CffexDataError(f"duplicate trade_date/contract rows across CFFEX ZIP: {archive_path}")
    return frame


def validate_cffex_month_zip(path: str | Path, month: str) -> dict[str, object]:
    archive_path = Path(path)
    normalized_month = str(month).replace("-", "")
    if not archive_path.is_file():
        raise CffexDataError(f"CFFEX ZIP is missing: {archive_path}")
    frame = parse_cffex_month_zip(archive_path)
    months = frame["trade_date"].astype(str).str[:6].unique().tolist()
    if months != [normalized_month]:
        raise CffexDataError(f"CFFEX ZIP month mismatch: expected {normalized_month}, found {months}")
    return {
        "entries": int(frame["trade_date"].nunique()),
        "rows": int(len(frame)),
        "first_date": str(frame["trade_date"].min()),
        "last_date": str(frame["trade_date"].max()),
        "products": ",".join(sorted(frame["product"].astype(str).unique())),
    }


def download_cffex_month(
    data_root: str | Path,
    month: str,
    interface: str = "en0",
    dns_server: str | None = None,
    timeout_seconds: float = 60.0,
    fetcher: Callable[..., DirectHttpResponse] = physical_http_get,
) -> CffexMonthDownload:
    normalized_month = str(month).replace("-", "")
    url = cffex_month_url(normalized_month)
    path = Path(data_root) / "raw" / "cffex" / "monthly" / f"{normalized_month}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            summary = validate_cffex_month_zip(path, normalized_month)
        except (CffexDataError, OSError, zipfile.BadZipFile):
            quarantine = path.with_name(f"{path.name}.invalid.{time.time_ns()}")
            path.replace(quarantine)
        else:
            return CffexMonthDownload(
                month=normalized_month,
                url=url,
                path=str(path),
                status="cached_valid",
                bytes=int(path.stat().st_size),
                sha256=_sha256(path),
                local_ip="",
                remote_ip="",
                interface=interface,
                **summary,
            )

    response = fetcher(
        url,
        interface=interface,
        dns_server=dns_server,
        timeout_seconds=timeout_seconds,
        max_bytes=20 * 1024 * 1024,
    )
    if response.status != 200:
        raise CffexDataError(f"CFFEX month {normalized_month} returned HTTP {response.status} {response.reason}")
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_bytes(response.body)
        summary = validate_cffex_month_zip(tmp_path, normalized_month)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return CffexMonthDownload(
        month=normalized_month,
        url=url,
        path=str(path),
        status="downloaded",
        bytes=int(path.stat().st_size),
        sha256=_sha256(path),
        local_ip=response.route.connected_local[0] if response.route.connected_local else "",
        remote_ip=response.route.connected_peer[0] if response.route.connected_peer else response.route.resolved_ip,
        interface=response.route.interface,
        **summary,
    )


def write_cffex_download_manifest(path: str | Path, records: Iterable[CffexMonthDownload]) -> Path:
    manifest_path = Path(path)
    incoming = pd.DataFrame([asdict(record) for record in records])
    if manifest_path.exists():
        current = pd.read_csv(manifest_path, dtype={"month": "string"})
        incoming = pd.concat([current, incoming], ignore_index=True, sort=False)
    if incoming.empty:
        return manifest_path
    incoming["month"] = incoming["month"].astype("string").str.zfill(6)
    incoming = incoming.drop_duplicates("month", keep="last").sort_values("month").reset_index(drop=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    incoming.to_csv(tmp_path, index=False)
    tmp_path.replace(manifest_path)
    return manifest_path


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def cffex_panel_manifest_path(panel_path: str | Path) -> Path:
    path = Path(panel_path)
    return path.with_suffix(path.suffix + ".manifest.json")


def build_cffex_contract_panel(
    archive_paths: Iterable[str | Path],
    panel_path: str | Path,
) -> Path:
    archives = sorted({Path(path) for path in archive_paths})
    if not archives:
        raise CffexDataError("no CFFEX monthly archives were supplied")
    output = Path(panel_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output.with_name(f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
    writer: pq.ParquetWriter | None = None
    rows = 0
    first_date = ""
    last_date = ""
    products: set[str] = set()
    source_archives: list[dict[str, object]] = []
    try:
        for archive_path in archives:
            month = archive_path.stem
            validate_cffex_month_zip(archive_path, month)
            frame = parse_cffex_month_zip(archive_path)
            table = pa.Table.from_pandas(frame, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(tmp_path, table.schema, compression="zstd")
            else:
                table = table.cast(writer.schema)
            writer.write_table(table)
            rows += int(len(frame))
            frame_first = str(frame["trade_date"].min())
            frame_last = str(frame["trade_date"].max())
            first_date = frame_first if not first_date else min(first_date, frame_first)
            last_date = max(last_date, frame_last)
            products.update(map(str, frame["product"].unique()))
            source_archives.append(
                {
                    "month": month,
                    "path": str(archive_path),
                    "bytes": int(archive_path.stat().st_size),
                    "sha256": _sha256(archive_path),
                }
            )
        if writer is None or rows == 0:
            raise CffexDataError("CFFEX panel build produced no rows")
        writer.close()
        writer = None
        tmp_path.replace(output)
    finally:
        if writer is not None:
            writer.close()
        if tmp_path.exists():
            tmp_path.unlink()

    source_set_sha256 = hashlib.sha256(
        json.dumps(source_archives, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "panel_path": str(output),
        "panel_sha256": _sha256(output),
        "rows": rows,
        "months": len(source_archives),
        "first_date": first_date,
        "last_date": last_date,
        "products": sorted(products),
        "source_tier": "official_exchange_daily",
        "execution_tier": "daily_settlement_no_quotes",
        "source_set_sha256": source_set_sha256,
        "parser_source_sha256": _sha256(Path(__file__)),
        "source_archives": source_archives,
    }
    _atomic_write_json(cffex_panel_manifest_path(output), manifest)
    return output


def validate_cffex_panel_manifest(panel_path: str | Path) -> dict[str, object]:
    output = Path(panel_path)
    manifest_path = cffex_panel_manifest_path(output)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CffexDataError(f"CFFEX panel manifest is unreadable: {manifest_path}") from exc
    if int(manifest.get("schema_version", 0)) != 1:
        raise CffexDataError("unsupported CFFEX panel manifest schema")
    if not output.is_file() or manifest.get("panel_sha256") != _sha256(output):
        raise CffexDataError("CFFEX panel hash does not match manifest")
    if manifest.get("parser_source_sha256") != _sha256(Path(__file__)):
        raise CffexDataError("CFFEX panel was built by a stale parser")
    parquet = pq.ParquetFile(output)
    missing = sorted(set(CFFEX_PANEL_COLUMNS) - set(parquet.schema.names))
    if missing:
        raise CffexDataError(f"CFFEX panel missing columns: {','.join(missing)}")
    if int(manifest.get("rows", -1)) != int(parquet.metadata.num_rows):
        raise CffexDataError("CFFEX panel row count does not match manifest")
    return manifest


def cffex_contract_master_manifest_path(master_path: str | Path) -> Path:
    path = Path(master_path)
    return path.with_suffix(path.suffix + ".manifest.json")


def build_cffex_contract_master(
    panel_path: str | Path,
    master_path: str | Path,
) -> Path:
    panel = Path(panel_path)
    panel_manifest = validate_cffex_panel_manifest(panel)
    columns = [
        "trade_date",
        "contract",
        "product",
        "instrument_type",
        "underlying_index",
        "contract_month",
        "option_type",
        "strike",
        "multiplier",
        "open",
        "volume",
        "source_tier",
    ]
    daily = pd.read_parquet(panel, columns=columns)
    static_columns = [
        "product",
        "instrument_type",
        "underlying_index",
        "contract_month",
        "option_type",
        "strike",
        "multiplier",
        "source_tier",
    ]
    static_counts = daily.groupby("contract", sort=True, observed=True)[static_columns].nunique(dropna=False)
    if bool(static_counts.gt(1).any().any()):
        raise CffexDataError("CFFEX contract metadata changes within the daily panel")
    grouped = daily.groupby("contract", sort=True, observed=True)
    master = grouped[static_columns].first().reset_index()
    master["first_observation_date"] = master["contract"].map(grouped["trade_date"].min())
    master["last_trade_date"] = master["contract"].map(grouped["trade_date"].max())
    master["trading_days"] = master["contract"].map(grouped["trade_date"].nunique()).astype(int)
    master["total_volume"] = master["contract"].map(grouped["volume"].sum(min_count=1)).fillna(0.0)
    master["open_price_days"] = master["contract"].map(
        daily["open"].notna().groupby(daily["contract"], sort=True).sum()
    ).astype(int)
    option_mask = master["instrument_type"].astype(str).eq("option")
    master["exercise_style"] = ""
    master.loc[option_mask, "exercise_style"] = "european"
    master["settlement_style"] = "daily_mtm_cash_delivery"
    master.loc[option_mask, "settlement_style"] = "premium_cash_settled"
    master["expiry_source"] = "last_official_daily_record"
    master = master.loc[:, list(CFFEX_CONTRACT_MASTER_COLUMNS)].sort_values("contract").reset_index(drop=True)

    output = Path(master_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output.with_name(f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        master.to_parquet(tmp_path, index=False, compression="zstd")
        tmp_path.replace(output)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    manifest = {
        "schema_version": 1,
        "master_path": str(output),
        "master_sha256": _sha256(output),
        "rows": int(len(master)),
        "first_observation_date": str(master["first_observation_date"].min()),
        "last_trade_date": str(master["last_trade_date"].max()),
        "products": sorted(master["product"].astype(str).unique()),
        "source_panel_path": str(panel),
        "source_panel_sha256": str(panel_manifest["panel_sha256"]),
        "builder_source_sha256": _sha256(Path(__file__)),
        "expiry_source": "last_official_daily_record",
    }
    _atomic_write_json(cffex_contract_master_manifest_path(output), manifest)
    return output


def validate_cffex_contract_master_manifest(
    master_path: str | Path,
    panel_path: str | Path,
) -> dict[str, object]:
    master = Path(master_path)
    panel_manifest = validate_cffex_panel_manifest(panel_path)
    manifest_path = cffex_contract_master_manifest_path(master)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CffexDataError(f"CFFEX contract master manifest is unreadable: {manifest_path}") from exc
    if int(manifest.get("schema_version", 0)) != 1:
        raise CffexDataError("unsupported CFFEX contract master manifest schema")
    if not master.is_file() or manifest.get("master_sha256") != _sha256(master):
        raise CffexDataError("CFFEX contract master hash does not match manifest")
    if manifest.get("source_panel_sha256") != panel_manifest.get("panel_sha256"):
        raise CffexDataError("CFFEX contract master is bound to a different daily panel")
    if manifest.get("builder_source_sha256") != _sha256(Path(__file__)):
        raise CffexDataError("CFFEX contract master was built by a stale builder")
    parquet = pq.ParquetFile(master)
    missing = sorted(set(CFFEX_CONTRACT_MASTER_COLUMNS) - set(parquet.schema.names))
    if missing:
        raise CffexDataError(f"CFFEX contract master missing columns: {','.join(missing)}")
    if int(manifest.get("rows", -1)) != int(parquet.metadata.num_rows):
        raise CffexDataError("CFFEX contract master row count does not match manifest")
    return manifest

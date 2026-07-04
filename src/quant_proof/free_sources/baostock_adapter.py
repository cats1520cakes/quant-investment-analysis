from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml

from .code_map import baostock_to_ts_code


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FreeRealConfig:
    raw: dict
    path: Path

    @property
    def data_root(self) -> Path:
        return Path(self.raw["data_root"]).expanduser()

    @property
    def start_date(self) -> str:
        return str(self.raw.get("date_range", {}).get("start_date") or "20100101")

    @property
    def end_date(self) -> str:
        configured = self.raw.get("date_range", {}).get("end_date")
        return str(configured) if configured else datetime.now().strftime("%Y%m%d")

    @property
    def baostock(self) -> dict:
        return self.raw.get("sources", {}).get("baostock", {})

    @property
    def manifest_path(self) -> Path:
        return self.data_root / self.raw["paths"]["manifest"]


def load_config(path: str | Path) -> FreeRealConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return FreeRealConfig(raw=raw, path=config_path)


def ensure_dirs(config: FreeRealConfig) -> None:
    for rel in [
        "raw/baostock",
        "raw/baostock/daily_raw",
        "raw/baostock/daily_qfq",
        "00_meta/manifests",
        "00_meta/errors",
    ]:
        (config.data_root / rel).mkdir(parents=True, exist_ok=True)


def normalize_date_for_baostock(value: str) -> str:
    text = str(value).strip()
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if "-" not in text else text


def baostock_result_to_frame(result) -> pd.DataFrame:
    rows = []
    while result.error_code == "0" and result.next():
        rows.append(result.get_row_data())
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock query failed: {result.error_msg}")
    return pd.DataFrame(rows, columns=result.fields)


def normalize_stock_basic(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["source_code"] = out["code"].astype(str)
    out["ts_code"] = out["source_code"].map(baostock_to_ts_code)
    rename = {
        "code_name": "name",
        "ipoDate": "list_date",
        "outDate": "delist_date",
        "status": "list_status",
    }
    out = out.rename(columns=rename)
    for column in ["type", "list_status", "name", "list_date", "delist_date"]:
        if column not in out.columns:
            out[column] = pd.NA
    keep = ["ts_code", "source_code", "name", "list_date", "delist_date", "type", "list_status"]
    return out.loc[:, keep].drop_duplicates("ts_code").reset_index(drop=True)


def normalize_trade_calendar(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "calendar_date" in out.columns and "date" not in out.columns:
        out = out.rename(columns={"calendar_date": "date"})
    out["trade_date"] = out["date"].astype(str).str.replace("-", "", regex=False)
    out["is_open"] = pd.to_numeric(out.get("is_trading_day", out.get("is_open")), errors="coerce").fillna(0).astype(int)
    return out.loc[:, ["trade_date", "is_open"]].drop_duplicates("trade_date").sort_values("trade_date").reset_index(drop=True)


def normalize_daily(frame: pd.DataFrame, signal: bool) -> pd.DataFrame:
    out = frame.copy()
    if out.empty:
        return out
    out["trade_date"] = out["date"].astype(str).str.replace("-", "", regex=False)
    out["source_code"] = out["code"].astype(str)
    out["ts_code"] = out["source_code"].map(baostock_to_ts_code)
    rename = {
        "preclose": "pre_close",
        "turn": "turnover_rate",
        "pctChg": "pct_chg",
        "peTTM": "pe_ttm",
        "pbMRQ": "pb",
        "psTTM": "ps_ttm",
        "pcfNcfTTM": "pcf_ttm",
        "tradestatus": "trade_status",
        "isST": "is_st_raw",
    }
    out = out.rename(columns=rename)
    numeric = [
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "amount",
        "turnover_rate",
        "pct_chg",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "pcf_ttm",
        "trade_status",
        "is_st_raw",
    ]
    for column in numeric:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    if signal:
        out = out.rename(
            columns={
                "open": "adj_open_for_signal",
                "high": "adj_high_for_signal",
                "low": "adj_low_for_signal",
                "close": "adj_close_for_signal",
            }
        )
        keep = ["trade_date", "ts_code", "source_code", "adj_open_for_signal", "adj_high_for_signal", "adj_low_for_signal", "adj_close_for_signal"]
    else:
        keep = [
            "trade_date",
            "ts_code",
            "source_code",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "volume",
            "amount",
            "turnover_rate",
            "pct_chg",
            "pe_ttm",
            "pb",
            "ps_ttm",
            "pcf_ttm",
            "trade_status",
            "is_st_raw",
        ]
    for column in keep:
        if column not in out.columns:
            out[column] = pd.NA
    return out.loc[:, keep].drop_duplicates(["trade_date", "ts_code"], keep="last").reset_index(drop=True)


def write_frame(path: Path, frame: pd.DataFrame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.to_parquet(tmp_path, index=False)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return path


def append_error(config: FreeRealConfig, table: str, message: str) -> None:
    path = config.data_root / "00_meta" / "errors" / f"phase2_free_{table}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().isoformat()} {message}\n")


def manifest_record(source: str, table: str, name: str, path: Path, frame: pd.DataFrame, data_tier: str = "free_real") -> dict:
    return {
        "data_tier": data_tier,
        "source": source,
        "table": table,
        "name": name,
        "path": str(path),
        "rows": int(len(frame)),
        "columns": ",".join(map(str, frame.columns)),
        "sha256": checksum(path) if path.exists() else "",
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
    }


class BaoStockClient:
    def __init__(self, config: FreeRealConfig, retries: int = 3, sleep_seconds: float = 0.2):
        import baostock as bs

        self.bs = bs
        self.config = config
        self.retries = retries
        self.sleep_seconds = sleep_seconds

    def __enter__(self) -> "BaoStockClient":
        login = self.bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.bs.logout()

    def _retry(self, func, *args, **kwargs) -> pd.DataFrame:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                frame = func(*args, **kwargs)
                if self.sleep_seconds > 0:
                    time.sleep(self.sleep_seconds)
                return frame
            except Exception as exc:  # noqa: BLE001 - keep batch downloads alive with exact error reporting.
                last_error = exc
                time.sleep(min(attempt * self.sleep_seconds * 4, 4.0))
        raise RuntimeError(f"BaoStock query failed after {self.retries} attempts: {last_error}") from last_error

    def stock_basic(self) -> pd.DataFrame:
        return normalize_stock_basic(self._retry(lambda: baostock_result_to_frame(self.bs.query_stock_basic())))

    def trade_calendar(self) -> pd.DataFrame:
        start = normalize_date_for_baostock(self.config.start_date)
        end = normalize_date_for_baostock(self.config.end_date)
        return normalize_trade_calendar(self._retry(lambda: baostock_result_to_frame(self.bs.query_trade_dates(start_date=start, end_date=end))))

    def daily(self, source_code: str, adjustflag: str, signal: bool) -> pd.DataFrame:
        fields = ",".join(self.config.baostock.get("fields_raw", []))
        start = normalize_date_for_baostock(self.config.start_date)
        end = normalize_date_for_baostock(self.config.end_date)
        return normalize_daily(
            self._retry(
                lambda: baostock_result_to_frame(
                    self.bs.query_history_k_data_plus(
                        source_code,
                        fields,
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag=str(adjustflag),
                    )
                )
            ),
            signal=signal,
        )


def write_manifest(config: FreeRealConfig, records: list[dict]) -> Path:
    if not records:
        return config.manifest_path
    import fcntl

    config.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = config.manifest_path.with_suffix(config.manifest_path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        frame = pd.DataFrame(records)
        if config.manifest_path.exists():
            old = pd.read_csv(config.manifest_path)
            frame = pd.concat([old, frame], ignore_index=True)
        frame = frame.drop_duplicates(["data_tier", "source", "table", "name", "path"], keep="last")
        tmp_path = config.manifest_path.with_name(f".{config.manifest_path.name}.{os.getpid()}.tmp")
        try:
            frame.to_csv(tmp_path, index=False, encoding="utf-8")
            tmp_path.replace(config.manifest_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        fcntl.flock(lock, fcntl.LOCK_UN)
    return config.manifest_path


def select_codes(
    stock_basic: pd.DataFrame,
    max_codes: int | None,
    start_index: int = 0,
    end_index: int | None = None,
) -> list[str]:
    frame = stock_basic.copy()
    if "type" in frame.columns:
        frame = frame.loc[frame["type"].astype(str) == "1"]
    if "list_status" in frame.columns:
        frame = frame.loc[frame["list_status"].astype(str) == "1"]
    codes = frame["source_code"].dropna().astype(str).sort_values().tolist()
    if max_codes:
        codes = codes[:max_codes]
    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    if end_index is not None and end_index < start_index:
        raise ValueError("end_index must be greater than or equal to start_index")
    return codes[start_index:end_index]


def download_baostock_free_real(
    config: FreeRealConfig,
    max_codes: int | None = None,
    force: bool = False,
    start_index: int = 0,
    end_index: int | None = None,
) -> Path:
    ensure_dirs(config)
    with BaoStockClient(config) as client:
        stock_basic = client.stock_basic()
        stock_basic_path = write_frame(config.data_root / "raw/baostock/stock_basic.parquet", stock_basic)
        write_manifest(config, [manifest_record("baostock", "stock_basic", "all", stock_basic_path, stock_basic)])

        trade_calendar = client.trade_calendar()
        trade_calendar_path = write_frame(config.data_root / "raw/baostock/trade_calendar.parquet", trade_calendar)
        write_manifest(config, [manifest_record("baostock", "trade_calendar", "all", trade_calendar_path, trade_calendar)])

        codes = select_codes(stock_basic, max_codes=max_codes, start_index=start_index, end_index=end_index)
        for i, source_code in enumerate(codes, start=1):
            print(f"[download] {i}/{len(codes)} {source_code}", flush=True)
            raw_path = config.data_root / "raw" / "baostock" / "daily_raw" / f"{source_code.replace('.', '_')}.parquet"
            qfq_path = config.data_root / "raw" / "baostock" / "daily_qfq" / f"{source_code.replace('.', '_')}.parquet"
            try:
                if raw_path.exists() and qfq_path.exists() and not force:
                    raw = pd.read_parquet(raw_path)
                    qfq = pd.read_parquet(qfq_path)
                else:
                    raw = client.daily(source_code, adjustflag=str(config.baostock.get("adjustflag_raw", "3")), signal=False)
                    qfq = client.daily(source_code, adjustflag=str(config.baostock.get("adjustflag_signal", "2")), signal=True)
                    write_frame(raw_path, raw)
                    write_frame(qfq_path, qfq)
                write_manifest(
                    config,
                    [
                        manifest_record("baostock", "daily_raw", source_code, raw_path, raw),
                        manifest_record("baostock", "daily_qfq", source_code, qfq_path, qfq),
                    ],
                )
            except Exception as exc:  # noqa: BLE001 - free source can be flaky; keep batch resumable.
                message = f"{source_code}: {type(exc).__name__}: {exc}"
                print(f"[fail] {message}", flush=True)
                append_error(config, "daily", message)
    return config.manifest_path


def iter_existing_daily_files(config: FreeRealConfig, table: str) -> Iterable[Path]:
    for path in sorted((config.data_root / "raw" / "baostock" / table).glob("*.parquet")):
        if path.name.startswith(".") or path.name.startswith("._"):
            continue
        yield path

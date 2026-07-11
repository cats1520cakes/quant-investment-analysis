from __future__ import annotations

import hashlib
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal

import pandas as pd
import yaml

from .code_map import baostock_to_ts_code
from .daily_integrity import (
    inspect_daily_pairs,
    invalidate_daily_integrity_cache,
    require_valid_daily_pair_frames,
)


UniverseScope = Literal["current", "point_in_time", "all_type1"]
UNIVERSE_SCOPES: tuple[UniverseScope, ...] = ("current", "point_in_time", "all_type1")


@contextmanager
def baostock_login_lock(data_root: Path):
    """Serialize BaoStock logins because a second session invalidates the first."""

    import fcntl

    lock_path = Path(data_root) / "00_meta" / "locks" / "baostock_login.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock.seek(0)
        lock.truncate()
        lock.write(f"pid={os.getpid()} acquired_at={datetime.now().isoformat()}\n")
        lock.flush()
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


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
        keep = [
            "trade_date",
            "ts_code",
            "source_code",
            "adj_open_for_signal",
            "adj_high_for_signal",
            "adj_low_for_signal",
            "adj_close_for_signal",
            "trade_status",
        ]
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


def _write_csv(path: Path, frame: pd.DataFrame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.to_csv(tmp_path, index=False, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return path


def _write_lines(path: Path, values: Iterable[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return path


def append_error(config: FreeRealConfig, table: str, message: str) -> None:
    path = config.data_root / "00_meta" / "errors" / f"phase2_free_{table}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat()} {message}\n")
    except OSError as exc:
        print(f"[warn] could not append error log {path}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


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
    universe_scope: UniverseScope = "current",
    universe_start_date: str = "19000101",
    universe_end_date: str = "29991231",
) -> list[str]:
    frame = select_stock_universe(
        stock_basic,
        universe_scope=universe_scope,
        universe_start_date=universe_start_date,
        universe_end_date=universe_end_date,
    )
    codes = frame["source_code"].dropna().astype(str).sort_values().tolist()
    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    if end_index is not None and end_index < start_index:
        raise ValueError("end_index must be greater than or equal to start_index")
    codes = codes[start_index:end_index]
    return codes[:max_codes] if max_codes else codes


def select_stock_universe(
    stock_basic: pd.DataFrame,
    universe_scope: UniverseScope = "current",
    universe_start_date: str = "19000101",
    universe_end_date: str = "29991231",
) -> pd.DataFrame:
    if universe_scope not in UNIVERSE_SCOPES:
        raise ValueError(f"unsupported universe_scope={universe_scope}; expected one of {UNIVERSE_SCOPES}")
    frame = stock_basic.copy()
    if "type" in frame.columns:
        frame = frame.loc[frame["type"].astype(str) == "1"]
    if universe_scope == "current" and "list_status" in frame.columns:
        frame = frame.loc[frame["list_status"].astype(str) == "1"]
    if universe_scope == "point_in_time":
        list_date = frame["list_date"].astype("string").str.replace("-", "", regex=False).str.slice(0, 8)
        delist_date = frame["delist_date"].astype("string").str.replace("-", "", regex=False).str.slice(0, 8)
        listed_by_end = list_date.notna() & list_date.ne("") & list_date.le(str(universe_end_date))
        alive_after_start = delist_date.isna() | delist_date.eq("") | delist_date.ge(str(universe_start_date))
        frame = frame.loc[listed_by_end & alive_after_start]
    return frame.sort_values("source_code").reset_index(drop=True)


def freeze_universe(
    config: FreeRealConfig,
    stock_basic: pd.DataFrame,
    universe_scope: UniverseScope,
    codes: list[str],
) -> tuple[Path, Path]:
    selected = stock_basic.loc[stock_basic["source_code"].astype(str).isin(codes)].copy()
    selected = selected.sort_values("source_code").reset_index(drop=True)
    selected.insert(0, "universe_scope", universe_scope)
    selected.insert(1, "universe_start_date", config.start_date)
    selected.insert(2, "universe_end_date", config.end_date)
    stem = f"phase2_free_{universe_scope}_{config.start_date}_{config.end_date}"
    csv_path = _write_csv(config.data_root / "00_meta" / "universes" / f"{stem}.csv", selected)
    codes_path = _write_lines(config.data_root / "00_meta" / "universes" / f"{stem}.txt", codes)
    return csv_path, codes_path


def _load_or_download_metadata(
    config: FreeRealConfig,
    client: BaoStockClient,
    refresh_metadata: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    stock_basic_path = config.data_root / "raw/baostock/stock_basic.parquet"
    trade_calendar_path = config.data_root / "raw/baostock/trade_calendar.parquet"
    records: list[dict] = []
    if stock_basic_path.exists() and not refresh_metadata:
        stock_basic = pd.read_parquet(stock_basic_path)
    else:
        stock_basic = client.stock_basic()
        write_frame(stock_basic_path, stock_basic)
        records.append(manifest_record("baostock", "stock_basic", "all", stock_basic_path, stock_basic))
    if trade_calendar_path.exists() and not refresh_metadata:
        trade_calendar = pd.read_parquet(trade_calendar_path)
    else:
        trade_calendar = client.trade_calendar()
        write_frame(trade_calendar_path, trade_calendar)
        records.append(manifest_record("baostock", "trade_calendar", "all", trade_calendar_path, trade_calendar))
    return stock_basic, trade_calendar, records


def download_baostock_free_real(
    config: FreeRealConfig,
    max_codes: int | None = None,
    force: bool = False,
    start_index: int = 0,
    end_index: int | None = None,
    codes_override: list[str] | None = None,
    universe_scope: UniverseScope = "current",
    refresh_metadata: bool = False,
    metadata_only: bool = False,
    manifest_batch_size: int = 50,
) -> Path:
    ensure_dirs(config)
    if manifest_batch_size <= 0:
        raise ValueError("manifest_batch_size must be positive")
    failures: list[tuple[str, str]] = []
    with baostock_login_lock(config.data_root), BaoStockClient(config) as client:
        stock_basic, _trade_calendar, metadata_records = _load_or_download_metadata(
            config,
            client,
            refresh_metadata=refresh_metadata,
        )
        write_manifest(config, metadata_records)

        selected_codes = select_codes(
            stock_basic,
            max_codes=max_codes,
            start_index=start_index,
            end_index=end_index,
            universe_scope=universe_scope,
            universe_start_date=config.start_date,
            universe_end_date=config.end_date,
        )
        if codes_override is None:
            codes = selected_codes
            universe_csv, universe_codes = freeze_universe(config, stock_basic, universe_scope, codes)
            print(f"[universe] scope={universe_scope} codes={len(codes)} csv={universe_csv} codes_file={universe_codes}", flush=True)
        else:
            valid_codes = set(stock_basic["source_code"].dropna().astype(str))
            codes = list(dict.fromkeys(map(str, codes_override)))
            unknown = sorted(set(codes) - valid_codes)
            if unknown:
                preview = ", ".join(unknown[:10])
                raise ValueError(f"codes_override contains {len(unknown)} codes absent from stock_basic: {preview}")
            if max_codes:
                codes = codes[:max_codes]
        if metadata_only:
            return config.manifest_path

        existing_integrity = {} if force else inspect_daily_pairs(config.data_root, codes)
        pending_manifest: list[dict] = []
        for i, source_code in enumerate(codes, start=1):
            print(f"[download] {i}/{len(codes)} {source_code}", flush=True)
            raw_path = config.data_root / "raw" / "baostock" / "daily_raw" / f"{source_code.replace('.', '_')}.parquet"
            qfq_path = config.data_root / "raw" / "baostock" / "daily_qfq" / f"{source_code.replace('.', '_')}.parquet"
            try:
                if not force and existing_integrity[source_code].complete:
                    print(f"[skip] {source_code} existing daily pair passed integrity checks", flush=True)
                    continue
                if not force and (raw_path.exists() or qfq_path.exists()):
                    print(
                        f"[repair] {source_code} {existing_integrity[source_code].error_summary}",
                        flush=True,
                    )
                raw = client.daily(source_code, adjustflag=str(config.baostock.get("adjustflag_raw", "3")), signal=False)
                qfq = client.daily(source_code, adjustflag=str(config.baostock.get("adjustflag_signal", "2")), signal=True)
                require_valid_daily_pair_frames(raw, qfq, source_code)
                invalidate_daily_integrity_cache(config.data_root, [source_code])
                write_frame(raw_path, raw)
                write_frame(qfq_path, qfq)
                pending_manifest.extend(
                    (
                        manifest_record("baostock", "daily_raw", source_code, raw_path, raw),
                        manifest_record("baostock", "daily_qfq", source_code, qfq_path, qfq),
                    )
                )
                if i % manifest_batch_size == 0:
                    write_manifest(config, pending_manifest)
                    pending_manifest.clear()
            except Exception as exc:  # noqa: BLE001 - free source can be flaky; keep batch resumable.
                message = f"{source_code}: {type(exc).__name__}: {exc}"
                print(f"[fail] {message}", flush=True)
                append_error(config, "daily", message)
                failures.append((source_code, f"{type(exc).__name__}: {exc}"))
        write_manifest(config, pending_manifest)
    if failures:
        preview = " | ".join(f"{code}: {message}" for code, message in failures[:10])
        if len(failures) > 10:
            preview += f" | ... {len(failures) - 10} more"
        print(f"[summary] daily_failures={len(failures)}/{len(codes)} {preview}", file=sys.stderr, flush=True)
        raise RuntimeError(f"BaoStock daily download failed for {len(failures)} of {len(codes)} codes: {preview}")
    return config.manifest_path


def iter_existing_daily_files(config: FreeRealConfig, table: str) -> Iterable[Path]:
    for path in sorted((config.data_root / "raw" / "baostock" / table).glob("*.parquet")):
        if path.name.startswith(".") or path.name.startswith("._"):
            continue
        yield path

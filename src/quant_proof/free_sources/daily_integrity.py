from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from .code_map import baostock_to_ts_code


DAILY_KEY_COLUMNS: tuple[str, ...] = ("trade_date", "ts_code")
DAILY_RAW_REQUIRED_COLUMNS: tuple[str, ...] = (
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
)
DAILY_QFQ_REQUIRED_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "ts_code",
    "source_code",
    "adj_open_for_signal",
    "adj_high_for_signal",
    "adj_low_for_signal",
    "adj_close_for_signal",
)
REQUIRED_COLUMNS_BY_TABLE = {
    "daily_raw": DAILY_RAW_REQUIRED_COLUMNS,
    "daily_qfq": DAILY_QFQ_REQUIRED_COLUMNS,
}

CACHE_VERSION = 2
DEFAULT_CACHE_PATH = Path("00_meta/daily_integrity_cache.json")


class DailyIntegrityError(ValueError):
    """Raised when an in-memory BaoStock daily pair is not safe to write."""


@dataclass(frozen=True)
class DailyFileIntegrity:
    table: str
    valid: bool
    rows: int
    key_digest: str
    errors: tuple[str, ...]
    cache_hit: bool = False

    def to_cache_record(self) -> dict:
        return {
            "table": self.table,
            "valid": self.valid,
            "rows": self.rows,
            "key_digest": self.key_digest,
            "errors": list(self.errors),
        }

    @classmethod
    def from_cache_record(cls, record: dict) -> "DailyFileIntegrity":
        return cls(
            table=str(record["table"]),
            valid=bool(record["valid"]),
            rows=int(record["rows"]),
            key_digest=str(record.get("key_digest", "")),
            errors=tuple(map(str, record.get("errors", []))),
            cache_hit=True,
        )


@dataclass(frozen=True)
class DailyPairIntegrity:
    source_code: str
    raw: DailyFileIntegrity
    qfq: DailyFileIntegrity
    keys_match: bool
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.raw.valid and self.qfq.valid and self.keys_match

    @property
    def raw_valid(self) -> bool:
        return self.raw.valid

    @property
    def qfq_valid(self) -> bool:
        return self.qfq.valid

    @property
    def cache_hit(self) -> bool:
        return self.raw.cache_hit and self.qfq.cache_hit

    @property
    def error_summary(self) -> str:
        return "; ".join(self.errors) if self.errors else "ok"


def daily_file_path(data_root: Path, table: str, source_code: str) -> Path:
    if table not in REQUIRED_COLUMNS_BY_TABLE:
        raise ValueError(f"unsupported daily table: {table}")
    filename = f"{source_code.replace('.', '_')}.parquet"
    return data_root / "raw" / "baostock" / table / filename


def _key_digest(frame: pd.DataFrame) -> str:
    keys = frame.loc[:, list(DAILY_KEY_COLUMNS)].astype("string")
    keys = keys.sort_values(list(DAILY_KEY_COLUMNS), kind="mergesort")
    digest = hashlib.sha256()
    for row in keys.itertuples(index=False, name=None):
        digest.update(json.dumps(list(row), ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_daily_frame(frame: pd.DataFrame, table: str, source_code: str) -> DailyFileIntegrity:
    try:
        required = REQUIRED_COLUMNS_BY_TABLE[table]
    except KeyError as exc:
        raise ValueError(f"unsupported daily table: {table}") from exc

    errors: list[str] = []
    rows = int(len(frame))
    if frame.empty:
        errors.append(f"{table}: frame is empty")

    missing = sorted(set(required) - set(map(str, frame.columns)))
    if missing:
        errors.append(f"{table}: missing required columns {','.join(missing)}")

    key_digest = ""
    if not set(DAILY_KEY_COLUMNS).issubset(frame.columns):
        pass
    else:
        keys = frame.loc[:, list(DAILY_KEY_COLUMNS)].astype("string")
        null_or_blank = keys.isna().to_numpy() | keys.fillna("").apply(
            lambda column: column.str.strip().eq("")
        ).to_numpy(dtype=bool)
        if bool(null_or_blank.any()):
            errors.append(f"{table}: key columns contain null or blank values")
        if bool(keys.duplicated(keep=False).any()):
            errors.append(f"{table}: duplicate trade_date/ts_code keys")
        if not bool(null_or_blank.any()):
            key_digest = _key_digest(frame)

    if "source_code" in frame.columns:
        values = frame["source_code"].astype("string")
        incorrect = values.isna() | values.ne(source_code)
        if bool(incorrect.any()):
            examples = sorted(values.loc[incorrect].dropna().astype(str).unique().tolist())[:3]
            detail = f" ({','.join(examples)})" if examples else ""
            errors.append(f"{table}: source_code does not equal {source_code}{detail}")

    if "ts_code" in frame.columns:
        expected_ts_code = baostock_to_ts_code(source_code)
        ts_values = frame["ts_code"].astype("string")
        if bool((ts_values.isna() | ts_values.ne(expected_ts_code)).any()):
            errors.append(f"{table}: ts_code does not equal {expected_ts_code}")

    if "trade_date" in frame.columns:
        date_values = frame["trade_date"].astype("string").str.strip()
        invalid_dates = ~date_values.str.fullmatch(r"\d{8}", na=False)
        parsed_dates = pd.to_datetime(date_values, format="%Y%m%d", errors="coerce")
        if bool((invalid_dates | parsed_dates.isna()).any()):
            errors.append(f"{table}: trade_date contains invalid YYYYMMDD values")

    if table == "daily_qfq" and "adj_close_for_signal" in frame.columns:
        adjusted_close = pd.to_numeric(frame["adj_close_for_signal"], errors="coerce")
        nonpositive = adjusted_close.notna() & adjusted_close.le(0.0)
        if "trade_status" in frame.columns:
            trade_status = pd.to_numeric(frame["trade_status"], errors="coerce")
            invalid_status = trade_status.isna() | ~trade_status.isin({0, 1})
            if bool(invalid_status.any()):
                errors.append("daily_qfq: trade_status must be complete and binary")
            missing_executable = trade_status.eq(1) & adjusted_close.isna()
        else:
            missing_executable = adjusted_close.isna()
        if bool((nonpositive | missing_executable).any()):
            errors.append(
                "daily_qfq: adj_close_for_signal must be positive on executable rows; "
                "only non-trading rows may be missing"
            )

    return DailyFileIntegrity(
        table=table,
        valid=not errors,
        rows=rows,
        key_digest=key_digest,
        errors=tuple(errors),
    )


def _combine_pair(
    source_code: str,
    raw: DailyFileIntegrity,
    qfq: DailyFileIntegrity,
) -> DailyPairIntegrity:
    keys_match = bool(
        raw.valid
        and qfq.valid
        and raw.rows == qfq.rows
        and raw.key_digest
        and raw.key_digest == qfq.key_digest
    )
    errors = [*raw.errors, *qfq.errors]
    if raw.valid and qfq.valid and not keys_match:
        errors.append("daily_raw/daily_qfq keys do not match exactly")
    return DailyPairIntegrity(
        source_code=source_code,
        raw=raw,
        qfq=qfq,
        keys_match=keys_match,
        errors=tuple(errors),
    )


def validate_daily_pair_frames(
    raw: pd.DataFrame,
    qfq: pd.DataFrame,
    source_code: str,
) -> DailyPairIntegrity:
    return _combine_pair(
        source_code,
        validate_daily_frame(raw, "daily_raw", source_code),
        validate_daily_frame(qfq, "daily_qfq", source_code),
    )


def require_valid_daily_pair_frames(
    raw: pd.DataFrame,
    qfq: pd.DataFrame,
    source_code: str,
) -> DailyPairIntegrity:
    result = validate_daily_pair_frames(raw, qfq, source_code)
    if not result.complete:
        raise DailyIntegrityError(f"invalid daily pair for {source_code}: {result.error_summary}")
    return result


def _fingerprint(path: Path) -> dict:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    except OSError as exc:
        return {"exists": False, "stat_error": f"{type(exc).__name__}: {exc}"}
    return {"exists": True, "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _empty_file_result(table: str, error: str) -> DailyFileIntegrity:
    return DailyFileIntegrity(table=table, valid=False, rows=0, key_digest="", errors=(f"{table}: {error}",))


def _read_cache(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"version": CACHE_VERSION, "records": {}}
    if payload.get("version") != CACHE_VERSION or not isinstance(payload.get("records"), dict):
        return {"version": CACHE_VERSION, "records": {}}
    return payload


def _inspect_file(
    path: Path,
    table: str,
    source_code: str,
    cached: dict | None,
) -> tuple[DailyFileIntegrity, dict | None]:
    fingerprint = _fingerprint(path)
    if cached and cached.get("fingerprint") == fingerprint:
        try:
            result = DailyFileIntegrity.from_cache_record(cached["integrity"])
            if result.table == table:
                return result, None
        except (KeyError, TypeError, ValueError):
            pass

    if not fingerprint.get("exists"):
        detail = fingerprint.get("stat_error") or "file is missing"
        result = _empty_file_result(table, str(detail))
    else:
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001 - corrupt parquet must become an incomplete, repairable pair.
            result = _empty_file_result(table, f"file is unreadable ({type(exc).__name__}: {exc})")
        else:
            result = validate_daily_frame(frame, table, source_code)
    update = {"fingerprint": fingerprint, "integrity": result.to_cache_record()}
    return result, update


def _atomic_write_cache(path: Path, payload: dict) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _merge_cache_updates(cache_path: Path, updates: dict[str, dict[str, dict]]) -> None:
    if not updates:
        return
    import fcntl

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        payload = _read_cache(cache_path)
        records = payload.setdefault("records", {})
        for source_code, table_updates in updates.items():
            record = records.setdefault(source_code, {})
            record.update(table_updates)
        _atomic_write_cache(cache_path, payload)
        fcntl.flock(lock, fcntl.LOCK_UN)


def inspect_daily_pairs(
    data_root: Path,
    source_codes: Iterable[str],
    cache_path: Path | None = None,
) -> dict[str, DailyPairIntegrity]:
    root = Path(data_root)
    resolved_cache_path = cache_path or root / DEFAULT_CACHE_PATH
    cached_records = _read_cache(resolved_cache_path).get("records", {})
    updates: dict[str, dict[str, dict]] = {}
    results: dict[str, DailyPairIntegrity] = {}

    for source_code in dict.fromkeys(map(str, source_codes)):
        cached = cached_records.get(source_code, {})
        raw, raw_update = _inspect_file(
            daily_file_path(root, "daily_raw", source_code),
            "daily_raw",
            source_code,
            cached.get("daily_raw"),
        )
        qfq, qfq_update = _inspect_file(
            daily_file_path(root, "daily_qfq", source_code),
            "daily_qfq",
            source_code,
            cached.get("daily_qfq"),
        )
        table_updates = {}
        if raw_update is not None:
            table_updates["daily_raw"] = raw_update
        if qfq_update is not None:
            table_updates["daily_qfq"] = qfq_update
        if table_updates:
            updates[source_code] = table_updates
        results[source_code] = _combine_pair(source_code, raw, qfq)

    _merge_cache_updates(resolved_cache_path, updates)
    return results


def invalidate_daily_integrity_cache(
    data_root: Path,
    source_codes: Iterable[str],
    cache_path: Path | None = None,
) -> None:
    import fcntl

    root = Path(data_root)
    resolved_cache_path = cache_path or root / DEFAULT_CACHE_PATH
    if not resolved_cache_path.exists():
        return
    resolved_cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = resolved_cache_path.with_suffix(resolved_cache_path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        payload = _read_cache(resolved_cache_path)
        records = payload.setdefault("records", {})
        changed = False
        for source_code in map(str, source_codes):
            changed = records.pop(source_code, None) is not None or changed
        if changed:
            _atomic_write_cache(resolved_cache_path, payload)
        fcntl.flock(lock, fcntl.LOCK_UN)

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pandas as pd


ROOT = Path("artifacts/derived/phase3_multi_asset_official_data")
LEDGER = ROOT / "szse_159915_attempt_ledger.csv"
OUT = ROOT / "szse_159915_coverage_checkpoint_v2.json"


def atomic_json(payload: dict, path: Path) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        with open(temporary, "w") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


frame = pd.read_csv(LEDGER)
required = {"chunk", "rows", "sha256", "mode", "validated"}
if set(frame.columns) != required:
    raise RuntimeError(f"unexpected schema: {frame.columns.tolist()}")
dates = frame.chunk.astype(str).str.split("_").str[0]
ends = frame.chunk.astype(str).str.split("_").str[-1]
valid_date = dates.str.fullmatch(r"\d{8}") & ends.eq(dates)
success = frame.rows.eq(1) & frame.validated.eq(True) & frame.sha256.str.fullmatch(r"[0-9a-f]{64}") & valid_date
duplicates = int(dates.duplicated(keep=False).sum())
valid_unique = sorted(set(dates[success]))
parsed = pd.to_datetime(pd.Series(valid_unique), format="%Y%m%d", errors="coerce")
monotonic = bool(parsed.notna().all() and parsed.is_monotonic_increasing)
# A trading-calendar file is not present in the disaster checkpoint. Therefore
# contiguous means ledger sequence continuity (no duplicate/reversal), not
# calendar-day continuity; exchange-session gaps remain explicitly unproven.
payload = {
    "schema_version": 2,
    "code": "159915",
    "registered": int(len(frame)),
    "success": int(success.sum()),
    "validated": int(frame.validated.eq(True).sum()),
    "unique": int(len(valid_unique)),
    "contiguous": monotonic and duplicates == 0 and int(success.sum()) == len(valid_unique),
    "contiguous_semantics": "ordered_unique_registered_exchange_responses; official_trade_calendar_not_bound",
    "first_date": valid_unique[0] if valid_unique else None,
    "last_date": valid_unique[-1] if valid_unique else None,
    "missing": "unknown_until_official_trade_calendar_bound",
    "duplicate": duplicates,
    "hash_mismatch": "not_recomputable_without_raw; registered_hash_schema_valid_only",
    "attempt_ledger_sha256": hashlib.sha256(LEDGER.read_bytes()).hexdigest(),
    "old_checkpoint_completed_date_responses": 100,
    "reconciled_recoverable_count": int(len(valid_unique)),
    "raw_committed": False,
    "canonical_promoted": False,
    "strategy_run_permitted": False,
    "strict_candidates": 0,
}
atomic_json(payload, OUT)
print(json.dumps(payload, ensure_ascii=False, indent=2))

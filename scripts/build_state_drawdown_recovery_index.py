from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd


root = Path("artifacts/derived/phase3_sse_four_asset_state_drawdown_v1/run")
rows = []
for path in sorted((root / "daily_ledgers").glob("*.parquet")):
    rows.append({"path": str(path), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "scope": "recovery_specs_0085_0107"})
pd.DataFrame(rows).to_csv(root / "daily_ledger_index_recovery_23.csv", index=False)

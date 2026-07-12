from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pandas as pd


ROOT = Path("artifacts/derived/phase3_sse_four_asset_trend_risk_budget_v1")


def atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        frame.to_csv(temporary, index=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", required=True, choices=["IH", "IF", "IC", "IM"])
    args = parser.parse_args()
    family = f"sse_four_asset_trend_risk_budget_v1__{args.product}"
    root = ROOT / family
    parts = sorted((root / "parts").glob("*.csv"))
    if len(parts) != 108:
        raise RuntimeError(f"atomic part coverage is {len(parts)}/108")
    frames, errors = [], []
    for part in parts:
        frame = pd.read_csv(part)
        frames.append(frame)
        if len(frame) != 2 or set(frame.deposit_timing) != {"beginning", "ending"}:
            errors.append(f"{part.name}: deposit timing")
        for row in frame.itertuples(index=False):
            ledger = root / "daily_ledgers" / f"{row.spec_id}_{row.deposit_timing}.parquet"
            if not ledger.exists():
                errors.append(f"{part.name}: missing ledger")
                continue
            if sha256(ledger) != row.daily_ledger_sha256:
                errors.append(f"{part.name}: ledger hash")
                continue
            daily = pd.read_parquet(ledger)
            if len(daily) != row.daily_ledger_rows:
                errors.append(f"{part.name}: ledger rows")
            if daily.asset_identity_residual.abs().max() > 1e-7:
                errors.append(f"{part.name}: asset identity")
    if errors:
        raise RuntimeError(json.dumps(errors[:20]))
    results = pd.concat(frames, ignore_index=True).sort_values(["spec_id", "deposit_timing"])
    if results.spec_id.nunique() != 108 or len(results) != 216:
        raise RuntimeError("aggregate cardinality mismatch")
    atomic_csv(results, root / "results.csv")
    worst = results.groupby("spec_id").agg(
        W12=("W12", "min"), W24=("W24", "min"), margin_peak=("margin_peak", "max"),
        asset_identity_failures=("asset_identity_failures", "sum"),
    ).reset_index()
    atomic_csv(worst.sort_values(["W24", "W12"], ascending=False), root / "pareto.csv")
    coverage_path = root / "coverage.json"
    coverage = json.loads(coverage_path.read_text()) if coverage_path.exists() else {
        "schema_version": 4,
        "family": family,
        "grid_sha256": "c80e5de617357f86f1915827a84f41b4505afa01b9f8634ac13dec3497e322cc",
        "etf_common_nonoverlap_w24_blocks": 6,
        "strategy_execution_nonoverlap_w24_blocks": 1,
        "sample_gate_pass": False,
        "strict_blockers": ["official_point_in_time_daily_margin", "strategy_level_six_nonoverlap_w24_blocks"],
    }
    best = worst.sort_values(["W24", "W12"], ascending=False).iloc[0]
    coverage.update({
        "completed_specs": 108,
        "deposit_timing_rows": 216,
        "daily_ledgers": 216,
        "results_sha256": sha256(root / "results.csv"),
        "attempt_ledger_sha256": sha256(root / "attempt_ledger.csv"),
        "pareto_sha256": sha256(root / "pareto.csv"),
        "atomic_audit_pass": True,
        "atomic_audit_issue_count": 0,
        "economic_dual_target_specs": int(worst.eval("W12 >= 500000 and W24 >= 1200000 and asset_identity_failures == 0").sum()),
        "asset_identity_failure_specs": int((worst.asset_identity_failures > 0).sum()),
        "best_spec": str(worst.sort_values(["W24", "W12"], ascending=False).iloc[0].spec_id),
        "best_worst_timing_W12": float(best.W12),
        "best_worst_timing_W24": float(best.W24),
        "strict_candidates": 0,
    })
    coverage_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(coverage, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

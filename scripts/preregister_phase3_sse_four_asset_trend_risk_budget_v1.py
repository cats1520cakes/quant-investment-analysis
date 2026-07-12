from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import pandas as pd


OUT = Path("artifacts/derived/phase3_sse_four_asset_trend_risk_budget_v1")
PANEL = Path("artifacts/runtime_data/processed/phase3_etf/multi_asset_sse_canonical.parquet")
SOURCE_OPERATION = Path("artifacts/derived/phase3_multi_asset_trend_risk_budget_v2/operation_manifest.json")
CODES = ["510300", "510880", "518880", "511010"]


def main() -> None:
    source = json.loads(SOURCE_OPERATION.read_text())
    axes = {
        "product": ["IH", "IF", "IC", "IM"],
        "trend_window": [20, 60, 120],
        "nav_margin_multiple": [1.25, 1.5, 2.0],
        "roll_lead_days": [1, 3, 5],
        "margin_buffer": [0.20, 0.30],
        "risk_reduction": [0.0, 0.5],
    }
    rows = []
    for i, values in enumerate(itertools.product(*axes.values())):
        params = dict(zip(axes, values))
        rows.append({
            "spec_id": f"S4ARB-{i:04d}",
            "family": f"sse_four_asset_trend_risk_budget_v1__{params['product']}",
            "parameters": json.dumps(params, separators=(",", ":")),
            "strict_eligible": False,
        })
    OUT.mkdir(parents=True, exist_ok=True)
    grid = pd.DataFrame(rows)
    grid_path = OUT / "grid.csv"
    grid.to_csv(grid_path, index=False)

    panel = pd.read_parquet(PANEL)
    panel["code"] = panel["code"].astype(str)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"].astype(str))
    common = sorted(set.intersection(*(set(panel.loc[panel.code.eq(c), "trade_date"]) for c in CODES)))
    if len(common) < 120:
        raise RuntimeError("four-asset common history does not satisfy signal warmup")
    warmup_end = common[119]
    last = common[-1]
    complete_months = (last.year - warmup_end.year) * 12 + last.month - warmup_end.month
    blocks = complete_months // 24
    if blocks < 6:
        raise RuntimeError(f"six non-overlapping W24 blocks unavailable: {blocks}")

    inherited = {k: source[k] for k in ["signal", "weights", "execution", "targets", "deposit_timings", "monthly_deposit"]}
    manifest = {
        "schema_version": 1,
        "family": "sse_four_asset_trend_risk_budget_v1",
        "status": "preregistered_before_results",
        "results_seen_before_freeze": False,
        "derivation": "mechanical projection of frozen multi_asset_trend_risk_budget_v2 caused only by official canonical data availability",
        "does_not_replace": "multi_asset_trend_risk_budget_v2",
        "source_operation_sha256": hashlib.sha256(SOURCE_OPERATION.read_bytes()).hexdigest(),
        "asset_roles": {
            "broad_equity": "510300",
            "dividend_equity": "510880",
            "gold": "518880",
            "government_bond": "511010",
        },
        **inherited,
        "common_history": {
            "first_common_date": common[0].strftime("%Y-%m-%d"),
            "warmup_observations": 120,
            "first_evaluable_date": warmup_end.strftime("%Y-%m-%d"),
            "last_date": last.strftime("%Y-%m-%d"),
            "complete_months_after_warmup": complete_months,
            "nonoverlap_W24_blocks": blocks,
        },
        "minimum_nonoverlap_W24_blocks": 6,
        "panel_sha256": hashlib.sha256(PANEL.read_bytes()).hexdigest(),
        "grid_sha256": hashlib.sha256(grid_path.read_bytes()).hexdigest(),
        "specifications": len(grid),
        "family_counts": grid.groupby("family").size().to_dict(),
        "strategy_run_permitted": True,
        "evidence_tier": "official_exchange_etf_canonical_plus_free_real_approx_conservative_futures_margin",
        "strict_blockers": ["official_point_in_time_daily_margin"],
        "strict_candidates": 0,
    }
    (OUT / "operation_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"grid_sha256": manifest["grid_sha256"], "specifications": len(grid), "blocks": blocks}, indent=2))


if __name__ == "__main__":
    main()

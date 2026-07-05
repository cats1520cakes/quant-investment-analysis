from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_sources.baostock_adapter import load_config
from quant_proof.free_sources.validators import strategy_allowed_in_tier
from quant_proof.real_strategies import build_real_stock_strategy_specs, compute_real_stock_scores


def summarize_scores(scores: pd.DataFrame, strategy: str, family: str, holding_k: int) -> dict:
    ranked = scores.loc[scores["rank_score"].notna()].copy()
    if ranked.empty:
        return {
            "strategy": strategy,
            "family": family,
            "data_tier": "free_real",
            "leaderboard_tier": "free_real",
            "allowed": True,
            "n_signal_days": 0,
            "eligible_rows": int(scores.get("eligible", pd.Series(dtype=bool)).sum()) if "eligible" in scores.columns else 0,
            "avg_top_score": float("nan"),
            "median_top_score": float("nan"),
            "uses_derived_limits": True,
            "uses_suspension_proxy": True,
            "market_cap_source": "derived_from_amount_turnover",
        }
    top = ranked.sort_values(["trade_date", "rank_score"], ascending=[True, False]).groupby("trade_date").head(holding_k)
    return {
        "strategy": strategy,
        "family": family,
        "data_tier": "free_real",
        "leaderboard_tier": "free_real",
        "allowed": True,
        "n_signal_days": int(top["trade_date"].nunique()),
        "eligible_rows": int(scores.get("eligible", pd.Series(dtype=bool)).sum()) if "eligible" in scores.columns else 0,
        "avg_top_score": float(top["rank_score"].mean()),
        "median_top_score": float(top["rank_score"].median()),
        "uses_derived_limits": True,
        "uses_suspension_proxy": True,
        "market_cap_source": "derived_from_amount_turnover",
    }


def panel_snapshot(panel: pd.DataFrame) -> dict[str, object]:
    frame = panel.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str)
    tiers = sorted(frame["data_tier"].astype(str).dropna().unique().tolist()) if "data_tier" in frame else []
    return {
        "rows": int(len(frame)),
        "symbols": int(frame["ts_code"].nunique()),
        "date_min": str(frame["trade_date"].min()),
        "date_max": str(frame["trade_date"].max()),
        "data_tiers": ", ".join(tiers) if tiers else "unknown",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 2 free-real S2/S3/S4 signal leaderboard.")
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    parser.add_argument("--max-strategies", type=int, default=0)
    args = parser.parse_args()

    config = load_config(args.config)
    panel_path = config.data_root / "processed/phase2_free/stock_panel.parquet"
    if not panel_path.exists():
        print(f"missing free-real stock panel: {panel_path}; run scripts/build_phase2_free_stock_panel.py first", file=sys.stderr)
        raise SystemExit(2)
    panel = pd.read_parquet(panel_path)
    snapshot = panel_snapshot(panel)
    specs = build_real_stock_strategy_specs(config.raw)
    if args.max_strategies:
        specs = specs[: args.max_strategies]

    rows = []
    for spec in specs:
        admission = strategy_allowed_in_tier(spec.family, "free_real")
        if not admission.allowed:
            rows.append(
                {
                    "strategy": spec.name,
                    "family": spec.family,
                    "data_tier": "free_real",
                    "leaderboard_tier": "free_real",
                    "allowed": False,
                    "blocked_reason": admission.reason,
                }
            )
            continue
        scores = compute_real_stock_scores(panel, spec)
        row = summarize_scores(scores, spec.name, spec.family, int(spec.params.get("holding_k", 10)))
        row["blocked_reason"] = ""
        rows.append(row)

    leaderboard = pd.DataFrame(rows).sort_values(["allowed", "avg_top_score", "n_signal_days"], ascending=[False, False, False])
    leaderboard_path = Path(config.raw["paths"]["leaderboard"])
    leaderboard_path.parent.mkdir(parents=True, exist_ok=True)
    leaderboard.to_csv(leaderboard_path, index=False, encoding="utf-8")

    family_rows = []
    allowed = leaderboard.loc[leaderboard["allowed"].fillna(False)].copy()
    for family, group in leaderboard.groupby("family", sort=True):
        allowed_group = group.loc[group["allowed"].fillna(False)].copy()
        best = allowed_group.sort_values(["avg_top_score", "n_signal_days"], ascending=[False, False]).head(1)
        family_rows.append(
            {
                "family": family,
                "specs": int(len(group)),
                "allowed_specs": int(allowed_group.shape[0]),
                "best_strategy": "" if best.empty else str(best.iloc[0]["strategy"]),
                "best_avg_top_score": float("nan") if best.empty else float(best.iloc[0]["avg_top_score"]),
                "max_signal_days": 0 if allowed_group.empty else int(allowed_group["n_signal_days"].max()),
            }
        )
    family_summary = pd.DataFrame(family_rows)
    best_by_family = (
        allowed.sort_values(["family", "avg_top_score", "n_signal_days"], ascending=[True, False, False])
        .groupby("family", sort=True)
        .head(3)
    )

    top_path = Path(config.raw["paths"]["top_strategies"])
    lines = [
        "# Phase 2 Free Real Top Strategies",
        "",
        "- Data tier: `free_real`.",
        f"- Panel snapshot: rows=`{snapshot['rows']}`, symbols=`{snapshot['symbols']}`, date_range=`{snapshot['date_min']}..{snapshot['date_max']}`, data_tier=`{snapshot['data_tiers']}`.",
        "- Strict real leaderboard remains separate and blocked until official/paid-grade fields exist.",
        "- `up_limit/down_limit` are derived; `is_suspended` is BaoStock `tradestatus` proxy.",
        f"- Strategy specs evaluated: `{len(leaderboard)}`.",
        "",
        "## Family Coverage",
        "",
        family_summary.to_markdown(index=False) if not family_summary.empty else "No family rows.",
        "",
        "## Global Top 20",
        "",
        leaderboard.head(20).to_markdown(index=False) if not leaderboard.empty else "No strategy rows.",
        "",
        "## Best By Family",
        "",
        best_by_family.to_markdown(index=False) if not best_by_family.empty else "No allowed strategy rows.",
    ]
    top_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"leaderboard={leaderboard_path}")
    print(f"top_strategies={top_path}")


if __name__ == "__main__":
    main()

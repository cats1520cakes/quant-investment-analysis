from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser(description="Whole-contract feasibility under monthly 30k contributions")
    parser.add_argument("--panel", required=True)
    parser.add_argument("--output", default="artifacts/derived/phase3_small_capital_feasibility")
    parser.add_argument("--monthly-deposit", type=float, default=30000.0)
    parser.add_argument("--futures-margin-rate", type=float, default=0.12)
    parser.add_argument("--cash-buffer-pct", type=float, default=0.15)
    parser.add_argument("--margin-snapshot", default="20260623")
    args = parser.parse_args()
    panel_path, output = Path(args.panel), Path(args.output)
    panel = pd.read_parquet(panel_path)
    dates = pd.to_datetime(sorted(panel.trade_date.astype(str).unique()))
    month_number = pd.Series((dates.year - dates.year.min()) * 12 + dates.month - dates.month.min() + 1, index=dates)
    rows = []
    futures = panel.loc[panel.instrument_type.eq("future") & panel.open_executable].copy()
    futures["one_contract_cash"] = futures.open * futures.multiplier * args.futures_margin_rate
    future_daily = futures.groupby(["trade_date", "product"]).one_contract_cash.min().unstack()
    future_daily.index = pd.to_datetime(future_daily.index)
    for timing in ("beginning", "ending"):
        cash = args.monthly_deposit * (month_number if timing == "beginning" else month_number - 1)
        for product in ("IF", "IH", "IC", "IM"):
            required = future_daily[product].reindex(dates)
            valid = required.notna()
            feasible = cash * (1 - args.cash_buffer_pct) >= required
            rows.append({"instrument": product, "instrument_type": "future", "deposit_timing": timing, "budget_rule": f"official_margin_{args.futures_margin_rate:.4f}_buffer_{args.cash_buffer_pct:.4f}", "observations": int(valid.sum()), "infeasible_share": float((~feasible[valid]).mean()), "first_feasible_date": str(feasible[valid & feasible].index.min().date()) if (valid & feasible).any() else "", "median_one_contract_cash": float(required[valid].median()), "evidence_tier": f"official_daily_open_plus_{args.margin_snapshot}_margin_snapshot"})
    options = panel.loc[panel.instrument_type.eq("option") & panel.open_executable & panel.delta.abs().between(.2, .5)].copy()
    options["one_contract_cash"] = options.open * options.multiplier
    option_daily = options.groupby(["trade_date", "product"]).one_contract_cash.min().unstack()
    option_daily.index = pd.to_datetime(option_daily.index)
    for budget_pct in (.003, .005, .008):
        for timing in ("beginning", "ending"):
            cash = args.monthly_deposit * (month_number if timing == "beginning" else month_number - 1)
            for product in ("IO", "HO", "MO"):
                required = option_daily[product].reindex(dates)
                valid = required.notna()
                feasible = cash * budget_pct >= required
                rows.append({"instrument": product, "instrument_type": "long_option", "deposit_timing": timing, "budget_rule": f"premium_pct_nav_{budget_pct:.3f}", "observations": int(valid.sum()), "infeasible_share": float((~feasible[valid]).mean()), "first_feasible_date": str(feasible[valid & feasible].index.min().date()) if (valid & feasible).any() else "", "median_one_contract_cash": float(required[valid].median()), "evidence_tier": "official_daily_open_prior_volume_pending_dte_lower_bound"})
    output.mkdir(parents=True, exist_ok=True)
    result = pd.DataFrame(rows)
    result.to_csv(output / "feasibility.csv", index=False)
    manifest = {"schema_version": 1, "panel_sha256": hashlib.sha256(panel_path.read_bytes()).hexdigest(), "rows": len(result), "monthly_deposit": args.monthly_deposit, "margin_snapshot": args.margin_snapshot, "strict_candidates": 0, "limitations": ["margin rate uses one official snapshot rather than the complete historical effective-date schedule", "option calculation is a lower-bound screen before DTE and exact target-delta selection", "six-month segment cannot support W12/W24"]}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()

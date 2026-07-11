from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
import yaml

from quant_proof.simulator import aggregate_windows, evaluate_rolling_strategy
from quant_proof.strategies import build_strategy_specs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="artifacts/runtime_data")
    parser.add_argument("--config", default="config/phase3_etf_screen.yaml")
    parser.add_argument("--output-root", default="artifacts/derived/phase3_etf_screen")
    args = parser.parse_args()
    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    panel_path = Path(args.data_root) / "processed" / "phase3_etf" / "sse_u4_canonical.parquet"
    panel = pd.read_parquet(panel_path)
    tradable = panel.loc[panel["tradable"]].copy()
    close = tradable.pivot(index="trade_date", columns="code", values="close")
    close.index = pd.to_datetime(close.index)
    specs = build_strategy_specs(config)
    windows = pd.concat(
        [evaluate_rolling_strategy(close, spec, config, timing) for spec in specs for timing in ("beginning", "ending")],
        ignore_index=True,
    )
    leaderboard = aggregate_windows(windows, config["target_month_12"], config["target_month_24"])
    tails = windows.groupby(["strategy", "family", "deposit_timing"]).agg(
        worst_w12=("w12", "min"), worst_w24=("w24", "min"), p5_w24=("w24", lambda x: x.quantile(.05)),
        p95_w24=("w24", lambda x: x.quantile(.95)), worst_drawdown=("max_drawdown", "max"),
        worst_recovery_days=("recovery_days", "max"),
    ).reset_index()
    leaderboard = leaderboard.merge(tails, on=["strategy", "family", "deposit_timing"], validate="one_to_one")
    by_strategy = leaderboard.groupby(["strategy", "family"]).agg(
        worst_timing_success=("p_success", "min"), median_w24=("median_w24", "median"),
        worst_w24=("worst_w24", "min"), p5_w24=("p5_w24", "min"), worst_drawdown=("worst_drawdown", "max"),
    ).reset_index().sort_values(["worst_timing_success", "median_w24"], ascending=False)
    by_strategy["passes_numeric_dual_target"] = by_strategy["worst_timing_success"] >= 0.5
    by_strategy["passes_strict_gate"] = False
    by_strategy["blocking_reason"] = "company_action_ledger_and_whole_lot_accounting_incomplete"
    output = Path(args.output_root)
    output.mkdir(parents=True, exist_ok=True)
    windows.to_parquet(output / "windows.parquet", index=False)
    leaderboard.to_csv(output / "leaderboard_by_timing.csv", index=False)
    by_strategy.to_csv(output / "candidate_registry.csv", index=False)
    manifest = {
        "schema_version": 1, "panel_manifest_sha256": hashlib.sha256(panel_path.with_suffix(panel_path.suffix + ".manifest.json").read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(), "strategies": len(specs),
        "windows": len(windows), "strict_candidates": 0, "development_only": True,
        "blocking_gates": ["corporate_action_ledger", "whole_100_share_lots", "raw_unadjusted_signal_discontinuities"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(by_strategy.head(10).to_string(index=False))
    print(f"[etf-screen] strategies={len(specs)} windows={len(windows)} strict_candidates=0")


if __name__ == "__main__":
    main()

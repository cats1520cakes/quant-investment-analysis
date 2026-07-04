from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quant_proof.config import ensure_data_dirs, load_config
from quant_proof.data import load_processed_close
from quant_proof.reporting import write_phase1_report
from quant_proof.simulator import aggregate_windows, bootstrap_strategy_returns, evaluate_rolling_strategy
from quant_proof.strategies import StrategySpec, build_strategy_specs


def _summarize_bootstrap(bootstrap: pd.DataFrame, target_12: float, target_24: float) -> pd.DataFrame:
    if bootstrap.empty:
        return bootstrap
    rows = []
    for key, group in bootstrap.groupby(["strategy", "family", "deposit_timing", "block_size"]):
        success = (group["w12"] >= target_12) & (group["w24"] >= target_24)
        rows.append(
            {
                "strategy": key[0],
                "family": key[1],
                "deposit_timing": key[2],
                "block_size": key[3],
                "paths": len(group),
                "p_success": float(success.mean()),
                "median_w24": float(group["w24"].median()),
                "p95_max_drawdown": float(group["max_drawdown"].quantile(0.95)),
            }
        )
    return pd.DataFrame(rows).sort_values(["p_success", "median_w24"], ascending=False)


def _select_followup_specs(specs: list[StrategySpec], leaderboard: pd.DataFrame, top_n: int) -> list[StrategySpec]:
    if leaderboard.empty or top_n <= 0:
        return []
    by_score = list(leaderboard.head(top_n)["strategy"])
    by_success = list(leaderboard.sort_values(["p_success", "median_w24"], ascending=False).head(top_n)["strategy"])
    selected_names = []
    seen = set()
    for name in by_score + by_success:
        if name not in seen:
            selected_names.append(name)
            seen.add(name)
    lookup = {spec.name: spec for spec in specs}
    return [lookup[name] for name in selected_names if name in lookup]


def _run_stress_cases(
    close: pd.DataFrame,
    selected_specs: list[StrategySpec],
    config: dict,
    target_12: float,
    target_24: float,
) -> pd.DataFrame:
    stress_cfg = config.get("stress", {})
    if not stress_cfg.get("enabled", False) or not selected_specs:
        return pd.DataFrame()

    frames = []
    slippage_base = float(config["execution"]["slippage_bps"])
    for multiplier in stress_cfg.get("slippage_multipliers", [1]):
        stressed_config = deepcopy(config)
        stressed_config["execution"]["slippage_bps"] = slippage_base * float(multiplier)
        for spec in selected_specs:
            for timing in ("beginning", "ending"):
                frame = evaluate_rolling_strategy(close, spec, stressed_config, deposit_timing=timing)
                if not frame.empty:
                    frame["stress_case"] = f"slippage_x{multiplier}"
                    frames.append(frame)

    for financing_rate in stress_cfg.get("financing_rates_annual", []):
        for spec in selected_specs:
            if float(spec.params.get("gross_exposure", 1.0)) <= 1.0:
                continue
            params = dict(spec.params)
            params["financing_rate_annual"] = float(financing_rate)
            stressed_spec = StrategySpec(
                name=f"{spec.name}_stress_fr{financing_rate}",
                family=spec.family,
                params=params,
            )
            for timing in ("beginning", "ending"):
                frame = evaluate_rolling_strategy(close, stressed_spec, config, deposit_timing=timing)
                if not frame.empty:
                    frame["strategy"] = spec.name
                    frame["stress_case"] = f"financing_rate_{financing_rate}"
                    frames.append(frame)

    if not frames:
        return pd.DataFrame()
    stress_windows = pd.concat(frames, ignore_index=True)
    summaries = []
    for stress_case, group in stress_windows.groupby("stress_case"):
        summary = aggregate_windows(group, target_12=target_12, target_24=target_24)
        if not summary.empty:
            summary.insert(0, "stress_case", stress_case)
            summaries.append(summary)
    return pd.concat(summaries, ignore_index=True) if summaries else pd.DataFrame()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/phase1.yaml")
    parser.add_argument("--max-strategies", type=int, default=0, help="Debug limit; 0 means all.")
    parser.add_argument("--bootstrap-paths", type=int, default=0, help="Override bootstrap paths.")
    args = parser.parse_args()

    config_obj = load_config(args.config)
    config = config_obj.raw
    ensure_data_dirs(config_obj.data_root)
    close = load_processed_close(config_obj)
    specs = build_strategy_specs(config)
    if args.max_strategies > 0:
        specs = specs[: args.max_strategies]

    window_frames = []
    for index, spec in enumerate(specs, start=1):
        if index == 1 or index % 50 == 0 or index == len(specs):
            print(f"[rolling] {index}/{len(specs)} {spec.name}", flush=True)
        for timing in ("beginning", "ending"):
            frame = evaluate_rolling_strategy(close, spec, config, deposit_timing=timing)
            if not frame.empty:
                window_frames.append(frame)

    windows = pd.concat(window_frames, ignore_index=True) if window_frames else pd.DataFrame()
    leaderboard = aggregate_windows(
        windows,
        target_12=float(config["target_month_12"]),
        target_24=float(config["target_month_24"]),
    )

    report_prefix = str(config.get("report_prefix", "phase1"))
    report_dir = config_obj.data_root / "reports"
    windows_path = report_dir / f"{report_prefix}_windows.csv"
    leaderboard_path = report_dir / f"{report_prefix}_leaderboard.csv"
    windows.to_csv(windows_path, index=False, encoding="utf-8")
    leaderboard.to_csv(leaderboard_path, index=False, encoding="utf-8")

    bootstrap_summary = pd.DataFrame()
    if config.get("bootstrap", {}).get("enabled", False) and not leaderboard.empty:
        rng = np.random.default_rng(config_obj.seed)
        paths = int(config["bootstrap"]["paths"]) if args.bootstrap_paths < 0 else args.bootstrap_paths
        top_n = int(config.get("bootstrap", {}).get("top_n", 10))
        selected_specs = _select_followup_specs(specs, leaderboard, top_n=top_n)
        boot_frames = []
        if paths > 0:
            for spec in selected_specs:
                print(f"[bootstrap] {spec.name} paths={paths}", flush=True)
                for timing in ("beginning", "ending"):
                    for block_size in config["bootstrap"]["block_sizes"]:
                        frame = bootstrap_strategy_returns(
                            close=close,
                            spec=spec,
                            config=config,
                            deposit_timing=timing,
                            rng=rng,
                            block_size=int(block_size),
                            paths=paths,
                        )
                        if not frame.empty:
                            boot_frames.append(frame)
        bootstrap = pd.concat(boot_frames, ignore_index=True) if boot_frames else pd.DataFrame()
        bootstrap_path = report_dir / f"{report_prefix}_bootstrap_paths.csv"
        bootstrap_summary_path = report_dir / f"{report_prefix}_bootstrap_summary.csv"
        bootstrap.to_csv(bootstrap_path, index=False, encoding="utf-8")
        bootstrap_summary = _summarize_bootstrap(
            bootstrap,
            target_12=float(config["target_month_12"]),
            target_24=float(config["target_month_24"]),
        )
        bootstrap_summary.to_csv(bootstrap_summary_path, index=False, encoding="utf-8")

    stress_summary = pd.DataFrame()
    if config.get("stress", {}).get("enabled", False) and not leaderboard.empty:
        stress_top_n = int(config.get("stress", {}).get("top_n", 20))
        selected_specs = _select_followup_specs(specs, leaderboard, top_n=stress_top_n)
        print(f"[stress] selected_specs={len(selected_specs)}", flush=True)
        stress_summary = _run_stress_cases(
            close=close,
            selected_specs=selected_specs,
            config=config,
            target_12=float(config["target_month_12"]),
            target_24=float(config["target_month_24"]),
        )
        stress_summary_path = report_dir / f"{report_prefix}_stress_summary.csv"
        stress_summary.to_csv(stress_summary_path, index=False, encoding="utf-8")

    workspace_report = Path("reports") / f"{report_prefix}_experiment_report.md"
    data_paths = {
        "raw": str(
            config_obj.data_root
            / "raw"
            / ("baostock/index_daily" if config.get("data_source") == "baostock_index" else "akshare/etf_daily")
        ),
        "processed_close": str(config_obj.data_root / "processed" / "phase1_daily_close.csv"),
        "manifest": str(config_obj.data_root / "00_meta" / "manifests" / "phase1_daily_manifest.csv"),
        "external_reports": str(report_dir),
        "scope_note": "BaoStock 指数日频代理，覆盖沪深300、中证500、中证1000、创业板、创业板50、上证50、上证、深证、中小100；用于第一阶段市场状态和操作族筛查，不等价于真实 ETF / 个股撮合。",
    }
    write_phase1_report(
        workspace_report,
        leaderboard=leaderboard,
        bootstrap_summary=bootstrap_summary,
        data_paths=data_paths,
        generated_at=datetime.now(),
    )
    if not stress_summary.empty:
        top_stress = stress_summary.head(20).copy()
        top_stress["p_success"] = top_stress["p_success"].map(lambda x: f"{x * 100:.2f}%")
        top_stress["median_w24"] = top_stress["median_w24"].map(lambda x: f"{x:,.0f}")
        top_stress["p95_max_drawdown"] = top_stress["p95_max_drawdown"].map(lambda x: f"{x * 100:.2f}%")
        with workspace_report.open("a", encoding="utf-8") as handle:
            handle.write("\n## 压力测试摘要\n\n")
            handle.write(top_stress[["stress_case", "strategy", "deposit_timing", "p_success", "median_w24", "p95_max_drawdown", "score"]].to_markdown(index=False))
            handle.write("\n")

    print(f"strategies={len(specs)}")
    print(f"windows={len(windows)}")
    print(f"leaderboard={leaderboard_path}")
    print(f"report={workspace_report}")
    if not leaderboard.empty:
        print(leaderboard.head(8).to_string(index=False))


if __name__ == "__main__":
    main()

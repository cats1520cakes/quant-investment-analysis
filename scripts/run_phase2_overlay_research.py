from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_real_backtest import _prepare_daily_panel, load_backtest_config
from quant_proof.free_sources.baostock_adapter import load_config
from quant_proof.overlay_research import (
    FUTURES_CONTRACTS,
    OPTION_UNDERLYINGS,
    aggregate_overlay_windows,
    apply_futures_overlay,
    apply_option_overlay,
    build_futures_specs,
    build_option_specs,
    equity_windows_for_strategy,
    load_index_close,
    select_base_rows,
)
from quant_proof.real_strategies import build_real_stock_strategy_specs


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _fmt_money(value: float) -> str:
    return f"{value:,.0f}"


def _overlay_config(raw: dict) -> dict:
    cfg = raw.get("overlay_research", {})
    return cfg if isinstance(cfg, dict) else {}


def _pick(items: dict, key: str, default: list) -> list:
    value = items.get(key, default)
    return value if isinstance(value, list) else default


def _format_participation_label(value: float) -> str:
    pct = value * 100.0
    if abs(pct - round(pct)) < 1e-9:
        return f"participation_{int(round(pct))}pct"
    return f"participation_{pct:.2f}pct".replace(".", "p")


def _output_path(path: Path, label: str) -> Path:
    if not label:
        return path
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in label)
    return path.with_name(f"{path.stem}_{safe}{path.suffix}")


def write_report(
    path: Path,
    leaderboard: pd.DataFrame,
    windows: pd.DataFrame,
    selected_base: pd.DataFrame,
    generated_at: datetime,
    index_close_path: Path,
    target_leaderboard_path: Path,
    base_run_label: str,
    max_daily_amount_participation: float | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    base_scope = (
        "- Base stock paths come from the 505-stock `free_real` target backtest layer."
        if max_daily_amount_participation is None
        else (
            "- Base stock paths come from the 505-stock `free_real` target backtest layer with "
            f"a `{max_daily_amount_participation:.4f}` BaoStock daily-amount participation cap."
        )
    )
    lines = [
        "# Phase 2 Overlay Research",
        "",
        f"Generated at: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Scope",
        "",
        "- Data tier: `proxy_overlay_research`.",
        "- This report is not a strict-real futures/options leaderboard.",
        base_scope,
        "- Futures overlays use index proxies, integer lots, daily mark-to-market, effective margin rates, and cash-buffer gates.",
        "- Option overlays are parametric call-budget tests: premiums are Black-Scholes approximations from realized-vol multiples, contracts are integer, and payoff is held to tenor expiry.",
        "- Real futures/options chains remain blocked until contract-level daily data, bid/ask, volume/open-interest, margin and adjustment fields exist.",
        "",
        "## Inputs",
        "",
        f"- Base target leaderboard: `{target_leaderboard_path}`",
        f"- Base run label: `{base_run_label or 'baseline'}`",
        f"- Index close proxy: `{index_close_path}`",
        f"- Selected base rows: `{len(selected_base)}`",
        f"- Window rows: `{len(windows)}`",
        "",
        "## Selected Base Strategies",
        "",
    ]
    if selected_base.empty:
        lines.append("No base strategies selected.")
    else:
        base_cols = ["strategy", "family", "deposit_timing", "p_success", "median_w24", "p95_max_drawdown", "score"]
        base = selected_base[[col for col in base_cols if col in selected_base.columns]].copy()
        if "p_success" in base:
            base["p_success"] = base["p_success"].map(_fmt_pct)
        if "p95_max_drawdown" in base:
            base["p95_max_drawdown"] = base["p95_max_drawdown"].map(_fmt_pct)
        if "median_w24" in base:
            base["median_w24"] = base["median_w24"].map(_fmt_money)
        lines.append(base.to_markdown(index=False))

    lines.extend(["", "## Overlay Leaderboard", ""])
    if leaderboard.empty:
        lines.append("No overlay rows were generated.")
    else:
        table = leaderboard.head(24).copy()
        for column in ["p_success", "p_w12", "p_w24", "p95_max_drawdown", "p_w24_below_deposit", "p_drawdown_gt_50"]:
            if column in table:
                table[column] = table[column].map(_fmt_pct)
        for column in ["median_w24", "p10_w24", "p90_w24", "avg_futures_pnl", "avg_option_premium_spent", "avg_option_payoff"]:
            if column in table:
                table[column] = table[column].map(_fmt_money)
        keep = [
            "base_strategy",
            "overlay_type",
            "overlay_name",
            "n_windows",
            "p_success",
            "median_w24",
            "p95_max_drawdown",
            "p_w24_below_deposit",
            "avg_futures_cannot_afford",
            "avg_futures_forced_liquidations",
            "avg_option_premium_spent",
            "avg_option_payoff",
            "score",
        ]
        lines.append(table[[col for col in keep if col in table.columns]].to_markdown(index=False))
        best = leaderboard.iloc[0]
        highest_success = leaderboard.sort_values(["p_success", "median_w24"], ascending=False).iloc[0]
        best_futures = leaderboard.loc[leaderboard["overlay_type"] == "futures_integer_lot_proxy"].head(1)
        lines.extend(
            [
                "",
                "## Readout",
                "",
                f"- Best overlay score: `{best['overlay_name']}` on `{best['base_strategy']}`, success={_fmt_pct(best['p_success'])}, median_w24={_fmt_money(best['median_w24'])}, p95 drawdown={_fmt_pct(best['p95_max_drawdown'])}.",
                f"- Highest success: `{highest_success['overlay_name']}` on `{highest_success['base_strategy']}`, success={_fmt_pct(highest_success['p_success'])}, median_w24={_fmt_money(highest_success['median_w24'])}.",
                "- If this table does not materially beat the selected base free-real rows under the same stock execution assumptions, derivatives should not be treated as a shortcut; the next honest step is either real contract data or a different base strategy family.",
            ]
        )
        if not best_futures.empty:
            futures = best_futures.iloc[0]
            lines.append(
                f"- Best futures proxy keeps success at {_fmt_pct(futures['p_success'])}; average cannot-afford events per window are {float(futures['avg_futures_cannot_afford']):.2f}, showing integer-lot and cash-buffer constraints dominate."
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 2 proxy overlay research for futures and option convexity budgets.")
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    parser.add_argument("--top-base", type=int, default=0, help="Override overlay_research.top_base_rows.")
    parser.add_argument("--max-windows", type=int, default=0, help="Debug limit per selected base strategy; 0 means all windows.")
    parser.add_argument("--base-output-label", default="", help="Suffix used by the base target leaderboard, e.g. participation_5pct.")
    parser.add_argument("--target-leaderboard", default="", help="Explicit base target leaderboard path.")
    parser.add_argument(
        "--max-daily-amount-participation",
        type=float,
        default=None,
        help="Use the same per-stock BaoStock amount participation cap when rebuilding base equity paths.",
    )
    parser.add_argument("--output-label", default="", help="Suffix for overlay output files.")
    parser.add_argument("--skip-futures", action="store_true")
    parser.add_argument("--skip-options", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    raw_cfg = _overlay_config(config.raw)
    base_output_label = args.base_output_label.strip()
    if not base_output_label and args.max_daily_amount_participation is not None:
        base_output_label = _format_participation_label(args.max_daily_amount_participation)
    target_leaderboard_path = (
        Path(args.target_leaderboard)
        if args.target_leaderboard
        else _output_path(Path(config.raw["paths"]["target_leaderboard"]), base_output_label)
    )
    if not target_leaderboard_path.exists():
        print(f"missing target leaderboard: {target_leaderboard_path}; run scripts/run_phase2_free_real_target_backtest.py first", file=sys.stderr)
        raise SystemExit(2)
    base_leaderboard = pd.read_csv(target_leaderboard_path)
    top_base = args.top_base or int(raw_cfg.get("top_base_rows", 4))
    selected_base = select_base_rows(base_leaderboard, top_n=top_base)

    panel_path = config.data_root / "processed/phase2_free/stock_panel.parquet"
    if not panel_path.exists():
        print(f"missing free-real stock panel: {panel_path}; run scripts/build_phase2_free_stock_panel.py first", file=sys.stderr)
        raise SystemExit(2)
    panel = pd.read_parquet(panel_path)
    panel_by_date = _prepare_daily_panel(panel)
    cfg = load_backtest_config(config.raw)
    if args.max_daily_amount_participation is not None:
        cfg = replace(cfg, max_daily_amount_participation=args.max_daily_amount_participation)
    specs_by_name = {spec.name: spec for spec in build_real_stock_strategy_specs(config.raw)}

    index_close_path = Path(raw_cfg.get("index_close_path", config.data_root / "processed/phase1_daily_close.csv"))
    index_close = load_index_close(index_close_path)

    futures_cfg = raw_cfg.get("futures", {}) if isinstance(raw_cfg.get("futures", {}), dict) else {}
    option_cfg = raw_cfg.get("options", {}) if isinstance(raw_cfg.get("options", {}), dict) else {}
    futures_specs = [] if args.skip_futures else build_futures_specs(
        contracts=_pick(futures_cfg, "contracts", list(FUTURES_CONTRACTS)),
        target_betas=[float(x) for x in _pick(futures_cfg, "target_beta", [0.3, 0.5])],
        margin_rates=[float(x) for x in _pick(futures_cfg, "effective_margin_rates", [0.15, 0.20])],
        cash_buffer_pcts=[float(x) for x in _pick(futures_cfg, "cash_buffer_pct", [0.33, 0.50])],
    )
    option_specs = [] if args.skip_options else build_option_specs(
        contracts=_pick(option_cfg, "contracts", list(OPTION_UNDERLYINGS)),
        budgets=[float(x) for x in _pick(option_cfg, "monthly_budget_pct_nav", [0.01, 0.02, 0.05])],
        tenors=[int(x) for x in _pick(option_cfg, "tenors_days", [30, 60])],
        deltas=[float(x) for x in _pick(option_cfg, "deltas", [0.35, 0.50])],
        iv_multipliers=[float(x) for x in _pick(option_cfg, "iv_multipliers_to_realized_vol", [1.3, 1.6])],
    )

    rows = []
    for base_index, base in selected_base.iterrows():
        strategy = str(base["strategy"])
        deposit_timing = str(base["deposit_timing"])
        spec = specs_by_name.get(strategy)
        if spec is None:
            print(f"[overlay] skip unknown base strategy {strategy}", file=sys.stderr)
            continue
        print(f"[overlay] base {base_index + 1}/{len(selected_base)} {strategy} / {deposit_timing}", flush=True)
        equity_windows = equity_windows_for_strategy(
            panel=panel,
            spec=spec,
            cfg=cfg,
            deposit_timing=deposit_timing,
            panel_by_date=panel_by_date,
            max_windows=args.max_windows,
        )
        for window_index, (start, end, equity) in enumerate(equity_windows, start=1):
            if window_index % 50 == 0:
                print(f"[overlay]   window {window_index}/{len(equity_windows)}", flush=True)
            for overlay in futures_specs:
                symbol = FUTURES_CONTRACTS[overlay.contract]["symbol"]
                overlaid, metrics = apply_futures_overlay(equity, index_close[symbol], overlay, cfg)
                rows.append(
                    {
                        "base_strategy": strategy,
                        "base_family": str(base["family"]),
                        "deposit_timing": deposit_timing,
                        "overlay_type": "futures_integer_lot_proxy",
                        "overlay_name": overlay.name,
                        "data_tier": "proxy_overlay_research",
                        "start": start.date().isoformat(),
                        "end": end.date().isoformat(),
                        **metrics,
                    }
                )
            for overlay in option_specs:
                symbol = OPTION_UNDERLYINGS[overlay.contract]["symbol"]
                overlaid, metrics = apply_option_overlay(equity, index_close[symbol], overlay, cfg)
                rows.append(
                    {
                        "base_strategy": strategy,
                        "base_family": str(base["family"]),
                        "deposit_timing": deposit_timing,
                        "overlay_type": "parametric_option_call_budget",
                        "overlay_name": overlay.name,
                        "data_tier": "proxy_overlay_research",
                        "start": start.date().isoformat(),
                        "end": end.date().isoformat(),
                        **metrics,
                    }
                )

    windows = pd.DataFrame(rows)
    leaderboard = aggregate_overlay_windows(windows, cfg)
    output_label = args.output_label.strip()
    if not output_label and cfg.max_daily_amount_participation is not None:
        output_label = _format_participation_label(cfg.max_daily_amount_participation)
    windows_path = _output_path(Path(config.raw["paths"]["overlay_windows"]), output_label)
    leaderboard_path = _output_path(Path(config.raw["paths"]["overlay_leaderboard"]), output_label)
    report_path = _output_path(Path(config.raw["paths"]["overlay_report"]), output_label)
    windows_path.parent.mkdir(parents=True, exist_ok=True)
    windows.to_csv(windows_path, index=False, encoding="utf-8")
    leaderboard.to_csv(leaderboard_path, index=False, encoding="utf-8")
    write_report(
        path=report_path,
        leaderboard=leaderboard,
        windows=windows,
        selected_base=selected_base,
        generated_at=datetime.now(),
        index_close_path=index_close_path,
        target_leaderboard_path=target_leaderboard_path,
        base_run_label=base_output_label,
        max_daily_amount_participation=cfg.max_daily_amount_participation,
    )
    print(f"selected_base={len(selected_base)}")
    print(f"futures_specs={len(futures_specs)}")
    print(f"option_specs={len(option_specs)}")
    print(f"windows={len(windows)}")
    print(f"leaderboard={leaderboard_path}")
    print(f"report={report_path}")
    if not leaderboard.empty:
        print(leaderboard.head(10).to_string(index=False))


if __name__ == "__main__":
    main()

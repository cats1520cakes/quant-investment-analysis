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

from quant_proof.free_real_backtest import (
    _prepare_daily_panel,
    aggregate_free_real_windows,
    evaluate_free_real_strategy,
    load_backtest_config,
)
from quant_proof.free_sources.baostock_adapter import load_config
from quant_proof.free_sources.validators import strategy_allowed_in_tier
from quant_proof.real_strategies import build_real_stock_strategy_specs


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _fmt_money(value: float) -> str:
    return f"{value:,.0f}"


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


def inspect_panel(panel: pd.DataFrame, min_symbols: int, require_amount: bool = False) -> dict[str, object]:
    required = {
        "trade_date",
        "ts_code",
        "source_code",
        "data_tier",
        "open",
        "close",
        "is_suspended",
        "up_limit",
        "down_limit",
    }
    if require_amount:
        required.add("amount")
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(f"free-real target backtest panel missing required columns: {missing}")

    frame = panel.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame["source_code"] = frame["source_code"].astype(str).str.lower()
    frame["data_tier"] = frame["data_tier"].astype(str)

    tiers = sorted(frame["data_tier"].dropna().unique().tolist())
    if tiers != ["free_real"]:
        raise ValueError(f"target backtest requires data_tier=free_real only; observed={tiers}")

    index_like = sorted(
        source
        for source in frame["source_code"].dropna().unique().tolist()
        if source.startswith("sh.000") or source.startswith("sz.399")
    )
    if index_like:
        raise ValueError(f"target backtest panel appears to contain index-like source_code values: {index_like[:10]}")

    n_symbols = int(frame["ts_code"].nunique())
    if min_symbols > 0 and n_symbols < min_symbols:
        raise ValueError(f"target backtest requires at least {min_symbols} free-real symbols; observed={n_symbols}")

    amount_positive_ratio = None
    if "amount" in frame.columns:
        amount = pd.to_numeric(frame["amount"], errors="coerce")
        amount_positive_ratio = float((amount > 0).mean())

    return {
        "rows": int(len(frame)),
        "symbols": n_symbols,
        "date_min": str(frame["trade_date"].min()),
        "date_max": str(frame["trade_date"].max()),
        "data_tiers": ", ".join(tiers),
        "min_symbols": int(min_symbols),
        "amount_positive_ratio": amount_positive_ratio,
    }


def write_report(
    path: Path,
    leaderboard: pd.DataFrame,
    windows: pd.DataFrame,
    config_path: str,
    panel_path: Path,
    panel_snapshot: dict[str, object],
    run_scope: str,
    max_daily_amount_participation: float | None,
    generated_at: datetime,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    participation_line = (
        "- Daily amount participation cap: disabled."
        if max_daily_amount_participation is None
        else (
            "- Daily amount participation cap: "
            f"`{max_daily_amount_participation:.4f}` of BaoStock full-day `amount` per stock per rebalance order."
        )
    )
    gap_line = (
        "- Known gaps: no participation-rate cap from daily amount in this pre-cap baseline, no dividend/corporate-action cash adjustment, and free-real suspension/limit evidence is proxy/derived rather than official."
        if max_daily_amount_participation is None
        else "- Known gaps: the participation cap uses BaoStock full-day `amount` as an approximate liquidity stress, not official intraday order-book depth or guaranteed fill capacity; dividend/corporate-action cash adjustment and official suspension/limit evidence are still absent."
    )
    lines = [
        "# Phase 2 Free Real Target Backtest",
        "",
        f"Generated at: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Scope",
        "",
        "- Data tier: `free_real`.",
        "- Strict real leaderboard remains separate and blocked until official/paid-grade fields exist.",
        "- This target backtest uses BaoStock raw OHLC for execution, qfq prices for signals, derived limit prices, and BaoStock `tradestatus` as suspension proxy.",
        "- It models monthly deposits, 12/24-month hard targets, T+1, suspended/no-trade checks, limit-up buy rejection, limit-down sell rejection, A-share board-lot buying, fixed order-notional caps, commission, transfer fee, stamp tax, and slippage.",
        participation_line,
        gap_line,
        "",
        "## Inputs",
        "",
        f"- Config: `{config_path}`",
        f"- Panel: `{panel_path}`",
        f"- Panel snapshot: rows=`{panel_snapshot['rows']}`, symbols=`{panel_snapshot['symbols']}`, date_range=`{panel_snapshot['date_min']}..{panel_snapshot['date_max']}`, data_tier=`{panel_snapshot['data_tiers']}`.",
        f"- Minimum symbol gate: `{panel_snapshot['min_symbols']}`.",
        f"- Positive `amount` ratio: `{panel_snapshot['amount_positive_ratio']}`.",
        f"- Run scope: `{run_scope}`.",
        f"- Window rows: `{len(windows)}`",
        "",
        "## Execution Semantics",
        "",
        "- Strategy rankings and filters come from S2/S3/S4 specs, then the target layer converts selected names into equal-weight rebalances under execution constraints.",
        "- S2/S3 stop-loss, trailing-stop, ATR stop, `risk_per_trade`, and `max_holding_days` parameters are not implemented as separate intra-window exits in this free-real target layer; they remain candidate-spec metadata until a stricter order-policy layer is added.",
        "- Participation-cap clipping is applied before submitting orders to the execution engine, so these rows are a more conservative free-real daily-amount stress rather than strict-real fill proof.",
        "- `avg_turnover` is reported as traded notional divided by summed daily wealth across the window, not annualized portfolio turnover.",
        "",
        "## Leaderboard",
        "",
    ]
    if leaderboard.empty:
        lines.append("No target-backtest rows were generated.")
    else:
        table = leaderboard.head(20).copy()
        for column in ["p_success", "p_w12", "p_w24", "p95_max_drawdown", "p_w24_below_deposit", "p_drawdown_gt_35"]:
            if column in table.columns:
                table[column] = table[column].map(_fmt_pct)
        for column in [
            "median_w24",
            "p10_w24",
            "p90_w24",
            "avg_fees",
            "avg_participation_clipped_notional",
            "avg_participation_blocked_notional",
        ]:
            if column in table.columns:
                table[column] = table[column].map(_fmt_money)
        keep = [
            "strategy",
            "family",
            "deposit_timing",
            "n_windows",
            "p_success",
            "p_w12",
            "p_w24",
            "median_w24",
            "p95_max_drawdown",
            "p_w24_below_deposit",
            "avg_rejected_orders",
            "avg_clipped_orders",
            "avg_participation_clipped_orders",
            "avg_participation_blocked_orders",
            "score",
        ]
        lines.append(table[[col for col in keep if col in table.columns]].to_markdown(index=False))

        best = leaderboard.iloc[0]
        highest_success = leaderboard.sort_values(["p_success", "median_w24"], ascending=False).iloc[0]
        lines.extend(
            [
                "",
                "## Readout",
                "",
                f"- Best score: `{best['strategy']}` / `{best['deposit_timing']}`, success={_fmt_pct(best['p_success'])}, median_w24={_fmt_money(best['median_w24'])}.",
                f"- Highest success: `{highest_success['strategy']}` / `{highest_success['deposit_timing']}`, success={_fmt_pct(highest_success['p_success'])}, median_w24={_fmt_money(highest_success['median_w24'])}.",
                "- A positive signal leaderboard is not enough; target proof requires these rolling-window success and drawdown fields.",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 2 free-real target-constrained backtest.")
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    parser.add_argument("--max-strategies", type=int, default=0, help="Debug limit; 0 means all S2/S3/S4 specs.")
    parser.add_argument("--max-windows", type=int, default=0, help="Debug limit per strategy/timing; 0 means all rolling windows.")
    parser.add_argument(
        "--max-daily-amount-participation",
        type=float,
        default=None,
        help="Optional per-stock order cap as a fraction of same-day BaoStock amount, e.g. 0.05.",
    )
    parser.add_argument(
        "--output-label",
        default="",
        help="Optional suffix for windows/leaderboard/report filenames so stress runs do not overwrite the baseline.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    panel_path = config.data_root / "processed/phase2_free/stock_panel.parquet"
    if not panel_path.exists():
        print(f"missing free-real stock panel: {panel_path}; run scripts/build_phase2_free_stock_panel.py first", file=sys.stderr)
        raise SystemExit(2)
    panel = pd.read_parquet(panel_path)
    cfg = load_backtest_config(config.raw)
    if args.max_daily_amount_participation is not None:
        cfg = replace(cfg, max_daily_amount_participation=args.max_daily_amount_participation)
    output_label = args.output_label.strip()
    if not output_label and cfg.max_daily_amount_participation is not None:
        output_label = _format_participation_label(cfg.max_daily_amount_participation)
    panel_snapshot = inspect_panel(
        panel,
        min_symbols=cfg.min_symbols,
        require_amount=cfg.max_daily_amount_participation is not None,
    )
    panel_by_date = _prepare_daily_panel(panel)
    specs = build_real_stock_strategy_specs(config.raw)
    specs = [spec for spec in specs if strategy_allowed_in_tier(spec.family, "free_real").allowed]
    if args.max_strategies > 0:
        specs = specs[: args.max_strategies]

    frames = []
    for index, spec in enumerate(specs, start=1):
        print(f"[target-backtest] {index}/{len(specs)} {spec.name}", flush=True)
        frame = evaluate_free_real_strategy(
            panel,
            spec,
            cfg=cfg,
            max_windows=args.max_windows,
            panel_by_date=panel_by_date,
        )
        if not frame.empty:
            frames.append(frame)
    windows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    leaderboard = aggregate_free_real_windows(windows, cfg)

    windows_path = _output_path(Path(config.raw["paths"]["target_windows"]), output_label)
    leaderboard_path = _output_path(Path(config.raw["paths"]["target_leaderboard"]), output_label)
    report_path = _output_path(Path(config.raw["paths"]["target_report"]), output_label)
    windows_path.parent.mkdir(parents=True, exist_ok=True)
    windows.to_csv(windows_path, index=False, encoding="utf-8")
    leaderboard.to_csv(leaderboard_path, index=False, encoding="utf-8")
    write_report(
        path=report_path,
        leaderboard=leaderboard,
        windows=windows,
        config_path=args.config,
        panel_path=panel_path,
        panel_snapshot=panel_snapshot,
        run_scope=(
            f"max_strategies={args.max_strategies or 'all'}, max_windows={args.max_windows or 'all'}, "
            f"output_label={output_label or 'baseline'}, max_daily_amount_participation={cfg.max_daily_amount_participation}"
        ),
        max_daily_amount_participation=cfg.max_daily_amount_participation,
        generated_at=datetime.now(),
    )
    print(f"strategies={len(specs)}")
    print(f"windows={len(windows)}")
    print(f"leaderboard={leaderboard_path}")
    print(f"report={report_path}")
    if not leaderboard.empty:
        print(leaderboard.head(8).to_string(index=False))


if __name__ == "__main__":
    main()

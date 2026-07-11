from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import erf, exp, floor, log, sqrt
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .free_real_backtest import (
    DailyRows,
    FreeRealBacktestConfig,
    _date_index,
    _prepare_daily_panel,
    select_rolling_windows,
    simulate_free_real_window,
)
from .real_strategies import (
    RealStockStrategySpec,
    compute_real_stock_scores,
    strategy_rebalance_dates,
    target_symbols_by_signal_date,
)
from .simulator import rolling_windows, summarize_equity


FUTURES_CONTRACTS = {
    "IF": {"symbol": "000300", "multiplier": 300, "name": "沪深300"},
    "IH": {"symbol": "000016", "multiplier": 300, "name": "上证50"},
    "IC": {"symbol": "000905", "multiplier": 200, "name": "中证500"},
    "IM": {"symbol": "000852", "multiplier": 200, "name": "中证1000"},
}

OPTION_UNDERLYINGS = {
    "IO": {"symbol": "000300", "multiplier": 100, "name": "沪深300股指期权"},
    "HO": {"symbol": "000016", "multiplier": 100, "name": "上证50股指期权"},
    "MO": {"symbol": "000852", "multiplier": 100, "name": "中证1000股指期权"},
}

DELTA_TO_MONEYNESS = {
    0.65: 0.95,
    0.50: 1.00,
    0.35: 1.05,
    0.25: 1.10,
}


@dataclass(frozen=True)
class FuturesOverlaySpec:
    contract: str
    target_beta: float
    margin_rate: float
    cash_buffer_pct: float
    signal_lookback: int = 60

    @property
    def name(self) -> str:
        return (
            f"futures_{self.contract}_beta{self.target_beta:g}"
            f"_margin{self.margin_rate:g}_buffer{self.cash_buffer_pct:g}"
        )


@dataclass(frozen=True)
class OptionOverlaySpec:
    contract: str
    monthly_budget_pct_nav: float
    tenor_days: int
    delta: float
    iv_multiplier: float
    signal_lookback: int = 60

    @property
    def name(self) -> str:
        return (
            f"option_{self.contract}_call_budget{self.monthly_budget_pct_nav:g}"
            f"_t{self.tenor_days}_d{self.delta:g}_iv{self.iv_multiplier:g}"
        )


def load_index_close(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "date" not in frame.columns:
        raise ValueError(f"index close file missing date column: {path}")
    date_values = frame["date"].astype(str)
    compact = date_values.str.replace("-", "", regex=False)
    frame["date"] = pd.to_datetime(compact, format="%Y%m%d")
    frame = frame.set_index("date").sort_index()
    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _returns_from_wealth(wealth: pd.Series) -> pd.Series:
    base = wealth.shift(1)
    returns = wealth.div(base.replace(0.0, np.nan)).sub(1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return returns.clip(lower=-1.0)


def _summarize_overlay(equity: pd.DataFrame, cfg: FreeRealBacktestConfig, extra: dict[str, float]) -> dict[str, float]:
    frame = equity.copy()
    frame["daily_return"] = _returns_from_wealth(frame["wealth"])
    metrics = summarize_equity(frame, monthly_deposit=cfg.monthly_deposit)
    metrics.update(extra)
    return metrics


def _signal_positive(prices: pd.Series, index_pos: int, lookback: int) -> bool:
    if index_pos <= lookback:
        return False
    current = prices.iloc[index_pos - 1]
    previous = prices.iloc[index_pos - lookback - 1]
    return pd.notna(current) and pd.notna(previous) and previous > 0.0 and current / previous - 1.0 > 0.0


def apply_futures_overlay(equity: pd.DataFrame, prices: pd.Series, spec: FuturesOverlaySpec, cfg: FreeRealBacktestConfig) -> tuple[pd.DataFrame, dict[str, float]]:
    contract = FUTURES_CONTRACTS[spec.contract]
    multiplier = float(contract["multiplier"])
    aligned = prices.reindex(equity.index).ffill()
    month_start = aligned.index.to_period("M") != aligned.index.to_series().shift(1).dt.to_period("M")

    cumulative_pnl = 0.0
    lots = 0
    records: list[dict[str, float | str]] = []
    stats = {
        "futures_pnl": 0.0,
        "futures_rebalance_events": 0.0,
        "futures_cannot_afford_events": 0.0,
        "futures_forced_liquidations": 0.0,
        "avg_futures_lots": 0.0,
    }

    for i, trade_date in enumerate(aligned.index):
        price = float(aligned.iloc[i]) if pd.notna(aligned.iloc[i]) else np.nan
        previous = float(aligned.iloc[i - 1]) if i > 0 and pd.notna(aligned.iloc[i - 1]) else price
        if lots and np.isfinite(price) and np.isfinite(previous):
            pnl = lots * multiplier * (price - previous)
            cumulative_pnl += pnl
            stats["futures_pnl"] += pnl

        base_cash = float(equity.iloc[i].get("cash", 0.0))
        base_wealth = float(equity.iloc[i]["wealth"])
        available_cash = max(base_cash + cumulative_pnl, 0.0)
        notional = price * multiplier if np.isfinite(price) else float("nan")
        margin = abs(lots) * notional * spec.margin_rate if np.isfinite(notional) else 0.0
        if lots and margin > available_cash * spec.cash_buffer_pct:
            lots = 0
            stats["futures_forced_liquidations"] += 1.0

        if bool(month_start.iloc[i]) and np.isfinite(notional) and notional > 0.0:
            stats["futures_rebalance_events"] += 1.0
            desired_lots = 0
            target_notional = 0.0
            if _signal_positive(aligned, i, spec.signal_lookback):
                target_notional = max(base_wealth, 0.0) * spec.target_beta
                desired_lots = int(floor(target_notional / notional))
            max_margin = max(available_cash * spec.cash_buffer_pct, 0.0)
            max_lots = int(floor(max_margin / (notional * spec.margin_rate))) if spec.margin_rate > 0.0 else 0
            next_lots = max(min(desired_lots, max_lots), 0)
            if target_notional > 0.0 and next_lots == 0:
                stats["futures_cannot_afford_events"] += 1.0
            lots = next_lots

        overlay_wealth = max(base_wealth + cumulative_pnl, 0.0)
        stats["avg_futures_lots"] += abs(float(lots))
        records.append(
            {
                "trade_date": trade_date.strftime("%Y%m%d"),
                "wealth": overlay_wealth,
                "cash": max(base_cash + cumulative_pnl, 0.0),
                "futures_lots": float(lots),
            }
        )

    if records:
        stats["avg_futures_lots"] /= len(records)
    out = pd.DataFrame(records, index=equity.index)
    return out, _summarize_overlay(out, cfg, stats)


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def black_scholes_call_price(spot: float, strike: float, years: float, sigma: float) -> float:
    if spot <= 0.0 or strike <= 0.0 or years <= 0.0 or sigma <= 0.0:
        return max(spot - strike, 0.0)
    vol_sqrt_t = sigma * sqrt(years)
    d1 = (log(spot / strike) + 0.5 * sigma * sigma * years) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)


def _realized_vol(prices: pd.Series, index_pos: int, lookback: int) -> float:
    start = max(0, index_pos - lookback)
    returns = prices.iloc[start:index_pos].pct_change(fill_method=None).dropna()
    if returns.empty:
        return 0.20
    vol = float(returns.std(ddof=0) * sqrt(252.0))
    return min(max(vol, 0.10), 0.80)


def _expiry_pos(index: pd.DatetimeIndex, start_pos: int, tenor_days: int) -> int:
    target = index[start_pos] + pd.Timedelta(days=tenor_days)
    pos = int(index.searchsorted(target, side="left"))
    return min(max(pos, start_pos + 1), len(index) - 1)


def apply_option_overlay(equity: pd.DataFrame, prices: pd.Series, spec: OptionOverlaySpec, cfg: FreeRealBacktestConfig) -> tuple[pd.DataFrame, dict[str, float]]:
    contract = OPTION_UNDERLYINGS[spec.contract]
    multiplier = float(contract["multiplier"])
    aligned = prices.reindex(equity.index).ffill()
    month_start = aligned.index.to_period("M") != aligned.index.to_series().shift(1).dt.to_period("M")
    moneyness = DELTA_TO_MONEYNESS.get(round(spec.delta, 2), 1.0)

    cumulative_pnl = 0.0
    positions: list[dict[str, float | int]] = []
    records: list[dict[str, float | str]] = []
    stats = {
        "option_premium_spent": 0.0,
        "option_payoff": 0.0,
        "option_contracts_bought": 0.0,
        "option_cannot_afford_events": 0.0,
        "option_expired_worthless": 0.0,
    }

    for i, trade_date in enumerate(aligned.index):
        price = float(aligned.iloc[i]) if pd.notna(aligned.iloc[i]) else np.nan
        remaining: list[dict[str, float | int]] = []
        for position in positions:
            if int(position["expiry_pos"]) <= i:
                payoff_per_point = max(price - float(position["strike"]), 0.0) if np.isfinite(price) else 0.0
                payoff = float(position["contracts"]) * multiplier * payoff_per_point
                cumulative_pnl += payoff
                stats["option_payoff"] += payoff
                if payoff <= 0.0:
                    stats["option_expired_worthless"] += float(position["contracts"])
            else:
                remaining.append(position)
        positions = remaining

        base_cash = float(equity.iloc[i].get("cash", 0.0))
        base_wealth = float(equity.iloc[i]["wealth"])
        if bool(month_start.iloc[i]) and np.isfinite(price) and price > 0.0 and _signal_positive(aligned, i, spec.signal_lookback):
            budget = min(max(base_wealth, 0.0) * spec.monthly_budget_pct_nav, max(base_cash + cumulative_pnl, 0.0))
            years = spec.tenor_days / 365.0
            sigma = _realized_vol(aligned, i, spec.signal_lookback) * spec.iv_multiplier
            strike = price * moneyness
            unit_price = black_scholes_call_price(price, strike, years, sigma)
            premium_per_contract = unit_price * multiplier
            contracts = int(floor(budget / premium_per_contract)) if premium_per_contract > 0.0 else 0
            if contracts <= 0:
                stats["option_cannot_afford_events"] += 1.0
            else:
                premium = contracts * premium_per_contract
                cumulative_pnl -= premium
                stats["option_premium_spent"] += premium
                stats["option_contracts_bought"] += contracts
                positions.append(
                    {
                        "expiry_pos": _expiry_pos(aligned.index, i, spec.tenor_days),
                        "strike": strike,
                        "contracts": contracts,
                    }
                )

        overlay_wealth = max(base_wealth + cumulative_pnl, 0.0)
        records.append(
            {
                "trade_date": trade_date.strftime("%Y%m%d"),
                "wealth": overlay_wealth,
                "cash": max(base_cash + cumulative_pnl, 0.0),
                "open_option_positions": float(len(positions)),
            }
        )

    out = pd.DataFrame(records, index=equity.index)
    return out, _summarize_overlay(out, cfg, stats)


def select_base_rows(leaderboard: pd.DataFrame, top_n: int) -> pd.DataFrame:
    if leaderboard.empty:
        return leaderboard
    rows = [leaderboard.sort_values(["score", "p_success", "median_w24"], ascending=False).head(top_n)]
    family_idx = leaderboard.groupby("family")["score"].idxmax()
    rows.append(leaderboard.loc[family_idx])
    selected = pd.concat(rows, ignore_index=True).drop_duplicates(["strategy", "deposit_timing"])
    return selected.sort_values(["score", "p_success", "median_w24"], ascending=False).reset_index(drop=True)


def highest_success_row(
    leaderboard: pd.DataFrame,
    overlay_type: str | None = None,
) -> pd.Series | None:
    rows = leaderboard
    if overlay_type is not None:
        rows = rows.loc[rows["overlay_type"] == overlay_type]
    if rows.empty:
        return None
    sort_columns = [
        column
        for column in ["p_success", "p10_w24", "median_w24", "p95_max_drawdown"]
        if column in rows.columns
    ]
    ascending = [False, False, False, True][: len(sort_columns)]
    return rows.sort_values(sort_columns, ascending=ascending).iloc[0]


def build_futures_specs(
    contracts: Iterable[str] = ("IF", "IH", "IC", "IM"),
    target_betas: Iterable[float] = (0.3, 0.5),
    margin_rates: Iterable[float] = (0.15, 0.20),
    cash_buffer_pcts: Iterable[float] = (0.33, 0.50),
) -> list[FuturesOverlaySpec]:
    return [
        FuturesOverlaySpec(contract=contract, target_beta=beta, margin_rate=margin, cash_buffer_pct=buffer)
        for contract in contracts
        for beta in target_betas
        for margin in margin_rates
        for buffer in cash_buffer_pcts
    ]


def build_option_specs(
    contracts: Iterable[str] = ("IO", "HO", "MO"),
    budgets: Iterable[float] = (0.01, 0.02, 0.05),
    tenors: Iterable[int] = (30, 60),
    deltas: Iterable[float] = (0.35, 0.50),
    iv_multipliers: Iterable[float] = (1.3, 1.6),
) -> list[OptionOverlaySpec]:
    return [
        OptionOverlaySpec(
            contract=contract,
            monthly_budget_pct_nav=budget,
            tenor_days=tenor,
            delta=delta,
            iv_multiplier=iv_multiplier,
        )
        for contract in contracts
        for budget in budgets
        for tenor in tenors
        for delta in deltas
        for iv_multiplier in iv_multipliers
    ]


def equity_windows_for_strategy(
    panel: pd.DataFrame,
    spec: RealStockStrategySpec,
    cfg: FreeRealBacktestConfig,
    deposit_timing: str,
    panel_by_date: Mapping[str, DailyRows] | None = None,
    max_windows: int = 0,
) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.DataFrame]]:
    date_index = _date_index(panel)
    windows = rolling_windows(date_index, window_months=cfg.window_months, min_trading_days=cfg.min_trading_days)
    windows = select_rolling_windows(windows, max_windows=max_windows, sampling="even")
    panel_by_date = panel_by_date or _prepare_daily_panel(panel)
    trading_dates = sorted(panel_by_date)
    scores = compute_real_stock_scores(panel, spec)
    holding_k = int(spec.params.get("holding_k", 10))
    top_by_signal_date = target_symbols_by_signal_date(scores, spec=spec, holding_k=holding_k)
    rebalance = str(spec.params.get("rebalance", "daily"))
    rebalance_dates = set(strategy_rebalance_dates(panel, rebalance))
    rebalance_only_on_target_change = str(spec.params.get("kind", "")) in {
        "real_stock_breakout",
        "real_stateful_trend",
        "real_volatility_contraction",
        "real_regime_contraction",
        "real_gap_intraday",
        "real_momentum_acceleration",
    }
    out = []
    for start, end in windows:
        equity, metrics = simulate_free_real_window(
            panel_by_date=panel_by_date,
            trading_dates=trading_dates,
            top_by_signal_date=top_by_signal_date,
            rebalance_dates=rebalance_dates,
            start=start,
            end=end,
            deposit_timing=deposit_timing,
            cfg=cfg,
            rebalance_only_on_target_change=rebalance_only_on_target_change,
        )
        if metrics:
            out.append((start, end, equity))
    return out


def aggregate_overlay_windows(windows: pd.DataFrame, cfg: FreeRealBacktestConfig) -> pd.DataFrame:
    if windows.empty:
        return pd.DataFrame()
    rows = []
    group_cols = ["base_strategy", "base_family", "deposit_timing", "overlay_type", "overlay_name"]

    def mean_col(group: pd.DataFrame, column: str) -> float:
        if column not in group.columns:
            return 0.0
        value = float(group[column].fillna(0.0).mean())
        return value if np.isfinite(value) else 0.0

    for key, group in windows.groupby(group_cols, sort=True):
        success = (group["w12"] >= cfg.target_month_12) & (group["w24"] >= cfg.target_month_24)
        rows.append(
            {
                "base_strategy": key[0],
                "base_family": key[1],
                "deposit_timing": key[2],
                "overlay_type": key[3],
                "overlay_name": key[4],
                "n_windows": int(len(group)),
                "p_success": float(success.mean()),
                "p_w12": float((group["w12"] >= cfg.target_month_12).mean()),
                "p_w24": float((group["w24"] >= cfg.target_month_24).mean()),
                "median_w24": float(group["w24"].median()),
                "p10_w24": float(group["w24"].quantile(0.10)),
                "p90_w24": float(group["w24"].quantile(0.90)),
                "p95_max_drawdown": float(group["max_drawdown"].quantile(0.95)),
                "p_w24_below_deposit": float((group["w24"] < group["total_deposit"]).mean()),
                "p_drawdown_gt_50": float((group["max_drawdown"] > 0.50).mean()),
                "avg_futures_pnl": mean_col(group, "futures_pnl"),
                "avg_futures_cannot_afford": mean_col(group, "futures_cannot_afford_events"),
                "avg_futures_forced_liquidations": mean_col(group, "futures_forced_liquidations"),
                "avg_option_premium_spent": mean_col(group, "option_premium_spent"),
                "avg_option_payoff": mean_col(group, "option_payoff"),
                "avg_option_contracts_bought": mean_col(group, "option_contracts_bought"),
            }
        )
    out = pd.DataFrame(rows)
    out["score"] = (
        100.0 * out["p_success"]
        + 20.0 * np.log((out["median_w24"] / cfg.target_month_24).clip(lower=1e-9))
        - 30.0 * out["p_w24_below_deposit"]
        - 20.0 * out["p_drawdown_gt_50"]
    )
    return out.sort_values(["score", "p_success", "median_w24"], ascending=False).reset_index(drop=True)

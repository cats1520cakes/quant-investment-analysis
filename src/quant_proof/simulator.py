from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from .metrics import expected_shortfall, max_drawdown, recovery_days, ulcer_index
from .strategies import StrategySpec, compute_weights


@dataclass(frozen=True)
class ExecutionCost:
    commission_bps: float
    min_commission: float
    transfer_fee_bps_each_side: float
    stamp_tax_sell_bps: float
    slippage_bps: float

    @property
    def buy_bps(self) -> float:
        return self.commission_bps + self.transfer_fee_bps_each_side + self.slippage_bps

    @property
    def sell_bps(self) -> float:
        return self.commission_bps + self.transfer_fee_bps_each_side + self.stamp_tax_sell_bps + self.slippage_bps


def first_trading_day_mask(index: pd.DatetimeIndex) -> pd.Series:
    dates = pd.Series(index=index, data=index)
    return dates.dt.to_period("M") != dates.shift(1).dt.to_period("M")


def last_trading_day_mask(index: pd.DatetimeIndex) -> pd.Series:
    dates = pd.Series(index=index, data=index)
    return dates.dt.to_period("M") != dates.shift(-1).dt.to_period("M")


def _window_end(index: pd.DatetimeIndex, start: pd.Timestamp, window_months: int) -> pd.Timestamp:
    boundary = start.to_period("M").start_time + pd.DateOffset(months=window_months)
    eligible = index[index < boundary]
    if eligible.empty:
        return index[-1]
    return eligible[-1]


def rolling_windows(index: pd.DatetimeIndex, window_months: int, min_trading_days: int) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    if window_months <= 0:
        raise ValueError("window_months must be positive")
    starts = index[first_trading_day_mask(index).values]
    windows: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    for start in starts:
        end = _window_end(index, start, window_months)
        count = int(((index >= start) & (index <= end)).sum())
        if count >= min_trading_days and end < index[-1]:
            windows.append((start, end))
    return windows


def simulate_path(
    returns: pd.DataFrame,
    target_weights: pd.DataFrame,
    monthly_deposit: float,
    deposit_timing: str,
    execution: ExecutionCost,
    gross_exposure: float = 1.0,
    financing_rate_annual: float = 0.0,
) -> Tuple[pd.DataFrame, Dict[str, float]]:
    if len(returns) != len(target_weights):
        raise ValueError("returns and target_weights must have the same length")
    index = returns.index
    month_start = first_trading_day_mask(index).to_numpy(dtype=bool)
    month_end = last_trading_day_mask(index).to_numpy(dtype=bool)
    weights = target_weights.shift(1).reindex(index).fillna(0.0)
    row_sums = weights.sum(axis=1).replace(0.0, np.nan)
    weights = weights.div(row_sums, axis=0).fillna(0.0) * gross_exposure
    returns_aligned = returns.reindex(index=index, columns=weights.columns).fillna(0.0)
    weight_values = weights.to_numpy(dtype=float)
    return_values = returns_aligned.to_numpy(dtype=float)

    wealth = 0.0
    prev_weights = np.zeros(weight_values.shape[1], dtype=float)
    wealth_values = np.zeros(len(index), dtype=float)
    day_returns = np.zeros(len(index), dtype=float)
    invested_weights = np.zeros(len(index), dtype=float)
    fee_values = np.zeros(len(index), dtype=float)
    financing_values = np.zeros(len(index), dtype=float)
    turnover_values = np.zeros(len(index), dtype=float)
    fee_drag = 0.0
    turnover_total = 0.0
    financing_drag = 0.0

    for i in range(len(index)):
        if deposit_timing == "beginning" and month_start[i]:
            wealth += monthly_deposit

        desired_weights = weight_values[i]
        if wealth > 0:
            diff = desired_weights - prev_weights
            buys = float(np.maximum(diff, 0.0).sum())
            sells = float(np.maximum(-diff, 0.0).sum())
            fee = wealth * (buys * execution.buy_bps + sells * execution.sell_bps) / 10000.0
            if buys + sells > 0 and fee > 0:
                fee = max(fee, execution.min_commission)
            fee = min(fee, wealth)
            wealth -= fee
            fee_drag += fee
            turnover_total += float(buys + sells)

        day_return = float(np.dot(return_values[i], desired_weights))
        if gross_exposure > 1.0 and financing_rate_annual > 0.0 and wealth > 0:
            daily_interest = wealth * (gross_exposure - 1.0) * financing_rate_annual / 252.0
            wealth -= daily_interest
            financing_drag += daily_interest

        wealth *= 1.0 + day_return

        if deposit_timing == "ending" and month_end[i]:
            wealth += monthly_deposit

        wealth = max(wealth, 0.0)
        wealth_values[i] = wealth
        day_returns[i] = day_return
        invested_weights[i] = float(desired_weights.sum())
        fee_values[i] = fee_drag
        financing_values[i] = financing_drag
        turnover_values[i] = turnover_total
        prev_weights = desired_weights

    equity = pd.DataFrame(
        {
            "wealth": wealth_values,
            "daily_return": day_returns,
            "invested_weight": invested_weights,
            "fee_drag": fee_values,
            "financing_drag": financing_values,
            "turnover": turnover_values,
        },
        index=index,
    )
    metrics = summarize_equity(equity, monthly_deposit=monthly_deposit)
    metrics.update(
        {
            "fee_drag": float(fee_drag),
            "financing_drag": float(financing_drag),
            "turnover": float(turnover_total),
            "avg_turnover": float(turnover_total / max(len(index), 1)),
        }
    )
    return equity, metrics


def _summarize_arrays(index: pd.DatetimeIndex, wealth: np.ndarray, daily_returns: np.ndarray, monthly_deposit: float) -> Dict[str, float]:
    if len(wealth) == 0:
        return {
            "w12": float("nan"),
            "w24": float("nan"),
            "total_deposit": 0.0,
            "net_profit": float("nan"),
            "max_drawdown": float("nan"),
            "ulcer_index": float("nan"),
            "p99_one_day_loss": float("nan"),
            "expected_shortfall_95": float("nan"),
            "recovery_days": 0.0,
        }
    start = index[0]
    month_number = ((index.year - start.year) * 12 + (index.month - start.month) + 1).astype(int)
    w12 = float(wealth[month_number <= 12][-1]) if np.any(month_number <= 12) else float("nan")
    w24 = float(wealth[-1])
    total_deposit = float(monthly_deposit * min(int(month_number.max()), 24))
    peak = np.maximum.accumulate(wealth)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = np.where(peak > 0, wealth / peak - 1.0, 0.0)
    underwater = drawdown < 0.0
    longest = current = 0
    for flag in underwater:
        if flag:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    p99 = float(np.quantile(daily_returns, 0.01)) if len(daily_returns) else float("nan")
    threshold = float(np.quantile(daily_returns, 0.05)) if len(daily_returns) else float("nan")
    tail = daily_returns[daily_returns <= threshold]
    return {
        "w12": w12,
        "w24": w24,
        "total_deposit": total_deposit,
        "net_profit": w24 - total_deposit,
        "max_drawdown": float(-np.min(drawdown)),
        "ulcer_index": float(np.sqrt(np.mean(np.square(np.minimum(drawdown, 0.0) * 100.0)))),
        "p99_one_day_loss": p99,
        "expected_shortfall_95": float(np.mean(tail)) if len(tail) else threshold,
        "recovery_days": float(longest),
    }


def simulate_path_metrics_fast(
    index: pd.DatetimeIndex,
    returns_values: np.ndarray,
    target_weights_values: np.ndarray,
    month_start: np.ndarray,
    month_end: np.ndarray,
    monthly_deposit: float,
    deposit_timing: str,
    execution: ExecutionCost,
    gross_exposure: float = 1.0,
    financing_rate_annual: float = 0.0,
) -> Dict[str, float]:
    if len(index) == 0:
        return _summarize_arrays(index, np.array([]), np.array([]), monthly_deposit)

    shifted_weights = np.zeros_like(target_weights_values, dtype=float)
    if len(target_weights_values) > 1:
        shifted_weights[1:] = target_weights_values[:-1]
    row_sums = shifted_weights.sum(axis=1)
    nonzero = row_sums > 0
    weights = np.zeros_like(shifted_weights, dtype=float)
    weights[nonzero] = shifted_weights[nonzero] / row_sums[nonzero, None] * gross_exposure

    wealth = 0.0
    prev_weights = np.zeros(weights.shape[1], dtype=float)
    wealth_values = np.zeros(len(index), dtype=float)
    day_returns = np.zeros(len(index), dtype=float)
    fee_drag = 0.0
    turnover_total = 0.0
    financing_drag = 0.0

    for i in range(len(index)):
        if deposit_timing == "beginning" and month_start[i]:
            wealth += monthly_deposit

        desired_weights = weights[i]
        if wealth > 0:
            diff = desired_weights - prev_weights
            buys = float(np.maximum(diff, 0.0).sum())
            sells = float(np.maximum(-diff, 0.0).sum())
            fee = wealth * (buys * execution.buy_bps + sells * execution.sell_bps) / 10000.0
            if buys + sells > 0 and fee > 0:
                fee = max(fee, execution.min_commission)
            fee = min(fee, wealth)
            wealth -= fee
            fee_drag += fee
            turnover_total += buys + sells

        day_return = float(np.dot(returns_values[i], desired_weights))
        if gross_exposure > 1.0 and financing_rate_annual > 0.0 and wealth > 0:
            daily_interest = wealth * (gross_exposure - 1.0) * financing_rate_annual / 252.0
            wealth -= daily_interest
            financing_drag += daily_interest
        wealth *= 1.0 + day_return

        if deposit_timing == "ending" and month_end[i]:
            wealth += monthly_deposit

        wealth = max(wealth, 0.0)
        wealth_values[i] = wealth
        day_returns[i] = day_return
        prev_weights = desired_weights

    metrics = _summarize_arrays(index, wealth_values, day_returns, monthly_deposit)
    metrics.update(
        {
            "fee_drag": float(fee_drag),
            "financing_drag": float(financing_drag),
            "turnover": float(turnover_total),
            "avg_turnover": float(turnover_total / max(len(index), 1)),
        }
    )
    return metrics


def summarize_equity(equity: pd.DataFrame, monthly_deposit: float) -> Dict[str, float]:
    if "daily_return" not in equity.columns:
        raise ValueError("equity must include a 'daily_return' column for flow-adjusted risk metrics")

    wealth = equity["wealth"]
    daily_returns = pd.to_numeric(equity["daily_return"], errors="coerce")
    if daily_returns.isna().any() or np.isinf(daily_returns).any() or daily_returns.lt(-1.0).any():
        raise ValueError("equity daily_return must be finite and no lower than -100%")
    compounded_nav = (1.0 + daily_returns).cumprod()
    flow_adjusted_nav = pd.Series(
        np.concatenate(([1.0], compounded_nav.to_numpy(dtype=float))),
        dtype=float,
    )
    start = equity.index[0]
    month_number = ((equity.index.year - start.year) * 12 + (equity.index.month - start.month) + 1).astype(int)
    w12_rows = equity.loc[month_number <= 12]
    w12 = float(w12_rows["wealth"].iloc[-1]) if not w12_rows.empty else float("nan")
    w24 = float(wealth.iloc[-1]) if not wealth.empty else float("nan")
    total_deposit = float(monthly_deposit * min(int(month_number.max()), 24))
    return {
        "w12": w12,
        "w24": w24,
        "total_deposit": total_deposit,
        "net_profit": w24 - total_deposit,
        "max_drawdown": max_drawdown(flow_adjusted_nav),
        "ulcer_index": ulcer_index(flow_adjusted_nav),
        "p99_one_day_loss": float(daily_returns.quantile(0.01)),
        "expected_shortfall_95": expected_shortfall(daily_returns, 0.95),
        "recovery_days": float(recovery_days(flow_adjusted_nav)),
    }


def evaluate_rolling_strategy(
    close: pd.DataFrame,
    spec: StrategySpec,
    config: Dict[str, object],
    deposit_timing: str,
) -> pd.DataFrame:
    prices = close.dropna(how="all").copy()
    returns = prices.pct_change(fill_method=None).fillna(0.0)
    weights = compute_weights(prices, spec)
    execution = ExecutionCost(**config["execution"])
    gross_exposure = float(spec.params.get("gross_exposure", 1.0))
    financing_rate = float(spec.params.get("financing_rate_annual", 0.0))
    returns_values = returns.reindex(index=prices.index, columns=weights.columns).fillna(0.0).to_numpy(dtype=float)
    weights_values = weights.reindex(index=prices.index, columns=weights.columns).fillna(0.0).to_numpy(dtype=float)
    month_start = first_trading_day_mask(prices.index).to_numpy(dtype=bool)
    month_end = last_trading_day_mask(prices.index).to_numpy(dtype=bool)
    windows = rolling_windows(
        prices.index,
        window_months=int(config["rolling"]["window_months"]),
        min_trading_days=int(config["rolling"]["min_trading_days"]),
    )
    rows = []
    for start, end in windows:
        start_i = int(prices.index.get_loc(start))
        end_i = int(prices.index.get_loc(end)) + 1
        metrics = simulate_path_metrics_fast(
            index=prices.index[start_i:end_i],
            returns_values=returns_values[start_i:end_i],
            target_weights_values=weights_values[start_i:end_i],
            month_start=month_start[start_i:end_i],
            month_end=month_end[start_i:end_i],
            monthly_deposit=float(config["monthly_deposit"]),
            deposit_timing=deposit_timing,
            execution=execution,
            gross_exposure=gross_exposure,
            financing_rate_annual=financing_rate,
        )
        rows.append(
            {
                "strategy": spec.name,
                "family": spec.family,
                "deposit_timing": deposit_timing,
                "start": start.date().isoformat(),
                "end": end.date().isoformat(),
                **metrics,
            }
        )
    return pd.DataFrame(rows)


def aggregate_windows(windows: pd.DataFrame, target_12: float, target_24: float) -> pd.DataFrame:
    group_cols = ["strategy", "family", "deposit_timing"]
    rows = []
    for key, group in windows.groupby(group_cols):
        success = (group["w12"] >= target_12) & (group["w24"] >= target_24)
        rows.append(
            {
                "strategy": key[0],
                "family": key[1],
                "deposit_timing": key[2],
                "n_windows": int(len(group)),
                "p_success": float(success.mean()),
                "p_w12": float((group["w12"] >= target_12).mean()),
                "p_w24": float((group["w24"] >= target_24).mean()),
                "median_w24": float(group["w24"].median()),
                "p10_w24": float(group["w24"].quantile(0.10)),
                "p90_w24": float(group["w24"].quantile(0.90)),
                "median_max_drawdown": float(group["max_drawdown"].median()),
                "p95_max_drawdown": float(group["max_drawdown"].quantile(0.95)),
                "p_w24_below_deposit": float((group["w24"] < group["total_deposit"]).mean()),
                "p_w24_below_720k": float((group["w24"] < 720000).mean()),
                "p_drawdown_gt_35": float((group["max_drawdown"] > 0.35).mean()),
                "p_drawdown_gt_50": float((group["max_drawdown"] > 0.50).mean()),
                "avg_turnover": float(group["avg_turnover"].mean()),
                "fee_drag": float(group["fee_drag"].mean()),
                "financing_drag": float(group["financing_drag"].mean()),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["score"] = (
        100.0 * out["p_success"]
        + 20.0 * np.log((out["median_w24"] / target_24).clip(lower=1e-9))
        - 35.0 * out["p_w24_below_720k"]
        - 25.0 * out["p_drawdown_gt_35"]
        - 15.0 * out["p_drawdown_gt_50"]
        - 10.0 * out["avg_turnover"].clip(lower=0.0, upper=1.0)
    )
    return out.sort_values(["score", "p_success", "median_w24"], ascending=False)


def bootstrap_strategy_returns(
    close: pd.DataFrame,
    spec: StrategySpec,
    config: Dict[str, object],
    deposit_timing: str,
    rng: np.random.Generator,
    block_size: int,
    paths: int,
) -> pd.DataFrame:
    prices = close.dropna(how="all").copy()
    returns = prices.pct_change(fill_method=None).fillna(0.0)
    weights = compute_weights(prices, spec).shift(1).fillna(0.0)
    gross_exposure = float(spec.params.get("gross_exposure", 1.0))
    strategy_returns = (returns * weights).sum(axis=1)
    strategy_returns = strategy_returns.dropna()
    if len(strategy_returns) < block_size * 4:
        return pd.DataFrame()

    execution = ExecutionCost(**config["execution"])
    trading_days = int(config["bootstrap"]["trading_days"])
    financing_rate = float(spec.params.get("financing_rate_annual", 0.0))
    rows = []
    values = strategy_returns.to_numpy()
    max_start = len(values) - block_size
    synthetic_index = pd.bdate_range("2000-01-03", periods=trading_days)
    synthetic_weights = np.ones((trading_days, 1), dtype=float)
    month_start = first_trading_day_mask(synthetic_index).to_numpy(dtype=bool)
    month_end = last_trading_day_mask(synthetic_index).to_numpy(dtype=bool)
    for path_id in range(paths):
        sampled = []
        while len(sampled) < trading_days:
            start = int(rng.integers(0, max_start + 1))
            sampled.extend(values[start : start + block_size])
        sampled = sampled[:trading_days]
        synthetic_returns = np.asarray(sampled, dtype=float).reshape(-1, 1)
        metrics = simulate_path_metrics_fast(
            index=synthetic_index,
            returns_values=synthetic_returns,
            target_weights_values=synthetic_weights,
            month_start=month_start,
            month_end=month_end,
            monthly_deposit=float(config["monthly_deposit"]),
            deposit_timing=deposit_timing,
            execution=execution,
            gross_exposure=gross_exposure,
            financing_rate_annual=financing_rate if gross_exposure > 1.0 else 0.0,
        )
        rows.append(
            {
                "strategy": spec.name,
                "family": spec.family,
                "deposit_timing": deposit_timing,
                "block_size": block_size,
                "path_id": path_id,
                **metrics,
            }
        )
    return pd.DataFrame(rows)

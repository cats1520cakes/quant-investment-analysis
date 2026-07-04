from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StrategySpec:
    name: str
    family: str
    params: Dict[str, object]


def _rebalance_mask(index: pd.DatetimeIndex, frequency: str) -> pd.Series:
    dates = pd.Series(index=index, data=index)
    if frequency == "daily":
        return pd.Series(True, index=index)
    if frequency == "2d":
        return pd.Series(np.arange(len(index)) % 2 == 0, index=index)
    if frequency == "weekly":
        return dates.dt.to_period("W-FRI") != dates.shift(1).dt.to_period("W-FRI")
    if frequency == "biweekly":
        week = dates.dt.isocalendar().week.astype(int)
        year = dates.dt.isocalendar().year.astype(int)
        bucket = year * 100 + (week // 2)
        return bucket != bucket.shift(1)
    if frequency == "monthly":
        return dates.dt.to_period("M") != dates.shift(1).dt.to_period("M")
    raise ValueError(f"unsupported rebalance frequency: {frequency}")


def _cash_filter(close: pd.DataFrame, benchmark: str, mode: str) -> pd.Series:
    if mode == "none" or benchmark not in close.columns:
        return pd.Series(True, index=close.index)
    if mode.startswith("ma"):
        window = int(mode.replace("ma", ""))
        ma = close[benchmark].rolling(window, min_periods=max(10, window // 2)).mean()
        return (close[benchmark] > ma).fillna(False)
    if mode == "dual_ma60_200":
        ma60 = close[benchmark].rolling(60, min_periods=30).mean()
        ma200 = close[benchmark].rolling(200, min_periods=100).mean()
        return ((close[benchmark] > ma60) & (ma60 > ma200)).fillna(False)
    raise ValueError(f"unsupported market filter: {mode}")


def buy_and_hold_weights(close: pd.DataFrame, symbol: str) -> pd.DataFrame:
    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    if symbol in weights.columns:
        weights[symbol] = close[symbol].notna().astype(float)
    return weights


def fixed_mix_weights(close: pd.DataFrame, symbols: Iterable[str]) -> pd.DataFrame:
    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    active_symbols = [symbol for symbol in symbols if symbol in close.columns]
    if not active_symbols:
        return weights
    valid = close[active_symbols].notna()
    counts = valid.sum(axis=1).replace(0, np.nan)
    for symbol in active_symbols:
        weights[symbol] = valid[symbol] / counts
    return weights.fillna(0.0)


def momentum_rotation_weights(
    close: pd.DataFrame,
    lookback: int,
    top_k: int,
    rebalance: str,
    market_filter: str,
    benchmark: str = "510300",
    risk_adjusted: bool = False,
) -> pd.DataFrame:
    returns = close.pct_change(fill_method=None)
    trailing_return = close / close.shift(lookback) - 1.0
    if risk_adjusted:
        min_periods = min(lookback, max(2, lookback // 2))
        trailing_vol = returns.rolling(lookback, min_periods=min_periods).std()
        score = trailing_return / trailing_vol.replace(0.0, np.nan)
    else:
        score = trailing_return

    can_invest = _cash_filter(close, benchmark=benchmark, mode=market_filter)
    rebalance_days = _rebalance_mask(close.index, rebalance)
    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    last_weight = pd.Series(0.0, index=close.columns)

    for date in close.index:
        if bool(rebalance_days.loc[date]):
            day_score = score.loc[date].dropna()
            day_score = day_score[day_score > 0.0]
            if not can_invest.loc[date] or day_score.empty:
                last_weight = pd.Series(0.0, index=close.columns)
            else:
                winners = list(day_score.sort_values(ascending=False).head(top_k).index)
                last_weight = pd.Series(0.0, index=close.columns)
                last_weight.loc[winners] = 1.0 / len(winners)
        weights.loc[date] = last_weight

    return weights.fillna(0.0)


def build_strategy_specs(config: Dict[str, object]) -> List[StrategySpec]:
    strategy_cfg = config["strategies"]
    benchmark_symbol = str(strategy_cfg.get("benchmark_symbol", strategy_cfg["dca_symbols"][0]))
    specs: List[StrategySpec] = []
    for symbol in strategy_cfg["dca_symbols"]:
        specs.append(StrategySpec(name=f"S0_dca_{symbol}", family="S0", params={"kind": "dca", "symbol": symbol}))

    fixed_mixes = strategy_cfg.get("fixed_mixes") or [list(strategy_cfg["dca_symbols"][:3])]
    for mix_symbols in fixed_mixes:
        symbols = list(mix_symbols)
        specs.append(StrategySpec(name=f"S0_mix_{'_'.join(symbols)}", family="S0", params={"kind": "fixed_mix", "symbols": symbols}))

    risk_adjusted_values = strategy_cfg.get("risk_adjusted", [False, True])
    for lookback in strategy_cfg["momentum_lookbacks"]:
        for top_k in strategy_cfg["momentum_top_k"]:
            for rebalance in strategy_cfg["rebalance"]:
                for market_filter in strategy_cfg["market_filters"]:
                    for risk_adjusted in risk_adjusted_values:
                        suffix = "ramom" if risk_adjusted else "mom"
                        name = f"S1_{suffix}_lb{lookback}_top{top_k}_{rebalance}_{market_filter}"
                        specs.append(
                            StrategySpec(
                                name=name,
                                family="S1",
                                params={
                                    "kind": "momentum",
                                    "lookback": lookback,
                                    "top_k": top_k,
                                    "rebalance": rebalance,
                                    "market_filter": market_filter,
                                    "risk_adjusted": risk_adjusted,
                                    "gross_exposure": 1.0,
                                    "benchmark": benchmark_symbol,
                                },
                            )
                        )

    levered_cfg = strategy_cfg.get("levered", {})
    if not levered_cfg.get("enabled", True):
        return specs

    levered_lookbacks = levered_cfg.get("lookbacks", [60])
    levered_top_k = levered_cfg.get("top_k", [2])
    levered_rebalance = levered_cfg.get("rebalance", ["monthly"])
    levered_filters = levered_cfg.get("market_filters", ["ma200"])
    levered_risk_adjusted = levered_cfg.get("risk_adjusted", [True])

    for leverage in strategy_cfg["gross_exposure"]:
        if float(leverage) <= 1.0:
            continue
        for financing_rate in strategy_cfg["financing_rate_annual"]:
            for lookback in levered_lookbacks:
                for top_k in levered_top_k:
                    for rebalance in levered_rebalance:
                        for market_filter in levered_filters:
                            for risk_adjusted in levered_risk_adjusted:
                                suffix = "ramom" if risk_adjusted else "mom"
                                specs.append(
                                    StrategySpec(
                                        name=(
                                            f"S7_{suffix}_lb{lookback}_top{top_k}_{rebalance}_"
                                            f"{market_filter}_x{leverage}_fr{financing_rate}"
                                        ),
                                        family="S7",
                                        params={
                                            "kind": "momentum",
                                            "lookback": int(lookback),
                                            "top_k": int(top_k),
                                            "rebalance": str(rebalance),
                                            "market_filter": str(market_filter),
                                            "risk_adjusted": bool(risk_adjusted),
                                            "benchmark": benchmark_symbol,
                                            "gross_exposure": float(leverage),
                                            "financing_rate_annual": float(financing_rate),
                                        },
                                    )
                                )

    return specs


def compute_weights(close: pd.DataFrame, spec: StrategySpec) -> pd.DataFrame:
    kind = spec.params["kind"]
    if kind == "dca":
        return buy_and_hold_weights(close, str(spec.params["symbol"]))
    if kind == "fixed_mix":
        return fixed_mix_weights(close, spec.params["symbols"])
    if kind == "momentum":
        return momentum_rotation_weights(
            close=close,
            lookback=int(spec.params["lookback"]),
            top_k=int(spec.params["top_k"]),
            rebalance=str(spec.params["rebalance"]),
            market_filter=str(spec.params["market_filter"]),
            benchmark=str(spec.params.get("benchmark", "510300")),
            risk_adjusted=bool(spec.params.get("risk_adjusted", False)),
        )
    raise ValueError(f"unsupported strategy kind: {kind}")

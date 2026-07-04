from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List

import numpy as np
import pandas as pd

from .realdata.schema import PANEL_COLUMNS


REQUIRED_REAL_STOCK_COLUMNS = set(PANEL_COLUMNS)
FREE_REAL_STOCK_COLUMNS = {
    "trade_date",
    "ts_code",
    "source_code",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "turnover_rate",
    "pct_chg",
    "pe_ttm",
    "pb",
    "ps_ttm",
    "pcf_ttm",
    "adj_close_for_signal",
    "trade_status",
    "is_suspended",
    "is_st",
    "list_date",
    "delist_date",
    "list_status",
    "listing_days",
    "board",
    "limit_pct",
    "up_limit",
    "down_limit",
    "circ_mv_approx",
    "data_tier",
}
REAL_STOCK_REQUIRED_TABLES = (
    "trade_cal",
    "stock_basic",
    "daily",
    "adj_factor",
    "daily_basic",
    "stk_limit",
    "suspend_d",
    "namechange",
)


@dataclass(frozen=True)
class RealStockStrategySpec:
    name: str
    family: str
    params: Dict[str, object]


def require_real_stock_panel(panel: pd.DataFrame) -> None:
    missing = sorted(REQUIRED_REAL_STOCK_COLUMNS - set(panel.columns))
    if missing and FREE_REAL_STOCK_COLUMNS.issubset(panel.columns):
        missing = []
    if missing:
        raise ValueError(f"stock_panel missing required real-stock columns: {missing}")
    duplicated = panel.duplicated(["trade_date", "ts_code"]).sum()
    if duplicated:
        raise ValueError(f"stock_panel has duplicated trade_date/ts_code rows: {int(duplicated)}")
    if panel.empty:
        raise ValueError("stock_panel is empty")


def _date_rebalance_mask(dates: pd.Series, frequency: str) -> pd.Series:
    index = pd.to_datetime(dates.astype(str), format="%Y%m%d", errors="coerce")
    if frequency == "daily":
        return pd.Series(True, index=dates.index)
    if frequency == "2d":
        return pd.Series(np.arange(len(dates)) % 2 == 0, index=dates.index)
    if frequency == "weekly":
        periods = index.dt.to_period("W-FRI")
        return periods != periods.shift(1)
    if frequency == "monthly":
        periods = index.dt.to_period("M")
        return periods != periods.shift(1)
    raise ValueError(f"unsupported real-stock rebalance frequency: {frequency}")


def _as_panel(panel: pd.DataFrame) -> pd.DataFrame:
    frame = panel.copy()
    if not REQUIRED_REAL_STOCK_COLUMNS.issubset(frame.columns) and FREE_REAL_STOCK_COLUMNS.issubset(frame.columns):
        frame["adj_factor"] = np.nan
        frame["total_mv"] = frame["circ_mv_approx"]
        frame["circ_mv"] = frame["circ_mv_approx"]
        frame["exchange"] = frame["ts_code"].astype(str).str.split(".").str[-1]
    require_real_stock_panel(frame)
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    numeric = [
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "amount",
        "adj_factor",
        "adj_close_for_signal",
        "turnover_rate",
        "total_mv",
        "circ_mv",
        "up_limit",
        "down_limit",
        "listing_days",
    ]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["is_suspended"] = frame["is_suspended"].fillna(False).astype(bool)
    frame["is_st"] = frame["is_st"].fillna(False).astype(bool)
    return frame


def add_real_stock_eligibility(
    panel: pd.DataFrame,
    min_listing_days: int = 120,
    min_price: float = 3.0,
    min_avg_amount_20d: float = 20_000_000.0,
    exclude_st: bool = True,
) -> pd.DataFrame:
    frame = _as_panel(panel)
    frame["avg_amount_20d"] = (
        frame.groupby("ts_code", sort=False)["amount"]
        .transform(lambda values: values.rolling(20, min_periods=5).mean())
        .astype(float)
    )
    frame["is_tradeable"] = (
        ~frame["is_suspended"]
        & frame["open"].notna()
        & frame["close"].notna()
        & frame["up_limit"].notna()
        & frame["down_limit"].notna()
    )
    eligible = (
        frame["is_tradeable"]
        & (frame["listing_days"] >= min_listing_days)
        & (frame["close"] >= min_price)
        & (frame["avg_amount_20d"] >= min_avg_amount_20d)
    )
    if exclude_st:
        eligible = eligible & ~frame["is_st"]
    frame["eligible"] = eligible.fillna(False)
    return frame


def _rank_desc_by_date(frame: pd.DataFrame, score_column: str) -> pd.Series:
    return frame.groupby("trade_date", sort=False)[score_column].rank(ascending=False, method="first")


def s2_real_stock_momentum_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 120)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    signal = frame["adj_close_for_signal"]
    frame["return_20"] = grouped["adj_close_for_signal"].pct_change(20)
    frame["return_60"] = grouped["adj_close_for_signal"].pct_change(60)
    frame["return_120"] = grouped["adj_close_for_signal"].pct_change(120)
    rolling_high_60 = grouped["adj_close_for_signal"].transform(lambda values: values.rolling(60, min_periods=20).max())
    frame["new_high_60"] = (signal >= rolling_high_60).astype(float)
    avg_volume_20 = grouped["volume"].transform(lambda values: values.rolling(20, min_periods=5).mean())
    frame["volume_breakout"] = frame["volume"] / avg_volume_20.replace(0.0, np.nan)
    daily_return = grouped["adj_close_for_signal"].pct_change()
    frame["volatility_20"] = daily_return.groupby(frame["ts_code"], sort=False).transform(lambda values: values.rolling(20, min_periods=5).std())
    frame["rank_score"] = (
        frame["return_20"].fillna(0.0) * 0.30
        + frame["return_60"].fillna(0.0) * 0.35
        + frame["return_120"].fillna(0.0) * 0.20
        + frame["new_high_60"].fillna(0.0) * 0.10
        + np.log1p(frame["volume_breakout"].clip(lower=0.0).fillna(0.0)) * 0.05
        - frame["volatility_20"].fillna(frame["volatility_20"].median()) * 0.25
    )
    frame.loc[~frame["eligible"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s3_real_stock_breakout_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    donchian = int(params.get("donchian", 55))
    exit_window = int(params.get("exit_donchian", 20))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 120)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    frame["donchian_high"] = grouped["adj_close_for_signal"].transform(lambda values: values.shift(1).rolling(donchian, min_periods=max(10, donchian // 2)).max())
    frame["donchian_low_exit"] = grouped["adj_close_for_signal"].transform(lambda values: values.shift(1).rolling(exit_window, min_periods=max(5, exit_window // 2)).min())
    frame["ma20"] = grouped["adj_close_for_signal"].transform(lambda values: values.rolling(20, min_periods=10).mean())
    frame["ma60"] = grouped["adj_close_for_signal"].transform(lambda values: values.rolling(60, min_periods=20).mean())
    avg_volume_20 = grouped["volume"].transform(lambda values: values.rolling(20, min_periods=5).mean())
    frame["volume_breakout"] = frame["volume"] / avg_volume_20.replace(0.0, np.nan)
    true_range = (frame["high"] - frame["low"]).abs()
    frame["atr20"] = true_range.groupby(frame["ts_code"], sort=False).transform(lambda values: values.rolling(20, min_periods=5).mean())
    frame["breakout_signal"] = (
        frame["eligible"]
        & (frame["adj_close_for_signal"] > frame["donchian_high"])
        & (frame["ma20"] > frame["ma60"])
        & (frame["volume_breakout"] > float(params.get("min_volume_breakout", 1.2)))
    )
    frame["rank_score"] = (
        (frame["adj_close_for_signal"] / frame["donchian_high"].replace(0.0, np.nan) - 1.0).fillna(0.0)
        + np.log1p(frame["volume_breakout"].clip(lower=0.0).fillna(0.0)) * 0.10
        - (frame["atr20"] / frame["close"]).replace([np.inf, -np.inf], np.nan).fillna(0.0) * 0.20
    )
    frame.loc[~frame["breakout_signal"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s4_real_smallcap_factor_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 10_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    frame["return_60"] = grouped["adj_close_for_signal"].pct_change(60)
    daily_return = grouped["adj_close_for_signal"].pct_change()
    frame["volatility_60"] = daily_return.groupby(frame["ts_code"], sort=False).transform(lambda values: values.rolling(60, min_periods=20).std())
    frame["turnover_20"] = grouped["turnover_rate"].transform(lambda values: values.rolling(20, min_periods=5).mean())
    size_score = -np.log(frame["circ_mv"].replace(0.0, np.nan))
    turnover_mode = str(params.get("factor_mode", "low_turnover"))
    if turnover_mode == "high_turnover_breakout":
        turnover_score = np.log1p(frame["turnover_20"].clip(lower=0.0))
    else:
        turnover_score = -np.log1p(frame["turnover_20"].clip(lower=0.0))
    frame["rank_score"] = (
        size_score.fillna(size_score.median()) * 0.45
        + frame["return_60"].fillna(0.0) * 0.25
        + turnover_score.fillna(turnover_score.median()) * 0.15
        - frame["volatility_60"].fillna(frame["volatility_60"].median()) * 0.15
    )
    frame.loc[~frame["eligible"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def build_real_stock_strategy_specs(config: Dict[str, object] | None = None) -> List[RealStockStrategySpec]:
    raw = config or {}
    cfg = raw.get("phase2_real_stock_strategies", {}) if isinstance(raw.get("phase2_real_stock_strategies", {}), dict) else {}
    specs: List[RealStockStrategySpec] = []

    s2 = cfg.get("S2_real_stock_momentum", {}) if isinstance(cfg.get("S2_real_stock_momentum", {}), dict) else {}
    for holding_k in s2.get("holding_k", [1, 2, 3, 5, 8, 10]):
        for rebalance in s2.get("rebalance", ["daily", "2d", "weekly"]):
            specs.append(
                RealStockStrategySpec(
                    name=f"S2_real_stock_momentum_k{holding_k}_{rebalance}",
                    family="S2_real_stock_momentum",
                    params={
                        "kind": "real_stock_momentum",
                        "holding_k": int(holding_k),
                        "rebalance": str(rebalance),
                        "min_listing_days": int(s2.get("min_listing_days", 120)),
                        "min_price": float(s2.get("min_price", 3.0)),
                        "min_avg_amount_20d": float(s2.get("min_avg_amount_20d", 20_000_000.0)),
                        "stop_loss": float(s2.get("stop_loss", 0.12)),
                        "trailing_stop": float(s2.get("trailing_stop", 0.18)),
                    },
                )
            )

    s3 = cfg.get("S3_real_stock_breakout", {}) if isinstance(cfg.get("S3_real_stock_breakout", {}), dict) else {}
    for donchian in s3.get("donchian", [20, 55]):
        for risk_pct in s3.get("risk_per_trade", [0.005, 0.01, 0.015, 0.02]):
            specs.append(
                RealStockStrategySpec(
                    name=f"S3_real_stock_breakout_d{donchian}_r{risk_pct}",
                    family="S3_real_stock_breakout",
                    params={
                        "kind": "real_stock_breakout",
                        "donchian": int(donchian),
                        "exit_donchian": int(s3.get("exit_donchian", 20)),
                        "risk_per_trade": float(risk_pct),
                        "atr_stop": float(s3.get("atr_stop", 2.0)),
                        "max_holding_days": int(s3.get("max_holding_days", 60)),
                        "min_volume_breakout": float(s3.get("min_volume_breakout", 1.2)),
                    },
                )
            )

    s4 = cfg.get("S4_real_smallcap_factor", {}) if isinstance(cfg.get("S4_real_smallcap_factor", {}), dict) else {}
    for factor_mode in s4.get("factor_modes", ["low_turnover", "high_turnover_breakout"]):
        for rebalance in s4.get("rebalance", ["weekly", "monthly"]):
            for holding_k in s4.get("holding_k", [10, 20, 30, 50]):
                specs.append(
                    RealStockStrategySpec(
                        name=f"S4_real_smallcap_factor_{factor_mode}_k{holding_k}_{rebalance}",
                        family="S4_real_smallcap_factor",
                        params={
                            "kind": "real_smallcap_factor",
                            "factor_mode": str(factor_mode),
                            "holding_k": int(holding_k),
                            "rebalance": str(rebalance),
                            "min_listing_days": int(s4.get("min_listing_days", 180)),
                            "min_price": float(s4.get("min_price", 3.0)),
                            "min_avg_amount_20d": float(s4.get("min_avg_amount_20d", 10_000_000.0)),
                        },
                    )
                )
    return specs


def compute_real_stock_scores(panel: pd.DataFrame, spec: RealStockStrategySpec) -> pd.DataFrame:
    kind = str(spec.params.get("kind", ""))
    if kind == "real_stock_momentum":
        return s2_real_stock_momentum_scores(panel, spec)
    if kind == "real_stock_breakout":
        return s3_real_stock_breakout_scores(panel, spec)
    if kind == "real_smallcap_factor":
        return s4_real_smallcap_factor_scores(panel, spec)
    raise ValueError(f"unsupported real stock strategy kind: {kind}")


def strategy_rebalance_dates(panel: pd.DataFrame, frequency: str) -> list[str]:
    require_real_stock_panel(panel)
    dates = pd.Series(sorted(panel["trade_date"].astype(str).unique()))
    mask = _date_rebalance_mask(dates, frequency)
    return dates.loc[mask].tolist()


def top_ranked_symbols(scores: pd.DataFrame, trade_date: str, holding_k: int) -> list[str]:
    day = scores.loc[scores["trade_date"].astype(str) == str(trade_date)].copy()
    day = day.loc[day["rank_score"].notna()].sort_values(["rank_score", "ts_code"], ascending=[False, True])
    return day["ts_code"].head(int(holding_k)).astype(str).tolist()


def required_tables_for_real_stock_strategies() -> tuple[str, ...]:
    return REAL_STOCK_REQUIRED_TABLES

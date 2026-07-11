from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

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
    "corporate_action_share_factor",
    "corporate_action_source",
    "trade_status",
    "is_suspended",
    "is_st",
    "list_date",
    "delist_date",
    "list_status",
    "is_last_observation",
    "delisting_exit_required",
    "terminal_value_source",
    "listing_days",
    "board",
    "limit_pct",
    "up_limit",
    "down_limit",
    "circ_mv_approx",
    "data_tier",
}
FREE_REAL_ANALYSIS_COLUMNS = {
    "trade_date",
    "ts_code",
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
    "corporate_action_share_factor",
    "trade_status",
    "is_suspended",
    "is_st",
    "delisting_exit_required",
    "listing_days",
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
COMMON_REAL_FEATURE_COLUMNS = {
    "signal_return",
    "signal_price",
    "avg_amount_20d",
    "is_tradeable",
}


@dataclass(frozen=True)
class RealStockStrategySpec:
    name: str
    family: str
    params: Dict[str, object]


def require_real_stock_panel(panel: pd.DataFrame) -> None:
    missing = sorted(REQUIRED_REAL_STOCK_COLUMNS - set(panel.columns))
    if missing and (
        FREE_REAL_STOCK_COLUMNS.issubset(panel.columns)
        or FREE_REAL_ANALYSIS_COLUMNS.issubset(panel.columns)
    ):
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
    frame = panel.copy(deep=False)
    if "listing_trading_days" not in frame.columns and "listing_days" in frame.columns:
        frame["listing_trading_days"] = frame["listing_days"]
    if not REQUIRED_REAL_STOCK_COLUMNS.issubset(frame.columns) and (
        FREE_REAL_STOCK_COLUMNS.issubset(frame.columns)
        or FREE_REAL_ANALYSIS_COLUMNS.issubset(frame.columns)
    ):
        frame["circ_mv"] = frame["circ_mv_approx"]
    require_real_stock_panel(frame)
    if not isinstance(frame["trade_date"].dtype, pd.StringDtype):
        frame["trade_date"] = frame["trade_date"].astype("string[pyarrow]")
    if not isinstance(frame["ts_code"].dtype, pd.StringDtype):
        frame["ts_code"] = frame["ts_code"].astype("string[pyarrow]")
    previous_symbol = frame["ts_code"].shift(1)
    previous_date = frame["trade_date"].shift(1)
    sorted_rows = (
        previous_symbol.isna()
        | frame["ts_code"].gt(previous_symbol)
        | (frame["ts_code"].eq(previous_symbol) & frame["trade_date"].ge(previous_date))
    )
    if not bool(sorted_rows.all()):
        frame = frame.sort_values(["ts_code", "trade_date"], kind="mergesort").reset_index(drop=True)
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
        "corporate_action_share_factor",
        "trade_status",
        "turnover_rate",
        "pct_chg",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "pcf_ttm",
        "total_mv",
        "circ_mv",
        "up_limit",
        "down_limit",
        "listing_days",
        "listing_trading_days",
    ]
    for column in numeric:
        if column in frame.columns and not pd.api.types.is_numeric_dtype(frame[column]):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ["is_suspended", "is_st", "delisting_exit_required"]:
        if not pd.api.types.is_bool_dtype(frame[column]) or frame[column].isna().any():
            frame[column] = frame[column].fillna(False).astype(bool)
    frame.attrs["quant_proof_panel_validated"] = True
    return frame


def load_free_real_analysis_panel(
    path: str | Path,
    start_date: str = "",
    end_date: str = "",
) -> pd.DataFrame:
    panel_path = Path(path)
    available = set(pq.ParquetFile(panel_path).schema.names)
    missing = sorted(FREE_REAL_ANALYSIS_COLUMNS - available)
    if missing:
        raise ValueError(f"free-real analysis panel missing columns: {missing}")
    columns = sorted(FREE_REAL_ANALYSIS_COLUMNS | ({"listing_trading_days"} & available))
    filters = []
    if start_date:
        filters.append(("trade_date", ">=", str(start_date).replace("-", "")))
    if end_date:
        filters.append(("trade_date", "<=", str(end_date).replace("-", "")))
    frame = pd.read_parquet(panel_path, columns=columns, filters=filters or None)
    frame["trade_date"] = frame["trade_date"].astype("string[pyarrow]")
    frame["ts_code"] = frame["ts_code"].astype("string[pyarrow]")
    frame["data_tier"] = frame["data_tier"].astype("string[pyarrow]")
    return frame


def prepare_real_stock_features(panel: pd.DataFrame) -> pd.DataFrame:
    frame = _as_panel(panel)
    if COMMON_REAL_FEATURE_COLUMNS.issubset(frame.columns):
        return frame
    fallback_return = frame.groupby("ts_code", sort=False)["adj_close_for_signal"].pct_change()
    frame["signal_return"] = (frame["pct_chg"] / 100.0).where(frame["pct_chg"].notna(), fallback_return)
    frame["signal_price"] = (1.0 + frame["signal_return"].fillna(0.0)).groupby(
        frame["ts_code"], sort=False
    ).cumprod()
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
    frame.attrs["quant_proof_panel_validated"] = True
    return frame


def add_real_stock_eligibility(
    panel: pd.DataFrame,
    min_listing_days: int = 120,
    min_price: float = 3.0,
    min_avg_amount_20d: float = 20_000_000.0,
    exclude_st: bool = True,
) -> pd.DataFrame:
    frame = (
        panel.copy(deep=False)
        if COMMON_REAL_FEATURE_COLUMNS.issubset(panel.columns)
        else prepare_real_stock_features(panel)
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


def _percentile_by_date(frame: pd.DataFrame, values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.groupby(frame["trade_date"], sort=False).rank(
        ascending=higher_is_better,
        method="average",
        pct=True,
    )


def _fill_cross_sectional_median(frame: pd.DataFrame, values: pd.Series) -> pd.Series:
    medians = values.groupby(frame["trade_date"], sort=False).transform("median")
    return values.fillna(medians)


def _stock_returns(frame: pd.DataFrame) -> pd.Series:
    if "signal_return" in frame.columns:
        return frame["signal_return"].where(~frame["is_suspended"])
    return frame.groupby("ts_code", sort=False)["adj_close_for_signal"].pct_change().where(~frame["is_suspended"])


def _market_proxy(frame: pd.DataFrame, daily_returns: pd.Series) -> tuple[pd.Series, pd.Series]:
    eligible_returns = daily_returns.where(frame["eligible"]).clip(lower=-0.20, upper=0.20)
    market_return = eligible_returns.groupby(frame["trade_date"], sort=True).median().fillna(0.0)
    market_level = (1.0 + market_return).cumprod()
    return market_return, market_level


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
        - _fill_cross_sectional_median(frame, frame["volatility_20"]) * 0.25
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
    frame["donchian_high"] = grouped["signal_price"].transform(lambda values: values.shift(1).rolling(donchian, min_periods=max(10, donchian // 2)).max())
    frame["donchian_low_exit"] = grouped["signal_price"].transform(lambda values: values.shift(1).rolling(exit_window, min_periods=max(5, exit_window // 2)).min())
    frame["ma20"] = grouped["signal_price"].transform(lambda values: values.rolling(20, min_periods=10).mean())
    frame["ma60"] = grouped["signal_price"].transform(lambda values: values.rolling(60, min_periods=20).mean())
    avg_volume_20 = grouped["volume"].transform(lambda values: values.rolling(20, min_periods=5).mean())
    frame["volume_breakout"] = frame["volume"] / avg_volume_20.replace(0.0, np.nan)
    true_range = (frame["high"] - frame["low"]).abs()
    frame["atr20"] = true_range.groupby(frame["ts_code"], sort=False).transform(lambda values: values.rolling(20, min_periods=5).mean())
    frame["breakout_signal"] = (
        frame["eligible"]
        & (frame["signal_price"] > frame["donchian_high"])
        & (frame["ma20"] > frame["ma60"])
        & (frame["volume_breakout"] > float(params.get("min_volume_breakout", 1.2)))
    )
    frame["rank_score"] = (
        (frame["signal_price"] / frame["donchian_high"].replace(0.0, np.nan) - 1.0).fillna(0.0)
        + np.log1p(frame["volume_breakout"].clip(lower=0.0).fillna(0.0)) * 0.10
        - (frame["atr20"] / frame["close"]).replace([np.inf, -np.inf], np.nan).fillna(0.0) * 0.20
    )
    frame.loc[~frame["breakout_signal"], "rank_score"] = np.nan
    frame["entry_signal"] = frame["breakout_signal"]
    delisting_exit = frame.get("delisting_exit_required", False)
    if not isinstance(delisting_exit, pd.Series):
        delisting_exit = pd.Series(bool(delisting_exit), index=frame.index)
    frame["exit_signal"] = (
        frame["signal_price"].lt(frame["donchian_low_exit"])
        | frame["is_st"]
        | delisting_exit.fillna(False).astype(bool)
    )
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
        _fill_cross_sectional_median(frame, size_score) * 0.45
        + frame["return_60"].fillna(0.0) * 0.25
        + _fill_cross_sectional_median(frame, turnover_score) * 0.15
        - _fill_cross_sectional_median(frame, frame["volatility_60"]) * 0.15
    )
    frame.loc[~frame["eligible"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s10_real_regime_selector_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    market_ma = int(params.get("market_ma", 120))
    breadth_ma = int(params.get("breadth_ma", 60))
    breadth_threshold = float(params.get("breadth_threshold", 0.50))
    ranking_mode = str(params.get("ranking_mode", "smallcap_momentum"))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    daily_return = _stock_returns(frame)
    frame["return_60"] = grouped["signal_price"].pct_change(60)
    frame["volatility_20"] = daily_return.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(20, min_periods=10).std()
    )
    stock_ma = grouped["signal_price"].transform(
        lambda values: values.rolling(breadth_ma, min_periods=max(10, breadth_ma // 2)).mean()
    )
    above_ma = (frame["signal_price"] > stock_ma).where(frame["eligible"])
    breadth = above_ma.groupby(frame["trade_date"], sort=True).mean()
    _, market_level = _market_proxy(frame, daily_return)
    market_average = market_level.rolling(market_ma, min_periods=max(20, market_ma // 2)).mean()
    frame["market_risk_on"] = (
        frame["trade_date"].map(market_level).gt(frame["trade_date"].map(market_average))
        & frame["trade_date"].map(breadth).ge(breadth_threshold)
    )
    momentum_score = _percentile_by_date(frame, frame["return_60"], higher_is_better=True)
    low_vol_score = _percentile_by_date(frame, frame["volatility_20"], higher_is_better=False)
    size_score = _percentile_by_date(frame, frame["circ_mv"], higher_is_better=False)
    if ranking_mode == "low_vol_momentum":
        frame["rank_score"] = momentum_score * 0.45 + low_vol_score * 0.45 + size_score * 0.10
    else:
        frame["rank_score"] = momentum_score * 0.40 + low_vol_score * 0.20 + size_score * 0.40
    frame.loc[~frame["eligible"] | ~frame["market_risk_on"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s11_real_short_term_reversal_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    reversal_days = int(params.get("reversal_days", 5))
    trend_days = int(params.get("trend_days", 120))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    daily_return = _stock_returns(frame)
    frame["reversal_return"] = grouped["signal_price"].pct_change(reversal_days)
    trend_ma = grouped["signal_price"].transform(
        lambda values: values.rolling(trend_days, min_periods=max(20, trend_days // 2)).mean()
    )
    frame["volatility_20"] = daily_return.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(20, min_periods=10).std()
    )
    reversal_score = _percentile_by_date(frame, frame["reversal_return"], higher_is_better=False)
    low_vol_score = _percentile_by_date(frame, frame["volatility_20"], higher_is_better=False)
    frame["rank_score"] = reversal_score * 0.80 + low_vol_score * 0.20
    valid = (
        frame["eligible"]
        & (frame["signal_price"] > trend_ma)
        & frame["reversal_return"].between(float(params.get("min_return", -0.25)), 0.0, inclusive="left")
    )
    frame.loc[~valid, "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s12_real_low_volatility_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    volatility_days = int(params.get("volatility_days", 60))
    momentum_days = int(params.get("momentum_days", 120))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    daily_return = _stock_returns(frame)
    frame["momentum"] = grouped["signal_price"].pct_change(momentum_days)
    frame["volatility"] = daily_return.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(volatility_days, min_periods=max(10, volatility_days // 2)).std()
    )
    frame["turnover_20"] = grouped["turnover_rate"].transform(
        lambda values: values.rolling(20, min_periods=10).mean()
    )
    low_vol_score = _percentile_by_date(frame, frame["volatility"], higher_is_better=False)
    momentum_score = _percentile_by_date(frame, frame["momentum"], higher_is_better=True)
    low_turnover_score = _percentile_by_date(frame, frame["turnover_20"], higher_is_better=False)
    frame["rank_score"] = low_vol_score * 0.55 + momentum_score * 0.35 + low_turnover_score * 0.10
    valid = frame["eligible"] & frame["momentum"].gt(float(params.get("min_momentum", 0.0)))
    frame.loc[~valid, "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s13_real_residual_momentum_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    lookback = int(params.get("lookback", 60))
    skip_days = int(params.get("skip_days", 5))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    daily_return = _stock_returns(frame).clip(lower=-0.20, upper=0.20)
    market_return, _ = _market_proxy(frame, daily_return)
    residual = daily_return - frame["trade_date"].map(market_return)
    lagged_residual = residual.groupby(frame["ts_code"], sort=False).shift(skip_days)
    frame["residual_momentum"] = lagged_residual.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(lookback, min_periods=max(10, lookback // 2)).sum()
    )
    frame["residual_volatility"] = lagged_residual.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(lookback, min_periods=max(10, lookback // 2)).std()
    )
    risk_adjusted = frame["residual_momentum"] / (
        frame["residual_volatility"].replace(0.0, np.nan) * np.sqrt(float(lookback))
    )
    frame["rank_score"] = _percentile_by_date(frame, risk_adjusted, higher_is_better=True)
    frame.loc[~frame["eligible"] | frame["residual_momentum"].le(0.0), "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s14_real_volume_price_shock_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    lookback = int(params.get("lookback", 5))
    mode = str(params.get("mode", "continuation"))
    min_amount_ratio = float(params.get("min_amount_ratio", 1.5))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    previous_amount = grouped["amount"].transform(lambda values: values.shift(1).rolling(20, min_periods=10).mean())
    frame["amount_ratio"] = frame["amount"] / previous_amount.replace(0.0, np.nan)
    frame["shock_return"] = grouped["signal_price"].pct_change(lookback)
    daily_return = _stock_returns(frame)
    frame["volatility_20"] = daily_return.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(20, min_periods=10).std()
    )
    amount_score = _percentile_by_date(frame, np.log1p(frame["amount_ratio"].clip(lower=0.0)), True)
    return_score = _percentile_by_date(
        frame,
        frame["shock_return"] if mode == "continuation" else -frame["shock_return"],
        True,
    )
    low_vol_score = _percentile_by_date(frame, frame["volatility_20"], False)
    frame["rank_score"] = amount_score * 0.45 + return_score * 0.45 + low_vol_score * 0.10
    direction = frame["shock_return"].gt(0.0) if mode == "continuation" else frame["shock_return"].lt(0.0)
    valid = frame["eligible"] & direction & frame["amount_ratio"].ge(min_amount_ratio)
    frame.loc[~valid, "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s20_real_stateful_trend_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    entry_window = int(params.get("entry_window", 55))
    exit_window = int(params.get("exit_window", 20))
    trend_window = int(params.get("trend_window", 120))
    momentum_window = int(params.get("momentum_window", 60))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    frame["entry_high"] = grouped["signal_price"].transform(
        lambda values: values.shift(1).rolling(entry_window, min_periods=max(10, entry_window // 2)).max()
    )
    frame["exit_low"] = grouped["signal_price"].transform(
        lambda values: values.shift(1).rolling(exit_window, min_periods=max(5, exit_window // 2)).min()
    )
    frame["trend_average"] = grouped["signal_price"].transform(
        lambda values: values.rolling(trend_window, min_periods=max(20, trend_window // 2)).mean()
    )
    frame["trend_momentum"] = grouped["signal_price"].pct_change(momentum_window)
    daily_return = _stock_returns(frame)
    frame["trend_volatility"] = daily_return.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(20, min_periods=10).std()
    )
    momentum_score = _percentile_by_date(frame, frame["trend_momentum"], True)
    low_vol_score = _percentile_by_date(frame, frame["trend_volatility"], False)
    liquidity_score = _percentile_by_date(frame, frame["avg_amount_20d"], True)
    frame["rank_score"] = momentum_score * 0.65 + low_vol_score * 0.25 + liquidity_score * 0.10
    frame["entry_signal"] = (
        frame["eligible"]
        & frame["signal_price"].gt(frame["entry_high"])
        & frame["signal_price"].gt(frame["trend_average"])
        & frame["trend_momentum"].gt(float(params.get("min_momentum", 0.05)))
    )
    delisting_exit = frame.get("delisting_exit_required", False)
    if not isinstance(delisting_exit, pd.Series):
        delisting_exit = pd.Series(bool(delisting_exit), index=frame.index)
    frame["exit_signal"] = (
        frame["signal_price"].lt(frame["exit_low"])
        | frame["is_st"]
        | delisting_exit.fillna(False).astype(bool)
    )
    frame.loc[~frame["entry_signal"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s21_real_volatility_contraction_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    breakout_window = int(params.get("breakout_window", 20))
    short_vol_window = int(params.get("short_vol_window", 10))
    long_vol_window = int(params.get("long_vol_window", 60))
    exit_window = int(params.get("exit_window", 10))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    daily_return = _stock_returns(frame)
    frame["breakout_high"] = grouped["signal_price"].transform(
        lambda values: values.shift(1).rolling(breakout_window, min_periods=max(10, breakout_window // 2)).max()
    )
    frame["exit_low"] = grouped["signal_price"].transform(
        lambda values: values.shift(1).rolling(exit_window, min_periods=max(5, exit_window // 2)).min()
    )
    frame["short_volatility"] = daily_return.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(short_vol_window, min_periods=max(5, short_vol_window // 2)).std()
    )
    frame["long_volatility"] = daily_return.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(long_vol_window, min_periods=max(20, long_vol_window // 2)).std()
    )
    frame["contraction_ratio"] = frame["short_volatility"] / frame["long_volatility"].replace(0.0, np.nan)
    prior_amount = grouped["amount"].transform(lambda values: values.shift(1).rolling(20, min_periods=10).mean())
    frame["breakout_amount_ratio"] = frame["amount"] / prior_amount.replace(0.0, np.nan)
    breakout_strength = frame["signal_price"] / frame["breakout_high"].replace(0.0, np.nan) - 1.0
    contraction_score = _percentile_by_date(frame, frame["contraction_ratio"], False)
    breakout_score = _percentile_by_date(frame, breakout_strength, True)
    amount_score = _percentile_by_date(frame, frame["breakout_amount_ratio"], True)
    frame["rank_score"] = contraction_score * 0.45 + breakout_score * 0.35 + amount_score * 0.20
    frame["entry_signal"] = (
        frame["eligible"]
        & frame["signal_price"].gt(frame["breakout_high"])
        & frame["contraction_ratio"].le(float(params.get("max_contraction_ratio", 0.65)))
        & frame["breakout_amount_ratio"].ge(float(params.get("min_amount_ratio", 1.0)))
    )
    delisting_exit = frame.get("delisting_exit_required", False)
    if not isinstance(delisting_exit, pd.Series):
        delisting_exit = pd.Series(bool(delisting_exit), index=frame.index)
    frame["exit_signal"] = (
        frame["signal_price"].lt(frame["exit_low"])
        | frame["is_st"]
        | delisting_exit.fillna(False).astype(bool)
    )
    frame.loc[~frame["entry_signal"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s24_real_regime_contraction_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    frame = s21_real_volatility_contraction_scores(panel, spec)
    market_ma = int(params.get("market_ma", 120))
    breadth_ma = int(params.get("breadth_ma", 60))
    breadth_threshold = float(params.get("breadth_threshold", 0.50))
    grouped = frame.groupby("ts_code", sort=False)
    daily_return = _stock_returns(frame)
    stock_average = grouped["signal_price"].transform(
        lambda values: values.rolling(breadth_ma, min_periods=max(10, breadth_ma // 2)).mean()
    )
    above_average = frame["signal_price"].gt(stock_average).where(frame["eligible"])
    breadth = above_average.groupby(frame["trade_date"], sort=True).mean()
    _, market_level = _market_proxy(frame, daily_return)
    market_average = market_level.rolling(market_ma, min_periods=max(20, market_ma // 2)).mean()
    frame["market_risk_on"] = (
        frame["trade_date"].map(market_level).gt(frame["trade_date"].map(market_average))
        & frame["trade_date"].map(breadth).ge(breadth_threshold)
    ).fillna(False)
    frame["entry_signal"] &= frame["market_risk_on"]
    frame["exit_signal"] |= ~frame["market_risk_on"]
    frame.loc[~frame["entry_signal"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s26_real_gap_intraday_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    mode = str(params.get("mode", "up_continuation"))
    min_gap = float(params.get("min_gap", 0.02))
    min_intraday = float(params.get("min_intraday", 0.01))
    min_amount_ratio = float(params.get("min_amount_ratio", 1.0))
    exit_window = int(params.get("exit_window", 5))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    pre_close = frame["pre_close"].replace(0.0, np.nan)
    open_price = frame["open"].replace(0.0, np.nan)
    frame["gap_return"] = open_price / pre_close - 1.0
    frame["intraday_return"] = frame["close"] / open_price - 1.0
    prior_amount = grouped["amount"].transform(
        lambda values: values.shift(1).rolling(20, min_periods=10).mean()
    )
    frame["gap_amount_ratio"] = frame["amount"] / prior_amount.replace(0.0, np.nan)
    frame["gap_exit_average"] = grouped["signal_price"].transform(
        lambda values: values.rolling(exit_window, min_periods=max(2, exit_window // 2)).mean()
    )

    if mode == "up_continuation":
        direction = frame["gap_return"].ge(min_gap) & frame["intraday_return"].ge(min_intraday)
        gap_component = frame["gap_return"]
        intraday_component = frame["intraday_return"]
    elif mode == "down_reversal":
        direction = frame["gap_return"].le(-min_gap) & frame["intraday_return"].ge(min_intraday)
        gap_component = -frame["gap_return"]
        intraday_component = frame["intraday_return"]
    elif mode == "up_exhaustion_reversal":
        direction = frame["gap_return"].ge(min_gap) & frame["intraday_return"].le(-min_intraday)
        gap_component = frame["gap_return"]
        intraday_component = -frame["intraday_return"]
    else:
        raise ValueError(f"unsupported real_gap_intraday mode: {mode}")

    gap_score = _percentile_by_date(frame, gap_component, True)
    intraday_score = _percentile_by_date(frame, intraday_component, True)
    amount_score = _percentile_by_date(frame, frame["gap_amount_ratio"], True)
    frame["rank_score"] = gap_score * 0.40 + intraday_score * 0.40 + amount_score * 0.20
    frame["entry_signal"] = (
        frame["eligible"]
        & direction
        & frame["gap_amount_ratio"].ge(min_amount_ratio)
    )
    delisting_exit = frame.get("delisting_exit_required", False)
    if not isinstance(delisting_exit, pd.Series):
        delisting_exit = pd.Series(bool(delisting_exit), index=frame.index)
    frame["exit_signal"] = (
        frame["signal_price"].lt(frame["gap_exit_average"])
        | frame["is_st"]
        | delisting_exit.fillna(False).astype(bool)
    )
    frame.loc[~frame["entry_signal"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s27_real_momentum_acceleration_scores(
    panel: pd.DataFrame,
    spec: RealStockStrategySpec | None = None,
) -> pd.DataFrame:
    params = spec.params if spec else {}
    short_window = int(params.get("short_window", 5))
    medium_window = int(params.get("medium_window", 20))
    rank_change_window = int(params.get("rank_change_window", 5))
    trend_window = int(params.get("trend_window", 60))
    exit_window = int(params.get("exit_window", 10))
    entry_percentile = float(params.get("entry_percentile", 0.90))
    exit_percentile = float(params.get("exit_percentile", 0.60))
    if min(short_window, medium_window, rank_change_window, trend_window, exit_window) <= 0:
        raise ValueError("momentum-acceleration windows must be positive")
    if short_window >= medium_window:
        raise ValueError("short_window must be smaller than medium_window")
    if not 0.0 <= exit_percentile < entry_percentile <= 1.0:
        raise ValueError("momentum-acceleration percentiles require 0 <= exit < entry <= 1")

    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    frame["accel_return_short"] = grouped["signal_price"].pct_change(short_window)
    frame["accel_return_medium"] = grouped["signal_price"].pct_change(medium_window)
    frame["acceleration"] = (
        frame["accel_return_short"]
        - frame["accel_return_medium"] * (short_window / float(medium_window))
    )
    frame["accel_trend_return"] = grouped["signal_price"].pct_change(trend_window)
    frame["momentum_percentile"] = _percentile_by_date(
        frame,
        frame["accel_return_medium"],
        higher_is_better=True,
    )
    frame["momentum_rank_improvement"] = frame["momentum_percentile"] - frame.groupby(
        "ts_code", sort=False
    )["momentum_percentile"].shift(rank_change_window)
    acceleration_score = _percentile_by_date(frame, frame["acceleration"], higher_is_better=True)
    improvement_score = _percentile_by_date(
        frame,
        frame["momentum_rank_improvement"],
        higher_is_better=True,
    )
    frame["rank_score"] = (
        acceleration_score * 0.45
        + improvement_score * 0.35
        + frame["momentum_percentile"] * 0.20
    )
    frame["accel_exit_average"] = grouped["signal_price"].transform(
        lambda values: values.rolling(exit_window, min_periods=max(2, exit_window // 2)).mean()
    )
    frame["entry_signal"] = (
        frame["eligible"]
        & frame["acceleration"].gt(0.0)
        & frame["momentum_rank_improvement"].gt(0.0)
        & frame["momentum_percentile"].ge(entry_percentile)
        & frame["accel_trend_return"].gt(0.0)
    )
    delisting_exit = frame.get("delisting_exit_required", False)
    if not isinstance(delisting_exit, pd.Series):
        delisting_exit = pd.Series(bool(delisting_exit), index=frame.index)
    frame["exit_signal"] = (
        frame["acceleration"].lt(0.0)
        | frame["momentum_percentile"].lt(exit_percentile)
        | frame["signal_price"].lt(frame["accel_exit_average"])
        | frame["is_st"]
        | delisting_exit.fillna(False).astype(bool)
    )
    frame.loc[~frame["entry_signal"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s28_real_signed_flow_accumulation_scores(
    panel: pd.DataFrame,
    spec: RealStockStrategySpec | None = None,
) -> pd.DataFrame:
    params = spec.params if spec else {}
    flow_window = int(params.get("flow_window", 20))
    entry_percentile = float(params.get("entry_percentile", 0.90))
    max_abs_return_20 = float(params.get("max_abs_return_20", 0.20))
    if flow_window <= 0:
        raise ValueError("signed-flow window must be positive")
    if not 0.0 <= entry_percentile <= 1.0:
        raise ValueError("signed-flow entry_percentile must be between 0 and 1")
    if not np.isfinite(max_abs_return_20) or max_abs_return_20 < 0.0:
        raise ValueError("signed-flow max_abs_return_20 must be non-negative")

    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    price_range = frame["high"] - frame["low"]
    finite_ohlc = (
        np.isfinite(frame["close"])
        & np.isfinite(frame["high"])
        & np.isfinite(frame["low"])
    )
    positive_range = finite_ohlc & price_range.gt(0.0)
    clv = pd.Series(np.nan, index=frame.index, dtype=float)
    clv.loc[price_range.notna() & price_range.le(0.0)] = 0.0
    clv.loc[positive_range] = (
        (2.0 * frame.loc[positive_range, "close"])
        - frame.loc[positive_range, "high"]
        - frame.loc[positive_range, "low"]
    ) / price_range.loc[positive_range]
    frame["clv"] = clv

    turnover = pd.to_numeric(frame["turnover_rate"], errors="coerce").astype(float)
    turnover = turnover.where(np.isfinite(turnover) & turnover.ge(0.0))
    weighted_flow = frame["clv"] * turnover
    frame["flow_turnover_denominator"] = turnover.groupby(
        frame["ts_code"], sort=False
    ).transform(
        lambda values: values.rolling(flow_window, min_periods=flow_window).sum()
    )
    flow_numerator = weighted_flow.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(flow_window, min_periods=flow_window).sum()
    )
    valid_denominator = (
        np.isfinite(frame["flow_turnover_denominator"])
        & frame["flow_turnover_denominator"].gt(0.0)
    )
    frame["signed_flow"] = (
        flow_numerator / frame["flow_turnover_denominator"].where(valid_denominator)
    )
    frame["total_return_20"] = frame.groupby("ts_code", sort=False)[
        "signal_price"
    ].pct_change(20, fill_method=None)
    frame["flow_percentile"] = _percentile_by_date(
        frame,
        frame["signed_flow"].where(frame["eligible"]),
        higher_is_better=True,
    )
    valid = (
        frame["eligible"]
        & valid_denominator
        & np.isfinite(frame["signed_flow"])
        & frame["flow_percentile"].ge(entry_percentile)
        & frame["total_return_20"].abs().le(max_abs_return_20)
    )
    frame["rank_score"] = frame["signed_flow"].where(valid)
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s29_real_beta_residual_shock_reversal_scores(
    panel: pd.DataFrame,
    spec: RealStockStrategySpec | None = None,
) -> pd.DataFrame:
    params = spec.params if spec else {}
    beta_window = int(params.get("beta_window", 60))
    residual_vol_window = int(params.get("residual_vol_window", 20))
    shock_horizon = int(params.get("shock_horizon", 1))
    entry_z = float(params.get("entry_z", -2.0))
    min_amount_ratio = float(params.get("min_amount_ratio", 0.8))
    if beta_window < 2:
        raise ValueError("beta-residual beta_window must be at least 2")
    if residual_vol_window < 2:
        raise ValueError("beta-residual residual_vol_window must be at least 2")
    if shock_horizon <= 0:
        raise ValueError("beta-residual shock_horizon must be positive")
    if not np.isfinite(entry_z) or entry_z >= 0.0:
        raise ValueError("beta-residual entry_z must be finite and negative")
    if not np.isfinite(min_amount_ratio) or min_amount_ratio < 0.0:
        raise ValueError("beta-residual min_amount_ratio must be non-negative")

    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    signal_return = pd.to_numeric(_stock_returns(frame), errors="coerce").astype(float)
    signal_return = signal_return.where(np.isfinite(signal_return))
    frame["market_return"] = signal_return.groupby(
        frame["trade_date"], sort=False
    ).transform("median")

    lagged_stock_return = signal_return.groupby(frame["ts_code"], sort=False).shift(1)
    lagged_market_return = frame["market_return"].groupby(
        frame["ts_code"], sort=False
    ).shift(1)

    def rolling_beta_mean(values: pd.Series) -> pd.Series:
        return values.rolling(beta_window, min_periods=beta_window).mean()

    mean_stock = lagged_stock_return.groupby(frame["ts_code"], sort=False).transform(
        rolling_beta_mean
    )
    mean_market = lagged_market_return.groupby(frame["ts_code"], sort=False).transform(
        rolling_beta_mean
    )
    mean_cross = (lagged_stock_return * lagged_market_return).groupby(
        frame["ts_code"], sort=False
    ).transform(rolling_beta_mean)
    mean_market_square = lagged_market_return.pow(2.0).groupby(
        frame["ts_code"], sort=False
    ).transform(rolling_beta_mean)
    market_variance = mean_market_square - mean_market.pow(2.0)
    return_covariance = mean_cross - mean_stock * mean_market
    valid_market_variance = np.isfinite(market_variance) & market_variance.gt(0.0)
    frame["rolling_beta"] = return_covariance / market_variance.where(
        valid_market_variance
    )

    frame["daily_residual"] = signal_return - frame["rolling_beta"] * frame["market_return"]
    lagged_residual = frame["daily_residual"].groupby(
        frame["ts_code"], sort=False
    ).shift(1)
    frame["residual_sigma"] = lagged_residual.groupby(
        frame["ts_code"], sort=False
    ).transform(
        lambda values: values.rolling(
            residual_vol_window,
            min_periods=residual_vol_window,
        ).std(ddof=1)
    )
    frame["residual_shock_sum"] = frame["daily_residual"].groupby(
        frame["ts_code"], sort=False
    ).transform(
        lambda values: values.rolling(
            shock_horizon,
            min_periods=shock_horizon,
        ).sum()
    )
    shock_scale = frame["residual_sigma"] * np.sqrt(float(shock_horizon))
    valid_shock_scale = np.isfinite(shock_scale) & shock_scale.gt(0.0)
    frame["residual_shock_z"] = frame["residual_shock_sum"] / shock_scale.where(
        valid_shock_scale
    )

    lagged_mean_amount = frame["amount"].groupby(
        frame["ts_code"], sort=False
    ).transform(
        lambda values: values.shift(1).rolling(20, min_periods=20).mean()
    )
    frame["lagged_mean_amount_20"] = lagged_mean_amount
    frame["amount_ratio"] = frame["amount"] / lagged_mean_amount.where(
        np.isfinite(lagged_mean_amount) & lagged_mean_amount.gt(0.0)
    )
    valid = (
        frame["eligible"]
        & np.isfinite(frame["residual_shock_z"])
        & frame["residual_shock_z"].le(entry_z)
        & np.isfinite(frame["amount_ratio"])
        & frame["amount_ratio"].ge(min_amount_ratio)
    )
    frame["rank_score"] = (-frame["residual_shock_z"]).where(valid)
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s30_real_idiosyncratic_strength_scores(
    panel: pd.DataFrame,
    spec: RealStockStrategySpec | None = None,
) -> pd.DataFrame:
    params = spec.params if spec else {}
    beta_window = int(params.get("beta_window", 60))
    strength_window = int(params.get("strength_window", 60))
    skip_recent = int(params.get("skip_recent", 5))
    residual_vol_window = int(params.get("residual_vol_window", strength_window))
    residual_vol_penalty = float(params.get("residual_vol_penalty", 1.0))
    min_residual_momentum = float(params.get("min_residual_momentum", 0.0))
    if beta_window < 2:
        raise ValueError("idiosyncratic-strength beta_window must be at least 2")
    if strength_window <= 0:
        raise ValueError("idiosyncratic-strength strength_window must be positive")
    if skip_recent < 0:
        raise ValueError("idiosyncratic-strength skip_recent must be non-negative")
    if residual_vol_window < 2:
        raise ValueError("idiosyncratic-strength residual_vol_window must be at least 2")
    if not np.isfinite(residual_vol_penalty) or not 0.0 <= residual_vol_penalty <= 1.0:
        raise ValueError(
            "idiosyncratic-strength residual_vol_penalty must be between 0 and 1"
        )
    if not np.isfinite(min_residual_momentum) or min_residual_momentum < 0.0:
        raise ValueError(
            "idiosyncratic-strength min_residual_momentum must be finite and non-negative"
        )

    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    signal_return = pd.to_numeric(_stock_returns(frame), errors="coerce").astype(float)
    signal_return = signal_return.where(np.isfinite(signal_return))
    frame["market_return"] = signal_return.groupby(
        frame["trade_date"], sort=False
    ).transform("median")

    # The signal-day beta uses returns ending on the previous trading day.
    lagged_stock_return = signal_return.groupby(frame["ts_code"], sort=False).shift(1)
    lagged_market_return = frame["market_return"].groupby(
        frame["ts_code"], sort=False
    ).shift(1)

    def rolling_beta_mean(values: pd.Series) -> pd.Series:
        return values.rolling(beta_window, min_periods=beta_window).mean()

    mean_stock = lagged_stock_return.groupby(frame["ts_code"], sort=False).transform(
        rolling_beta_mean
    )
    mean_market = lagged_market_return.groupby(frame["ts_code"], sort=False).transform(
        rolling_beta_mean
    )
    mean_cross = (lagged_stock_return * lagged_market_return).groupby(
        frame["ts_code"], sort=False
    ).transform(rolling_beta_mean)
    mean_market_square = lagged_market_return.pow(2.0).groupby(
        frame["ts_code"], sort=False
    ).transform(rolling_beta_mean)
    market_variance = mean_market_square - mean_market.pow(2.0)
    return_covariance = mean_cross - mean_stock * mean_market
    valid_market_variance = np.isfinite(market_variance) & market_variance.gt(0.0)
    frame["rolling_beta"] = return_covariance / market_variance.where(
        valid_market_variance
    )

    frame["daily_residual"] = signal_return - frame["rolling_beta"] * frame["market_return"]
    lagged_residual = frame["daily_residual"].groupby(
        frame["ts_code"], sort=False
    ).shift(skip_recent)
    frame["residual_momentum"] = lagged_residual.groupby(
        frame["ts_code"], sort=False
    ).transform(
        lambda values: values.rolling(
            strength_window,
            min_periods=strength_window,
        ).sum()
    )
    frame["residual_volatility"] = lagged_residual.groupby(
        frame["ts_code"], sort=False
    ).transform(
        lambda values: values.rolling(
            residual_vol_window,
            min_periods=residual_vol_window,
        ).std(ddof=1)
    )
    residual_scale = frame["residual_volatility"] * np.sqrt(float(strength_window))
    valid_residual_scale = np.isfinite(residual_scale) & residual_scale.gt(0.0)
    penalty_scale = residual_scale.pow(residual_vol_penalty)
    frame["idiosyncratic_strength"] = frame["residual_momentum"] / penalty_scale.where(
        valid_residual_scale
    )
    valid = (
        frame["eligible"]
        & np.isfinite(frame["rolling_beta"])
        & np.isfinite(frame["residual_momentum"])
        & frame["residual_momentum"].gt(min_residual_momentum)
        & valid_residual_scale
        & np.isfinite(frame["idiosyncratic_strength"])
    )
    frame["rank_score"] = frame["idiosyncratic_strength"].where(valid)
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s31_real_post_limit_release_scores(
    panel: pd.DataFrame,
    spec: RealStockStrategySpec | None = None,
) -> pd.DataFrame:
    params = spec.params if spec else {}
    lookback = int(params.get("lookback", 20))
    exit_low_window = int(params.get("exit_low_window", 3))
    min_close_gap = float(params.get("min_close_below_limit_pct", 0.0005))
    max_close_gap = float(params.get("max_close_to_limit_pct", 0.02))
    min_amount_ratio = float(params.get("min_amount_ratio", 1.0))
    min_previous_amount = float(params.get("min_previous_amount", 20_000_000.0))
    touch_epsilon = float(params.get("limit_touch_epsilon", 1e-8))
    if lookback <= 0:
        raise ValueError("post-limit-release lookback must be positive")
    if exit_low_window <= 0:
        raise ValueError("post-limit-release exit_low_window must be positive")
    if not np.isfinite(min_close_gap) or min_close_gap <= 0.0:
        raise ValueError("post-limit-release min_close_below_limit_pct must be positive")
    if not np.isfinite(max_close_gap) or max_close_gap < min_close_gap:
        raise ValueError(
            "post-limit-release max_close_to_limit_pct must be at least the minimum close gap"
        )
    if not np.isfinite(min_amount_ratio) or min_amount_ratio <= 0.0:
        raise ValueError("post-limit-release min_amount_ratio must be positive")
    if not np.isfinite(min_previous_amount) or min_previous_amount < 0.0:
        raise ValueError("post-limit-release min_previous_amount must be non-negative")
    if not np.isfinite(touch_epsilon) or touch_epsilon < 0.0:
        raise ValueError("post-limit-release limit_touch_epsilon must be non-negative")

    required = {
        "amount",
        "close",
        "high",
        "trade_status",
        "up_limit",
    }
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(f"post-limit-release requires free-real daily columns: {missing}")

    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 180)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    frame["previous_amount"] = grouped["amount"].shift(1)
    frame["post_limit_amount_baseline"] = grouped["amount"].transform(
        lambda values: values.shift(1).rolling(lookback, min_periods=lookback).mean()
    )
    valid_baseline = (
        np.isfinite(frame["post_limit_amount_baseline"])
        & frame["post_limit_amount_baseline"].gt(0.0)
    )
    frame["post_limit_amount_ratio"] = frame["amount"] / frame[
        "post_limit_amount_baseline"
    ].where(valid_baseline)

    valid_limit = np.isfinite(frame["up_limit"]) & frame["up_limit"].gt(0.0)
    frame["post_limit_close_gap"] = (
        (frame["up_limit"] - frame["close"]) / frame["up_limit"].where(valid_limit)
    )
    frame["post_limit_touched"] = valid_limit & frame["high"].ge(
        frame["up_limit"] - touch_epsilon
    )
    frame["post_limit_released"] = (
        frame["close"].lt(frame["up_limit"] - touch_epsilon)
        & frame["post_limit_close_gap"].ge(min_close_gap)
    )

    frame["post_limit_exit_low"] = grouped["signal_price"].transform(
        lambda values: values.shift(1).rolling(
            exit_low_window,
            min_periods=exit_low_window,
        ).min()
    )
    trade_status = pd.to_numeric(frame["trade_status"], errors="coerce")
    delisting_exit = frame.get("delisting_exit_required", False)
    if not isinstance(delisting_exit, pd.Series):
        delisting_exit = pd.Series(bool(delisting_exit), index=frame.index)
    frame["exit_signal"] = (
        frame["signal_price"].lt(frame["post_limit_exit_low"])
        | trade_status.ne(1)
        | frame["is_suspended"]
        | frame["is_st"]
        | delisting_exit.fillna(False).astype(bool)
    )

    # Daily OHLC can establish a touch and a released close, but not seal timing or count.
    frame["entry_signal"] = (
        frame["eligible"]
        & trade_status.eq(1)
        & ~frame["is_suspended"]
        & ~frame["is_st"]
        & frame["post_limit_touched"]
        & frame["post_limit_released"]
        & frame["post_limit_close_gap"].le(max_close_gap)
        & frame["previous_amount"].ge(min_previous_amount)
        & frame["post_limit_amount_ratio"].ge(min_amount_ratio)
        & ~frame["exit_signal"]
    ).fillna(False)
    close_score = _percentile_by_date(frame, frame["post_limit_close_gap"], False)
    amount_score = _percentile_by_date(frame, frame["post_limit_amount_ratio"], True)
    frame["rank_score"] = close_score * 0.70 + amount_score * 0.30
    frame.loc[~frame["entry_signal"], "rank_score"] = np.nan
    frame["rank"] = _rank_desc_by_date(frame, "rank_score")
    return frame


def s16_real_value_composite_scores(panel: pd.DataFrame, spec: RealStockStrategySpec | None = None) -> pd.DataFrame:
    params = spec.params if spec else {}
    mode = str(params.get("mode", "value_momentum"))
    valuation_lag_days = int(params.get("valuation_lag_days", 5))
    frame = add_real_stock_eligibility(
        panel,
        min_listing_days=int(params.get("min_listing_days", 250)),
        min_price=float(params.get("min_price", 3.0)),
        min_avg_amount_20d=float(params.get("min_avg_amount_20d", 20_000_000.0)),
        exclude_st=bool(params.get("exclude_st", True)),
    )
    grouped = frame.groupby("ts_code", sort=False)
    daily_return = _stock_returns(frame)
    frame["return_120"] = grouped["signal_price"].pct_change(120)
    frame["volatility_60"] = daily_return.groupby(frame["ts_code"], sort=False).transform(
        lambda values: values.rolling(60, min_periods=30).std()
    )
    pe = frame["pe_ttm"].where(frame["pe_ttm"].between(0.0, 200.0, inclusive="neither"))
    pb = frame["pb"].where(frame["pb"].between(0.0, 30.0, inclusive="neither"))
    ps = frame["ps_ttm"].where(frame["ps_ttm"].between(0.0, 50.0, inclusive="neither"))
    pe = pe.groupby(frame["ts_code"], sort=False).shift(valuation_lag_days)
    pb = pb.groupby(frame["ts_code"], sort=False).shift(valuation_lag_days)
    ps = ps.groupby(frame["ts_code"], sort=False).shift(valuation_lag_days)
    pe_score = _percentile_by_date(frame, pe, False)
    pb_score = _percentile_by_date(frame, pb, False)
    ps_score = _percentile_by_date(frame, ps, False)
    momentum_score = _percentile_by_date(frame, frame["return_120"], True)
    low_vol_score = _percentile_by_date(frame, frame["volatility_60"], False)
    if mode == "deep_value":
        frame["rank_score"] = pe_score * 0.35 + pb_score * 0.35 + ps_score * 0.20 + low_vol_score * 0.10
    else:
        frame["rank_score"] = (
            pe_score * 0.20
            + pb_score * 0.20
            + ps_score * 0.10
            + momentum_score * 0.40
            + low_vol_score * 0.10
        )
    valid = frame["eligible"] & pe.notna() & pb.notna() & ps.notna()
    if mode == "value_momentum":
        valid &= frame["return_120"].gt(0.0)
    frame.loc[~valid, "rank_score"] = np.nan
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
                    },
                )
            )

    s3 = cfg.get("S3_real_stock_breakout", {}) if isinstance(cfg.get("S3_real_stock_breakout", {}), dict) else {}
    for donchian in s3.get("donchian", [20, 55]):
        for holding_k in s3.get("holding_k", [1, 2, 3, 5]):
            specs.append(
                RealStockStrategySpec(
                    name=f"S3_real_stock_breakout_d{donchian}_k{holding_k}",
                    family="S3_real_stock_breakout",
                    params={
                        "kind": "real_stock_breakout",
                        "donchian": int(donchian),
                        "exit_donchian": int(s3.get("exit_donchian", 20)),
                        "max_holding_days": int(s3.get("max_holding_days", 60)),
                        "min_volume_breakout": float(s3.get("min_volume_breakout", 1.2)),
                        "holding_k": int(holding_k),
                        "rebalance": "daily",
                        "entry_rebalance": "daily",
                        "trailing_stop": 0.0,
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
    if kind == "real_regime_selector":
        return s10_real_regime_selector_scores(panel, spec)
    if kind == "real_short_term_reversal":
        return s11_real_short_term_reversal_scores(panel, spec)
    if kind == "real_low_volatility":
        return s12_real_low_volatility_scores(panel, spec)
    if kind == "real_residual_momentum":
        return s13_real_residual_momentum_scores(panel, spec)
    if kind == "real_volume_price_shock":
        return s14_real_volume_price_shock_scores(panel, spec)
    if kind == "real_value_composite":
        return s16_real_value_composite_scores(panel, spec)
    if kind == "real_stateful_trend":
        return s20_real_stateful_trend_scores(panel, spec)
    if kind == "real_volatility_contraction":
        return s21_real_volatility_contraction_scores(panel, spec)
    if kind == "real_regime_contraction":
        return s24_real_regime_contraction_scores(panel, spec)
    if kind == "real_gap_intraday":
        return s26_real_gap_intraday_scores(panel, spec)
    if kind == "real_momentum_acceleration":
        return s27_real_momentum_acceleration_scores(panel, spec)
    if kind == "real_signed_flow_accumulation":
        return s28_real_signed_flow_accumulation_scores(panel, spec)
    if kind == "real_beta_residual_shock_reversal":
        return s29_real_beta_residual_shock_reversal_scores(panel, spec)
    if kind == "real_idiosyncratic_strength":
        return s30_real_idiosyncratic_strength_scores(panel, spec)
    if kind == "real_post_limit_release":
        return s31_real_post_limit_release_scores(panel, spec)
    raise ValueError(f"unsupported real stock strategy kind: {kind}")


def target_symbols_by_signal_date(
    scores: pd.DataFrame,
    spec: RealStockStrategySpec,
    holding_k: int,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, list[str]]:
    kind = str(spec.params.get("kind", ""))
    date_values = scores["trade_date"].astype(str)
    in_scope = pd.Series(True, index=scores.index)
    if start_date:
        in_scope &= date_values.ge(str(start_date).replace("-", ""))
    if end_date:
        in_scope &= date_values.le(str(end_date).replace("-", ""))
    scoped_scores = scores.loc[in_scope]
    if kind not in {
        "real_stock_breakout",
        "real_stateful_trend",
        "real_volatility_contraction",
        "real_regime_contraction",
        "real_gap_intraday",
        "real_momentum_acceleration",
        "real_post_limit_release",
    }:
        ranked = scoped_scores.loc[scoped_scores["rank_score"].notna()].copy()
        if ranked.empty:
            return {}
        ranked["trade_date"] = ranked["trade_date"].astype(str)
        ranked["ts_code"] = ranked["ts_code"].astype(str)
        ranked = ranked.sort_values(["trade_date", "rank_score", "ts_code"], ascending=[True, False, True])
        return {
            str(trade_date): group["ts_code"].head(holding_k).tolist()
            for trade_date, group in ranked.groupby("trade_date", sort=True)
        }

    required = {"trade_date", "ts_code", "signal_price", "entry_signal", "exit_signal", "rank_score"}
    missing = sorted(required - set(scoped_scores.columns))
    if missing:
        raise ValueError(f"stateful target book missing score columns: {missing}")
    frame = scoped_scores.loc[:, sorted(required)].copy()
    if frame.empty:
        return {}
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame["signal_price"] = pd.to_numeric(frame["signal_price"], errors="coerce")
    frame["entry_signal"] = frame["entry_signal"].fillna(False).astype(bool)
    frame["exit_signal"] = frame["exit_signal"].fillna(False).astype(bool)
    dates = pd.Series(sorted(frame["trade_date"].unique()))
    entry_frequency = str(spec.params.get("entry_rebalance", "weekly"))
    entry_dates = set(dates.loc[_date_rebalance_mask(dates, entry_frequency)].astype(str))
    trailing_stop = float(spec.params.get("trailing_stop", 0.18))
    max_holding_days = int(spec.params.get("max_holding_days", 120))
    daily = {
        str(trade_date): day.set_index("ts_code", drop=False)
        for trade_date, day in frame.groupby("trade_date", sort=True)
    }
    holdings: dict[str, dict[str, float | int]] = {}
    targets: dict[str, list[str]] = {}
    for date_index, trade_date in enumerate(dates.astype(str)):
        day = daily[trade_date]
        exited: set[str] = set()
        for symbol in list(holdings):
            if symbol not in day.index:
                continue
            row = day.loc[symbol]
            price = float(row["signal_price"])
            if np.isfinite(price) and price > 0.0:
                holdings[symbol]["high"] = max(float(holdings[symbol]["high"]), price)
            held_days = date_index - int(holdings[symbol]["entry_index"])
            trail_hit = (
                trailing_stop > 0.0
                and np.isfinite(price)
                and price <= float(holdings[symbol]["high"]) * (1.0 - trailing_stop)
            )
            time_hit = max_holding_days > 0 and held_days >= max_holding_days
            if bool(row["exit_signal"]) or trail_hit or time_hit:
                holdings.pop(symbol, None)
                exited.add(symbol)
        if trade_date in entry_dates and len(holdings) < holding_k:
            candidates = day.loc[day["entry_signal"] & day["rank_score"].notna()].copy().reset_index(drop=True)
            candidates = candidates.sort_values(["rank_score", "ts_code"], ascending=[False, True])
            for row in candidates.itertuples(index=False):
                symbol = str(row.ts_code)
                if symbol in holdings or symbol in exited:
                    continue
                price = float(row.signal_price)
                if not np.isfinite(price) or price <= 0.0:
                    continue
                holdings[symbol] = {"entry_index": date_index, "high": price}
                if len(holdings) >= holding_k:
                    break
        targets[trade_date] = sorted(holdings)
    return targets


def _capped_proportional_weights(
    raw_scores: Mapping[str, float],
    *,
    gross_exposure: float,
    max_weight: float,
) -> dict[str, float]:
    remaining = min(gross_exposure, max_weight * len(raw_scores))
    active = {
        str(symbol): float(score)
        for symbol, score in raw_scores.items()
        if np.isfinite(score) and score > 0.0
    }
    weights: dict[str, float] = {}
    while active and remaining > 1e-15:
        total_score = sum(active.values())
        if total_score <= 0.0:
            break
        capped = {
            symbol
            for symbol, score in active.items()
            if remaining * score / total_score >= max_weight - 1e-15
        }
        if not capped:
            for symbol, score in active.items():
                weights[symbol] = remaining * score / total_score
            remaining = 0.0
            break
        for symbol in sorted(capped):
            weights[symbol] = max_weight
            remaining -= max_weight
            active.pop(symbol)
    return dict(sorted(weights.items()))


def stock_target_weights_by_signal_date(
    scores: pd.DataFrame,
    targets_by_signal_date: Mapping[str, list[str]],
    spec: RealStockStrategySpec,
) -> dict[str, dict[str, float]]:
    """Build causal unlevered stock weights for an already selected target book."""

    mode = str(spec.params.get("weighting", "equal")).strip().lower()
    if mode not in {"equal", "inverse_volatility", "atr_risk", "rank"}:
        raise ValueError(f"unsupported stock weighting mode: {mode}")
    gross_exposure = float(spec.params.get("gross_exposure", 1.0))
    max_weight = float(spec.params.get("max_weight", 1.0))
    risk_window = int(spec.params.get("risk_window", 20))
    risk_floor = float(spec.params.get("risk_floor", 0.005))
    if not np.isfinite(gross_exposure) or not 0.0 <= gross_exposure <= 1.0:
        raise ValueError("gross_exposure must be finite and between 0 and 1")
    if not np.isfinite(max_weight) or not 0.0 < max_weight <= 1.0:
        raise ValueError("max_weight must be finite and in (0, 1]")
    if risk_window < 2:
        raise ValueError("risk_window must be at least 2")
    if not np.isfinite(risk_floor) or risk_floor <= 0.0:
        raise ValueError("risk_floor must be finite and positive")

    required = {"trade_date", "ts_code", "signal_price", "rank_score"}
    if mode == "atr_risk":
        required.update({"high", "low"})
    missing = sorted(required - set(scores.columns))
    if missing:
        raise ValueError(f"stock target weighting missing score columns: {missing}")

    frame = scores.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame["signal_price"] = pd.to_numeric(frame["signal_price"], errors="coerce")
    frame = frame.sort_values(["ts_code", "trade_date"], kind="mergesort")
    grouped = frame.groupby("ts_code", sort=False)
    if mode == "inverse_volatility":
        daily_return = grouped["signal_price"].pct_change(fill_method=None)
        frame["_weighting_risk"] = daily_return.groupby(
            frame["ts_code"], sort=False
        ).transform(
            lambda values: values.rolling(
                risk_window,
                min_periods=max(2, risk_window // 2),
            ).std(ddof=1)
        )
    elif mode == "atr_risk":
        high = pd.to_numeric(frame["high"], errors="coerce")
        low = pd.to_numeric(frame["low"], errors="coerce")
        previous_close = grouped["signal_price"].shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1, skipna=False)
        atr = true_range.groupby(frame["ts_code"], sort=False).transform(
            lambda values: values.rolling(
                risk_window,
                min_periods=max(2, risk_window // 2),
            ).mean()
        )
        frame["_weighting_risk"] = atr / frame["signal_price"].where(
            frame["signal_price"].gt(0.0)
        )
    frame = frame.sort_values(["trade_date", "ts_code"], kind="mergesort")
    daily = {
        str(trade_date): day.set_index("ts_code", drop=False)
        for trade_date, day in frame.groupby("trade_date", sort=True)
    }

    output: dict[str, dict[str, float]] = {}
    for trade_date, target_symbols in sorted(targets_by_signal_date.items()):
        symbols = list(dict.fromkeys(map(str, target_symbols)))
        day = daily.get(str(trade_date))
        if day is None or not symbols or gross_exposure <= 0.0:
            output[str(trade_date)] = {}
            continue
        rank_positions: dict[str, int] = {}
        if mode == "rank":
            selected = day.loc[day.index.intersection(symbols)].copy()
            selected["rank_score"] = pd.to_numeric(
                selected["rank_score"], errors="coerce"
            )
            selected = selected.sort_values(
                ["rank_score", "ts_code"],
                ascending=[False, True],
                kind="mergesort",
            )
            rank_positions = {
                str(symbol): position
                for position, symbol in enumerate(selected.index.astype(str), start=1)
            }
        raw_scores: dict[str, float] = {}
        for symbol in symbols:
            if symbol not in day.index:
                continue
            row = day.loc[symbol]
            if mode == "equal":
                raw_score = 1.0
            elif mode == "rank":
                rank_position = rank_positions.get(symbol)
                if rank_position is None:
                    continue
                raw_score = 1.0 / float(rank_position)
            else:
                risk = float(row.get("_weighting_risk", np.nan))
                if not np.isfinite(risk) or risk < 0.0:
                    continue
                raw_score = 1.0 / max(risk, risk_floor)
            raw_scores[symbol] = raw_score
        output[str(trade_date)] = _capped_proportional_weights(
            raw_scores,
            gross_exposure=gross_exposure,
            max_weight=max_weight,
        )
    return output


def strategy_rebalance_dates(panel: pd.DataFrame, frequency: str) -> list[str]:
    if not bool(panel.attrs.get("quant_proof_panel_validated", False)):
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

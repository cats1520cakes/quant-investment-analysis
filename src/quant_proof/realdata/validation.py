from __future__ import annotations

import pandas as pd

from .corporate_actions import add_signal_adjusted_close
from .schema import NUMERIC_PANEL_COLUMNS, PANEL_COLUMNS, PRICE_COLUMNS, normalize_date_series, require_columns
from .universe import build_active_stock_calendar


def build_stock_daily(raw: pd.DataFrame) -> pd.DataFrame:
    require_columns(raw, "daily", ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "amount"])
    if "volume" not in raw.columns and "vol" not in raw.columns:
        raise ValueError(f"daily missing required volume column: expected 'vol' or 'volume'; got {list(raw.columns)}")
    frame = raw.copy()
    if "volume" not in frame.columns:
        frame = frame.rename(columns={"vol": "volume"})
    frame["trade_date"] = normalize_date_series(frame["trade_date"])
    frame["ts_code"] = frame["ts_code"].astype("string").str.strip()
    for column in ["open", "high", "low", "close", "pre_close", "volume", "amount"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    keep = ["trade_date", "ts_code", "open", "high", "low", "close", "pre_close", "volume", "amount"]
    frame = frame.loc[:, keep].dropna(subset=["trade_date", "ts_code"])
    frame = frame.sort_values(["trade_date", "ts_code"]).drop_duplicates(["trade_date", "ts_code"], keep="last")
    if frame.empty:
        raise ValueError("daily produced no usable rows")
    return frame.reset_index(drop=True)


def build_stock_daily_basic(raw: pd.DataFrame) -> pd.DataFrame:
    require_columns(raw, "daily_basic", ["ts_code", "trade_date", "turnover_rate", "total_mv", "circ_mv"])
    frame = raw.copy()
    frame["trade_date"] = normalize_date_series(frame["trade_date"])
    frame["ts_code"] = frame["ts_code"].astype("string").str.strip()
    for column in ["turnover_rate", "total_mv", "circ_mv"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    keep = ["trade_date", "ts_code", "turnover_rate", "total_mv", "circ_mv"]
    frame = frame.loc[:, keep].dropna(subset=["trade_date", "ts_code"])
    frame = frame.sort_values(["trade_date", "ts_code"]).drop_duplicates(["trade_date", "ts_code"], keep="last")
    if frame.empty:
        raise ValueError("daily_basic produced no usable rows")
    return frame.reset_index(drop=True)


def validate_unique_keys(frame: pd.DataFrame, name: str, keys: list[str]) -> None:
    duplicated = frame.duplicated(keys).sum()
    if duplicated:
        raise ValueError(f"{name} has {int(duplicated)} duplicated key rows for keys={keys}")


def build_stock_panel(
    trade_calendar: pd.DataFrame,
    stock_basic: pd.DataFrame,
    stock_daily: pd.DataFrame,
    stock_adj_factor: pd.DataFrame,
    stock_daily_basic: pd.DataFrame,
    stock_limit: pd.DataFrame,
    suspension_flags: pd.DataFrame,
    st_flags: pd.DataFrame,
) -> pd.DataFrame:
    for name, frame in [
        ("stock_daily", stock_daily),
        ("stock_adj_factor", stock_adj_factor),
        ("stock_daily_basic", stock_daily_basic),
        ("stock_limit", stock_limit),
    ]:
        validate_unique_keys(frame, name, ["trade_date", "ts_code"])

    panel = build_active_stock_calendar(stock_basic, trade_calendar)
    for frame in [stock_daily, stock_adj_factor, stock_daily_basic, stock_limit]:
        panel = panel.merge(frame, on=["trade_date", "ts_code"], how="left")

    if not suspension_flags.empty:
        validate_unique_keys(suspension_flags, "suspension_flags", ["trade_date", "ts_code"])
        panel = panel.merge(suspension_flags, on=["trade_date", "ts_code"], how="left")
    else:
        panel["is_suspended"] = False

    if not st_flags.empty:
        validate_unique_keys(st_flags, "st_flags", ["trade_date", "ts_code"])
        panel = panel.merge(st_flags, on=["trade_date", "ts_code"], how="left")
    else:
        panel["is_st"] = False

    panel["is_suspended"] = panel["is_suspended"].fillna(False).astype(bool)
    missing_price = panel.loc[:, list(PRICE_COLUMNS)].isna().any(axis=1)
    panel["is_suspended"] = panel["is_suspended"] | missing_price
    panel["is_st"] = panel["is_st"].fillna(False).astype(bool)
    panel = add_signal_adjusted_close(panel)

    for column in NUMERIC_PANEL_COLUMNS:
        panel[column] = pd.to_numeric(panel[column], errors="coerce")
    panel["listing_days"] = pd.to_numeric(panel["listing_days"], errors="coerce").astype("Int64")
    missing_columns = [column for column in PANEL_COLUMNS if column not in panel.columns]
    if missing_columns:
        raise ValueError(f"stock_panel missing required output columns: {missing_columns}")
    panel = panel.loc[:, list(PANEL_COLUMNS)].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    validate_stock_panel(panel)
    return panel


def validate_stock_panel(panel: pd.DataFrame) -> None:
    missing = [column for column in PANEL_COLUMNS if column not in panel.columns]
    if missing:
        raise ValueError(f"stock_panel missing columns: {missing}")
    validate_unique_keys(panel, "stock_panel", ["trade_date", "ts_code"])
    if panel.empty:
        raise ValueError("stock_panel is empty")
    if panel["trade_date"].isna().any() or panel["ts_code"].isna().any():
        raise ValueError("stock_panel contains null trade_date or ts_code")


from __future__ import annotations

import pandas as pd

from .calendar import open_trade_dates
from .schema import date_to_datetime, normalize_date_series, require_columns


def infer_exchange(ts_code: object) -> str:
    text = "" if ts_code is None else str(ts_code)
    if "." in text:
        return text.split(".")[-1]
    if text.startswith(("60", "68", "90")):
        return "SH"
    if text.startswith(("00", "30", "20")):
        return "SZ"
    if text.startswith(("43", "83", "87", "92")):
        return "BJ"
    return ""


def infer_board(ts_code: object, market: object | None = None) -> str:
    if market is not None and not pd.isna(market) and str(market).strip():
        return str(market).strip()
    text = "" if ts_code is None else str(ts_code)
    code = text.split(".")[0]
    if code.startswith("688"):
        return "科创板"
    if code.startswith("300"):
        return "创业板"
    if code.startswith(("43", "83", "87", "92")):
        return "北交所"
    if code.startswith(("60", "00")):
        return "主板"
    return ""


def build_stock_basic(raw: pd.DataFrame) -> pd.DataFrame:
    require_columns(raw, "stock_basic", ["ts_code", "list_date", "list_status"])
    frame = raw.copy()
    frame["ts_code"] = frame["ts_code"].astype("string").str.strip()
    frame["list_date"] = normalize_date_series(frame["list_date"])
    if "delist_date" in frame.columns:
        frame["delist_date"] = normalize_date_series(frame["delist_date"])
    else:
        frame["delist_date"] = pd.NA
    frame["list_status"] = frame["list_status"].astype("string").str.strip()
    if "exchange" in frame.columns:
        frame["exchange"] = frame["exchange"].where(frame["exchange"].notna(), frame["ts_code"].map(infer_exchange))
        frame["exchange"] = frame["exchange"].astype("string").str.strip()
        frame.loc[frame["exchange"].isna() | (frame["exchange"] == ""), "exchange"] = frame["ts_code"].map(infer_exchange)
    else:
        frame["exchange"] = frame["ts_code"].map(infer_exchange)
    market = frame["market"] if "market" in frame.columns else pd.Series([pd.NA] * len(frame), index=frame.index)
    frame["board"] = [infer_board(ts_code, value) for ts_code, value in zip(frame["ts_code"], market)]

    keep = [
        "ts_code",
        "symbol",
        "name",
        "area",
        "industry",
        "market",
        "list_date",
        "delist_date",
        "list_status",
        "exchange",
        "board",
        "is_hs",
    ]
    for column in keep:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame.loc[:, keep].dropna(subset=["ts_code", "list_date"])
    frame = frame.sort_values(["ts_code", "list_status"]).drop_duplicates("ts_code", keep="first")
    if frame.empty:
        raise ValueError("stock_basic produced no rows with ts_code and list_date")
    return frame.reset_index(drop=True)


def build_active_stock_calendar(stock_basic: pd.DataFrame, trade_calendar: pd.DataFrame) -> pd.DataFrame:
    dates = open_trade_dates(trade_calendar)
    scaffold = dates.merge(
        stock_basic[["ts_code", "list_date", "delist_date", "list_status", "board", "exchange"]],
        how="cross",
    )
    trade_dt = date_to_datetime(scaffold["trade_date"])
    list_dt = date_to_datetime(scaffold["list_date"])
    delist_dt = date_to_datetime(scaffold["delist_date"])
    active = trade_dt.ge(list_dt) & (delist_dt.isna() | trade_dt.le(delist_dt))
    scaffold = scaffold.loc[active].copy()
    scaffold["listing_days"] = (trade_dt.loc[active] - list_dt.loc[active]).dt.days.astype("Int64")
    return scaffold.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


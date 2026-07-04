from __future__ import annotations

import pandas as pd

from .calendar import open_trade_dates
from .schema import normalize_date_series, require_columns


def is_st_name(value: object) -> bool:
    if value is None or pd.isna(value):
        return False
    text = str(value).upper().replace(" ", "")
    return "ST" in text


def build_stock_namechange(raw: pd.DataFrame) -> pd.DataFrame:
    require_columns(raw, "namechange", ["ts_code", "name", "start_date"])
    frame = raw.copy()
    frame["ts_code"] = frame["ts_code"].astype("string").str.strip()
    frame["name"] = frame["name"].astype("string").str.strip()
    frame["start_date"] = normalize_date_series(frame["start_date"])
    if "end_date" in frame.columns:
        frame["end_date"] = normalize_date_series(frame["end_date"])
    else:
        frame["end_date"] = pd.NA
    if "ann_date" in frame.columns:
        frame["ann_date"] = normalize_date_series(frame["ann_date"])
    else:
        frame["ann_date"] = pd.NA
    if "change_reason" not in frame.columns:
        frame["change_reason"] = pd.NA
    frame["is_st_name"] = frame["name"].map(is_st_name)
    keep = ["ts_code", "name", "start_date", "end_date", "ann_date", "change_reason", "is_st_name"]
    frame = frame.loc[:, keep].dropna(subset=["ts_code", "start_date"])
    frame = frame.sort_values(["start_date", "ts_code", "name"]).drop_duplicates(["ts_code", "start_date", "name"], keep="last")
    if frame.empty:
        raise ValueError("namechange produced no usable rows")
    return frame.reset_index(drop=True)


def build_st_flags(stock_namechange: pd.DataFrame, trade_calendar: pd.DataFrame) -> pd.DataFrame:
    st_rows = stock_namechange.loc[stock_namechange["is_st_name"]].copy()
    if st_rows.empty:
        return pd.DataFrame(columns=["trade_date", "ts_code", "is_st"])

    dates = open_trade_dates(trade_calendar)["trade_date"].astype("string")
    max_date = str(dates.max()) if not dates.empty else ""
    parts: list[pd.DataFrame] = []
    for row in st_rows.itertuples(index=False):
        start = getattr(row, "start_date")
        end = getattr(row, "end_date")
        if pd.isna(start):
            continue
        stop = max_date if pd.isna(end) else str(end)
        active_dates = dates[(dates >= str(start)) & (dates <= stop)]
        if active_dates.empty:
            continue
        parts.append(pd.DataFrame({"trade_date": active_dates.to_numpy(), "ts_code": getattr(row, "ts_code"), "is_st": True}))
    if not parts:
        return pd.DataFrame(columns=["trade_date", "ts_code", "is_st"])
    flags = pd.concat(parts, ignore_index=True)
    return flags.drop_duplicates(["trade_date", "ts_code"]).sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


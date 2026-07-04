from __future__ import annotations

import pandas as pd

from .calendar import open_trade_dates
from .schema import normalize_date_series, require_columns


def build_stock_suspend(raw: pd.DataFrame) -> pd.DataFrame:
    require_columns(raw, "suspend_d", ["ts_code", "suspend_date"])
    frame = raw.copy()
    frame["ts_code"] = frame["ts_code"].astype("string").str.strip()
    frame["suspend_date"] = normalize_date_series(frame["suspend_date"])
    if "resume_date" in frame.columns:
        frame["resume_date"] = normalize_date_series(frame["resume_date"])
    else:
        frame["resume_date"] = pd.NA
    for column in ["ann_date"]:
        if column in frame.columns:
            frame[column] = normalize_date_series(frame[column])
        else:
            frame[column] = pd.NA
    for column in ["suspend_reason", "reason_type"]:
        if column not in frame.columns:
            frame[column] = pd.NA
    keep = ["ts_code", "suspend_date", "resume_date", "ann_date", "suspend_reason", "reason_type"]
    frame = frame.loc[:, keep].dropna(subset=["ts_code", "suspend_date"])
    frame = frame.sort_values(["suspend_date", "ts_code"]).drop_duplicates(["ts_code", "suspend_date", "resume_date"], keep="last")
    if frame.empty:
        raise ValueError("suspend_d produced no usable rows")
    return frame.reset_index(drop=True)


def build_suspension_flags(stock_suspend: pd.DataFrame, trade_calendar: pd.DataFrame) -> pd.DataFrame:
    dates = open_trade_dates(trade_calendar)["trade_date"].astype("string")
    parts: list[pd.DataFrame] = []
    for row in stock_suspend.itertuples(index=False):
        start = getattr(row, "suspend_date")
        end = getattr(row, "resume_date")
        if pd.isna(start):
            continue
        if pd.isna(end) or str(end) <= str(start):
            active_dates = dates[dates == str(start)]
        else:
            active_dates = dates[(dates >= str(start)) & (dates < str(end))]
        if active_dates.empty:
            continue
        parts.append(pd.DataFrame({"trade_date": active_dates.to_numpy(), "ts_code": getattr(row, "ts_code"), "is_suspended": True}))
    if not parts:
        return pd.DataFrame(columns=["trade_date", "ts_code", "is_suspended"])
    flags = pd.concat(parts, ignore_index=True)
    return flags.drop_duplicates(["trade_date", "ts_code"]).sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


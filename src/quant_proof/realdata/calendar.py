from __future__ import annotations

import pandas as pd

from .schema import Phase2RealDataConfig, filter_date_range, normalize_date_series, require_columns


def build_trade_calendar(raw: pd.DataFrame, config: Phase2RealDataConfig) -> pd.DataFrame:
    require_columns(raw, "trade_cal", ["cal_date", "is_open"])
    frame = raw.copy()
    frame["trade_date"] = normalize_date_series(frame["cal_date"])
    frame = filter_date_range(frame, "trade_date", config.start_date, config.end_date)
    frame["is_open"] = pd.to_numeric(frame["is_open"], errors="coerce").fillna(0).astype("int64")
    if "exchange" not in frame.columns:
        frame["exchange"] = ""
    keep = ["trade_date", "exchange", "is_open"]
    if "pretrade_date" in frame.columns:
        frame["pretrade_date"] = normalize_date_series(frame["pretrade_date"])
        keep.append("pretrade_date")
    frame = frame.loc[:, keep].dropna(subset=["trade_date"])
    frame = frame.sort_values(["trade_date", "exchange"]).drop_duplicates(["trade_date", "exchange"], keep="last")
    if frame.empty:
        raise ValueError("trade_cal produced no rows after date filtering")
    return frame.reset_index(drop=True)


def open_trade_dates(trade_calendar: pd.DataFrame) -> pd.DataFrame:
    return (
        trade_calendar.loc[trade_calendar["is_open"] == 1, ["trade_date"]]
        .drop_duplicates()
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


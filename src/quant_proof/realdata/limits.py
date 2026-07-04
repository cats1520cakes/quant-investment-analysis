from __future__ import annotations

import pandas as pd

from .schema import normalize_date_series, require_columns


def build_stock_limit(raw: pd.DataFrame) -> pd.DataFrame:
    require_columns(raw, "stk_limit", ["trade_date", "ts_code", "up_limit", "down_limit"])
    frame = raw.copy()
    frame["trade_date"] = normalize_date_series(frame["trade_date"])
    frame["ts_code"] = frame["ts_code"].astype("string").str.strip()
    frame["up_limit"] = pd.to_numeric(frame["up_limit"], errors="coerce")
    frame["down_limit"] = pd.to_numeric(frame["down_limit"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "ts_code"])
    frame = frame.loc[:, ["trade_date", "ts_code", "up_limit", "down_limit"]]
    frame = frame.sort_values(["trade_date", "ts_code"]).drop_duplicates(["trade_date", "ts_code"], keep="last")
    if frame.empty:
        raise ValueError("stk_limit produced no usable rows")
    return frame.reset_index(drop=True)


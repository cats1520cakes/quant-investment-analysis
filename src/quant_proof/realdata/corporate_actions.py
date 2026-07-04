from __future__ import annotations

import pandas as pd

from .schema import normalize_date_series, require_columns


def build_stock_adj_factor(raw: pd.DataFrame) -> pd.DataFrame:
    require_columns(raw, "adj_factor", ["ts_code", "trade_date", "adj_factor"])
    frame = raw.copy()
    frame["ts_code"] = frame["ts_code"].astype("string").str.strip()
    frame["trade_date"] = normalize_date_series(frame["trade_date"])
    frame["adj_factor"] = pd.to_numeric(frame["adj_factor"], errors="coerce")
    frame = frame.dropna(subset=["ts_code", "trade_date", "adj_factor"])
    frame = frame.loc[:, ["trade_date", "ts_code", "adj_factor"]]
    frame = frame.sort_values(["trade_date", "ts_code"]).drop_duplicates(["trade_date", "ts_code"], keep="last")
    if frame.empty:
        raise ValueError("adj_factor produced no usable rows")
    return frame.reset_index(drop=True)


def add_signal_adjusted_close(panel: pd.DataFrame) -> pd.DataFrame:
    frame = panel.copy()

    def latest_factor(values: pd.Series) -> float:
        usable = values.dropna()
        if usable.empty:
            return float("nan")
        return float(usable.iloc[-1])

    latest = frame.groupby("ts_code", sort=False)["adj_factor"].transform(latest_factor)
    frame["adj_close_for_signal"] = frame["close"] * frame["adj_factor"] / latest
    return frame


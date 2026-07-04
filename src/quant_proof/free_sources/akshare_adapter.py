from __future__ import annotations

import pandas as pd


def fetch_a_share_hist(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str = "",
) -> pd.DataFrame:
    import akshare as ak

    return ak.stock_zh_a_hist(
        symbol=str(symbol),
        period="daily",
        start_date=str(start_date),
        end_date=str(end_date),
        adjust=str(adjust),
    )


def normalize_akshare_hist(frame: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "日期": "trade_date",
        "股票代码": "symbol",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
        "涨跌幅": "pct_chg",
        "涨跌额": "change",
        "换手率": "turnover_rate",
    }
    out = frame.rename(columns=rename).copy()
    if "trade_date" in out.columns:
        out["trade_date"] = out["trade_date"].astype(str).str.replace("-", "", regex=False)
    return out

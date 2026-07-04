from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd


ST_MAIN_BOARD_10PCT_EFFECTIVE_DATE = "20260706"


def round_to_cent(value: float | int | None) -> float:
    if value is None or pd.isna(value):
        return float("nan")
    rounded = Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def infer_free_board(ts_code: object, listed_board: object | None = None) -> str:
    if listed_board is not None and not pd.isna(listed_board) and str(listed_board).strip():
        return str(listed_board).strip()
    text = "" if ts_code is None else str(ts_code)
    symbol = text.split(".")[0]
    if symbol.startswith("688"):
        return "科创板"
    if symbol.startswith("300"):
        return "创业板"
    if symbol.startswith(("43", "83", "87", "92")):
        return "北交所"
    if symbol.startswith(("60", "00")):
        return "主板"
    return ""


def limit_pct_for_row(
    ts_code: object,
    trade_date: object,
    listing_days: object,
    is_st: object = False,
    board: object | None = None,
) -> float:
    board_name = infer_free_board(ts_code, board)
    date_text = str(trade_date)
    listing = 10**9 if listing_days is None or pd.isna(listing_days) else int(listing_days)
    st_flag = bool(is_st)

    if board_name in {"科创板", "创业板"} and listing < 5:
        return float("nan")
    if board_name == "科创板":
        return 0.20
    if board_name == "创业板":
        if date_text >= "20200824":
            return 0.20
        return 0.10
    if board_name == "北交所":
        return 0.30
    if st_flag and board_name == "主板":
        return 0.10 if date_text >= ST_MAIN_BOARD_10PCT_EFFECTIVE_DATE else 0.05
    if st_flag:
        return 0.05
    return 0.10


def derive_limit_prices(pre_close: float, limit_pct: float) -> tuple[float, float]:
    if pre_close is None or limit_pct is None or pd.isna(pre_close) or pd.isna(limit_pct):
        return float("nan"), float("nan")
    return round_to_cent(float(pre_close) * (1.0 + float(limit_pct))), round_to_cent(float(pre_close) * (1.0 - float(limit_pct)))


def add_derived_limit_prices(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    limit_pct = []
    up_limit = []
    down_limit = []
    for row in out.itertuples(index=False):
        pct = limit_pct_for_row(
            ts_code=getattr(row, "ts_code"),
            trade_date=getattr(row, "trade_date"),
            listing_days=getattr(row, "listing_days"),
            is_st=getattr(row, "is_st", False),
            board=getattr(row, "board", None),
        )
        up, down = derive_limit_prices(getattr(row, "pre_close", math.nan), pct)
        limit_pct.append(pct)
        up_limit.append(up)
        down_limit.append(down)
    out["limit_pct"] = limit_pct
    out["up_limit"] = up_limit
    out["down_limit"] = down_limit
    out["up_limit_source"] = "derived"
    out["down_limit_source"] = "derived"
    return out

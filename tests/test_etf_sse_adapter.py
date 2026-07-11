from __future__ import annotations

import pandas as pd
import pytest

from quant_proof.free_sources.etf_sse_adapter import (
    OFFICIAL_SUSPENSION_EXCEPTIONS,
    SseEtfDataError,
    expand_official_calendar,
    parse_sse_dayk,
)


def test_parse_sse_dayk_preserves_official_units() -> None:
    frame = parse_sse_dayk({"kline": [["2022-09-01", 1, 2, 0.5, 1.5, 123, 456.7]]}, "512100")
    assert frame.iloc[0].to_dict()["volume"] == 123
    assert frame.iloc[0].to_dict()["amount"] == 456.7


def test_512100_confirmed_suspension_is_retained() -> None:
    quotes = pd.DataFrame([["20220901", "512100", 1, 1, 1, 1, 100, 100]], columns=["trade_date", "code", "open", "high", "low", "close", "volume", "amount"])
    panel = expand_official_calendar(quotes, ["20220901", "20220902"], OFFICIAL_SUSPENSION_EXCEPTIONS)
    row = panel.loc[panel["trade_date"].eq("20220902")].iloc[0]
    assert bool(row["is_suspended"])
    assert not bool(row["tradable"])
    assert row["suspension_status"] == "confirmed_suspension"
    assert pd.isna(row["close"])


def test_510500_two_day_exception_is_exact() -> None:
    quotes = pd.DataFrame([["20150410", "510500", 1, 1, 1, 1, 100, 100]], columns=["trade_date", "code", "open", "high", "low", "close", "volume", "amount"])
    panel = expand_official_calendar(quotes, ["20150410", "20150413", "20150414"])
    assert panel["is_suspended"].tolist() == [False, True, True]


def test_undeclared_gap_and_conflicting_quote_fail_closed() -> None:
    quotes = pd.DataFrame([["20220901", "512100", 1, 1, 1, 1, 100, 100]], columns=["trade_date", "code", "open", "high", "low", "close", "volume", "amount"])
    with pytest.raises(SseEtfDataError, match="undeclared"):
        expand_official_calendar(quotes, ["20220901", "20220906"])
    conflicting = quotes.copy()
    conflicting.loc[0, "trade_date"] = "20220902"
    with pytest.raises(SseEtfDataError, match="conflicts"):
        expand_official_calendar(conflicting, ["20220902"])


def test_parse_rejects_declared_payload_without_rows() -> None:
    with pytest.raises(SseEtfDataError, match="kline"):
        parse_sse_dayk({"total": 1}, "510050")

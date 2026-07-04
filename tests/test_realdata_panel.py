from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant_proof.realdata.calendar import build_trade_calendar
from quant_proof.realdata.corporate_actions import build_stock_adj_factor
from quant_proof.realdata.limits import build_stock_limit
from quant_proof.realdata.schema import PANEL_COLUMNS, Phase2RealDataConfig
from quant_proof.realdata.st_status import build_st_flags, build_stock_namechange
from quant_proof.realdata.suspension import build_stock_suspend, build_suspension_flags
from quant_proof.realdata.universe import build_stock_basic
from quant_proof.realdata.validation import build_stock_daily, build_stock_daily_basic, build_stock_panel, validate_stock_panel
from quant_proof.real_strategies import (
    build_real_stock_strategy_specs,
    required_tables_for_real_stock_strategies,
    top_ranked_symbols,
)


def config() -> Phase2RealDataConfig:
    return Phase2RealDataConfig(
        raw={"data_root": "/tmp/phase2-realdata-test", "start_date": "20200101", "end_date": "20200102"},
        path=Path("synthetic.yaml"),
    )


def test_stock_panel_has_required_fields_and_execution_price_boundaries() -> None:
    trade_calendar = build_trade_calendar(
        pd.DataFrame({"cal_date": ["20200101", "20200102"], "is_open": [1, 1]}),
        config(),
    )
    stock_basic = build_stock_basic(
        pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "symbol": ["000001"],
                "name": ["平安银行"],
                "market": ["主板"],
                "list_date": ["20191201"],
                "delist_date": [None],
                "list_status": ["L"],
                "exchange": ["SZ"],
            }
        )
    )
    stock_daily = build_stock_daily(
        pd.DataFrame(
            {
                "trade_date": ["20200101"],
                "ts_code": ["000001.SZ"],
                "open": [9.5],
                "high": [10.5],
                "low": [9.0],
                "close": [10.0],
                "pre_close": [9.8],
                "vol": [1000],
                "amount": [10_000],
            }
        )
    )
    stock_adj_factor = build_stock_adj_factor(
        pd.DataFrame({"trade_date": ["20200101"], "ts_code": ["000001.SZ"], "adj_factor": [2.0]})
    )
    stock_daily_basic = build_stock_daily_basic(
        pd.DataFrame(
            {
                "trade_date": ["20200101"],
                "ts_code": ["000001.SZ"],
                "turnover_rate": [1.2],
                "total_mv": [100_000],
                "circ_mv": [80_000],
            }
        )
    )
    stock_limit = build_stock_limit(
        pd.DataFrame(
            {
                "trade_date": ["20200101", "20200102"],
                "ts_code": ["000001.SZ", "000001.SZ"],
                "up_limit": [10.78, 11.0],
                "down_limit": [8.82, 9.0],
            }
        )
    )
    stock_suspend = build_stock_suspend(pd.DataFrame({"ts_code": ["000001.SZ"], "suspend_date": ["20200102"]}))
    stock_namechange = build_stock_namechange(
        pd.DataFrame({"ts_code": ["000001.SZ"], "name": ["ST平安"], "start_date": ["20200102"]})
    )

    panel = build_stock_panel(
        trade_calendar=trade_calendar,
        stock_basic=stock_basic,
        stock_daily=stock_daily,
        stock_adj_factor=stock_adj_factor,
        stock_daily_basic=stock_daily_basic,
        stock_limit=stock_limit,
        suspension_flags=build_suspension_flags(stock_suspend, trade_calendar),
        st_flags=build_st_flags(stock_namechange, trade_calendar),
    )

    assert tuple(panel.columns) == PANEL_COLUMNS
    first_day = panel.loc[panel["trade_date"] == "20200101"].iloc[0]
    second_day = panel.loc[panel["trade_date"] == "20200102"].iloc[0]
    assert first_day["close"] == 10.0
    assert first_day["adj_close_for_signal"] == 10.0
    assert not bool(first_day["is_suspended"])
    assert bool(second_day["is_suspended"])
    assert bool(second_day["is_st"])
    assert second_day["listing_days"] == 32


def test_stock_panel_required_field_validation() -> None:
    bad_panel = pd.DataFrame({"trade_date": ["20200101"], "ts_code": ["000001.SZ"]})

    with pytest.raises(ValueError, match="stock_panel missing columns"):
        validate_stock_panel(bad_panel)


def test_real_stock_strategy_specs_focus_on_s2_s3_s4() -> None:
    specs = build_real_stock_strategy_specs()
    families = {spec.family for spec in specs}

    assert families == {"S2_real_stock_momentum", "S3_real_stock_breakout", "S4_real_smallcap_factor"}
    assert len([spec for spec in specs if spec.family == "S2_real_stock_momentum"]) == 18
    assert len([spec for spec in specs if spec.family == "S3_real_stock_breakout"]) == 8
    assert len([spec for spec in specs if spec.family == "S4_real_smallcap_factor"]) == 16
    assert "stk_limit" in required_tables_for_real_stock_strategies()
    assert "suspend_d" in required_tables_for_real_stock_strategies()


def test_top_ranked_symbols_uses_rank_score() -> None:
    scores = pd.DataFrame(
        {
            "trade_date": ["20200102", "20200102", "20200102"],
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "rank_score": [0.2, 0.5, None],
        }
    )

    assert top_ranked_symbols(scores, "20200102", 2) == ["000002.SZ", "000001.SZ"]

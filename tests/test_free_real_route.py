from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant_proof.free_sources.baostock_adapter import FreeRealConfig, select_codes, write_manifest
from quant_proof.free_sources.code_map import baostock_to_ts_code, ts_code_to_baostock
from quant_proof.free_sources.validators import strategy_allowed_in_tier
from quant_proof.real_strategies import add_real_stock_eligibility
from quant_proof.realdata.derived_limits import derive_limit_prices, limit_pct_for_row
from quant_proof.realdata.derived_market_cap import derive_circ_mv_from_amount_turnover
from quant_proof.realdata.free_panel_builder import FREE_PANEL_COLUMNS, build_free_stock_panel


def free_config(tmp_path: Path) -> FreeRealConfig:
    return FreeRealConfig(
        raw={
            "data_root": str(tmp_path),
            "date_range": {"start_date": "20200101", "end_date": "20200110"},
            "paths": {"manifest": "00_meta/manifests/test.csv"},
        },
        path=tmp_path / "phase2_free.yaml",
    )


def write_synthetic_free_raw(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "baostock"
    (root / "daily_raw").mkdir(parents=True)
    (root / "daily_qfq").mkdir(parents=True)
    pd.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "source_code": ["sh.600000"],
            "name": ["浦发银行"],
            "list_date": ["20100101"],
            "delist_date": [None],
            "type": ["1"],
            "list_status": ["1"],
        }
    ).to_parquet(root / "stock_basic.parquet", index=False)
    pd.DataFrame({"trade_date": ["20200101", "20200102", "20200103", "20200106", "20200107"], "is_open": [1, 1, 1, 1, 1]}).to_parquet(
        root / "trade_calendar.parquet", index=False
    )
    raw = pd.DataFrame(
        {
            "trade_date": ["20200101", "20200102", "20200103", "20200106", "20200107"],
            "ts_code": ["600000.SH"] * 5,
            "source_code": ["sh.600000"] * 5,
            "open": [9.8, 10.1, 10.2, 10.3, 10.4],
            "high": [10.2, 10.5, 10.6, 10.7, 10.8],
            "low": [9.7, 9.9, 10.0, 10.1, 10.2],
            "close": [10.0, 10.2, 10.4, 10.5, 10.6],
            "pre_close": [9.9, 10.0, 10.2, 10.4, 10.5],
            "volume": [1000, 1000, 1000, 1000, 1000],
            "amount": [1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000],
            "turnover_rate": [2.0, 2.0, 2.0, 2.0, 2.0],
            "pct_chg": [1.0, 2.0, 1.9, 1.0, 1.0],
            "pe_ttm": [10, 10, 10, 10, 10],
            "pb": [1, 1, 1, 1, 1],
            "ps_ttm": [2, 2, 2, 2, 2],
            "pcf_ttm": [3, 3, 3, 3, 3],
            "trade_status": [1, 0, 1, 1, 1],
            "is_st_raw": [0, 0, 0, 1, 0],
        }
    )
    qfq = raw[["trade_date", "ts_code", "source_code"]].copy()
    qfq["adj_close_for_signal"] = [20.0, 20.4, 20.8, 21.0, 21.2]
    raw.to_parquet(root / "daily_raw" / "sh_600000.parquet", index=False)
    qfq.to_parquet(root / "daily_qfq" / "sh_600000.parquet", index=False)


def test_baostock_code_mapping_round_trip() -> None:
    assert baostock_to_ts_code("sh.600000") == "600000.SH"
    assert baostock_to_ts_code("sz.000001") == "000001.SZ"
    assert ts_code_to_baostock("600000.SH") == "sh.600000"


def test_free_panel_uses_raw_for_execution_and_qfq_for_signal(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    panel = build_free_stock_panel(free_config(tmp_path))

    assert tuple(panel.columns) == FREE_PANEL_COLUMNS
    first = panel.iloc[0]
    assert first["close"] == 10.0
    assert first["adj_close_for_signal"] == 20.0
    assert first["up_limit_source"] == "derived"
    assert first["down_limit_source"] == "derived"


def test_tradestatus_and_is_st_are_mapped_to_free_real_flags(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    panel = build_free_stock_panel(free_config(tmp_path))

    suspended = panel.loc[panel["trade_date"] == "20200102"].iloc[0]
    st_row = panel.loc[panel["trade_date"] == "20200106"].iloc[0]
    assert bool(suspended["is_suspended"])
    assert bool(st_row["is_st"])

    eligible = add_real_stock_eligibility(panel, min_listing_days=0, min_price=0, min_avg_amount_20d=1, exclude_st=True)
    assert not bool(eligible.loc[eligible["trade_date"] == "20200106", "eligible"].iloc[0])


def test_limit_pct_rules_and_derived_prices() -> None:
    assert limit_pct_for_row("688001.SH", "20200110", 10, False, None) == 0.20
    assert pd.isna(limit_pct_for_row("688001.SH", "20200102", 2, False, None))
    assert limit_pct_for_row("300001.SZ", "20210101", 10, False, None) == 0.20
    assert limit_pct_for_row("830001.BJ", "20210101", 10, False, None) == 0.30
    assert limit_pct_for_row("600000.SH", "20260705", 10, True, None) == 0.05
    assert limit_pct_for_row("600000.SH", "20260706", 10, True, None) == 0.10
    assert derive_limit_prices(10.0, 0.10) == (11.0, 9.0)


def test_circ_mv_approximation() -> None:
    out = derive_circ_mv_from_amount_turnover(pd.Series([1_000_000.0]), pd.Series([2.0]))
    assert out.iloc[0] == 50_000_000.0


def test_free_real_and_proxy_strategy_admission() -> None:
    assert strategy_allowed_in_tier("S2_real_stock_momentum", "free_real").allowed
    assert not strategy_allowed_in_tier("S5_real_limitup_board", "free_real").allowed
    assert not strategy_allowed_in_tier("S2_real_stock_momentum", "proxy_research").allowed


def test_select_codes_supports_prefix_and_slices() -> None:
    stock_basic = pd.DataFrame(
        {
            "source_code": ["sh.600003", "sh.000001", "sh.600001", "sh.600002", "sz.000001"],
            "type": ["1", "2", "1", "1", "1"],
            "list_status": ["1", "1", "1", "0", "1"],
        }
    )

    assert select_codes(stock_basic, max_codes=None) == ["sh.600001", "sh.600003", "sz.000001"]
    assert select_codes(stock_basic, max_codes=2) == ["sh.600001", "sh.600003"]
    assert select_codes(stock_basic, max_codes=3, start_index=1, end_index=3) == ["sh.600003", "sz.000001"]


def test_write_manifest_upserts_records(tmp_path: Path) -> None:
    config = free_config(tmp_path)
    record = {
        "data_tier": "free_real",
        "source": "baostock",
        "table": "daily_raw",
        "name": "sh.600000",
        "path": str(tmp_path / "raw.parquet"),
        "rows": 1,
        "columns": "a",
        "sha256": "old",
        "downloaded_at": "2026-01-01T00:00:00",
    }
    updated = record | {"rows": 2, "sha256": "new"}

    write_manifest(config, [record])
    write_manifest(config, [updated])

    manifest = pd.read_csv(tmp_path / "00_meta" / "manifests" / "test.csv")
    assert len(manifest) == 1
    assert int(manifest.iloc[0]["rows"]) == 2
    assert manifest.iloc[0]["sha256"] == "new"

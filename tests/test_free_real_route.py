from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant_proof.free_sources.baostock_adapter import (
    FreeRealConfig,
    baostock_login_lock,
    select_codes,
    write_manifest,
)
from quant_proof.free_sources.code_map import baostock_to_ts_code, ts_code_to_baostock
from quant_proof.free_sources.download_planner import (
    ISOLATION_COLUMNS,
    refresh_download_plan,
    write_download_plan,
)
from quant_proof.free_sources.validators import strategy_allowed_in_tier
from quant_proof.real_strategies import (
    FREE_REAL_ANALYSIS_COLUMNS,
    add_real_stock_eligibility,
    load_free_real_analysis_panel,
)
from quant_proof.realdata.derived_limits import add_derived_limit_prices, derive_limit_prices, limit_pct_for_row
from quant_proof.realdata.derived_market_cap import derive_circ_mv_from_amount_turnover
from quant_proof.realdata.free_panel_builder import (
    FREE_PANEL_COLUMNS,
    FreePanelBuildError,
    build_and_write_free_stock_panel_streaming,
    build_free_stock_panel,
    panel_manifest_path,
    validate_panel_manifest,
)


def free_config(tmp_path: Path) -> FreeRealConfig:
    return FreeRealConfig(
        raw={
            "data_root": str(tmp_path),
            "date_range": {"start_date": "20200101", "end_date": "20200107"},
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
    pd.DataFrame(
        {
            "trade_date": [
                "20200101",
                "20200102",
                "20200103",
                "20200104",
                "20200105",
                "20200106",
                "20200107",
            ],
            "is_open": [1, 1, 1, 0, 0, 1, 1],
        }
    ).to_parquet(root / "trade_calendar.parquet", index=False)
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
    qfq["adj_open_for_signal"] = [19.6, 20.2, 20.4, 20.6, 20.8]
    qfq["adj_high_for_signal"] = [20.4, 21.0, 21.2, 21.4, 21.6]
    qfq["adj_low_for_signal"] = [19.4, 19.8, 20.0, 20.2, 20.4]
    qfq["adj_close_for_signal"] = [20.0, 20.4, 20.8, 21.0, 21.2]
    raw.to_parquet(root / "daily_raw" / "sh_600000.parquet", index=False)
    qfq.to_parquet(root / "daily_qfq" / "sh_600000.parquet", index=False)


def frozen_config_with_complete_plan(
    tmp_path: Path,
    *,
    isolation_status: str = "resolved",
) -> tuple[FreeRealConfig, Path]:
    root = tmp_path / "raw" / "baostock"
    stock_basic = pd.read_parquet(root / "stock_basic.parquet")
    frozen = stock_basic.loc[
        :, ["ts_code", "source_code", "list_date", "delist_date", "list_status"]
    ].copy()
    frozen.insert(0, "universe_scope", "point_in_time")
    frozen.insert(1, "universe_start_date", "20200101")
    frozen.insert(2, "universe_end_date", "20200107")
    universe_path = tmp_path / "00_meta" / "universes" / "frozen.csv"
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    frozen.to_csv(universe_path, index=False)

    raw_path = root / "daily_raw" / "sh_600000.parquet"
    qfq_path = root / "daily_qfq" / "sh_600000.parquet"
    held_raw = tmp_path / "held_raw.parquet"
    held_qfq = tmp_path / "held_qfq.parquet"
    raw_path.replace(held_raw)
    qfq_path.replace(held_qfq)
    plan_path, _, manifest = write_download_plan(tmp_path, universe_path, shard_size=1)
    held_raw.replace(raw_path)
    held_qfq.replace(qfq_path)
    refresh_download_plan(tmp_path, plan_path)

    timestamp = "2026-07-11T00:00:00"
    isolation = pd.DataFrame(
        [
            {
                "plan_id": str(manifest.iloc[0]["plan_id"]),
                "shard_id": 1,
                "source_code": "sh.600000",
                "status": isolation_status,
                "attempt_count": 2,
                "first_failed_at": timestamp,
                "last_attempt_at": timestamp,
                "last_checked_at": timestamp,
                "resolved_at": timestamp if isolation_status == "resolved" else "",
                "last_exit_code": 0,
                "last_pair_error": "synthetic retry",
                "last_integrity_error": "ok" if isolation_status == "resolved" else "missing",
            }
        ],
        columns=ISOLATION_COLUMNS,
    )
    isolation.to_csv(plan_path.parent / "isolated_codes.csv", index=False)
    config = FreeRealConfig(
        raw={
            "data_root": str(tmp_path),
            "date_range": {"start_date": "20200101", "end_date": "20200107"},
            "download": {
                "universe_scope": "point_in_time",
                "frozen_universe_path": str(universe_path.relative_to(tmp_path)),
                "download_plan_path": str(plan_path.relative_to(tmp_path)),
            },
            "panel_build": {"expected_symbols": 1, "allow_partial": False},
            "paths": {"manifest": "00_meta/manifests/test.csv"},
        },
        path=tmp_path / "canonical.yaml",
    )
    return config, plan_path


def test_baostock_code_mapping_round_trip() -> None:
    assert baostock_to_ts_code("sh.600000") == "600000.SH"
    assert baostock_to_ts_code("sz.000001") == "000001.SZ"
    assert ts_code_to_baostock("600000.SH") == "sh.600000"


def test_baostock_login_lock_records_the_current_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "00_meta" / "locks" / "baostock_login.lock"

    with baostock_login_lock(tmp_path):
        owner = lock_path.read_text(encoding="utf-8")

    assert "pid=" in owner
    assert "acquired_at=" in owner


def test_free_panel_uses_raw_for_execution_and_qfq_for_signal(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    panel = build_free_stock_panel(free_config(tmp_path))

    assert tuple(panel.columns) == FREE_PANEL_COLUMNS
    first = panel.iloc[0]
    assert first["close"] == 10.0
    assert first["adj_close_for_signal"] == 20.0
    assert first["corporate_action_share_factor"] == 1.0
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


def test_non_trading_terminal_row_may_lack_qfq_signal_price(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    raw_path = tmp_path / "raw" / "baostock" / "daily_raw" / "sh_600000.parquet"
    qfq_path = tmp_path / "raw" / "baostock" / "daily_qfq" / "sh_600000.parquet"
    raw = pd.read_parquet(raw_path)
    qfq = pd.read_parquet(qfq_path)
    terminal = raw["trade_date"].eq("20200107")
    raw.loc[terminal, ["open", "high", "low", "close"]] = float("nan")
    raw.loc[terminal, "trade_status"] = 0
    qfq.loc[
        terminal,
        [
            "adj_open_for_signal",
            "adj_high_for_signal",
            "adj_low_for_signal",
            "adj_close_for_signal",
        ],
    ] = float("nan")
    raw.to_parquet(raw_path, index=False)
    qfq.to_parquet(qfq_path, index=False)

    panel = build_free_stock_panel(free_config(tmp_path))

    terminal_row = panel.loc[panel["trade_date"].eq("20200107")].iloc[0]
    assert bool(terminal_row["is_suspended"])
    assert pd.isna(terminal_row["adj_close_for_signal"])

    raw.loc[terminal, "trade_status"] = 1
    raw.to_parquet(raw_path, index=False)
    with pytest.raises(ValueError, match="positive qfq signal"):
        build_free_stock_panel(free_config(tmp_path))


def test_date_coverage_rejects_missing_internal_trading_day(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    root = tmp_path / "raw" / "baostock"
    for table in ["daily_raw", "daily_qfq"]:
        path = root / table / "sh_600000.parquet"
        frame = pd.read_parquet(path)
        frame.loc[~frame["trade_date"].eq("20200103")].to_parquet(path, index=False)

    with pytest.raises(FreePanelBuildError, match=r"date coverage.*20200103"):
        build_free_stock_panel(free_config(tmp_path))


def test_delist_date_is_an_exclusive_coverage_boundary(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    root = tmp_path / "raw" / "baostock"
    stock_basic_path = root / "stock_basic.parquet"
    stock_basic = pd.read_parquet(stock_basic_path)
    stock_basic["delist_date"] = "20200107"
    stock_basic["list_status"] = "0"
    stock_basic.to_parquet(stock_basic_path, index=False)
    for table in ["daily_raw", "daily_qfq"]:
        path = root / table / "sh_600000.parquet"
        frame = pd.read_parquet(path)
        frame = frame.loc[~frame["trade_date"].eq("20200107")].copy()
        terminal = frame["trade_date"].eq("20200106")
        if table == "daily_raw":
            frame.loc[terminal, ["open", "high", "low", "close"]] = float("nan")
            frame.loc[terminal, "trade_status"] = 0
        else:
            frame["trade_status"] = 1
            frame.loc[
                terminal,
                [
                    "adj_open_for_signal",
                    "adj_high_for_signal",
                    "adj_low_for_signal",
                    "adj_close_for_signal",
                ],
            ] = float("nan")
            frame.loc[terminal, "trade_status"] = 0
        frame.to_parquet(path, index=False)

    path = build_and_write_free_stock_panel_streaming(free_config(tmp_path), batch_codes=1)
    panel = pd.read_parquet(path)

    terminal = panel.iloc[-1]
    assert terminal["trade_date"] == "20200106"
    assert bool(terminal["is_suspended"])
    assert bool(terminal["delisting_exit_required"])


def test_pre_research_recent_listing_is_not_marked_artificially_mature(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    root = tmp_path / "raw" / "baostock"
    stock_basic = pd.read_parquet(root / "stock_basic.parquet")
    stock_basic["ts_code"] = "300001.SZ"
    stock_basic["source_code"] = "sz.300001"
    stock_basic["list_date"] = "20191230"
    stock_basic.to_parquet(root / "stock_basic.parquet", index=False)
    for table in ["daily_raw", "daily_qfq"]:
        path = next((root / table).glob("*.parquet"))
        frame = pd.read_parquet(path)
        frame["ts_code"] = "300001.SZ"
        frame["source_code"] = "sz.300001"
        frame.to_parquet(path, index=False)

    panel = build_free_stock_panel(free_config(tmp_path))
    first = panel.iloc[0]

    assert first["listing_days"] == 2
    assert first["listing_trading_days"] < 5
    assert pd.isna(first["limit_pct"])


def test_delisted_stock_marks_only_its_last_observation_for_terminal_exit(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    stock_basic_path = tmp_path / "raw" / "baostock" / "stock_basic.parquet"
    stock_basic = pd.read_parquet(stock_basic_path)
    stock_basic["delist_date"] = "20200107"
    stock_basic["list_status"] = "0"
    stock_basic.to_parquet(stock_basic_path, index=False)

    panel = build_free_stock_panel(free_config(tmp_path))

    assert int(panel["delisting_exit_required"].sum()) == 1
    terminal = panel.loc[panel["delisting_exit_required"]].iloc[0]
    assert terminal["trade_date"] == "20200107"
    assert terminal["terminal_value_source"] == "delisting_last_observation"


def test_delisting_after_research_end_is_not_written_off(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    stock_basic_path = tmp_path / "raw" / "baostock" / "stock_basic.parquet"
    stock_basic = pd.read_parquet(stock_basic_path)
    stock_basic["delist_date"] = "20200111"
    stock_basic["list_status"] = "0"
    stock_basic.to_parquet(stock_basic_path, index=False)

    panel = build_free_stock_panel(free_config(tmp_path))

    assert not panel["delisting_exit_required"].any()


def test_free_panel_rejects_truncated_qfq_keys(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    qfq_path = tmp_path / "raw" / "baostock" / "daily_qfq" / "sh_600000.parquet"
    pd.read_parquet(qfq_path).head(2).to_parquet(qfq_path, index=False)

    with pytest.raises(FreePanelBuildError, match="raw/qfq daily keys differ"):
        build_free_stock_panel(free_config(tmp_path))


def test_streaming_panel_builder_writes_valid_single_parquet(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)

    path = build_and_write_free_stock_panel_streaming(free_config(tmp_path), batch_codes=1)
    panel = pd.read_parquet(path)

    assert path == tmp_path / "processed" / "phase2_free" / "stock_panel.parquet"
    assert tuple(panel.columns) == FREE_PANEL_COLUMNS
    assert len(panel) == 5
    assert panel_manifest_path(path).exists()
    manifest = validate_panel_manifest(path)
    assert int(manifest["symbols"]) == 1
    assert manifest["universe_scope"] == "unspecified"
    assert manifest["download_plan"] == {"required": False}
    assert manifest["raw_qfq_inputs"]["symbols"] == 1
    assert len(manifest["raw_qfq_inputs"]["records"]) == 1
    assert manifest["date_coverage"]["passed"] is True

    analysis_panel = load_free_real_analysis_panel(path)
    assert FREE_REAL_ANALYSIS_COLUMNS.issubset(analysis_panel.columns)
    assert "source_code" not in analysis_panel.columns
    assert len(analysis_panel) == len(panel)
    filtered = load_free_real_analysis_panel(path, start_date="20200103", end_date="20200106")
    assert set(filtered["trade_date"].astype(str)) == {"20200103", "20200106"}


def test_panel_manifest_rejects_stale_builder_hash(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    path = build_and_write_free_stock_panel_streaming(free_config(tmp_path), batch_codes=1)
    manifest_path = panel_manifest_path(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["builder_source_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(FreePanelBuildError, match="stale panel builder"):
        validate_panel_manifest(path)


def test_panel_manifest_rejects_changed_raw_input(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    path = build_and_write_free_stock_panel_streaming(free_config(tmp_path), batch_codes=1)
    raw_path = tmp_path / "raw" / "baostock" / "daily_raw" / "sh_600000.parquet"
    raw = pd.read_parquet(raw_path)
    raw.loc[0, "close"] = 99.0
    raw.to_parquet(raw_path, index=False)

    with pytest.raises(FreePanelBuildError, match=r"raw input (size|hash) changed"):
        validate_panel_manifest(path)


def test_canonical_manifest_binds_complete_plan_retry_ledger_and_inputs(
    tmp_path: Path,
) -> None:
    write_synthetic_free_raw(tmp_path)
    config, _ = frozen_config_with_complete_plan(tmp_path)

    path = build_and_write_free_stock_panel_streaming(config, batch_codes=1)
    manifest = validate_panel_manifest(path, expected_symbols=1)

    plan = manifest["download_plan"]
    assert plan["required"] is True
    assert plan["status"] == "complete"
    assert plan["remaining_codes"] == 0
    assert plan["isolation_records"] == 1
    assert plan["resolved_isolation_records"] == 1
    assert len(manifest["raw_qfq_inputs"]["records"][0]["raw_sha256"]) == 64
    assert len(manifest["raw_qfq_inputs"]["records"][0]["qfq_sha256"]) == 64


def test_appledouble_sidecars_do_not_enter_input_or_plan_provenance(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    config, plan_path = frozen_config_with_complete_plan(tmp_path)
    for table in ["daily_raw", "daily_qfq"]:
        sidecar = tmp_path / "raw" / "baostock" / table / "._sh_600000.parquet"
        sidecar.write_bytes(b"AppleDouble metadata, not parquet")
    for sidecar in [
        plan_path.parent / "._download_plan.csv",
        plan_path.parent / "._missing_universe.csv",
        plan_path.parent / "._isolated_codes.csv",
        plan_path.parent / "shards" / "._shard_0001.txt",
    ]:
        sidecar.write_bytes(b"AppleDouble metadata")

    path = build_and_write_free_stock_panel_streaming(config, batch_codes=1)
    manifest = validate_panel_manifest(path, expected_symbols=1)

    inputs = manifest["raw_qfq_inputs"]
    assert inputs["symbols"] == 1
    assert inputs["raw_files"] == 1
    assert inputs["qfq_files"] == 1
    assert len(inputs["records"]) == 1
    assert all("/._" not in value for value in [
        inputs["records"][0]["raw_path"],
        inputs["records"][0]["qfq_path"],
        manifest["download_plan"]["manifest_path"],
        manifest["download_plan"]["isolation_manifest_path"],
    ])


def test_canonical_build_rejects_unresolved_isolation_retry(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    config, _ = frozen_config_with_complete_plan(
        tmp_path,
        isolation_status="isolated",
    )

    with pytest.raises(FreePanelBuildError, match="isolation retry is unresolved"):
        build_and_write_free_stock_panel_streaming(config, batch_codes=1)


def test_panel_manifest_rejects_changed_isolation_retry_record(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    config, plan_path = frozen_config_with_complete_plan(tmp_path)
    path = build_and_write_free_stock_panel_streaming(config, batch_codes=1)
    isolation_path = plan_path.parent / "isolated_codes.csv"
    isolation = pd.read_csv(isolation_path, dtype=str, keep_default_na=False)
    isolation.loc[0, "last_pair_error"] = "tampered retry record"
    isolation.to_csv(isolation_path, index=False)

    with pytest.raises(FreePanelBuildError, match="download-plan provenance changed"):
        validate_panel_manifest(path, expected_symbols=1)


def test_streaming_panel_builder_refuses_incomplete_frozen_universe(tmp_path: Path) -> None:
    write_synthetic_free_raw(tmp_path)
    stock_basic_path = tmp_path / "raw" / "baostock" / "stock_basic.parquet"
    stock_basic = pd.read_parquet(stock_basic_path)
    second = stock_basic.iloc[0].copy()
    second["ts_code"] = "600001.SH"
    second["source_code"] = "sh.600001"
    pd.concat([stock_basic, second.to_frame().T], ignore_index=True).to_parquet(stock_basic_path, index=False)

    with pytest.raises(FreePanelBuildError, match="incomplete universe"):
        build_and_write_free_stock_panel_streaming(free_config(tmp_path), batch_codes=1)


def test_limit_pct_rules_and_derived_prices() -> None:
    assert limit_pct_for_row("688001.SH", "20200110", 10, False, None) == 0.20
    assert pd.isna(limit_pct_for_row("688001.SH", "20200102", 2, False, None))
    assert limit_pct_for_row("300001.SZ", "20210101", 10, False, None) == 0.20
    assert limit_pct_for_row("301001.SZ", "20210101", 10, False, None) == 0.20
    assert limit_pct_for_row("830001.BJ", "20210101", 10, False, None) == 0.30
    assert limit_pct_for_row("600000.SH", "20260705", 10, True, None) == 0.05
    assert limit_pct_for_row("600000.SH", "20260706", 10, True, None) == 0.10
    assert derive_limit_prices(10.0, 0.10) == (11.0, 9.0)


def test_vectorized_limit_prices_match_scalar_rules() -> None:
    frame = pd.DataFrame(
        {
            "ts_code": ["688001.SH", "300001.SZ", "300002.SZ", "830001.BJ", "600001.SH"],
            "trade_date": ["20200110", "20210101", "20190101", "20210101", "20200101"],
            "listing_days": [10, 10, 10, 10, 10],
            "is_st": [False, False, False, False, True],
            "board": ["科创板", "创业板", "创业板", "北交所", "主板"],
            "pre_close": [10.01, 10.01, 10.01, 10.01, 10.01],
        }
    )

    out = add_derived_limit_prices(frame)

    expected_pct = [
        limit_pct_for_row(row.ts_code, row.trade_date, row.listing_days, row.is_st, row.board)
        for row in frame.itertuples(index=False)
    ]
    expected_prices = [derive_limit_prices(10.01, pct) for pct in expected_pct]
    assert out["limit_pct"].tolist() == expected_pct
    assert out["up_limit"].tolist() == [pair[0] for pair in expected_prices]
    assert out["down_limit"].tolist() == [pair[1] for pair in expected_prices]


def test_circ_mv_approximation() -> None:
    out = derive_circ_mv_from_amount_turnover(pd.Series([1_000_000.0]), pd.Series([2.0]))
    assert out.iloc[0] == 50_000_000.0


def test_free_real_and_proxy_strategy_admission() -> None:
    assert strategy_allowed_in_tier("S2_real_stock_momentum", "free_real").allowed
    assert not strategy_allowed_in_tier("S10_real_regime_selector", "free_real").allowed
    assert not strategy_allowed_in_tier("S16_real_value_composite", "free_real").allowed
    assert strategy_allowed_in_tier("S11_real_short_term_reversal", "free_real").allowed
    assert strategy_allowed_in_tier("S20_real_stateful_trend", "free_real").allowed
    assert strategy_allowed_in_tier("S21_real_volatility_contraction", "free_real").allowed
    assert strategy_allowed_in_tier("S22_real_concentrated_trend", "free_real").allowed
    assert strategy_allowed_in_tier("S23_real_concentrated_contraction", "free_real").allowed
    assert strategy_allowed_in_tier("S24_real_regime_contraction", "free_real").allowed
    assert strategy_allowed_in_tier("S26_real_gap_intraday", "free_real").allowed
    assert strategy_allowed_in_tier("S27_real_momentum_acceleration", "free_real").allowed
    for family in [
        "S28_real_signed_flow_accumulation",
        "S29_real_beta_residual_shock_reversal",
        "S30_real_idiosyncratic_strength",
    ]:
        assert strategy_allowed_in_tier(family, "free_real").allowed
        assert not strategy_allowed_in_tier(family, "proxy_research").allowed
        assert strategy_allowed_in_tier(family, "strict_real").allowed
    assert strategy_allowed_in_tier(
        "S31_real_post_limit_release", "free_real_derived_limits"
    ).allowed
    assert not strategy_allowed_in_tier(
        "S31_real_post_limit_release", "free_real"
    ).allowed
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


def test_select_codes_supports_point_in_time_delisted_universe() -> None:
    stock_basic = pd.DataFrame(
        {
            "source_code": ["sh.600001", "sh.600002", "sh.600003", "sh.000001"],
            "type": ["1", "1", "1", "2"],
            "list_status": ["1", "0", "0", "1"],
            "list_date": ["2010-01-01", "2008-01-01", "2027-01-01", "1990-01-01"],
            "delist_date": [None, "2018-06-01", None, None],
        }
    )

    assert select_codes(
        stock_basic,
        max_codes=None,
        universe_scope="point_in_time",
        universe_start_date="20100101",
        universe_end_date="20260704",
    ) == ["sh.600001", "sh.600002"]


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

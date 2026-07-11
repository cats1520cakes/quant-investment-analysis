from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quant_proof.free_real_backtest import (
    DerivativeEndOfDaySnapshot,
    FreeRealBacktestConfig,
    _apply_terminal_settlements,
    _apply_corporate_actions,
    _prepare_daily_panel,
    aggregate_free_real_windows,
    filter_rolling_windows_by_start,
    evaluate_free_real_strategy,
    load_backtest_config,
    select_rolling_windows,
    simulate_free_real_window,
)
from quant_proof.engine.account import Account
from quant_proof.engine.combined_account import CombinedAccount
from quant_proof.real_strategies import (
    RealStockStrategySpec,
    compute_real_stock_scores,
    prepare_real_stock_features,
    target_symbols_by_signal_date,
)


def synthetic_panel() -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-01", "2020-04-30")
    rows = []
    for symbol, source, base, slope in [
        ("600001.SH", "sh.600001", 10.0, 0.03),
        ("600002.SH", "sh.600002", 9.0, 0.01),
    ]:
        for i, dt in enumerate(dates):
            close = base + i * slope
            rows.append(
                {
                    "trade_date": dt.strftime("%Y%m%d"),
                    "ts_code": symbol,
                    "source_code": source,
                    "open": close * 0.995,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "pre_close": close / 1.001,
                    "corporate_action_share_factor": 1.0,
                    "corporate_action_source": "none",
                    "volume": 1_000_000,
                    "amount": 30_000_000,
                    "turnover_rate": 2.0,
                    "pct_chg": 0.1,
                    "pe_ttm": 10.0,
                    "pb": 1.0,
                    "ps_ttm": 2.0,
                    "pcf_ttm": 3.0,
                    "adj_close_for_signal": close,
                    "trade_status": 1,
                    "is_suspended": False,
                    "is_st": False,
                    "list_date": "20190101",
                    "delist_date": "",
                    "list_status": "1",
                    "is_last_observation": i == len(dates) - 1,
                    "delisting_exit_required": False,
                    "terminal_value_source": "",
                    "listing_days": 300 + i,
                    "board": "main",
                    "limit_pct": 0.10,
                    "up_limit": close * 1.10,
                    "down_limit": close * 0.90,
                    "circ_mv_approx": 1_500_000_000,
                    "data_tier": "free_real",
                    "up_limit_source": "derived",
                    "down_limit_source": "derived",
                    "market_cap_source": "derived_from_amount_turnover",
                }
            )
    return pd.DataFrame(rows)


def test_load_backtest_config_defaults_and_overrides() -> None:
    defaults = load_backtest_config({})
    assert defaults.max_daily_amount_participation is None
    assert defaults.candidate_joint_success_alpha == 0.05
    assert defaults.candidate_min_nonoverlap_blocks == 5
    assert defaults.candidate_min_nonoverlap_w24 == 720_000.0
    assert defaults.candidate_max_nonoverlap_drawdown == 0.35
    assert defaults.candidate_max_unfilled_sell_share == 0.10

    cfg = load_backtest_config(
        {
            "target_backtest": {
                "monthly_deposit": 1000,
                "target_month_12": 12_000,
                "target_month_24": 24_000,
                "candidate_gates": {
                    "joint_success_alpha": 0.01,
                    "min_nonoverlap_blocks": 7,
                    "min_nonoverlap_w24": 18_000,
                    "max_nonoverlap_drawdown": 0.25,
                    "max_unfilled_sell_share": 0.05,
                },
                "execution": {
                    "lot_size": 100,
                    "max_order_notional": 20_000,
                    "max_daily_amount_participation": 0.01,
                },
            }
        }
    )

    assert cfg.monthly_deposit == 1000
    assert cfg.target_month_12 == 12_000
    assert cfg.target_month_24 == 24_000
    assert cfg.lot_size == 100
    assert cfg.max_order_notional == 20_000
    assert cfg.max_daily_amount_participation == 0.01
    assert cfg.candidate_joint_success_alpha == 0.01
    assert cfg.candidate_min_nonoverlap_blocks == 7
    assert cfg.candidate_min_nonoverlap_w24 == 18_000
    assert cfg.candidate_max_nonoverlap_drawdown == 0.25
    assert cfg.candidate_max_unfilled_sell_share == 0.05


@pytest.mark.parametrize(
    ("candidate_gates", "message"),
    [
        ({"joint_success_alpha": 0.0}, "joint_success_alpha"),
        ({"min_nonoverlap_blocks": 4.5}, "min_nonoverlap_blocks"),
        ({"max_unfilled_sell_share": 1.1}, "max_unfilled_sell_share"),
    ],
)
def test_load_backtest_config_rejects_invalid_formal_gates(
    candidate_gates: dict[str, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_backtest_config({"target_backtest": {"candidate_gates": candidate_gates}})


def test_terminal_settlement_writes_off_delisted_position() -> None:
    account = Account(cash=0.0)
    account.portfolio.add_buy(
        symbol="600001.SH",
        quantity=100,
        price=10.0,
        fees=0.0,
        trade_date=date(2020, 1, 1),
        available_from=date(2020, 1, 2),
    )
    last_prices = {"600001.SH": 10.0}

    counters = _apply_terminal_settlements(
        account,
        {"600001.SH": {"ts_code": "600001.SH", "close": 10.0, "delisting_exit_required": True}},
        FreeRealBacktestConfig(delisting_terminal_value_fraction=0.0),
        last_prices,
    )

    assert account.portfolio.quantity("600001.SH") == 0
    assert account.cash == 0.0
    assert last_prices == {}
    assert counters["terminal_exit_positions"] == 1.0
    assert counters["terminal_writeoff_notional"] == 1000.0


def test_select_rolling_windows_evenly_spans_history() -> None:
    windows = [(pd.Timestamp(f"2020-{month:02d}-01"), pd.Timestamp(f"2020-{month:02d}-28")) for month in range(1, 11)]

    selected = select_rolling_windows(windows, max_windows=4, sampling="even")

    assert len(selected) == 4
    assert selected[0] == windows[0]
    assert selected[-1] == windows[-1]
    assert select_rolling_windows(windows, max_windows=2, sampling="head") == windows[:2]


def test_daily_panel_indexes_symbol_major_frame_without_full_date_sort() -> None:
    panel = synthetic_panel()
    assert not panel["trade_date"].is_monotonic_increasing

    daily = _prepare_daily_panel(panel)
    first_date = str(panel["trade_date"].min())

    assert daily._bounds == {}
    assert daily._indices
    assert set(daily[first_date]) == {"600001.SH", "600002.SH"}
    assert daily[first_date]["600001.SH"]["trade_date"] == first_date


def test_stateful_target_book_holds_until_independent_exit() -> None:
    scores = pd.DataFrame(
        [
            {"trade_date": "20200101", "ts_code": "A", "signal_price": 10.0, "entry_signal": True, "exit_signal": False, "rank_score": 1.0},
            {"trade_date": "20200101", "ts_code": "B", "signal_price": 10.0, "entry_signal": True, "exit_signal": False, "rank_score": 0.5},
            {"trade_date": "20200102", "ts_code": "A", "signal_price": 10.5, "entry_signal": False, "exit_signal": False, "rank_score": None},
            {"trade_date": "20200102", "ts_code": "B", "signal_price": 11.0, "entry_signal": True, "exit_signal": False, "rank_score": 2.0},
            {"trade_date": "20200103", "ts_code": "A", "signal_price": 9.5, "entry_signal": False, "exit_signal": True, "rank_score": None},
            {"trade_date": "20200103", "ts_code": "B", "signal_price": 11.2, "entry_signal": True, "exit_signal": False, "rank_score": 2.0},
        ]
    )
    spec = RealStockStrategySpec(
        name="stateful",
        family="S20_real_stateful_trend",
        params={
            "kind": "real_stateful_trend",
            "entry_rebalance": "daily",
            "trailing_stop": 0.20,
            "max_holding_days": 20,
        },
    )

    targets = target_symbols_by_signal_date(scores, spec=spec, holding_k=1)

    assert targets["20200101"] == ["A"]
    assert targets["20200102"] == ["A"]
    assert targets["20200103"] == ["B"]


def test_stateful_target_book_resets_at_each_rolling_window_start() -> None:
    scores = pd.DataFrame(
        [
            {"trade_date": "20191231", "ts_code": "A", "signal_price": 10.0, "entry_signal": True, "exit_signal": False, "rank_score": 1.0},
            {"trade_date": "20200102", "ts_code": "A", "signal_price": 10.2, "entry_signal": False, "exit_signal": False, "rank_score": None},
        ]
    )
    spec = RealStockStrategySpec(
        name="stateful",
        family="S20_real_stateful_trend",
        params={"kind": "real_stateful_trend", "entry_rebalance": "daily"},
    )

    inherited = target_symbols_by_signal_date(scores, spec=spec, holding_k=1)
    reset = target_symbols_by_signal_date(
        scores,
        spec=spec,
        holding_k=1,
        start_date="20200101",
        end_date="20200131",
    )

    assert inherited["20200102"] == ["A"]
    assert reset["20200102"] == []


def test_stateful_strategy_scorers_emit_entry_and_exit_state() -> None:
    panel = synthetic_panel()
    for kind, family, extra in [
        (
            "real_stateful_trend",
            "S20_real_stateful_trend",
            {"entry_window": 10, "exit_window": 5, "trend_window": 20, "momentum_window": 10},
        ),
        (
            "real_volatility_contraction",
            "S21_real_volatility_contraction",
            {"breakout_window": 10, "short_vol_window": 5, "long_vol_window": 20, "exit_window": 5},
        ),
    ]:
        spec = RealStockStrategySpec(
            name=family,
            family=family,
            params={
                "kind": kind,
                "min_listing_days": 0,
                "min_price": 0.0,
                "min_avg_amount_20d": 1.0,
                **extra,
            },
        )

        scores = compute_real_stock_scores(panel, spec)

        assert {"entry_signal", "exit_signal", "rank_score", "signal_price"}.issubset(scores.columns)
        assert len(scores) == len(panel)


def test_indexed_daily_panel_returns_only_requested_day_rows() -> None:
    panel = synthetic_panel()
    lookup = _prepare_daily_panel(panel)
    first_date = min(lookup)
    day = lookup[first_date]

    assert len(lookup) == panel["trade_date"].nunique()
    assert set(day) == {"600001.SH", "600002.SH"}
    assert day["600001.SH"]["trade_date"] == first_date
    assert day.get("missing") is None
    assert lookup[first_date] is day


def test_precomputed_common_features_preserve_strategy_scores() -> None:
    panel = synthetic_panel()
    spec = RealStockStrategySpec(
        name="S2_test",
        family="S2_real_stock_momentum",
        params={
            "kind": "real_stock_momentum",
            "min_listing_days": 0,
            "min_price": 0.0,
            "min_avg_amount_20d": 1.0,
        },
    )

    direct = compute_real_stock_scores(panel, spec)
    prepared = compute_real_stock_scores(prepare_real_stock_features(panel), spec)

    pd.testing.assert_series_equal(direct["rank_score"], prepared["rank_score"])
    pd.testing.assert_series_equal(direct["eligible"], prepared["eligible"])


def test_free_real_target_backtest_generates_goal_windows() -> None:
    spec = RealStockStrategySpec(
        name="S2_real_stock_momentum_k1_daily",
        family="S2_real_stock_momentum",
        params={
            "kind": "real_stock_momentum",
            "holding_k": 1,
            "rebalance": "daily",
            "min_listing_days": 0,
            "min_price": 0.0,
            "min_avg_amount_20d": 1.0,
        },
    )
    cfg = FreeRealBacktestConfig(
        monthly_deposit=10_000,
        target_month_12=120_000,
        target_month_24=240_000,
        window_months=1,
        min_trading_days=10,
        max_order_notional=50_000,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    windows = evaluate_free_real_strategy(synthetic_panel(), spec, cfg, deposit_timings=("beginning",), max_windows=2)

    assert not windows.empty
    assert set(["w12", "w24", "max_drawdown", "filled_orders", "rejected_orders"]).issubset(windows.columns)
    assert windows["filled_orders"].sum() > 0


def _formal_gate_windows(
    n_blocks: int,
    *,
    w24_values: list[float] | None = None,
    drawdowns: list[float] | None = None,
    unfilled_sell_shares: list[float] | None = None,
    missing_row_blocks: list[float] | None = None,
) -> pd.DataFrame:
    w24_values = w24_values or [1_300_000.0] * n_blocks
    drawdowns = drawdowns or [0.2] * n_blocks
    unfilled_sell_shares = unfilled_sell_shares or [0.0] * n_blocks
    missing_row_blocks = missing_row_blocks or [0.0] * n_blocks
    assert all(
        len(values) == n_blocks
        for values in [w24_values, drawdowns, unfilled_sell_shares, missing_row_blocks]
    )
    return pd.DataFrame(
        [
            {
                "strategy": "s",
                "family": "S2",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "start": f"{2000 + 2 * index}-01-01",
                "end": f"{2001 + 2 * index}-12-31",
                "w12": 550_000.0,
                "w24": w24_values[index],
                "total_deposit": 720_000.0,
                "max_drawdown": drawdowns[index],
                "requested_sell_notional": 100.0,
                "participation_unfilled_sell_notional": 100.0 * unfilled_sell_shares[index],
                "participation_unfilled_sell_share": unfilled_sell_shares[index],
                "blocked_missing_rows": missing_row_blocks[index],
            }
            for index in range(n_blocks)
        ]
    )


def test_free_real_window_aggregation_uses_hard_targets() -> None:
    windows = pd.DataFrame(
        [
            {
                "strategy": "s",
                "family": "S2",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "w12": 500_000.0,
                "w24": 1_200_000.0,
                "total_deposit": 720_000.0,
                "max_drawdown": 0.1,
                "avg_turnover": 0.01,
                "filled_orders": 10,
                "rejected_orders": 1,
                "rejected_limit_up": 0,
                "rejected_limit_down": 0,
                "rejected_suspended": 1,
                "fees": 100,
                "participation_clipped_orders": 2,
                "participation_clipped_notional": 1000,
                "participation_blocked_orders": 1,
                "participation_blocked_notional": 500,
            },
            {
                "strategy": "s",
                "family": "S2",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "w12": 499_999.0,
                "w24": 1_300_000.0,
                "total_deposit": 720_000.0,
                "max_drawdown": 0.4,
                "avg_turnover": 0.02,
                "filled_orders": 12,
                "rejected_orders": 2,
                "rejected_limit_up": 1,
                "rejected_limit_down": 0,
                "rejected_suspended": 0,
                "fees": 120,
                "participation_clipped_orders": 4,
                "participation_clipped_notional": 2000,
                "participation_blocked_orders": 3,
                "participation_blocked_notional": 1500,
            },
        ]
    )

    leaderboard = aggregate_free_real_windows(windows, FreeRealBacktestConfig())
    row = leaderboard.iloc[0]

    assert row["p_success"] == 0.5
    assert row["p_w12"] == 0.5
    assert row["p_w24"] == 1.0
    assert row["median_w12"] == 499_999.5
    assert row["p05_w24"] == 1_205_000.0
    assert not bool(row["passes_median_target"])
    assert not bool(row["passes_joint_success_gate"])
    assert bool(row["passes_p05_deposit_floor"])
    assert not bool(row["passes_drawdown_gate"])
    assert not bool(row["passes_core_candidate_gates"])
    assert row["avg_rejected_orders"] == 1.5
    assert row["avg_participation_clipped_orders"] == 3.0
    assert row["avg_participation_blocked_orders"] == 2.0


def test_wilson_bound_remains_descriptive() -> None:
    windows = _formal_gate_windows(8)
    cfg = FreeRealBacktestConfig(max_daily_amount_participation=0.05)

    row = aggregate_free_real_windows(windows, cfg).iloc[0]

    assert row["n_windows"] == 8
    assert row["n_nonoverlap_windows"] == 8
    assert row["rolling_start_hit_share"] == 1.0
    assert row["nonoverlap_hit_share"] == 1.0
    assert row["nonoverlap_hit_share_lower95"] > 0.5
    assert row["nonoverlap_binomial_pvalue"] == pytest.approx(1.0 / 256.0)
    assert bool(row["passes_core_candidate_gates"])


def test_exact_joint_success_gate_rejects_four_of_four() -> None:
    cfg = FreeRealBacktestConfig(max_daily_amount_participation=0.05)

    row = aggregate_free_real_windows(_formal_gate_windows(4), cfg).iloc[0]

    assert row["n_nonoverlap_successes"] == 4
    assert row["nonoverlap_binomial_pvalue"] == pytest.approx(0.0625)
    assert row["nonoverlap_hit_share_lower95"] > 0.5
    assert bool(row["passes_wilson_descriptive_threshold"])
    assert not bool(row["passes_min_nonoverlap_blocks"])
    assert not bool(row["passes_joint_success_gate"])


def test_exact_joint_success_gate_accepts_five_of_five_uncorrected() -> None:
    cfg = FreeRealBacktestConfig(max_daily_amount_participation=0.05)

    row = aggregate_free_real_windows(_formal_gate_windows(5), cfg).iloc[0]

    assert row["n_nonoverlap_successes"] == 5
    assert row["nonoverlap_binomial_pvalue"] == pytest.approx(0.03125)
    assert bool(row["passes_min_nonoverlap_blocks"])
    assert bool(row["passes_joint_success_gate"])
    assert bool(row["passes_core_candidate_gates"])

    strict_cfg = FreeRealBacktestConfig(
        max_daily_amount_participation=0.05,
        candidate_joint_success_alpha=0.01,
    )
    strict_row = aggregate_free_real_windows(_formal_gate_windows(5), strict_cfg).iloc[0]
    assert not bool(strict_row["passes_joint_success_gate"])


def test_formal_tail_and_drawdown_gates_use_worst_nonoverlap_block() -> None:
    windows = _formal_gate_windows(
        5,
        w24_values=[700_000.0, 1_300_000.0, 1_300_000.0, 1_300_000.0, 1_300_000.0],
        drawdowns=[0.4, 0.1, 0.1, 0.1, 0.1],
    )
    cfg = FreeRealBacktestConfig(
        target_month_24=600_000.0,
        max_daily_amount_participation=0.05,
    )

    row = aggregate_free_real_windows(windows, cfg).iloc[0]

    assert bool(row["passes_joint_success_gate"])
    assert row["nonoverlap_p05_w24"] == pytest.approx(820_000.0)
    assert bool(row["passes_p05_deposit_floor"])
    assert row["nonoverlap_min_w24"] == 700_000.0
    assert not bool(row["passes_tail_gate"])
    assert row["nonoverlap_p95_max_drawdown"] == pytest.approx(0.34)
    assert bool(row["passes_p95_drawdown_gate"])
    assert row["nonoverlap_max_drawdown"] == 0.4
    assert not bool(row["passes_drawdown_gate"])
    assert not bool(row["passes_core_candidate_gates"])


def test_liquidity_gate_requires_cap_complete_rows_and_worst_share() -> None:
    windows = _formal_gate_windows(
        5,
        unfilled_sell_shares=[0.0, 0.02, 0.04, 0.08, 0.10],
    )
    capped_cfg = FreeRealBacktestConfig(max_daily_amount_participation=0.05)

    row = aggregate_free_real_windows(windows, capped_cfg).iloc[0]
    assert row["mean_participation_unfilled_sell_share"] == pytest.approx(0.048)
    assert row["worst_participation_unfilled_sell_share"] == pytest.approx(0.10)
    assert bool(row["passes_liquidity_gate"])

    no_cap = aggregate_free_real_windows(windows, FreeRealBacktestConfig()).iloc[0]
    assert not bool(no_cap["passes_liquidity_gate"])

    incomplete = aggregate_free_real_windows(
        windows.drop(columns="participation_unfilled_sell_share"), capped_cfg
    ).iloc[0]
    assert not bool(incomplete["has_sell_liquidity_accounting"])
    assert not bool(incomplete["passes_liquidity_gate"])

    missing_row_windows = windows.copy()
    missing_row_windows.loc[0, "blocked_missing_rows"] = 1.0
    missing_row = aggregate_free_real_windows(missing_row_windows, capped_cfg).iloc[0]
    assert not bool(missing_row["passes_liquidity_gate"])

    over_cap_windows = windows.copy()
    over_cap_windows.loc[0, "participation_unfilled_sell_notional"] = 11.0
    over_cap_windows.loc[0, "participation_unfilled_sell_share"] = 0.11
    over_cap = aggregate_free_real_windows(over_cap_windows, capped_cfg).iloc[0]
    assert over_cap["worst_participation_unfilled_sell_share"] == pytest.approx(0.11)
    assert not bool(over_cap["passes_liquidity_gate"])


def test_derivative_candidate_gate_rejects_margin_calls_defaults_and_missing_audit() -> None:
    windows = _formal_gate_windows(5)
    windows["derivative_coordinator_active"] = 1.0
    windows["margin_call_days"] = 0.0
    windows["default_events"] = 0.0
    windows["max_margin_shortfall"] = 0.0
    windows["raw_default_nav"] = 0.0
    cfg = FreeRealBacktestConfig(max_daily_amount_participation=0.05)

    clean = aggregate_free_real_windows(windows, cfg).iloc[0]
    assert bool(clean["has_margin_accounting"])
    assert bool(clean["margin_accounting_valid"])
    assert bool(clean["passes_margin_gate"])
    assert bool(clean["passes_core_candidate_gates"])

    margin_call_windows = windows.copy()
    margin_call_windows.loc[0, ["margin_call_days", "max_margin_shortfall"]] = [1.0, 5_000.0]
    margin_call = aggregate_free_real_windows(margin_call_windows, cfg).iloc[0]
    assert margin_call["margin_call_window_share"] == pytest.approx(0.2)
    assert not bool(margin_call["passes_margin_gate"])

    default_windows = windows.copy()
    default_windows.loc[0, ["default_events", "raw_default_nav"]] = [1.0, -10_000.0]
    default = aggregate_free_real_windows(default_windows, cfg).iloc[0]
    assert default["default_window_share"] == pytest.approx(0.2)
    assert default["min_raw_default_nav"] == -10_000.0
    assert not bool(default["passes_margin_gate"])

    missing_audit = aggregate_free_real_windows(
        windows.drop(columns="max_margin_shortfall"),
        cfg,
    ).iloc[0]
    assert not bool(missing_audit["has_margin_accounting"])
    assert not bool(missing_audit["passes_margin_gate"])


def test_free_real_window_aggregation_rejects_duplicate_windows() -> None:
    windows = pd.DataFrame(
        [
            {
                "strategy": "s",
                "family": "S2",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "start": "2020-01-01",
                "end": "2021-12-31",
                "w12": 500_000.0,
                "w24": 1_200_000.0,
                "total_deposit": 720_000.0,
                "max_drawdown": 0.1,
            }
        ]
        * 2
    )

    try:
        aggregate_free_real_windows(windows, FreeRealBacktestConfig())
    except ValueError as exc:
        assert "duplicate rolling window result" in str(exc)
    else:
        raise AssertionError("duplicate windows must be rejected")


def test_filter_rolling_windows_by_start_creates_disjoint_stage_ranges() -> None:
    windows = [
        (pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year + 1}-12-31"))
        for year in range(2010, 2025)
    ]

    discovery = filter_rolling_windows_by_start(windows, "2010-01-01", "2016-12-31")
    confirmation = filter_rolling_windows_by_start(windows, "2017-01-01", "2021-12-31")
    holdout = filter_rolling_windows_by_start(windows, "2022-01-01", "2024-12-31")

    assert {start.year for start, _ in discovery} == set(range(2010, 2017))
    assert {start.year for start, _ in confirmation} == set(range(2017, 2022))
    assert {start.year for start, _ in holdout} == set(range(2022, 2025))


def test_corporate_action_preserves_value_before_ex_date_return() -> None:
    account = Account(cash=0.0)
    account.portfolio.add_buy(
        symbol="600001.SH",
        quantity=100,
        price=10.0,
        fees=0.0,
        trade_date=date(2020, 1, 1),
        available_from=date(2020, 1, 2),
    )
    day = {
        "600001.SH": {
            "ts_code": "600001.SH",
            "pre_close": 5.0,
            "close": 5.5,
            "corporate_action_share_factor": 2.0,
        }
    }

    counters = _apply_corporate_actions(account, day)

    assert account.portfolio.quantity("600001.SH") == 200
    assert account.total_equity({"600001.SH": 5.0}) == 1_000.0
    assert account.total_equity({"600001.SH": 5.5}) == 1_100.0
    assert counters["corporate_action_events"] == 1.0


def test_corporate_action_credits_fractional_cash_in_lieu() -> None:
    account = Account(cash=0.0)
    account.portfolio.add_buy(
        symbol="600001.SH",
        quantity=100,
        price=10.0,
        fees=0.0,
        trade_date=date(2020, 1, 1),
        available_from=date(2020, 1, 2),
    )
    factor = 10.0 / 9.5
    day = {
        "600001.SH": {
            "ts_code": "600001.SH",
            "pre_close": 9.5,
            "close": 9.5,
            "corporate_action_share_factor": factor,
        }
    }

    counters = _apply_corporate_actions(account, day)

    assert account.portfolio.quantity("600001.SH") == 105
    assert account.cash == pytest.approx(2.5)
    assert account.total_equity({"600001.SH": 9.5}) == pytest.approx(1_000.0)
    assert counters["corporate_action_cash_in_lieu"] == pytest.approx(2.5)


def _execution_row(symbol: str, open_price: float, amount: float) -> dict[str, object]:
    return {
        "ts_code": symbol,
        "open": open_price,
        "close": open_price,
        "amount": amount,
        "is_suspended": False,
        "up_limit": open_price * 1.1,
        "down_limit": open_price * 0.9,
    }


def test_participation_cap_clips_buy_orders_before_engine_submission() -> None:
    panel_by_date = {
        "20200101": {"600001.SH": _execution_row("600001.SH", 10.0, 15_000.0)},
        "20200102": {"600001.SH": _execution_row("600001.SH", 10.0, 15_000.0)},
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
        max_daily_amount_participation=0.10,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={"20200101": ["600001.SH"]},
        rebalance_dates=set(panel_by_date),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-02"),
        deposit_timing="beginning",
        cfg=cfg,
    )

    assert metrics["filled_orders"] == 1.0
    assert metrics["traded_notional"] == 1000.0
    assert metrics["participation_clipped_orders"] == 1.0
    assert metrics["participation_clipped_notional"] == 19_000.0
    assert metrics["participation_blocked_orders"] == 0.0
    assert metrics["rejected_orders"] == 0.0


def test_combined_account_no_overlay_replays_stock_window_exactly() -> None:
    panel_by_date = {
        "20200101": {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)},
        "20200102": {"600001.SH": _execution_row("600001.SH", 11.0, 1_000_000.0)},
        "20200103": {"600001.SH": _execution_row("600001.SH", 12.0, 1_000_000.0)},
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )
    kwargs = {
        "panel_by_date": panel_by_date,
        "trading_dates": list(panel_by_date),
        "top_by_signal_date": {
            "20200101": ["600001.SH"],
            "20200102": ["600001.SH"],
        },
        "rebalance_dates": set(panel_by_date),
        "start": pd.Timestamp("2020-01-01"),
        "end": pd.Timestamp("2020-01-03"),
        "deposit_timing": "beginning",
        "cfg": cfg,
    }

    stock_equity, stock_metrics = simulate_free_real_window(**kwargs)
    combined_equity, combined_metrics = simulate_free_real_window(
        **kwargs,
        account_factory=CombinedAccount,
    )

    pd.testing.assert_frame_equal(stock_equity, combined_equity)
    assert stock_metrics == combined_metrics


def test_explicit_stock_target_weights_preserve_a_cash_sleeve() -> None:
    symbols = ["600001.SH", "600002.SH"]
    panel_by_date = {
        trade_date: {
            symbol: _execution_row(symbol, 10.0, 1_000_000.0)
            for symbol in symbols
        }
        for trade_date in ["20200101", "20200102"]
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    equity, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={"20200101": symbols},
        target_weights_by_signal_date={
            "20200101": {"600001.SH": 0.25, "600002.SH": 0.50}
        },
        rebalance_dates=set(panel_by_date),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-02"),
        deposit_timing="beginning",
        cfg=cfg,
    )

    assert metrics["traded_notional"] == 15_000.0
    assert equity.iloc[-1]["cash"] == 5_000.0
    assert equity.iloc[-1]["wealth"] == 20_000.0


@pytest.mark.parametrize(
    "weights",
    [
        {"600001.SH": 0.75, "600002.SH": 0.50},
        {"600001.SH": 0.50, "600099.SH": 0.25},
    ],
)
def test_invalid_explicit_stock_target_weights_fail_closed(
    weights: dict[str, float],
) -> None:
    symbols = ["600001.SH", "600002.SH"]
    panel_by_date = {
        trade_date: {
            symbol: _execution_row(symbol, 10.0, 1_000_000.0)
            for symbol in symbols
        }
        for trade_date in ["20200101", "20200102"]
    }

    with pytest.raises(ValueError, match="target weight|target weights"):
        simulate_free_real_window(
            panel_by_date=panel_by_date,
            trading_dates=list(panel_by_date),
            top_by_signal_date={"20200101": symbols},
            target_weights_by_signal_date={"20200101": weights},
            rebalance_dates=set(panel_by_date),
            start=pd.Timestamp("2020-01-01"),
            end=pd.Timestamp("2020-01-02"),
            deposit_timing="beginning",
            cfg=FreeRealBacktestConfig(monthly_deposit=20_000, min_trading_days=1),
        )


class _FakeDerivativeCoordinator:
    def __init__(
        self,
        account: Account,
        *,
        reserve_on_increase: float = 0.0,
        eod_statuses: dict[str, tuple[float | None, str, float]] | None = None,
    ) -> None:
        self.account = account
        self.reserve_on_increase = reserve_on_increase
        self.eod_statuses = eod_statuses or {}
        self.pending = False
        self.derivative_value = 0.0
        self.events: list[tuple[str, str, bool | None]] = []

    def execute_reductions(self, trade_date: str) -> dict[str, float]:
        self.events.append(("reduce", trade_date, None))
        return {"derivative_reduction_phases": 1.0 if self.pending else 0.0}

    def execute_increases(self, trade_date: str) -> dict[str, float]:
        self.events.append(("increase", trade_date, None))
        if not self.pending or self.derivative_value > 0.0:
            return {"derivative_increase_phases": 0.0}
        self.account.cash -= self.reserve_on_increase
        self.derivative_value = self.reserve_on_increase
        return {"derivative_increase_phases": 1.0}

    def settle_end_of_day(
        self,
        trade_date: str,
        stock_prices: dict[str, float],
    ) -> DerivativeEndOfDaySnapshot:
        self.events.append(("settle", trade_date, None))
        override_nav, status, shortfall = self.eod_statuses.get(
            trade_date,
            (None, "ok", 0.0),
        )
        nav = (
            self.account.total_equity(stock_prices) + self.derivative_value
            if override_nav is None
            else override_nav
        )
        return DerivativeEndOfDaySnapshot(
            nav=float(nav),
            margin_status=status,
            margin_shortfall=shortfall,
        )

    def latch_close_signal(
        self,
        trade_date: str,
        nav: float,
        *,
        force_flat: bool,
    ) -> dict[str, float]:
        self.events.append(("latch", trade_date, force_flat))
        self.pending = not force_flat
        return {"derivative_signal_latches": 1.0}


def test_derivative_coordinator_reserves_cash_before_stock_buys() -> None:
    panel_by_date = {
        "20200101": {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)},
        "20200102": {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)},
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )
    created: list[_FakeDerivativeCoordinator] = []

    def coordinator_factory(account: Account) -> _FakeDerivativeCoordinator:
        coordinator = _FakeDerivativeCoordinator(account, reserve_on_increase=5_000.0)
        created.append(coordinator)
        return coordinator

    equity, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={"20200101": ["600001.SH"]},
        rebalance_dates=set(panel_by_date),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-02"),
        deposit_timing="beginning",
        cfg=cfg,
        derivative_coordinator_factory=coordinator_factory,
    )

    assert metrics["traded_notional"] == 15_000.0
    assert metrics["derivative_increase_phases"] == 1.0
    assert equity.iloc[-1]["wealth"] == 20_000.0
    day_two_events = [event[0] for event in created[0].events if event[1] == "20200102"]
    assert day_two_events == ["reduce", "increase", "settle"]


def test_strategy_evaluator_propagates_account_and_derivative_factories() -> None:
    spec = RealStockStrategySpec(
        name="S2_with_derivative_factory",
        family="S2_real_stock_momentum",
        params={
            "kind": "real_stock_momentum",
            "holding_k": 1,
            "rebalance": "daily",
            "min_listing_days": 0,
            "min_price": 0.0,
            "min_avg_amount_20d": 1.0,
        },
    )
    cfg = FreeRealBacktestConfig(
        monthly_deposit=10_000,
        target_month_12=120_000,
        target_month_24=240_000,
        window_months=1,
        min_trading_days=10,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )
    created: list[_FakeDerivativeCoordinator] = []

    def coordinator_factory(account: Account) -> _FakeDerivativeCoordinator:
        coordinator = _FakeDerivativeCoordinator(account)
        created.append(coordinator)
        return coordinator

    windows = evaluate_free_real_strategy(
        synthetic_panel(),
        spec,
        cfg,
        deposit_timings=("beginning",),
        max_windows=1,
        account_factory=CombinedAccount,
        derivative_coordinator_factory=coordinator_factory,
    )

    assert len(created) == 1
    assert isinstance(created[0].account, CombinedAccount)
    assert windows.iloc[0]["derivative_coordinator_active"] == 1.0
    assert windows.iloc[0]["derivative_signal_latches"] > 0.0


def test_derivative_default_is_absorbing_and_blocks_future_deposits() -> None:
    panel_by_date = {
        "20200131": {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)},
        "20200203": {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)},
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=30_000,
        window_months=1,
        min_trading_days=1,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )
    created: list[_FakeDerivativeCoordinator] = []

    def coordinator_factory(account: Account) -> _FakeDerivativeCoordinator:
        coordinator = _FakeDerivativeCoordinator(
            account,
            eod_statuses={"20200131": (-100.0, "default", 100.0)},
        )
        created.append(coordinator)
        return coordinator

    equity, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={},
        rebalance_dates=set(),
        start=pd.Timestamp("2020-01-31"),
        end=pd.Timestamp("2020-02-03"),
        deposit_timing="beginning",
        cfg=cfg,
        derivative_coordinator_factory=coordinator_factory,
    )

    assert equity["wealth"].tolist() == [0.0, 0.0]
    assert equity["defaulted"].tolist() == [True, True]
    assert equity.iloc[0]["raw_wealth"] == -100.0
    assert equity.iloc[1]["raw_wealth"] == -100.0
    assert metrics["default_events"] == 1.0
    assert metrics["raw_default_nav"] == -100.0
    assert metrics["max_margin_shortfall"] == 100.0
    assert created[0].account.cash == 30_000.0
    assert not any(event[0] == "latch" for event in created[0].events)


def test_derivative_margin_call_forces_the_next_signal_flat() -> None:
    panel_by_date = {
        "20200101": {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)},
        "20200102": {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)},
    }
    cfg = FreeRealBacktestConfig(monthly_deposit=30_000, min_trading_days=1)
    created: list[_FakeDerivativeCoordinator] = []

    def coordinator_factory(account: Account) -> _FakeDerivativeCoordinator:
        coordinator = _FakeDerivativeCoordinator(
            account,
            eod_statuses={"20200101": (30_000.0, "margin_call", 2_500.0)},
        )
        created.append(coordinator)
        return coordinator

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={},
        rebalance_dates=set(),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-02"),
        deposit_timing="beginning",
        cfg=cfg,
        derivative_coordinator_factory=coordinator_factory,
    )

    assert ("latch", "20200101", True) in created[0].events
    assert metrics["margin_call_days"] == 1.0
    assert metrics["max_margin_shortfall"] == 2_500.0


def test_participation_cap_blocks_order_when_cap_is_below_one_lot() -> None:
    panel_by_date = {
        "20200101": {"600001.SH": _execution_row("600001.SH", 10.0, 5_000.0)},
        "20200102": {"600001.SH": _execution_row("600001.SH", 10.0, 5_000.0)},
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
        max_daily_amount_participation=0.10,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={"20200101": ["600001.SH"]},
        rebalance_dates=set(panel_by_date),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-02"),
        deposit_timing="beginning",
        cfg=cfg,
    )

    assert metrics["filled_orders"] == 0.0
    assert metrics["participation_blocked_orders"] == 1.0
    assert metrics["participation_blocked_notional"] == 20_000.0
    assert metrics["participation_clipped_orders"] == 0.0
    assert metrics["rejected_orders"] == 0.0


def test_participation_cap_uses_prior_signal_day_amount() -> None:
    panel_by_date = {
        "20200101": {"600001.SH": _execution_row("600001.SH", 10.0, 5_000.0)},
        "20200102": {"600001.SH": _execution_row("600001.SH", 10.0, 5_000_000.0)},
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
        max_daily_amount_participation=0.10,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={"20200101": ["600001.SH"]},
        rebalance_dates=set(panel_by_date),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-02"),
        deposit_timing="beginning",
        cfg=cfg,
    )

    assert metrics["filled_orders"] == 0.0
    assert metrics["participation_blocked_orders"] == 1.0


def test_sell_liquidity_accounts_for_blocked_and_clipped_notional() -> None:
    symbols = ["600001.SH", "600002.SH", "600003.SH"]
    panel_by_date = {
        "20200101": {
            symbol: _execution_row(symbol, 10.0, 1_000_000.0) for symbol in symbols
        },
        "20200102": {
            "600001.SH": _execution_row("600001.SH", 10.0, 100_000.0),
            "600002.SH": _execution_row("600002.SH", 10.0, 5_000.0),
            "600003.SH": _execution_row("600003.SH", 10.0, 1_000_000.0),
        },
        "20200103": {
            symbol: _execution_row(symbol, 10.0, 1_000_000.0) for symbol in symbols
        },
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=40_000,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
        max_daily_amount_participation=0.10,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={
            "20200101": ["600001.SH", "600002.SH"],
            "20200102": ["600003.SH"],
        },
        rebalance_dates=set(panel_by_date),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-03"),
        deposit_timing="beginning",
        cfg=cfg,
    )

    assert metrics["requested_sell_notional"] == 40_000.0
    assert metrics["participation_clipped_notional"] == 10_000.0
    assert metrics["participation_blocked_notional"] == 20_000.0
    assert metrics["participation_unfilled_sell_notional"] == 30_000.0
    assert metrics["participation_unfilled_sell_share"] == pytest.approx(0.75)


def test_stateful_rebalance_executes_only_on_target_change_or_month_start() -> None:
    dates = ["20200101", "20200102", "20200103", "20200106"]
    panel_by_date = {
        trade_date: {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)}
        for trade_date in dates
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=dates,
        top_by_signal_date={trade_date: ["600001.SH"] for trade_date in dates},
        rebalance_dates=set(dates),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-06"),
        deposit_timing="beginning",
        cfg=cfg,
        rebalance_only_on_target_change=True,
    )

    assert metrics["rebalance_checks"] == 4.0
    assert metrics["rebalance_executions"] == 2.0
    assert metrics["filled_orders"] == 1.0


def test_rejected_target_order_retries_on_next_tradable_day() -> None:
    dates = ["20200101", "20200102", "20200103"]
    panel_by_date = {
        "20200101": {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)},
        "20200102": {
            "600001.SH": {
                **_execution_row("600001.SH", 10.0, 1_000_000.0),
                "up_limit": 10.0,
            }
        },
        "20200103": {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)},
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=dates,
        top_by_signal_date={"20200101": ["600001.SH"]},
        rebalance_dates={"20200101", "20200102"},
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-03"),
        deposit_timing="beginning",
        cfg=cfg,
        rebalance_only_on_target_change=True,
    )

    assert metrics["rejected_limit_up"] == 1.0
    assert metrics["filled_orders"] == 1.0
    assert metrics["rebalance_retry_executions"] == 1.0


def test_partial_target_order_retries_until_target_is_filled() -> None:
    dates = ["20200101", "20200102", "20200103", "20200106"]
    panel_by_date = {
        trade_date: {"600001.SH": _execution_row("600001.SH", 10.0, 1_000_000.0)}
        for trade_date in dates
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=30_000,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=10_000,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=dates,
        top_by_signal_date={"20200101": ["600001.SH"]},
        rebalance_dates={"20200101", "20200102"},
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-06"),
        deposit_timing="beginning",
        cfg=cfg,
        rebalance_only_on_target_change=True,
    )

    assert metrics["filled_orders"] == 3.0
    assert metrics["clipped_orders"] == 2.0
    assert metrics["rebalance_retry_executions"] == 2.0


def test_target_window_counts_execution_constraints() -> None:
    def row(
        symbol: str,
        open_price: float,
        *,
        close_price: float | None = None,
        suspended: bool = False,
        up_limit: float | None = None,
        down_limit: float | None = None,
    ) -> dict[str, object]:
        close = open_price if close_price is None else close_price
        return {
            "ts_code": symbol,
            "open": open_price,
            "close": close,
            "is_suspended": suspended,
            "up_limit": open_price * 1.1 if up_limit is None else up_limit,
            "down_limit": open_price * 0.9 if down_limit is None else down_limit,
        }

    panel_by_date = {
        "20200101": {
            "600001.SH": row("600001.SH", 50.0),
            "600002.SH": row("600002.SH", 10.0),
            "600003.SH": row("600003.SH", 20.0),
        },
        "20200102": {
            "600001.SH": row("600001.SH", 50.0),
            "600002.SH": row("600002.SH", 10.0),
            "600003.SH": row("600003.SH", 20.0),
        },
        "20200103": {
            "600001.SH": row("600001.SH", 45.0, down_limit=45.0),
            "600002.SH": row("600002.SH", 10.0, suspended=True),
            "600003.SH": row("600003.SH", 20.0),
        },
        "20200106": {
            "600001.SH": row("600001.SH", 46.0),
            "600002.SH": row("600002.SH", 10.0),
            "600003.SH": row("600003.SH", 20.0, up_limit=20.0),
        },
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        window_months=1,
        min_trading_days=2,
        lot_size=100,
        max_order_notional=6_000,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={
            "20200101": ["600001.SH"],
            "20200102": ["600002.SH"],
            "20200103": ["600003.SH"],
        },
        rebalance_dates=set(panel_by_date),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-06"),
        deposit_timing="beginning",
        cfg=cfg,
    )

    assert metrics["filled_orders"] == 2.0
    assert metrics["clipped_orders"] == 1.0
    assert metrics["rejected_limit_down"] == 1.0
    assert metrics["rejected_suspended"] == 1.0
    assert metrics["rejected_limit_up"] == 1.0

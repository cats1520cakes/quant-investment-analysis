from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant_proof.free_real_backtest import (
    FreeRealBacktestConfig,
    _prepare_daily_panel,
    simulate_free_real_window,
)
from quant_proof.free_sources.validators import strategy_allowed_in_tier
from quant_proof.real_strategies import (
    RealStockStrategySpec,
    compute_real_stock_scores,
    target_symbols_by_signal_date,
)
from quant_proof.search_manager import (
    build_search_strategy_specs,
    load_search_config,
    parse_search_stages,
)


ROOT = Path(__file__).resolve().parents[1]
EVENT_INDEX = 21
SYMBOL = "600001.SH"


def _post_limit_panel(periods: int = 27) -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-02", periods=periods)
    rows: list[dict[str, object]] = []
    previous_close = 10.0
    for index, trade_date in enumerate(dates):
        pre_close = previous_close
        up_limit = round(pre_close * 1.10, 2)
        down_limit = round(pre_close * 0.90, 2)
        close = pre_close
        high = round(pre_close * 1.01, 2)
        amount = 30_000_000.0
        if index == EVENT_INDEX:
            high = up_limit
            close = round(up_limit * 0.99, 2)
            amount = 60_000_000.0
        pct_chg = (close / pre_close - 1.0) * 100.0
        rows.append(
            {
                "trade_date": trade_date.strftime("%Y%m%d"),
                "ts_code": SYMBOL,
                "open": pre_close,
                "high": max(high, close),
                "low": round(min(pre_close, close) * 0.99, 2),
                "close": close,
                "pre_close": pre_close,
                "volume": amount / close,
                "amount": amount,
                "turnover_rate": 2.0,
                "pct_chg": pct_chg,
                "pe_ttm": 12.0,
                "pb": 1.2,
                "ps_ttm": 2.0,
                "pcf_ttm": 5.0,
                "adj_close_for_signal": close,
                "corporate_action_share_factor": 1.0,
                "trade_status": 1,
                "is_suspended": False,
                "is_st": False,
                "delisting_exit_required": False,
                "listing_days": 300 + index,
                "up_limit": up_limit,
                "down_limit": down_limit,
                "circ_mv_approx": 1_500_000_000.0,
                "data_tier": "free_real",
            }
        )
        previous_close = close
    return pd.DataFrame(rows)


def _spec(**overrides: object) -> RealStockStrategySpec:
    params: dict[str, object] = {
        "kind": "real_post_limit_release",
        "data_tier": "free_real_derived_limits",
        "rebalance": "daily",
        "entry_rebalance": "daily",
        "lookback": 20,
        "min_close_below_limit_pct": 0.0005,
        "max_close_to_limit_pct": 0.02,
        "min_amount_ratio": 1.5,
        "min_previous_amount": 20_000_000.0,
        "min_listing_days": 180,
        "min_price": 3.0,
        "min_avg_amount_20d": 20_000_000.0,
        "exclude_st": True,
        "holding_k": 1,
        "max_holding_days": 5,
        "trailing_stop": 0.06,
        "exit_low_window": 3,
    }
    params.update(overrides)
    return RealStockStrategySpec(
        name="S31_real_post_limit_release_test",
        family="S31_real_post_limit_release",
        params=params,
    )


def _event_row(frame: pd.DataFrame) -> pd.Series:
    return frame.iloc[EVENT_INDEX]


def test_post_limit_release_signal_is_causal_under_future_mutation() -> None:
    panel = _post_limit_panel()
    spec = _spec()
    baseline = compute_real_stock_scores(panel, spec)
    mutated = panel.copy()
    future = mutated.index > EVENT_INDEX
    mutated.loc[future, "amount"] = 1.0
    mutated.loc[future, "adj_close_for_signal"] = 1.0
    mutated.loc[future, "close"] = 1.0
    mutated.loc[future, "high"] = 50.0
    mutated.loc[future, "up_limit"] = 50.0
    mutated.loc[future, "trade_status"] = 0

    changed = compute_real_stock_scores(mutated, spec)
    columns = [
        "post_limit_amount_baseline",
        "post_limit_amount_ratio",
        "post_limit_close_gap",
        "entry_signal",
        "exit_signal",
        "rank_score",
    ]
    pd.testing.assert_frame_equal(
        baseline.loc[:EVENT_INDEX, columns].reset_index(drop=True),
        changed.loc[:EVENT_INDEX, columns].reset_index(drop=True),
    )
    event_date = str(panel.iloc[EVENT_INDEX]["trade_date"])
    baseline_targets = target_symbols_by_signal_date(baseline, spec=spec, holding_k=1)
    changed_targets = target_symbols_by_signal_date(changed, spec=spec, holding_k=1)
    assert baseline_targets[event_date] == changed_targets[event_date] == [SYMBOL]


def test_post_limit_release_sealed_at_close_does_not_trigger() -> None:
    panel = _post_limit_panel()
    panel.loc[EVENT_INDEX, "close"] = panel.loc[EVENT_INDEX, "up_limit"]
    panel.loc[EVENT_INDEX, "adj_close_for_signal"] = panel.loc[EVENT_INDEX, "close"]
    panel.loc[EVENT_INDEX, "pct_chg"] = (
        panel.loc[EVENT_INDEX, "close"] / panel.loc[EVENT_INDEX, "pre_close"] - 1.0
    ) * 100.0

    event = _event_row(compute_real_stock_scores(panel, _spec()))

    assert bool(event["post_limit_touched"])
    assert not bool(event["post_limit_released"])
    assert not bool(event["entry_signal"])
    assert pd.isna(event["rank_score"])


def test_post_limit_release_event_triggers_and_thresholds_change_behavior() -> None:
    panel = _post_limit_panel()
    event = _event_row(compute_real_stock_scores(panel, _spec()))

    assert bool(event["post_limit_touched"])
    assert bool(event["post_limit_released"])
    assert bool(event["entry_signal"])
    assert event["previous_amount"] == 30_000_000.0
    assert event["post_limit_amount_ratio"] == pytest.approx(2.0)

    low_volume = panel.copy()
    low_volume.loc[EVENT_INDEX, "amount"] = 36_000_000.0
    strict_event = _event_row(compute_real_stock_scores(low_volume, _spec()))
    relaxed_event = _event_row(
        compute_real_stock_scores(low_volume, _spec(min_amount_ratio=1.0))
    )
    assert not bool(strict_event["entry_signal"])
    assert bool(relaxed_event["entry_signal"])


def test_post_limit_release_stateful_exit_parameters_change_targets() -> None:
    panel = _post_limit_panel()
    event_date = str(panel.loc[EVENT_INDEX, "trade_date"])
    exit_index = EVENT_INDEX + 2
    exit_date = str(panel.loc[exit_index, "trade_date"])

    long_spec = _spec(max_holding_days=20, trailing_stop=0.0)
    timed_spec = _spec(max_holding_days=2, trailing_stop=0.0)
    long_targets = target_symbols_by_signal_date(
        compute_real_stock_scores(panel, long_spec), spec=long_spec, holding_k=1
    )
    timed_targets = target_symbols_by_signal_date(
        compute_real_stock_scores(panel, timed_spec), spec=timed_spec, holding_k=1
    )
    assert long_targets[event_date] == timed_targets[event_date] == [SYMBOL]
    assert long_targets[exit_date] == [SYMBOL]
    assert timed_targets[exit_date] == []

    pullback = panel.copy()
    pullback.loc[exit_index, "close"] = 10.35
    pullback.loc[exit_index, "adj_close_for_signal"] = 10.35
    pullback.loc[exit_index, "low"] = 10.25
    pullback.loc[exit_index, "pct_chg"] = (
        10.35 / pullback.loc[exit_index, "pre_close"] - 1.0
    ) * 100.0
    short_low_spec = _spec(
        exit_low_window=2,
        max_holding_days=20,
        trailing_stop=0.0,
    )
    wide_low_spec = _spec(
        exit_low_window=5,
        max_holding_days=20,
        trailing_stop=0.0,
    )
    trailing_spec = _spec(
        exit_low_window=5,
        max_holding_days=20,
        trailing_stop=0.04,
    )
    short_low_targets = target_symbols_by_signal_date(
        compute_real_stock_scores(pullback, short_low_spec),
        spec=short_low_spec,
        holding_k=1,
    )
    wide_low_targets = target_symbols_by_signal_date(
        compute_real_stock_scores(pullback, wide_low_spec),
        spec=wide_low_spec,
        holding_k=1,
    )
    trailing_targets = target_symbols_by_signal_date(
        compute_real_stock_scores(pullback, trailing_spec),
        spec=trailing_spec,
        holding_k=1,
    )
    assert short_low_targets[exit_date] == []
    assert wide_low_targets[exit_date] == [SYMBOL]
    assert trailing_targets[exit_date] == []


@pytest.mark.parametrize(
    "exit_changes",
    [
        {"trade_status": 0, "is_suspended": True},
        {"delisting_exit_required": True},
    ],
)
def test_post_limit_release_suspension_and_delisting_exit_state(
    exit_changes: dict[str, object],
) -> None:
    panel = _post_limit_panel()
    exit_index = EVENT_INDEX + 1
    for column, value in exit_changes.items():
        panel.loc[exit_index, column] = value
    spec = _spec(max_holding_days=20, trailing_stop=0.0)

    scores = compute_real_stock_scores(panel, spec)
    targets = target_symbols_by_signal_date(scores, spec=spec, holding_k=1)
    event_date = str(panel.loc[EVENT_INDEX, "trade_date"])
    exit_date = str(panel.loc[exit_index, "trade_date"])

    assert targets[event_date] == [SYMBOL]
    assert bool(scores.loc[exit_index, "exit_signal"])
    assert targets[exit_date] == []


def test_post_limit_release_next_open_limit_up_retries_pending_rebalance() -> None:
    panel = _post_limit_panel()
    first_execution_index = EVENT_INDEX + 1
    retry_index = EVENT_INDEX + 2
    panel.loc[first_execution_index, "open"] = panel.loc[first_execution_index, "up_limit"]
    panel.loc[retry_index, "open"] = panel.loc[retry_index, "close"]
    spec = _spec(max_holding_days=20, trailing_stop=0.0)
    scores = compute_real_stock_scores(panel, spec)
    targets = target_symbols_by_signal_date(scores, spec=spec, holding_k=1)
    trading_dates = panel["trade_date"].astype(str).tolist()
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000.0,
        window_months=1,
        min_trading_days=1,
        lot_size=100,
        max_order_notional=None,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
        max_daily_amount_participation=0.05,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=_prepare_daily_panel(panel),
        trading_dates=trading_dates,
        top_by_signal_date=targets,
        rebalance_dates=set(trading_dates),
        start=pd.Timestamp(panel.iloc[0]["trade_date"]),
        end=pd.Timestamp(panel.iloc[retry_index]["trade_date"]),
        deposit_timing="beginning",
        cfg=cfg,
        rebalance_only_on_target_change=True,
    )

    assert metrics["rejected_limit_up"] == 1.0
    assert metrics["filled_orders"] == 1.0
    assert metrics["rebalance_retry_executions"] == 1.0
    assert metrics["participation_blocked_orders"] == 0.0


def test_post_limit_release_search_is_capped_and_confirm_is_local() -> None:
    raw = load_search_config(ROOT / "config" / "phase3_post_limit_release_search.yaml")
    specs = build_search_strategy_specs(raw)
    stages = {stage.name: stage for stage in parse_search_stages(raw)}
    space = raw["strategy_spaces"]["S31_real_post_limit_release"]

    assert len(specs) == space["budget"] == 12
    assert {spec.params["data_tier"] for spec in specs} == {
        "free_real_derived_limits"
    }
    assert set(space["parameters"]) == {
        "holding_k",
        "max_close_to_limit_pct",
        "min_amount_ratio",
        "max_holding_days",
        "trailing_stop",
        "exit_low_window",
    }
    assert not stages["screen"].expand_parameter_neighbors
    assert stages["confirm"].expand_parameter_neighbors
    assert stages["confirm"].require_neighborhood_gate
    assert strategy_allowed_in_tier(
        "S31_real_post_limit_release", "free_real_derived_limits"
    ).allowed
    assert not strategy_allowed_in_tier(
        "S31_real_post_limit_release", "free_real"
    ).allowed
    assert not strategy_allowed_in_tier(
        "S31_real_post_limit_release", "proxy_research"
    ).allowed

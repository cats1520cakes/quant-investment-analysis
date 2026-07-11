from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quant_proof.cffex_catalog import CffexCatalog
from quant_proof.cffex_execution_parameters import CffexExecutionParameterSchedule
from quant_proof.derivative_event_loop import (
    DevelopmentResearchAssumptions,
    DerivativeEventLoop,
)
from quant_proof.engine.combined_account import CombinedAccount
from quant_proof.free_real_backtest import DerivativeEndOfDaySnapshot
from quant_proof.phase3_overlay_coordinator import (
    FuturesOverlaySpec,
    LongOptionOverlaySpec,
    Phase3OverlayCoordinator,
)


P0 = "20260102"
D0 = "20260105"
D1 = "20260106"
D2 = "20260107"


def _row(
    trade_date: str,
    contract: str,
    product: str,
    *,
    multiplier: float,
    open_price: float | None = 100.0,
    settle: float | None = 100.0,
    volume: float = 100.0,
    open_interest: float = 100.0,
    option_type: str = "",
    strike: float | None = None,
    delta: float | None = None,
    open_executable: bool = True,
    settlement_mark_valid: bool = True,
) -> dict[str, object]:
    is_future = product in {"IF", "IH", "IC", "IM"}
    return {
        "trade_date": trade_date,
        "contract": contract,
        "product": product,
        "instrument_type": "future" if is_future else "option",
        "option_type": "" if is_future else option_type,
        "strike": None if is_future else strike,
        "multiplier": multiplier,
        "open": open_price,
        "settle": settle,
        "volume": volume,
        "open_interest": open_interest,
        "delta": None if is_future else delta,
        "open_executable": open_executable,
        "settlement_mark_valid": settlement_mark_valid,
    }


def _catalog(
    rows: list[dict[str, object]],
    expiries: dict[str, str],
) -> CffexCatalog:
    master = pd.DataFrame(
        [
            {"contract": contract, "last_trade_date": expiry}
            for contract, expiry in expiries.items()
        ]
    )
    return CffexCatalog.from_frames(pd.DataFrame(rows), master)


def _assumptions(
    *,
    participation: float = 1.0,
) -> DevelopmentResearchAssumptions:
    return DevelopmentResearchAssumptions(
        futures_initial_margin_rate=0.10,
        futures_maintenance_margin_rate=0.08,
        futures_slippage_bps=1.0,
        option_slippage_bps=1.0,
        futures_fee_per_contract=2.0,
        option_fee_per_contract=1.0,
        prior_day_volume_participation=participation,
    )


def _execution_schedule(
    tmp_path: Path,
    *,
    snapshot_date: str,
    contract: str,
    margin_rate: float,
) -> CffexExecutionParameterSchedule:
    path = tmp_path / "cffex_settlement_params.parquet"
    pd.DataFrame(
        [
            {
                "snapshot_date": snapshot_date,
                "instrument_type": "future",
                "parameter_scope": "contract",
                "contract_or_series": contract,
                "product": contract[:2],
                "long_margin_rate": margin_rate,
                "short_margin_rate": margin_rate,
                "trading_fee_value": 0.001,
                "trading_fee_unit": "notional_rate",
                "settlement_fee_value": 0.0001,
                "settlement_fee_unit": "notional_rate",
                "settlement_fee_kind": "delivery",
                "close_today_fee_multiplier": 10.0,
                "close_today_fee_semantics": "fraction_of_trading_fee",
                "option_shorting_enabled": False,
                "source_section_title_matches_snapshot": True,
                "source_sha256": "b" * 64,
            }
        ]
    ).to_parquet(path, index=False)
    return CffexExecutionParameterSchedule(path, validate=False)


def _future_catalog(
    *,
    product: str = "IF",
    contract: str = "IF2602",
    signal_settle: float = 100.0,
    next_open: float = 100.0,
    dates: tuple[str, ...] = (D0, D1),
) -> CffexCatalog:
    rows = [
        _row(
            trade_date,
            contract,
            product,
            multiplier=10.0,
            settle=signal_settle,
            open_price=next_open if index else 80.0,
        )
        for index, trade_date in enumerate(dates)
    ]
    return _catalog(rows, {contract: "20260320"})


class RecordingAccount(CombinedAccount):
    def __init__(self, cash: float) -> None:
        super().__init__(cash=cash)
        self.derivative_actions: list[tuple[str, int | None]] = []

    def adjust_futures_position(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        self.derivative_actions.append(
            ("future", int(kwargs["target_contracts"]))
        )
        return super().adjust_futures_position(*args, **kwargs)

    def close_option(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        self.derivative_actions.append(("option_close", None))
        return super().close_option(*args, **kwargs)

    def buy_option(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        self.derivative_actions.append(("option_buy", None))
        return super().buy_option(*args, **kwargs)


def test_specs_are_frozen_normalized_and_reject_ambiguous_sizing() -> None:
    directions = {"2026-01-05": "SHORT", D1: "flat"}
    futures = FuturesOverlaySpec(
        product="if",
        fixed_contracts=2,
        direction="long",
        direction_by_signal_date=directions,
        rebalance_frequency="WEEKLY",
        cash_buffer_pct=0.25,
        max_contracts=4,
    )
    option = LongOptionOverlaySpec(
        product="mo",
        option_type="P",
        target_abs_delta=0.35,
        min_dte=10,
        max_dte=45,
        budget_pct_nav=0.02,
        exit_dte=5,
    )

    assert futures.product == "IF"
    assert futures.target_mode == "fixed_contracts"
    assert futures.direction_on(D0) == "short"
    assert futures.direction_on(D2) == "flat"
    assert futures.rebalance_frequency == "weekly"
    assert option.product == "MO"
    assert option.option_type == "put"
    with pytest.raises(FrozenInstanceError):
        futures.product = "IH"  # type: ignore[misc]
    with pytest.raises(TypeError):
        assert futures.direction_by_signal_date is not None
        futures.direction_by_signal_date[D0] = "long"  # type: ignore[index]
    with pytest.raises(ValueError, match="exactly one"):
        FuturesOverlaySpec(
            product="IF",
            fixed_contracts=1,
            margin_budget=1_000.0,
        )


def test_coordinator_requires_the_event_loop_shared_account() -> None:
    catalog = _future_catalog()
    first = CombinedAccount(cash=10_000.0)
    second = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(first, catalog, _assumptions())

    with pytest.raises(ValueError, match="share the same"):
        Phase3OverlayCoordinator(
            second,
            loop,
            FuturesOverlaySpec(product="IF", fixed_contracts=1),
        )


def test_signal_close_plans_short_integer_target_for_next_open_only() -> None:
    catalog = _future_catalog(signal_settle=100.0, next_open=500.0)
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        FuturesOverlaySpec(
            product="IF",
            direction="short",
            fixed_contracts=3,
            min_dte=0,
            rebalance_frequency="daily",
        ),
    )

    latch = coordinator.latch_close_signal(D0, 10_000.0, force_flat=False)

    assert account.futures_positions == {}
    assert coordinator.pending_execution_dates == (D1,)
    pending = coordinator.pending_plans[D1]
    assert pending.signal_date == D0
    assert pending.futures_target_contracts == -3
    assert pending.futures_plan is not None
    assert pending.futures_plan.selection is not None
    assert pending.futures_plan.selection.settlement_price == 100.0
    assert latch["futures_plans"] == 1.0
    assert all(isinstance(value, float) for value in latch.values())

    reductions = coordinator.execute_reductions(D1)
    assert reductions["derivative_execution_events"] == 0.0
    increases = coordinator.execute_increases(D1)
    event = loop.execute_futures_rebalance(pending.futures_plan).events[0]
    assert event.signal_date == D0
    assert event.execution_date == D1
    assert event.reference_open_price == 500.0
    assert account.futures_positions["IF2602"].contracts == -3
    assert increases["derivative_filled_contracts"] == 3.0

    cash_after = account.free_cash
    assert coordinator.latch_close_signal(D0, 10_000.0, force_flat=False) == latch
    assert coordinator.execute_reductions(D1) == reductions
    assert coordinator.execute_increases(D1) == increases
    assert account.free_cash == cash_after
    assert account.futures_positions["IF2602"].contracts == -3


@pytest.mark.parametrize(
    ("mode", "value", "expected"),
    [
        ("fixed_contracts", 3, 3),
        ("target_notional_multiple", 0.50, 2),
        ("margin_budget", 350.0, 3),
    ],
)
def test_three_futures_sizing_modes_use_signal_settlement_and_whole_lots(
    mode: str,
    value: float,
    expected: int,
) -> None:
    catalog = _future_catalog(signal_settle=100.0, next_open=999.0)
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    kwargs = {mode: value}
    spec = FuturesOverlaySpec(
        product="IF",
        direction="long",
        min_dte=0,
        rebalance_frequency="daily",
        **kwargs,  # type: ignore[arg-type]
    )
    coordinator = Phase3OverlayCoordinator(account, loop, spec)

    counters = coordinator.latch_close_signal(D0, 5_000.0, force_flat=False)

    assert coordinator.pending_plans[D1].futures_target_contracts == expected
    assert counters["futures_sizing_truncated_contracts"] == 0.0


def test_futures_sizing_preserves_cash_buffer_and_honors_max_contracts() -> None:
    catalog = _future_catalog(signal_settle=100.0)
    account = CombinedAccount(cash=500.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        FuturesOverlaySpec(
            product="IF",
            fixed_contracts=10,
            min_dte=0,
            rebalance_frequency="daily",
            cash_buffer_pct=0.20,
            max_contracts=3,
        ),
    )

    counters = coordinator.latch_close_signal(D0, 10_000.0, force_flat=False)

    assert coordinator.pending_plans[D1].futures_target_contracts == 3
    assert counters["futures_sizing_truncated_contracts"] == 7.0


def test_futures_sizing_uses_signal_date_official_margin_rate(
    tmp_path: Path,
) -> None:
    contract = "IF2602"
    catalog = _future_catalog(contract=contract, signal_settle=100.0)
    schedule = _execution_schedule(
        tmp_path,
        snapshot_date=P0,
        contract=contract,
        margin_rate=0.40,
    )
    account = CombinedAccount(cash=500.0)
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(),
        execution_parameters=schedule,
    )
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        FuturesOverlaySpec(
            product="IF",
            fixed_contracts=10,
            min_dte=0,
            rebalance_frequency="daily",
        ),
    )

    counters = coordinator.latch_close_signal(D0, 10_000.0, force_flat=False)

    assert coordinator.pending_plans[D1].futures_target_contracts == 1
    assert counters["futures_sizing_truncated_contracts"] == 9.0


def test_reduction_phase_applies_official_margin_update_without_pending_order(
    tmp_path: Path,
) -> None:
    contract = "IF2602"
    catalog = _future_catalog(contract=contract, dates=(D0, D1))
    schedule = _execution_schedule(
        tmp_path,
        snapshot_date=P0,
        contract=contract,
        margin_rate=0.40,
    )
    account = CombinedAccount(cash=1_000.0)
    account.open_futures(
        contract,
        contracts=1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.10,
        trade_date=date(2026, 1, 5),
    )
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(),
        execution_parameters=schedule,
    )
    coordinator = Phase3OverlayCoordinator(account, loop)

    counters = coordinator.execute_reductions(D1)

    assert counters["futures_margin_rate_updates"] == 1.0
    assert counters["futures_margin_transfer"] == pytest.approx(300.0)
    assert account.futures_positions[contract].initial_margin_rate == pytest.approx(
        0.40
    )
    cash_after = account.free_cash
    assert coordinator.execute_reductions(D1) == counters
    assert account.free_cash == cash_after


@pytest.mark.parametrize(
    ("frequency", "second_day_plans"),
    [("daily", 1.0), ("weekly", 0.0), ("monthly", 0.0)],
)
def test_futures_rebalance_frequency_is_calendar_causal(
    frequency: str,
    second_day_plans: float,
) -> None:
    catalog = _future_catalog(dates=(D0, D1, D2))
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        FuturesOverlaySpec(
            product="IF",
            fixed_contracts=1,
            min_dte=0,
            rebalance_frequency=frequency,
        ),
    )

    first = coordinator.latch_close_signal(D0, 10_000.0, force_flat=False)
    second = coordinator.latch_close_signal(D1, 10_000.0, force_flat=False)

    assert first["futures_plans"] == 1.0
    assert second["futures_plans"] == second_day_plans


def test_execution_date_dte_prevents_opening_a_too_near_expiry_future() -> None:
    old_contract = "IF2601"
    new_contract = "IF2602"
    rows: list[dict[str, object]] = []
    for trade_date in (D0, D1, D2):
        rows.extend(
            [
                _row(
                    trade_date,
                    old_contract,
                    "IF",
                    multiplier=10.0,
                    open_interest=1_000.0,
                ),
                _row(
                    trade_date,
                    new_contract,
                    "IF",
                    multiplier=10.0,
                    open_interest=500.0,
                ),
            ]
        )
    catalog = _catalog(
        rows,
        {old_contract: D2, new_contract: "20260320"},
    )
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        FuturesOverlaySpec(
            product="IF",
            fixed_contracts=1,
            min_dte=2,
            rebalance_frequency="monthly",
        ),
    )

    coordinator.latch_close_signal(D0, 10_000.0, force_flat=False)
    coordinator.execute_reductions(D1)
    coordinator.execute_increases(D1)
    coordinator.settle_end_of_day(D1, {})
    second = coordinator.latch_close_signal(D1, 10_000.0, force_flat=False)

    assert account.futures_positions[new_contract].contracts == 1
    assert second["futures_expiry_roll_plans"] == 0.0


def test_held_future_roll_due_looks_through_a_long_holiday() -> None:
    first_signal = "20180213"
    holiday_eve = "20180214"
    expiry_execution = "20180222"
    old_contract = "IF1802"
    new_contract = "IF1803"
    rows: list[dict[str, object]] = []
    for trade_date in (first_signal, holiday_eve, expiry_execution):
        rows.extend(
            [
                _row(
                    trade_date,
                    old_contract,
                    "IF",
                    multiplier=10.0,
                    open_interest=1_000.0,
                ),
                _row(
                    trade_date,
                    new_contract,
                    "IF",
                    multiplier=10.0,
                    open_interest=500.0,
                ),
            ]
        )
    catalog = _catalog(
        rows,
        {old_contract: expiry_execution, new_contract: "20180316"},
    )
    account = CombinedAccount(cash=10_000.0)
    account.open_futures(
        old_contract,
        contracts=1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=date(2018, 2, 13),
    )
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        FuturesOverlaySpec(
            product="IF",
            fixed_contracts=1,
            min_dte=5,
            rebalance_frequency="monthly",
        ),
    )

    coordinator.latch_close_signal(first_signal, 10_000.0, force_flat=False)
    coordinator.execute_reductions(holiday_eve)
    coordinator.execute_increases(holiday_eve)
    coordinator.settle_end_of_day(holiday_eve, {})
    second = coordinator.latch_close_signal(
        holiday_eve,
        10_000.0,
        force_flat=False,
    )

    assert second["futures_expiry_roll_plans"] == 1.0
    plan = coordinator.pending_plans[expiry_execution].futures_plan
    assert plan is not None
    assert plan.selection is not None
    assert plan.selection.contract == new_contract


def test_futures_reversal_reduces_to_zero_before_opening_short() -> None:
    catalog = _future_catalog()
    account = RecordingAccount(cash=10_000.0)
    account.open_futures(
        "IF2602",
        contracts=2,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=date(2026, 1, 2),
    )
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        FuturesOverlaySpec(
            product="IF",
            direction="short",
            fixed_contracts=3,
            min_dte=0,
            rebalance_frequency="daily",
        ),
    )
    coordinator.latch_close_signal(D0, 10_000.0, force_flat=False)

    reductions = coordinator.execute_reductions(D1)

    assert reductions["derivative_filled_contracts"] == 2.0
    assert account.futures_positions == {}
    assert account.derivative_actions == [("future", 0)]

    increases = coordinator.execute_increases(D1)
    assert increases["derivative_filled_contracts"] == 3.0
    assert account.derivative_actions == [("future", 0), ("future", -3)]
    assert account.futures_positions["IF2602"].contracts == -3


def test_monthly_option_budget_is_fixed_at_signal_nav_and_bought_next_open() -> None:
    contract = "HO2602-P-2500"
    catalog = _catalog(
        [
            _row(
                D0,
                contract,
                "HO",
                multiplier=10.0,
                option_type="put",
                strike=2500.0,
                delta=-0.40,
                volume=100.0,
            ),
            _row(
                D1,
                contract,
                "HO",
                multiplier=10.0,
                option_type="put",
                strike=2500.0,
                delta=-0.40,
                open_price=5.0,
                volume=100.0,
            ),
        ],
        {contract: "20260220"},
    )
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        option_spec=LongOptionOverlaySpec(
            product="HO",
            option_type="put",
            target_abs_delta=0.40,
            min_dte=10,
            max_dte=60,
            budget_pct_nav=0.10,
            exit_dte=5,
        ),
    )

    latch = coordinator.latch_close_signal(D0, 1_000.0, force_flat=False)

    pending = coordinator.pending_plans[D1]
    assert pending.option_purchase_plan is not None
    assert pending.option_purchase_plan.order.cash_budget == 100.0
    assert pending.option_purchase_plan.execution_date == D1
    assert account.option_positions == {}
    assert latch["option_buy_plans"] == 1.0

    coordinator.execute_reductions(D1)
    increases = coordinator.execute_increases(D1)

    assert increases["derivative_filled_contracts"] == 1.0
    assert account.option_positions[contract].contracts == 1


def test_monthly_option_close_gate_blocks_buy_after_partial_close() -> None:
    old = "IO2601-C-4000"
    new = "IO2602-C-4300"
    rows = [
        _row(
            P0,
            old,
            "IO",
            multiplier=10.0,
            option_type="call",
            strike=4000.0,
            delta=0.90,
        ),
        _row(
            P0,
            new,
            "IO",
            multiplier=10.0,
            option_type="call",
            strike=4300.0,
            delta=0.50,
        ),
        _row(
            D0,
            old,
            "IO",
            multiplier=10.0,
            option_type="call",
            strike=4000.0,
            delta=0.90,
            volume=2.0,
        ),
        _row(
            D0,
            new,
            "IO",
            multiplier=10.0,
            option_type="call",
            strike=4300.0,
            delta=0.50,
            volume=100.0,
        ),
        _row(
            D1,
            old,
            "IO",
            multiplier=10.0,
            option_type="call",
            strike=4000.0,
            delta=0.90,
            open_price=5.0,
        ),
        _row(
            D1,
            new,
            "IO",
            multiplier=10.0,
            option_type="call",
            strike=4300.0,
            delta=0.50,
            open_price=10.0,
        ),
    ]
    catalog = _catalog(
        rows,
        {old: "20260120", new: "20260220"},
    )
    account = RecordingAccount(cash=10_000.0)
    account.buy_option(
        old,
        option_type="call",
        contracts=2,
        premium=4.0,
        strike=4000.0,
        expiry=date(2026, 1, 20),
        multiplier=10.0,
        trade_date=date(2026, 1, 2),
    )
    account.derivative_actions.clear()
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(participation=0.5),
    )
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        option_spec=LongOptionOverlaySpec(
            product="IO",
            option_type="call",
            target_abs_delta=0.50,
            min_dte=10,
            max_dte=60,
            budget_pct_nav=0.10,
            exit_dte=20,
        ),
    )

    latch = coordinator.latch_close_signal(D0, 10_000.0, force_flat=False)
    assert latch["option_close_plans"] == 1.0
    assert latch["option_buy_plans"] == 1.0
    assert set(account.option_positions) == {old}

    reductions = coordinator.execute_reductions(D1)
    assert reductions["derivative_filled_contracts"] == 1.0
    assert reductions["derivative_clipped_contracts"] == 1.0
    assert account.option_positions[old].contracts == 1

    increases = coordinator.execute_increases(D1)
    assert increases["option_buys_blocked"] == 1.0
    assert increases["derivative_rejected_orders"] == 1.0
    assert set(account.option_positions) == {old}
    assert account.derivative_actions == [("option_close", None)]
    assert "incomplete or failed close" in coordinator.audit_log[-1].reason

    cash_after = account.free_cash
    coordinator.execute_reductions(D1)
    coordinator.execute_increases(D1)
    assert account.free_cash == cash_after
    assert account.option_positions[old].contracts == 1


def test_force_flat_overrides_both_strategies_and_reduces_futures_first() -> None:
    option = "IO2602-P-4000"
    rows = [
        _row(
            P0,
            option,
            "IO",
            multiplier=10.0,
            option_type="put",
            strike=4000.0,
            delta=-0.50,
        ),
        _row(D0, "IF2602", "IF", multiplier=10.0, volume=100.0),
        _row(
            D0,
            option,
            "IO",
            multiplier=10.0,
            option_type="put",
            strike=4000.0,
            delta=-0.50,
            volume=100.0,
        ),
        _row(D1, "IF2602", "IF", multiplier=10.0, open_price=100.0),
        _row(
            D1,
            option,
            "IO",
            multiplier=10.0,
            option_type="put",
            strike=4000.0,
            delta=-0.50,
            open_price=5.0,
        ),
    ]
    catalog = _catalog(
        rows,
        {"IF2602": "20260320", option: "20260220"},
    )
    account = RecordingAccount(cash=10_000.0)
    account.open_futures(
        "IF2602",
        contracts=2,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=date(2026, 1, 2),
    )
    account.buy_option(
        option,
        option_type="put",
        contracts=1,
        premium=4.0,
        strike=4000.0,
        expiry=date(2026, 2, 20),
        multiplier=10.0,
        trade_date=date(2026, 1, 2),
    )
    account.derivative_actions.clear()
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        FuturesOverlaySpec(
            product="IF",
            direction="long",
            fixed_contracts=5,
            min_dte=0,
            rebalance_frequency="daily",
        ),
        LongOptionOverlaySpec(
            product="IO",
            option_type="put",
            target_abs_delta=0.50,
            min_dte=10,
            max_dte=60,
            budget_pct_nav=0.10,
            exit_dte=5,
        ),
    )

    latch = coordinator.latch_close_signal(D0, 10_000.0, force_flat=True)

    pending = coordinator.pending_plans[D1]
    assert pending.futures_target_contracts == 0
    assert pending.option_purchase_plan is None
    assert latch["derivative_force_flat_signals"] == 1.0
    assert latch["option_buy_plans"] == 0.0

    coordinator.execute_reductions(D1)
    assert account.futures_positions == {}
    assert account.option_positions == {}
    assert account.derivative_actions == [
        ("future", 0),
        ("option_close", None),
    ]
    coordinator.execute_increases(D1)
    assert account.futures_positions == {}
    assert account.option_positions == {}


def test_eod_conversion_returns_protocol_snapshot_and_margin_value() -> None:
    catalog = _catalog(
        [_row(D0, "IF2602", "IF", multiplier=10.0, settle=90.0)],
        {"IF2602": "20260320"},
    )
    account = CombinedAccount(cash=120.0)
    account.open_futures(
        "IF2602",
        contracts=1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=date(2026, 1, 2),
    )
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(account, loop)

    snapshot = coordinator.settle_end_of_day(D0, {})

    assert isinstance(snapshot, DerivativeEndOfDaySnapshot)
    assert snapshot.nav == pytest.approx(20.0)
    assert snapshot.margin_status == "margin_call"
    assert snapshot.margin_shortfall > 0.0
    assert coordinator.settle_end_of_day(D0, {}) == snapshot


def test_missing_catalog_fails_closed_with_float_counters_and_audit() -> None:
    catalog = _catalog(
        [
            _row(D0, "IH2602", "IH", multiplier=10.0),
            _row(D1, "IH2602", "IH", multiplier=10.0),
        ],
        {"IH2602": "20260320"},
    )
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    coordinator = Phase3OverlayCoordinator(
        account,
        loop,
        FuturesOverlaySpec(
            product="IF",
            fixed_contracts=1,
            min_dte=0,
            rebalance_frequency="daily",
        ),
    )

    counters = coordinator.latch_close_signal(D0, 10_000.0, force_flat=False)

    assert counters["derivative_missing_catalog_events"] == 1.0
    assert counters["derivative_plan_rejections"] == 1.0
    assert all(isinstance(value, float) for value in counters.values())
    assert coordinator.pending_execution_dates == ()
    assert account.futures_positions == {}
    assert coordinator.audit_log[-1].fail_closed is True
    assert "no valid IF future" in coordinator.audit_log[-1].reason

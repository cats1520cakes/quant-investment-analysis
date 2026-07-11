from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quant_proof.cffex_catalog import CffexCatalog
from quant_proof.cffex_execution_parameters import CffexExecutionParameterSchedule
from quant_proof.derivative_event_loop import (
    DEVELOPMENT_SCOPE_LIMITATIONS,
    DevelopmentResearchAssumptions,
    DerivativeEventError,
    DerivativeEventLoop,
    EventStatus,
    FuturesRebalanceOrder,
    LongOptionCloseOrder,
    LongOptionPurchaseOrder,
    MissingExecutionParameterError,
    MissingSettlementMarkError,
)
from quant_proof.engine.combined_account import CombinedAccount, MarginStatus


P0 = "20251231"
D0 = "20260102"
D1 = "20260105"
D2 = "20260106"
D3 = "20260107"
D4 = "20260108"
D5 = "20260109"


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


def _assumptions(**overrides: float) -> DevelopmentResearchAssumptions:
    values = {
        "futures_initial_margin_rate": 0.10,
        "futures_maintenance_margin_rate": 0.08,
        "futures_slippage_bps": 100.0,
        "option_slippage_bps": 100.0,
        "futures_fee_per_contract": 2.0,
        "option_fee_per_contract": 1.0,
        "prior_day_volume_participation": 1.0,
    }
    values.update(overrides)
    return DevelopmentResearchAssumptions(**values)


def _parameter_row(
    snapshot_date: str,
    key: str,
    *,
    instrument_type: str = "future",
    long_margin_rate: float | None = 0.10,
    short_margin_rate: float | None = 0.12,
    trading_fee_value: float = 0.001,
    trading_fee_unit: str = "notional_rate",
    settlement_fee_value: float = 0.0001,
    settlement_fee_unit: str = "notional_rate",
    close_today_fee_multiplier: float = 10.0,
) -> dict[str, object]:
    is_option = instrument_type == "option"
    return {
        "snapshot_date": snapshot_date,
        "instrument_type": instrument_type,
        "parameter_scope": "series" if is_option else "contract",
        "contract_or_series": key,
        "product": key[:2],
        "long_margin_rate": None if is_option else long_margin_rate,
        "short_margin_rate": None if is_option else short_margin_rate,
        "trading_fee_value": trading_fee_value,
        "trading_fee_unit": trading_fee_unit,
        "settlement_fee_value": settlement_fee_value,
        "settlement_fee_unit": settlement_fee_unit,
        "settlement_fee_kind": "exercise" if is_option else "delivery",
        "close_today_fee_multiplier": close_today_fee_multiplier,
        "close_today_fee_semantics": "fraction_of_trading_fee",
        "option_shorting_enabled": False,
        "source_section_title_matches_snapshot": True,
        "source_sha256": "a" * 64,
    }


def _execution_schedule(
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> CffexExecutionParameterSchedule:
    path = tmp_path / "cffex_settlement_params.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return CffexExecutionParameterSchedule(path, validate=False)


class SpyCombinedAccount(CombinedAccount):
    def __init__(self, cash: float) -> None:
        super().__init__(cash=cash)
        self.futures_targets: list[int] = []

    def adjust_futures_position(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        target = kwargs["target_contracts"]
        self.futures_targets.append(int(target))
        return super().adjust_futures_position(*args, **kwargs)


def test_research_assumptions_and_orders_enforce_development_contracts() -> None:
    assumptions = _assumptions()

    assert assumptions.assumption_label == "development_tier_research_assumption"
    assert "At most one open futures contract symbol" in DEVELOPMENT_SCOPE_LIMITATIONS[0]
    with pytest.raises(ValueError, match="cannot exceed"):
        _assumptions(
            futures_initial_margin_rate=0.08,
            futures_maintenance_margin_rate=0.10,
        )
    with pytest.raises(ValueError, match="whole number"):
        FuturesRebalanceOrder(D0, "IF", 1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="whole number"):
        LongOptionCloseOrder(D0, "IO2602-C-4321", 1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive whole"):
        LongOptionCloseOrder(D0, "IO2602-C-4321", -1)


def test_futures_causal_execution_conserves_nav_and_is_idempotent() -> None:
    rows = [
        _row(D0, "IF2602", "IF", multiplier=10.0, settle=99.0, open_interest=500.0),
        _row(D0, "IF2603", "IF", multiplier=11.0, settle=98.0, open_interest=100.0),
        _row(D1, "IF2602", "IF", multiplier=10.0, open_price=100.0, settle=110.0, open_interest=1.0),
        _row(D1, "IF2603", "IF", multiplier=11.0, open_price=90.0, settle=90.0, open_interest=10_000.0),
    ]
    catalog = _catalog(
        rows,
        {"IF2602": "20260220", "IF2603": "20260320"},
    )
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())

    plan = loop.plan_futures_rebalance(FuturesRebalanceOrder(D0, "IF", 2, min_dte=0))
    assert plan.selection is not None
    assert plan.selection.contract == "IF2602"
    assert plan.selection.multiplier == 10.0

    result = loop.execute_futures_rebalance(plan)
    event = result.events[0]
    assert event.reference_open_price == 100.0
    assert event.execution_price == pytest.approx(101.0)
    assert event.fees == 4.0
    assert account.net_asset_value() == pytest.approx(9_996.0)

    cash_after_execution = account.free_cash
    assert loop.execute_futures_rebalance(plan) is result
    assert account.free_cash == cash_after_execution

    eod = loop.settle_end_of_day(D1)
    assert eod.nav_inputs.futures_settlements == (("IF2602", 110.0),)
    assert eod.nav == pytest.approx(10_176.0)
    assert eod.margin.status == MarginStatus.OK
    cash_after_eod = account.free_cash
    assert loop.settle_end_of_day(D1) is eod
    assert account.free_cash == cash_after_eod

    later_target_same_day = loop.plan_futures_rebalance(
        FuturesRebalanceOrder(D0, "IF", 3, min_dte=0)
    )
    with pytest.raises(DerivativeEventError, match="completed end-of-day"):
        loop.execute_futures_rebalance(later_target_same_day)


def test_missing_next_open_rejects_without_settlement_fallback() -> None:
    catalog = _catalog(
        [
            _row(D0, "IF2602", "IF", multiplier=10.0, settle=100.0),
            _row(
                D1,
                "IF2602",
                "IF",
                multiplier=10.0,
                open_price=None,
                settle=999.0,
                open_executable=False,
            ),
        ],
        {"IF2602": "20260220"},
    )
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())

    result = loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D0, "IF", 1, min_dte=0))
    )
    event = result.events[0]

    assert event.status == EventStatus.REJECTED
    assert event.requested_contracts == 1
    assert event.filled_contracts == 0
    assert event.rejected_contracts == 1
    assert "next open" in event.reason
    assert event.execution_price is None
    assert account.futures_positions == {}
    assert account.free_cash == 10_000.0


def test_futures_fill_uses_prior_day_volume_cap_not_execution_volume() -> None:
    catalog = _catalog(
        [
            _row(D0, "IC2602", "IC", multiplier=13.0, volume=5.0),
            _row(D1, "IC2602", "IC", multiplier=13.0, volume=10_000.0),
        ],
        {"IC2602": "20260220"},
    )
    account = CombinedAccount(cash=100_000.0)
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(prior_day_volume_participation=0.4),
    )

    event = loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D0, "IC", 7, min_dte=0))
    ).events[0]

    assert event.prior_day_volume == 5.0
    assert event.participation_limit == 2
    assert event.requested_contracts == 7
    assert event.filled_contracts == 2
    assert event.clipped_contracts == 5
    assert event.rejected_contracts == 0
    assert account.futures_positions["IC2602"].contracts == 2
    assert account.futures_positions["IC2602"].multiplier == 13.0


def test_same_symbol_add_reduce_reverse_and_close_are_explicitly_phased() -> None:
    dates = [D0, D1, D2, D3, D4, D5]
    rows = [
        _row(
            trade_date,
            "IF2603",
            "IF",
            multiplier=10.0,
            open_price=100.0 + index,
            settle=100.0 + index,
            volume=100.0,
        )
        for index, trade_date in enumerate(dates)
    ]
    catalog = _catalog(rows, {"IF2603": "20260320"})
    account = SpyCombinedAccount(cash=100_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())

    loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D0, "IF", 2, min_dte=0))
    )
    loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D1, "IF", 4, min_dte=0))
    )
    loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D2, "IF", 1, min_dte=0))
    )

    account.futures_targets.clear()
    reverse = loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D3, "IF", -2, min_dte=0))
    )

    assert [event.action for event in reverse.events] == ["reverse_close", "reverse_open"]
    assert account.futures_targets == [0, -2]
    assert reverse.events[0].side == "sell"
    assert reverse.events[1].side == "sell"
    assert reverse.events[0].contracts_after == 0
    assert reverse.events[1].contracts_before == 0
    assert account.futures_positions["IF2603"].contracts == -2

    closed = loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D4, "IF", 0, min_dte=0))
    )
    assert closed.events[0].action == "close"
    assert account.futures_positions == {}


def test_stock_sale_can_inject_cash_between_idempotent_futures_phases() -> None:
    catalog = _catalog(
        [
            _row(D0, "IF2602", "IF", multiplier=10.0, volume=100.0),
            _row(D1, "IF2602", "IF", multiplier=10.0, open_price=100.0),
        ],
        {"IF2602": "20260220"},
    )
    account = CombinedAccount(cash=1_000.0)
    account.apply_buy(
        "stock.cash.source",
        quantity=9,
        price=100.0,
        fees=0.0,
        trade_date=date(2025, 12, 29),
        available_from=date(2025, 12, 30),
    )
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    plan = loop.plan_futures_rebalance(
        FuturesRebalanceOrder(D0, "IF", 2, min_dte=0)
    )

    reductions = loop.execute_futures_reduction_phase(plan)
    assert reductions.events == ()
    assert reductions.required_closes_succeeded is True
    assert loop.execute_futures_reduction_phase(plan) is reductions
    assert account.free_cash == 100.0

    account.apply_sell(
        "stock.cash.source",
        quantity=9,
        price=100.0,
        fees=0.0,
        trade_date=date(2026, 1, 5),
    )
    increases = loop.execute_futures_increase_phase(plan, reductions)

    assert increases.events[0].filled_contracts == 2
    assert account.futures_positions["IF2602"].contracts == 2
    assert loop.execute_futures_increase_phase(plan, reductions) is increases


def test_margin_call_allows_reverse_close_but_blocks_increase_phase() -> None:
    catalog = _catalog(
        [
            _row(D1, "IF2602", "IF", multiplier=10.0, settle=90.0),
            _row(D2, "IF2602", "IF", multiplier=10.0, open_price=90.0, settle=90.0),
        ],
        {"IF2602": "20260220"},
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
    account.settle_futures("IF2602", 90.0, settlement_date=date(2026, 1, 5))
    assert account.check_margin().status == MarginStatus.MARGIN_CALL
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    plan = loop.plan_futures_rebalance(
        FuturesRebalanceOrder(D1, "IF", -1, min_dte=0)
    )

    reductions = loop.execute_futures_reduction_phase(plan)

    assert reductions.margin_before_status == MarginStatus.MARGIN_CALL
    assert reductions.required_closes_succeeded is True
    assert reductions.events[0].action == "reverse_close"
    assert reductions.events[0].filled_contracts == 1
    assert account.futures_positions == {}
    assert account.check_margin().status == MarginStatus.OK

    increases = loop.execute_futures_increase_phase(plan, reductions)

    assert increases.events[0].action == "reverse_open"
    assert increases.events[0].status == EventStatus.REJECTED
    assert increases.events[0].reason == "risk_increase_blocked_by_margin_status"
    assert account.futures_positions == {}


def test_partial_reverse_close_state_blocks_opposite_open() -> None:
    catalog = _catalog(
        [
            _row(D0, "IF2602", "IF", multiplier=10.0, volume=2.0),
            _row(D1, "IF2602", "IF", multiplier=10.0, open_price=100.0),
        ],
        {"IF2602": "20260220"},
    )
    account = CombinedAccount(cash=10_000.0)
    account.open_futures(
        "IF2602",
        contracts=2,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=date(2025, 12, 31),
    )
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(prior_day_volume_participation=0.5),
    )
    plan = loop.plan_futures_rebalance(
        FuturesRebalanceOrder(D0, "IF", -1, min_dte=0)
    )

    reductions = loop.execute_futures_reduction_phase(plan)

    assert reductions.events[0].action == "reverse_close"
    assert reductions.events[0].filled_contracts == 1
    assert reductions.events[0].clipped_contracts == 1
    assert reductions.required_closes_succeeded is False
    assert account.futures_positions["IF2602"].contracts == 1

    increases = loop.execute_futures_increase_phase(plan, reductions)
    assert increases.events[0].status == EventStatus.REJECTED
    assert "incomplete_required_close" in increases.events[0].reason
    assert account.futures_positions["IF2602"].contracts == 1


def test_future_expiry_cash_settles_releases_margin_and_charges_delivery_fee(
    tmp_path: Path,
) -> None:
    contract = "IF2601"
    rows = [
        _row(D0, contract, "IF", multiplier=10.0, settle=100.0),
        _row(D1, contract, "IF", multiplier=10.0, settle=110.0),
        _row(D2, "IF2602", "IF", multiplier=10.0, settle=111.0),
    ]
    catalog = _catalog(rows, {contract: D1, "IF2602": "20260220"})
    schedule = _execution_schedule(
        tmp_path,
        [
            _parameter_row(
                P0,
                contract,
                settlement_fee_value=0.0001,
                settlement_fee_unit="notional_rate",
            )
        ],
    )
    account = CombinedAccount(cash=10_000.0)
    account.open_futures(
        contract,
        contracts=1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=date(2026, 1, 2),
    )
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(),
        execution_parameters=schedule,
    )

    result = loop.settle_end_of_day(D1)

    assert [event.action for event in result.events] == [
        "daily_settlement",
        "expiry_cash_settlement",
    ]
    expiry = result.events[1]
    assert expiry.fees == pytest.approx(0.11)
    assert expiry.cash_flow == pytest.approx(-0.11)
    assert not account.futures_positions
    assert account.locked_margin == pytest.approx(0.0)
    assert result.nav == pytest.approx(10_099.89)
    assert loop.settle_end_of_day(D1) is result


def test_futures_compatibility_wrapper_matches_consecutive_phases() -> None:
    catalog = _catalog(
        [
            _row(D0, "IF2602", "IF", multiplier=10.0, volume=100.0),
            _row(D1, "IF2602", "IF", multiplier=10.0, open_price=105.0),
        ],
        {"IF2602": "20260220"},
    )

    def make_loop() -> tuple[CombinedAccount, DerivativeEventLoop]:
        account = CombinedAccount(cash=10_000.0)
        account.open_futures(
            "IF2602",
            contracts=1,
            price=100.0,
            multiplier=10.0,
            initial_margin_rate=0.10,
            maintenance_margin_rate=0.08,
            trade_date=date(2025, 12, 31),
        )
        return account, DerivativeEventLoop(account, catalog, _assumptions())

    wrapped_account, wrapped_loop = make_loop()
    phased_account, phased_loop = make_loop()
    wrapped_plan = wrapped_loop.plan_futures_rebalance(
        FuturesRebalanceOrder(D0, "IF", -2, min_dte=0)
    )
    phased_plan = phased_loop.plan_futures_rebalance(
        FuturesRebalanceOrder(D0, "IF", -2, min_dte=0)
    )

    wrapped = wrapped_loop.execute_futures_rebalance(wrapped_plan)
    reductions = phased_loop.execute_futures_reduction_phase(phased_plan)
    increases = phased_loop.execute_futures_increase_phase(phased_plan, reductions)

    assert wrapped.events == (*reductions.events, *increases.events)
    assert wrapped.margin == increases.margin
    assert wrapped_account.free_cash == pytest.approx(phased_account.free_cash)
    assert wrapped_account.locked_margin == pytest.approx(phased_account.locked_margin)
    assert dict(wrapped_account.futures_positions) == dict(
        phased_account.futures_positions
    )
    assert wrapped_loop.execute_futures_rebalance(wrapped_plan) is wrapped


def test_failed_required_roll_close_blocks_new_contract_open() -> None:
    rows = [
        _row(D0, "IF2601", "IF", multiplier=10.0, open_interest=1_000.0),
        _row(D0, "IF2602", "IF", multiplier=10.0, open_interest=100.0),
        _row(D1, "IF2601", "IF", multiplier=10.0, open_interest=1_000.0),
        _row(D1, "IF2602", "IF", multiplier=10.0, open_interest=100.0),
        _row(
            D2,
            "IF2601",
            "IF",
            multiplier=10.0,
            open_price=None,
            open_executable=False,
        ),
        _row(D2, "IF2602", "IF", multiplier=10.0, open_price=102.0),
    ]
    catalog = _catalog(
        rows,
        {"IF2601": "20260120", "IF2602": "20260220"},
    )
    account = CombinedAccount(cash=100_000.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D0, "IF", 1, min_dte=0))
    )

    roll_plan = loop.plan_futures_rebalance(
        FuturesRebalanceOrder(D1, "IF", 1, min_dte=20)
    )
    assert roll_plan.selection is not None
    assert roll_plan.selection.contract == "IF2602"
    assert [leg.action for leg in roll_plan.legs] == ["roll_close", "roll_open"]

    reductions = loop.execute_futures_reduction_phase(roll_plan)
    assert reductions.required_closes_succeeded is False
    assert reductions.events[0].status == EventStatus.REJECTED

    increases = loop.execute_futures_increase_phase(roll_plan, reductions)
    assert increases.events[0].status == EventStatus.REJECTED
    assert "blocked" in increases.events[0].reason
    assert set(account.futures_positions) == {"IF2601"}
    assert account.futures_positions["IF2601"].contracts == 1


def test_single_futures_symbol_scope_rejects_multi_symbol_plan_and_eod() -> None:
    rows = [
        _row(D0, "IF2602", "IF", multiplier=10.0),
        _row(D0, "IH2602", "IH", multiplier=12.0),
        _row(D1, "IF2602", "IF", multiplier=10.0, settle=90.0),
        _row(D1, "IH2602", "IH", multiplier=12.0, settle=190.0),
    ]
    catalog = _catalog(
        rows,
        {"IF2602": "20260220", "IH2602": "20260220"},
    )
    account = CombinedAccount(cash=100_000.0)
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

    with pytest.raises(DerivativeEventError, match="create multiple"):
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D0, "IH", 1, min_dte=0))

    account.open_futures(
        "IH2602",
        contracts=1,
        price=200.0,
        multiplier=12.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=date(2026, 1, 2),
    )
    cash_before = account.free_cash
    positions_before = dict(account.futures_positions)
    with pytest.raises(DerivativeEventError, match="sequential settlement"):
        loop.settle_end_of_day(D1)
    assert account.free_cash == cash_before
    assert dict(account.futures_positions) == positions_before


def test_option_selection_is_causal_and_budget_and_signal_volume_bound_fill() -> None:
    selected = "IO2602-C-4321"
    alternative = "IO2602-C-5000"
    rows = [
        _row(
            D0,
            selected,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4321.0,
            delta=0.50,
            volume=5.0,
            open_interest=500.0,
        ),
        _row(
            D0,
            alternative,
            "IO",
            multiplier=9.0,
            option_type="call",
            strike=5000.0,
            delta=0.70,
            volume=100.0,
            open_interest=100.0,
        ),
        _row(
            D1,
            selected,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4321.0,
            delta=0.90,
            open_price=10.0,
            volume=10_000.0,
            open_interest=1.0,
        ),
        _row(
            D1,
            alternative,
            "IO",
            multiplier=9.0,
            option_type="call",
            strike=5000.0,
            delta=0.50,
            open_price=1.0,
            volume=10_000.0,
            open_interest=10_000.0,
        ),
    ]
    catalog = _catalog(
        rows,
        {selected: "20260220", alternative: "20260220"},
    )
    account = CombinedAccount(cash=1_000.0)
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(
            option_slippage_bps=1_000.0,
            prior_day_volume_participation=0.4,
        ),
    )
    order = LongOptionPurchaseOrder(D0, "IO", "call", 0.50, 0, 60, 500.0)

    plan = loop.plan_long_option_purchase(order)
    assert plan.selection.contract == selected
    assert plan.selection.multiplier == 7.0
    assert plan.selection.strike == 4321.0
    event = loop.execute_long_option_purchase(plan).events[0]

    assert event.reference_open_price == 10.0
    assert event.execution_price == pytest.approx(11.0)
    assert event.requested_contracts == 6
    assert event.participation_limit == 2
    assert event.filled_contracts == 2
    assert event.clipped_contracts == 4
    assert account.option_positions[selected].contracts == 2
    assert account.option_positions[selected].multiplier == 7.0
    assert account.option_positions[selected].strike == 4321.0
    assert account.option_positions[selected].expiry == date(2026, 2, 20)
    assert account.free_cash == pytest.approx(844.0)


def test_long_option_partial_then_full_close_uses_actual_adverse_opens() -> None:
    contract = "IO2602-C-4321"
    rows = [
        _row(P0, contract, "IO", multiplier=7.0, option_type="call", strike=4321.0, delta=0.5),
        _row(
            D0,
            contract,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4321.0,
            delta=0.5,
            volume=3.0,
        ),
        _row(
            D1,
            contract,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4321.0,
            delta=0.5,
            open_price=8.0,
        ),
        _row(
            D2,
            contract,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4321.0,
            delta=0.5,
            open_price=9.0,
        ),
    ]
    catalog = _catalog(rows, {contract: "20260220"})
    account = CombinedAccount(cash=10_000.0)
    account.buy_option(
        contract,
        option_type="call",
        contracts=3,
        premium=5.0,
        strike=4321.0,
        expiry=date(2026, 2, 20),
        multiplier=7.0,
        trade_date=date(2026, 1, 1),
    )
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(
            option_slippage_bps=100.0,
            prior_day_volume_participation=0.5,
        ),
    )

    partial = loop.execute_long_option_close(
        loop.plan_long_option_close(LongOptionCloseOrder(D0, contract, 2))
    ).events[0]
    assert partial.execution_price == pytest.approx(7.92)
    assert partial.prior_day_volume == 3.0
    assert partial.participation_limit == 1
    assert partial.filled_contracts == 1
    assert partial.clipped_contracts == 1
    assert partial.contracts_after == 2
    assert account.option_positions[contract].contracts == 2

    full = loop.execute_long_option_close(
        loop.plan_long_option_close(LongOptionCloseOrder(D1, contract))
    ).events[0]
    assert full.execution_price == pytest.approx(8.91)
    assert full.filled_contracts == 2
    assert full.contracts_after == 0
    assert account.option_positions == {}


def test_failed_option_close_blocks_replacement_purchase() -> None:
    old = "IO2601-C-4000"
    new = "IO2602-C-4321"
    rows = [
        _row(
            P0,
            old,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4000.0,
            delta=0.9,
        ),
        _row(
            P0,
            new,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4321.0,
            delta=0.5,
        ),
        _row(
            D0,
            old,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4000.0,
            delta=0.9,
        ),
        _row(
            D0,
            new,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4321.0,
            delta=0.5,
        ),
        _row(
            D1,
            old,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4000.0,
            delta=0.9,
            open_price=None,
            open_executable=False,
        ),
        _row(
            D1,
            new,
            "IO",
            multiplier=7.0,
            option_type="call",
            strike=4321.0,
            delta=0.5,
            open_price=10.0,
        ),
    ]
    catalog = _catalog(rows, {old: "20260220", new: "20260220"})
    account = CombinedAccount(cash=10_000.0)
    account.buy_option(
        old,
        option_type="call",
        contracts=1,
        premium=5.0,
        strike=4000.0,
        expiry=date(2026, 2, 20),
        multiplier=7.0,
        trade_date=date(2026, 1, 1),
    )
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    replacement = loop.plan_long_option_replacement(
        LongOptionCloseOrder(D0, old),
        LongOptionPurchaseOrder(D0, "IO", "call", 0.5, 0, 60, 1_000.0),
    )

    result = loop.execute_long_option_replacement(replacement)

    assert result.events[0].status == EventStatus.REJECTED
    assert result.events[1].status == EventStatus.REJECTED
    assert "blocked" in result.events[1].reason
    assert set(account.option_positions) == {old}


def test_option_marks_and_exact_expiry_use_official_option_settlement() -> None:
    contract = "IO2601-C-NONPARSED"
    catalog = _catalog(
        [
            _row(
                D2,
                contract,
                "IO",
                multiplier=10.0,
                option_type="call",
                strike=1234.5,
                delta=0.5,
                settle=3.0,
            ),
            _row(
                D3,
                contract,
                "IO",
                multiplier=10.0,
                option_type="call",
                strike=1234.5,
                delta=0.5,
                settle=7.5,
            ),
        ],
        {contract: D3},
    )
    account = CombinedAccount(cash=1_000.0)
    account.buy_option(
        contract,
        option_type="call",
        contracts=2,
        premium=2.0,
        strike=1234.5,
        expiry=date(2026, 1, 7),
        multiplier=10.0,
        trade_date=date(2026, 1, 5),
    )
    loop = DerivativeEventLoop(account, catalog, _assumptions())

    marked = loop.settle_end_of_day(D2)
    assert marked.events[0].action == "daily_mark"
    assert marked.nav == pytest.approx(1_020.0)
    expired = loop.settle_end_of_day(D3)

    assert expired.events[0].action == "expiry_cash_settlement"
    assert expired.events[0].settlement_price == 7.5
    assert expired.events[0].cash_flow == 150.0
    assert expired.nav == pytest.approx(1_110.0)
    position = account.option_positions[contract]
    assert position.settled is True
    assert position.settlement_option_price == 7.5
    cash_after = account.free_cash
    assert loop.settle_end_of_day(D3) is expired
    assert account.free_cash == cash_after


def test_missing_any_eod_mark_fails_before_all_account_mutation() -> None:
    future = "IF2602"
    option = "IO2602-C-4321"
    catalog = _catalog(
        [
            _row(D2, future, "IF", multiplier=10.0, settle=90.0),
            _row(
                D2,
                option,
                "IO",
                multiplier=7.0,
                option_type="call",
                strike=4321.0,
                delta=0.5,
                settle=None,
                settlement_mark_valid=False,
            ),
        ],
        {future: "20260220", option: "20260220"},
    )
    account = CombinedAccount(cash=2_000.0)
    account.open_futures(
        future,
        contracts=1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=date(2026, 1, 5),
    )
    account.buy_option(
        option,
        option_type="call",
        contracts=1,
        premium=2.0,
        strike=4321.0,
        expiry=date(2026, 2, 20),
        multiplier=7.0,
        trade_date=date(2026, 1, 5),
    )
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    cash_before = account.free_cash
    future_before = account.futures_positions[future]
    option_before = account.option_positions[option]

    with pytest.raises(MissingSettlementMarkError, match="exact official"):
        loop.settle_end_of_day(D2)

    assert account.free_cash == cash_before
    assert account.futures_positions[future] == future_before
    assert account.option_positions[option] == option_before


def test_eod_requires_complete_stock_closes_before_derivative_mutation() -> None:
    future = "IF2602"
    option = "IO2602-C-4321"
    stock = "stock.lowercase"
    catalog = _catalog(
        [
            _row(D2, future, "IF", multiplier=10.0, settle=90.0),
            _row(
                D2,
                option,
                "IO",
                multiplier=7.0,
                option_type="call",
                strike=4321.0,
                delta=0.5,
                settle=3.0,
            ),
        ],
        {future: "20260220", option: "20260220"},
    )
    account = CombinedAccount(cash=5_000.0)
    account.apply_buy(
        stock,
        quantity=10,
        price=10.0,
        fees=0.0,
        trade_date=date(2026, 1, 2),
        available_from=date(2026, 1, 5),
    )
    account.open_futures(
        future,
        contracts=1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=date(2026, 1, 5),
    )
    account.buy_option(
        option,
        option_type="call",
        contracts=1,
        premium=2.0,
        strike=4321.0,
        expiry=date(2026, 2, 20),
        multiplier=7.0,
        trade_date=date(2026, 1, 5),
    )
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    cash_before = account.free_cash
    future_before = account.futures_positions[future]
    option_before = account.option_positions[option]

    with pytest.raises(MissingSettlementMarkError, match="every held stock"):
        loop.settle_end_of_day(D2, stock_prices={"EXTRA": 99.0})

    assert account.free_cash == cash_before
    assert account.futures_positions[future] == future_before
    assert account.option_positions[option] == option_before

    eod = loop.settle_end_of_day(
        D2,
        stock_prices={stock: 11.0, "EXTRA": 99.0},
    )

    assert eod.margin.nav_is_complete is True
    assert eod.nav == pytest.approx(4_917.0)
    assert eod.nav_inputs.stock_prices == (("EXTRA", 99.0), (stock, 11.0))


def test_eod_reports_margin_call_without_liquidation_or_clamping() -> None:
    catalog = _catalog(
        [
            _row(D0, "IF2602", "IF", multiplier=10.0, open_price=100.0, settle=100.0),
            _row(D1, "IF2602", "IF", multiplier=10.0, open_price=100.0, settle=91.0),
        ],
        {"IF2602": "20260220"},
    )
    account = CombinedAccount(cash=107.0)
    loop = DerivativeEventLoop(account, catalog, _assumptions())
    loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(FuturesRebalanceOrder(D0, "IF", 1, min_dte=0))
    )

    eod = loop.settle_end_of_day(D1)

    assert eod.margin_status == MarginStatus.MARGIN_CALL
    assert eod.nav == pytest.approx(5.0)
    assert account.free_cash == pytest.approx(-86.0)
    assert account.futures_positions["IF2602"].contracts == 1
    assert eod.margin.shortfall > 0.0


def test_official_futures_margin_notional_fee_and_close_today_override_assumptions(
    tmp_path: Path,
) -> None:
    contract = "IF2602"
    catalog = _catalog(
        [
            _row(D0, contract, "IF", multiplier=10.0, open_price=100.0),
            _row(D1, contract, "IF", multiplier=10.0, open_price=100.0),
        ],
        {contract: "20260220"},
    )
    schedule = _execution_schedule(
        tmp_path,
        [
            _parameter_row(
                D0,
                contract,
                long_margin_rate=0.40,
                short_margin_rate=0.45,
                trading_fee_value=0.001,
                close_today_fee_multiplier=10.0,
            )
        ],
    )
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(futures_initial_margin_rate=0.10),
        execution_parameters=schedule,
    )

    opened = loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(
            FuturesRebalanceOrder(D0, "IF", 1, min_dte=0)
        )
    ).events[0]
    assert opened.execution_price == pytest.approx(101.0)
    assert opened.fees == pytest.approx(1.01)
    assert opened.fee_per_contract == pytest.approx(1.01)
    assert account.futures_positions[contract].initial_margin_rate == pytest.approx(
        0.40
    )

    closed = loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(
            FuturesRebalanceOrder(D0, "IF", 0, min_dte=0)
        )
    ).events[0]
    assert closed.execution_price == pytest.approx(99.0)
    assert closed.fees == pytest.approx(9.9)
    assert closed.fee_per_contract == pytest.approx(9.9)
    assert account.futures_positions == {}


def test_official_start_of_day_margin_revision_rebalances_locked_cash(
    tmp_path: Path,
) -> None:
    contract = "IF2602"
    catalog = _catalog(
        [
            _row(D0, contract, "IF", multiplier=10.0),
            _row(D1, contract, "IF", multiplier=10.0),
            _row(D2, contract, "IF", multiplier=10.0),
        ],
        {contract: "20260220"},
    )
    schedule = _execution_schedule(
        tmp_path,
        [
            _parameter_row(D0, contract, long_margin_rate=0.40),
            _parameter_row(D2, contract, long_margin_rate=0.50),
        ],
    )
    account = CombinedAccount(cash=10_000.0)
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(),
        execution_parameters=schedule,
    )
    loop.execute_futures_rebalance(
        loop.plan_futures_rebalance(
            FuturesRebalanceOrder(D0, "IF", 1, min_dte=0)
        )
    )
    loop.settle_end_of_day(D1)
    cash_before = account.free_cash

    updates = loop.apply_start_of_day_parameters(D2)

    assert len(updates) == 1
    assert updates[0].initial_margin_rate_before == pytest.approx(0.40)
    assert updates[0].initial_margin_rate_after == pytest.approx(0.50)
    assert updates[0].margin_transfer == pytest.approx(100.0)
    assert account.free_cash == pytest.approx(cash_before - 100.0)
    assert account.futures_positions[contract].initial_margin_rate == pytest.approx(
        0.50
    )


def test_official_option_trading_and_expiry_fees_share_one_cash_account(
    tmp_path: Path,
) -> None:
    contract = "IO2601-C-100"
    option_rows = [
        _row(
            trade_date,
            contract,
            "IO",
            multiplier=10.0,
            open_price=2.0,
            settle=5.0 if trade_date == D2 else 2.0,
            option_type="call",
            strike=100.0,
            delta=0.5,
        )
        for trade_date in (D0, D1, D2)
    ]
    catalog = _catalog(option_rows, {contract: D2})
    schedule = _execution_schedule(
        tmp_path,
        [
            _parameter_row(
                D0,
                "IO2601",
                instrument_type="option",
                trading_fee_value=3.0,
                trading_fee_unit="currency_per_contract",
                settlement_fee_value=4.0,
                settlement_fee_unit="currency_per_contract",
                close_today_fee_multiplier=1.0,
            )
        ],
    )
    account = CombinedAccount(cash=1_000.0)
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(),
        execution_parameters=schedule,
    )

    purchase = loop.execute_long_option_purchase(
        loop.plan_long_option_purchase(
            LongOptionPurchaseOrder(D0, "IO", "call", 0.5, 0, 10, 100.0)
        )
    ).events[0]
    assert purchase.filled_contracts == 4
    assert purchase.fees == pytest.approx(12.0)
    assert purchase.fee_per_contract == pytest.approx(3.0)

    loop.settle_end_of_day(D1)
    expiry = loop.settle_end_of_day(D2)
    event = expiry.events[0]
    assert event.action == "expiry_cash_settlement"
    assert event.fees == pytest.approx(16.0)
    assert event.cash_flow == pytest.approx(184.0)
    assert expiry.nav == pytest.approx(1_091.2)


def test_official_option_same_day_close_uses_close_today_multiplier(
    tmp_path: Path,
) -> None:
    contract = "IO2602-C-100"
    rows = [
        _row(
            trade_date,
            contract,
            "IO",
            multiplier=10.0,
            open_price=2.0,
            settle=2.0,
            option_type="call",
            strike=100.0,
            delta=0.5,
        )
        for trade_date in (P0, D0, D1)
    ]
    catalog = _catalog(rows, {contract: "20260220"})
    schedule = _execution_schedule(
        tmp_path,
        [
            _parameter_row(
                D0,
                "IO2602",
                instrument_type="option",
                trading_fee_value=3.0,
                trading_fee_unit="currency_per_contract",
                settlement_fee_value=4.0,
                settlement_fee_unit="currency_per_contract",
                close_today_fee_multiplier=5.0,
            )
        ],
    )
    account = CombinedAccount(cash=1_000.0)
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(),
        execution_parameters=schedule,
    )
    purchase = loop.execute_long_option_purchase(
        loop.plan_long_option_purchase(
            LongOptionPurchaseOrder(D0, "IO", "call", 0.5, 0, 90, 25.0)
        )
    ).events[0]
    assert purchase.filled_contracts == 1
    assert purchase.fees == pytest.approx(3.0)

    close = loop.execute_long_option_close(
        loop.plan_long_option_close(LongOptionCloseOrder(D0, contract))
    ).events[0]

    assert close.filled_contracts == 1
    assert close.fees == pytest.approx(15.0)
    assert close.fee_per_contract == pytest.approx(15.0)
    assert account.option_positions == {}


def test_missing_official_expiry_parameter_fails_before_account_mutation(
    tmp_path: Path,
) -> None:
    contract = "IO2601-C-100"
    catalog = _catalog(
        [
            _row(
                D2,
                contract,
                "IO",
                multiplier=10.0,
                settle=5.0,
                option_type="call",
                strike=100.0,
                delta=0.5,
            )
        ],
        {contract: D2},
    )
    schedule = _execution_schedule(
        tmp_path,
        [_parameter_row(D0, "IF2602")],
    )
    account = CombinedAccount(cash=1_000.0)
    account.buy_option(
        contract,
        option_type="call",
        contracts=1,
        premium=2.0,
        strike=100.0,
        expiry=date(2026, 1, 6),
        multiplier=10.0,
        trade_date=date(2026, 1, 5),
    )
    loop = DerivativeEventLoop(
        account,
        catalog,
        _assumptions(),
        execution_parameters=schedule,
    )
    cash_before = account.free_cash
    position_before = account.option_positions[contract]

    with pytest.raises(MissingExecutionParameterError, match="official"):
        loop.settle_end_of_day(D2)

    assert account.free_cash == cash_before
    assert account.option_positions[contract] == position_before

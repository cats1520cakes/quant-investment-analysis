from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from quant_proof.engine.account import Account
from quant_proof.engine.combined_account import (
    CombinedAccount,
    FuturesMarginRateUpdate,
    MarginStatus,
    OptionType,
    UnifiedAccount,
)


TRADE_DATE = date(2026, 7, 1)
DAY_2 = date(2026, 7, 2)
DAY_3 = date(2026, 7, 3)
DAY_4 = date(2026, 7, 4)
DAY_5 = date(2026, 7, 5)


def open_long_future(
    account: CombinedAccount,
    *,
    initial_margin_rate: float = 0.10,
    maintenance_margin_rate: float = 0.08,
) -> None:
    account.open_futures(
        symbol="IF2607",
        contracts=1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=initial_margin_rate,
        maintenance_margin_rate=maintenance_margin_rate,
        trade_date=TRADE_DATE,
    )


def test_margin_lock_is_an_internal_transfer_and_does_not_change_nav() -> None:
    account = CombinedAccount(cash=10_000.0)
    nav_before = account.net_asset_value()

    account.open_futures(
        symbol="IF2607",
        contracts=2,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=TRADE_DATE,
    )

    assert account.free_cash == pytest.approx(9_800.0)
    assert account.locked_margin == pytest.approx(200.0)
    assert account.net_asset_value() == pytest.approx(nav_before)
    assert account.check_margin().status == MarginStatus.OK
    assert account.futures_positions["IF2607"].last_margin_rate_date == TRADE_DATE


def test_futures_margin_rate_increase_is_fully_funded_without_settling_pnl() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(account)
    account.settle_futures("IF2607", 105.0, DAY_2)
    nav_before = account.net_asset_value()
    position_before = account.futures_positions["IF2607"]

    update = account.update_futures_margin_rates(
        "IF2607",
        initial_margin_rate=0.20,
        maintenance_margin_rate=0.15,
        effective_date=DAY_3,
    )

    assert isinstance(update, FuturesMarginRateUpdate)
    assert update.initial_margin_rate_before == pytest.approx(0.10)
    assert update.maintenance_margin_rate_before == pytest.approx(0.08)
    assert update.initial_margin_rate_after == pytest.approx(0.20)
    assert update.maintenance_margin_rate_after == pytest.approx(0.15)
    assert update.margin_before == pytest.approx(105.0)
    assert update.margin_required == pytest.approx(210.0)
    assert update.margin_after == pytest.approx(210.0)
    assert update.margin_transfer == pytest.approx(105.0)
    assert update.free_cash_after == pytest.approx(840.0)
    assert update.status == MarginStatus.OK
    assert update.already_updated is False
    assert account.net_asset_value() == pytest.approx(nav_before)

    position_after = account.futures_positions["IF2607"]
    assert position_after.settlement_price == position_before.settlement_price
    assert (
        position_after.cumulative_settled_pnl
        == position_before.cumulative_settled_pnl
    )
    assert position_after.last_settlement_date == position_before.last_settlement_date
    assert position_after.last_margin_rate_date == DAY_3
    with pytest.raises(FrozenInstanceError):
        setattr(update, "margin_after", 0.0)


def test_futures_margin_rate_increase_locks_only_free_cash_and_calls_margin() -> None:
    account = CombinedAccount(cash=150.0)
    open_long_future(account)
    nav_before = account.net_asset_value()

    update = account.update_futures_margin_rates(
        "IF2607",
        initial_margin_rate=0.20,
        maintenance_margin_rate=0.15,
        effective_date=DAY_2,
    )
    check = account.check_margin()

    assert update.margin_before == pytest.approx(100.0)
    assert update.margin_required == pytest.approx(200.0)
    assert update.margin_transfer == pytest.approx(50.0)
    assert update.margin_after == pytest.approx(150.0)
    assert update.free_cash_after == 0.0
    assert update.status == MarginStatus.MARGIN_CALL
    assert check.status == MarginStatus.MARGIN_CALL
    assert check.shortfall == pytest.approx(50.0)
    assert account.net_asset_value() == pytest.approx(nav_before)


def test_futures_margin_rate_decrease_releases_excess_locked_margin() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(
        account,
        initial_margin_rate=0.20,
        maintenance_margin_rate=0.15,
    )
    nav_before = account.net_asset_value()

    update = account.update_futures_margin_rates(
        "IF2607",
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        effective_date=DAY_2,
    )

    assert update.margin_before == pytest.approx(200.0)
    assert update.margin_required == pytest.approx(100.0)
    assert update.margin_after == pytest.approx(100.0)
    assert update.margin_transfer == pytest.approx(-100.0)
    assert account.free_cash == pytest.approx(900.0)
    assert account.check_margin().status == MarginStatus.OK
    assert account.net_asset_value() == pytest.approx(nav_before)


def test_futures_margin_rate_update_uses_absolute_short_contracts() -> None:
    account = CombinedAccount(cash=1_000.0)
    account.open_futures(
        symbol="IF2607",
        contracts=-2,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=TRADE_DATE,
    )

    update = account.update_futures_margin_rates(
        "IF2607",
        initial_margin_rate=0.15,
        maintenance_margin_rate=0.10,
        effective_date=DAY_2,
    )

    assert update.margin_required == pytest.approx(300.0)
    assert update.margin_transfer == pytest.approx(100.0)
    assert account.locked_margin == pytest.approx(300.0)
    assert account.free_cash == pytest.approx(700.0)


def test_futures_margin_rate_same_day_is_idempotent_or_conflicting() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(account)
    first = account.update_futures_margin_rates(
        "IF2607",
        initial_margin_rate=0.12,
        maintenance_margin_rate=0.09,
        effective_date=DAY_2,
    )
    cash_after_first = account.free_cash
    position_after_first = account.futures_positions["IF2607"]

    duplicate = account.update_futures_margin_rates(
        "IF2607",
        initial_margin_rate=0.12,
        maintenance_margin_rate=0.09,
        effective_date=DAY_2,
    )

    assert first.already_updated is False
    assert duplicate.already_updated is True
    assert duplicate.margin_before == pytest.approx(120.0)
    assert duplicate.margin_required == pytest.approx(120.0)
    assert duplicate.margin_after == pytest.approx(120.0)
    assert duplicate.margin_transfer == 0.0
    assert account.free_cash == pytest.approx(cash_after_first)
    assert account.futures_positions["IF2607"] == position_after_first

    with pytest.raises(ValueError, match="conflicting futures margin-rate update"):
        account.update_futures_margin_rates(
            "IF2607",
            initial_margin_rate=0.13,
            maintenance_margin_rate=0.09,
            effective_date=DAY_2,
        )
    assert account.free_cash == pytest.approx(cash_after_first)
    assert account.futures_positions["IF2607"] == position_after_first


def test_margin_rate_update_rejects_invalid_and_reverse_dates_atomically() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(account)
    cash_before = account.free_cash
    position_before = account.futures_positions["IF2607"]

    with pytest.raises(ValueError, match="effective_date must be a date"):
        account.update_futures_margin_rates(
            "IF2607",
            0.12,
            0.09,
            None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        account.update_futures_margin_rates("IF2607", 0.10, 0.11, DAY_2)
    with pytest.raises(ValueError, match="latest trade_date"):
        account.update_futures_margin_rates(
            "IF2607",
            0.12,
            0.09,
            date(2026, 6, 30),
        )
    assert account.free_cash == pytest.approx(cash_before)
    assert account.futures_positions["IF2607"] == position_before

    account.update_futures_margin_rates("IF2607", 0.12, 0.09, DAY_3)
    cash_after_update = account.free_cash
    position_after_update = account.futures_positions["IF2607"]
    with pytest.raises(ValueError, match="margin-rate updates must be chronological"):
        account.update_futures_margin_rates("IF2607", 0.11, 0.08, DAY_2)
    assert account.free_cash == pytest.approx(cash_after_update)
    assert account.futures_positions["IF2607"] == position_after_update


def test_futures_margin_rate_and_settlement_dates_are_jointly_chronological() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(account)
    account.update_futures_margin_rates("IF2607", 0.12, 0.09, DAY_3)
    cash_before = account.free_cash
    position_before = account.futures_positions["IF2607"]

    with pytest.raises(ValueError, match="latest futures margin-rate update"):
        account.settle_futures("IF2607", 105.0, DAY_2)
    assert account.free_cash == pytest.approx(cash_before)
    assert account.futures_positions["IF2607"] == position_before

    settlement = account.settle_futures("IF2607", 105.0, DAY_3)
    assert settlement.variation_margin == pytest.approx(50.0)
    assert account.futures_positions["IF2607"].last_margin_rate_date == DAY_3

    settled_first = CombinedAccount(cash=1_000.0)
    open_long_future(settled_first)
    settled_first.settle_futures("IF2607", 105.0, DAY_3)
    with pytest.raises(ValueError, match="latest futures settlement"):
        settled_first.update_futures_margin_rates("IF2607", 0.12, 0.09, DAY_2)

    replay = CombinedAccount(cash=1_000.0)
    open_long_future(replay)
    replay.settle_futures("IF2607", 105.0, DAY_2)
    replay.update_futures_margin_rates("IF2607", 0.12, 0.09, DAY_3)
    with pytest.raises(ValueError, match="latest futures margin-rate update"):
        replay.settle_futures("IF2607", 105.0, DAY_2)


def test_adjust_reverse_and_partial_close_preserve_margin_rate_date() -> None:
    account = CombinedAccount(cash=10_000.0)
    account.open_futures(
        symbol="IF2607",
        contracts=2,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=TRADE_DATE,
    )
    account.update_futures_margin_rates("IF2607", 0.12, 0.09, DAY_2)
    position_after_update = account.futures_positions["IF2607"]

    with pytest.raises(ValueError, match="latest futures margin-rate update"):
        account.close_futures("IF2607", 100.0, contracts=1, trade_date=TRADE_DATE)
    assert account.futures_positions["IF2607"] == position_after_update

    account.adjust_futures_position("IF2607", 3, 101.0, trade_date=DAY_3)
    assert account.futures_positions["IF2607"].last_margin_rate_date == DAY_2
    account.adjust_futures_position("IF2607", -2, 102.0, trade_date=DAY_4)
    assert account.futures_positions["IF2607"].last_margin_rate_date == DAY_2
    account.close_futures("IF2607", 103.0, contracts=1, trade_date=DAY_5)
    assert account.futures_positions["IF2607"].contracts == -1
    assert account.futures_positions["IF2607"].last_margin_rate_date == DAY_2


def test_locked_margin_cannot_be_reused_for_stock_or_option_purchase() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(
        account,
        initial_margin_rate=0.60,
        maintenance_margin_rate=0.40,
    )

    assert account.free_cash == pytest.approx(400.0)
    assert account.locked_margin == pytest.approx(600.0)

    with pytest.raises(ValueError, match="insufficient cash"):
        account.apply_buy(
            symbol="510300",
            quantity=5,
            price=100.0,
            fees=0.0,
            trade_date=TRADE_DATE,
            available_from=TRADE_DATE,
        )
    with pytest.raises(ValueError, match="insufficient free cash"):
        account.buy_option(
            symbol="IO2607-C-100",
            option_type="call",
            contracts=1,
            premium=5.0,
            strike=100.0,
            expiry=date(2026, 7, 31),
            multiplier=100.0,
        )

    assert account.portfolio.quantity("510300") == 0
    assert account.option_positions == {}
    assert account.free_cash == pytest.approx(400.0)
    assert account.locked_margin == pytest.approx(600.0)


def test_futures_profit_and_loss_are_settled_daily_without_double_counting() -> None:
    account = CombinedAccount(cash=1_000.0)
    account.open_futures(
        symbol="IF2607",
        contracts=2,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
    )

    assert account.net_asset_value(futures_prices={"IF2607": 105.0}) == pytest.approx(
        1_100.0
    )
    with pytest.raises(ValueError, match="settlement_date is required"):
        account.settle_futures("IF2607", 105.0)

    profit = account.settle_futures("IF2607", 105.0, DAY_2)

    assert profit.variation_margin == pytest.approx(100.0)
    assert profit.margin_transfer == pytest.approx(10.0)
    assert account.free_cash == pytest.approx(890.0)
    assert account.locked_margin == pytest.approx(210.0)
    assert account.net_asset_value() == pytest.approx(1_100.0)

    cash_after_first = account.free_cash
    position_after_first = account.futures_positions["IF2607"]
    duplicate = account.settle_futures("IF2607", 105.0, DAY_2)

    assert duplicate.already_settled is True
    assert duplicate.variation_margin == 0.0
    assert duplicate.margin_transfer == 0.0
    assert account.free_cash == pytest.approx(cash_after_first)
    assert account.futures_positions["IF2607"] == position_after_first

    with pytest.raises(ValueError, match="conflicting futures settlement"):
        account.settle_futures("IF2607", 106.0, DAY_2)
    with pytest.raises(ValueError, match="strictly chronological"):
        account.settle_futures("IF2607", 104.0, TRADE_DATE)

    assert account.free_cash == pytest.approx(cash_after_first)
    assert account.futures_positions["IF2607"] == position_after_first

    loss = account.settle_futures("IF2607", 95.0, DAY_3)

    assert loss.variation_margin == pytest.approx(-200.0)
    assert loss.margin_transfer == pytest.approx(-20.0)
    assert account.free_cash == pytest.approx(710.0)
    assert account.locked_margin == pytest.approx(190.0)
    assert account.net_asset_value() == pytest.approx(900.0)
    assert account.net_asset_value(futures_prices={"IF2607": 100.0}) == pytest.approx(
        1_000.0
    )
    assert account.futures_positions["IF2607"].cumulative_settled_pnl == pytest.approx(
        -100.0
    )


def test_dated_futures_position_rejects_undated_adjustment_and_close_atomically() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(account)
    cash_before = account.free_cash
    position_before = account.futures_positions["IF2607"]

    with pytest.raises(ValueError, match="trade_date is required"):
        account.adjust_futures_position(
            "IF2607",
            target_contracts=2,
            price=110.0,
        )
    assert account.free_cash == pytest.approx(cash_before)
    assert account.futures_positions["IF2607"] == position_before

    with pytest.raises(ValueError, match="trade_date is required"):
        account.close_futures("IF2607", settlement_price=110.0)
    assert account.free_cash == pytest.approx(cash_before)
    assert account.futures_positions["IF2607"] == position_before


def test_undated_adjustment_cannot_overwrite_a_dated_settlement_fact() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(account)
    account.settle_futures("IF2607", 105.0, DAY_2)
    cash_before = account.free_cash
    position_before = account.futures_positions["IF2607"]

    with pytest.raises(ValueError, match="trade_date is required"):
        account.adjust_futures_position(
            "IF2607",
            target_contracts=2,
            price=110.0,
            trade_date=None,
        )

    assert account.free_cash == pytest.approx(cash_before)
    assert account.futures_positions["IF2607"] == position_before
    with pytest.raises(ValueError, match="conflicting futures settlement"):
        account.settle_futures("IF2607", 110.0, DAY_2)
    assert account.free_cash == pytest.approx(cash_before)
    assert account.futures_positions["IF2607"] == position_before


def test_dated_intraday_adjustment_can_settle_at_same_day_eod() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(account)
    account.settle_futures("IF2607", 105.0, DAY_2)

    adjustment = account.adjust_futures_position(
        "IF2607",
        target_contracts=2,
        price=110.0,
        trade_date=DAY_3,
    )
    settlement = account.settle_futures("IF2607", 112.0, DAY_3)

    assert adjustment.action == "add"
    assert adjustment.variation_margin == pytest.approx(50.0)
    assert settlement.already_settled is False
    assert settlement.variation_margin == pytest.approx(40.0)
    assert account.free_cash == pytest.approx(916.0)
    assert account.locked_margin == pytest.approx(224.0)
    assert account.net_asset_value() == pytest.approx(1_140.0)
    position = account.futures_positions["IF2607"]
    assert position.last_settlement_date == DAY_3
    assert position.settlement_price == pytest.approx(112.0)


def test_entirely_undated_synthetic_futures_position_can_still_adjust() -> None:
    account = CombinedAccount(cash=1_000.0)
    account.open_futures(
        symbol="IF2607",
        contracts=1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
    )

    adjustment = account.adjust_futures_position(
        "IF2607",
        target_contracts=2,
        price=105.0,
    )

    assert adjustment.action == "add"
    assert adjustment.trade_date is None
    assert account.futures_positions["IF2607"].trade_date is None
    assert account.futures_positions["IF2607"].last_settlement_date is None


def test_margin_call_and_default_preserve_negative_cash_and_nav() -> None:
    account = CombinedAccount(cash=105.0)
    open_long_future(account)

    margin_call = account.settle_futures("IF2607", 90.0, DAY_2)
    check = account.check_margin()

    assert margin_call.status == MarginStatus.MARGIN_CALL
    assert check.status == MarginStatus.MARGIN_CALL
    assert account.free_cash == pytest.approx(-85.0)
    assert account.locked_margin == pytest.approx(90.0)
    assert check.nav == pytest.approx(5.0)
    assert check.collateral_equity == pytest.approx(5.0)
    assert check.shortfall > 0.0

    default = account.settle_futures("IF2607", 80.0, DAY_3)
    check = account.check_margin()

    assert default.status == MarginStatus.DEFAULT
    assert check.status == MarginStatus.DEFAULT
    assert account.free_cash == pytest.approx(-175.0)
    assert account.net_asset_value() == pytest.approx(-95.0)
    assert check.nav == pytest.approx(-95.0)


def test_negative_futures_collateral_is_margin_call_while_total_nav_is_positive() -> None:
    account = CombinedAccount(cash=1_105.0)
    account.apply_buy(
        symbol="510300",
        quantity=10,
        price=100.0,
        fees=0.0,
        trade_date=TRADE_DATE,
        available_from=TRADE_DATE,
    )
    open_long_future(account)
    account.settle_futures("IF2607", 80.0, DAY_2)

    incomplete = account.check_margin()
    solvent = account.check_margin(stock_prices={"510300": 100.0})
    insolvent = account.check_margin(stock_prices={"510300": 0.0})

    assert account.free_cash == pytest.approx(-175.0)
    assert incomplete.nav == pytest.approx(-95.0)
    assert incomplete.nav_is_complete is False
    assert incomplete.status == MarginStatus.MARGIN_CALL
    assert solvent.collateral_equity == pytest.approx(-95.0)
    assert solvent.nav == pytest.approx(905.0)
    assert solvent.nav_is_complete is True
    assert solvent.status == MarginStatus.MARGIN_CALL
    assert insolvent.nav == pytest.approx(-95.0)
    assert insolvent.nav_is_complete is True
    assert insolvent.status == MarginStatus.DEFAULT


def test_futures_close_releases_margin_and_deducts_costs() -> None:
    account = CombinedAccount(cash=1_000.0)
    account.open_futures(
        symbol="IF2607",
        contracts=-1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.20,
        maintenance_margin_rate=0.15,
        fees=5.0,
    )

    assert account.net_asset_value() == pytest.approx(995.0)
    close = account.close_futures("IF2607", settlement_price=100.0, fees=7.0)

    assert close.closed_contracts == 1
    assert close.remaining_contracts == 0
    assert close.margin_transfer == pytest.approx(-200.0)
    assert account.locked_margin == 0.0
    assert account.free_cash == pytest.approx(988.0)
    assert account.net_asset_value() == pytest.approx(988.0)
    assert "IF2607" not in account.futures_positions


def test_option_mark_to_market_and_expiry_settle_exactly_once() -> None:
    expiry = date(2026, 7, 31)
    account = CombinedAccount(cash=1_000.0)
    account.buy_option(
        symbol="IO2607-C-100",
        option_type=OptionType.CALL,
        contracts=1,
        premium=2.0,
        strike=100.0,
        expiry=expiry,
        multiplier=100.0,
        trade_date=TRADE_DATE,
    )

    assert account.free_cash == pytest.approx(800.0)
    assert account.net_asset_value() == pytest.approx(1_000.0)

    account.mark_option(
        "IO2607-C-100",
        liquidation_price=3.0,
        as_of=date(2026, 7, 15),
    )
    assert account.free_cash == pytest.approx(800.0)
    assert account.net_asset_value() == pytest.approx(1_100.0)

    first = account.settle_option_expiry(
        "IO2607-C-100",
        underlying_price=110.0,
        settlement_date=expiry,
    )
    cash_after_first = account.free_cash
    second = account.settle_option_expiry(
        "IO2607-C-100",
        underlying_price=120.0,
        settlement_date=date(2026, 8, 1),
    )

    assert first.cash_flow == pytest.approx(1_000.0)
    assert first.original_cash_flow == pytest.approx(1_000.0)
    assert first.already_settled is False
    assert second.settlement_date == expiry
    assert second.underlying_price == pytest.approx(110.0)
    assert second.intrinsic_value == pytest.approx(10.0)
    assert second.cash_flow == 0.0
    assert second.original_cash_flow == pytest.approx(1_000.0)
    assert second.already_settled is True
    assert account.free_cash == pytest.approx(cash_after_first)
    assert account.net_asset_value() == pytest.approx(1_800.0)
    settled_position = account.option_positions["IO2607-C-100"]
    assert settled_position.settled is True
    assert settled_position.settlement_date == expiry
    assert settled_position.settlement_underlying_price == pytest.approx(110.0)
    assert settled_position.settlement_intrinsic_value == pytest.approx(10.0)
    assert settled_position.settlement_cash_flow == pytest.approx(1_000.0)


def test_option_expiry_fee_is_charged_once_and_preserved_in_audit_state() -> None:
    expiry = date(2026, 7, 31)
    account = CombinedAccount(cash=1_000.0)
    account.buy_option(
        symbol="IO2607-C-100",
        option_type="call",
        contracts=2,
        premium=1.0,
        strike=100.0,
        expiry=expiry,
        multiplier=10.0,
        trade_date=TRADE_DATE,
    )

    first = account.settle_option_expiry(
        "IO2607-C-100",
        settlement_option_price=5.0,
        settlement_date=expiry,
        fees=6.0,
    )
    cash_after_first = account.free_cash
    second = account.settle_option_expiry(
        "IO2607-C-100",
        settlement_option_price=5.0,
        settlement_date=expiry,
        fees=6.0,
    )

    assert first.cash_flow == pytest.approx(94.0)
    assert first.fees == pytest.approx(6.0)
    assert first.original_fees == pytest.approx(6.0)
    assert second.cash_flow == 0.0
    assert second.fees == 0.0
    assert second.original_fees == pytest.approx(6.0)
    assert account.free_cash == pytest.approx(cash_after_first)
    position = account.option_positions["IO2607-C-100"]
    assert position.settlement_cash_flow == pytest.approx(94.0)
    assert position.settlement_fees == pytest.approx(6.0)
    assert position.fees_paid == pytest.approx(6.0)
    with pytest.raises(ValueError, match="conflicting option settlement fees"):
        account.settle_option_expiry(
            "IO2607-C-100",
            settlement_option_price=5.0,
            settlement_date=expiry,
            fees=5.0,
        )


def test_long_put_uses_put_intrinsic_value_at_expiry() -> None:
    expiry = date(2026, 7, 31)
    account = CombinedAccount(cash=1_000.0)
    account.buy_option(
        symbol="IO2607-P-100",
        option_type="put",
        contracts=2,
        premium=1.0,
        strike=100.0,
        expiry=expiry,
        multiplier=10.0,
    )

    settlement = account.settle_option_expiry(
        "IO2607-P-100",
        underlying_price=90.0,
        settlement_date=expiry,
    )

    assert settlement.intrinsic_value == pytest.approx(10.0)
    assert settlement.cash_flow == pytest.approx(200.0)
    assert account.free_cash == pytest.approx(1_180.0)


def test_window_end_only_values_open_option_and_keeps_real_expiry() -> None:
    expiry = date(2026, 12, 31)
    account = CombinedAccount(cash=1_000.0)
    account.buy_option(
        symbol="IO2612-C-100",
        option_type="call",
        contracts=1,
        premium=2.0,
        strike=100.0,
        expiry=expiry,
        multiplier=100.0,
    )
    cash_before = account.free_cash

    nav = account.window_end_nav(option_prices={"IO2612-C-100": 4.0})

    position = account.option_positions["IO2612-C-100"]
    assert nav == pytest.approx(1_200.0)
    assert account.free_cash == pytest.approx(cash_before)
    assert position.expiry == expiry
    assert position.mark_price == pytest.approx(2.0)
    assert position.settled is False


def test_without_derivatives_stock_nav_matches_existing_account() -> None:
    stock_account = Account(cash=1_000.0)
    combined_account = CombinedAccount(cash=1_000.0)

    for account in (stock_account, combined_account):
        account.apply_buy(
            symbol="510300",
            quantity=10,
            price=20.0,
            fees=1.0,
            trade_date=TRADE_DATE,
            available_from=TRADE_DATE,
        )

    prices = {"510300": 25.0}
    assert combined_account.total_equity(prices) == pytest.approx(
        stock_account.total_equity(prices)
    )
    assert combined_account.locked_margin == 0.0


def test_public_nav_requires_every_held_stock_price_but_allows_extra_symbols() -> None:
    account = CombinedAccount(cash=1_000.0)
    account.apply_buy(
        symbol="510300",
        quantity=1,
        price=100.0,
        fees=0.0,
        trade_date=TRADE_DATE,
        available_from=TRADE_DATE,
    )
    account.apply_buy(
        symbol="510500",
        quantity=1,
        price=200.0,
        fees=0.0,
        trade_date=TRADE_DATE,
        available_from=TRADE_DATE,
    )
    incomplete_prices = {"510300": 110.0}

    with pytest.raises(ValueError, match="missing stock prices.*510500"):
        account.net_asset_value(stock_prices=incomplete_prices)
    with pytest.raises(ValueError, match="missing stock prices.*510500"):
        account.total_equity(incomplete_prices)
    with pytest.raises(ValueError, match="missing stock prices.*510500"):
        account.window_end_nav(stock_prices=incomplete_prices)

    complete_with_extra = {
        "510300": 110.0,
        "510500": 220.0,
        "UNHELD": 999.0,
    }
    assert account.net_asset_value(complete_with_extra) == pytest.approx(1_030.0)


def test_alias_and_free_cash_constructor_are_stock_account_compatible() -> None:
    account = UnifiedAccount(free_cash=100.0)

    assert isinstance(account, Account)
    assert isinstance(account, CombinedAccount)
    assert account.cash == 100.0
    assert account.free_cash == 100.0


@pytest.mark.parametrize(
    "contracts,price,initial_rate,maintenance_rate,error",
    [
        (1.5, 100.0, 0.10, 0.08, "non-zero integer"),
        (True, 100.0, 0.10, 0.08, "non-zero integer"),
        (1, float("nan"), 0.10, 0.08, "finite and positive"),
        (1, 0.0, 0.10, 0.08, "finite and positive"),
        (1, -1.0, 0.10, 0.08, "finite and positive"),
        (1, 100.0, 0.0, 0.08, "finite and positive"),
        (1, 100.0, -0.10, 0.08, "finite and positive"),
        (1, 100.0, 0.10, 0.0, "finite and positive"),
        (1, 100.0, 0.10, -0.01, "finite and positive"),
        (1, 100.0, 0.05, 0.08, "cannot exceed"),
    ],
)
def test_futures_parameters_are_strictly_validated(
    contracts: object,
    price: float,
    initial_rate: float,
    maintenance_rate: float,
    error: str,
) -> None:
    account = CombinedAccount(cash=1_000.0)

    with pytest.raises(ValueError, match=error):
        account.open_futures(
            symbol="IF2607",
            contracts=contracts,  # type: ignore[arg-type]
            price=price,
            multiplier=10.0,
            initial_margin_rate=initial_rate,
            maintenance_margin_rate=maintenance_rate,
        )

    assert account.free_cash == 1_000.0
    assert account.locked_margin == 0.0


def test_futures_settlement_close_and_nav_marks_reject_zero_price_atomically() -> None:
    account = CombinedAccount(cash=1_000.0)
    open_long_future(account)
    cash_before = account.free_cash
    position_before = account.futures_positions["IF2607"]

    with pytest.raises(ValueError, match="finite and positive"):
        account.settle_futures("IF2607", 0.0)
    with pytest.raises(ValueError, match="finite and positive"):
        account.close_futures("IF2607", settlement_price=0.0)
    with pytest.raises(ValueError, match="finite and positive"):
        account.net_asset_value(futures_prices={"IF2607": 0.0})

    assert account.free_cash == pytest.approx(cash_before)
    assert account.futures_positions["IF2607"] == position_before


def test_option_rejects_early_expiry_and_non_finite_marks() -> None:
    expiry = date(2026, 7, 31)
    account = CombinedAccount(cash=1_000.0)
    account.buy_option(
        symbol="IO2607-C-100",
        option_type="call",
        contracts=1,
        premium=2.0,
        strike=100.0,
        expiry=expiry,
        multiplier=100.0,
    )

    with pytest.raises(ValueError, match="finite non-negative"):
        account.mark_option("IO2607-C-100", float("inf"))
    with pytest.raises(ValueError, match="before expiry"):
        account.settle_option_expiry(
            "IO2607-C-100",
            underlying_price=110.0,
            settlement_date=date(2026, 7, 30),
        )

    assert account.free_cash == pytest.approx(800.0)
    assert account.option_positions["IO2607-C-100"].settled is False


@pytest.mark.parametrize(
    "multiplier,fees",
    [
        (0.0, 0.0),
        (-1.0, 0.0),
        (float("nan"), 0.0),
        (10.0, -1.0),
        (10.0, float("inf")),
        (10.0, True),
    ],
)
def test_futures_reject_invalid_multiplier_and_fees_atomically(
    multiplier: object,
    fees: object,
) -> None:
    account = CombinedAccount(cash=1_000.0)

    with pytest.raises(ValueError):
        account.open_futures(
            symbol="IF2607",
            contracts=1,
            price=100.0,
            multiplier=multiplier,  # type: ignore[arg-type]
            initial_margin_rate=0.10,
            maintenance_margin_rate=0.08,
            fees=fees,  # type: ignore[arg-type]
        )

    assert account.free_cash == 1_000.0
    assert account.futures_positions == {}


@pytest.mark.parametrize(
    "multiplier,fees",
    [
        (0.0, 0.0),
        (100.0, -1.0),
        (100.0, float("nan")),
    ],
)
def test_option_rejects_invalid_multiplier_and_fees_atomically(
    multiplier: float,
    fees: float,
) -> None:
    account = CombinedAccount(cash=1_000.0)

    with pytest.raises(ValueError):
        account.buy_option(
            symbol="IO2607-C-100",
            option_type="call",
            contracts=1,
            premium=2.0,
            strike=100.0,
            expiry=date(2026, 7, 31),
            multiplier=multiplier,
            fees=fees,
            trade_date=TRADE_DATE,
        )

    assert account.free_cash == 1_000.0
    assert account.option_positions == {}


def test_requested_date_nav_rejects_stale_or_backward_derivative_marks() -> None:
    account = CombinedAccount(cash=2_000.0)
    account.open_futures(
        symbol="IF2607",
        contracts=1,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=TRADE_DATE,
    )
    account.settle_futures("IF2607", 105.0, DAY_2)
    account.buy_option(
        symbol="IO2607-C-100",
        option_type="call",
        contracts=1,
        premium=2.0,
        strike=100.0,
        expiry=date(2026, 7, 31),
        multiplier=100.0,
        trade_date=TRADE_DATE,
    )
    account.mark_option("IO2607-C-100", 3.0, DAY_2)

    assert account.net_asset_value(valuation_date=DAY_2) == pytest.approx(2_150.0)
    with pytest.raises(ValueError, match="precedes option position state"):
        account.net_asset_value(
            option_prices={"IO2607-C-100": 3.0},
            futures_prices={"IF2607": 105.0},
            valuation_date=TRADE_DATE,
        )
    with pytest.raises(ValueError, match="stale futures mark"):
        account.net_asset_value(
            option_prices={"IO2607-C-100": 3.0},
            valuation_date=DAY_3,
        )
    with pytest.raises(ValueError, match="option marks cannot move backwards"):
        account.mark_option("IO2607-C-100", 2.5, TRADE_DATE)

    with pytest.raises(ValueError, match="stale option mark"):
        account.net_asset_value(
            futures_prices={"IF2607": 105.0},
            valuation_date=DAY_3,
        )
    assert account.net_asset_value(
        option_prices={"IO2607-C-100": 3.5},
        futures_prices={"IF2607": 105.0},
        valuation_date=DAY_3,
    ) == pytest.approx(2_200.0)


def test_futures_target_adjustments_conserve_cash_and_margin() -> None:
    account = CombinedAccount(cash=10_000.0)
    account.open_futures(
        symbol="IF2607",
        contracts=2,
        price=100.0,
        multiplier=10.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.08,
        trade_date=TRADE_DATE,
    )

    nav_at_add = account.net_asset_value(futures_prices={"IF2607": 110.0})
    added = account.set_futures_target(
        "IF2607",
        target_contracts=3,
        price=110.0,
        fees=5.0,
        trade_date=DAY_2,
    )
    assert added.action == "add"
    assert added.variation_margin == pytest.approx(200.0)
    assert account.free_cash == pytest.approx(9_865.0)
    assert account.locked_margin == pytest.approx(330.0)
    assert account.net_asset_value() == pytest.approx(nav_at_add - 5.0)

    nav_at_reduce = account.net_asset_value(futures_prices={"IF2607": 90.0})
    reduced = account.set_futures_target(
        "IF2607",
        target_contracts=1,
        price=90.0,
        fees=4.0,
        trade_date=DAY_3,
    )
    assert reduced.action == "reduce"
    assert reduced.variation_margin == pytest.approx(-600.0)
    assert account.free_cash == pytest.approx(9_501.0)
    assert account.locked_margin == pytest.approx(90.0)
    assert account.net_asset_value() == pytest.approx(nav_at_reduce - 4.0)

    nav_at_reverse = account.net_asset_value(futures_prices={"IF2607": 95.0})
    reversed_position = account.set_futures_target(
        "IF2607",
        target_contracts=-2,
        price=95.0,
        fees=6.0,
        trade_date=DAY_4,
    )
    assert reversed_position.action == "reverse"
    assert reversed_position.traded_contracts == 3
    assert account.free_cash == pytest.approx(9_445.0)
    assert account.locked_margin == pytest.approx(190.0)
    assert account.net_asset_value() == pytest.approx(nav_at_reverse - 6.0)

    nav_at_close = account.net_asset_value(futures_prices={"IF2607": 85.0})
    closed = account.set_futures_target(
        "IF2607",
        target_contracts=0,
        price=85.0,
        fees=3.0,
        trade_date=DAY_5,
    )
    assert closed.action == "close"
    assert account.free_cash == pytest.approx(9_832.0)
    assert account.locked_margin == 0.0
    assert account.net_asset_value() == pytest.approx(nav_at_close - 3.0)
    assert account.futures_positions == {}


def test_long_option_add_then_partial_and_full_close_before_expiry() -> None:
    expiry = date(2026, 7, 31)
    account = CombinedAccount(cash=10_000.0)
    account.buy_option(
        symbol="IO2607-C-100",
        option_type="call",
        contracts=2,
        premium=2.0,
        strike=100.0,
        expiry=expiry,
        multiplier=100.0,
        trade_date=TRADE_DATE,
    )
    added = account.add_option(
        symbol="IO2607-C-100",
        option_type="call",
        contracts=1,
        premium=3.0,
        strike=100.0,
        expiry=expiry,
        multiplier=100.0,
        fees=1.0,
        trade_date=DAY_2,
    )

    assert added.contracts == 3
    assert added.premium == pytest.approx(7.0 / 3.0)
    assert added.premium_paid == pytest.approx(700.0)
    assert account.free_cash == pytest.approx(9_299.0)

    partial = account.close_option(
        "IO2607-C-100",
        liquidation_price=4.0,
        contracts=1,
        fees=2.0,
        trade_date=DAY_3,
    )
    remaining = account.option_positions["IO2607-C-100"]
    assert partial.remaining_contracts == 2
    assert partial.gross_proceeds == pytest.approx(400.0)
    assert remaining.premium_paid == pytest.approx(1_400.0 / 3.0)
    assert account.free_cash == pytest.approx(9_697.0)
    assert account.net_asset_value(valuation_date=DAY_3) == pytest.approx(10_497.0)

    full = account.close_option(
        "IO2607-C-100",
        liquidation_price=5.0,
        fees=3.0,
        trade_date=DAY_4,
    )
    assert full.closed_contracts == 2
    assert full.remaining_contracts == 0
    assert account.free_cash == pytest.approx(10_694.0)
    assert account.option_positions == {}


def test_option_expiry_accepts_official_option_settlement_price() -> None:
    expiry = date(2026, 7, 31)
    account = CombinedAccount(cash=1_000.0)
    account.buy_option(
        symbol="IO2607-C-100",
        option_type="call",
        contracts=2,
        premium=1.0,
        strike=100.0,
        expiry=expiry,
        multiplier=10.0,
        trade_date=TRADE_DATE,
    )

    settlement = account.settle_option_expiry(
        "IO2607-C-100",
        settlement_date=expiry,
        settlement_option_price=7.5,
    )

    assert settlement.underlying_price is None
    assert settlement.settlement_option_price == pytest.approx(7.5)
    assert settlement.cash_flow == pytest.approx(150.0)
    assert account.free_cash == pytest.approx(1_130.0)
    assert account.net_asset_value() == pytest.approx(1_130.0)

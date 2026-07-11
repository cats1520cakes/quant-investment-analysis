from __future__ import annotations

from datetime import date, timedelta

import pytest

from quant_proof.engine import (
    Account,
    Broker,
    CostModel,
    ExchangeRules,
    MarketSnapshot,
    Order,
    OrderSide,
    OrderStatus,
    RejectReason,
    RiskLimits,
)


def zero_fee_broker(**risk_kwargs: object) -> Broker:
    return Broker(
        exchange_rules=ExchangeRules(),
        cost_model=CostModel(
            commission_bps=0.0,
            min_commission=0.0,
            transfer_fee_bps=0.0,
            stamp_tax_sell_bps=0.0,
        ),
        risk_limits=RiskLimits(**risk_kwargs),
    )


def snapshot(symbol: str, trade_date: date, price: float, **kwargs: object) -> MarketSnapshot:
    return MarketSnapshot(symbol=symbol, trade_date=trade_date, price=price, **kwargs)


def test_t_plus_one_blocks_same_day_sell_until_next_trade_day() -> None:
    broker = zero_fee_broker()
    account = Account(cash=100_000.0)
    day = date(2026, 7, 1)

    buy_report = broker.buy(
        account=account,
        symbol="510300",
        quantity=100,
        snapshot=snapshot("510300", day, 10.0),
    )

    assert buy_report.status == OrderStatus.FILLED
    assert account.portfolio.quantity("510300") == 100
    assert account.portfolio.available_quantity("510300", day) == 0

    same_day_sell = broker.sell(
        account=account,
        symbol="510300",
        quantity=100,
        snapshot=snapshot("510300", day, 10.0),
    )

    assert same_day_sell.status == OrderStatus.REJECTED
    assert same_day_sell.reason == RejectReason.T_PLUS_ONE
    assert account.portfolio.quantity("510300") == 100

    next_day = day + timedelta(days=1)
    next_day_sell = broker.sell(
        account=account,
        symbol="510300",
        quantity=100,
        snapshot=snapshot("510300", next_day, 10.0),
    )

    assert next_day_sell.status == OrderStatus.FILLED
    assert account.portfolio.quantity("510300") == 0


def test_suspended_security_cannot_trade() -> None:
    broker = zero_fee_broker()
    account = Account(cash=100_000.0)

    report = broker.buy(
        account=account,
        symbol="510300",
        quantity=100,
        snapshot=snapshot("510300", date(2026, 7, 1), 10.0, suspended=True),
    )

    assert report.status == OrderStatus.REJECTED
    assert report.reason == RejectReason.SUSPENDED
    assert account.portfolio.quantity("510300") == 0


def test_limit_up_buy_rejects_without_opening_position() -> None:
    broker = zero_fee_broker()
    account = Account(cash=100_000.0)

    report = broker.buy(
        account=account,
        symbol="510300",
        quantity=100,
        snapshot=snapshot("510300", date(2026, 7, 1), 11.0, limit_up=11.0),
    )

    assert report.status == OrderStatus.REJECTED
    assert report.reason == RejectReason.LIMIT_UP
    assert account.portfolio.quantity("510300") == 0
    assert account.cash == 100_000.0


def test_limit_down_sell_rejects_and_keeps_position() -> None:
    broker = zero_fee_broker()
    account = Account(cash=100_000.0)
    buy_day = date(2026, 7, 1)
    sell_day = date(2026, 7, 2)
    broker.buy(
        account=account,
        symbol="510300",
        quantity=100,
        snapshot=snapshot("510300", buy_day, 10.0),
    )

    report = broker.sell(
        account=account,
        symbol="510300",
        quantity=100,
        snapshot=snapshot("510300", sell_day, 9.0, limit_down=9.0),
    )

    assert report.status == OrderStatus.REJECTED
    assert report.reason == RejectReason.LIMIT_DOWN
    assert account.portfolio.quantity("510300") == 100


def test_stamp_tax_uses_pre_halving_baseline_on_2023_08_25() -> None:
    model = CostModel(stamp_tax_sell_bps=10.0)

    cost = model.calculate(
        OrderSide.SELL,
        1000.0,
        trade_date=date(2023, 8, 25),
    )

    assert cost.stamp_tax == 1.0


def test_stamp_tax_halves_on_2023_08_28() -> None:
    model = CostModel(stamp_tax_sell_bps=10.0)

    cost = model.calculate(
        OrderSide.SELL,
        1000.0,
        trade_date=date(2023, 8, 28),
    )

    assert cost.stamp_tax == 0.5


def test_buy_does_not_pay_stamp_tax() -> None:
    model = CostModel(stamp_tax_sell_bps=10.0)

    cost = model.calculate(
        OrderSide.BUY,
        1000.0,
        trade_date=date(2023, 8, 28),
    )

    assert cost.stamp_tax == 0.0


def test_stamp_tax_without_trade_date_keeps_static_baseline() -> None:
    model = CostModel(stamp_tax_sell_bps=10.0)

    cost = model.calculate(OrderSide.SELL, 1000.0)

    assert cost.stamp_tax == 1.0


def test_stamp_tax_halving_can_be_disabled_or_rescheduled() -> None:
    disabled_model = CostModel(
        stamp_tax_sell_bps=10.0,
        stamp_tax_halving_date=None,
    )
    rescheduled_model = CostModel(
        stamp_tax_sell_bps=10.0,
        stamp_tax_halving_date=date(2023, 8, 29),
    )

    assert (
        disabled_model.calculate(
            OrderSide.SELL,
            1000.0,
            trade_date=date(2026, 7, 1),
        ).stamp_tax
        == 1.0
    )
    assert (
        rescheduled_model.calculate(
            OrderSide.SELL,
            1000.0,
            trade_date=date(2023, 8, 28),
        ).stamp_tax
        == 1.0
    )
    assert (
        rescheduled_model.calculate(
            OrderSide.SELL,
            1000.0,
            trade_date=date(2023, 8, 29),
        ).stamp_tax
        == 0.5
    )


@pytest.mark.parametrize(
    ("trade_date", "symbol", "quantity", "expected"),
    [
        (date(2012, 5, 31), "600001.SH", 10_000, 10.0),
        (date(2012, 6, 1), "600001.SH", 10_000, 7.5),
        (date(2012, 9, 1), "600001.SH", 10_000, 6.0),
        (date(2015, 7, 31), "000001.SZ", 1_000, 0.255),
        (date(2015, 8, 1), "000001.SZ", 1_000, 0.2),
        (date(2022, 4, 28), "000001.SZ", 1_000, 0.2),
        (date(2022, 4, 29), "000001.SZ", 1_000, 0.1),
    ],
)
def test_a_share_transfer_fee_uses_historical_schedule(
    trade_date: date,
    symbol: str,
    quantity: int,
    expected: float,
) -> None:
    model = CostModel(
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.1,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    cost = model.calculate(
        OrderSide.BUY,
        10_000.0,
        trade_date=trade_date,
        symbol=symbol,
        quantity=quantity,
    )

    assert cost.transfer_fee == pytest.approx(expected)


def test_transfer_fee_without_date_or_with_history_disabled_keeps_config_rate() -> None:
    current = CostModel(transfer_fee_bps=0.1)
    static = CostModel(
        transfer_fee_bps=0.1,
        historical_a_share_transfer_fees=False,
    )

    assert current.calculate(OrderSide.BUY, 10_000.0).transfer_fee == pytest.approx(0.1)
    assert static.calculate(
        OrderSide.BUY,
        10_000.0,
        trade_date=date(2012, 1, 1),
        symbol="600001.SH",
        quantity=1_000,
    ).transfer_fee == pytest.approx(0.1)


def test_pre_2015_sse_transfer_fee_has_one_yuan_floor() -> None:
    model = CostModel(transfer_fee_bps=0.1)

    cost = model.calculate(
        OrderSide.BUY,
        10_000.0,
        trade_date=date(2014, 1, 2),
        symbol="600001.SH",
        quantity=100,
    )

    assert cost.transfer_fee == pytest.approx(1.0)


def test_sell_cost_uses_snapshot_date_for_stamp_tax_halving() -> None:
    broker = Broker(
        exchange_rules=ExchangeRules(t_plus_one=False),
        cost_model=CostModel(
            commission_bps=0.0,
            min_commission=0.0,
            transfer_fee_bps=0.0,
            stamp_tax_sell_bps=10.0,
        ),
        risk_limits=RiskLimits(),
    )
    account = Account(cash=100_000.0)
    day = date(2026, 7, 1)
    account.portfolio.add_buy(
        symbol="510300",
        quantity=100,
        price=10.0,
        fees=0.0,
        trade_date=day,
        available_from=day,
    )

    report = broker.sell(
        account=account,
        symbol="510300",
        quantity=100,
        snapshot=snapshot("510300", day, 10.0),
    )

    assert report.status == OrderStatus.FILLED
    assert report.gross_notional == 1000.0
    assert report.stamp_tax == 0.5
    assert report.fees == 0.5
    assert report.net_cash_flow == 999.5


def test_max_order_notional_caps_filled_amount() -> None:
    broker = zero_fee_broker(max_order_notional=1000.0)
    account = Account(cash=100_000.0)
    day = date(2026, 7, 1)
    order = Order(
        symbol="510300",
        side=OrderSide.BUY,
        quantity=1000,
        submitted_at=day,
    )

    report = broker.submit_order(
        account=account,
        order=order,
        snapshot=snapshot("510300", day, 10.0),
    )

    assert report.status == OrderStatus.PARTIALLY_FILLED
    assert report.requested_quantity == 1000
    assert report.filled_quantity == 100
    assert report.gross_notional == 1000.0
    assert account.portfolio.quantity("510300") == 100

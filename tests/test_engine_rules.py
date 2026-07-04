from __future__ import annotations

from datetime import date, timedelta

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


def test_sell_cost_includes_stamp_tax() -> None:
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
    assert report.stamp_tax == 1.0
    assert report.fees == 1.0
    assert report.net_cash_flow == 999.0


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

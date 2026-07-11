from __future__ import annotations

from datetime import date

import pytest

from quant_proof.engine import Account


def test_split_adjusts_each_lot_and_preserves_availability() -> None:
    account = Account(cash=1_000.0)
    trade_date = date(2026, 7, 10)
    available_from = date(2026, 7, 13)
    account.portfolio.add_buy(
        symbol="600000.SH",
        quantity=100,
        price=12.0,
        fees=0.0,
        trade_date=trade_date,
        available_from=available_from,
    )

    result = account.apply_share_factor(
        symbol="600000.SH",
        share_factor=2.0,
        settlement_price=6.0,
    )

    assert result == (100, 200, 0.0)
    assert account.cash == 1_000.0
    lot = account.portfolio.lots["600000.SH"][0]
    assert lot.quantity == 200
    assert isinstance(lot.quantity, int)
    assert lot.cost_basis == pytest.approx(6.0)
    assert lot.trade_date == trade_date
    assert lot.available_from == available_from
    assert account.portfolio.available_quantity("600000.SH", trade_date) == 0
    assert account.portfolio.available_quantity("600000.SH", available_from) == 200


def test_reverse_split_floors_each_lot_and_credits_cash_in_lieu() -> None:
    account = Account(cash=100.0)
    first_trade_date = date(2026, 7, 8)
    second_trade_date = date(2026, 7, 9)
    account.portfolio.add_buy(
        symbol="600000.SH",
        quantity=5,
        price=10.0,
        fees=0.0,
        trade_date=first_trade_date,
        available_from=date(2026, 7, 9),
    )
    account.portfolio.add_buy(
        symbol="600000.SH",
        quantity=3,
        price=12.0,
        fees=0.0,
        trade_date=second_trade_date,
        available_from=date(2026, 7, 10),
    )

    result = account.apply_share_factor(
        symbol="600000.SH",
        share_factor=0.5,
        settlement_price=24.0,
    )

    assert result == (8, 3, 24.0)
    assert account.cash == pytest.approx(124.0)
    lots = account.portfolio.lots["600000.SH"]
    assert [lot.quantity for lot in lots] == [2, 1]
    assert all(isinstance(lot.quantity, int) for lot in lots)
    assert [lot.cost_basis for lot in lots] == pytest.approx([20.0, 24.0])
    assert [lot.trade_date for lot in lots] == [first_trade_date, second_trade_date]
    assert [lot.available_from for lot in lots] == [
        date(2026, 7, 9),
        date(2026, 7, 10),
    ]


def test_share_factor_is_noop_without_a_position() -> None:
    account = Account(cash=321.0)

    result = account.apply_share_factor(
        symbol="600000.SH",
        share_factor=1.5,
        settlement_price=8.0,
    )

    assert result == (0, 0, 0.0)
    assert account.cash == 321.0
    assert "600000.SH" not in account.portfolio.lots


@pytest.mark.parametrize(
    "share_factor",
    [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
)
def test_invalid_share_factor_is_rejected_without_mutation(share_factor: float) -> None:
    account = Account(cash=100.0)
    trade_date = date(2026, 7, 10)
    available_from = date(2026, 7, 13)
    account.portfolio.add_buy(
        symbol="600000.SH",
        quantity=5,
        price=10.0,
        fees=0.0,
        trade_date=trade_date,
        available_from=available_from,
    )

    with pytest.raises(ValueError, match="share_factor must be finite and positive"):
        account.apply_share_factor(
            symbol="600000.SH",
            share_factor=share_factor,
            settlement_price=24.0,
        )

    lot = account.portfolio.lots["600000.SH"][0]
    assert account.cash == 100.0
    assert lot.quantity == 5
    assert lot.cost_basis == 10.0
    assert lot.trade_date == trade_date
    assert lot.available_from == available_from

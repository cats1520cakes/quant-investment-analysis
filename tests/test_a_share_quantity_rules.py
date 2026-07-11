from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from quant_proof.engine import (
    Account,
    Broker,
    CostModel,
    ExchangeRules,
    MarketSnapshot,
    OrderStatus,
    RejectReason,
    RiskLimits,
)
from quant_proof.free_real_backtest import FreeRealBacktestConfig, simulate_free_real_window


TRADE_DATE = date(2026, 7, 13)
STAR_SYMBOL = "688001.SH"


def _broker(
    *,
    t_plus_one: bool = False,
    max_order_notional: float | None = None,
) -> Broker:
    return Broker(
        exchange_rules=ExchangeRules(t_plus_one=t_plus_one),
        cost_model=CostModel(
            commission_bps=0.0,
            min_commission=0.0,
            transfer_fee_bps=0.0,
            stamp_tax_sell_bps=0.0,
            slippage_bps=0.0,
        ),
        risk_limits=RiskLimits(max_order_notional=max_order_notional),
    )


def _snapshot(
    symbol: str,
    *,
    trade_date: date = TRADE_DATE,
    price: float = 10.0,
    board: str | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        trade_date=trade_date,
        price=price,
        limit_up=price * 1.2,
        limit_down=price * 0.8,
        board=board,
    )


def test_snapshot_resolves_quantity_rules_from_symbol_or_board() -> None:
    ordinary = _snapshot("300001.SZ", board="创业板")
    star_by_code = _snapshot("sh.688001")
    star_by_board = _snapshot("600001.SH", board="科创板")

    assert (ordinary.buy_minimum, ordinary.quantity_step, ordinary.odd_lot_threshold) == (
        100,
        100,
        100,
    )
    assert (star_by_code.buy_minimum, star_by_code.quantity_step) == (200, 1)
    assert star_by_code.odd_lot_threshold == 200
    assert star_by_board.quantity_rules == star_by_code.quantity_rules


def test_ordinary_buy_keeps_round_lot_behavior_for_100_and_105_shares() -> None:
    broker = _broker()

    exact = broker.buy(
        account=Account(cash=10_000.0),
        symbol="600001.SH",
        quantity=100,
        snapshot=_snapshot("600001.SH"),
    )
    rounded = broker.buy(
        account=Account(cash=10_000.0),
        symbol="600001.SH",
        quantity=105,
        snapshot=_snapshot("600001.SH"),
    )

    assert exact.status == OrderStatus.FILLED
    assert exact.filled_quantity == 100
    assert rounded.status == OrderStatus.FILLED
    assert rounded.filled_quantity == 100


def test_star_buy_below_200_shares_is_rejected() -> None:
    report = _broker().buy(
        account=Account(cash=10_000.0),
        symbol=STAR_SYMBOL,
        quantity=100,
        snapshot=_snapshot(STAR_SYMBOL),
    )

    assert report.status == OrderStatus.REJECTED
    assert report.reason == RejectReason.INVALID_ORDER


@pytest.mark.parametrize(
    ("quantity", "limit_price"),
    [(200, None), (201, 10.0), (299, None)],
)
def test_star_market_and_limit_buys_allow_one_share_increments(
    quantity: int,
    limit_price: float | None,
) -> None:
    report = _broker().buy(
        account=Account(cash=10_000.0),
        symbol=STAR_SYMBOL,
        quantity=quantity,
        snapshot=_snapshot(STAR_SYMBOL),
        limit_price=limit_price,
    )

    assert report.status == OrderStatus.FILLED
    assert report.filled_quantity == quantity


def test_star_notional_limit_clips_to_a_legal_one_share_increment() -> None:
    report = _broker(max_order_notional=2_500.0).buy(
        account=Account(cash=10_000.0),
        symbol=STAR_SYMBOL,
        quantity=299,
        snapshot=_snapshot(STAR_SYMBOL),
    )

    assert report.status == OrderStatus.PARTIALLY_FILLED
    assert report.filled_quantity == 250
    assert report.gross_notional == 2_500.0


def test_star_odd_lot_waits_for_t_plus_one_and_cannot_be_partially_split() -> None:
    broker = _broker(t_plus_one=True)
    account = Account(cash=0.0)
    next_day = TRADE_DATE + timedelta(days=1)
    account.portfolio.add_buy(
        symbol=STAR_SYMBOL,
        quantity=199,
        price=10.0,
        fees=0.0,
        trade_date=TRADE_DATE,
        available_from=next_day,
    )

    same_day = broker.sell(
        account=account,
        symbol=STAR_SYMBOL,
        quantity=199,
        snapshot=_snapshot(STAR_SYMBOL),
    )
    partial = broker.sell(
        account=account,
        symbol=STAR_SYMBOL,
        quantity=100,
        snapshot=_snapshot(STAR_SYMBOL, trade_date=next_day),
    )
    liquidation = broker.sell(
        account=account,
        symbol=STAR_SYMBOL,
        quantity=199,
        snapshot=_snapshot(STAR_SYMBOL, trade_date=next_day),
    )

    assert same_day.status == OrderStatus.REJECTED
    assert same_day.reason == RejectReason.T_PLUS_ONE
    assert partial.status == OrderStatus.REJECTED
    assert partial.reason == RejectReason.INVALID_ORDER
    assert liquidation.status == OrderStatus.FILLED
    assert liquidation.filled_quantity == 199
    assert account.portfolio.quantity(STAR_SYMBOL) == 0


def test_default_risk_limits_remain_backward_compatible_for_unknown_securities() -> None:
    report = _broker().buy(
        account=Account(cash=100.0),
        symbol="UNKNOWN",
        quantity=1,
        snapshot=_snapshot("UNKNOWN", price=1.0),
    )

    assert report.status == OrderStatus.FILLED
    assert report.filled_quantity == 1


def _daily_row(
    trade_date: str,
    *,
    price: float,
    amount: float = 1_000_000.0,
    share_factor: float = 1.0,
) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "ts_code": STAR_SYMBOL,
        "board": "科创板",
        "open": price,
        "close": price,
        "pre_close": price,
        "corporate_action_share_factor": share_factor,
        "amount": amount,
        "is_suspended": False,
        "up_limit": price * 1.2,
        "down_limit": price * 0.8,
        "delisting_exit_required": False,
    }


def _backtest_config(**overrides: object) -> FreeRealBacktestConfig:
    values: dict[str, object] = {
        "monthly_deposit": 5_000.0,
        "window_months": 1,
        "min_trading_days": 1,
        "lot_size": 100,
        "max_order_notional": None,
        "commission_bps": 0.0,
        "min_commission": 0.0,
        "transfer_fee_bps": 0.0,
        "stamp_tax_sell_bps": 0.0,
        "slippage_bps": 0.0,
    }
    values.update(overrides)
    return FreeRealBacktestConfig(**values)


def test_star_participation_cap_clips_to_a_legal_quantity() -> None:
    dates = ["20200102", "20200103"]
    panel_by_date = {
        "20200102": {
            STAR_SYMBOL: _daily_row("20200102", price=10.0, amount=25_100.0),
        },
        "20200103": {
            STAR_SYMBOL: _daily_row("20200103", price=10.0),
        },
    }

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=dates,
        top_by_signal_date={"20200102": [STAR_SYMBOL]},
        rebalance_dates=set(dates),
        start=pd.Timestamp("2020-01-02"),
        end=pd.Timestamp("2020-01-03"),
        deposit_timing="beginning",
        cfg=_backtest_config(max_daily_amount_participation=0.10),
    )

    assert metrics["filled_orders"] == 1.0
    assert metrics["rejected_orders"] == 0.0
    assert metrics["participation_clipped_orders"] == 1.0
    assert metrics["traded_notional"] == 2_510.0


def test_corporate_action_star_balance_below_200_is_sold_in_full() -> None:
    dates = ["20200102", "20200103", "20200106"]
    ex_price = 2_000.0 / 150.0
    panel_by_date = {
        "20200102": {STAR_SYMBOL: _daily_row("20200102", price=10.0)},
        "20200103": {STAR_SYMBOL: _daily_row("20200103", price=10.0)},
        "20200106": {
            STAR_SYMBOL: _daily_row(
                "20200106",
                price=ex_price,
                share_factor=0.75,
            )
        },
    }

    equity, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=dates,
        top_by_signal_date={"20200102": [STAR_SYMBOL], "20200103": []},
        rebalance_dates=set(dates),
        start=pd.Timestamp("2020-01-02"),
        end=pd.Timestamp("2020-01-06"),
        deposit_timing="beginning",
        cfg=_backtest_config(monthly_deposit=2_000.0),
        rebalance_only_on_target_change=True,
    )

    assert metrics["corporate_action_shares_before"] == 200.0
    assert metrics["corporate_action_shares_after"] == 150.0
    assert metrics["filled_orders"] == 2.0
    assert metrics["rejected_orders"] == 0.0
    assert equity.iloc[-1]["positions"] == 0
    assert equity.iloc[-1]["cash"] == pytest.approx(2_000.0)

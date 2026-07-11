from __future__ import annotations

from datetime import date

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


SYMBOL = "600001.SH"


def _broker(*, max_order_notional: float | None = None) -> Broker:
    return Broker(
        exchange_rules=ExchangeRules(t_plus_one=False),
        cost_model=CostModel(
            commission_bps=0.0,
            min_commission=0.0,
            transfer_fee_bps=0.0,
            stamp_tax_sell_bps=0.0,
            slippage_bps=0.0,
        ),
        risk_limits=RiskLimits(
            max_order_notional=max_order_notional,
            min_quantity=100,
            lot_size=100,
        ),
    )


def _snapshot(trade_date: date, price: float = 10.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=SYMBOL,
        trade_date=trade_date,
        price=price,
        limit_up=price * 1.1,
        limit_down=price * 0.9,
    )


def _daily_row(
    trade_date: str,
    *,
    price: float,
    share_factor: float = 1.0,
) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "ts_code": SYMBOL,
        "open": price,
        "close": price,
        "pre_close": price,
        "corporate_action_share_factor": share_factor,
        "amount": 1_000_000.0,
        "is_suspended": False,
        "up_limit": price * 1.1,
        "down_limit": price * 0.9,
        "delisting_exit_required": False,
    }


def test_buys_remain_round_lot_but_full_odd_lot_sell_is_allowed() -> None:
    broker = _broker()
    account = Account(cash=2_000.0)
    trade_date = date(2026, 7, 13)

    buy = broker.buy(
        account=account,
        symbol=SYMBOL,
        quantity=105,
        snapshot=_snapshot(trade_date),
    )
    account.apply_share_factor(
        symbol=SYMBOL,
        share_factor=1.05,
        settlement_price=10.0,
    )
    partial_odd_lot = broker.sell(
        account=account,
        symbol=SYMBOL,
        quantity=5,
        snapshot=_snapshot(trade_date),
    )
    liquidation = broker.sell(
        account=account,
        symbol=SYMBOL,
        quantity=105,
        snapshot=_snapshot(trade_date),
    )

    assert buy.status == OrderStatus.FILLED
    assert buy.filled_quantity == 100
    assert partial_odd_lot.status == OrderStatus.REJECTED
    assert partial_odd_lot.reason == RejectReason.INVALID_ORDER
    assert liquidation.status == OrderStatus.FILLED
    assert liquidation.filled_quantity == 105
    assert account.portfolio.quantity(SYMBOL) == 0


def test_odd_lot_liquidation_respects_notional_cap_then_clears_remainder() -> None:
    broker = _broker(max_order_notional=1_000.0)
    account = Account(cash=0.0)
    trade_date = date(2026, 7, 13)
    account.portfolio.add_buy(
        symbol=SYMBOL,
        quantity=105,
        price=10.0,
        fees=0.0,
        trade_date=trade_date,
        available_from=trade_date,
    )

    first = broker.sell(
        account=account,
        symbol=SYMBOL,
        quantity=105,
        snapshot=_snapshot(trade_date),
    )
    second = broker.sell(
        account=account,
        symbol=SYMBOL,
        quantity=5,
        snapshot=_snapshot(trade_date),
    )

    assert first.status == OrderStatus.PARTIALLY_FILLED
    assert first.filled_quantity == 100
    assert second.status == OrderStatus.FILLED
    assert second.filled_quantity == 5
    assert account.portfolio.quantity(SYMBOL) == 0


def test_free_real_rebalance_sells_corporate_action_odd_lot_without_residue() -> None:
    dates = ["20200102", "20200103", "20200106"]
    ex_price = 1_000.0 / 105.0
    panel_by_date = {
        "20200102": {
            SYMBOL: _daily_row("20200102", price=10.0),
        },
        "20200103": {
            SYMBOL: _daily_row("20200103", price=10.0),
        },
        "20200106": {
            SYMBOL: _daily_row(
                "20200106",
                price=ex_price,
                share_factor=1.05,
            ),
        },
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=1_000.0,
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

    equity, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=dates,
        top_by_signal_date={"20200102": [SYMBOL], "20200103": []},
        rebalance_dates=set(dates),
        start=pd.Timestamp("2020-01-02"),
        end=pd.Timestamp("2020-01-06"),
        deposit_timing="beginning",
        cfg=cfg,
        rebalance_only_on_target_change=True,
    )

    assert metrics["corporate_action_shares_before"] == 100.0
    assert metrics["corporate_action_shares_after"] == 105.0
    assert metrics["filled_orders"] == 2.0
    assert metrics["clipped_orders"] == 0.0
    assert metrics["traded_notional"] == pytest.approx(2_000.0)
    assert equity.iloc[-1]["positions"] == 0
    assert equity.iloc[-1]["cash"] == pytest.approx(1_000.0)

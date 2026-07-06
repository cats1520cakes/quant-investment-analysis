from __future__ import annotations

import pandas as pd

from quant_proof.free_real_backtest import FreeRealBacktestConfig
from quant_proof.overlay_research import (
    FuturesOverlaySpec,
    OptionOverlaySpec,
    apply_futures_overlay,
    apply_option_overlay,
    black_scholes_call_price,
)


def test_futures_overlay_refuses_fractional_or_unaffordable_lots() -> None:
    index = pd.bdate_range("2020-01-01", periods=80)
    equity = pd.DataFrame(
        {
            "wealth": [100_000.0] * len(index),
            "cash": [5_000.0] * len(index),
        },
        index=index,
    )
    prices = pd.Series([3500.0 + i * 2.0 for i in range(len(index))], index=index)
    spec = FuturesOverlaySpec(contract="IF", target_beta=0.5, margin_rate=0.15, cash_buffer_pct=0.33, signal_lookback=20)

    _, metrics = apply_futures_overlay(equity, prices, spec, FreeRealBacktestConfig())

    assert metrics["avg_futures_lots"] == 0.0
    assert metrics["futures_cannot_afford_events"] > 0.0
    assert metrics["futures_pnl"] == 0.0


def test_futures_overlay_uses_integer_lots_when_cash_allows() -> None:
    index = pd.bdate_range("2020-01-01", periods=80)
    equity = pd.DataFrame(
        {
            "wealth": [2_000_000.0] * len(index),
            "cash": [1_000_000.0] * len(index),
        },
        index=index,
    )
    prices = pd.Series([3500.0 + i * 5.0 for i in range(len(index))], index=index)
    spec = FuturesOverlaySpec(contract="IF", target_beta=1.0, margin_rate=0.15, cash_buffer_pct=0.50, signal_lookback=20)

    _, metrics = apply_futures_overlay(equity, prices, spec, FreeRealBacktestConfig())

    assert metrics["avg_futures_lots"] >= 0.0
    assert metrics["futures_rebalance_events"] >= 3.0
    assert metrics["futures_pnl"] > 0.0


def test_parametric_option_overlay_spends_premium_and_collects_payoff() -> None:
    index = pd.bdate_range("2020-01-01", periods=120)
    equity = pd.DataFrame(
        {
            "wealth": [1_000_000.0] * len(index),
            "cash": [500_000.0] * len(index),
        },
        index=index,
    )
    prices = pd.Series([3000.0 + i * 12.0 for i in range(len(index))], index=index)
    spec = OptionOverlaySpec(
        contract="IO",
        monthly_budget_pct_nav=0.05,
        tenor_days=30,
        delta=0.50,
        iv_multiplier=1.3,
        signal_lookback=20,
    )

    _, metrics = apply_option_overlay(equity, prices, spec, FreeRealBacktestConfig())

    assert metrics["option_contracts_bought"] > 0.0
    assert metrics["option_premium_spent"] > 0.0
    assert metrics["option_payoff"] > 0.0


def test_black_scholes_call_price_is_positive_for_atm_option() -> None:
    assert black_scholes_call_price(spot=100.0, strike=100.0, years=30 / 365, sigma=0.2) > 0.0

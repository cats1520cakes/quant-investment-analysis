from __future__ import annotations

import pandas as pd
import pytest

from quant_proof.simulator import summarize_equity


def test_deposit_cannot_hide_loss_from_flow_adjusted_drawdown() -> None:
    equity = pd.DataFrame(
        {
            "wealth": [100.0, 180.0, 180.0],
            "daily_return": [0.0, -0.10, 0.0],
        },
        index=pd.to_datetime(["2024-01-02", "2024-02-01", "2024-02-02"]),
    )

    metrics = summarize_equity(equity, monthly_deposit=100.0)

    assert equity["wealth"].is_monotonic_increasing
    assert metrics["w12"] == 180.0
    assert metrics["w24"] == 180.0
    assert metrics["total_deposit"] == 200.0
    assert metrics["max_drawdown"] == pytest.approx(0.10)
    assert metrics["ulcer_index"] == pytest.approx((200.0 / 4.0) ** 0.5)
    assert metrics["recovery_days"] == 2.0
    assert metrics["p99_one_day_loss"] == pytest.approx(equity["daily_return"].quantile(0.01))
    assert metrics["expected_shortfall_95"] == pytest.approx(-0.10)


def test_lossless_deposits_do_not_create_flow_adjusted_drawdown() -> None:
    index = pd.date_range("2024-01-01", periods=13, freq="MS")
    equity = pd.DataFrame(
        {
            "wealth": [100.0 * month for month in range(1, 14)],
            "daily_return": [0.0] * 13,
        },
        index=index,
    )

    metrics = summarize_equity(equity, monthly_deposit=100.0)

    assert metrics["w12"] == 1_200.0
    assert metrics["w24"] == 1_300.0
    assert metrics["total_deposit"] == 1_300.0
    assert metrics["max_drawdown"] == 0.0
    assert metrics["ulcer_index"] == 0.0
    assert metrics["recovery_days"] == 0.0


def test_missing_daily_return_fails_explicitly() -> None:
    equity = pd.DataFrame(
        {"wealth": [100.0, 200.0]},
        index=pd.to_datetime(["2024-01-02", "2024-02-01"]),
    )

    with pytest.raises(ValueError, match="daily_return"):
        summarize_equity(equity, monthly_deposit=100.0)


def test_first_day_loss_draws_down_from_initial_nav() -> None:
    equity = pd.DataFrame(
        {"wealth": [80.0, 80.0], "daily_return": [-0.20, 0.0]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )

    metrics = summarize_equity(equity, monthly_deposit=100.0)

    assert metrics["max_drawdown"] == pytest.approx(0.20)

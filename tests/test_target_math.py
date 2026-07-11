from __future__ import annotations

import pytest

from quant_proof.target_math import annualize_monthly_return, contribution_future_value, required_monthly_return


def test_zero_return_equals_total_deposits() -> None:
    assert contribution_future_value(30_000.0, 24, 0.0, "beginning") == 720_000.0
    assert contribution_future_value(30_000.0, 24, 0.0, "ending") == 720_000.0


@pytest.mark.parametrize(
    ("months", "target", "timing", "expected_monthly", "expected_annualized"),
    [
        (12, 500_000.0, "beginning", 0.049588, 0.7874),
        (12, 500_000.0, "ending", 0.057922, 0.9654),
        (24, 1_200_000.0, "beginning", 0.038838, 0.5797),
        (24, 1_200_000.0, "ending", 0.041803, 0.6347),
    ],
)
def test_required_return_hurdles(
    months: int,
    target: float,
    timing: str,
    expected_monthly: float,
    expected_annualized: float,
) -> None:
    monthly = required_monthly_return(30_000.0, months, target, timing)

    assert monthly == pytest.approx(expected_monthly, abs=1e-6)
    assert annualize_monthly_return(monthly) == pytest.approx(expected_annualized, abs=1e-4)
    assert contribution_future_value(30_000.0, months, monthly, timing) == pytest.approx(target, abs=1e-4)

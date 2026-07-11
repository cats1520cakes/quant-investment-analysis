from __future__ import annotations


def contribution_future_value(
    monthly_deposit: float,
    months: int,
    monthly_return: float,
    deposit_timing: str,
) -> float:
    if months <= 0:
        raise ValueError("months must be positive")
    if monthly_deposit < 0.0:
        raise ValueError("monthly_deposit must be non-negative")
    if monthly_return <= -1.0:
        raise ValueError("monthly_return must be greater than -1")
    if deposit_timing not in {"beginning", "ending"}:
        raise ValueError("deposit_timing must be beginning or ending")
    first_exponent = months if deposit_timing == "beginning" else months - 1
    return float(
        monthly_deposit
        * sum((1.0 + monthly_return) ** (first_exponent - deposit_index) for deposit_index in range(months))
    )


def required_monthly_return(
    monthly_deposit: float,
    months: int,
    target_wealth: float,
    deposit_timing: str,
    tolerance: float = 1e-12,
) -> float:
    if target_wealth < 0.0:
        raise ValueError("target_wealth must be non-negative")
    if target_wealth <= monthly_deposit * months:
        return 0.0
    lower = 0.0
    upper = 1.0
    while contribution_future_value(monthly_deposit, months, upper, deposit_timing) < target_wealth:
        upper *= 2.0
    for _ in range(200):
        midpoint = (lower + upper) / 2.0
        value = contribution_future_value(monthly_deposit, months, midpoint, deposit_timing)
        if value < target_wealth:
            lower = midpoint
        else:
            upper = midpoint
        if upper - lower <= tolerance:
            break
    return float((lower + upper) / 2.0)


def annualize_monthly_return(monthly_return: float) -> float:
    return float((1.0 + monthly_return) ** 12 - 1.0)

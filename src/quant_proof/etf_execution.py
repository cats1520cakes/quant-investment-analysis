from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite


class EtfExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class EtfExecutionBar:
    raw_open: float | None
    hfq_signal: float | None
    tradable: bool
    company_action_status: str = "none"


@dataclass(frozen=True)
class EtfFill:
    quantity: int
    raw_price: float
    cash_after: float
    signal_value: float


def execute_etf_buy(
    *, cash_before: float, monthly_deposit: float, deposit_timing: str,
    requested_notional: float, bar: EtfExecutionBar, lot_size: int = 100,
) -> EtfFill:
    if deposit_timing not in {"beginning", "ending"}:
        raise ValueError("deposit_timing must be beginning or ending")
    if lot_size != 100:
        raise EtfExecutionError("strict ETF execution requires 100-share board lots")
    if bar.company_action_status not in {"none", "official_ledger_complete"}:
        raise EtfExecutionError("company action ledger is incomplete")
    if not bar.tradable or bar.raw_open is None or not isfinite(bar.raw_open) or bar.raw_open <= 0:
        raise EtfExecutionError("ETF raw execution price is unavailable or suspended")
    if bar.hfq_signal is None or not isfinite(bar.hfq_signal):
        raise EtfExecutionError("ETF hfq signal value is unavailable")
    available = cash_before + (monthly_deposit if deposit_timing == "beginning" else 0.0)
    budget = min(max(requested_notional, 0.0), available)
    quantity = floor(budget / (bar.raw_open * lot_size)) * lot_size
    cost = quantity * bar.raw_open
    cash_after = available - cost + (monthly_deposit if deposit_timing == "ending" else 0.0)
    return EtfFill(quantity=quantity, raw_price=bar.raw_open, cash_after=cash_after, signal_value=bar.hfq_signal)

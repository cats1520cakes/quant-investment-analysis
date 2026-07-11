from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from math import isfinite
from numbers import Integral
from typing import Dict, List


@dataclass
class Lot:
    symbol: str
    quantity: int
    cost_basis: float
    trade_date: date
    available_from: date


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    available_quantity: int
    average_cost: float = 0.0


@dataclass
class Portfolio:
    lots: Dict[str, List[Lot]] = field(default_factory=dict)

    def quantity(self, symbol: str) -> int:
        return sum(lot.quantity for lot in self.lots.get(symbol, []))

    def available_quantity(self, symbol: str, on_date: date) -> int:
        return sum(
            lot.quantity
            for lot in self.lots.get(symbol, [])
            if lot.available_from <= on_date
        )

    def position(self, symbol: str, on_date: date | None = None) -> Position:
        lots = self.lots.get(symbol, [])
        quantity = sum(lot.quantity for lot in lots)
        if on_date is None:
            available_quantity = quantity
        else:
            available_quantity = self.available_quantity(symbol, on_date)
        if quantity <= 0:
            return Position(symbol=symbol, quantity=0, available_quantity=0)
        average_cost = sum(lot.quantity * lot.cost_basis for lot in lots) / quantity
        return Position(
            symbol=symbol,
            quantity=quantity,
            available_quantity=available_quantity,
            average_cost=average_cost,
        )

    def add_buy(
        self,
        symbol: str,
        quantity: int,
        price: float,
        fees: float,
        trade_date: date,
        available_from: date,
    ) -> None:
        if quantity <= 0:
            return
        gross_notional = quantity * price
        cost_basis = (gross_notional + fees) / quantity
        self.lots.setdefault(symbol, []).append(
            Lot(
                symbol=symbol,
                quantity=quantity,
                cost_basis=cost_basis,
                trade_date=trade_date,
                available_from=available_from,
            )
        )

    def remove_sell(self, symbol: str, quantity: int, trade_date: date) -> None:
        if quantity <= 0:
            return
        if self.available_quantity(symbol, trade_date) < quantity:
            raise ValueError(f"insufficient available quantity for {symbol}")

        remaining = quantity
        updated_lots: List[Lot] = []
        for lot in self.lots.get(symbol, []):
            if remaining > 0 and lot.available_from <= trade_date:
                sold = min(lot.quantity, remaining)
                lot.quantity -= sold
                remaining -= sold
            if lot.quantity > 0:
                updated_lots.append(lot)

        if updated_lots:
            self.lots[symbol] = updated_lots
        else:
            self.lots.pop(symbol, None)

    def apply_share_factor(
        self,
        symbol: str,
        share_factor: float,
        settlement_price: float,
    ) -> tuple[int, int, float]:
        try:
            factor = Decimal(str(share_factor))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("share_factor must be finite and positive") from None
        if not factor.is_finite() or factor <= 0:
            raise ValueError("share_factor must be finite and positive")

        try:
            cash_price = Decimal(str(settlement_price))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("settlement_price must be finite and non-negative") from None
        if not cash_price.is_finite() or cash_price < 0:
            raise ValueError("settlement_price must be finite and non-negative")

        lots = self.lots.get(symbol, [])
        if not lots:
            return 0, 0, 0.0

        old_quantity = 0
        new_quantity = 0
        cash_in_lieu = Decimal("0")
        adjustments: List[tuple[Lot, int, float]] = []

        for lot in lots:
            if not isinstance(lot.quantity, Integral) or lot.quantity < 0:
                raise ValueError("lot quantity must be a non-negative integer")
            lot_quantity = int(lot.quantity)
            exact_quantity = Decimal(lot_quantity) * factor
            floored_quantity = exact_quantity.to_integral_value(rounding=ROUND_FLOOR)
            adjusted_quantity = int(floored_quantity)

            try:
                adjusted_cost_basis = float(Decimal(str(lot.cost_basis)) / factor)
            except (InvalidOperation, OverflowError, TypeError, ValueError):
                raise ValueError("share_factor produces a non-finite cost_basis") from None
            if not isfinite(adjusted_cost_basis):
                raise ValueError("share_factor produces a non-finite cost_basis")

            old_quantity += lot_quantity
            new_quantity += adjusted_quantity
            cash_in_lieu += (exact_quantity - floored_quantity) * cash_price
            adjustments.append((lot, adjusted_quantity, adjusted_cost_basis))

        try:
            cash_in_lieu_value = float(cash_in_lieu)
        except (OverflowError, ValueError):
            raise ValueError("corporate action produces non-finite cash-in-lieu") from None
        if not isfinite(cash_in_lieu_value):
            raise ValueError("corporate action produces non-finite cash-in-lieu")

        adjusted_lots: List[Lot] = []
        for lot, adjusted_quantity, adjusted_cost_basis in adjustments:
            if adjusted_quantity == 0:
                continue
            lot.quantity = adjusted_quantity
            lot.cost_basis = adjusted_cost_basis
            adjusted_lots.append(lot)

        if adjusted_lots:
            self.lots[symbol] = adjusted_lots
        else:
            self.lots.pop(symbol, None)

        return old_quantity, new_quantity, cash_in_lieu_value

    def market_value(self, prices: Dict[str, float]) -> float:
        return sum(self.quantity(symbol) * price for symbol, price in prices.items())

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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

    def market_value(self, prices: Dict[str, float]) -> float:
        return sum(self.quantity(symbol) * price for symbol, price in prices.items())

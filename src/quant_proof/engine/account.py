from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict

from .portfolio import Portfolio


@dataclass
class Account:
    cash: float
    portfolio: Portfolio = field(default_factory=Portfolio)

    def apply_buy(
        self,
        symbol: str,
        quantity: int,
        price: float,
        fees: float,
        trade_date: date,
        available_from: date,
    ) -> None:
        gross_notional = quantity * price
        required_cash = gross_notional + fees
        if required_cash > self.cash + 1e-9:
            raise ValueError("insufficient cash")
        self.cash -= required_cash
        self.portfolio.add_buy(
            symbol=symbol,
            quantity=quantity,
            price=price,
            fees=fees,
            trade_date=trade_date,
            available_from=available_from,
        )

    def apply_sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        fees: float,
        trade_date: date,
    ) -> None:
        gross_notional = quantity * price
        self.portfolio.remove_sell(symbol=symbol, quantity=quantity, trade_date=trade_date)
        self.cash += gross_notional - fees

    def apply_share_factor(
        self,
        symbol: str,
        share_factor: float,
        settlement_price: float,
    ) -> tuple[int, int, float]:
        old_quantity, new_quantity, cash_in_lieu = self.portfolio.apply_share_factor(
            symbol=symbol,
            share_factor=share_factor,
            settlement_price=settlement_price,
        )
        self.cash += cash_in_lieu
        return old_quantity, new_quantity, cash_in_lieu

    def total_equity(self, prices: Dict[str, float]) -> float:
        return self.cash + self.portfolio.market_value(prices)

    def apply_terminal_value(self, symbol: str, price: float) -> tuple[int, float]:
        quantity = self.portfolio.quantity(symbol)
        if quantity <= 0:
            return 0, 0.0
        recovery = quantity * max(float(price), 0.0)
        self.portfolio.lots.pop(symbol, None)
        self.cash += recovery
        return quantity, recovery

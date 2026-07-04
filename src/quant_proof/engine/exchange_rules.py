from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from .orders import Order, OrderSide, OrderType, RejectReason
from .portfolio import Portfolio


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    trade_date: date
    price: float
    suspended: bool = False
    limit_up: float | None = None
    limit_down: float | None = None

    def is_limit_up(self, epsilon: float = 1e-9) -> bool:
        return self.limit_up is not None and self.price >= self.limit_up - epsilon

    def is_limit_down(self, epsilon: float = 1e-9) -> bool:
        return self.limit_down is not None and self.price <= self.limit_down + epsilon


@dataclass(frozen=True)
class RuleCheck:
    allowed: bool
    reason: RejectReason | None = None
    message: str = ""


@dataclass(frozen=True)
class ExchangeRules:
    t_plus_one: bool = True
    trading_calendar: Sequence[date] | None = None
    price_epsilon: float = 1e-9

    def next_trade_date(self, trade_date: date) -> date:
        if self.trading_calendar:
            for candidate in sorted(self.trading_calendar):
                if candidate > trade_date:
                    return candidate
        return trade_date + timedelta(days=1)

    def available_from_buy(self, trade_date: date) -> date:
        if not self.t_plus_one:
            return trade_date
        return self.next_trade_date(trade_date)

    def check_order(self, order: Order, snapshot: MarketSnapshot, portfolio: Portfolio) -> RuleCheck:
        if order.symbol != snapshot.symbol:
            return RuleCheck(
                allowed=False,
                reason=RejectReason.INVALID_ORDER,
                message="order symbol does not match market snapshot",
            )
        if order.quantity <= 0 or snapshot.price <= 0.0:
            return RuleCheck(
                allowed=False,
                reason=RejectReason.INVALID_ORDER,
                message="order quantity and execution price must be positive",
            )
        if snapshot.suspended:
            return RuleCheck(
                allowed=False,
                reason=RejectReason.SUSPENDED,
                message="security is suspended",
            )

        if order.side == OrderSide.BUY and snapshot.is_limit_up(self.price_epsilon):
            return RuleCheck(
                allowed=False,
                reason=RejectReason.LIMIT_UP,
                message="buy cannot be filled at limit-up",
            )
        if order.side == OrderSide.SELL and snapshot.is_limit_down(self.price_epsilon):
            return RuleCheck(
                allowed=False,
                reason=RejectReason.LIMIT_DOWN,
                message="sell cannot be filled at limit-down",
            )

        if order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                return RuleCheck(
                    allowed=False,
                    reason=RejectReason.INVALID_ORDER,
                    message="limit order requires limit_price",
                )
            if order.side == OrderSide.BUY and snapshot.price > order.limit_price + self.price_epsilon:
                return RuleCheck(
                    allowed=False,
                    reason=RejectReason.PRICE_NOT_MARKETABLE,
                    message="buy limit price is below execution price",
                )
            if order.side == OrderSide.SELL and snapshot.price < order.limit_price - self.price_epsilon:
                return RuleCheck(
                    allowed=False,
                    reason=RejectReason.PRICE_NOT_MARKETABLE,
                    message="sell limit price is above execution price",
                )

        if order.side == OrderSide.SELL:
            available_quantity = portfolio.available_quantity(order.symbol, snapshot.trade_date)
            if available_quantity < order.quantity:
                total_quantity = portfolio.quantity(order.symbol)
                reason = RejectReason.INSUFFICIENT_POSITION
                message = "insufficient position"
                if self.t_plus_one and total_quantity >= order.quantity:
                    reason = RejectReason.T_PLUS_ONE
                    message = "same-day buy is not sellable under T+1"
                return RuleCheck(allowed=False, reason=reason, message=message)

        return RuleCheck(allowed=True)

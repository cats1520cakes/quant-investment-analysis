from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from .orders import Order, OrderSide, OrderType, RejectReason
from .portfolio import Portfolio


@dataclass(frozen=True)
class ShareQuantityRules:
    buy_minimum: int
    quantity_step: int
    odd_lot_threshold: int

    def __post_init__(self) -> None:
        if min(self.buy_minimum, self.quantity_step, self.odd_lot_threshold) <= 0:
            raise ValueError("share quantity rules must be positive")

    @staticmethod
    def _normalize(quantity: int, minimum: int, step: int) -> int:
        quantity = int(quantity)
        if quantity < minimum:
            return 0
        return minimum + ((quantity - minimum) // step) * step

    def normalize_buy(self, quantity: int) -> int:
        return self._normalize(quantity, self.buy_minimum, self.quantity_step)

    def normalize_regular_sell(self, quantity: int) -> int:
        return self._normalize(quantity, self.odd_lot_threshold, self.quantity_step)

    def is_valid_buy(self, quantity: int) -> bool:
        return int(quantity) == self.normalize_buy(quantity)

    def is_valid_regular_sell(self, quantity: int) -> bool:
        return int(quantity) == self.normalize_regular_sell(quantity)


ORDINARY_A_SHARE_QUANTITY_RULES = ShareQuantityRules(
    buy_minimum=100,
    quantity_step=100,
    odd_lot_threshold=100,
)
STAR_MARKET_QUANTITY_RULES = ShareQuantityRules(
    buy_minimum=200,
    quantity_step=1,
    odd_lot_threshold=200,
)


def _security_code(symbol: str) -> str:
    value = str(symbol).strip().lower()
    if value.startswith(("sh.", "sz.")):
        return value[3:]
    if "." in value:
        return value.split(".", 1)[0]
    return value


def quantity_rules_for(symbol: str, board: str | None = None) -> ShareQuantityRules | None:
    code = _security_code(symbol)
    board_name = "" if board is None else str(board).strip().lower()
    compact_board = board_name.replace("-", "").replace("_", "").replace(" ", "")

    if code.startswith("688") or compact_board in {
        "科创板",
        "star",
        "starmarket",
        "kcb",
    }:
        return STAR_MARKET_QUANTITY_RULES

    if compact_board in {
        "主板",
        "main",
        "mainboard",
        "创业板",
        "chinext",
        "gem",
    }:
        return ORDINARY_A_SHARE_QUANTITY_RULES

    if len(code) == 6 and code.isdigit() and code.startswith(
        ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605")
    ):
        return ORDINARY_A_SHARE_QUANTITY_RULES
    return None


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    trade_date: date
    price: float
    suspended: bool = False
    limit_up: float | None = None
    limit_down: float | None = None
    board: str | None = None

    @property
    def quantity_rules(self) -> ShareQuantityRules | None:
        return quantity_rules_for(self.symbol, self.board)

    @property
    def buy_minimum(self) -> int | None:
        rules = self.quantity_rules
        return None if rules is None else rules.buy_minimum

    @property
    def quantity_step(self) -> int | None:
        rules = self.quantity_rules
        return None if rules is None else rules.quantity_step

    @property
    def odd_lot_threshold(self) -> int | None:
        rules = self.quantity_rules
        return None if rules is None else rules.odd_lot_threshold

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

        quantity_rules = snapshot.quantity_rules
        if quantity_rules is not None:
            if order.side == OrderSide.BUY and not quantity_rules.is_valid_buy(order.quantity):
                return RuleCheck(
                    allowed=False,
                    reason=RejectReason.INVALID_ORDER,
                    message="buy quantity does not satisfy the security's board rules",
                )
            if order.side == OrderSide.SELL and not quantity_rules.is_valid_regular_sell(
                order.quantity
            ):
                total_quantity = portfolio.quantity(order.symbol)
                if order.quantity != total_quantity:
                    return RuleCheck(
                        allowed=False,
                        reason=RejectReason.INVALID_ORDER,
                        message="odd-lot balance must be sold in one order",
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

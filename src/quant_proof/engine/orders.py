from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Dict
from uuid import uuid4


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    NEW = "new"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class RejectReason(str, Enum):
    INVALID_ORDER = "invalid_order"
    SUSPENDED = "suspended"
    LIMIT_UP = "limit_up"
    LIMIT_DOWN = "limit_down"
    T_PLUS_ONE = "t_plus_one"
    PRICE_NOT_MARKETABLE = "price_not_marketable"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_POSITION = "insufficient_position"
    NOTIONAL_LIMIT = "notional_limit"


@dataclass(frozen=True)
class Order:
    symbol: str
    side: OrderSide
    quantity: int
    submitted_at: date
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    order_id: str = field(default_factory=lambda: uuid4().hex)
    metadata: Dict[str, object] = field(default_factory=dict)

    @property
    def is_buy(self) -> bool:
        return self.side == OrderSide.BUY

    @property
    def is_sell(self) -> bool:
        return self.side == OrderSide.SELL

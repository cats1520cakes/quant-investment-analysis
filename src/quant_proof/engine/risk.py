from __future__ import annotations

from dataclasses import dataclass
from math import floor

from .orders import Order, RejectReason


@dataclass(frozen=True)
class RiskDecision:
    quantity: int
    clipped: bool = False
    reason: RejectReason | None = None
    message: str = ""

    @property
    def allowed(self) -> bool:
        return self.reason is None and self.quantity > 0


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float | None = None
    min_quantity: int = 1
    lot_size: int = 1

    def limit_quantity(self, order: Order, price: float) -> RiskDecision:
        if order.quantity <= 0 or price <= 0.0:
            return RiskDecision(
                quantity=0,
                reason=RejectReason.INVALID_ORDER,
                message="order quantity and execution price must be positive",
            )

        quantity = int(order.quantity)
        if self.lot_size > 1:
            quantity = (quantity // self.lot_size) * self.lot_size
        if quantity < self.min_quantity:
            return RiskDecision(
                quantity=0,
                reason=RejectReason.INVALID_ORDER,
                message="order quantity is below the minimum executable size",
            )

        clipped = False
        if self.max_order_notional is not None:
            if self.max_order_notional <= 0.0:
                return RiskDecision(
                    quantity=0,
                    reason=RejectReason.NOTIONAL_LIMIT,
                    message="max order notional is non-positive",
                )
            max_quantity = floor(self.max_order_notional / price)
            if self.lot_size > 1:
                max_quantity = (max_quantity // self.lot_size) * self.lot_size
            if max_quantity < self.min_quantity:
                return RiskDecision(
                    quantity=0,
                    reason=RejectReason.NOTIONAL_LIMIT,
                    message="price is above the max order notional",
                )
            if quantity > max_quantity:
                quantity = max_quantity
                clipped = True

        return RiskDecision(quantity=quantity, clipped=clipped)

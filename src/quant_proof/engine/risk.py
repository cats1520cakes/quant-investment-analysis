from __future__ import annotations

from dataclasses import dataclass
from math import floor

from .exchange_rules import ShareQuantityRules
from .orders import Order, OrderSide, RejectReason


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

    def limit_quantity(
        self,
        order: Order,
        price: float,
        *,
        allow_odd_lot_liquidation: bool = False,
        quantity_rules: ShareQuantityRules | None = None,
    ) -> RiskDecision:
        if order.quantity <= 0 or price <= 0.0:
            return RiskDecision(
                quantity=0,
                reason=RejectReason.INVALID_ORDER,
                message="order quantity and execution price must be positive",
            )

        quantity = int(order.quantity)
        if allow_odd_lot_liquidation:
            minimum_quantity = 1
        elif quantity_rules is not None:
            if order.side == OrderSide.BUY:
                minimum_quantity = quantity_rules.buy_minimum
                quantity = quantity_rules.normalize_buy(quantity)
            else:
                minimum_quantity = quantity_rules.odd_lot_threshold
                quantity = quantity_rules.normalize_regular_sell(quantity)
        else:
            minimum_quantity = self.min_quantity
            if self.lot_size > 1:
                quantity = (quantity // self.lot_size) * self.lot_size
        if quantity < minimum_quantity:
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
            full_liquidation_fits = allow_odd_lot_liquidation and quantity <= max_quantity
            if quantity_rules is not None and not full_liquidation_fits:
                if order.side == OrderSide.BUY:
                    max_quantity = quantity_rules.normalize_buy(max_quantity)
                else:
                    max_quantity = quantity_rules.normalize_regular_sell(max_quantity)
            elif self.lot_size > 1 and not full_liquidation_fits:
                max_quantity = (max_quantity // self.lot_size) * self.lot_size
            if max_quantity < minimum_quantity:
                return RiskDecision(
                    quantity=0,
                    reason=RejectReason.NOTIONAL_LIMIT,
                    message="price is above the max order notional",
                )
            if quantity > max_quantity:
                quantity = max_quantity
                clipped = True

        return RiskDecision(quantity=quantity, clipped=clipped)

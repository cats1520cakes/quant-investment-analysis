from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from .account import Account
from .cost import CostModel
from .exchange_rules import ExchangeRules, MarketSnapshot
from .orders import Order, OrderSide, OrderStatus, RejectReason
from .risk import RiskLimits


@dataclass(frozen=True)
class ExecutionReport:
    order_id: str
    symbol: str
    side: OrderSide
    status: OrderStatus
    trade_date: date
    requested_quantity: int
    filled_quantity: int = 0
    price: float = 0.0
    gross_notional: float = 0.0
    fees: float = 0.0
    stamp_tax: float = 0.0
    net_cash_flow: float = 0.0
    reason: RejectReason | None = None
    message: str = ""

    @property
    def filled(self) -> bool:
        return self.status in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}


def rejected_report(
    order: Order,
    snapshot: MarketSnapshot,
    reason: RejectReason,
    message: str,
) -> ExecutionReport:
    return ExecutionReport(
        order_id=order.order_id,
        symbol=order.symbol,
        side=order.side,
        status=OrderStatus.REJECTED,
        trade_date=snapshot.trade_date,
        requested_quantity=order.quantity,
        price=snapshot.price,
        reason=reason,
        message=message,
    )


@dataclass(frozen=True)
class ExecutionEngine:
    exchange_rules: ExchangeRules
    cost_model: CostModel
    risk_limits: RiskLimits

    def execute(self, account: Account, order: Order, snapshot: MarketSnapshot) -> ExecutionReport:
        risk_decision = self.risk_limits.limit_quantity(order, snapshot.price)
        if not risk_decision.allowed:
            return rejected_report(
                order=order,
                snapshot=snapshot,
                reason=risk_decision.reason or RejectReason.INVALID_ORDER,
                message=risk_decision.message,
            )

        effective_order = replace(order, quantity=risk_decision.quantity)
        rule_check = self.exchange_rules.check_order(
            order=effective_order,
            snapshot=snapshot,
            portfolio=account.portfolio,
        )
        if not rule_check.allowed:
            return rejected_report(
                order=order,
                snapshot=snapshot,
                reason=rule_check.reason or RejectReason.INVALID_ORDER,
                message=rule_check.message,
            )

        filled_quantity = risk_decision.quantity
        gross_notional = filled_quantity * snapshot.price
        trade_cost = self.cost_model.calculate(effective_order.side, gross_notional)
        total_fees = trade_cost.total

        if effective_order.side == OrderSide.BUY:
            required_cash = gross_notional + total_fees
            if required_cash > account.cash + 1e-9:
                return rejected_report(
                    order=order,
                    snapshot=snapshot,
                    reason=RejectReason.INSUFFICIENT_CASH,
                    message="insufficient cash after fees",
                )
            account.apply_buy(
                symbol=effective_order.symbol,
                quantity=filled_quantity,
                price=snapshot.price,
                fees=total_fees,
                trade_date=snapshot.trade_date,
                available_from=self.exchange_rules.available_from_buy(snapshot.trade_date),
            )
            net_cash_flow = -required_cash
        else:
            account.apply_sell(
                symbol=effective_order.symbol,
                quantity=filled_quantity,
                price=snapshot.price,
                fees=total_fees,
                trade_date=snapshot.trade_date,
            )
            net_cash_flow = gross_notional - total_fees

        status = OrderStatus.FILLED
        message = ""
        if risk_decision.clipped:
            status = OrderStatus.PARTIALLY_FILLED
            message = "filled quantity was clipped by max_order_notional"

        return ExecutionReport(
            order_id=order.order_id,
            symbol=effective_order.symbol,
            side=effective_order.side,
            status=status,
            trade_date=snapshot.trade_date,
            requested_quantity=order.quantity,
            filled_quantity=filled_quantity,
            price=snapshot.price,
            gross_notional=gross_notional,
            fees=total_fees,
            stamp_tax=trade_cost.stamp_tax,
            net_cash_flow=net_cash_flow,
            message=message,
        )

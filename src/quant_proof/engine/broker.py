from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .account import Account
from .cost import CostModel
from .exchange_rules import ExchangeRules, MarketSnapshot
from .execution import ExecutionEngine, ExecutionReport
from .orders import Order, OrderSide, OrderType
from .risk import RiskLimits


@dataclass(frozen=True)
class Broker:
    exchange_rules: ExchangeRules = field(default_factory=ExchangeRules)
    cost_model: CostModel = field(default_factory=CostModel)
    risk_limits: RiskLimits = field(default_factory=RiskLimits)

    def submit_order(self, account: Account, order: Order, snapshot: MarketSnapshot) -> ExecutionReport:
        engine = ExecutionEngine(
            exchange_rules=self.exchange_rules,
            cost_model=self.cost_model,
            risk_limits=self.risk_limits,
        )
        return engine.execute(account=account, order=order, snapshot=snapshot)

    def buy(
        self,
        account: Account,
        symbol: str,
        quantity: int,
        snapshot: MarketSnapshot,
        submitted_at: date | None = None,
        limit_price: float | None = None,
    ) -> ExecutionReport:
        order_type = OrderType.LIMIT if limit_price is not None else OrderType.MARKET
        order = Order(
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            submitted_at=submitted_at or snapshot.trade_date,
            order_type=order_type,
            limit_price=limit_price,
        )
        return self.submit_order(account=account, order=order, snapshot=snapshot)

    def sell(
        self,
        account: Account,
        symbol: str,
        quantity: int,
        snapshot: MarketSnapshot,
        submitted_at: date | None = None,
        limit_price: float | None = None,
    ) -> ExecutionReport:
        order_type = OrderType.LIMIT if limit_price is not None else OrderType.MARKET
        order = Order(
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=quantity,
            submitted_at=submitted_at or snapshot.trade_date,
            order_type=order_type,
            limit_price=limit_price,
        )
        return self.submit_order(account=account, order=order, snapshot=snapshot)

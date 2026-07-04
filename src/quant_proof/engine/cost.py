from __future__ import annotations

from dataclasses import dataclass

from .orders import OrderSide


@dataclass(frozen=True)
class TradeCost:
    commission: float = 0.0
    transfer_fee: float = 0.0
    stamp_tax: float = 0.0
    slippage: float = 0.0

    @property
    def total(self) -> float:
        return self.commission + self.transfer_fee + self.stamp_tax + self.slippage


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 2.5
    min_commission: float = 5.0
    transfer_fee_bps: float = 0.1
    stamp_tax_sell_bps: float = 10.0
    slippage_bps: float = 0.0

    def calculate(self, side: OrderSide, gross_notional: float) -> TradeCost:
        if gross_notional <= 0.0:
            return TradeCost()

        commission = gross_notional * self.commission_bps / 10000.0
        if self.min_commission > 0.0:
            commission = max(commission, self.min_commission)

        transfer_fee = gross_notional * self.transfer_fee_bps / 10000.0
        stamp_tax = 0.0
        if side == OrderSide.SELL:
            stamp_tax = gross_notional * self.stamp_tax_sell_bps / 10000.0

        slippage = gross_notional * self.slippage_bps / 10000.0
        return TradeCost(
            commission=commission,
            transfer_fee=transfer_fee,
            stamp_tax=stamp_tax,
            slippage=slippage,
        )


def default_a_share_cost_model() -> CostModel:
    return CostModel()

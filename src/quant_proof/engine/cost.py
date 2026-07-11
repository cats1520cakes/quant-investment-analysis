from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .orders import OrderSide


# MOF/STA Announcement No. 39 of 2023 halves securities stamp tax from this date.
DEFAULT_A_SHARE_STAMP_TAX_HALVING_DATE = date(2023, 8, 28)
DEFAULT_A_SHARE_TRANSFER_FEE_HALVING_DATE = date(2022, 4, 29)
DEFAULT_A_SHARE_TRANSFER_FEE_UNIFICATION_DATE = date(2015, 8, 1)
DEFAULT_SSE_TRANSFER_FEE_FIRST_REDUCTION_DATE = date(2012, 6, 1)
DEFAULT_SSE_TRANSFER_FEE_SECOND_REDUCTION_DATE = date(2012, 9, 1)


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
    stamp_tax_halving_date: date | None = DEFAULT_A_SHARE_STAMP_TAX_HALVING_DATE
    historical_a_share_transfer_fees: bool = True
    a_share_par_value: float = 1.0

    @staticmethod
    def _exchange(symbol: str | None) -> str:
        value = "" if symbol is None else str(symbol).strip().lower()
        if value.startswith("sh.") or value.endswith(".sh"):
            return "sh"
        if value.startswith("sz.") or value.endswith(".sz"):
            return "sz"
        return ""

    def _transfer_fee(
        self,
        gross_notional: float,
        trade_date: date | None,
        symbol: str | None,
        quantity: int | None,
    ) -> float:
        current_fee = gross_notional * self.transfer_fee_bps / 10000.0
        if (
            not self.historical_a_share_transfer_fees
            or trade_date is None
            or trade_date >= DEFAULT_A_SHARE_TRANSFER_FEE_HALVING_DATE
            or self.transfer_fee_bps == 0.0
        ):
            return current_fee

        # Scale the official historical schedule from the configured current
        # 0.01 per mille baseline so zeroing or stressing the config still works.
        scale = self.transfer_fee_bps / 0.1
        if trade_date >= DEFAULT_A_SHARE_TRANSFER_FEE_UNIFICATION_DATE:
            return gross_notional * 0.00002 * scale

        exchange = self._exchange(symbol)
        if exchange == "sz":
            return gross_notional * 0.0000255 * scale
        if exchange == "sh" and quantity is not None:
            if trade_date < DEFAULT_SSE_TRANSFER_FEE_FIRST_REDUCTION_DATE:
                par_rate = 0.0005
            elif trade_date < DEFAULT_SSE_TRANSFER_FEE_SECOND_REDUCTION_DATE:
                par_rate = 0.000375
            else:
                par_rate = 0.0003
            # Before unification, the Shanghai investor-facing charge combined
            # equal CSDC and broker components and had a CNY 1 per-order floor.
            transfer_fee = (
                int(quantity)
                * self.a_share_par_value
                * par_rate
                * 2.0
                * scale
            )
            return max(transfer_fee, 1.0 * scale)

        # Actual stock execution always supplies symbol and quantity. Keep a
        # conservative, transaction-value fallback for generic callers.
        return gross_notional * 0.00002 * scale

    def calculate(
        self,
        side: OrderSide,
        gross_notional: float,
        trade_date: date | None = None,
        symbol: str | None = None,
        quantity: int | None = None,
    ) -> TradeCost:
        if gross_notional <= 0.0:
            return TradeCost()

        commission = gross_notional * self.commission_bps / 10000.0
        if self.min_commission > 0.0:
            commission = max(commission, self.min_commission)

        transfer_fee = self._transfer_fee(
            gross_notional,
            trade_date,
            symbol,
            quantity,
        )
        stamp_tax = 0.0
        if side == OrderSide.SELL:
            stamp_tax_bps = self.stamp_tax_sell_bps
            if (
                trade_date is not None
                and self.stamp_tax_halving_date is not None
                and trade_date >= self.stamp_tax_halving_date
            ):
                stamp_tax_bps *= 0.5
            stamp_tax = gross_notional * stamp_tax_bps / 10000.0

        slippage = gross_notional * self.slippage_bps / 10000.0
        return TradeCost(
            commission=commission,
            transfer_fee=transfer_fee,
            stamp_tax=stamp_tax,
            slippage=slippage,
        )


def default_a_share_cost_model() -> CostModel:
    return CostModel()

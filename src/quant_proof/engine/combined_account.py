from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from math import isfinite
from numbers import Integral, Real
from types import MappingProxyType
from typing import Dict, Mapping

from .account import Account
from .portfolio import Portfolio


_EPSILON = 1e-9


class MarginStatus(str, Enum):
    OK = "ok"
    MARGIN_CALL = "margin_call"
    DEFAULT = "default"


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class FuturesPosition:
    symbol: str
    contracts: int
    multiplier: float
    entry_price: float
    settlement_price: float
    initial_margin_rate: float
    maintenance_margin_rate: float
    locked_margin: float
    cumulative_settled_pnl: float = 0.0
    trade_date: date | None = None
    last_settlement_date: date | None = None
    last_margin_rate_date: date | None = None

    @property
    def notional(self) -> float:
        return abs(self.contracts) * self.multiplier * self.settlement_price

    @property
    def initial_margin_required(self) -> float:
        return self.notional * self.initial_margin_rate

    @property
    def maintenance_margin_required(self) -> float:
        return self.notional * self.maintenance_margin_rate


@dataclass(frozen=True)
class LongOptionPosition:
    symbol: str
    option_type: OptionType
    contracts: int
    multiplier: float
    strike: float
    expiry: date
    premium: float
    premium_paid: float
    mark_price: float
    fees_paid: float = 0.0
    trade_date: date | None = None
    last_mark_date: date | None = None
    settled: bool = False
    settlement_date: date | None = None
    settlement_underlying_price: float | None = None
    settlement_option_price: float | None = None
    settlement_intrinsic_value: float | None = None
    settlement_cash_flow: float = 0.0
    settlement_fees: float = 0.0

    @property
    def market_value(self) -> float:
        if self.settled:
            return 0.0
        return self.contracts * self.multiplier * self.mark_price


@dataclass(frozen=True)
class MarginCheck:
    status: MarginStatus
    free_cash: float
    locked_margin: float
    collateral_equity: float
    initial_margin_required: float
    maintenance_margin_required: float
    nav: float
    nav_is_complete: bool
    shortfall: float

    @property
    def margin_call(self) -> bool:
        return self.status == MarginStatus.MARGIN_CALL

    @property
    def default(self) -> bool:
        return self.status == MarginStatus.DEFAULT


@dataclass(frozen=True)
class FuturesSettlement:
    symbol: str
    previous_settlement_price: float
    settlement_price: float
    variation_margin: float
    margin_before: float
    margin_required: float
    margin_after: float
    margin_transfer: float
    free_cash_after: float
    status: MarginStatus
    settlement_date: date | None = None
    already_settled: bool = False


@dataclass(frozen=True)
class FuturesMarginRateUpdate:
    symbol: str
    effective_date: date
    initial_margin_rate_before: float
    maintenance_margin_rate_before: float
    initial_margin_rate_after: float
    maintenance_margin_rate_after: float
    margin_before: float
    margin_required: float
    margin_after: float
    margin_transfer: float
    free_cash_after: float
    status: MarginStatus
    already_updated: bool = False

    @property
    def previous_initial_margin_rate(self) -> float:
        return self.initial_margin_rate_before

    @property
    def previous_maintenance_margin_rate(self) -> float:
        return self.maintenance_margin_rate_before

    @property
    def initial_margin_rate(self) -> float:
        return self.initial_margin_rate_after

    @property
    def maintenance_margin_rate(self) -> float:
        return self.maintenance_margin_rate_after


@dataclass(frozen=True)
class FuturesClose:
    symbol: str
    closed_contracts: int
    remaining_contracts: int
    settlement_price: float
    variation_margin: float
    fees: float
    margin_before: float
    margin_after: float
    margin_transfer: float
    free_cash_after: float
    status: MarginStatus


@dataclass(frozen=True)
class FuturesAdjustment:
    symbol: str
    action: str
    previous_contracts: int
    target_contracts: int
    traded_contracts: int
    price: float
    variation_margin: float
    fees: float
    margin_before: float
    margin_after: float
    margin_transfer: float
    free_cash_after: float
    status: MarginStatus
    trade_date: date | None = None


@dataclass(frozen=True)
class OptionClose:
    symbol: str
    closed_contracts: int
    remaining_contracts: int
    liquidation_price: float
    gross_proceeds: float
    fees: float
    realized_pnl: float
    free_cash_after: float
    trade_date: date


@dataclass(frozen=True)
class OptionSettlement:
    symbol: str
    settlement_date: date
    underlying_price: float | None
    settlement_option_price: float
    intrinsic_value: float
    cash_flow: float
    original_cash_flow: float
    already_settled: bool
    free_cash_after: float
    fees: float = 0.0
    original_fees: float = 0.0


def _symbol(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("symbol must be a non-empty string")
    return value


def _nonnegative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite non-negative number")
    converted = float(value)
    if not isfinite(converted) or converted < 0.0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return converted


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be finite and positive")
    converted = float(value)
    if not isfinite(converted) or converted <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return converted


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _nonzero_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value == 0:
        raise ValueError(f"{name} must be a non-zero integer")
    return int(value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _date(value: object, name: str) -> date:
    if not isinstance(value, date):
        raise ValueError(f"{name} must be a date")
    return value


def _optional_date(value: object, name: str) -> date | None:
    if value is None:
        return None
    return _date(value, name)


def _finite_result(value: float, name: str) -> float:
    if not isfinite(value):
        raise ValueError(f"{name} must remain finite")
    return value


def _product(name: str, *values: float) -> float:
    result = 1.0
    for value in values:
        result *= value
    return _finite_result(result, name)


def _sum(name: str, *values: float) -> float:
    return _finite_result(sum(values), name)


def _same_number(left: float, right: float) -> bool:
    scale = max(1.0, abs(left), abs(right))
    return abs(left - right) <= _EPSILON * scale


def _option_type(value: OptionType | str) -> OptionType:
    if isinstance(value, OptionType):
        return value
    if isinstance(value, str):
        try:
            return OptionType(value.lower())
        except ValueError:
            pass
    raise ValueError("option_type must be 'call' or 'put'")


class CombinedAccount(Account):
    """A stock-compatible account with one cash pool for all instruments."""

    def __init__(
        self,
        cash: float | None = None,
        portfolio: Portfolio | None = None,
        *,
        free_cash: float | None = None,
    ) -> None:
        if (cash is None) == (free_cash is None):
            raise ValueError("provide exactly one of cash or free_cash")
        initial_cash = _nonnegative(cash if cash is not None else free_cash, "cash")
        if portfolio is not None and not isinstance(portfolio, Portfolio):
            raise ValueError("portfolio must be a Portfolio")
        super().__init__(cash=initial_cash, portfolio=portfolio or Portfolio())
        self._futures: Dict[str, FuturesPosition] = {}
        self._options: Dict[str, LongOptionPosition] = {}

    @property
    def free_cash(self) -> float:
        return self.cash

    @property
    def futures_positions(self) -> Mapping[str, FuturesPosition]:
        return MappingProxyType(self._futures)

    @property
    def option_positions(self) -> Mapping[str, LongOptionPosition]:
        return MappingProxyType(self._options)

    @property
    def locked_margin(self) -> float:
        return _finite_result(
            sum(position.locked_margin for position in self._futures.values()),
            "locked_margin",
        )

    @property
    def initial_margin_required(self) -> float:
        return _finite_result(
            sum(position.initial_margin_required for position in self._futures.values()),
            "initial_margin_required",
        )

    @property
    def maintenance_margin_required(self) -> float:
        return _finite_result(
            sum(position.maintenance_margin_required for position in self._futures.values()),
            "maintenance_margin_required",
        )

    def _current_cash(self) -> float:
        if isinstance(self.cash, bool) or not isinstance(self.cash, Real):
            raise ValueError("cash must remain finite")
        return _finite_result(float(self.cash), "cash")

    def apply_buy(
        self,
        symbol: str,
        quantity: int,
        price: float,
        fees: float,
        trade_date: date,
        available_from: date,
    ) -> None:
        checked_symbol = _symbol(symbol)
        checked_quantity = _positive_integer(quantity, "quantity")
        checked_price = _nonnegative(price, "price")
        checked_fees = _nonnegative(fees, "fees")
        checked_trade_date = _date(trade_date, "trade_date")
        checked_available_from = _date(available_from, "available_from")
        if checked_available_from < checked_trade_date:
            raise ValueError("available_from cannot precede trade_date")

        gross_notional = _product("gross_notional", checked_quantity, checked_price)
        required_cash = _sum("required_cash", gross_notional, checked_fees)
        current_cash = self._current_cash()
        if required_cash > current_cash:
            raise ValueError("insufficient cash")
        cash_after = _sum("cash", current_cash, -required_cash)

        self.portfolio.add_buy(
            symbol=checked_symbol,
            quantity=checked_quantity,
            price=checked_price,
            fees=checked_fees,
            trade_date=checked_trade_date,
            available_from=checked_available_from,
        )
        self.cash = cash_after

    def apply_sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        fees: float,
        trade_date: date,
    ) -> None:
        checked_symbol = _symbol(symbol)
        checked_quantity = _positive_integer(quantity, "quantity")
        checked_price = _nonnegative(price, "price")
        checked_fees = _nonnegative(fees, "fees")
        checked_trade_date = _date(trade_date, "trade_date")
        gross_notional = _product("gross_notional", checked_quantity, checked_price)
        cash_after = _sum("cash", self._current_cash(), gross_notional, -checked_fees)

        self.portfolio.remove_sell(
            symbol=checked_symbol,
            quantity=checked_quantity,
            trade_date=checked_trade_date,
        )
        self.cash = cash_after

    def apply_terminal_value(self, symbol: str, price: float) -> tuple[int, float]:
        checked_symbol = _symbol(symbol)
        checked_price = _nonnegative(price, "price")
        quantity = self.portfolio.quantity(checked_symbol)
        if quantity <= 0:
            return 0, 0.0
        recovery = _product("terminal recovery", quantity, checked_price)
        cash_after = _sum("cash", self._current_cash(), recovery)
        self.portfolio.lots.pop(checked_symbol, None)
        self.cash = cash_after
        return quantity, recovery

    def open_futures(
        self,
        symbol: str,
        contracts: int,
        price: float,
        multiplier: float,
        initial_margin_rate: float,
        maintenance_margin_rate: float,
        fees: float = 0.0,
        trade_date: date | None = None,
    ) -> FuturesPosition:
        checked_symbol = _symbol(symbol)
        if checked_symbol in self._futures:
            raise ValueError(f"futures position already exists for {checked_symbol}")
        checked_contracts = _nonzero_integer(contracts, "contracts")
        checked_price = _positive(price, "price")
        checked_multiplier = _positive(multiplier, "multiplier")
        checked_initial_rate = _positive(initial_margin_rate, "initial_margin_rate")
        checked_maintenance_rate = _positive(
            maintenance_margin_rate,
            "maintenance_margin_rate",
        )
        if checked_maintenance_rate > checked_initial_rate:
            raise ValueError(
                "maintenance_margin_rate cannot exceed initial_margin_rate"
            )
        checked_fees = _nonnegative(fees, "fees")
        checked_trade_date = _optional_date(trade_date, "trade_date")

        notional = _product(
            "futures notional",
            abs(checked_contracts),
            checked_multiplier,
            checked_price,
        )
        margin = _product("initial margin", notional, checked_initial_rate)
        required_cash = _sum("required_cash", margin, checked_fees)
        current_cash = self._current_cash()
        if required_cash > current_cash:
            raise ValueError("insufficient free cash for futures margin and fees")

        position = FuturesPosition(
            symbol=checked_symbol,
            contracts=checked_contracts,
            multiplier=checked_multiplier,
            entry_price=checked_price,
            settlement_price=checked_price,
            initial_margin_rate=checked_initial_rate,
            maintenance_margin_rate=checked_maintenance_rate,
            locked_margin=margin,
            trade_date=checked_trade_date,
            last_margin_rate_date=checked_trade_date,
        )
        self.cash = _sum("cash", current_cash, -required_cash)
        self._futures[checked_symbol] = position
        return position

    def _rebalance_margin(
        self,
        cash: float,
        locked_margin: float,
        required_margin: float,
    ) -> tuple[float, float, float]:
        if required_margin < locked_margin:
            release = locked_margin - required_margin
            return (
                _sum("cash", cash, release),
                required_margin,
                -release,
            )

        needed = required_margin - locked_margin
        transfer = min(needed, cash) if cash > 0.0 else 0.0
        return (
            _sum("cash", cash, -transfer),
            _sum("locked_margin", locked_margin, transfer),
            transfer,
        )

    def update_futures_margin_rates(
        self,
        symbol: str,
        initial_margin_rate: float,
        maintenance_margin_rate: float,
        effective_date: date,
    ) -> FuturesMarginRateUpdate:
        checked_symbol = _symbol(symbol)
        checked_initial_rate = _positive(
            initial_margin_rate,
            "initial_margin_rate",
        )
        checked_maintenance_rate = _positive(
            maintenance_margin_rate,
            "maintenance_margin_rate",
        )
        if checked_maintenance_rate > checked_initial_rate:
            raise ValueError(
                "maintenance_margin_rate cannot exceed initial_margin_rate"
            )
        checked_date = _date(effective_date, "effective_date")
        try:
            position = self._futures[checked_symbol]
        except KeyError:
            raise ValueError(f"no futures position for {checked_symbol}") from None

        if position.trade_date is not None and checked_date < position.trade_date:
            raise ValueError("effective_date cannot precede the latest trade_date")
        if (
            position.last_settlement_date is not None
            and checked_date < position.last_settlement_date
        ):
            raise ValueError(
                "effective_date cannot precede the latest futures settlement"
            )
        if position.last_margin_rate_date is not None:
            if checked_date < position.last_margin_rate_date:
                raise ValueError("futures margin-rate updates must be chronological")
            if checked_date == position.last_margin_rate_date:
                if not (
                    _same_number(
                        checked_initial_rate,
                        position.initial_margin_rate,
                    )
                    and _same_number(
                        checked_maintenance_rate,
                        position.maintenance_margin_rate,
                    )
                ):
                    raise ValueError(
                        "conflicting futures margin-rate update for the same "
                        "effective_date"
                    )
                current_cash = self._current_cash()
                return FuturesMarginRateUpdate(
                    symbol=checked_symbol,
                    effective_date=checked_date,
                    initial_margin_rate_before=position.initial_margin_rate,
                    maintenance_margin_rate_before=position.maintenance_margin_rate,
                    initial_margin_rate_after=position.initial_margin_rate,
                    maintenance_margin_rate_after=position.maintenance_margin_rate,
                    margin_before=position.locked_margin,
                    margin_required=position.initial_margin_required,
                    margin_after=position.locked_margin,
                    margin_transfer=0.0,
                    free_cash_after=current_cash,
                    status=self.check_margin().status,
                    already_updated=True,
                )

        required_margin = _product(
            "initial margin",
            abs(position.contracts),
            position.multiplier,
            position.settlement_price,
            checked_initial_rate,
        )
        current_cash = self._current_cash()
        cash_after, margin_after, margin_transfer = self._rebalance_margin(
            current_cash,
            position.locked_margin,
            required_margin,
        )
        self.cash = cash_after
        self._futures[checked_symbol] = replace(
            position,
            initial_margin_rate=checked_initial_rate,
            maintenance_margin_rate=checked_maintenance_rate,
            locked_margin=margin_after,
            last_margin_rate_date=checked_date,
        )
        status = self.check_margin().status
        return FuturesMarginRateUpdate(
            symbol=checked_symbol,
            effective_date=checked_date,
            initial_margin_rate_before=position.initial_margin_rate,
            maintenance_margin_rate_before=position.maintenance_margin_rate,
            initial_margin_rate_after=checked_initial_rate,
            maintenance_margin_rate_after=checked_maintenance_rate,
            margin_before=position.locked_margin,
            margin_required=required_margin,
            margin_after=margin_after,
            margin_transfer=margin_transfer,
            free_cash_after=cash_after,
            status=status,
            already_updated=False,
        )

    def settle_futures(
        self,
        symbol: str,
        settlement_price: float,
        settlement_date: date | None = None,
    ) -> FuturesSettlement:
        checked_symbol = _symbol(symbol)
        checked_price = _positive(settlement_price, "settlement_price")
        checked_date = _optional_date(settlement_date, "settlement_date")
        try:
            position = self._futures[checked_symbol]
        except KeyError:
            raise ValueError(f"no futures position for {checked_symbol}") from None

        if checked_date is None:
            raise ValueError("settlement_date is required for futures settlement")
        if (
            checked_date is not None
            and position.trade_date is not None
            and checked_date < position.trade_date
        ):
            raise ValueError("settlement_date cannot precede the latest trade_date")
        if (
            position.last_margin_rate_date is not None
            and checked_date < position.last_margin_rate_date
        ):
            raise ValueError(
                "settlement_date cannot precede the latest futures margin-rate update"
            )
        if position.last_settlement_date is not None and checked_date is not None:
            if checked_date < position.last_settlement_date:
                raise ValueError("futures settlements must be strictly chronological")
            if checked_date == position.last_settlement_date:
                if not _same_number(checked_price, position.settlement_price):
                    raise ValueError(
                        "conflicting futures settlement for the same settlement_date"
                    )
                current_cash = self._current_cash()
                return FuturesSettlement(
                    symbol=checked_symbol,
                    previous_settlement_price=position.settlement_price,
                    settlement_price=checked_price,
                    variation_margin=0.0,
                    margin_before=position.locked_margin,
                    margin_required=position.initial_margin_required,
                    margin_after=position.locked_margin,
                    margin_transfer=0.0,
                    free_cash_after=current_cash,
                    status=self.check_margin().status,
                    settlement_date=checked_date,
                    already_settled=True,
                )

        price_change = checked_price - position.settlement_price
        variation_margin = _product(
            "variation_margin",
            position.contracts,
            position.multiplier,
            price_change,
        )
        cash_after_pnl = _sum("cash", self._current_cash(), variation_margin)
        required_margin = _product(
            "initial margin",
            abs(position.contracts),
            position.multiplier,
            checked_price,
            position.initial_margin_rate,
        )
        cash_after, margin_after, margin_transfer = self._rebalance_margin(
            cash_after_pnl,
            position.locked_margin,
            required_margin,
        )
        updated_position = replace(
            position,
            settlement_price=checked_price,
            locked_margin=margin_after,
            cumulative_settled_pnl=_sum(
                "cumulative_settled_pnl",
                position.cumulative_settled_pnl,
                variation_margin,
            ),
            last_settlement_date=checked_date,
        )
        self.cash = cash_after
        self._futures[checked_symbol] = updated_position
        status = self.check_margin().status
        return FuturesSettlement(
            symbol=checked_symbol,
            previous_settlement_price=position.settlement_price,
            settlement_price=checked_price,
            variation_margin=variation_margin,
            margin_before=position.locked_margin,
            margin_required=required_margin,
            margin_after=margin_after,
            margin_transfer=margin_transfer,
            free_cash_after=cash_after,
            status=status,
            settlement_date=checked_date,
            already_settled=False,
        )

    def adjust_futures_position(
        self,
        symbol: str,
        target_contracts: int,
        price: float,
        multiplier: float | None = None,
        initial_margin_rate: float | None = None,
        maintenance_margin_rate: float | None = None,
        fees: float = 0.0,
        trade_date: date | None = None,
    ) -> FuturesAdjustment:
        """Trade a futures symbol to an exact signed target quantity."""

        checked_symbol = _symbol(symbol)
        checked_target = _integer(target_contracts, "target_contracts")
        checked_price = _positive(price, "price")
        checked_fees = _nonnegative(fees, "fees")
        checked_date = _optional_date(trade_date, "trade_date")
        position = self._futures.get(checked_symbol)

        if position is None:
            if checked_target == 0:
                if checked_fees > _EPSILON:
                    raise ValueError("fees must be zero when the target is already flat")
                return FuturesAdjustment(
                    symbol=checked_symbol,
                    action="unchanged",
                    previous_contracts=0,
                    target_contracts=0,
                    traded_contracts=0,
                    price=checked_price,
                    variation_margin=0.0,
                    fees=0.0,
                    margin_before=0.0,
                    margin_after=0.0,
                    margin_transfer=0.0,
                    free_cash_after=self._current_cash(),
                    status=self.check_margin().status,
                    trade_date=checked_date,
                )
            if multiplier is None:
                raise ValueError("multiplier is required to open a futures position")
            if initial_margin_rate is None:
                raise ValueError(
                    "initial_margin_rate is required to open a futures position"
                )
            if maintenance_margin_rate is None:
                raise ValueError(
                    "maintenance_margin_rate is required to open a futures position"
                )
            opened = self.open_futures(
                symbol=checked_symbol,
                contracts=checked_target,
                price=checked_price,
                multiplier=multiplier,
                initial_margin_rate=initial_margin_rate,
                maintenance_margin_rate=maintenance_margin_rate,
                fees=checked_fees,
                trade_date=checked_date,
            )
            return FuturesAdjustment(
                symbol=checked_symbol,
                action="open",
                previous_contracts=0,
                target_contracts=checked_target,
                traded_contracts=abs(checked_target),
                price=checked_price,
                variation_margin=0.0,
                fees=checked_fees,
                margin_before=0.0,
                margin_after=opened.locked_margin,
                margin_transfer=opened.locked_margin,
                free_cash_after=self._current_cash(),
                status=self.check_margin().status,
                trade_date=checked_date,
            )

        checked_multiplier = (
            position.multiplier
            if multiplier is None
            else _positive(multiplier, "multiplier")
        )
        checked_initial_rate = (
            position.initial_margin_rate
            if initial_margin_rate is None
            else _positive(initial_margin_rate, "initial_margin_rate")
        )
        checked_maintenance_rate = (
            position.maintenance_margin_rate
            if maintenance_margin_rate is None
            else _positive(maintenance_margin_rate, "maintenance_margin_rate")
        )
        if checked_maintenance_rate > checked_initial_rate:
            raise ValueError("maintenance_margin_rate cannot exceed initial_margin_rate")
        if not _same_number(checked_multiplier, position.multiplier):
            raise ValueError("cannot change multiplier for an open futures position")
        if not _same_number(checked_initial_rate, position.initial_margin_rate):
            raise ValueError(
                "cannot change initial_margin_rate for an open futures position"
            )
        if not _same_number(
            checked_maintenance_rate,
            position.maintenance_margin_rate,
        ):
            raise ValueError(
                "cannot change maintenance_margin_rate for an open futures position"
            )
        if checked_date is None and (
            position.trade_date is not None
            or position.last_settlement_date is not None
            or position.last_margin_rate_date is not None
        ):
            raise ValueError("trade_date is required for a dated futures position")
        if (
            checked_date is not None
            and position.trade_date is not None
            and checked_date < position.trade_date
        ):
            raise ValueError("trade_date cannot move backwards")
        if (
            checked_date is not None
            and position.last_settlement_date is not None
            and checked_date < position.last_settlement_date
        ):
            raise ValueError("trade_date cannot precede the latest futures settlement")
        if (
            checked_date is not None
            and position.last_margin_rate_date is not None
            and checked_date < position.last_margin_rate_date
        ):
            raise ValueError(
                "trade_date cannot precede the latest futures margin-rate update"
            )
        if checked_target == position.contracts and checked_fees > _EPSILON:
            raise ValueError("fees must be zero when target_contracts is unchanged")

        variation_margin = _product(
            "variation_margin",
            position.contracts,
            position.multiplier,
            checked_price - position.settlement_price,
        )
        cash_after_pnl_and_fees = _sum(
            "cash",
            self._current_cash(),
            variation_margin,
            -checked_fees,
        )
        required_margin = _product(
            "initial margin",
            abs(checked_target),
            position.multiplier,
            checked_price,
            position.initial_margin_rate,
        )
        opens_exposure = checked_target != 0 and (
            checked_target * position.contracts < 0
            or abs(checked_target) > abs(position.contracts)
        )
        available_for_margin = _sum(
            "available collateral",
            cash_after_pnl_and_fees,
            position.locked_margin,
        )
        if opens_exposure and required_margin > available_for_margin + _EPSILON:
            raise ValueError("insufficient free cash for futures margin and fees")

        cash_after, margin_after, margin_transfer = self._rebalance_margin(
            cash_after_pnl_and_fees,
            position.locked_margin,
            required_margin,
        )
        if checked_target == 0:
            action = "close"
        elif checked_target * position.contracts < 0:
            action = "reverse"
        elif abs(checked_target) > abs(position.contracts):
            action = "add"
        elif abs(checked_target) < abs(position.contracts):
            action = "reduce"
        else:
            action = "unchanged"

        if action in {"open", "reverse"}:
            entry_price = checked_price
        elif action == "add":
            added = abs(checked_target) - abs(position.contracts)
            entry_price = _sum(
                "weighted futures entry notional",
                abs(position.contracts) * position.entry_price,
                added * checked_price,
            ) / abs(checked_target)
        else:
            entry_price = position.entry_price

        self.cash = cash_after
        if checked_target == 0:
            self._futures.pop(checked_symbol)
        else:
            self._futures[checked_symbol] = replace(
                position,
                contracts=checked_target,
                entry_price=entry_price,
                settlement_price=checked_price,
                locked_margin=margin_after,
                cumulative_settled_pnl=_sum(
                    "cumulative_settled_pnl",
                    position.cumulative_settled_pnl,
                    variation_margin,
                ),
                trade_date=checked_date or position.trade_date,
            )
        status = self.check_margin().status
        return FuturesAdjustment(
            symbol=checked_symbol,
            action=action,
            previous_contracts=position.contracts,
            target_contracts=checked_target,
            traded_contracts=abs(checked_target - position.contracts),
            price=checked_price,
            variation_margin=variation_margin,
            fees=checked_fees,
            margin_before=position.locked_margin,
            margin_after=margin_after,
            margin_transfer=margin_transfer,
            free_cash_after=cash_after,
            status=status,
            trade_date=checked_date,
        )

    def close_futures(
        self,
        symbol: str,
        settlement_price: float,
        contracts: int | None = None,
        fees: float = 0.0,
        trade_date: date | None = None,
    ) -> FuturesClose:
        checked_symbol = _symbol(symbol)
        try:
            position = self._futures[checked_symbol]
        except KeyError:
            raise ValueError(f"no futures position for {checked_symbol}") from None
        absolute_position = abs(position.contracts)
        closed_contracts = (
            absolute_position
            if contracts is None
            else _positive_integer(contracts, "contracts")
        )
        if closed_contracts > absolute_position:
            raise ValueError("cannot close more contracts than are open")
        remaining_absolute = absolute_position - closed_contracts
        target_contracts = (
            remaining_absolute if position.contracts > 0 else -remaining_absolute
        )
        adjustment = self.adjust_futures_position(
            symbol=checked_symbol,
            target_contracts=target_contracts,
            price=settlement_price,
            fees=fees,
            trade_date=trade_date,
        )
        return FuturesClose(
            symbol=checked_symbol,
            closed_contracts=closed_contracts,
            remaining_contracts=target_contracts,
            settlement_price=adjustment.price,
            variation_margin=adjustment.variation_margin,
            fees=adjustment.fees,
            margin_before=adjustment.margin_before,
            margin_after=adjustment.margin_after,
            margin_transfer=adjustment.margin_transfer,
            free_cash_after=adjustment.free_cash_after,
            status=adjustment.status,
        )

    def buy_option(
        self,
        symbol: str,
        option_type: OptionType | str,
        contracts: int,
        premium: float,
        strike: float,
        expiry: date,
        multiplier: float = 1.0,
        fees: float = 0.0,
        trade_date: date | None = None,
    ) -> LongOptionPosition:
        checked_symbol = _symbol(symbol)
        checked_type = _option_type(option_type)
        checked_contracts = _positive_integer(contracts, "contracts")
        checked_premium = _nonnegative(premium, "premium")
        checked_strike = _nonnegative(strike, "strike")
        checked_expiry = _date(expiry, "expiry")
        checked_multiplier = _positive(multiplier, "multiplier")
        checked_fees = _nonnegative(fees, "fees")
        checked_trade_date = _optional_date(trade_date, "trade_date")
        if checked_trade_date is not None and checked_trade_date > checked_expiry:
            raise ValueError("trade_date cannot follow expiry")

        existing = self._options.get(checked_symbol)
        if existing is not None:
            if existing.settled:
                raise ValueError(f"option position is already settled for {checked_symbol}")
            if existing.option_type != checked_type:
                raise ValueError("cannot change option_type for an open option position")
            if existing.expiry != checked_expiry:
                raise ValueError("cannot change expiry for an open option position")
            if not _same_number(existing.strike, checked_strike):
                raise ValueError("cannot change strike for an open option position")
            if not _same_number(existing.multiplier, checked_multiplier):
                raise ValueError("cannot change multiplier for an open option position")
            if checked_trade_date is None and existing.last_mark_date is not None:
                raise ValueError("trade_date is required when adding to a dated option")
            if (
                checked_trade_date is not None
                and existing.last_mark_date is not None
                and checked_trade_date < existing.last_mark_date
            ):
                raise ValueError("option trade_date cannot move backwards")

        premium_paid = _product(
            "premium_paid",
            checked_contracts,
            checked_multiplier,
            checked_premium,
        )
        required_cash = _sum("required_cash", premium_paid, checked_fees)
        current_cash = self._current_cash()
        if required_cash > current_cash:
            raise ValueError("insufficient free cash for option premium and fees")

        if existing is None:
            position = LongOptionPosition(
                symbol=checked_symbol,
                option_type=checked_type,
                contracts=checked_contracts,
                multiplier=checked_multiplier,
                strike=checked_strike,
                expiry=checked_expiry,
                premium=checked_premium,
                premium_paid=premium_paid,
                mark_price=checked_premium,
                fees_paid=checked_fees,
                trade_date=checked_trade_date,
                last_mark_date=checked_trade_date,
            )
        else:
            total_contracts = existing.contracts + checked_contracts
            total_premium_paid = _sum(
                "premium_paid",
                existing.premium_paid,
                premium_paid,
            )
            weighted_premium = total_premium_paid / _product(
                "option contract multiplier",
                total_contracts,
                existing.multiplier,
            )
            position = replace(
                existing,
                contracts=total_contracts,
                premium=weighted_premium,
                premium_paid=total_premium_paid,
                mark_price=checked_premium,
                fees_paid=_sum("fees_paid", existing.fees_paid, checked_fees),
                trade_date=existing.trade_date or checked_trade_date,
                last_mark_date=checked_trade_date,
            )
        self.cash = _sum("cash", current_cash, -required_cash)
        self._options[checked_symbol] = position
        return position

    def mark_option(
        self,
        symbol: str,
        liquidation_price: float,
        as_of: date | None = None,
    ) -> LongOptionPosition:
        checked_symbol = _symbol(symbol)
        checked_price = _nonnegative(liquidation_price, "liquidation_price")
        checked_as_of = _optional_date(as_of, "as_of")
        try:
            position = self._options[checked_symbol]
        except KeyError:
            raise ValueError(f"no option position for {checked_symbol}") from None
        if position.settled:
            raise ValueError(f"option position is already settled for {checked_symbol}")
        if checked_as_of is None:
            raise ValueError("as_of is required for an option mark")
        if checked_as_of > position.expiry:
            raise ValueError("cannot mark an option after expiry")
        if position.trade_date is not None and checked_as_of < position.trade_date:
            raise ValueError("option mark date cannot precede trade_date")
        if (
            position.last_mark_date is not None
            and checked_as_of < position.last_mark_date
        ):
            raise ValueError("option marks cannot move backwards")

        updated = replace(
            position,
            mark_price=checked_price,
            last_mark_date=checked_as_of,
        )
        self._options[checked_symbol] = updated
        return updated

    def close_option(
        self,
        symbol: str,
        liquidation_price: float,
        contracts: int | None = None,
        fees: float = 0.0,
        trade_date: date | None = None,
    ) -> OptionClose:
        checked_symbol = _symbol(symbol)
        checked_price = _nonnegative(liquidation_price, "liquidation_price")
        checked_fees = _nonnegative(fees, "fees")
        checked_date = _optional_date(trade_date, "trade_date")
        if checked_date is None:
            raise ValueError("trade_date is required to close an option")
        try:
            position = self._options[checked_symbol]
        except KeyError:
            raise ValueError(f"no option position for {checked_symbol}") from None
        if position.settled:
            raise ValueError(f"option position is already settled for {checked_symbol}")
        if checked_date >= position.expiry:
            raise ValueError("long options must be closed before expiry")
        if position.trade_date is not None and checked_date < position.trade_date:
            raise ValueError("option close trade_date cannot precede trade_date")
        if (
            position.last_mark_date is not None
            and checked_date < position.last_mark_date
        ):
            raise ValueError("option close trade_date cannot move backwards")

        closed_contracts = (
            position.contracts
            if contracts is None
            else _positive_integer(contracts, "contracts")
        )
        if closed_contracts > position.contracts:
            raise ValueError("cannot close more option contracts than are open")
        gross_proceeds = _product(
            "option close proceeds",
            closed_contracts,
            position.multiplier,
            checked_price,
        )
        allocated_premium = _product(
            "allocated option premium",
            position.premium_paid,
            closed_contracts / position.contracts,
        )
        realized_pnl = _sum(
            "option realized pnl",
            gross_proceeds,
            -allocated_premium,
            -checked_fees,
        )
        cash_after = _sum(
            "cash",
            self._current_cash(),
            gross_proceeds,
            -checked_fees,
        )
        remaining_contracts = position.contracts - closed_contracts
        self.cash = cash_after
        if remaining_contracts == 0:
            self._options.pop(checked_symbol)
        else:
            self._options[checked_symbol] = replace(
                position,
                contracts=remaining_contracts,
                premium_paid=_sum(
                    "premium_paid",
                    position.premium_paid,
                    -allocated_premium,
                ),
                mark_price=checked_price,
                fees_paid=_sum("fees_paid", position.fees_paid, checked_fees),
                last_mark_date=checked_date,
            )
        return OptionClose(
            symbol=checked_symbol,
            closed_contracts=closed_contracts,
            remaining_contracts=remaining_contracts,
            liquidation_price=checked_price,
            gross_proceeds=gross_proceeds,
            fees=checked_fees,
            realized_pnl=realized_pnl,
            free_cash_after=cash_after,
            trade_date=checked_date,
        )

    def settle_option_expiry(
        self,
        symbol: str,
        underlying_price: float | None = None,
        settlement_date: date | None = None,
        *,
        settlement_option_price: float | None = None,
        option_settlement_price: float | None = None,
        fees: float = 0.0,
    ) -> OptionSettlement:
        checked_symbol = _symbol(symbol)
        checked_date = _date(settlement_date, "settlement_date")
        checked_fees = _nonnegative(fees, "fees")
        try:
            position = self._options[checked_symbol]
        except KeyError:
            raise ValueError(f"no option position for {checked_symbol}") from None
        if checked_date < position.expiry:
            raise ValueError("option cannot settle before expiry")

        if position.settled:
            if (
                position.settlement_date is None
                or position.settlement_option_price is None
                or position.settlement_intrinsic_value is None
            ):
                raise RuntimeError("settled option is missing original settlement facts")
            if not _same_number(checked_fees, position.settlement_fees):
                raise ValueError(
                    "conflicting option settlement fees for an idempotent expiry"
                )
            return OptionSettlement(
                symbol=checked_symbol,
                settlement_date=position.settlement_date,
                underlying_price=position.settlement_underlying_price,
                settlement_option_price=position.settlement_option_price,
                intrinsic_value=position.settlement_intrinsic_value,
                cash_flow=0.0,
                original_cash_flow=position.settlement_cash_flow,
                already_settled=True,
                free_cash_after=self._current_cash(),
                fees=0.0,
                original_fees=position.settlement_fees,
            )

        explicit_prices = [
            value
            for value in (settlement_option_price, option_settlement_price)
            if value is not None
        ]
        if len(explicit_prices) > 1:
            raise ValueError("provide only one settlement option price")
        if underlying_price is not None and explicit_prices:
            raise ValueError(
                "provide either underlying_price or settlement_option_price, not both"
            )
        if underlying_price is None and not explicit_prices:
            raise ValueError(
                "provide underlying_price or an explicit settlement_option_price"
            )

        checked_underlying: float | None
        if explicit_prices:
            checked_underlying = None
            intrinsic_value = _nonnegative(
                explicit_prices[0],
                "settlement_option_price",
            )
        else:
            checked_underlying = _nonnegative(underlying_price, "underlying_price")
            if position.option_type == OptionType.CALL:
                intrinsic_value = max(checked_underlying - position.strike, 0.0)
            else:
                intrinsic_value = max(position.strike - checked_underlying, 0.0)

        gross_cash_flow = _product(
            "option settlement cash flow",
            position.contracts,
            position.multiplier,
            intrinsic_value,
        )
        cash_flow = _sum(
            "net option settlement cash flow",
            gross_cash_flow,
            -checked_fees,
        )
        cash_after = _sum("cash", self._current_cash(), cash_flow)
        self.cash = cash_after
        self._options[checked_symbol] = replace(
            position,
            mark_price=0.0,
            settled=True,
            settlement_date=checked_date,
            settlement_underlying_price=checked_underlying,
            settlement_option_price=intrinsic_value,
            settlement_intrinsic_value=intrinsic_value,
            settlement_cash_flow=cash_flow,
            settlement_fees=checked_fees,
            fees_paid=_sum("fees_paid", position.fees_paid, checked_fees),
            last_mark_date=checked_date,
        )
        return OptionSettlement(
            symbol=checked_symbol,
            settlement_date=checked_date,
            underlying_price=checked_underlying,
            settlement_option_price=intrinsic_value,
            intrinsic_value=intrinsic_value,
            cash_flow=cash_flow,
            original_cash_flow=cash_flow,
            already_settled=False,
            free_cash_after=cash_after,
            fees=checked_fees,
            original_fees=checked_fees,
        )

    def _validated_prices(
        self,
        prices: Mapping[str, float] | None,
        name: str,
        *,
        strictly_positive: bool = False,
    ) -> Dict[str, float]:
        if prices is None:
            return {}
        if not isinstance(prices, Mapping):
            raise ValueError(f"{name} must be a mapping")
        validator = _positive if strictly_positive else _nonnegative
        return {
            _symbol(symbol): validator(price, f"{name}[{symbol!r}]")
            for symbol, price in prices.items()
        }

    def _stock_market_value(
        self,
        stock_prices: Mapping[str, float] | None,
        *,
        require_complete: bool,
    ) -> tuple[float, bool]:
        checked_prices = self._validated_prices(stock_prices, "stock_prices")
        held_symbols = {
            symbol
            for symbol in self.portfolio.lots
            if self.portfolio.quantity(symbol) > 0
        }
        missing_symbols = held_symbols.difference(checked_prices)
        if require_complete and missing_symbols:
            missing = ", ".join(sorted(missing_symbols))
            raise ValueError(f"missing stock prices for held positions: {missing}")

        # Full market snapshots may contain non-held symbols; they have zero value here.
        stock_value = sum(
            self.portfolio.quantity(symbol) * checked_prices[symbol]
            for symbol in held_symbols.intersection(checked_prices)
        )
        return _finite_result(stock_value, "stock market value"), not missing_symbols

    def option_market_value(
        self,
        liquidation_prices: Mapping[str, float] | None = None,
        valuation_date: date | None = None,
    ) -> float:
        checked_prices = self._validated_prices(liquidation_prices, "option_prices")
        checked_date = _optional_date(valuation_date, "valuation_date")
        unknown = set(checked_prices).difference(self._options)
        if unknown:
            raise ValueError(f"unknown option symbols: {sorted(unknown)}")

        value = 0.0
        for symbol, position in self._options.items():
            if position.settled:
                continue
            if checked_date is not None:
                if position.trade_date is not None and checked_date < position.trade_date:
                    raise ValueError(
                        f"valuation_date precedes option trade_date for {symbol}"
                    )
                if (
                    position.last_mark_date is not None
                    and checked_date < position.last_mark_date
                ):
                    raise ValueError(
                        f"valuation_date precedes option position state for {symbol}"
                    )
                if checked_date > position.expiry:
                    raise ValueError(f"unsettled option is past expiry: {symbol}")
                if (
                    symbol not in checked_prices
                    and position.last_mark_date != checked_date
                ):
                    raise ValueError(
                        f"stale option mark for {symbol}: expected {checked_date}, "
                        f"got {position.last_mark_date}"
                    )
            mark_price = checked_prices.get(symbol, position.mark_price)
            value = _sum(
                "option market value",
                value,
                _product(
                    "option position market value",
                    position.contracts,
                    position.multiplier,
                    mark_price,
                ),
            )
        return value

    def _unsettled_futures_pnl(
        self,
        futures_prices: Mapping[str, float] | None,
        valuation_date: date | None = None,
    ) -> tuple[float, Dict[str, float]]:
        checked_prices = self._validated_prices(
            futures_prices,
            "futures_prices",
            strictly_positive=True,
        )
        unknown = set(checked_prices).difference(self._futures)
        if unknown:
            raise ValueError(f"unknown futures symbols: {sorted(unknown)}")

        checked_date = _optional_date(valuation_date, "valuation_date")
        if checked_date is not None:
            for symbol, position in self._futures.items():
                dated_states = [
                    mark_date
                    for mark_date in (
                        position.trade_date,
                        position.last_settlement_date,
                        position.last_margin_rate_date,
                    )
                    if mark_date is not None
                ]
                latest_state_date = max(dated_states) if dated_states else None
                if latest_state_date is not None and checked_date < latest_state_date:
                    raise ValueError(
                        f"valuation_date precedes futures position state for {symbol}"
                    )
                dated_marks = [
                    mark_date
                    for mark_date in (
                        position.trade_date,
                        position.last_settlement_date,
                    )
                    if mark_date is not None
                ]
                latest_mark_date = max(dated_marks) if dated_marks else None
                if symbol not in checked_prices and latest_mark_date != checked_date:
                    raise ValueError(
                        f"stale futures mark for {symbol}: expected {checked_date}, "
                        f"got {latest_mark_date}"
                    )

        pnl = 0.0
        for symbol, price in checked_prices.items():
            position = self._futures[symbol]
            pnl = _sum(
                "unsettled futures pnl",
                pnl,
                _product(
                    "unsettled futures position pnl",
                    position.contracts,
                    position.multiplier,
                    price - position.settlement_price,
                ),
            )
        return pnl, checked_prices

    def net_asset_value(
        self,
        stock_prices: Mapping[str, float] | None = None,
        option_prices: Mapping[str, float] | None = None,
        futures_prices: Mapping[str, float] | None = None,
        valuation_date: date | None = None,
    ) -> float:
        stock_value, _ = self._stock_market_value(
            stock_prices,
            require_complete=True,
        )
        option_value = self.option_market_value(option_prices, valuation_date)
        unsettled_futures_pnl, _ = self._unsettled_futures_pnl(
            futures_prices,
            valuation_date,
        )
        return _sum(
            "net asset value",
            self._current_cash(),
            self.locked_margin,
            stock_value,
            option_value,
            unsettled_futures_pnl,
        )

    def total_equity(
        self,
        prices: Mapping[str, float],
        *,
        option_prices: Mapping[str, float] | None = None,
        futures_prices: Mapping[str, float] | None = None,
        valuation_date: date | None = None,
    ) -> float:
        return self.net_asset_value(
            stock_prices=prices,
            option_prices=option_prices,
            futures_prices=futures_prices,
            valuation_date=valuation_date,
        )

    def window_end_nav(
        self,
        stock_prices: Mapping[str, float] | None = None,
        option_prices: Mapping[str, float] | None = None,
        futures_prices: Mapping[str, float] | None = None,
        valuation_date: date | None = None,
    ) -> float:
        return self.net_asset_value(
            stock_prices,
            option_prices,
            futures_prices,
            valuation_date,
        )

    def check_margin(
        self,
        stock_prices: Mapping[str, float] | None = None,
        option_prices: Mapping[str, float] | None = None,
        futures_prices: Mapping[str, float] | None = None,
        valuation_date: date | None = None,
    ) -> MarginCheck:
        unsettled_pnl, checked_futures_prices = self._unsettled_futures_pnl(
            futures_prices,
            valuation_date,
        )
        effective_cash = _sum("effective cash", self._current_cash(), unsettled_pnl)
        collateral_equity = _sum(
            "collateral equity",
            effective_cash,
            self.locked_margin,
        )

        initial_required = 0.0
        maintenance_required = 0.0
        for symbol, position in self._futures.items():
            price = checked_futures_prices.get(symbol, position.settlement_price)
            notional = _product(
                "futures notional",
                abs(position.contracts),
                position.multiplier,
                price,
            )
            initial_required = _sum(
                "initial margin required",
                initial_required,
                _product("initial margin", notional, position.initial_margin_rate),
            )
            maintenance_required = _sum(
                "maintenance margin required",
                maintenance_required,
                _product(
                    "maintenance margin",
                    notional,
                    position.maintenance_margin_rate,
                ),
            )

        stock_value, nav_is_complete = self._stock_market_value(
            stock_prices,
            require_complete=False,
        )
        option_value = self.option_market_value(option_prices, valuation_date)
        nav = _sum(
            "net asset value",
            self._current_cash(),
            self.locked_margin,
            stock_value,
            option_value,
            unsettled_pnl,
        )
        nav_default = nav_is_complete and nav < -_EPSILON
        maintenance_shortfall = max(
            maintenance_required - collateral_equity,
            0.0,
        )
        locked_margin_shortfall = max(
            self.initial_margin_required - self.locked_margin,
            0.0,
        )
        cash_shortfall = max(-effective_cash, 0.0)

        if nav_default:
            status = MarginStatus.DEFAULT
        elif self._futures and (
            maintenance_shortfall > _EPSILON
            or locked_margin_shortfall > _EPSILON
            or cash_shortfall > _EPSILON
        ):
            status = MarginStatus.MARGIN_CALL
        else:
            status = MarginStatus.OK

        shortfall = max(
            maintenance_shortfall,
            locked_margin_shortfall,
            cash_shortfall,
            -nav if nav_is_complete else 0.0,
            0.0,
        )
        return MarginCheck(
            status=status,
            free_cash=self._current_cash(),
            locked_margin=self.locked_margin,
            collateral_equity=collateral_equity,
            initial_margin_required=initial_required,
            maintenance_margin_required=maintenance_required,
            nav=nav,
            nav_is_complete=nav_is_complete,
            shortfall=shortfall,
        )

    check_maintenance_margin = check_margin
    open_future = open_futures
    settle_future = settle_futures
    adjust_futures = adjust_futures_position
    set_futures_target = adjust_futures_position
    target_futures_position = adjust_futures_position
    close_future = close_futures
    buy_long_option = buy_option
    add_option = buy_option
    add_long_option = buy_option
    close_long_option = close_option
    expire_option = settle_option_expiry


UnifiedAccount = CombinedAccount


__all__ = [
    "CombinedAccount",
    "UnifiedAccount",
    "FuturesPosition",
    "LongOptionPosition",
    "FuturesSettlement",
    "FuturesMarginRateUpdate",
    "FuturesClose",
    "FuturesAdjustment",
    "OptionClose",
    "OptionSettlement",
    "MarginCheck",
    "MarginStatus",
    "OptionType",
]

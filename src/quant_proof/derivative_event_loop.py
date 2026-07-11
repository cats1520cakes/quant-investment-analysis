from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from numbers import Integral, Real
from typing import Mapping

from quant_proof.cffex_execution_parameters import (
    CffexExecutionParameterError,
    CffexExecutionParameterSchedule,
)
from quant_proof.cffex_catalog import (
    CffexCatalog,
    CffexCatalogError,
    FuturesSelection,
    OptionSelection,
)
from quant_proof.engine.combined_account import (
    CombinedAccount,
    FuturesMarginRateUpdate,
    MarginCheck,
    MarginStatus,
)
from quant_proof.free_sources.cffex_settlement_params import CffexLookupError


_FUTURES_PRODUCTS = frozenset({"IF", "IH", "IC", "IM"})
_OPTION_PRODUCTS = frozenset({"IO", "HO", "MO"})
_BPS_DENOMINATOR = 10_000.0
_EPSILON = 1e-12

DEVELOPMENT_SCOPE_LIMITATIONS = (
    "At most one open futures contract symbol may exist in a CombinedAccount; "
    "multi-symbol portfolio margin and simultaneous settlement are out of scope.",
)


class DerivativeEventError(ValueError):
    """Raised when an event would violate the causal or chronological contract."""


class MissingSettlementMarkError(DerivativeEventError):
    """Raised before mutation when an exact official end-of-day mark is missing."""


class MissingExecutionParameterError(DerivativeEventError):
    """Raised when a held derivative lacks an exact causal official parameter row."""


class EventStatus(str, Enum):
    FILLED = "filled"
    CLIPPED = "clipped"
    REJECTED = "rejected"
    NOOP = "noop"


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be finite")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _positive(value: object, name: str) -> float:
    converted = _finite(value, name)
    if converted <= 0.0:
        raise ValueError(f"{name} must be positive")
    return converted


def _nonnegative(value: object, name: str) -> float:
    converted = _finite(value, name)
    if converted < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return converted


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be a whole number of contracts")
    return int(value)


def _positive_integer(value: object, name: str) -> int:
    converted = _integer(value, name)
    if converted <= 0:
        raise ValueError(f"{name} must be a positive whole number of contracts")
    return converted


def _nonnegative_integer(value: object, name: str) -> int:
    converted = _integer(value, name)
    if converted < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return converted


def _date_key(value: object, name: str) -> str:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = (
                datetime.strptime(text, "%Y%m%d").date()
                if len(text) == 8 and text.isdigit()
                else date.fromisoformat(text)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a valid date") from exc
    return parsed.strftime("%Y%m%d")


def _as_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _product(value: object, allowed: frozenset[str], name: str = "product") -> str:
    normalized = str(value).strip().upper()
    if normalized not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _option_type(value: object) -> str:
    normalized = str(value).strip().lower()
    aliases = {"c": "call", "call": "call", "p": "put", "put": "put"}
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError("option_type must be call/C or put/P") from exc


def _symbol(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("contract must be a non-empty string")
    return value.strip().upper()


def _stock_symbol(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("stock symbol must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class DevelopmentResearchAssumptions:
    """Fixed development-tier research assumptions, not exchange specifications."""

    futures_initial_margin_rate: float = 0.12
    futures_maintenance_margin_rate: float = 0.10
    futures_slippage_bps: float = 1.0
    option_slippage_bps: float = 2.0
    futures_fee_per_contract: float = 2.0
    option_fee_per_contract: float = 1.0
    prior_day_volume_participation: float = 0.10
    assumption_label: str = "development_tier_research_assumption"

    def __post_init__(self) -> None:
        initial = _positive(
            self.futures_initial_margin_rate,
            "futures_initial_margin_rate research assumption",
        )
        maintenance = _positive(
            self.futures_maintenance_margin_rate,
            "futures_maintenance_margin_rate research assumption",
        )
        if maintenance > initial:
            raise ValueError(
                "futures_maintenance_margin_rate research assumption cannot exceed "
                "futures_initial_margin_rate"
            )
        object.__setattr__(self, "futures_initial_margin_rate", initial)
        object.__setattr__(self, "futures_maintenance_margin_rate", maintenance)
        for field_name in (
            "futures_slippage_bps",
            "option_slippage_bps",
            "futures_fee_per_contract",
            "option_fee_per_contract",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive(
                    getattr(self, field_name),
                    f"{field_name} research assumption",
                ),
            )
        if self.futures_slippage_bps >= _BPS_DENOMINATOR:
            raise ValueError("futures_slippage_bps must be below 10000")
        if self.option_slippage_bps >= _BPS_DENOMINATOR:
            raise ValueError("option_slippage_bps must be below 10000")
        participation = _positive(
            self.prior_day_volume_participation,
            "prior_day_volume_participation research assumption",
        )
        if participation > 1.0:
            raise ValueError("prior_day_volume_participation cannot exceed 1")
        object.__setattr__(self, "prior_day_volume_participation", participation)
        if self.assumption_label != "development_tier_research_assumption":
            raise ValueError("assumption_label must explicitly identify the research tier")


@dataclass(frozen=True)
class FuturesRebalanceOrder:
    signal_date: str
    product: str
    target_contracts: int
    min_dte: int = 5

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_date", _date_key(self.signal_date, "signal_date"))
        object.__setattr__(self, "product", _product(self.product, _FUTURES_PRODUCTS))
        object.__setattr__(
            self,
            "target_contracts",
            _integer(self.target_contracts, "target_contracts"),
        )
        object.__setattr__(self, "min_dte", _nonnegative_integer(self.min_dte, "min_dte"))


@dataclass(frozen=True)
class LongOptionPurchaseOrder:
    signal_date: str
    product: str
    option_type: str
    target_abs_delta: float
    min_dte: int
    max_dte: int
    cash_budget: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_date", _date_key(self.signal_date, "signal_date"))
        object.__setattr__(self, "product", _product(self.product, _OPTION_PRODUCTS))
        object.__setattr__(self, "option_type", _option_type(self.option_type))
        target = _finite(self.target_abs_delta, "target_abs_delta")
        if not 0.0 <= target <= 1.0:
            raise ValueError("target_abs_delta must be between 0 and 1")
        object.__setattr__(self, "target_abs_delta", target)
        minimum = _nonnegative_integer(self.min_dte, "min_dte")
        maximum = _nonnegative_integer(self.max_dte, "max_dte")
        if minimum > maximum:
            raise ValueError("min_dte must not exceed max_dte")
        object.__setattr__(self, "min_dte", minimum)
        object.__setattr__(self, "max_dte", maximum)
        object.__setattr__(self, "cash_budget", _nonnegative(self.cash_budget, "cash_budget"))


@dataclass(frozen=True)
class LongOptionCloseOrder:
    signal_date: str
    contract: str
    contracts: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_date", _date_key(self.signal_date, "signal_date"))
        object.__setattr__(self, "contract", _symbol(self.contract))
        if self.contracts is not None:
            object.__setattr__(
                self,
                "contracts",
                _positive_integer(self.contracts, "contracts"),
            )


@dataclass(frozen=True)
class FuturesLeg:
    contract: str
    action: str
    contracts_before: int
    target_contracts: int
    multiplier: float
    prior_day_volume: float | None
    risk_increasing: bool
    required_before_increase: bool

    @property
    def requested_contracts(self) -> int:
        return abs(self.target_contracts - self.contracts_before)


@dataclass(frozen=True)
class FuturesRebalancePlan:
    plan_id: str
    order: FuturesRebalanceOrder
    execution_date: str
    selection: FuturesSelection | None
    expected_positions: tuple[tuple[str, int], ...]
    legs: tuple[FuturesLeg, ...]


@dataclass(frozen=True)
class LongOptionPurchasePlan:
    plan_id: str
    order: LongOptionPurchaseOrder
    execution_date: str
    selection: OptionSelection


@dataclass(frozen=True)
class LongOptionClosePlan:
    plan_id: str
    order: LongOptionCloseOrder
    execution_date: str
    expected_contracts: int
    requested_contracts: int
    option_type: str
    multiplier: float
    strike: float
    expiry: date
    prior_day_volume: float | None


@dataclass(frozen=True)
class LongOptionReplacementPlan:
    plan_id: str
    close: LongOptionClosePlan
    purchase: LongOptionPurchasePlan


@dataclass(frozen=True)
class ExecutionEvent:
    event_id: str
    plan_id: str
    sequence: int
    instrument: str
    product: str
    contract: str
    action: str
    side: str
    status: EventStatus
    signal_date: str
    execution_date: str
    requested_contracts: int
    filled_contracts: int
    clipped_contracts: int
    rejected_contracts: int
    contracts_before: int
    contracts_after: int
    prior_day_volume: float | None
    participation_limit: int | None
    reference_open_price: float | None
    execution_price: float | None
    multiplier: float
    strike: float | None
    expiry: date | None
    option_type: str | None
    cash_budget: float | None
    fee_per_contract: float
    fees: float
    reason: str

    def __post_init__(self) -> None:
        quantities = (
            self.requested_contracts,
            self.filled_contracts,
            self.clipped_contracts,
            self.rejected_contracts,
        )
        if any(value < 0 for value in quantities):
            raise ValueError("event contract quantities must be non-negative")
        if self.filled_contracts + self.clipped_contracts + self.rejected_contracts != self.requested_contracts:
            raise ValueError("filled, clipped, and rejected contracts must reconcile to requested")


@dataclass(frozen=True)
class DerivativeExecutionResult:
    result_id: str
    signal_date: str
    execution_date: str
    events: tuple[ExecutionEvent, ...]
    margin: MarginCheck


@dataclass(frozen=True)
class FuturesReductionPhaseState:
    phase_id: str
    plan_id: str
    signal_date: str
    execution_date: str
    events: tuple[ExecutionEvent, ...]
    margin_before_status: MarginStatus
    margin: MarginCheck
    preflight_succeeded: bool
    required_closes_succeeded: bool
    blocking_reason: str | None
    futures_positions_after: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class SettlementEvent:
    instrument: str
    contract: str
    action: str
    settlement_price: float
    variation_margin: float
    cash_flow: float
    fees: float
    already_processed: bool


@dataclass(frozen=True)
class NavInputs:
    valuation_date: date
    stock_prices: tuple[tuple[str, float], ...]
    futures_settlements: tuple[tuple[str, float], ...]
    option_settlements: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class EndOfDayResult:
    valuation_date: date
    events: tuple[SettlementEvent, ...]
    nav_inputs: NavInputs
    margin_rate_updates: tuple[FuturesMarginRateUpdate, ...]
    margin: MarginCheck

    @property
    def nav(self) -> float:
        return self.margin.nav

    @property
    def margin_status(self) -> MarginStatus:
        return self.margin.status


def _status(requested: int, filled: int, clipped: int, rejected: int) -> EventStatus:
    if requested == 0:
        return EventStatus.NOOP if rejected == 0 else EventStatus.REJECTED
    if filled == requested:
        return EventStatus.FILLED
    if filled > 0 or clipped > 0:
        return EventStatus.CLIPPED
    return EventStatus.REJECTED


class DerivativeEventLoop:
    """Causal, daily CFFEX execution slice over one CombinedAccount."""

    development_scope_limitations = DEVELOPMENT_SCOPE_LIMITATIONS

    def __init__(
        self,
        account: CombinedAccount,
        catalog: CffexCatalog,
        assumptions: DevelopmentResearchAssumptions | None = None,
        execution_parameters: CffexExecutionParameterSchedule | None = None,
    ) -> None:
        if not isinstance(account, CombinedAccount):
            raise ValueError("account must be a CombinedAccount")
        if not isinstance(catalog, CffexCatalog):
            raise ValueError("catalog must be a CffexCatalog")
        if execution_parameters is not None and not isinstance(
            execution_parameters,
            CffexExecutionParameterSchedule,
        ):
            raise ValueError(
                "execution_parameters must be a CffexExecutionParameterSchedule"
            )
        self.account = account
        self.catalog = catalog
        self.assumptions = assumptions or DevelopmentResearchAssumptions()
        self.execution_parameters = execution_parameters
        self._future_plans: dict[FuturesRebalanceOrder, FuturesRebalancePlan] = {}
        self._option_purchase_plans: dict[
            LongOptionPurchaseOrder, LongOptionPurchasePlan
        ] = {}
        self._option_close_plans: dict[LongOptionCloseOrder, LongOptionClosePlan] = {}
        self._execution_results: dict[str, DerivativeExecutionResult] = {}
        self._futures_reduction_states: dict[
            str, FuturesReductionPhaseState
        ] = {}
        self._futures_increase_results: dict[
            str, DerivativeExecutionResult
        ] = {}
        self._eod_results: dict[str, EndOfDayResult] = {}
        self._margin_rate_updates: dict[
            str, tuple[FuturesMarginRateUpdate, ...]
        ] = {}
        self._used_participation: dict[tuple[str, str], int] = {}
        self._last_execution_date: str | None = None
        self._last_eod_date: str | None = None

    @property
    def uses_official_execution_parameters(self) -> bool:
        return self.execution_parameters is not None

    def futures_margin_rates(
        self,
        contract: str,
        as_of_date: object,
        target_contracts: int,
    ) -> tuple[float, float]:
        checked_target = _integer(target_contracts, "target_contracts")
        if checked_target == 0:
            raise ValueError("target_contracts must be non-zero for a margin lookup")
        if self.execution_parameters is None:
            return (
                self.assumptions.futures_initial_margin_rate,
                self.assumptions.futures_maintenance_margin_rate,
            )
        parameters = self.execution_parameters.lookup(
            contract,
            as_of_date,
            position_side="long" if checked_target > 0 else "short",
        )
        if (
            parameters.initial_margin_rate is None
            or parameters.maintenance_margin_rate is None
        ):
            raise CffexExecutionParameterError(
                f"official futures margin rates are unavailable for {contract}"
            )
        return (
            float(parameters.initial_margin_rate),
            float(parameters.maintenance_margin_rate),
        )

    def _trading_fee(
        self,
        *,
        contract: str,
        trade_date: str,
        contracts: int,
        price: float,
        multiplier: float,
        action: str,
        opened_date: date | None = None,
        instrument: str,
    ) -> float:
        if contracts <= 0:
            return 0.0
        if self.execution_parameters is None:
            per_contract = (
                self.assumptions.futures_fee_per_contract
                if instrument == "future"
                else self.assumptions.option_fee_per_contract
            )
            return float(contracts * per_contract)
        return self.execution_parameters.fee_amount(
            contract,
            trade_date,
            contracts,
            price,
            multiplier,
            action,
            opened_date=opened_date,
        )

    @staticmethod
    def _effective_fee_per_contract(
        fees: float,
        filled_contracts: int,
        fallback: float,
    ) -> float:
        return (
            float(fees) / float(filled_contracts)
            if filled_contracts > 0
            else float(fallback)
        )

    def apply_start_of_day_parameters(
        self,
        trade_date: object,
    ) -> tuple[FuturesMarginRateUpdate, ...]:
        date_key = _date_key(trade_date, "trade_date")
        cached = self._margin_rate_updates.get(date_key)
        if cached is not None:
            return cached
        if self._last_eod_date is not None and date_key <= self._last_eod_date:
            raise DerivativeEventError(
                "start-of-day parameters cannot be applied at or before completed EOD"
            )
        if self.execution_parameters is None or not self.account.futures_positions:
            self._margin_rate_updates[date_key] = ()
            return ()

        prepared: list[tuple[str, float, float]] = []
        for symbol, position in sorted(self.account.futures_positions.items()):
            initial, maintenance = self.futures_margin_rates(
                symbol,
                date_key,
                position.contracts,
            )
            prepared.append((symbol, initial, maintenance))
        updates = tuple(
            self.account.update_futures_margin_rates(
                symbol,
                initial,
                maintenance,
                effective_date=_as_date(date_key),
            )
            for symbol, initial, maintenance in prepared
        )
        self._margin_rate_updates[date_key] = updates
        return updates

    def _product_positions(self, product: str) -> tuple[tuple[str, int], ...]:
        return tuple(
            sorted(
                (symbol, position.contracts)
                for symbol, position in self.account.futures_positions.items()
                if symbol.upper().startswith(product)
            )
        )

    def _validate_single_future_symbol_plan(
        self,
        order: FuturesRebalanceOrder,
        selection: FuturesSelection | None,
        product_positions: tuple[tuple[str, int], ...],
    ) -> None:
        current_symbols = set(self.account.futures_positions)
        if len(current_symbols) > 1:
            raise DerivativeEventError(
                "development scope permits at most one open futures contract symbol"
            )
        final_symbols = current_symbols.difference(
            symbol for symbol, _ in product_positions
        )
        if selection is not None and order.target_contracts != 0:
            final_symbols.add(selection.contract)
        if len(final_symbols) > 1:
            raise DerivativeEventError(
                "plan would create multiple futures contract symbols; close the existing "
                "symbol in a prior event"
            )

    @staticmethod
    def _execution_violates_single_future_symbol(
        current_symbols: set[str],
        plan: FuturesRebalancePlan,
    ) -> bool:
        if len(current_symbols) > 1:
            return True
        final_symbols = set(current_symbols)
        for leg in plan.legs:
            if leg.target_contracts == 0:
                final_symbols.discard(leg.contract)
            else:
                final_symbols.add(leg.contract)
        return len(final_symbols) > 1

    def _future_signal_volumes(self, product: str, signal_date: str) -> dict[str, float]:
        try:
            curve = self.catalog.curve_snapshot(product, signal_date, min_dte=0)
        except CffexCatalogError:
            return {}
        return {
            point.contract: float(point.volume)
            for point in curve
            if point.volume is not None and math.isfinite(point.volume) and point.volume >= 0.0
        }

    def _exact_signal_date_volume(
        self,
        contract: str,
        signal_date: str,
    ) -> float | None:
        """Read only the signal-date row through the catalog's public next-open API."""

        dates = self.catalog.available_dates
        try:
            index = dates.index(signal_date)
        except ValueError:
            return None
        if index == 0:
            return None
        prior_catalog_date = dates[index - 1]
        try:
            observation = self.catalog.next_open(contract, prior_catalog_date)
        except CffexCatalogError:
            return None
        if observation.execution_date != signal_date:
            return None
        return observation.volume

    def plan_futures_rebalance(
        self,
        order: FuturesRebalanceOrder,
    ) -> FuturesRebalancePlan:
        if not isinstance(order, FuturesRebalanceOrder):
            raise ValueError("order must be a FuturesRebalanceOrder")
        cached = self._future_plans.get(order)
        if cached is not None:
            return cached

        execution_date = self.catalog.next_trading_date(order.signal_date)
        execution_gap_days = (
            _as_date(execution_date) - _as_date(order.signal_date)
        ).days
        selection = (
            None
            if order.target_contracts == 0
            else self.catalog.select_future(
                order.product,
                order.signal_date,
                min_dte=order.min_dte + execution_gap_days,
            )
        )
        positions = self._product_positions(order.product)
        self._validate_single_future_symbol_plan(order, selection, positions)
        volumes = self._future_signal_volumes(order.product, order.signal_date)
        if selection is not None:
            volumes[selection.contract] = selection.volume

        selected_contract = selection.contract if selection is not None else None
        old_positions = [
            (symbol, contracts)
            for symbol, contracts in positions
            if symbol != selected_contract
        ]
        legs: list[FuturesLeg] = []
        for symbol, contracts in old_positions:
            position = self.account.futures_positions[symbol]
            legs.append(
                FuturesLeg(
                    contract=symbol,
                    action="close" if selection is None else "roll_close",
                    contracts_before=contracts,
                    target_contracts=0,
                    multiplier=position.multiplier,
                    prior_day_volume=volumes.get(symbol),
                    risk_increasing=False,
                    required_before_increase=selection is not None,
                )
            )

        if selection is not None:
            current = dict(positions).get(selection.contract, 0)
            target = order.target_contracts
            if current * target < 0:
                legs.append(
                    FuturesLeg(
                        contract=selection.contract,
                        action="reverse_close",
                        contracts_before=current,
                        target_contracts=0,
                        multiplier=selection.multiplier,
                        prior_day_volume=selection.volume,
                        risk_increasing=False,
                        required_before_increase=True,
                    )
                )
                legs.append(
                    FuturesLeg(
                        contract=selection.contract,
                        action="reverse_open",
                        contracts_before=0,
                        target_contracts=target,
                        multiplier=selection.multiplier,
                        prior_day_volume=selection.volume,
                        risk_increasing=True,
                        required_before_increase=False,
                    )
                )
            elif current != target:
                increasing = current == 0 or abs(target) > abs(current)
                if current == 0:
                    action = "roll_open" if old_positions else "open"
                elif increasing:
                    action = "add"
                elif target == 0:
                    action = "close"
                else:
                    action = "reduce"
                legs.append(
                    FuturesLeg(
                        contract=selection.contract,
                        action=action,
                        contracts_before=current,
                        target_contracts=target,
                        multiplier=selection.multiplier,
                        prior_day_volume=selection.volume,
                        risk_increasing=increasing,
                        required_before_increase=False,
                    )
                )

        plan = FuturesRebalancePlan(
            plan_id=(
                f"futures:{order.signal_date}:{order.product}:"
                f"{order.target_contracts}:{order.min_dte}"
            ),
            order=order,
            execution_date=execution_date,
            selection=selection,
            expected_positions=positions,
            legs=tuple(legs),
        )
        self._future_plans[order] = plan
        return plan

    def _ensure_execution_date(self, execution_date: str) -> None:
        if self._last_eod_date is not None and execution_date <= self._last_eod_date:
            raise DerivativeEventError("cannot execute at or before a completed end-of-day event")
        if self._last_execution_date is not None and execution_date < self._last_execution_date:
            raise DerivativeEventError("derivative executions must be chronological")
        self._last_execution_date = execution_date

    def _participation_limit(self, prior_day_volume: float) -> int:
        return max(
            int(
                math.floor(
                    prior_day_volume
                    * self.assumptions.prior_day_volume_participation
                    + _EPSILON
                )
            ),
            0,
        )

    def _available_participation(
        self,
        execution_date: str,
        contract: str,
        prior_day_volume: float,
    ) -> tuple[int, int]:
        limit = self._participation_limit(prior_day_volume)
        used = self._used_participation.get((execution_date, contract), 0)
        return limit, max(limit - used, 0)

    @staticmethod
    def _adverse_price(reference: float, side: str, bps: float) -> float:
        direction = 1.0 if side == "buy" else -1.0
        return reference * (1.0 + direction * bps / _BPS_DENOMINATOR)

    def _future_event(
        self,
        plan: FuturesRebalancePlan,
        leg: FuturesLeg,
        sequence: int,
        *,
        filled: int,
        clipped: int,
        rejected: int,
        contracts_after: int,
        participation_limit: int | None,
        reference_open_price: float | None,
        execution_price: float | None,
        fees: float,
        reason: str,
    ) -> ExecutionEvent:
        side = "buy" if leg.target_contracts > leg.contracts_before else "sell"
        requested = leg.requested_contracts
        return ExecutionEvent(
            event_id=f"{plan.plan_id}:{sequence}:{leg.action}:{leg.contract}",
            plan_id=plan.plan_id,
            sequence=sequence,
            instrument="future",
            product=plan.order.product,
            contract=leg.contract,
            action=leg.action,
            side=side,
            status=_status(requested, filled, clipped, rejected),
            signal_date=plan.order.signal_date,
            execution_date=plan.execution_date,
            requested_contracts=requested,
            filled_contracts=filled,
            clipped_contracts=clipped,
            rejected_contracts=rejected,
            contracts_before=leg.contracts_before,
            contracts_after=contracts_after,
            prior_day_volume=leg.prior_day_volume,
            participation_limit=participation_limit,
            reference_open_price=reference_open_price,
            execution_price=execution_price,
            multiplier=leg.multiplier,
            strike=None,
            expiry=None,
            option_type=None,
            cash_budget=None,
            fee_per_contract=self._effective_fee_per_contract(
                fees,
                filled,
                0.0
                if self.execution_parameters is not None
                else self.assumptions.futures_fee_per_contract,
            ),
            fees=fees,
            reason=reason,
        )

    @staticmethod
    def _future_phase_legs(
        plan: FuturesRebalancePlan,
        *,
        risk_increasing: bool,
    ) -> tuple[tuple[int, FuturesLeg], ...]:
        return tuple(
            (sequence, leg)
            for sequence, leg in enumerate(plan.legs, start=1)
            if leg.risk_increasing is risk_increasing
        )

    def _rejected_future_phase_events(
        self,
        plan: FuturesRebalancePlan,
        entries: tuple[tuple[int, FuturesLeg], ...],
        reason: str,
    ) -> tuple[ExecutionEvent, ...]:
        events: list[ExecutionEvent] = []
        for sequence, leg in entries:
            current = self.account.futures_positions.get(leg.contract)
            current_contracts = current.contracts if current is not None else 0
            events.append(
                self._future_event(
                    plan,
                    leg,
                    sequence,
                    filled=0,
                    clipped=0,
                    rejected=leg.requested_contracts,
                    contracts_after=current_contracts,
                    participation_limit=None,
                    reference_open_price=None,
                    execution_price=None,
                    fees=0.0,
                    reason=reason,
                )
            )
        return tuple(events)

    def _execute_future_leg(
        self,
        plan: FuturesRebalancePlan,
        sequence: int,
        leg: FuturesLeg,
    ) -> ExecutionEvent:
        requested = leg.requested_contracts
        current_position = self.account.futures_positions.get(leg.contract)
        current_contracts = current_position.contracts if current_position else 0
        if leg.prior_day_volume is None:
            return self._future_event(
                plan,
                leg,
                sequence,
                filled=0,
                clipped=0,
                rejected=requested,
                contracts_after=current_contracts,
                participation_limit=None,
                reference_open_price=None,
                execution_price=None,
                fees=0.0,
                reason="missing_signal_date_volume",
            )

        try:
            opened = self.catalog.next_open(leg.contract, plan.order.signal_date)
        except CffexCatalogError as exc:
            return self._future_event(
                plan,
                leg,
                sequence,
                filled=0,
                clipped=0,
                rejected=requested,
                contracts_after=current_contracts,
                participation_limit=None,
                reference_open_price=None,
                execution_price=None,
                fees=0.0,
                reason=f"missing_or_non_executable_next_open: {exc}",
            )

        if opened.execution_date != plan.execution_date:
            raise DerivativeEventError("catalog next-open date changed after planning")
        limit, available = self._available_participation(
            plan.execution_date,
            leg.contract,
            leg.prior_day_volume,
        )
        attempted = min(requested, available)
        clipped = requested - attempted
        if attempted == 0:
            return self._future_event(
                plan,
                leg,
                sequence,
                filled=0,
                clipped=clipped,
                rejected=0,
                contracts_after=current_contracts,
                participation_limit=limit,
                reference_open_price=opened.open_price,
                execution_price=None,
                fees=0.0,
                reason="prior_day_volume_participation_cap",
            )

        side_sign = 1 if leg.target_contracts > leg.contracts_before else -1
        target_after_fill = current_contracts + side_sign * attempted
        side = "buy" if side_sign > 0 else "sell"
        execution_price = self._adverse_price(
            opened.open_price,
            side,
            self.assumptions.futures_slippage_bps,
        )
        opening = current_position is None and target_after_fill != 0
        if leg.risk_increasing:
            fee_action = "open_long" if target_after_fill > 0 else "open_short"
            opened_date = None
        else:
            fee_action = "close_long" if current_contracts > 0 else "close_short"
            opened_date = (
                current_position.trade_date if current_position is not None else None
            )
        try:
            fees = self._trading_fee(
                contract=leg.contract,
                trade_date=plan.execution_date,
                contracts=attempted,
                price=execution_price,
                multiplier=leg.multiplier,
                action=fee_action,
                opened_date=opened_date,
                instrument="future",
            )
            if opening:
                initial_margin_rate, maintenance_margin_rate = (
                    self.futures_margin_rates(
                        leg.contract,
                        plan.execution_date,
                        target_after_fill,
                    )
                )
            else:
                initial_margin_rate = None
                maintenance_margin_rate = None
        except (CffexExecutionParameterError, CffexLookupError) as exc:
            return self._future_event(
                plan,
                leg,
                sequence,
                filled=0,
                clipped=clipped,
                rejected=attempted,
                contracts_after=current_contracts,
                participation_limit=limit,
                reference_open_price=opened.open_price,
                execution_price=execution_price,
                fees=0.0,
                reason=f"execution_parameter_unavailable: {exc}",
            )
        if (
            current_position is not None
            and leg.contract == (plan.selection.contract if plan.selection else None)
            and not math.isclose(
                current_position.multiplier,
                leg.multiplier,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            multiplier_error: ValueError | None = ValueError(
                "open position multiplier conflicts with official selection multiplier"
            )
        else:
            multiplier_error = None
        try:
            if multiplier_error is not None:
                raise multiplier_error
            self.account.adjust_futures_position(
                symbol=leg.contract,
                target_contracts=target_after_fill,
                price=execution_price,
                multiplier=leg.multiplier if opening else None,
                initial_margin_rate=initial_margin_rate,
                maintenance_margin_rate=maintenance_margin_rate,
                fees=fees,
                trade_date=_as_date(plan.execution_date),
            )
        except ValueError as exc:
            return self._future_event(
                plan,
                leg,
                sequence,
                filled=0,
                clipped=clipped,
                rejected=attempted,
                contracts_after=current_contracts,
                participation_limit=limit,
                reference_open_price=opened.open_price,
                execution_price=execution_price,
                fees=0.0,
                reason=f"account_rejected: {exc}",
            )

        self._used_participation[(plan.execution_date, leg.contract)] = (
            self._used_participation.get((plan.execution_date, leg.contract), 0)
            + attempted
        )
        return self._future_event(
            plan,
            leg,
            sequence,
            filled=attempted,
            clipped=clipped,
            rejected=0,
            contracts_after=target_after_fill,
            participation_limit=limit,
            reference_open_price=opened.open_price,
            execution_price=execution_price,
            fees=fees,
            reason=("filled" if clipped == 0 else "prior_day_volume_participation_cap"),
        )

    def execute_futures_reduction_phase(
        self,
        plan: FuturesRebalancePlan,
    ) -> FuturesReductionPhaseState:
        if not isinstance(plan, FuturesRebalancePlan):
            raise ValueError("plan must be a FuturesRebalancePlan")
        cached = self._futures_reduction_states.get(plan.plan_id)
        if cached is not None:
            return cached
        self._ensure_execution_date(plan.execution_date)
        parameter_error: ValueError | None = None
        try:
            self.apply_start_of_day_parameters(plan.execution_date)
        except (CffexExecutionParameterError, CffexLookupError) as exc:
            parameter_error = exc
        margin_before = self.account.check_margin()
        entries = self._future_phase_legs(plan, risk_increasing=False)

        blocking_reason: str | None = None
        if parameter_error is not None:
            blocking_reason = f"execution_parameter_unavailable: {parameter_error}"
        elif self._execution_violates_single_future_symbol(
            set(self.account.futures_positions),
            plan,
        ):
            blocking_reason = "development_scope_multiple_futures_symbols"
        elif self._product_positions(plan.order.product) != plan.expected_positions:
            blocking_reason = "stale_plan_account_position_changed"

        if blocking_reason is None:
            events = tuple(
                self._execute_future_leg(plan, sequence, leg)
                for sequence, leg in entries
            )
            required_closes_succeeded = all(
                not leg.required_before_increase
                or (
                    event.filled_contracts == event.requested_contracts
                    and event.clipped_contracts == 0
                    and event.rejected_contracts == 0
                )
                for (_, leg), event in zip(entries, events)
            )
            preflight_succeeded = True
        else:
            events = self._rejected_future_phase_events(
                plan,
                entries,
                blocking_reason,
            )
            required_closes_succeeded = False
            preflight_succeeded = False

        state = FuturesReductionPhaseState(
            phase_id=f"{plan.plan_id}:reductions",
            plan_id=plan.plan_id,
            signal_date=plan.order.signal_date,
            execution_date=plan.execution_date,
            events=events,
            margin_before_status=margin_before.status,
            margin=self.account.check_margin(),
            preflight_succeeded=preflight_succeeded,
            required_closes_succeeded=required_closes_succeeded,
            blocking_reason=blocking_reason,
            futures_positions_after=self._product_positions(plan.order.product),
        )
        self._futures_reduction_states[plan.plan_id] = state
        return state

    def execute_futures_increase_phase(
        self,
        plan: FuturesRebalancePlan,
        reduction_state: FuturesReductionPhaseState,
    ) -> DerivativeExecutionResult:
        if not isinstance(plan, FuturesRebalancePlan):
            raise ValueError("plan must be a FuturesRebalancePlan")
        if not isinstance(reduction_state, FuturesReductionPhaseState):
            raise ValueError("reduction_state must be a FuturesReductionPhaseState")
        produced_state = self._futures_reduction_states.get(plan.plan_id)
        if produced_state is None or produced_state != reduction_state:
            raise DerivativeEventError(
                "increase phase requires the idempotent reduction state for this plan"
            )
        cached = self._futures_increase_results.get(plan.plan_id)
        if cached is not None:
            return cached
        self._ensure_execution_date(plan.execution_date)
        entries = self._future_phase_legs(plan, risk_increasing=True)

        blocking_reason: str | None = None
        if not reduction_state.preflight_succeeded:
            blocking_reason = reduction_state.blocking_reason or "reduction_phase_preflight_failed"
        elif self._product_positions(plan.order.product) != reduction_state.futures_positions_after:
            blocking_reason = "futures_position_changed_between_phases"
        elif not reduction_state.required_closes_succeeded:
            blocking_reason = "risk_increase_blocked_by_incomplete_required_close"
        elif reduction_state.margin_before_status != MarginStatus.OK:
            blocking_reason = "risk_increase_blocked_by_margin_status"
        else:
            final_symbols = set(self.account.futures_positions)
            for _, leg in entries:
                if leg.target_contracts != 0:
                    final_symbols.add(leg.contract)
            if len(final_symbols) > 1:
                blocking_reason = "development_scope_multiple_futures_symbols"

        if blocking_reason is not None:
            events = self._rejected_future_phase_events(
                plan,
                entries,
                blocking_reason,
            )
        else:
            phase_events: list[ExecutionEvent] = []
            for sequence, leg in entries:
                if self.account.check_margin().status != MarginStatus.OK:
                    phase_events.extend(
                        self._rejected_future_phase_events(
                            plan,
                            ((sequence, leg),),
                            "risk_increase_blocked_by_margin_status",
                        )
                    )
                else:
                    phase_events.append(
                        self._execute_future_leg(plan, sequence, leg)
                    )
            events = tuple(phase_events)

        result = DerivativeExecutionResult(
            result_id=f"{plan.plan_id}:increases",
            signal_date=plan.order.signal_date,
            execution_date=plan.execution_date,
            events=events,
            margin=self.account.check_margin(),
        )
        self._futures_increase_results[plan.plan_id] = result
        return result

    def execute_futures_rebalance(
        self,
        plan: FuturesRebalancePlan,
    ) -> DerivativeExecutionResult:
        """Compatibility wrapper executing reductions and increases consecutively."""

        if not isinstance(plan, FuturesRebalancePlan):
            raise ValueError("plan must be a FuturesRebalancePlan")
        cached = self._execution_results.get(plan.plan_id)
        if cached is not None:
            return cached
        reduction_state = self.execute_futures_reduction_phase(plan)
        increase_result = self.execute_futures_increase_phase(plan, reduction_state)
        result = DerivativeExecutionResult(
            result_id=plan.plan_id,
            signal_date=plan.order.signal_date,
            execution_date=plan.execution_date,
            events=(*reduction_state.events, *increase_result.events),
            margin=increase_result.margin,
        )
        self._execution_results[plan.plan_id] = result
        return result

    execute_futures_reductions = execute_futures_reduction_phase
    execute_futures_increases = execute_futures_increase_phase
    execute_futures_close_reduce_phase = execute_futures_reduction_phase
    execute_futures_open_add_phase = execute_futures_increase_phase

    def plan_long_option_purchase(
        self,
        order: LongOptionPurchaseOrder,
    ) -> LongOptionPurchasePlan:
        if not isinstance(order, LongOptionPurchaseOrder):
            raise ValueError("order must be a LongOptionPurchaseOrder")
        cached = self._option_purchase_plans.get(order)
        if cached is not None:
            return cached
        execution_date = self.catalog.next_trading_date(order.signal_date)
        execution_gap_days = (
            _as_date(execution_date) - _as_date(order.signal_date)
        ).days
        selection = self.catalog.select_option(
            order.product,
            order.signal_date,
            option_type=order.option_type,
            target_abs_delta=order.target_abs_delta,
            min_dte=order.min_dte + execution_gap_days,
            max_dte=order.max_dte + execution_gap_days,
        )
        plan = LongOptionPurchasePlan(
            plan_id=(
                f"option-buy:{order.signal_date}:{selection.contract}:"
                f"{order.cash_budget:.12g}"
            ),
            order=order,
            execution_date=execution_date,
            selection=selection,
        )
        self._option_purchase_plans[order] = plan
        return plan

    def _option_event(
        self,
        *,
        plan_id: str,
        sequence: int,
        product: str,
        contract: str,
        action: str,
        side: str,
        signal_date: str,
        execution_date: str,
        requested: int,
        filled: int,
        clipped: int,
        rejected: int,
        contracts_before: int,
        contracts_after: int,
        prior_day_volume: float | None,
        participation_limit: int | None,
        reference_open_price: float | None,
        execution_price: float | None,
        multiplier: float,
        strike: float,
        expiry: date,
        option_type: str,
        cash_budget: float | None,
        fees: float,
        reason: str,
    ) -> ExecutionEvent:
        event_status = _status(requested, filled, clipped, rejected)
        if requested == 0 and (
            reason.startswith("missing_or_non_executable_next_open")
            or reason.startswith("replacement_blocked")
            or reason.startswith("execution_parameter_unavailable")
        ):
            event_status = EventStatus.REJECTED
        return ExecutionEvent(
            event_id=f"{plan_id}:{sequence}:{action}:{contract}",
            plan_id=plan_id,
            sequence=sequence,
            instrument="option",
            product=product,
            contract=contract,
            action=action,
            side=side,
            status=event_status,
            signal_date=signal_date,
            execution_date=execution_date,
            requested_contracts=requested,
            filled_contracts=filled,
            clipped_contracts=clipped,
            rejected_contracts=rejected,
            contracts_before=contracts_before,
            contracts_after=contracts_after,
            prior_day_volume=prior_day_volume,
            participation_limit=participation_limit,
            reference_open_price=reference_open_price,
            execution_price=execution_price,
            multiplier=multiplier,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
            cash_budget=cash_budget,
            fee_per_contract=self._effective_fee_per_contract(
                fees,
                filled,
                0.0
                if self.execution_parameters is not None
                else self.assumptions.option_fee_per_contract,
            ),
            fees=fees,
            reason=reason,
        )

    def execute_long_option_purchase(
        self,
        plan: LongOptionPurchasePlan,
    ) -> DerivativeExecutionResult:
        if not isinstance(plan, LongOptionPurchasePlan):
            raise ValueError("plan must be a LongOptionPurchasePlan")
        cached = self._execution_results.get(plan.plan_id)
        if cached is not None:
            return cached
        self._ensure_execution_date(plan.execution_date)
        selection = plan.selection
        expiry = _as_date(selection.last_trade_date)
        current = self.account.option_positions.get(selection.contract)
        contracts_before = current.contracts if current is not None and not current.settled else 0
        try:
            opened = self.catalog.next_open(selection.contract, plan.order.signal_date)
        except CffexCatalogError as exc:
            event = self._option_event(
                plan_id=plan.plan_id,
                sequence=1,
                product=selection.product,
                contract=selection.contract,
                action="buy",
                side="buy",
                signal_date=plan.order.signal_date,
                execution_date=plan.execution_date,
                requested=0,
                filled=0,
                clipped=0,
                rejected=0,
                contracts_before=contracts_before,
                contracts_after=contracts_before,
                prior_day_volume=selection.volume,
                participation_limit=self._participation_limit(selection.volume),
                reference_open_price=None,
                execution_price=None,
                multiplier=selection.multiplier,
                strike=selection.strike,
                expiry=expiry,
                option_type=selection.option_type,
                cash_budget=plan.order.cash_budget,
                fees=0.0,
                reason=f"missing_or_non_executable_next_open: {exc}",
            )
            result = DerivativeExecutionResult(
                plan.plan_id,
                plan.order.signal_date,
                plan.execution_date,
                (event,),
                self.account.check_margin(),
            )
            self._execution_results[plan.plan_id] = result
            return result

        if opened.execution_date != plan.execution_date:
            raise DerivativeEventError("catalog next-open date changed after planning")
        execution_price = self._adverse_price(
            opened.open_price,
            "buy",
            self.assumptions.option_slippage_bps,
        )
        try:
            unit_fee = self._trading_fee(
                contract=selection.contract,
                trade_date=plan.execution_date,
                contracts=1,
                price=execution_price,
                multiplier=selection.multiplier,
                action="open_long",
                instrument="option",
            )
        except (CffexExecutionParameterError, CffexLookupError) as exc:
            event = self._option_event(
                plan_id=plan.plan_id,
                sequence=1,
                product=selection.product,
                contract=selection.contract,
                action="buy",
                side="buy",
                signal_date=plan.order.signal_date,
                execution_date=plan.execution_date,
                requested=0,
                filled=0,
                clipped=0,
                rejected=0,
                contracts_before=contracts_before,
                contracts_after=contracts_before,
                prior_day_volume=selection.volume,
                participation_limit=self._participation_limit(selection.volume),
                reference_open_price=opened.open_price,
                execution_price=execution_price,
                multiplier=selection.multiplier,
                strike=selection.strike,
                expiry=expiry,
                option_type=selection.option_type,
                cash_budget=plan.order.cash_budget,
                fees=0.0,
                reason=f"execution_parameter_unavailable: {exc}",
            )
            result = DerivativeExecutionResult(
                plan.plan_id,
                plan.order.signal_date,
                plan.execution_date,
                (event,),
                self.account.check_margin(),
            )
            self._execution_results[plan.plan_id] = result
            return result
        unit_cost = (
            execution_price * selection.multiplier
            + unit_fee
        )
        requested = int(math.floor(plan.order.cash_budget / unit_cost + _EPSILON))
        limit, volume_available = self._available_participation(
            plan.execution_date,
            selection.contract,
            selection.volume,
        )
        cash_available = max(self.account.free_cash, 0.0)
        cash_limit = int(math.floor(cash_available / unit_cost + _EPSILON))
        attempted = min(requested, volume_available, cash_limit)
        clipped = requested - attempted
        reasons: list[str] = []
        if requested == 0:
            reasons.append("cash_budget_below_one_contract")
        if volume_available < requested:
            reasons.append("prior_day_volume_participation_cap")
        if cash_limit < min(requested, volume_available):
            reasons.append("insufficient_account_free_cash")

        if attempted == 0:
            event = self._option_event(
                plan_id=plan.plan_id,
                sequence=1,
                product=selection.product,
                contract=selection.contract,
                action="buy",
                side="buy",
                signal_date=plan.order.signal_date,
                execution_date=plan.execution_date,
                requested=requested,
                filled=0,
                clipped=clipped,
                rejected=0,
                contracts_before=contracts_before,
                contracts_after=contracts_before,
                prior_day_volume=selection.volume,
                participation_limit=limit,
                reference_open_price=opened.open_price,
                execution_price=execution_price,
                multiplier=selection.multiplier,
                strike=selection.strike,
                expiry=expiry,
                option_type=selection.option_type,
                cash_budget=plan.order.cash_budget,
                fees=0.0,
                reason="; ".join(reasons) or "no_contracts_requested",
            )
            result = DerivativeExecutionResult(
                plan.plan_id,
                plan.order.signal_date,
                plan.execution_date,
                (event,),
                self.account.check_margin(),
            )
            self._execution_results[plan.plan_id] = result
            return result

        fees = attempted * unit_fee
        try:
            self.account.buy_option(
                symbol=selection.contract,
                option_type=selection.option_type,
                contracts=attempted,
                premium=execution_price,
                strike=selection.strike,
                expiry=expiry,
                multiplier=selection.multiplier,
                fees=fees,
                trade_date=_as_date(plan.execution_date),
            )
        except ValueError as exc:
            event = self._option_event(
                plan_id=plan.plan_id,
                sequence=1,
                product=selection.product,
                contract=selection.contract,
                action="buy",
                side="buy",
                signal_date=plan.order.signal_date,
                execution_date=plan.execution_date,
                requested=requested,
                filled=0,
                clipped=clipped,
                rejected=attempted,
                contracts_before=contracts_before,
                contracts_after=contracts_before,
                prior_day_volume=selection.volume,
                participation_limit=limit,
                reference_open_price=opened.open_price,
                execution_price=execution_price,
                multiplier=selection.multiplier,
                strike=selection.strike,
                expiry=expiry,
                option_type=selection.option_type,
                cash_budget=plan.order.cash_budget,
                fees=0.0,
                reason=f"account_rejected: {exc}",
            )
        else:
            self._used_participation[(plan.execution_date, selection.contract)] = (
                self._used_participation.get(
                    (plan.execution_date, selection.contract), 0
                )
                + attempted
            )
            event = self._option_event(
                plan_id=plan.plan_id,
                sequence=1,
                product=selection.product,
                contract=selection.contract,
                action="buy",
                side="buy",
                signal_date=plan.order.signal_date,
                execution_date=plan.execution_date,
                requested=requested,
                filled=attempted,
                clipped=clipped,
                rejected=0,
                contracts_before=contracts_before,
                contracts_after=contracts_before + attempted,
                prior_day_volume=selection.volume,
                participation_limit=limit,
                reference_open_price=opened.open_price,
                execution_price=execution_price,
                multiplier=selection.multiplier,
                strike=selection.strike,
                expiry=expiry,
                option_type=selection.option_type,
                cash_budget=plan.order.cash_budget,
                fees=fees,
                reason="; ".join(reasons) or "filled",
            )
        result = DerivativeExecutionResult(
            plan.plan_id,
            plan.order.signal_date,
            plan.execution_date,
            (event,),
            self.account.check_margin(),
        )
        self._execution_results[plan.plan_id] = result
        return result

    def plan_long_option_close(
        self,
        order: LongOptionCloseOrder,
    ) -> LongOptionClosePlan:
        if not isinstance(order, LongOptionCloseOrder):
            raise ValueError("order must be a LongOptionCloseOrder")
        cached = self._option_close_plans.get(order)
        if cached is not None:
            return cached
        try:
            position = self.account.option_positions[order.contract]
        except KeyError:
            raise DerivativeEventError(f"no long option position for {order.contract}") from None
        if position.settled:
            raise DerivativeEventError(f"option position is settled for {order.contract}")
        requested = position.contracts if order.contracts is None else order.contracts
        if requested > position.contracts:
            raise DerivativeEventError("cannot close more option contracts than are open")
        execution_date = self.catalog.next_trading_date(order.signal_date)
        if _as_date(execution_date) >= position.expiry:
            raise DerivativeEventError(
                "option close must execute before expiry; use exact-date official settlement"
            )
        plan = LongOptionClosePlan(
            plan_id=f"option-close:{order.signal_date}:{order.contract}:{requested}",
            order=order,
            execution_date=execution_date,
            expected_contracts=position.contracts,
            requested_contracts=requested,
            option_type=position.option_type.value,
            multiplier=position.multiplier,
            strike=position.strike,
            expiry=position.expiry,
            prior_day_volume=self._exact_signal_date_volume(
                order.contract,
                order.signal_date,
            ),
        )
        self._option_close_plans[order] = plan
        return plan

    def execute_long_option_close(
        self,
        plan: LongOptionClosePlan,
    ) -> DerivativeExecutionResult:
        if not isinstance(plan, LongOptionClosePlan):
            raise ValueError("plan must be a LongOptionClosePlan")
        cached = self._execution_results.get(plan.plan_id)
        if cached is not None:
            return cached
        self._ensure_execution_date(plan.execution_date)
        current = self.account.option_positions.get(plan.order.contract)
        current_contracts = current.contracts if current is not None and not current.settled else 0
        if current_contracts != plan.expected_contracts:
            event = self._option_event(
                plan_id=plan.plan_id,
                sequence=1,
                product=plan.order.contract[:2],
                contract=plan.order.contract,
                action="close",
                side="sell",
                signal_date=plan.order.signal_date,
                execution_date=plan.execution_date,
                requested=plan.requested_contracts,
                filled=0,
                clipped=0,
                rejected=plan.requested_contracts,
                contracts_before=current_contracts,
                contracts_after=current_contracts,
                prior_day_volume=plan.prior_day_volume,
                participation_limit=(
                    None
                    if plan.prior_day_volume is None
                    else self._participation_limit(plan.prior_day_volume)
                ),
                reference_open_price=None,
                execution_price=None,
                multiplier=plan.multiplier,
                strike=plan.strike,
                expiry=plan.expiry,
                option_type=plan.option_type,
                cash_budget=None,
                fees=0.0,
                reason="stale_plan_account_position_changed",
            )
        elif plan.prior_day_volume is None:
            event = self._option_event(
                plan_id=plan.plan_id,
                sequence=1,
                product=plan.order.contract[:2],
                contract=plan.order.contract,
                action="close",
                side="sell",
                signal_date=plan.order.signal_date,
                execution_date=plan.execution_date,
                requested=plan.requested_contracts,
                filled=0,
                clipped=0,
                rejected=plan.requested_contracts,
                contracts_before=current_contracts,
                contracts_after=current_contracts,
                prior_day_volume=None,
                participation_limit=None,
                reference_open_price=None,
                execution_price=None,
                multiplier=plan.multiplier,
                strike=plan.strike,
                expiry=plan.expiry,
                option_type=plan.option_type,
                cash_budget=None,
                fees=0.0,
                reason="missing_signal_date_volume",
            )
        else:
            try:
                opened = self.catalog.next_open(plan.order.contract, plan.order.signal_date)
            except CffexCatalogError as exc:
                event = self._option_event(
                    plan_id=plan.plan_id,
                    sequence=1,
                    product=plan.order.contract[:2],
                    contract=plan.order.contract,
                    action="close",
                    side="sell",
                    signal_date=plan.order.signal_date,
                    execution_date=plan.execution_date,
                    requested=plan.requested_contracts,
                    filled=0,
                    clipped=0,
                    rejected=plan.requested_contracts,
                    contracts_before=current_contracts,
                    contracts_after=current_contracts,
                    prior_day_volume=plan.prior_day_volume,
                    participation_limit=self._participation_limit(
                        plan.prior_day_volume
                    ),
                    reference_open_price=None,
                    execution_price=None,
                    multiplier=plan.multiplier,
                    strike=plan.strike,
                    expiry=plan.expiry,
                    option_type=plan.option_type,
                    cash_budget=None,
                    fees=0.0,
                    reason=f"missing_or_non_executable_next_open: {exc}",
                )
            else:
                if opened.execution_date != plan.execution_date:
                    raise DerivativeEventError("catalog next-open date changed after planning")
                limit, available = self._available_participation(
                    plan.execution_date,
                    plan.order.contract,
                    plan.prior_day_volume,
                )
                attempted = min(plan.requested_contracts, available)
                clipped = plan.requested_contracts - attempted
                if attempted == 0:
                    event = self._option_event(
                        plan_id=plan.plan_id,
                        sequence=1,
                        product=plan.order.contract[:2],
                        contract=plan.order.contract,
                        action="close",
                        side="sell",
                        signal_date=plan.order.signal_date,
                        execution_date=plan.execution_date,
                        requested=plan.requested_contracts,
                        filled=0,
                        clipped=clipped,
                        rejected=0,
                        contracts_before=current_contracts,
                        contracts_after=current_contracts,
                        prior_day_volume=plan.prior_day_volume,
                        participation_limit=limit,
                        reference_open_price=opened.open_price,
                        execution_price=None,
                        multiplier=plan.multiplier,
                        strike=plan.strike,
                        expiry=plan.expiry,
                        option_type=plan.option_type,
                        cash_budget=None,
                        fees=0.0,
                        reason="prior_day_volume_participation_cap",
                    )
                    result = DerivativeExecutionResult(
                        plan.plan_id,
                        plan.order.signal_date,
                        plan.execution_date,
                        (event,),
                        self.account.check_margin(),
                    )
                    self._execution_results[plan.plan_id] = result
                    return result
                execution_price = self._adverse_price(
                    opened.open_price,
                    "sell",
                    self.assumptions.option_slippage_bps,
                )
                try:
                    fees = self._trading_fee(
                        contract=plan.order.contract,
                        trade_date=plan.execution_date,
                        contracts=attempted,
                        price=execution_price,
                        multiplier=plan.multiplier,
                        action="close_long",
                        opened_date=(
                            current.trade_date if current is not None else None
                        ),
                        instrument="option",
                    )
                except (CffexExecutionParameterError, CffexLookupError) as exc:
                    event = self._option_event(
                        plan_id=plan.plan_id,
                        sequence=1,
                        product=plan.order.contract[:2],
                        contract=plan.order.contract,
                        action="close",
                        side="sell",
                        signal_date=plan.order.signal_date,
                        execution_date=plan.execution_date,
                        requested=plan.requested_contracts,
                        filled=0,
                        clipped=clipped,
                        rejected=attempted,
                        contracts_before=current_contracts,
                        contracts_after=current_contracts,
                        prior_day_volume=plan.prior_day_volume,
                        participation_limit=limit,
                        reference_open_price=opened.open_price,
                        execution_price=execution_price,
                        multiplier=plan.multiplier,
                        strike=plan.strike,
                        expiry=plan.expiry,
                        option_type=plan.option_type,
                        cash_budget=None,
                        fees=0.0,
                        reason=f"execution_parameter_unavailable: {exc}",
                    )
                    result = DerivativeExecutionResult(
                        plan.plan_id,
                        plan.order.signal_date,
                        plan.execution_date,
                        (event,),
                        self.account.check_margin(),
                    )
                    self._execution_results[plan.plan_id] = result
                    return result
                try:
                    closed = self.account.close_option(
                        plan.order.contract,
                        liquidation_price=execution_price,
                        contracts=attempted,
                        fees=fees,
                        trade_date=_as_date(plan.execution_date),
                    )
                except ValueError as exc:
                    event = self._option_event(
                        plan_id=plan.plan_id,
                        sequence=1,
                        product=plan.order.contract[:2],
                        contract=plan.order.contract,
                        action="close",
                        side="sell",
                        signal_date=plan.order.signal_date,
                        execution_date=plan.execution_date,
                        requested=plan.requested_contracts,
                        filled=0,
                        clipped=clipped,
                        rejected=attempted,
                        contracts_before=current_contracts,
                        contracts_after=current_contracts,
                        prior_day_volume=plan.prior_day_volume,
                        participation_limit=limit,
                        reference_open_price=opened.open_price,
                        execution_price=execution_price,
                        multiplier=plan.multiplier,
                        strike=plan.strike,
                        expiry=plan.expiry,
                        option_type=plan.option_type,
                        cash_budget=None,
                        fees=0.0,
                        reason=f"account_rejected: {exc}",
                    )
                else:
                    self._used_participation[
                        (plan.execution_date, plan.order.contract)
                    ] = (
                        self._used_participation.get(
                            (plan.execution_date, plan.order.contract), 0
                        )
                        + attempted
                    )
                    event = self._option_event(
                        plan_id=plan.plan_id,
                        sequence=1,
                        product=plan.order.contract[:2],
                        contract=plan.order.contract,
                        action="close",
                        side="sell",
                        signal_date=plan.order.signal_date,
                        execution_date=plan.execution_date,
                        requested=plan.requested_contracts,
                        filled=attempted,
                        clipped=clipped,
                        rejected=0,
                        contracts_before=current_contracts,
                        contracts_after=closed.remaining_contracts,
                        prior_day_volume=plan.prior_day_volume,
                        participation_limit=limit,
                        reference_open_price=opened.open_price,
                        execution_price=execution_price,
                        multiplier=plan.multiplier,
                        strike=plan.strike,
                        expiry=plan.expiry,
                        option_type=plan.option_type,
                        cash_budget=None,
                        fees=fees,
                        reason=(
                            "filled"
                            if clipped == 0
                            else "prior_day_volume_participation_cap"
                        ),
                    )
        result = DerivativeExecutionResult(
            plan.plan_id,
            plan.order.signal_date,
            plan.execution_date,
            (event,),
            self.account.check_margin(),
        )
        self._execution_results[plan.plan_id] = result
        return result

    def plan_long_option_replacement(
        self,
        close_order: LongOptionCloseOrder,
        purchase_order: LongOptionPurchaseOrder,
    ) -> LongOptionReplacementPlan:
        if close_order.signal_date != purchase_order.signal_date:
            raise DerivativeEventError("close and replacement must share one signal date")
        close = self.plan_long_option_close(close_order)
        purchase = self.plan_long_option_purchase(purchase_order)
        if close.execution_date != purchase.execution_date:
            raise DerivativeEventError("close and replacement execution dates differ")
        return LongOptionReplacementPlan(
            plan_id=f"option-replace:{close.plan_id}:{purchase.plan_id}",
            close=close,
            purchase=purchase,
        )

    def execute_long_option_replacement(
        self,
        plan: LongOptionReplacementPlan,
    ) -> DerivativeExecutionResult:
        if not isinstance(plan, LongOptionReplacementPlan):
            raise ValueError("plan must be a LongOptionReplacementPlan")
        cached = self._execution_results.get(plan.plan_id)
        if cached is not None:
            return cached
        close_result = self.execute_long_option_close(plan.close)
        close_event = close_result.events[0]
        full_close_requested = (
            plan.close.requested_contracts == plan.close.expected_contracts
        )
        close_succeeded = (
            full_close_requested
            and close_event.filled_contracts == close_event.requested_contracts
            and close_event.contracts_after == 0
        )
        if close_succeeded:
            purchase_result = self.execute_long_option_purchase(plan.purchase)
            events = (close_event, *purchase_result.events)
        else:
            selection = plan.purchase.selection
            blocked = self._option_event(
                plan_id=plan.plan_id,
                sequence=2,
                product=selection.product,
                contract=selection.contract,
                action="replacement_buy",
                side="buy",
                signal_date=plan.purchase.order.signal_date,
                execution_date=plan.purchase.execution_date,
                requested=0,
                filled=0,
                clipped=0,
                rejected=0,
                contracts_before=0,
                contracts_after=0,
                prior_day_volume=selection.volume,
                participation_limit=self._participation_limit(selection.volume),
                reference_open_price=None,
                execution_price=None,
                multiplier=selection.multiplier,
                strike=selection.strike,
                expiry=_as_date(selection.last_trade_date),
                option_type=selection.option_type,
                cash_budget=plan.purchase.order.cash_budget,
                fees=0.0,
                reason="replacement_blocked_by_incomplete_or_failed_close",
            )
            events = (close_event, blocked)
        result = DerivativeExecutionResult(
            plan.plan_id,
            plan.close.order.signal_date,
            plan.close.execution_date,
            events,
            self.account.check_margin(),
        )
        self._execution_results[plan.plan_id] = result
        return result

    @staticmethod
    def _stock_price_items(
        stock_prices: Mapping[str, float] | None,
    ) -> tuple[tuple[str, float], ...]:
        if stock_prices is None:
            return ()
        if not isinstance(stock_prices, Mapping):
            raise ValueError("stock_prices must be a mapping")
        checked = [
            (_stock_symbol(symbol), _nonnegative(price, f"stock_prices[{symbol!r}]"))
            for symbol, price in stock_prices.items()
        ]
        return tuple(sorted(checked))

    def settle_end_of_day(
        self,
        valuation_date: object,
        *,
        stock_prices: Mapping[str, float] | None = None,
    ) -> EndOfDayResult:
        date_key = _date_key(valuation_date, "valuation_date")
        date_value = _as_date(date_key)
        stock_items = self._stock_price_items(stock_prices)
        stock_price_map = dict(stock_items)
        cached = self._eod_results.get(date_key)
        if cached is not None:
            if cached.nav_inputs.stock_prices != stock_items:
                raise DerivativeEventError(
                    "conflicting stock-price inputs for an idempotent end-of-day event"
                )
            return cached
        held_stock_symbols = {
            symbol
            for symbol in self.account.portfolio.lots
            if self.account.portfolio.quantity(symbol) > 0
        }
        missing_stock_prices = sorted(held_stock_symbols.difference(stock_price_map))
        if missing_stock_prices:
            raise MissingSettlementMarkError(
                "finite end-of-day close prices are required for every held stock: "
                + ", ".join(missing_stock_prices)
            )
        if date_key not in self.catalog.available_dates:
            raise MissingSettlementMarkError(
                f"valuation date is absent from CFFEX catalog: {date_key}"
            )
        if self._last_execution_date is not None and date_key < self._last_execution_date:
            raise DerivativeEventError("end-of-day events cannot precede executions")
        if self._last_eod_date is not None and date_key < self._last_eod_date:
            raise DerivativeEventError("end-of-day events must be chronological")
        if len(self.account.futures_positions) > 1:
            raise DerivativeEventError(
                "development scope permits at most one open futures contract symbol; "
                "refusing order-dependent sequential settlement"
            )

        futures_marks: list[tuple[str, float]] = []
        futures_expiries: dict[str, date] = {}
        option_marks: list[tuple[str, float]] = []
        try:
            for symbol, position in sorted(self.account.futures_positions.items()):
                if position.trade_date is not None and date_value < position.trade_date:
                    raise DerivativeEventError(
                        f"valuation date precedes futures trade date for {symbol}"
                    )
                if (
                    position.last_settlement_date is not None
                    and date_value < position.last_settlement_date
                ):
                    raise DerivativeEventError(
                        f"valuation date precedes futures settlement state for {symbol}"
                    )
                expiry = _as_date(self.catalog.last_trade_date(symbol, date_key))
                if date_value > expiry:
                    raise DerivativeEventError(
                        f"unsettled future passed its exact expiry date: {symbol}"
                    )
                mark = self.catalog.settlement(symbol, date_key)
                if mark <= 0.0:
                    raise CffexCatalogError(
                        f"official futures settlement must be positive for {symbol} on {date_key}"
                    )
                futures_marks.append((symbol, mark))
                futures_expiries[symbol] = expiry
            for symbol, position in sorted(self.account.option_positions.items()):
                if position.settled:
                    continue
                if date_value > position.expiry:
                    raise DerivativeEventError(
                        f"unsettled option passed its exact expiry date: {symbol}"
                    )
                if position.trade_date is not None and date_value < position.trade_date:
                    raise DerivativeEventError(
                        f"valuation date precedes option trade date for {symbol}"
                    )
                if position.last_mark_date is not None and date_value < position.last_mark_date:
                    raise DerivativeEventError(
                        f"valuation date precedes option mark state for {symbol}"
                    )
                option_marks.append((symbol, self.catalog.settlement(symbol, date_key)))
        except CffexCatalogError as exc:
            raise MissingSettlementMarkError(
                f"exact official end-of-day mark unavailable: {exc}"
            ) from exc

        futures_settlement_fees: dict[str, float] = {}
        option_settlement_fees: dict[str, float] = {}
        try:
            if self.execution_parameters is not None:
                for symbol, mark in futures_marks:
                    position = self.account.futures_positions[symbol]
                    self.futures_margin_rates(
                        symbol,
                        date_key,
                        position.contracts,
                    )
                    futures_settlement_fees[symbol] = (
                        self.execution_parameters.settlement_fee_amount(
                            symbol,
                            date_key,
                            abs(position.contracts),
                            mark,
                            position.multiplier,
                            position_side=(
                                "long" if position.contracts > 0 else "short"
                            ),
                        )
                        if date_value == futures_expiries[symbol]
                        else 0.0
                    )
                for symbol, mark in option_marks:
                    position = self.account.option_positions[symbol]
                    option_settlement_fees[symbol] = (
                        self.execution_parameters.settlement_fee_amount(
                            symbol,
                            date_key,
                            position.contracts,
                            mark,
                            position.multiplier,
                            position_side="long",
                        )
                        if date_value == position.expiry and mark > 0.0
                        else 0.0
                    )
        except (CffexExecutionParameterError, CffexLookupError) as exc:
            raise MissingExecutionParameterError(
                f"exact causal official end-of-day parameter unavailable: {exc}"
            ) from exc

        margin_rate_updates = self.apply_start_of_day_parameters(date_key)

        events: list[SettlementEvent] = []
        for symbol, mark in futures_marks:
            settled = self.account.settle_futures(
                symbol,
                mark,
                settlement_date=date_value,
            )
            events.append(
                SettlementEvent(
                    instrument="future",
                    contract=symbol,
                    action="daily_settlement",
                    settlement_price=mark,
                    variation_margin=settled.variation_margin,
                    cash_flow=settled.variation_margin,
                    fees=0.0,
                    already_processed=settled.already_settled,
                )
            )
            if date_value == futures_expiries[symbol]:
                delivery_fee = futures_settlement_fees.get(symbol, 0.0)
                expired = self.account.close_futures(
                    symbol,
                    mark,
                    fees=delivery_fee,
                    trade_date=date_value,
                )
                events.append(
                    SettlementEvent(
                        instrument="future",
                        contract=symbol,
                        action="expiry_cash_settlement",
                        settlement_price=mark,
                        variation_margin=expired.variation_margin,
                        cash_flow=expired.variation_margin - expired.fees,
                        fees=expired.fees,
                        already_processed=False,
                    )
                )
        for symbol, mark in option_marks:
            position = self.account.option_positions[symbol]
            if date_value == position.expiry:
                settled = self.account.settle_option_expiry(
                    symbol,
                    settlement_date=date_value,
                    settlement_option_price=mark,
                    fees=option_settlement_fees.get(symbol, 0.0),
                )
                events.append(
                    SettlementEvent(
                        instrument="option",
                        contract=symbol,
                        action="expiry_cash_settlement",
                        settlement_price=mark,
                        variation_margin=0.0,
                        cash_flow=settled.cash_flow,
                        fees=settled.fees,
                        already_processed=settled.already_settled,
                    )
                )
            else:
                self.account.mark_option(symbol, mark, as_of=date_value)
                events.append(
                    SettlementEvent(
                        instrument="option",
                        contract=symbol,
                        action="daily_mark",
                        settlement_price=mark,
                        variation_margin=0.0,
                        cash_flow=0.0,
                        fees=0.0,
                        already_processed=False,
                    )
                )

        margin = self.account.check_margin(
            stock_prices=stock_price_map,
            valuation_date=date_value,
        )
        if not margin.nav_is_complete:
            raise RuntimeError("complete stock marks did not produce a complete unified NAV")
        result = EndOfDayResult(
            valuation_date=date_value,
            events=tuple(events),
            nav_inputs=NavInputs(
                valuation_date=date_value,
                stock_prices=stock_items,
                futures_settlements=tuple(futures_marks),
                option_settlements=tuple(option_marks),
            ),
            margin_rate_updates=margin_rate_updates,
            margin=margin,
        )
        self._eod_results[date_key] = result
        self._last_eod_date = date_key
        return result

    end_of_day = settle_end_of_day


__all__ = [
    "DEVELOPMENT_SCOPE_LIMITATIONS",
    "DevelopmentResearchAssumptions",
    "DerivativeEventError",
    "DerivativeEventLoop",
    "DerivativeExecutionResult",
    "EndOfDayResult",
    "EventStatus",
    "ExecutionEvent",
    "FuturesLeg",
    "FuturesReductionPhaseState",
    "FuturesRebalanceOrder",
    "FuturesRebalancePlan",
    "LongOptionCloseOrder",
    "LongOptionClosePlan",
    "LongOptionPurchaseOrder",
    "LongOptionPurchasePlan",
    "LongOptionReplacementPlan",
    "MissingExecutionParameterError",
    "MissingSettlementMarkError",
    "NavInputs",
    "SettlementEvent",
]

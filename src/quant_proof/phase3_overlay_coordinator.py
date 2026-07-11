from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from numbers import Integral, Real
from types import MappingProxyType
from typing import Mapping

from quant_proof.cffex_catalog import CffexCatalogError, FuturesSelection
from quant_proof.cffex_execution_parameters import CffexExecutionParameterError
from quant_proof.derivative_event_loop import (
    DerivativeEventError,
    DerivativeEventLoop,
    DerivativeExecutionResult,
    ExecutionEvent,
    FuturesRebalanceOrder,
    FuturesRebalancePlan,
    FuturesReductionPhaseState,
    LongOptionCloseOrder,
    LongOptionClosePlan,
    LongOptionPurchaseOrder,
    LongOptionPurchasePlan,
    MissingExecutionParameterError,
    MissingSettlementMarkError,
)
from quant_proof.engine.combined_account import CombinedAccount
from quant_proof.free_real_backtest import DerivativeEndOfDaySnapshot
from quant_proof.free_sources.cffex_settlement_params import CffexLookupError


_FUTURES_PRODUCTS = frozenset({"IF", "IH", "IC", "IM"})
_OPTION_PRODUCTS = frozenset({"IO", "HO", "MO"})
_DIRECTIONS = frozenset({"long", "short", "flat"})
_REBALANCE_FREQUENCIES = frozenset({"daily", "weekly", "monthly"})
_EPSILON = 1e-12

_COUNTER_KEYS = (
    "derivative_signal_latches",
    "derivative_force_flat_signals",
    "derivative_plans",
    "futures_plans",
    "futures_expiry_roll_plans",
    "option_close_plans",
    "option_buy_plans",
    "derivative_reduction_phases",
    "derivative_increase_phases",
    "derivative_execution_events",
    "derivative_order_requests",
    "derivative_filled_orders",
    "derivative_clipped_orders",
    "derivative_rejected_orders",
    "derivative_requested_contracts",
    "derivative_filled_contracts",
    "derivative_clipped_contracts",
    "derivative_rejected_contracts",
    "derivative_fees",
    "derivative_missing_catalog_events",
    "derivative_missing_execution_parameter_events",
    "derivative_plan_rejections",
    "futures_sizing_truncated_contracts",
    "futures_margin_rate_updates",
    "futures_margin_transfer",
    "option_buys_blocked",
)


class OverlayCoordinatorError(ValueError):
    """Raised when coordinator inputs conflict with an already-latched event."""


def _counters() -> dict[str, float]:
    return {key: 0.0 for key in _COUNTER_KEYS}


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be finite")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _nonnegative(value: object, name: str) -> float:
    converted = _finite(value, name)
    if converted < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return converted


def _ratio(value: object, name: str) -> float:
    converted = _finite(value, name)
    if not 0.0 <= converted <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return converted


def _whole(value: object, name: str, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be a whole number")
    converted = int(value)
    minimum = 0 if allow_zero else 1
    if converted < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {qualifier} whole number")
    return converted


def _date_key(value: object, name: str = "trade_date") -> str:
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


def _direction(value: object, name: str = "direction") -> str:
    normalized = str(value).strip().lower()
    if normalized not in _DIRECTIONS:
        raise ValueError(f"{name} must be long, short, or flat")
    return normalized


def _option_type(value: object) -> str:
    normalized = str(value).strip().lower()
    aliases = {"c": "call", "call": "call", "p": "put", "put": "put"}
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError("option_type must be call/C or put/P") from exc


def _period_key(trade_date: str, frequency: str) -> str:
    parsed = _as_date(trade_date)
    if frequency == "daily":
        return trade_date
    if frequency == "weekly":
        iso_year, iso_week, _ = parsed.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    return trade_date[:6]


@dataclass(frozen=True)
class FuturesOverlaySpec:
    """One-product, whole-contract futures target specification."""

    product: str
    direction: str = "long"
    direction_by_signal_date: Mapping[str, str] | None = None
    fixed_contracts: int | None = None
    target_notional_multiple: float | None = None
    margin_budget: float | None = None
    min_dte: int = 5
    rebalance_frequency: str = "monthly"
    cash_buffer_pct: float = 0.0
    max_contracts: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "product", _product(self.product, _FUTURES_PRODUCTS))
        object.__setattr__(self, "direction", _direction(self.direction))

        supplied_modes = sum(
            value is not None
            for value in (
                self.fixed_contracts,
                self.target_notional_multiple,
                self.margin_budget,
            )
        )
        if supplied_modes != 1:
            raise ValueError(
                "provide exactly one futures target mode: fixed_contracts, "
                "target_notional_multiple, or margin_budget"
            )
        if self.fixed_contracts is not None:
            object.__setattr__(
                self,
                "fixed_contracts",
                _whole(self.fixed_contracts, "fixed_contracts"),
            )
        if self.target_notional_multiple is not None:
            object.__setattr__(
                self,
                "target_notional_multiple",
                _nonnegative(
                    self.target_notional_multiple,
                    "target_notional_multiple",
                ),
            )
        if self.margin_budget is not None:
            object.__setattr__(
                self,
                "margin_budget",
                _nonnegative(self.margin_budget, "margin_budget"),
            )

        object.__setattr__(self, "min_dte", _whole(self.min_dte, "min_dte"))
        frequency = str(self.rebalance_frequency).strip().lower()
        if frequency not in _REBALANCE_FREQUENCIES:
            raise ValueError("rebalance_frequency must be daily, weekly, or monthly")
        object.__setattr__(self, "rebalance_frequency", frequency)
        object.__setattr__(
            self,
            "cash_buffer_pct",
            _ratio(self.cash_buffer_pct, "cash_buffer_pct"),
        )
        if self.max_contracts is not None:
            object.__setattr__(
                self,
                "max_contracts",
                _whole(self.max_contracts, "max_contracts"),
            )

        if self.direction_by_signal_date is not None:
            if not isinstance(self.direction_by_signal_date, Mapping):
                raise ValueError("direction_by_signal_date must be a mapping")
            normalized: dict[str, str] = {}
            for signal_date, mapped_direction in self.direction_by_signal_date.items():
                key = _date_key(signal_date, "direction signal_date")
                if key in normalized:
                    raise ValueError("direction_by_signal_date contains duplicate dates")
                normalized[key] = _direction(
                    mapped_direction,
                    f"direction_by_signal_date[{key}]",
                )
            object.__setattr__(
                self,
                "direction_by_signal_date",
                MappingProxyType(dict(sorted(normalized.items()))),
            )

    @property
    def target_mode(self) -> str:
        if self.fixed_contracts is not None:
            return "fixed_contracts"
        if self.target_notional_multiple is not None:
            return "target_notional_multiple"
        return "margin_budget"

    def direction_on(self, signal_date: object) -> str:
        key = _date_key(signal_date, "signal_date")
        if self.direction_by_signal_date is None:
            return self.direction
        return self.direction_by_signal_date.get(key, "flat")


@dataclass(frozen=True)
class LongOptionOverlaySpec:
    """Monthly long-option convexity budget specification."""

    product: str
    option_type: str
    target_abs_delta: float
    min_dte: int
    max_dte: int
    budget_pct_nav: float
    exit_dte: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "product", _product(self.product, _OPTION_PRODUCTS))
        object.__setattr__(self, "option_type", _option_type(self.option_type))
        object.__setattr__(
            self,
            "target_abs_delta",
            _ratio(self.target_abs_delta, "target_abs_delta"),
        )
        minimum = _whole(self.min_dte, "min_dte")
        maximum = _whole(self.max_dte, "max_dte")
        if minimum > maximum:
            raise ValueError("min_dte must not exceed max_dte")
        object.__setattr__(self, "min_dte", minimum)
        object.__setattr__(self, "max_dte", maximum)
        object.__setattr__(
            self,
            "budget_pct_nav",
            _ratio(self.budget_pct_nav, "budget_pct_nav"),
        )
        object.__setattr__(self, "exit_dte", _whole(self.exit_dte, "exit_dte"))


@dataclass(frozen=True)
class OverlayAuditRecord:
    signal_date: str
    execution_date: str | None
    stage: str
    instrument: str
    reason: str
    fail_closed: bool = True


@dataclass(frozen=True)
class PendingOverlayPlan:
    signal_date: str
    execution_date: str
    force_flat: bool
    futures_target_contracts: int | None
    futures_plan: FuturesRebalancePlan | None
    option_close_plans: tuple[LongOptionClosePlan, ...]
    option_purchase_plan: LongOptionPurchasePlan | None
    option_close_planning_succeeded: bool


@dataclass(frozen=True)
class _ReductionBundle:
    futures_state: FuturesReductionPhaseState | None
    option_close_results: tuple[DerivativeExecutionResult, ...]
    option_closes_succeeded: bool


class Phase3OverlayCoordinator:
    """CFFEX overlay adapter for ``FreeRealDerivativeCoordinator``."""

    def __init__(
        self,
        account: CombinedAccount,
        event_loop: DerivativeEventLoop,
        futures_spec: FuturesOverlaySpec | None = None,
        option_spec: LongOptionOverlaySpec | None = None,
        *,
        futures: FuturesOverlaySpec | None = None,
        long_option: LongOptionOverlaySpec | None = None,
    ) -> None:
        if not isinstance(account, CombinedAccount):
            raise ValueError("account must be a CombinedAccount")
        if not isinstance(event_loop, DerivativeEventLoop):
            raise ValueError("event_loop must be a DerivativeEventLoop")
        if event_loop.account is not account:
            raise ValueError("account and event_loop must share the same CombinedAccount")
        if futures_spec is not None and futures is not None:
            raise ValueError("provide futures_spec or futures, not both")
        if option_spec is not None and long_option is not None:
            raise ValueError("provide option_spec or long_option, not both")
        selected_futures = futures_spec if futures_spec is not None else futures
        selected_option = option_spec if option_spec is not None else long_option
        if selected_futures is not None and not isinstance(
            selected_futures,
            FuturesOverlaySpec,
        ):
            raise ValueError("futures_spec must be a FuturesOverlaySpec")
        if selected_option is not None and not isinstance(
            selected_option,
            LongOptionOverlaySpec,
        ):
            raise ValueError("option_spec must be a LongOptionOverlaySpec")

        self.account = account
        self.event_loop = event_loop
        self.futures_spec = selected_futures
        self.option_spec = selected_option
        self._pending: dict[str, PendingOverlayPlan] = {}
        self._completed_execution_dates: set[str] = set()
        self._latched_inputs: dict[str, tuple[float, bool]] = {}
        self._latch_counters: dict[str, dict[str, float]] = {}
        self._reduction_bundles: dict[str, _ReductionBundle] = {}
        self._reduction_counters: dict[str, dict[str, float]] = {}
        self._increase_counters: dict[str, dict[str, float]] = {}
        self._futures_periods: set[str] = set()
        self._option_periods: set[str] = set()
        self._audit: list[OverlayAuditRecord] = []

    @property
    def audit_log(self) -> tuple[OverlayAuditRecord, ...]:
        return tuple(self._audit)

    @property
    def pending_plans(self) -> Mapping[str, PendingOverlayPlan]:
        active = {
            execution_date: plan
            for execution_date, plan in self._pending.items()
            if execution_date not in self._completed_execution_dates
        }
        return MappingProxyType(active)

    @property
    def pending_execution_dates(self) -> tuple[str, ...]:
        return tuple(sorted(self.pending_plans))

    def _audit_failure(
        self,
        counters: dict[str, float],
        *,
        signal_date: str,
        execution_date: str | None,
        stage: str,
        instrument: str,
        reason: object,
        missing_catalog: bool,
    ) -> None:
        counters["derivative_plan_rejections"] += 1.0
        if missing_catalog:
            counters["derivative_missing_catalog_events"] += 1.0
        self._audit.append(
            OverlayAuditRecord(
                signal_date=signal_date,
                execution_date=execution_date,
                stage=stage,
                instrument=instrument,
                reason=str(reason),
            )
        )

    def _matching_option_symbols(self) -> tuple[str, ...]:
        spec = self.option_spec
        if spec is None:
            return ()
        return tuple(
            sorted(
                symbol
                for symbol, position in self.account.option_positions.items()
                if not position.settled
                and symbol.upper().startswith(spec.product)
                and position.option_type.value == spec.option_type
            )
        )

    def _current_product_margin(self, product: str) -> float:
        return sum(
            position.locked_margin
            for symbol, position in self.account.futures_positions.items()
            if symbol.upper().startswith(product)
        )

    def _futures_roll_due(
        self,
        spec: FuturesOverlaySpec,
        signal_date: str,
    ) -> bool:
        held = tuple(
            symbol
            for symbol in self.account.futures_positions
            if symbol.upper().startswith(spec.product)
        )
        if not held:
            return False
        try:
            curve = self.event_loop.catalog.curve_snapshot(
                spec.product,
                signal_date,
                min_dte=0,
            )
            execution_date = self.event_loop.catalog.next_trading_date(signal_date)
        except CffexCatalogError:
            return True
        dte_by_contract = {point.contract: point.dte for point in curve}
        execution_gap_days = (
            _as_date(execution_date) - _as_date(signal_date)
        ).days
        return any(
            symbol not in dte_by_contract
            or int(dte_by_contract[symbol]) - execution_gap_days <= spec.min_dte
            for symbol in held
        )

    def _futures_target(
        self,
        spec: FuturesOverlaySpec,
        selection: FuturesSelection,
        nav: float,
        direction: str,
    ) -> tuple[int, int]:
        contract_notional = selection.settlement_price * selection.multiplier
        sign = {"long": 1, "short": -1, "flat": 0}[direction]
        if sign == 0:
            raise ValueError("futures target sizing requires a non-flat direction")
        margin_rate, _ = self.event_loop.futures_margin_rates(
            selection.contract,
            selection.signal_date,
            sign,
        )
        contract_margin = contract_notional * margin_rate
        if not math.isfinite(contract_notional) or contract_notional <= 0.0:
            raise CffexCatalogError("official selection notional must be positive")
        if not math.isfinite(contract_margin) or contract_margin <= 0.0:
            raise ValueError("futures margin per contract must be positive")

        if spec.fixed_contracts is not None:
            raw_contracts = spec.fixed_contracts
        elif spec.target_notional_multiple is not None:
            target_notional = max(nav, 0.0) * spec.target_notional_multiple
            raw_contracts = int(
                math.floor(target_notional / contract_notional + _EPSILON)
            )
        else:
            assert spec.margin_budget is not None
            raw_contracts = int(
                math.floor(spec.margin_budget / contract_margin + _EPSILON)
            )

        capped_contracts = raw_contracts
        if spec.max_contracts is not None:
            capped_contracts = min(capped_contracts, spec.max_contracts)
        free_cash = max(float(self.account.free_cash), 0.0)
        spendable_cash = free_cash * (1.0 - spec.cash_buffer_pct)
        target_margin_capacity = spendable_cash + self._current_product_margin(
            spec.product
        )
        affordable_contracts = int(
            math.floor(target_margin_capacity / contract_margin + _EPSILON)
        )
        target_abs = min(capped_contracts, affordable_contracts)
        truncated = max(raw_contracts - target_abs, 0)
        return sign * target_abs, truncated

    def _plan_flat_future_after_selection_failure(
        self,
        signal_date: str,
        spec: FuturesOverlaySpec,
        counters: dict[str, float],
    ) -> FuturesRebalancePlan | None:
        has_position = any(
            symbol.upper().startswith(spec.product)
            for symbol in self.account.futures_positions
        )
        if not has_position:
            return None
        try:
            return self.event_loop.plan_futures_rebalance(
                FuturesRebalanceOrder(
                    signal_date,
                    spec.product,
                    0,
                    min_dte=spec.min_dte,
                )
            )
        except (CffexCatalogError, DerivativeEventError, ValueError) as exc:
            self._audit_failure(
                counters,
                signal_date=signal_date,
                execution_date=None,
                stage="plan_fail_closed_future",
                instrument="future",
                reason=exc,
                missing_catalog=isinstance(exc, CffexCatalogError),
            )
            return None

    def latch_close_signal(
        self,
        trade_date: str,
        nav: float,
        *,
        force_flat: bool,
    ) -> Mapping[str, float]:
        signal_date = _date_key(trade_date, "trade_date")
        checked_nav = _finite(nav, "nav")
        if not isinstance(force_flat, bool):
            raise ValueError("force_flat must be a bool")
        prior_input = self._latched_inputs.get(signal_date)
        if prior_input is not None:
            if prior_input != (checked_nav, force_flat):
                raise OverlayCoordinatorError(
                    "conflicting inputs for an idempotent close signal"
                )
            return dict(self._latch_counters[signal_date])

        counters = _counters()
        counters["derivative_signal_latches"] = 1.0
        if force_flat:
            counters["derivative_force_flat_signals"] = 1.0
        self._latched_inputs[signal_date] = (checked_nav, force_flat)

        try:
            execution_date = self.event_loop.catalog.next_trading_date(signal_date)
        except CffexCatalogError as exc:
            self._audit_failure(
                counters,
                signal_date=signal_date,
                execution_date=None,
                stage="plan_execution_date",
                instrument="all",
                reason=exc,
                missing_catalog=True,
            )
            self._latch_counters[signal_date] = counters
            return dict(counters)

        futures_target: int | None = None
        futures_plan: FuturesRebalancePlan | None = None
        spec = self.futures_spec
        if spec is not None:
            futures_period = _period_key(signal_date, spec.rebalance_frequency)
            periodic_due = futures_period not in self._futures_periods
            expiry_roll_due = self._futures_roll_due(spec, signal_date)
            futures_due = force_flat or periodic_due or expiry_roll_due
            if futures_due:
                if expiry_roll_due and not force_flat and not periodic_due:
                    counters["futures_expiry_roll_plans"] += 1.0
                direction = "flat" if force_flat else spec.direction_on(signal_date)
                selection: FuturesSelection | None = None
                if direction != "flat":
                    try:
                        selection = self.event_loop.catalog.select_future(
                            spec.product,
                            signal_date,
                            min_dte=spec.min_dte,
                        )
                        futures_target, truncated = self._futures_target(
                            spec,
                            selection,
                            checked_nav,
                            direction,
                        )
                        counters["futures_sizing_truncated_contracts"] += float(
                            truncated
                        )
                    except CffexCatalogError as exc:
                        self._audit_failure(
                            counters,
                            signal_date=signal_date,
                            execution_date=execution_date,
                            stage="plan_future_selection",
                            instrument="future",
                            reason=exc,
                            missing_catalog=True,
                        )
                        futures_target = 0
                        futures_plan = self._plan_flat_future_after_selection_failure(
                            signal_date,
                            spec,
                            counters,
                        )
                    except (CffexExecutionParameterError, CffexLookupError) as exc:
                        self._audit_failure(
                            counters,
                            signal_date=signal_date,
                            execution_date=execution_date,
                            stage="plan_future_execution_parameters",
                            instrument="future",
                            reason=exc,
                            missing_catalog=False,
                        )
                        futures_target = 0
                        futures_plan = self._plan_flat_future_after_selection_failure(
                            signal_date,
                            spec,
                            counters,
                        )
                else:
                    futures_target = 0

                if futures_plan is None and (
                    selection is not None or direction == "flat"
                ):
                    try:
                        futures_plan = self.event_loop.plan_futures_rebalance(
                            FuturesRebalanceOrder(
                                signal_date,
                                spec.product,
                                int(futures_target),
                                min_dte=spec.min_dte,
                            )
                        )
                    except CffexCatalogError as exc:
                        self._audit_failure(
                            counters,
                            signal_date=signal_date,
                            execution_date=execution_date,
                            stage="plan_future_rebalance",
                            instrument="future",
                            reason=exc,
                            missing_catalog=True,
                        )
                    except (DerivativeEventError, ValueError) as exc:
                        self._audit_failure(
                            counters,
                            signal_date=signal_date,
                            execution_date=execution_date,
                            stage="plan_future_rebalance",
                            instrument="future",
                            reason=exc,
                            missing_catalog=False,
                        )
                if futures_plan is not None:
                    counters["derivative_plans"] += 1.0
                    counters["futures_plans"] += 1.0
                    if not force_flat:
                        self._futures_periods.add(futures_period)

        option_close_plans: list[LongOptionClosePlan] = []
        option_purchase_plan: LongOptionPurchasePlan | None = None
        option_close_planning_succeeded = True
        option_spec = self.option_spec
        if option_spec is not None:
            option_period = _period_key(signal_date, "monthly")
            option_due = option_period not in self._option_periods
            matching_options = self._matching_option_symbols()
            near_expiry_options = tuple(
                symbol
                for symbol in matching_options
                if (
                    self.account.option_positions[symbol].expiry
                    - _as_date(signal_date)
                ).days
                <= option_spec.exit_dte
            )
            options_to_close = (
                matching_options if force_flat else near_expiry_options
            )
            buy_required = not force_flat and option_due

            if options_to_close:
                for symbol in options_to_close:
                    try:
                        close_plan = self.event_loop.plan_long_option_close(
                            LongOptionCloseOrder(signal_date, symbol)
                        )
                    except CffexCatalogError as exc:
                        option_close_planning_succeeded = False
                        self._audit_failure(
                            counters,
                            signal_date=signal_date,
                            execution_date=execution_date,
                            stage="plan_option_close",
                            instrument="option",
                            reason=exc,
                            missing_catalog=True,
                        )
                    except (DerivativeEventError, ValueError) as exc:
                        option_close_planning_succeeded = False
                        self._audit_failure(
                            counters,
                            signal_date=signal_date,
                            execution_date=execution_date,
                            stage="plan_option_close",
                            instrument="option",
                            reason=exc,
                            missing_catalog=False,
                        )
                    else:
                        option_close_plans.append(close_plan)
                        counters["derivative_plans"] += 1.0
                        counters["option_close_plans"] += 1.0

            if buy_required:
                cash_budget = max(checked_nav, 0.0) * option_spec.budget_pct_nav
                try:
                    option_purchase_plan = self.event_loop.plan_long_option_purchase(
                        LongOptionPurchaseOrder(
                            signal_date=signal_date,
                            product=option_spec.product,
                            option_type=option_spec.option_type,
                            target_abs_delta=option_spec.target_abs_delta,
                            min_dte=option_spec.min_dte,
                            max_dte=option_spec.max_dte,
                            cash_budget=cash_budget,
                        )
                    )
                except CffexCatalogError as exc:
                    self._audit_failure(
                        counters,
                        signal_date=signal_date,
                        execution_date=execution_date,
                        stage="plan_option_purchase",
                        instrument="option",
                        reason=exc,
                        missing_catalog=True,
                    )
                except (DerivativeEventError, ValueError) as exc:
                    self._audit_failure(
                        counters,
                        signal_date=signal_date,
                        execution_date=execution_date,
                        stage="plan_option_purchase",
                        instrument="option",
                        reason=exc,
                        missing_catalog=False,
                    )
                else:
                    counters["derivative_plans"] += 1.0
                    counters["option_buy_plans"] += 1.0
                    self._option_periods.add(option_period)

        pending = PendingOverlayPlan(
            signal_date=signal_date,
            execution_date=execution_date,
            force_flat=force_flat,
            futures_target_contracts=futures_target,
            futures_plan=futures_plan,
            option_close_plans=tuple(option_close_plans),
            option_purchase_plan=option_purchase_plan,
            option_close_planning_succeeded=option_close_planning_succeeded,
        )
        if (
            pending.futures_plan is not None
            or pending.option_close_plans
            or pending.option_purchase_plan is not None
        ):
            existing = self._pending.get(execution_date)
            if existing is not None and existing != pending:
                raise OverlayCoordinatorError(
                    f"conflicting pending plans for execution_date {execution_date}"
                )
            self._pending[execution_date] = pending

        self._latch_counters[signal_date] = counters
        return dict(counters)

    def _record_execution_events(
        self,
        counters: dict[str, float],
        events: tuple[ExecutionEvent, ...],
    ) -> None:
        for event in events:
            counters["derivative_execution_events"] += 1.0
            counters["derivative_order_requests"] += 1.0
            counters["derivative_requested_contracts"] += float(
                event.requested_contracts
            )
            counters["derivative_filled_contracts"] += float(event.filled_contracts)
            counters["derivative_clipped_contracts"] += float(
                event.clipped_contracts
            )
            counters["derivative_rejected_contracts"] += float(
                event.rejected_contracts
            )
            counters["derivative_fees"] += float(event.fees)
            status = event.status.value
            if status == "filled":
                counters["derivative_filled_orders"] += 1.0
            elif status == "clipped":
                counters["derivative_clipped_orders"] += 1.0
            elif status == "rejected":
                counters["derivative_rejected_orders"] += 1.0

            missing_catalog = (
                "missing_signal_date_volume" in event.reason
                or "missing_or_non_executable_next_open" in event.reason
            )
            if missing_catalog:
                counters["derivative_missing_catalog_events"] += 1.0
                self._audit.append(
                    OverlayAuditRecord(
                        signal_date=event.signal_date,
                        execution_date=event.execution_date,
                        stage="execute_order",
                        instrument=event.instrument,
                        reason=event.reason,
                    )
                )
            if "execution_parameter_unavailable" in event.reason:
                counters["derivative_missing_execution_parameter_events"] += 1.0
                self._audit.append(
                    OverlayAuditRecord(
                        signal_date=event.signal_date,
                        execution_date=event.execution_date,
                        stage="execute_order",
                        instrument=event.instrument,
                        reason=event.reason,
                    )
                )

    @staticmethod
    def _full_option_close(result: DerivativeExecutionResult) -> bool:
        if not result.events:
            return False
        return all(
            event.action == "close"
            and event.filled_contracts == event.requested_contracts
            and event.clipped_contracts == 0
            and event.rejected_contracts == 0
            and event.contracts_after == 0
            for event in result.events
        )

    def execute_reductions(self, trade_date: str) -> Mapping[str, float]:
        execution_date = _date_key(trade_date, "trade_date")
        cached = self._reduction_counters.get(execution_date)
        if cached is not None:
            return dict(cached)
        counters = _counters()
        try:
            margin_updates = self.event_loop.apply_start_of_day_parameters(
                execution_date
            )
        except (CffexExecutionParameterError, CffexLookupError) as exc:
            self._audit.append(
                OverlayAuditRecord(
                    signal_date=execution_date,
                    execution_date=execution_date,
                    stage="apply_start_of_day_parameters",
                    instrument="future",
                    reason=str(exc),
                )
            )
            raise MissingExecutionParameterError(
                f"exact causal official start-of-day parameter unavailable: {exc}"
            ) from exc
        counters["futures_margin_rate_updates"] = float(
            sum(not update.already_updated for update in margin_updates)
        )
        counters["futures_margin_transfer"] = float(
            sum(update.margin_transfer for update in margin_updates)
        )
        pending = self._pending.get(execution_date)
        if pending is None:
            self._reduction_counters[execution_date] = counters
            return dict(counters)

        counters["derivative_reduction_phases"] = 1.0
        futures_state: FuturesReductionPhaseState | None = None
        if pending.futures_plan is not None:
            futures_state = self.event_loop.execute_futures_reduction_phase(
                pending.futures_plan
            )
            self._record_execution_events(counters, futures_state.events)

        close_results: list[DerivativeExecutionResult] = []
        for close_plan in pending.option_close_plans:
            result = self.event_loop.execute_long_option_close(close_plan)
            close_results.append(result)
            self._record_execution_events(counters, result.events)
        closes_succeeded = pending.option_close_planning_succeeded and all(
            self._full_option_close(result) for result in close_results
        )
        bundle = _ReductionBundle(
            futures_state=futures_state,
            option_close_results=tuple(close_results),
            option_closes_succeeded=closes_succeeded,
        )
        self._reduction_bundles[execution_date] = bundle
        self._reduction_counters[execution_date] = counters
        return dict(counters)

    def execute_increases(self, trade_date: str) -> Mapping[str, float]:
        execution_date = _date_key(trade_date, "trade_date")
        cached = self._increase_counters.get(execution_date)
        if cached is not None:
            return dict(cached)
        pending = self._pending.get(execution_date)
        if pending is None:
            return _counters()

        counters = _counters()
        counters["derivative_increase_phases"] = 1.0
        reductions = self._reduction_bundles.get(execution_date)
        if reductions is None:
            blocked_orders = int(pending.futures_plan is not None) + int(
                pending.option_purchase_plan is not None
            )
            counters["derivative_rejected_orders"] += float(blocked_orders)
            if pending.option_purchase_plan is not None:
                counters["option_buys_blocked"] += 1.0
            self._audit.append(
                OverlayAuditRecord(
                    signal_date=pending.signal_date,
                    execution_date=execution_date,
                    stage="execute_increases",
                    instrument="all",
                    reason="increase phase called before reductions",
                )
            )
            self._increase_counters[execution_date] = counters
            return dict(counters)

        if pending.futures_plan is not None:
            if reductions.futures_state is None:
                raise RuntimeError("futures reduction state is missing")
            result = self.event_loop.execute_futures_increase_phase(
                pending.futures_plan,
                reductions.futures_state,
            )
            self._record_execution_events(counters, result.events)

        if pending.option_purchase_plan is not None:
            closes_are_dependency = bool(pending.option_close_plans) or not (
                pending.option_close_planning_succeeded
            )
            if closes_are_dependency and not reductions.option_closes_succeeded:
                counters["derivative_rejected_orders"] += 1.0
                counters["option_buys_blocked"] += 1.0
                self._audit.append(
                    OverlayAuditRecord(
                        signal_date=pending.signal_date,
                        execution_date=execution_date,
                        stage="execute_option_purchase",
                        instrument="option",
                        reason="option buy blocked by incomplete or failed close",
                    )
                )
            else:
                result = self.event_loop.execute_long_option_purchase(
                    pending.option_purchase_plan
                )
                self._record_execution_events(counters, result.events)

        self._completed_execution_dates.add(execution_date)
        self._increase_counters[execution_date] = counters
        return dict(counters)

    def settle_end_of_day(
        self,
        trade_date: str,
        stock_prices: Mapping[str, float],
    ) -> DerivativeEndOfDaySnapshot:
        valuation_date = _date_key(trade_date, "trade_date")
        try:
            result = self.event_loop.settle_end_of_day(
                valuation_date,
                stock_prices=stock_prices,
            )
        except (MissingSettlementMarkError, MissingExecutionParameterError) as exc:
            self._audit.append(
                OverlayAuditRecord(
                    signal_date=valuation_date,
                    execution_date=None,
                    stage="settle_end_of_day",
                    instrument="all",
                    reason=str(exc),
                )
            )
            raise
        future_expiries = sum(
            event.instrument == "future"
            and event.action == "expiry_cash_settlement"
            for event in result.events
        )
        option_expiries = sum(
            event.instrument == "option"
            and event.action == "expiry_cash_settlement"
            for event in result.events
        )
        if future_expiries and self.futures_spec is not None:
            self._futures_periods.discard(
                _period_key(valuation_date, self.futures_spec.rebalance_frequency)
            )
        return DerivativeEndOfDaySnapshot(
            nav=float(result.nav),
            margin_status=result.margin_status.value,
            margin_shortfall=float(result.margin.shortfall),
            settlement_fees=float(sum(event.fees for event in result.events)),
            futures_expiry_settlements=int(future_expiries),
            option_expiry_settlements=int(option_expiries),
        )


CffexOverlayCoordinator = Phase3OverlayCoordinator
CffexUnifiedAccountCoordinator = Phase3OverlayCoordinator
CffexFuturesOverlaySpec = FuturesOverlaySpec
CffexLongOptionOverlaySpec = LongOptionOverlaySpec
LongOptionConvexitySpec = LongOptionOverlaySpec


__all__ = [
    "CffexFuturesOverlaySpec",
    "CffexLongOptionOverlaySpec",
    "CffexOverlayCoordinator",
    "CffexUnifiedAccountCoordinator",
    "FuturesOverlaySpec",
    "LongOptionConvexitySpec",
    "LongOptionOverlaySpec",
    "OverlayAuditRecord",
    "OverlayCoordinatorError",
    "PendingOverlayPlan",
    "Phase3OverlayCoordinator",
]

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from numbers import Integral, Real
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

from quant_proof.cffex_catalog import CffexCatalog
from quant_proof.cffex_execution_parameters import CffexExecutionParameterSchedule
from quant_proof.derivative_event_loop import (
    DerivativeEventLoop,
    DevelopmentResearchAssumptions,
)
from quant_proof.engine.combined_account import CombinedAccount
from quant_proof.phase3_derivative_signals import (
    FUTURES_DIRECTION_KINDS,
    FUTURES_POSITION_MODES,
    FUTURES_TREND_VARIANTS,
    FuturesDirectionRule,
    build_futures_direction_map,
)
from quant_proof.phase3_overlay_coordinator import (
    FuturesOverlaySpec,
    LongOptionOverlaySpec,
    Phase3OverlayCoordinator,
)


OFFICIAL_EXPIRY_MODE = "official_asof_history"
OverlaySearchID = str
OverlayCoordinatorFactory = Callable[[CombinedAccount], Phase3OverlayCoordinator]

_STAGES = ("screen", "neighborhood", "stress")
_STAGE_ORDER = {stage: index for index, stage in enumerate(_STAGES)}
_COMPOSITIONS = frozenset(
    {"futures_only", "long_option_only", "futures_plus_long_option"}
)
_FUTURES_PRODUCTS = frozenset({"IF", "IH", "IC", "IM"})
_OPTION_PRODUCTS = frozenset({"IO", "HO", "MO"})
_FUTURES_DIRECTIONS = frozenset({"long", "short"})
_OPTION_TYPES = frozenset({"call", "put"})
_REBALANCE_FREQUENCIES = frozenset({"daily", "weekly", "monthly"})
_SIZING_FIELDS = (
    "fixed_contracts",
    "target_notional_multiple",
    "margin_budget",
)
_SHORT_OPTION_KEYS = frozenset(
    {"position_side", "side", "short", "short_options", "write", "write_options"}
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class Phase3OverlayFactoryError(ValueError):
    """Raised when a derivative factory cannot preserve official-data invariants."""


class OverlaySearchConfigError(ValueError):
    """Raised when an overlay search config is ambiguous or exceeds its budget."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OverlaySearchConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OverlaySearchConfigError(f"{name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise OverlaySearchConfigError(f"{name} keys must be strings")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise OverlaySearchConfigError(f"{name} must be a list")
    if not value:
        raise OverlaySearchConfigError(f"{name} must not be empty")
    return value


def _check_keys(
    raw: Mapping[str, Any],
    name: str,
    *,
    allowed: set[str] | frozenset[str],
    required: set[str] | frozenset[str] = frozenset(),
) -> None:
    missing = sorted(set(required) - set(raw))
    unknown = sorted(set(raw) - set(allowed))
    if missing:
        raise OverlaySearchConfigError(f"{name} is missing keys: {', '.join(missing)}")
    if unknown:
        raise OverlaySearchConfigError(f"{name} has unknown keys: {', '.join(unknown)}")


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise OverlaySearchConfigError(f"{name} must be boolean")
    return value


def _whole(value: object, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise OverlaySearchConfigError(f"{name} must be a whole number")
    converted = int(value)
    minimum = 0 if allow_zero else 1
    if converted < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise OverlaySearchConfigError(f"{name} must be {qualifier}")
    return converted


def _finite(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strict_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise OverlaySearchConfigError(f"{name} must be finite")
    converted = float(value)
    if not math.isfinite(converted):
        raise OverlaySearchConfigError(f"{name} must be finite")
    if minimum is not None:
        invalid = converted <= minimum if strict_minimum else converted < minimum
        if invalid:
            comparator = "greater than" if strict_minimum else "at least"
            raise OverlaySearchConfigError(f"{name} must be {comparator} {minimum}")
    if maximum is not None and converted > maximum:
        raise OverlaySearchConfigError(f"{name} must not exceed {maximum}")
    return converted


def _choice(value: object, name: str, allowed: frozenset[str], *, upper: bool = False) -> str:
    normalized = _required_text(value, name)
    normalized = normalized.upper() if upper else normalized.lower()
    if normalized not in allowed:
        raise OverlaySearchConfigError(
            f"{name} must be one of: {', '.join(sorted(allowed))}"
        )
    return normalized


def _grid_values(
    value: object,
    name: str,
    parser: Callable[[object, str], object],
) -> tuple[object, ...]:
    parsed = tuple(parser(item, f"{name}[]") for item in _sequence(value, name))
    keys = tuple(_canonical_json(item) for item in parsed)
    if len(keys) != len(set(keys)):
        raise OverlaySearchConfigError(f"{name} contains duplicate values")
    return tuple(item for _, item in sorted(zip(keys, parsed), key=lambda pair: pair[0]))


def _futures_payload(spec: FuturesOverlaySpec) -> dict[str, object]:
    return {
        "product": spec.product,
        "direction": spec.direction,
        "direction_by_signal_date": None,
        "fixed_contracts": spec.fixed_contracts,
        "target_notional_multiple": spec.target_notional_multiple,
        "margin_budget": spec.margin_budget,
        "min_dte": spec.min_dte,
        "rebalance_frequency": spec.rebalance_frequency,
        "cash_buffer_pct": spec.cash_buffer_pct,
        "max_contracts": spec.max_contracts,
    }


def _direction_rule_payload(
    rule: FuturesDirectionRule | None,
) -> dict[str, object] | None:
    return None if rule is None else rule.to_compact_dict()


def _option_payload(spec: LongOptionOverlaySpec) -> dict[str, object]:
    return {
        "product": spec.product,
        "option_type": spec.option_type,
        "target_abs_delta": spec.target_abs_delta,
        "min_dte": spec.min_dte,
        "max_dte": spec.max_dte,
        "premium_budget_pct_nav": spec.budget_pct_nav,
        "exit_dte": spec.exit_dte,
        "position_side": "long",
    }


@dataclass(frozen=True)
class OverlaySearchSpec:
    """One immutable overlay candidate with a semantic, content-addressed ID."""

    stage: str
    composition: str
    futures: FuturesOverlaySpec | None = None
    long_option: LongOptionOverlaySpec | None = None
    direction_rule: FuturesDirectionRule | None = None
    source_label: str = field(default="", compare=False)
    overlay_id: OverlaySearchID = field(init=False)

    def __post_init__(self) -> None:
        stage = str(self.stage).strip().lower()
        composition = str(self.composition).strip().lower()
        if stage not in _STAGE_ORDER:
            raise ValueError(f"stage must be one of: {', '.join(_STAGES)}")
        if composition not in _COMPOSITIONS:
            raise ValueError(
                "composition must be futures_only, long_option_only, or "
                "futures_plus_long_option"
            )
        if self.futures is not None and not isinstance(self.futures, FuturesOverlaySpec):
            raise ValueError("futures must be a FuturesOverlaySpec")
        if self.long_option is not None and not isinstance(
            self.long_option, LongOptionOverlaySpec
        ):
            raise ValueError("long_option must be a LongOptionOverlaySpec")
        if self.direction_rule is not None and not isinstance(
            self.direction_rule,
            FuturesDirectionRule,
        ):
            raise ValueError("direction_rule must be a FuturesDirectionRule")
        if self.direction_rule is not None and self.futures is None:
            raise ValueError("direction_rule requires a futures leg")
        if self.futures is not None and self.futures.direction_by_signal_date is not None:
            raise ValueError(
                "OverlaySearchSpec must bind a compact direction_rule, not a resolved "
                "direction_by_signal_date map"
            )
        expected_legs = {
            "futures_only": (True, False),
            "long_option_only": (False, True),
            "futures_plus_long_option": (True, True),
        }[composition]
        actual_legs = (self.futures is not None, self.long_option is not None)
        if actual_legs != expected_legs:
            raise ValueError(f"{composition} has inconsistent derivative legs")
        source_label = str(self.source_label).strip()
        payload = {
            "composition": composition,
            "futures": None if self.futures is None else _futures_payload(self.futures),
            "long_option": (
                None if self.long_option is None else _option_payload(self.long_option)
            ),
        }
        if self.direction_rule is not None:
            payload["direction_rule"] = _direction_rule_payload(self.direction_rule)
        digest = hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()[:20]
        prefix = {
            "futures_only": "fut",
            "long_option_only": "opt",
            "futures_plus_long_option": "combo",
        }[composition]
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "composition", composition)
        object.__setattr__(self, "source_label", source_label)
        object.__setattr__(self, "overlay_id", f"p3ovl-{prefix}-{digest}")

    @property
    def id(self) -> OverlaySearchID:
        return self.overlay_id

    @property
    def futures_spec(self) -> FuturesOverlaySpec | None:
        return self.futures

    @property
    def option_spec(self) -> LongOptionOverlaySpec | None:
        return self.long_option

    def to_audit_dict(self) -> dict[str, object]:
        return {
            "overlay_id": self.overlay_id,
            "stage": self.stage,
            "composition": self.composition,
            "source_label": self.source_label,
            "futures": None if self.futures is None else _futures_payload(self.futures),
            "direction_rule": _direction_rule_payload(self.direction_rule),
            "long_option": (
                None if self.long_option is None else _option_payload(self.long_option)
            ),
        }


@dataclass(frozen=True)
class OverlayStageBudget:
    stage: str
    max_combinations: int
    max_windows: int
    max_window_evaluations: int
    max_promotions: int
    futures_slippage_bps: float
    option_slippage_bps: float
    prior_day_volume_participation: float

    def __post_init__(self) -> None:
        stage = str(self.stage).strip().lower()
        if stage not in _STAGE_ORDER:
            raise ValueError(f"unknown overlay search stage: {stage}")
        object.__setattr__(self, "stage", stage)
        object.__setattr__(
            self,
            "max_combinations",
            _whole(self.max_combinations, "max_combinations"),
        )
        object.__setattr__(self, "max_windows", _whole(self.max_windows, "max_windows"))
        object.__setattr__(
            self,
            "max_window_evaluations",
            _whole(self.max_window_evaluations, "max_window_evaluations"),
        )
        object.__setattr__(
            self,
            "max_promotions",
            _whole(self.max_promotions, "max_promotions", allow_zero=True),
        )
        if self.max_promotions > self.max_combinations:
            raise ValueError("max_promotions cannot exceed max_combinations")
        object.__setattr__(
            self,
            "futures_slippage_bps",
            _finite(
                self.futures_slippage_bps,
                "futures_slippage_bps",
                minimum=0.0,
                strict_minimum=True,
            ),
        )
        object.__setattr__(
            self,
            "option_slippage_bps",
            _finite(
                self.option_slippage_bps,
                "option_slippage_bps",
                minimum=0.0,
                strict_minimum=True,
            ),
        )
        object.__setattr__(
            self,
            "prior_day_volume_participation",
            _finite(
                self.prior_day_volume_participation,
                "prior_day_volume_participation",
                minimum=0.0,
                maximum=1.0,
                strict_minimum=True,
            ),
        )


@dataclass(frozen=True)
class OverlaySearchSpace:
    search_id: str
    config_sha256: str
    max_total_combinations: int
    stage_budgets: tuple[OverlayStageBudget, ...]
    specs: tuple[OverlaySearchSpec, ...]
    stock_strategy_binding: str = "none"

    def __post_init__(self) -> None:
        search_id = str(self.search_id).strip()
        if not search_id or _SAFE_ID.fullmatch(search_id) is None:
            raise ValueError("search_id must be a filesystem-safe identifier")
        if not re.fullmatch(r"[0-9a-f]{64}", self.config_sha256):
            raise ValueError("config_sha256 must be a lowercase SHA-256 digest")
        if self.stock_strategy_binding != "none":
            raise ValueError("derivative overlay search cannot multiply stock strategies")
        if tuple(budget.stage for budget in self.stage_budgets) != _STAGES:
            raise ValueError("stage_budgets must contain screen, neighborhood, stress in order")
        maximum = _whole(
            self.max_total_combinations,
            "max_total_combinations",
        )
        if len(self.specs) > maximum:
            raise ValueError("expanded overlay search exceeds max_total_combinations")
        budgets = {budget.stage: budget for budget in self.stage_budgets}
        for stage in _STAGES:
            stage_specs = tuple(spec for spec in self.specs if spec.stage == stage)
            if not stage_specs:
                raise ValueError(f"overlay search stage {stage} is empty")
            ids = tuple(spec.overlay_id for spec in stage_specs)
            if len(ids) != len(set(ids)):
                raise ValueError(f"overlay search stage {stage} contains duplicate candidates")
            budget = budgets[stage]
            if len(stage_specs) > budget.max_combinations:
                raise ValueError(f"overlay search stage {stage} exceeds max_combinations")
            if len(stage_specs) * budget.max_windows > budget.max_window_evaluations:
                raise ValueError(f"overlay search stage {stage} exceeds its evaluation budget")
            if budget.max_promotions > len(stage_specs):
                raise ValueError(
                    f"overlay search stage {stage} promotes more candidates than it has"
                )
        object.__setattr__(self, "search_id", search_id)
        object.__setattr__(self, "max_total_combinations", maximum)

    def specs_for_stage(self, stage: str) -> tuple[OverlaySearchSpec, ...]:
        normalized = str(stage).strip().lower()
        if normalized not in _STAGE_ORDER:
            raise ValueError(f"unknown overlay search stage: {stage}")
        return tuple(spec for spec in self.specs if spec.stage == normalized)

    def budget_for_stage(self, stage: str) -> OverlayStageBudget:
        normalized = str(stage).strip().lower()
        for budget in self.stage_budgets:
            if budget.stage == normalized:
                return budget
        raise ValueError(f"unknown overlay search stage: {stage}")

    def to_audit_manifest(self) -> dict[str, object]:
        return {
            "search_id": self.search_id,
            "config_sha256": self.config_sha256,
            "stock_strategy_binding": self.stock_strategy_binding,
            "max_total_combinations": self.max_total_combinations,
            "stages": [
                {
                    "stage": budget.stage,
                    "max_combinations": budget.max_combinations,
                    "max_windows": budget.max_windows,
                    "max_window_evaluations": budget.max_window_evaluations,
                    "max_promotions": budget.max_promotions,
                    "futures_slippage_bps": budget.futures_slippage_bps,
                    "option_slippage_bps": budget.option_slippage_bps,
                    "prior_day_volume_participation": budget.prior_day_volume_participation,
                    "expanded_combinations": len(self.specs_for_stage(budget.stage)),
                }
                for budget in self.stage_budgets
            ],
            "specs": [spec.to_audit_dict() for spec in self.specs],
        }


def _parse_direction_rule(value: object, name: str) -> FuturesDirectionRule:
    raw = _mapping(value, name)
    allowed = {
        "kind",
        "position_mode",
        "lookback_days",
        "trend_variant",
        "fast_window",
        "slow_window",
        "neutral_band",
    }
    _check_keys(
        raw,
        name,
        allowed=allowed,
        required={"kind", "position_mode"},
    )
    kwargs: dict[str, object] = {
        "kind": _choice(raw["kind"], f"{name}.kind", FUTURES_DIRECTION_KINDS),
        "position_mode": _choice(
            raw["position_mode"],
            f"{name}.position_mode",
            FUTURES_POSITION_MODES,
        ),
        "neutral_band": _finite(
            raw.get("neutral_band", 0.0),
            f"{name}.neutral_band",
            minimum=0.0,
        ),
    }
    for field_name in ("lookback_days", "fast_window", "slow_window"):
        if field_name in raw:
            kwargs[field_name] = _whole(raw[field_name], f"{name}.{field_name}")
    if "trend_variant" in raw:
        kwargs["trend_variant"] = _choice(
            raw["trend_variant"],
            f"{name}.trend_variant",
            FUTURES_TREND_VARIANTS,
        )
    try:
        return FuturesDirectionRule(**kwargs)
    except ValueError as exc:
        raise OverlaySearchConfigError(f"{name}: {exc}") from exc


def _parse_direction_rule_grid(
    value: object,
    name: str,
) -> tuple[FuturesDirectionRule, ...]:
    parsed = tuple(
        _parse_direction_rule(item, f"{name}[{index}]")
        for index, item in enumerate(_sequence(value, name))
    )
    keyed = tuple(
        (_canonical_json(rule.to_compact_dict()), rule) for rule in parsed
    )
    keys = tuple(key for key, _ in keyed)
    if len(keys) != len(set(keys)):
        raise OverlaySearchConfigError(f"{name} contains duplicate rules")
    return tuple(rule for _, rule in sorted(keyed, key=lambda pair: pair[0]))


def _parse_futures_record(
    value: object,
    name: str,
) -> tuple[FuturesOverlaySpec, FuturesDirectionRule | None]:
    raw = _mapping(value, name)
    allowed = {
        "product",
        "direction",
        "direction_rule",
        "fixed_contracts",
        "target_notional_multiple",
        "margin_budget",
        "min_dte",
        "rebalance_frequency",
        "cash_buffer_pct",
        "max_contracts",
    }
    _check_keys(raw, name, allowed=allowed, required={"product"})
    direction_fields = [
        field_name
        for field_name in ("direction", "direction_rule")
        if field_name in raw
    ]
    if len(direction_fields) != 1:
        raise OverlaySearchConfigError(
            f"{name} must provide exactly one direction or direction_rule"
        )
    sizing = [field_name for field_name in _SIZING_FIELDS if field_name in raw]
    if len(sizing) != 1:
        raise OverlaySearchConfigError(
            f"{name} must provide exactly one futures sizing mode: "
            + ", ".join(_SIZING_FIELDS)
        )
    product = _choice(raw["product"], f"{name}.product", _FUTURES_PRODUCTS, upper=True)
    direction_rule = (
        _parse_direction_rule(raw["direction_rule"], f"{name}.direction_rule")
        if "direction_rule" in raw
        else None
    )
    direction = (
        "flat"
        if direction_rule is not None
        else _choice(raw["direction"], f"{name}.direction", _FUTURES_DIRECTIONS)
    )
    sizing_name = sizing[0]
    if sizing_name == "fixed_contracts":
        sizing_value: int | float = _whole(raw[sizing_name], f"{name}.{sizing_name}")
    else:
        sizing_value = _finite(
            raw[sizing_name],
            f"{name}.{sizing_name}",
            minimum=0.0,
            strict_minimum=True,
        )
    kwargs: dict[str, object] = {sizing_name: sizing_value}
    max_contracts = raw.get("max_contracts")
    if max_contracts is not None:
        max_contracts = _whole(max_contracts, f"{name}.max_contracts")
    kwargs.update(
        product=product,
        direction=direction,
        min_dte=_whole(raw.get("min_dte", 5), f"{name}.min_dte", allow_zero=True),
        rebalance_frequency=_choice(
            raw.get("rebalance_frequency", "monthly"),
            f"{name}.rebalance_frequency",
            _REBALANCE_FREQUENCIES,
        ),
        cash_buffer_pct=_finite(
            raw.get("cash_buffer_pct", 0.0),
            f"{name}.cash_buffer_pct",
            minimum=0.0,
            maximum=1.0,
        ),
        max_contracts=max_contracts,
    )
    return FuturesOverlaySpec(**kwargs), direction_rule


def _reject_short_option_fields(raw: Mapping[str, Any], name: str) -> None:
    present = sorted(set(raw) & _SHORT_OPTION_KEYS)
    if present:
        raise OverlaySearchConfigError(
            f"short options are forbidden; remove {', '.join(present)} from {name}"
        )


def _premium_budget(raw: Mapping[str, Any], name: str) -> float:
    aliases = (
        "premium_budget_pct",
        "premium_budget_pct_nav",
        "budget_pct_nav",
    )
    supplied = [alias for alias in aliases if alias in raw]
    if len(supplied) != 1:
        raise OverlaySearchConfigError(
            f"{name} must provide exactly one long-option premium budget percentage"
        )
    return _finite(
        raw[supplied[0]],
        f"{name}.{supplied[0]}",
        minimum=0.0,
        maximum=1.0,
        strict_minimum=True,
    )


def _premium_budget_grid(
    raw: Mapping[str, Any],
    name: str,
) -> tuple[object, ...]:
    aliases = (
        "premium_budget_pct",
        "premium_budget_pct_nav",
        "budget_pct_nav",
    )
    supplied = [alias for alias in aliases if alias in raw]
    if len(supplied) != 1:
        raise OverlaySearchConfigError(
            f"{name} must provide exactly one long-option premium budget grid"
        )
    alias = supplied[0]
    return _grid_values(
        raw[alias],
        f"{name}.{alias}",
        lambda item, item_name: _finite(
            item,
            item_name,
            minimum=0.0,
            maximum=1.0,
            strict_minimum=True,
        ),
    )


def _parse_option_record(value: object, name: str) -> LongOptionOverlaySpec:
    raw = _mapping(value, name)
    _reject_short_option_fields(raw, name)
    allowed = {
        "product",
        "option_type",
        "target_abs_delta",
        "min_dte",
        "max_dte",
        "premium_budget_pct",
        "premium_budget_pct_nav",
        "budget_pct_nav",
        "exit_dte",
    }
    _check_keys(
        raw,
        name,
        allowed=allowed,
        required={"product", "option_type", "target_abs_delta", "min_dte", "max_dte", "exit_dte"},
    )
    minimum = _whole(raw["min_dte"], f"{name}.min_dte", allow_zero=True)
    maximum = _whole(raw["max_dte"], f"{name}.max_dte", allow_zero=True)
    exit_dte = _whole(raw["exit_dte"], f"{name}.exit_dte", allow_zero=True)
    if minimum > maximum:
        raise OverlaySearchConfigError(f"{name}.min_dte must not exceed max_dte")
    if exit_dte >= minimum:
        raise OverlaySearchConfigError(f"{name}.exit_dte must be below min_dte")
    return LongOptionOverlaySpec(
        product=_choice(raw["product"], f"{name}.product", _OPTION_PRODUCTS, upper=True),
        option_type=_choice(raw["option_type"], f"{name}.option_type", _OPTION_TYPES),
        target_abs_delta=_finite(
            raw["target_abs_delta"],
            f"{name}.target_abs_delta",
            minimum=0.0,
            maximum=1.0,
            strict_minimum=True,
        ),
        min_dte=minimum,
        max_dte=maximum,
        budget_pct_nav=_premium_budget(raw, name),
        exit_dte=exit_dte,
    )


def _expand_futures_grid(
    value: object,
    name: str,
    *,
    hard_max: int,
) -> tuple[tuple[FuturesOverlaySpec, FuturesDirectionRule | None], ...]:
    raw = _mapping(value, name)
    allowed = {
        "max_combinations",
        "products",
        "directions",
        "direction_rules",
        "sizing",
        "cash_buffer_pct",
        "min_dte",
        "rebalance_frequency",
        "max_contracts",
    }
    _check_keys(
        raw,
        name,
        allowed=allowed,
        required={
            "max_combinations",
            "products",
            "sizing",
            "cash_buffer_pct",
            "min_dte",
            "rebalance_frequency",
        },
    )
    local_max = _whole(raw["max_combinations"], f"{name}.max_combinations")
    direction_fields = [
        field_name
        for field_name in ("directions", "direction_rules")
        if field_name in raw
    ]
    if len(direction_fields) != 1:
        raise OverlaySearchConfigError(
            f"{name} must provide exactly one directions or direction_rules grid"
        )
    products = _grid_values(
        raw["products"],
        f"{name}.products",
        lambda item, item_name: _choice(item, item_name, _FUTURES_PRODUCTS, upper=True),
    )
    direction_variants: tuple[tuple[str, FuturesDirectionRule | None], ...]
    if "directions" in raw:
        directions = _grid_values(
            raw["directions"],
            f"{name}.directions",
            lambda item, item_name: _choice(item, item_name, _FUTURES_DIRECTIONS),
        )
        direction_variants = tuple((str(direction), None) for direction in directions)
    else:
        rules = _parse_direction_rule_grid(
            raw["direction_rules"],
            f"{name}.direction_rules",
        )
        direction_variants = tuple(("flat", rule) for rule in rules)
    buffers = _grid_values(
        raw["cash_buffer_pct"],
        f"{name}.cash_buffer_pct",
        lambda item, item_name: _finite(item, item_name, minimum=0.0, maximum=1.0),
    )
    minimum_dtes = _grid_values(
        raw["min_dte"],
        f"{name}.min_dte",
        lambda item, item_name: _whole(item, item_name, allow_zero=True),
    )
    frequencies = _grid_values(
        raw["rebalance_frequency"],
        f"{name}.rebalance_frequency",
        lambda item, item_name: _choice(item, item_name, _REBALANCE_FREQUENCIES),
    )
    max_contract_values: tuple[object, ...]
    if "max_contracts" not in raw:
        max_contract_values = (None,)
    else:
        max_contract_values = _grid_values(
            raw["max_contracts"],
            f"{name}.max_contracts",
            lambda item, item_name: (
                None if item is None else _whole(item, item_name)
            ),
        )

    sizing_raw = _mapping(raw["sizing"], f"{name}.sizing")
    _check_keys(sizing_raw, f"{name}.sizing", allowed=set(_SIZING_FIELDS))
    if not sizing_raw:
        raise OverlaySearchConfigError(f"{name}.sizing must not be empty")
    sizing: list[tuple[str, int | float]] = []
    for sizing_name in _SIZING_FIELDS:
        if sizing_name not in sizing_raw:
            continue
        parser: Callable[[object, str], object]
        if sizing_name == "fixed_contracts":
            parser = lambda item, item_name: _whole(item, item_name)
        else:
            parser = lambda item, item_name: _finite(
                item, item_name, minimum=0.0, strict_minimum=True
            )
        values = _grid_values(
            sizing_raw[sizing_name],
            f"{name}.sizing.{sizing_name}",
            parser,
        )
        sizing.extend((sizing_name, value) for value in values)

    estimated = (
        len(products)
        * len(direction_variants)
        * len(sizing)
        * len(buffers)
        * len(minimum_dtes)
        * len(frequencies)
        * len(max_contract_values)
    )
    if estimated > local_max:
        raise OverlaySearchConfigError(
            f"{name} expands to {estimated} combinations, above max_combinations={local_max}"
        )
    if estimated > hard_max:
        raise OverlaySearchConfigError(
            f"{name} expands to {estimated} combinations, above remaining stage budget={hard_max}"
        )

    specs: list[tuple[FuturesOverlaySpec, FuturesDirectionRule | None]] = []
    for product in products:
        for direction, direction_rule in direction_variants:
            for sizing_name, sizing_value in sizing:
                for buffer in buffers:
                    for min_dte in minimum_dtes:
                        for frequency in frequencies:
                            for max_contracts in max_contract_values:
                                record: dict[str, object] = {
                                    "product": product,
                                    "direction": direction,
                                    sizing_name: sizing_value,
                                    "cash_buffer_pct": buffer,
                                    "min_dte": min_dte,
                                    "rebalance_frequency": frequency,
                                    "max_contracts": max_contracts,
                                }
                                specs.append(
                                    (FuturesOverlaySpec(**record), direction_rule)
                                )
    return tuple(specs)


def _parse_dte_window(value: object, name: str) -> tuple[int, int]:
    raw = _mapping(value, name)
    _check_keys(raw, name, allowed={"min_dte", "max_dte"}, required={"min_dte", "max_dte"})
    minimum = _whole(raw["min_dte"], f"{name}.min_dte", allow_zero=True)
    maximum = _whole(raw["max_dte"], f"{name}.max_dte", allow_zero=True)
    if minimum > maximum:
        raise OverlaySearchConfigError(f"{name}.min_dte must not exceed max_dte")
    return minimum, maximum


def _expand_option_grid(
    value: object,
    name: str,
    *,
    hard_max: int,
) -> tuple[LongOptionOverlaySpec, ...]:
    raw = _mapping(value, name)
    _reject_short_option_fields(raw, name)
    allowed = {
        "max_combinations",
        "products",
        "option_types",
        "premium_budget_pct",
        "premium_budget_pct_nav",
        "budget_pct_nav",
        "target_abs_delta",
        "dte_windows",
        "exit_dte",
    }
    _check_keys(
        raw,
        name,
        allowed=allowed,
        required={
            "max_combinations",
            "products",
            "option_types",
            "target_abs_delta",
            "dte_windows",
            "exit_dte",
        },
    )
    local_max = _whole(raw["max_combinations"], f"{name}.max_combinations")
    products = _grid_values(
        raw["products"],
        f"{name}.products",
        lambda item, item_name: _choice(item, item_name, _OPTION_PRODUCTS, upper=True),
    )
    option_types = _grid_values(
        raw["option_types"],
        f"{name}.option_types",
        lambda item, item_name: _choice(item, item_name, _OPTION_TYPES),
    )
    budgets = _premium_budget_grid(raw, name)
    deltas = _grid_values(
        raw["target_abs_delta"],
        f"{name}.target_abs_delta",
        lambda item, item_name: _finite(
            item,
            item_name,
            minimum=0.0,
            maximum=1.0,
            strict_minimum=True,
        ),
    )
    dte_windows = _grid_values(
        raw["dte_windows"],
        f"{name}.dte_windows",
        _parse_dte_window,
    )
    exit_dtes = _grid_values(
        raw["exit_dte"],
        f"{name}.exit_dte",
        lambda item, item_name: _whole(item, item_name, allow_zero=True),
    )
    estimated = (
        len(products)
        * len(option_types)
        * len(budgets)
        * len(deltas)
        * len(dte_windows)
        * len(exit_dtes)
    )
    if estimated > local_max:
        raise OverlaySearchConfigError(
            f"{name} expands to {estimated} combinations, above max_combinations={local_max}"
        )
    if estimated > hard_max:
        raise OverlaySearchConfigError(
            f"{name} expands to {estimated} combinations, above remaining stage budget={hard_max}"
        )

    specs: list[LongOptionOverlaySpec] = []
    for product in products:
        for option_type in option_types:
            for budget in budgets:
                for delta in deltas:
                    for min_dte, max_dte in dte_windows:
                        for exit_dte in exit_dtes:
                            if exit_dte >= min_dte:
                                raise OverlaySearchConfigError(
                                    f"{name}.exit_dte must be below every min_dte"
                                )
                            specs.append(
                                LongOptionOverlaySpec(
                                    product=product,
                                    option_type=option_type,
                                    target_abs_delta=delta,
                                    min_dte=min_dte,
                                    max_dte=max_dte,
                                    budget_pct_nav=budget,
                                    exit_dte=exit_dte,
                                )
                            )
    return tuple(specs)


def _expand_stage(
    stage: str,
    value: object,
) -> tuple[OverlayStageBudget, tuple[OverlaySearchSpec, ...]]:
    name = f"stages.{stage}"
    raw = _mapping(value, name)
    _check_keys(
        raw,
        name,
        allowed={"max_combinations", "budget", "overlay_only", "futures_plus_long_option"},
        required={"max_combinations", "budget", "overlay_only", "futures_plus_long_option"},
    )
    max_combinations = _whole(raw["max_combinations"], f"{name}.max_combinations")
    budget_raw = _mapping(raw["budget"], f"{name}.budget")
    _check_keys(
        budget_raw,
        f"{name}.budget",
        allowed={
            "max_windows",
            "max_window_evaluations",
            "max_promotions",
            "futures_slippage_bps",
            "option_slippage_bps",
            "prior_day_volume_participation",
        },
        required={
            "max_windows",
            "max_window_evaluations",
            "max_promotions",
            "futures_slippage_bps",
            "option_slippage_bps",
            "prior_day_volume_participation",
        },
    )
    budget = OverlayStageBudget(
        stage=stage,
        max_combinations=max_combinations,
        max_windows=_whole(budget_raw["max_windows"], f"{name}.budget.max_windows"),
        max_window_evaluations=_whole(
            budget_raw["max_window_evaluations"],
            f"{name}.budget.max_window_evaluations",
        ),
        max_promotions=_whole(
            budget_raw["max_promotions"],
            f"{name}.budget.max_promotions",
            allow_zero=True,
        ),
        futures_slippage_bps=_finite(
            budget_raw["futures_slippage_bps"],
            f"{name}.budget.futures_slippage_bps",
            minimum=0.0,
            strict_minimum=True,
        ),
        option_slippage_bps=_finite(
            budget_raw["option_slippage_bps"],
            f"{name}.budget.option_slippage_bps",
            minimum=0.0,
            strict_minimum=True,
        ),
        prior_day_volume_participation=_finite(
            budget_raw["prior_day_volume_participation"],
            f"{name}.budget.prior_day_volume_participation",
            minimum=0.0,
            maximum=1.0,
            strict_minimum=True,
        ),
    )

    specs: list[OverlaySearchSpec] = []
    overlay_only = _mapping(raw["overlay_only"], f"{name}.overlay_only")
    _check_keys(
        overlay_only,
        f"{name}.overlay_only",
        allowed={"enabled", "futures", "long_options"},
        required={"enabled"},
    )
    overlay_enabled = _strict_bool(
        overlay_only["enabled"], f"{name}.overlay_only.enabled"
    )
    if overlay_enabled:
        if "futures" not in overlay_only and "long_options" not in overlay_only:
            raise OverlaySearchConfigError(
                f"{name}.overlay_only must configure futures or long_options"
            )
        if "futures" in overlay_only:
            futures_specs = _expand_futures_grid(
                overlay_only["futures"],
                f"{name}.overlay_only.futures",
                hard_max=max_combinations - len(specs),
            )
            specs.extend(
                OverlaySearchSpec(
                    stage=stage,
                    composition="futures_only",
                    futures=futures,
                    direction_rule=direction_rule,
                    source_label=f"{name}.overlay_only.futures",
                )
                for futures, direction_rule in futures_specs
            )
        if "long_options" in overlay_only:
            option_specs = _expand_option_grid(
                overlay_only["long_options"],
                f"{name}.overlay_only.long_options",
                hard_max=max_combinations - len(specs),
            )
            specs.extend(
                OverlaySearchSpec(
                    stage=stage,
                    composition="long_option_only",
                    long_option=option,
                    source_label=f"{name}.overlay_only.long_options",
                )
                for option in option_specs
            )
    elif len(overlay_only) != 1:
        raise OverlaySearchConfigError(
            f"{name}.overlay_only cannot contain grids when disabled"
        )

    combos = _mapping(
        raw["futures_plus_long_option"], f"{name}.futures_plus_long_option"
    )
    _check_keys(
        combos,
        f"{name}.futures_plus_long_option",
        allowed={"enabled", "max_combinations", "pairs"},
        required={"enabled"},
    )
    combo_enabled = _strict_bool(
        combos["enabled"], f"{name}.futures_plus_long_option.enabled"
    )
    if combo_enabled:
        _check_keys(
            combos,
            f"{name}.futures_plus_long_option",
            allowed={"enabled", "max_combinations", "pairs"},
            required={"enabled", "max_combinations", "pairs"},
        )
        combo_max = _whole(
            combos["max_combinations"],
            f"{name}.futures_plus_long_option.max_combinations",
        )
        pairs = _sequence(combos["pairs"], f"{name}.futures_plus_long_option.pairs")
        if len(pairs) > combo_max:
            raise OverlaySearchConfigError(
                f"{name}.futures_plus_long_option has {len(pairs)} explicit pairs, "
                f"above max_combinations={combo_max}"
            )
        if len(pairs) > max_combinations - len(specs):
            raise OverlaySearchConfigError(
                f"{name} explicit pairs exceed max_combinations={max_combinations}"
            )
        labels: set[str] = set()
        for index, pair_value in enumerate(pairs):
            pair_name = f"{name}.futures_plus_long_option.pairs[{index}]"
            pair = _mapping(pair_value, pair_name)
            _check_keys(
                pair,
                pair_name,
                allowed={"label", "futures", "long_option"},
                required={"label", "futures", "long_option"},
            )
            label = _required_text(pair["label"], f"{pair_name}.label")
            if label in labels:
                raise OverlaySearchConfigError(f"{pair_name}.label is duplicated: {label}")
            labels.add(label)
            futures, direction_rule = _parse_futures_record(
                pair["futures"],
                f"{pair_name}.futures",
            )
            specs.append(
                OverlaySearchSpec(
                    stage=stage,
                    composition="futures_plus_long_option",
                    futures=futures,
                    long_option=_parse_option_record(
                        pair["long_option"], f"{pair_name}.long_option"
                    ),
                    direction_rule=direction_rule,
                    source_label=label,
                )
            )
    elif len(combos) != 1:
        raise OverlaySearchConfigError(
            f"{name}.futures_plus_long_option cannot contain pairs when disabled"
        )

    specs.sort(key=lambda spec: (spec.composition, spec.overlay_id))
    ids = tuple(spec.overlay_id for spec in specs)
    if len(ids) != len(set(ids)):
        raise OverlaySearchConfigError(f"{name} contains duplicate overlay candidates")
    if len(specs) > max_combinations:
        raise OverlaySearchConfigError(
            f"{name} expands to {len(specs)} combinations, "
            f"above max_combinations={max_combinations}"
        )
    if len(specs) * budget.max_windows > budget.max_window_evaluations:
        raise OverlaySearchConfigError(
            f"{name} requires {len(specs) * budget.max_windows} window evaluations, "
            f"above budget={budget.max_window_evaluations}"
        )
    if budget.max_promotions > len(specs):
        raise OverlaySearchConfigError(
            f"{name}.budget.max_promotions exceeds expanded candidates"
        )
    return budget, tuple(specs)


def load_overlay_search_space(path: str | Path) -> OverlaySearchSpace:
    """Safely load, validate, and deterministically expand a Phase 3 overlay YAML."""

    config_path = Path(path)
    raw_bytes = config_path.read_bytes()
    try:
        loaded = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OverlaySearchConfigError(f"invalid overlay search YAML: {config_path}") from exc
    raw = _mapping(loaded, "overlay search config")
    _check_keys(
        raw,
        "overlay search config",
        allowed={"version", "search_id", "policy", "limits", "stages"},
        required={"version", "search_id", "policy", "limits", "stages"},
    )
    if _whole(raw["version"], "version") != 1:
        raise OverlaySearchConfigError("overlay search config version must be 1")
    search_id = _required_text(raw["search_id"], "search_id")
    if _SAFE_ID.fullmatch(search_id) is None:
        raise OverlaySearchConfigError("search_id must be a filesystem-safe identifier")

    policy = _mapping(raw["policy"], "policy")
    _check_keys(
        policy,
        "policy",
        allowed={
            "catalog_expiry_mode",
            "execution_parameters",
            "short_options",
            "stock_strategy_binding",
            "automatic_stock_strategy_product",
        },
        required={
            "catalog_expiry_mode",
            "execution_parameters",
            "short_options",
            "stock_strategy_binding",
            "automatic_stock_strategy_product",
        },
    )
    expected_policy = {
        "catalog_expiry_mode": OFFICIAL_EXPIRY_MODE,
        "execution_parameters": "official_required",
        "short_options": "forbidden",
        "stock_strategy_binding": "none",
        "automatic_stock_strategy_product": False,
    }
    for key, expected in expected_policy.items():
        if policy[key] != expected:
            raise OverlaySearchConfigError(f"policy.{key} must be {expected!r}")

    limits = _mapping(raw["limits"], "limits")
    _check_keys(
        limits,
        "limits",
        allowed={"max_total_combinations"},
        required={"max_total_combinations"},
    )
    max_total = _whole(
        limits["max_total_combinations"], "limits.max_total_combinations"
    )
    stages = _mapping(raw["stages"], "stages")
    _check_keys(stages, "stages", allowed=set(_STAGES), required=set(_STAGES))

    budgets: list[OverlayStageBudget] = []
    specs: list[OverlaySearchSpec] = []
    for stage in _STAGES:
        budget, stage_specs = _expand_stage(stage, stages[stage])
        budgets.append(budget)
        specs.extend(stage_specs)
    if len(specs) > max_total:
        raise OverlaySearchConfigError(
            f"overlay search expands to {len(specs)} combinations, "
            f"above max_total_combinations={max_total}"
        )
    return OverlaySearchSpace(
        search_id=search_id,
        config_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        max_total_combinations=max_total,
        stage_budgets=tuple(budgets),
        specs=tuple(specs),
        stock_strategy_binding="none",
    )


def expand_overlay_search_space(path: str | Path) -> tuple[OverlaySearchSpec, ...]:
    return load_overlay_search_space(path).specs


def load_overlay_search_specs(path: str | Path) -> tuple[OverlaySearchSpec, ...]:
    return expand_overlay_search_space(path)


def _validate_official_catalog(catalog: CffexCatalog) -> None:
    expiry_mode = getattr(catalog, "expiry_mode", None)
    if expiry_mode != OFFICIAL_EXPIRY_MODE:
        raise Phase3OverlayFactoryError(
            "CFFEX catalog must use expiry_mode=official_asof_history"
        )
    unresolved = getattr(catalog, "unresolved_expiry_contracts", None)
    if isinstance(unresolved, (str, bytes)):
        raise Phase3OverlayFactoryError(
            "CFFEX catalog unresolved expiry state is not auditable"
        )
    try:
        unresolved_contracts = tuple(unresolved) if unresolved is not None else None
    except TypeError as exc:
        raise Phase3OverlayFactoryError(
            "CFFEX catalog unresolved expiry state is not auditable"
        ) from exc
    if unresolved_contracts is None or unresolved_contracts:
        preview = (
            "unknown"
            if unresolved_contracts is None
            else ",".join(map(str, unresolved_contracts[:5]))
        )
        raise Phase3OverlayFactoryError(
            f"CFFEX catalog has unresolved expiry contracts: {preview}"
        )


def _runtime_specs(
    overlay_spec: OverlaySearchSpec | None,
    futures_spec: FuturesOverlaySpec | None,
    option_spec: LongOptionOverlaySpec | None,
) -> tuple[
    FuturesOverlaySpec | None,
    LongOptionOverlaySpec | None,
    FuturesDirectionRule | None,
]:
    if overlay_spec is not None:
        if not isinstance(overlay_spec, OverlaySearchSpec):
            raise ValueError("overlay_spec must be an OverlaySearchSpec")
        if futures_spec is not None or option_spec is not None:
            raise ValueError("provide overlay_spec or direct runtime specs, not both")
        return (
            overlay_spec.futures,
            overlay_spec.long_option,
            overlay_spec.direction_rule,
        )
    if futures_spec is not None and not isinstance(futures_spec, FuturesOverlaySpec):
        raise ValueError("futures_spec must be a FuturesOverlaySpec")
    if option_spec is not None and not isinstance(option_spec, LongOptionOverlaySpec):
        raise ValueError("option_spec must be a LongOptionOverlaySpec")
    return futures_spec, option_spec, None


@dataclass(frozen=True)
class Phase3OverlayResources:
    """Validated read-only CFFEX resources shared by every backtest window."""

    catalog: CffexCatalog
    execution_parameters: CffexExecutionParameterSchedule
    assumptions: DevelopmentResearchAssumptions
    _direction_runtime_cache: dict[
        tuple[FuturesOverlaySpec, FuturesDirectionRule],
        FuturesOverlaySpec,
    ] = field(default_factory=dict, repr=False, compare=False)
    _direction_cache_lock: Any = field(
        default_factory=Lock,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, CffexCatalog):
            raise ValueError("catalog must be a CffexCatalog")
        if not isinstance(
            self.execution_parameters,
            CffexExecutionParameterSchedule,
        ):
            raise ValueError(
                "execution_parameters must be a CffexExecutionParameterSchedule"
            )
        if not isinstance(self.assumptions, DevelopmentResearchAssumptions):
            raise ValueError("assumptions must be DevelopmentResearchAssumptions")
        _validate_official_catalog(self.catalog)


def _resolve_runtime_futures_spec(
    resources: Phase3OverlayResources,
    futures_spec: FuturesOverlaySpec | None,
    direction_rule: FuturesDirectionRule | None,
) -> FuturesOverlaySpec | None:
    if direction_rule is None:
        return futures_spec
    if futures_spec is None:
        raise Phase3OverlayFactoryError("direction_rule requires a futures spec")
    if futures_spec.direction_by_signal_date is not None:
        raise Phase3OverlayFactoryError(
            "direction_rule cannot be combined with a resolved direction map"
        )
    cache_key = (futures_spec, direction_rule)
    with resources._direction_cache_lock:
        cached = resources._direction_runtime_cache.get(cache_key)
        if cached is not None:
            return cached
        directions = build_futures_direction_map(
            resources.catalog,
            futures_spec.product,
            direction_rule,
            min_dte=futures_spec.min_dte,
        )
        resolved = replace(
            futures_spec,
            direction="flat",
            direction_by_signal_date=directions,
        )
        resources._direction_runtime_cache[cache_key] = resolved
        return resolved


def load_phase3_overlay_resources(
    panel_path: str | Path,
    master_path: str | Path,
    trade_parameter_metadata_path: str | Path,
    settlement_parameters_path: str | Path | None = None,
    *,
    execution_parameters_path: str | Path | None = None,
    assumptions: DevelopmentResearchAssumptions | None = None,
    verify_execution_sources: bool = False,
) -> Phase3OverlayResources:
    supplied_execution_paths = sum(
        value is not None
        for value in (settlement_parameters_path, execution_parameters_path)
    )
    if supplied_execution_paths != 1:
        raise ValueError(
            "provide exactly one settlement_parameters_path or execution_parameters_path"
        )
    if not isinstance(verify_execution_sources, bool):
        raise TypeError("verify_execution_sources must be boolean")
    if assumptions is not None and not isinstance(
        assumptions,
        DevelopmentResearchAssumptions,
    ):
        raise ValueError("assumptions must be DevelopmentResearchAssumptions")
    selected_execution_path = (
        settlement_parameters_path
        if settlement_parameters_path is not None
        else execution_parameters_path
    )
    catalog = CffexCatalog(
        panel_path,
        master_path,
        trade_parameter_metadata_path=trade_parameter_metadata_path,
    )
    _validate_official_catalog(catalog)
    execution_schedule = CffexExecutionParameterSchedule(
        selected_execution_path,
        validate_artifact=True,
        verify_artifact_sources=verify_execution_sources,
    )
    return Phase3OverlayResources(
        catalog=catalog,
        execution_parameters=execution_schedule,
        assumptions=assumptions or DevelopmentResearchAssumptions(),
    )


def build_phase3_overlay_factory_from_resources(
    resources: Phase3OverlayResources,
    overlay_spec: OverlaySearchSpec | None = None,
    *,
    futures_spec: FuturesOverlaySpec | None = None,
    option_spec: LongOptionOverlaySpec | None = None,
) -> OverlayCoordinatorFactory:
    """Bind one candidate to already-loaded resources without re-reading artifacts."""

    if not isinstance(resources, Phase3OverlayResources):
        raise ValueError("resources must be Phase3OverlayResources")
    _validate_official_catalog(resources.catalog)
    selected_futures, selected_option, direction_rule = _runtime_specs(
        overlay_spec,
        futures_spec,
        option_spec,
    )
    selected_futures = _resolve_runtime_futures_spec(
        resources,
        selected_futures,
        direction_rule,
    )

    def coordinator_factory(account: CombinedAccount) -> Phase3OverlayCoordinator:
        if not isinstance(account, CombinedAccount):
            raise ValueError("account must be a CombinedAccount")
        _validate_official_catalog(resources.catalog)
        event_loop = DerivativeEventLoop(
            account=account,
            catalog=resources.catalog,
            assumptions=resources.assumptions,
            execution_parameters=resources.execution_parameters,
        )
        if event_loop.account is not account or event_loop.catalog is not resources.catalog:
            raise Phase3OverlayFactoryError(
                "per-window event loop did not preserve account/catalog identity"
            )
        if (
            event_loop.execution_parameters is not resources.execution_parameters
            or not event_loop.uses_official_execution_parameters
        ):
            raise Phase3OverlayFactoryError(
                "per-window event loop must explicitly use official execution parameters"
            )
        coordinator = Phase3OverlayCoordinator(
            account=account,
            event_loop=event_loop,
            futures_spec=selected_futures,
            option_spec=selected_option,
        )
        if coordinator.account is not account or coordinator.event_loop is not event_loop:
            raise Phase3OverlayFactoryError(
                "per-window coordinator did not preserve account/event-loop identity"
            )
        return coordinator

    return coordinator_factory


def build_phase3_overlay_factory(
    panel_path: str | Path,
    master_path: str | Path,
    trade_parameter_metadata_path: str | Path,
    settlement_parameters_path: str | Path | None = None,
    overlay_spec: OverlaySearchSpec | None = None,
    *,
    execution_parameters_path: str | Path | None = None,
    futures_spec: FuturesOverlaySpec | None = None,
    option_spec: LongOptionOverlaySpec | None = None,
    assumptions: DevelopmentResearchAssumptions | None = None,
    verify_execution_sources: bool = False,
) -> OverlayCoordinatorFactory:
    """Load official CFFEX resources once and return a per-window coordinator closure."""
    resources = load_phase3_overlay_resources(
        panel_path,
        master_path,
        trade_parameter_metadata_path,
        settlement_parameters_path,
        execution_parameters_path=execution_parameters_path,
        assumptions=assumptions,
        verify_execution_sources=verify_execution_sources,
    )
    return build_phase3_overlay_factory_from_resources(
        resources,
        overlay_spec,
        futures_spec=futures_spec,
        option_spec=option_spec,
    )


make_phase3_overlay_factory = build_phase3_overlay_factory
create_phase3_overlay_factory = build_phase3_overlay_factory


__all__ = [
    "OFFICIAL_EXPIRY_MODE",
    "OverlayCoordinatorFactory",
    "OverlaySearchConfigError",
    "OverlaySearchID",
    "OverlaySearchSpace",
    "OverlaySearchSpec",
    "OverlayStageBudget",
    "FuturesDirectionRule",
    "Phase3OverlayResources",
    "Phase3OverlayFactoryError",
    "build_phase3_overlay_factory",
    "build_phase3_overlay_factory_from_resources",
    "create_phase3_overlay_factory",
    "expand_overlay_search_space",
    "load_overlay_search_space",
    "load_overlay_search_specs",
    "load_phase3_overlay_resources",
    "make_phase3_overlay_factory",
]

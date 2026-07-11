from __future__ import annotations

import math
from dataclasses import dataclass, field
from numbers import Integral, Real
from types import MappingProxyType
from typing import Mapping

from quant_proof.cffex_catalog import (
    CffexCatalog,
    CffexCatalogError,
    FUTURES_PRODUCTS,
    FuturesSelection,
)


FUTURES_DIRECTION_KINDS = frozenset(
    {
        "time_series_momentum",
        "moving_average_or_breakout",
        "front_next_carry",
    }
)
FUTURES_POSITION_MODES = frozenset({"long_flat", "long_short_flat"})
FUTURES_TREND_VARIANTS = frozenset({"moving_average", "breakout"})


class FuturesDirectionSignalError(ValueError):
    """Raised when an official-data direction series cannot be built causally."""


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be a positive whole number")
    converted = int(value)
    if converted <= 0:
        raise ValueError(f"{name} must be a positive whole number")
    return converted


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be a non-negative whole number")
    converted = int(value)
    if converted < 0:
        raise ValueError(f"{name} must be a non-negative whole number")
    return converted


def _nonnegative_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be finite and non-negative")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return converted


@dataclass(frozen=True)
class FuturesDirectionRule:
    """Compact close-of-signal-date futures direction rule.

    ``neutral_band`` is measured in return units for momentum and moving-average
    rules, breakout distance for breakout rules, and annualized carry for carry.
    """

    kind: str
    position_mode: str
    lookback_days: int | None = None
    trend_variant: str | None = None
    fast_window: int | None = None
    slow_window: int | None = None
    neutral_band: float = 0.0

    def __post_init__(self) -> None:
        kind = str(self.kind).strip().lower()
        mode = str(self.position_mode).strip().lower()
        if kind not in FUTURES_DIRECTION_KINDS:
            raise ValueError(
                "kind must be time_series_momentum, "
                "moving_average_or_breakout, or front_next_carry"
            )
        if mode not in FUTURES_POSITION_MODES:
            raise ValueError("position_mode must be long_flat or long_short_flat")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "position_mode", mode)
        object.__setattr__(
            self,
            "neutral_band",
            _nonnegative_float(self.neutral_band, "neutral_band"),
        )

        if kind == "time_series_momentum":
            if self.lookback_days is None:
                raise ValueError("time_series_momentum requires lookback_days")
            object.__setattr__(
                self,
                "lookback_days",
                _positive_int(self.lookback_days, "lookback_days"),
            )
            if any(
                value is not None
                for value in (self.trend_variant, self.fast_window, self.slow_window)
            ):
                raise ValueError(
                    "time_series_momentum does not accept trend_variant or MA windows"
                )
            return

        if kind == "front_next_carry":
            if any(
                value is not None
                for value in (
                    self.lookback_days,
                    self.trend_variant,
                    self.fast_window,
                    self.slow_window,
                )
            ):
                raise ValueError("front_next_carry does not accept trend lookbacks")
            return

        if self.trend_variant is None:
            raise ValueError("moving_average_or_breakout requires trend_variant")
        variant = str(self.trend_variant).strip().lower()
        if variant not in FUTURES_TREND_VARIANTS:
            raise ValueError("trend_variant must be moving_average or breakout")
        object.__setattr__(self, "trend_variant", variant)
        if variant == "moving_average":
            if self.fast_window is None or self.slow_window is None:
                raise ValueError("moving_average requires fast_window and slow_window")
            fast = _positive_int(self.fast_window, "fast_window")
            slow = _positive_int(self.slow_window, "slow_window")
            if fast >= slow:
                raise ValueError("fast_window must be below slow_window")
            if self.lookback_days is not None:
                raise ValueError("moving_average does not accept lookback_days")
            object.__setattr__(self, "fast_window", fast)
            object.__setattr__(self, "slow_window", slow)
            return

        if self.lookback_days is None:
            raise ValueError("breakout requires lookback_days")
        if self.fast_window is not None or self.slow_window is not None:
            raise ValueError("breakout does not accept MA windows")
        object.__setattr__(
            self,
            "lookback_days",
            _positive_int(self.lookback_days, "lookback_days"),
        )

    def to_compact_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "position_mode": self.position_mode,
            "neutral_band": self.neutral_band,
        }
        if self.kind == "time_series_momentum":
            payload["lookback_days"] = self.lookback_days
        elif self.kind == "moving_average_or_breakout":
            payload["trend_variant"] = self.trend_variant
            if self.trend_variant == "moving_average":
                payload["fast_window"] = self.fast_window
                payload["slow_window"] = self.slow_window
            else:
                payload["lookback_days"] = self.lookback_days
        return payload


@dataclass(frozen=True)
class FuturesDirectionObservation:
    signal_date: str
    selected_contract: str
    return_contract: str | None
    continuous_return: float | None
    continuous_index: float
    score: float | None
    direction: str


@dataclass(frozen=True)
class FuturesDirectionResolution:
    product: str
    min_dte: int
    rule: FuturesDirectionRule
    observations: tuple[FuturesDirectionObservation, ...]
    direction_by_signal_date: Mapping[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        directions = {
            observation.signal_date: observation.direction
            for observation in self.observations
        }
        if len(directions) != len(self.observations):
            raise ValueError("direction observations contain duplicate signal dates")
        object.__setattr__(
            self,
            "direction_by_signal_date",
            MappingProxyType(directions),
        )


def _direction_from_score(score: float | None, rule: FuturesDirectionRule) -> str:
    if score is None:
        return "flat"
    if score > rule.neutral_band:
        return "long"
    if rule.position_mode == "long_short_flat" and score < -rule.neutral_band:
        return "short"
    return "flat"


def _trend_score(
    rule: FuturesDirectionRule,
    continuous_returns: list[float],
    continuous_levels: list[float],
) -> float | None:
    if rule.kind == "time_series_momentum":
        assert rule.lookback_days is not None
        if len(continuous_returns) < rule.lookback_days:
            return None
        gross_return = math.prod(
            1.0 + value for value in continuous_returns[-rule.lookback_days :]
        )
        return gross_return - 1.0

    if rule.kind != "moving_average_or_breakout":
        return None
    if rule.trend_variant == "moving_average":
        assert rule.fast_window is not None and rule.slow_window is not None
        if len(continuous_levels) < rule.slow_window:
            return None
        fast = sum(continuous_levels[-rule.fast_window :]) / rule.fast_window
        slow = sum(continuous_levels[-rule.slow_window :]) / rule.slow_window
        if slow <= 0.0:
            raise FuturesDirectionSignalError("continuous slow average must be positive")
        return fast / slow - 1.0

    assert rule.lookback_days is not None
    if len(continuous_levels) <= rule.lookback_days:
        return None
    current = continuous_levels[-1]
    trailing = continuous_levels[-rule.lookback_days - 1 : -1]
    trailing_high = max(trailing)
    trailing_low = min(trailing)
    if current > trailing_high:
        return current / trailing_high - 1.0
    if current < trailing_low:
        return current / trailing_low - 1.0
    return 0.0


def _carry_score(
    catalog: CffexCatalog,
    product: str,
    signal_date: str,
    min_dte: int,
) -> float:
    return catalog.front_next_carry(
        product,
        signal_date,
        min_dte=min_dte,
    ).annualized_carry


def resolve_futures_direction_rule(
    catalog: CffexCatalog,
    product: object,
    rule: FuturesDirectionRule,
    *,
    min_dte: int = 5,
) -> FuturesDirectionResolution:
    """Resolve one compact rule using only official rows visible by each date.

    The return recorded for date ``t`` uses the contract selected on ``t-1`` at
    both endpoints. The contract selected on ``t`` starts contributing on the
    next trading date, so a roll spread never appears as a strategy return.
    """

    if not isinstance(catalog, CffexCatalog):
        raise ValueError("catalog must be a CffexCatalog")
    if not isinstance(rule, FuturesDirectionRule):
        raise ValueError("rule must be a FuturesDirectionRule")
    normalized_product = str(product).strip().upper()
    if normalized_product not in FUTURES_PRODUCTS:
        raise ValueError("product must be IF, IH, IC, or IM")
    minimum_dte = _nonnegative_int(min_dte, "min_dte")
    try:
        first_date, last_date = catalog.product_date_ranges[normalized_product]
    except KeyError as exc:
        raise FuturesDirectionSignalError(
            f"{normalized_product} is absent from the CFFEX catalog"
        ) from exc
    signal_dates = tuple(
        signal_date
        for signal_date in catalog.available_trade_dates
        if first_date <= signal_date <= last_date
    )
    if not signal_dates:
        raise FuturesDirectionSignalError(
            f"{normalized_product} has no available signal dates"
        )

    previous_selection: FuturesSelection | None = None
    continuous_returns: list[float] = []
    continuous_levels: list[float] = []
    observations: list[FuturesDirectionObservation] = []
    continuous_index = 1.0

    for signal_date in signal_dates:
        try:
            selected = catalog.select_future(
                normalized_product,
                signal_date,
                min_dte=minimum_dte,
            )
        except CffexCatalogError as exc:
            raise FuturesDirectionSignalError(
                f"cannot select {normalized_product} on {signal_date}: {exc}"
            ) from exc

        return_contract: str | None = None
        continuous_return: float | None = None
        if previous_selection is not None:
            try:
                current_settlement = catalog.settlement(
                    previous_selection.contract,
                    signal_date,
                )
            except CffexCatalogError as exc:
                raise FuturesDirectionSignalError(
                    "continuous return requires the prior selected contract "
                    f"{previous_selection.contract} on {signal_date}: {exc}"
                ) from exc
            if current_settlement <= 0.0 or previous_selection.settle <= 0.0:
                raise FuturesDirectionSignalError(
                    "continuous return settlements must be strictly positive"
                )
            return_contract = previous_selection.contract
            continuous_return = current_settlement / previous_selection.settle - 1.0
            if not math.isfinite(continuous_return):
                raise FuturesDirectionSignalError("continuous return must be finite")
            continuous_returns.append(continuous_return)
            continuous_index *= 1.0 + continuous_return

        continuous_levels.append(continuous_index)
        try:
            score = (
                _carry_score(
                    catalog,
                    normalized_product,
                    signal_date,
                    minimum_dte,
                )
                if rule.kind == "front_next_carry"
                else _trend_score(rule, continuous_returns, continuous_levels)
            )
        except CffexCatalogError as exc:
            raise FuturesDirectionSignalError(
                f"cannot compute {rule.kind} on {signal_date}: {exc}"
            ) from exc
        direction = _direction_from_score(score, rule)
        observations.append(
            FuturesDirectionObservation(
                signal_date=signal_date,
                selected_contract=selected.contract,
                return_contract=return_contract,
                continuous_return=continuous_return,
                continuous_index=continuous_index,
                score=score,
                direction=direction,
            )
        )
        previous_selection = selected

    return FuturesDirectionResolution(
        product=normalized_product,
        min_dte=minimum_dte,
        rule=rule,
        observations=tuple(observations),
    )


def build_futures_direction_map(
    catalog: CffexCatalog,
    product: object,
    rule: FuturesDirectionRule,
    *,
    min_dte: int = 5,
) -> Mapping[str, str]:
    return resolve_futures_direction_rule(
        catalog,
        product,
        rule,
        min_dte=min_dte,
    ).direction_by_signal_date


__all__ = [
    "FUTURES_DIRECTION_KINDS",
    "FUTURES_POSITION_MODES",
    "FUTURES_TREND_VARIANTS",
    "FuturesDirectionObservation",
    "FuturesDirectionResolution",
    "FuturesDirectionRule",
    "FuturesDirectionSignalError",
    "build_futures_direction_map",
    "resolve_futures_direction_rule",
]

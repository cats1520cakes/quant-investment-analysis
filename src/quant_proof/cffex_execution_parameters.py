from __future__ import annotations

import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from numbers import Integral, Real
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd

from quant_proof.free_sources.cffex_settlement_params import (
    CffexArtifactError,
    CffexLookupError,
    ShortOptionsDisabledError,
    normalize_snapshot_date,
    validate_cffex_settlement_artifact,
)


EVIDENCE_LABEL: Final = "official_snapshot_asof_not_legal_effective_date"
CONSERVATIVE_MAINTENANCE_POLICY: Final = "conservative_same_rate"

_FUTURE_PRODUCTS = frozenset({"IF", "IH", "IC", "IM"})
_OPTION_PRODUCTS = frozenset({"IO", "HO", "MO"})
_ALL_PRODUCTS = _FUTURE_PRODUCTS | _OPTION_PRODUCTS
_SUPPORTED_FEE_UNITS = frozenset({"notional_rate", "currency_per_contract"})

_FUTURE_CONTRACT = re.compile(r"^(?P<product>IF|IH|IC|IM)(?P<yymm>\d{4})$")
_OPTION_SERIES = re.compile(r"^(?P<product>IO|HO|MO)(?P<yymm>\d{4})$")
_OPTION_CONTRACT = re.compile(
    r"^(?P<series>(?P<product>IO|HO|MO)(?P<yymm>\d{4}))-"
    r"(?P<option_type>C|P)-(?P<strike>\d+(?:\.\d+)?)$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_EXECUTION_COLUMNS = (
    "snapshot_date",
    "instrument_type",
    "parameter_scope",
    "contract_or_series",
    "product",
    "long_margin_rate",
    "short_margin_rate",
    "trading_fee_value",
    "trading_fee_unit",
    "settlement_fee_value",
    "settlement_fee_unit",
    "settlement_fee_kind",
    "close_today_fee_multiplier",
    "close_today_fee_semantics",
    "option_shorting_enabled",
    "source_section_title_matches_snapshot",
    "source_sha256",
)

_ACTIONS = {
    "open": ("open", "long"),
    "open_long": ("open", "long"),
    "buy_to_open": ("open", "long"),
    "open_short": ("open", "short"),
    "sell_to_open": ("open", "short"),
    "close": ("close", "long"),
    "close_long": ("close", "long"),
    "sell_to_close": ("close", "long"),
    "close_short": ("close", "short"),
    "buy_to_close": ("close", "short"),
}


class CffexExecutionParameterError(ValueError):
    """Raised when execution parameters cannot be used without assumptions."""


class UnsupportedFeeUnitError(CffexExecutionParameterError):
    """Raised when an official fee unit has no approved amount formula."""


@dataclass(frozen=True)
class CffexExecutionParameters:
    contract: str
    parameter_key: str
    instrument_type: str
    product: str
    position_side: str
    long_margin_rate: float | None
    short_margin_rate: float | None
    initial_margin_rate: float | None
    maintenance_margin_rate: float | None
    maintenance_margin_policy: str | None
    trading_fee_value: float
    trading_fee_unit: str
    settlement_fee_value: float
    settlement_fee_unit: str
    settlement_fee_kind: str
    close_today_fee_multiplier: float
    source_snapshot_date: str
    source_sha256: str
    section_title_mismatch: bool
    evidence_label: str = EVIDENCE_LABEL

    @property
    def contract_or_series(self) -> str:
        return self.parameter_key

    @property
    def close_today_multiplier(self) -> float:
        return self.close_today_fee_multiplier

    @property
    def source_snapshot(self) -> str:
        return self.source_snapshot_date

    @property
    def source_hash(self) -> str:
        return self.source_sha256

    @property
    def source_section_title_mismatch(self) -> bool:
        return self.section_title_mismatch

    @property
    def maintenance_margin_basis(self) -> str | None:
        return self.maintenance_margin_policy


# A singular alias keeps call sites readable when annotating one lookup result.
CffexExecutionParameter = CffexExecutionParameters


@dataclass(frozen=True)
class _ParameterRow:
    snapshot_date: str
    instrument_type: str
    parameter_key: str
    product: str
    long_margin_rate: float | None
    short_margin_rate: float | None
    trading_fee_value: float
    trading_fee_unit: str
    settlement_fee_value: float
    settlement_fee_unit: str
    settlement_fee_kind: str
    close_today_fee_multiplier: float
    source_sha256: str
    section_title_mismatch: bool


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CffexExecutionParameterError(f"{name} must be a non-empty string")
    return value.strip()


def _finite(value: object, name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise CffexExecutionParameterError(f"{name} must be finite")
    converted = float(value)
    if not math.isfinite(converted):
        raise CffexExecutionParameterError(f"{name} must be finite")
    return converted


def _positive(value: object, name: str) -> float:
    converted = _finite(value, name)
    if converted <= 0.0:
        raise CffexExecutionParameterError(f"{name} must be positive")
    return converted


def _nonnegative(value: object, name: str) -> float:
    converted = _finite(value, name)
    if converted < 0.0:
        raise CffexExecutionParameterError(f"{name} must be non-negative")
    return converted


def _positive_contract_count(value: object) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral):
        raise CffexExecutionParameterError(
            "contracts must be a positive whole number of contracts"
        )
    converted = int(value)
    if converted <= 0:
        raise CffexExecutionParameterError(
            "contracts must be a positive whole number of contracts"
        )
    return converted


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, (bool, np.bool_)):
        raise CffexExecutionParameterError(f"{name} must be boolean")
    return bool(value)


def _validate_yymm(value: str, name: str) -> None:
    month = int(value[-2:])
    if month < 1 or month > 12:
        raise CffexLookupError(f"{name} must contain a valid yymm month")


def _normalize_contract(value: object) -> tuple[str, str, str, str]:
    if not isinstance(value, str) or not value.strip():
        raise CffexLookupError("an exact CFFEX contract or option series is required")
    contract = value.strip().upper()
    if contract in _ALL_PRODUCTS:
        raise CffexLookupError(
            "an exact contract or option series is required; product substitution is forbidden"
        )

    future = _FUTURE_CONTRACT.fullmatch(contract)
    if future:
        _validate_yymm(future.group("yymm"), "futures contract")
        return contract, contract, "future", future.group("product")

    series = _OPTION_SERIES.fullmatch(contract)
    if series:
        _validate_yymm(series.group("yymm"), "option series")
        return contract, contract, "option", series.group("product")

    option = _OPTION_CONTRACT.fullmatch(contract)
    if option:
        _validate_yymm(option.group("yymm"), "option contract")
        strike = float(option.group("strike"))
        if not math.isfinite(strike) or strike <= 0.0:
            raise CffexLookupError("option strike must be positive and finite")
        return contract, option.group("series"), "option", option.group("product")

    raise CffexLookupError(
        "unsupported CFFEX identifier; require exact IF/IH/IC/IM+yymm or "
        "IO/HO/MO+yymm[-C/P-strike], and product substitution is forbidden"
    )


def _normalize_side(value: object) -> str:
    side = _required_text(value, "position_side").lower()
    if side not in {"long", "short"}:
        raise CffexExecutionParameterError("position_side must be long or short")
    return side


def _normalize_action(value: object) -> tuple[str, str]:
    action = _required_text(value, "action").lower()
    try:
        return _ACTIONS[action]
    except KeyError as exc:
        raise CffexExecutionParameterError(
            f"unsupported action {action!r}; fee classification fails closed"
        ) from exc


def _normalized_stored_date(value: object) -> str:
    raw = _required_text(value, "snapshot_date")
    normalized = normalize_snapshot_date(raw)
    if raw != normalized:
        raise CffexExecutionParameterError(
            f"snapshot_date must use canonical YYYYMMDD form: {raw!r}"
        )
    return normalized


def _optional_margin(value: object, name: str) -> float | None:
    if pd.isna(value):
        return None
    return _positive(value, name)


class CffexExecutionParameterSchedule:
    """Immutable, causal execution-parameter view over one canonical parquet."""

    def __init__(
        self,
        canonical_parquet: str | Path,
        *,
        validate_artifact: bool = True,
        verify_artifact_sources: bool = False,
        validate: bool | None = None,
        verify_sources: bool | None = None,
    ) -> None:
        if validate is not None:
            if not isinstance(validate, bool):
                raise TypeError("validate must be boolean when provided")
            validate_artifact = validate
        if verify_sources is not None:
            if not isinstance(verify_sources, bool):
                raise TypeError("verify_sources must be boolean when provided")
            verify_artifact_sources = verify_sources
        if not isinstance(validate_artifact, bool) or not isinstance(
            verify_artifact_sources, bool
        ):
            raise TypeError(
                "validate_artifact and verify_artifact_sources must be boolean"
            )
        if not isinstance(canonical_parquet, (str, Path)):
            raise TypeError("canonical_parquet must be a filesystem path")
        path = Path(canonical_parquet)
        if not path.is_file():
            raise FileNotFoundError(path)

        if validate_artifact:
            manifest = validate_cffex_settlement_artifact(
                path,
                verify_sources=verify_artifact_sources,
            )
            if manifest.get("is_canonical") is not True:
                raise CffexArtifactError(
                    "CFFEX execution parameters require a canonical settlement artifact"
                )

        frame = pd.read_parquet(path, columns=list(_EXECUTION_COLUMNS))
        self._path = path
        self._records = self._build_index(frame)
        self._rows_loaded = len(frame)

    @staticmethod
    def _build_index(
        frame: pd.DataFrame,
    ) -> dict[tuple[str, str], tuple[_ParameterRow, ...]]:
        if frame.empty:
            raise CffexExecutionParameterError(
                "canonical CFFEX execution parameter parquet is empty"
            )
        missing = sorted(set(_EXECUTION_COLUMNS) - set(frame.columns))
        if missing:
            raise CffexExecutionParameterError(
                f"CFFEX execution parameter parquet is missing columns: {missing}"
            )

        grouped: dict[tuple[str, str], list[_ParameterRow]] = {}
        seen: set[tuple[str, str, str]] = set()
        for raw in frame.loc[:, list(_EXECUTION_COLUMNS)].to_dict(orient="records"):
            snapshot_date = _normalized_stored_date(raw["snapshot_date"])
            instrument_type = _required_text(
                raw["instrument_type"], "instrument_type"
            ).lower()
            parameter_key = _required_text(
                raw["contract_or_series"], "contract_or_series"
            ).upper()
            product = _required_text(raw["product"], "product").upper()
            _, normalized_key, parsed_type, parsed_product = _normalize_contract(
                parameter_key
            )
            if parameter_key != normalized_key or instrument_type != parsed_type:
                raise CffexExecutionParameterError(
                    f"instrument/key disagreement for {parameter_key}"
                )
            if product != parsed_product:
                raise CffexExecutionParameterError(
                    f"product/key disagreement for {parameter_key}"
                )

            expected_scope = "contract" if instrument_type == "future" else "series"
            if _required_text(raw["parameter_scope"], "parameter_scope") != expected_scope:
                raise CffexExecutionParameterError(
                    f"parameter_scope must be {expected_scope} for {parameter_key}"
                )
            expected_kind = "delivery" if instrument_type == "future" else "exercise"
            settlement_fee_kind = _required_text(
                raw["settlement_fee_kind"], "settlement_fee_kind"
            )
            if settlement_fee_kind != expected_kind:
                raise CffexExecutionParameterError(
                    f"settlement_fee_kind must be {expected_kind} for {parameter_key}"
                )
            if (
                _required_text(
                    raw["close_today_fee_semantics"],
                    "close_today_fee_semantics",
                )
                != "fraction_of_trading_fee"
            ):
                raise CffexExecutionParameterError(
                    "unsupported close_today_fee_semantics"
                )
            if _strict_bool(
                raw["option_shorting_enabled"], "option_shorting_enabled"
            ):
                raise CffexExecutionParameterError("short options must remain disabled")

            long_margin_rate = _optional_margin(
                raw["long_margin_rate"], "long_margin_rate"
            )
            short_margin_rate = _optional_margin(
                raw["short_margin_rate"], "short_margin_rate"
            )
            if instrument_type == "future" and (
                long_margin_rate is None or short_margin_rate is None
            ):
                raise CffexExecutionParameterError(
                    f"future margin rates are required for {parameter_key}"
                )
            if instrument_type == "option" and (
                long_margin_rate is not None or short_margin_rate is not None
            ):
                raise CffexExecutionParameterError(
                    f"option rows must not expose futures margin rates: {parameter_key}"
                )

            source_sha256 = _required_text(raw["source_sha256"], "source_sha256")
            if not _SHA256.fullmatch(source_sha256):
                raise CffexExecutionParameterError("source_sha256 must be lowercase SHA-256")
            title_matches = _strict_bool(
                raw["source_section_title_matches_snapshot"],
                "source_section_title_matches_snapshot",
            )

            unique_key = (snapshot_date, instrument_type, parameter_key)
            if unique_key in seen:
                raise CffexExecutionParameterError(
                    f"duplicate CFFEX execution parameter row: {unique_key}"
                )
            seen.add(unique_key)
            row = _ParameterRow(
                snapshot_date=snapshot_date,
                instrument_type=instrument_type,
                parameter_key=parameter_key,
                product=product,
                long_margin_rate=long_margin_rate,
                short_margin_rate=short_margin_rate,
                trading_fee_value=_positive(
                    raw["trading_fee_value"], "trading_fee_value"
                ),
                trading_fee_unit=_required_text(
                    raw["trading_fee_unit"], "trading_fee_unit"
                ),
                settlement_fee_value=_positive(
                    raw["settlement_fee_value"], "settlement_fee_value"
                ),
                settlement_fee_unit=_required_text(
                    raw["settlement_fee_unit"], "settlement_fee_unit"
                ),
                settlement_fee_kind=settlement_fee_kind,
                close_today_fee_multiplier=_nonnegative(
                    raw["close_today_fee_multiplier"],
                    "close_today_fee_multiplier",
                ),
                source_sha256=source_sha256,
                section_title_mismatch=not title_matches,
            )
            grouped.setdefault((instrument_type, parameter_key), []).append(row)

        return {
            key: tuple(sorted(rows, key=lambda item: item.snapshot_date))
            for key, rows in grouped.items()
        }

    @property
    def canonical_parquet(self) -> Path:
        return self._path

    @property
    def rows_loaded(self) -> int:
        return self._rows_loaded

    def lookup(
        self,
        contract: object,
        date: object,
        position_side: object = "long",
        *,
        side: object | None = None,
    ) -> CffexExecutionParameters:
        normalized_side = _normalize_side(position_side)
        if side is not None:
            alias_side = _normalize_side(side)
            if normalized_side != "long" and normalized_side != alias_side:
                raise CffexExecutionParameterError(
                    "position_side and side specify conflicting values"
                )
            normalized_side = alias_side
        requested, parameter_key, instrument_type, product = _normalize_contract(contract)
        if instrument_type == "option" and normalized_side == "short":
            raise ShortOptionsDisabledError(
                "short options are disabled; no execution margin parameter is authorized"
            )
        cutoff = normalize_snapshot_date(date)
        return self._lookup_cached(
            requested,
            parameter_key,
            instrument_type,
            product,
            cutoff,
            normalized_side,
        )

    # These aliases make the intended operation explicit at integration call sites.
    parameters_for = lookup
    get = lookup

    @lru_cache(maxsize=4096)
    def _lookup_cached(
        self,
        requested: str,
        parameter_key: str,
        instrument_type: str,
        product: str,
        cutoff: str,
        position_side: str,
    ) -> CffexExecutionParameters:
        rows = self._records.get((instrument_type, parameter_key), ())
        dates = tuple(row.snapshot_date for row in rows)
        index = bisect_right(dates, cutoff) - 1
        if index < 0:
            raise CffexLookupError(
                f"no exact execution parameter for {parameter_key} on or before {cutoff}; "
                "future backfill and product substitution are forbidden"
            )
        row = rows[index]
        if row.product != product:
            raise CffexLookupError(
                f"exact product disagreement for {parameter_key}; product substitution is forbidden"
            )

        if instrument_type == "future":
            initial_margin_rate = (
                row.long_margin_rate
                if position_side == "long"
                else row.short_margin_rate
            )
            maintenance_margin_rate = initial_margin_rate
            maintenance_policy = CONSERVATIVE_MAINTENANCE_POLICY
        else:
            initial_margin_rate = None
            maintenance_margin_rate = None
            maintenance_policy = None

        return CffexExecutionParameters(
            contract=requested,
            parameter_key=parameter_key,
            instrument_type=instrument_type,
            product=product,
            position_side=position_side,
            long_margin_rate=row.long_margin_rate,
            short_margin_rate=row.short_margin_rate,
            initial_margin_rate=initial_margin_rate,
            maintenance_margin_rate=maintenance_margin_rate,
            maintenance_margin_policy=maintenance_policy,
            trading_fee_value=row.trading_fee_value,
            trading_fee_unit=row.trading_fee_unit,
            settlement_fee_value=row.settlement_fee_value,
            settlement_fee_unit=row.settlement_fee_unit,
            settlement_fee_kind=row.settlement_fee_kind,
            close_today_fee_multiplier=row.close_today_fee_multiplier,
            source_snapshot_date=row.snapshot_date,
            source_sha256=row.source_sha256,
            section_title_mismatch=row.section_title_mismatch,
        )

    def cache_info(self) -> object:
        return self._lookup_cached.cache_info()

    def clear_cache(self) -> None:
        self._lookup_cached.cache_clear()

    @staticmethod
    def _amount_from_unit(
        *,
        value: float,
        unit: str,
        contracts: int,
        price: float,
        multiplier: float,
        field_name: str,
    ) -> float:
        if unit == "notional_rate":
            amount = value * contracts * price * multiplier
        elif unit == "currency_per_contract":
            amount = value * contracts
        else:
            supported = ", ".join(sorted(_SUPPORTED_FEE_UNITS))
            raise UnsupportedFeeUnitError(
                f"unsupported {field_name} unit {unit!r}; supported units are {supported}"
            )
        if not math.isfinite(amount) or amount < 0.0:
            raise CffexExecutionParameterError(
                f"calculated {field_name} amount must be non-negative and finite"
            )
        return float(amount)

    def fee_amount(
        self,
        contract: object,
        date: object,
        contracts: object,
        price: object,
        multiplier: object,
        action: object,
        opened_date: object | None = None,
    ) -> float:
        count = _positive_contract_count(contracts)
        checked_price = _positive(price, "price")
        checked_multiplier = _positive(multiplier, "multiplier")
        trade_date = normalize_snapshot_date(date)
        action_kind, position_side = _normalize_action(action)

        if action_kind == "close":
            if opened_date is None:
                raise CffexExecutionParameterError(
                    "opened_date is required for a close fee classification"
                )
            normalized_opened_date = normalize_snapshot_date(opened_date)
            if normalized_opened_date > trade_date:
                raise CffexExecutionParameterError(
                    "opened_date cannot be later than the fee date"
                )
            same_day_close = normalized_opened_date == trade_date
        else:
            if opened_date is not None:
                raise CffexExecutionParameterError(
                    "opened_date is only valid for a close action"
                )
            same_day_close = False

        parameters = self.lookup(
            contract,
            trade_date,
            position_side=position_side,
        )
        amount = self._amount_from_unit(
            value=parameters.trading_fee_value,
            unit=parameters.trading_fee_unit,
            contracts=count,
            price=checked_price,
            multiplier=checked_multiplier,
            field_name="trading fee",
        )
        if same_day_close:
            amount *= parameters.close_today_fee_multiplier
            if not math.isfinite(amount) or amount < 0.0:
                raise CffexExecutionParameterError(
                    "calculated close-today fee must be non-negative and finite"
                )
        return float(amount)

    def settlement_fee_amount(
        self,
        contract: object,
        date: object,
        contracts: object,
        price: object,
        multiplier: object,
        position_side: object = "long",
        *,
        side: object | None = None,
    ) -> float:
        count = _positive_contract_count(contracts)
        checked_price = _positive(price, "price")
        checked_multiplier = _positive(multiplier, "multiplier")
        parameters = self.lookup(
            contract,
            date,
            position_side=position_side,
            side=side,
        )
        return self._amount_from_unit(
            value=parameters.settlement_fee_value,
            unit=parameters.settlement_fee_unit,
            contracts=count,
            price=checked_price,
            multiplier=checked_multiplier,
            field_name="settlement fee",
        )

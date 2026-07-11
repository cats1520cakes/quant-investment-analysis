from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from quant_proof.free_sources.cffex_adapter import (
    CffexDataError,
    validate_cffex_contract_master_manifest,
    validate_cffex_panel_manifest,
)
from quant_proof.free_sources.cffex_trade_parameters import (
    cffex_trade_parameter_metadata_manifest_path,
    validate_cffex_trade_parameter_metadata_manifest,
)


FUTURES_PRODUCTS = frozenset({"IF", "IH", "IC", "IM"})
OPTION_PRODUCTS = frozenset({"IO", "HO", "MO"})

_PANEL_COLUMNS = (
    "trade_date",
    "contract",
    "product",
    "instrument_type",
    "option_type",
    "strike",
    "multiplier",
    "open",
    "settle",
    "volume",
    "open_interest",
    "delta",
    "open_executable",
    "settlement_mark_valid",
)
_MASTER_COLUMNS = ("contract", "last_trade_date")
_EXPIRY_HISTORY_COLUMNS = (
    "snapshot_date",
    "contract",
    "official_last_trade_date",
)


class CffexCatalogError(CffexDataError):
    """Raised when a causal CFFEX lookup cannot be answered exactly."""


class RightCensoredExpiryError(CffexCatalogError):
    """Raised when the panel horizon prevents a contract's expiry/DTE from being known."""


@dataclass(frozen=True)
class FuturesSelection:
    signal_date: str
    product: str
    contract: str
    last_trade_date: str
    dte: int
    multiplier: float
    open_interest: float
    volume: float
    settle: float
    expiry_snapshot_date: str | None = None

    @property
    def settlement_price(self) -> float:
        return self.settle


@dataclass(frozen=True)
class OptionSelection:
    signal_date: str
    product: str
    contract: str
    option_type: str
    last_trade_date: str
    dte: int
    multiplier: float
    strike: float
    delta: float
    target_abs_delta: float
    delta_distance: float
    open_interest: float
    volume: float
    expiry_snapshot_date: str | None = None


@dataclass(frozen=True)
class OpenExecution:
    signal_date: str
    execution_date: str
    contract: str
    open_price: float
    volume: float

    @property
    def price(self) -> float:
        return self.open_price


@dataclass(frozen=True)
class CurvePoint:
    signal_date: str
    product: str
    contract: str
    last_trade_date: str
    dte: int
    settle: float
    open_interest: float | None
    volume: float | None
    expiry_snapshot_date: str | None = None


@dataclass(frozen=True)
class FrontNextCarry:
    """Observed front/next spread and simple annualized long-front roll carry."""

    signal_date: str
    product: str
    front: CurvePoint
    next: CurvePoint
    tenor_days: int
    settlement_spread: float
    relative_spread: float
    annualized_carry: float


def _normalize_date(value: object, field: str) -> str:
    if isinstance(value, pd.Timestamp):
        timestamp = value
    elif isinstance(value, (datetime, date)):
        timestamp = pd.Timestamp(value)
    else:
        text = str(value).strip()
        try:
            timestamp = pd.to_datetime(text, format="%Y%m%d", errors="raise") if len(text) == 8 else pd.Timestamp(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a valid date") from exc
    if pd.isna(timestamp):
        raise ValueError(f"{field} must be a valid date")
    return timestamp.strftime("%Y%m%d")


def _normalize_contract(value: object) -> str:
    if pd.isna(value):
        raise ValueError("contract must not be empty")
    contract = str(value).strip().upper()
    if not contract:
        raise ValueError("contract must not be empty")
    return contract


def _normalize_product(value: object, allowed: frozenset[str], field: str = "product") -> str:
    product = str(value).strip().upper()
    if product not in allowed:
        expected = ", ".join(sorted(allowed))
        raise ValueError(f"{field} must be one of: {expected}")
    return product


def _normalize_option_type(value: object) -> str:
    normalized = str(value).strip().lower()
    aliases = {"c": "call", "call": "call", "p": "put", "put": "put"}
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError("option_type must be call/C or put/P") from exc


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        converted = int(value)
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if converted < 0 or not math.isfinite(numeric) or numeric != converted:
        raise ValueError(f"{field} must be a non-negative integer")
    return converted


def _coerce_bool(series: pd.Series, field: str) -> pd.Series:
    aliases = {
        True: True,
        False: False,
        1: True,
        0: False,
        "true": True,
        "false": False,
        "1": True,
        "0": False,
    }

    def convert(value: object) -> bool:
        if pd.isna(value):
            raise CffexCatalogError(f"{field} contains a missing value")
        key = value.strip().lower() if isinstance(value, str) else value
        if key not in aliases:
            raise CffexCatalogError(f"{field} contains a non-boolean value")
        return aliases[key]

    return series.map(convert).astype(bool)


def _optional_finite(value: object) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _optional_text(value: object) -> str | None:
    return None if pd.isna(value) else str(value)


def _load_verified_official_expiry_history(
    metadata_path: str | Path,
    panel_path: str | Path,
    master_path: str | Path,
) -> tuple[pd.DataFrame, dict[str, object]]:
    metadata = Path(metadata_path)
    manifest_path = cffex_trade_parameter_metadata_manifest_path(metadata)
    try:
        manifest_hint = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CffexCatalogError(
            f"official CFFEX expiry metadata manifest is unreadable: {manifest_path}"
        ) from exc
    if not isinstance(manifest_hint, dict):
        raise CffexCatalogError(
            f"official CFFEX expiry metadata manifest is invalid: {manifest_path}"
        )
    download_manifest = manifest_hint.get("source_download_manifest_path")
    if not isinstance(download_manifest, str) or not download_manifest.strip():
        raise CffexCatalogError(
            "official CFFEX expiry metadata manifest is missing its source manifest path"
        )
    manifest = validate_cffex_trade_parameter_metadata_manifest(
        metadata,
        panel_path,
        master_path,
        download_manifest,
    )
    if manifest.get("canonical") is not True:
        raise CffexCatalogError("official CFFEX expiry history must be canonical")
    if manifest.get("complete_master_coverage") is not True:
        raise CffexCatalogError(
            "official CFFEX expiry history must cover the exact contract master"
        )
    history_path = Path(str(manifest.get("history_path", "")))
    try:
        history = pd.read_parquet(history_path, columns=list(_EXPIRY_HISTORY_COLUMNS))
    except (OSError, ValueError, TypeError) as exc:
        raise CffexCatalogError(
            "validated official CFFEX expiry history could not be loaded"
        ) from exc
    return history, dict(manifest)


class CffexCatalog:
    """Exact-date causal access to a manifest-bound CFFEX contract panel."""

    def __init__(
        self,
        panel_path: str | Path,
        master_path: str | Path,
        *,
        trade_parameter_metadata_path: str | Path | None = None,
    ) -> None:
        panel = Path(panel_path)
        master = Path(master_path)
        panel_manifest = validate_cffex_panel_manifest(panel)
        master_manifest = validate_cffex_contract_master_manifest(master, panel)
        if "last_date" not in panel_manifest:
            raise CffexCatalogError("validated CFFEX panel manifest is missing last_date")
        try:
            daily = pd.read_parquet(panel, columns=list(_PANEL_COLUMNS))
            contracts = pd.read_parquet(master, columns=list(_MASTER_COLUMNS))
        except (OSError, ValueError, TypeError) as exc:
            raise CffexCatalogError("validated CFFEX parquet data could not be loaded") from exc
        expiry_history: pd.DataFrame | None = None
        expiry_manifest: Mapping[str, object] | None = None
        expiry_mode = "master_observation"
        if trade_parameter_metadata_path is not None:
            expiry_history, expiry_manifest = _load_verified_official_expiry_history(
                trade_parameter_metadata_path,
                panel,
                master,
            )
            expiry_mode = "official_asof_history"
        self._initialize(
            daily,
            contracts,
            panel_manifest,
            master_manifest,
            panel_horizon=panel_manifest["last_date"],
            expiry_history=expiry_history,
            expiry_mode=expiry_mode,
            expiry_manifest=expiry_manifest,
        )

    @classmethod
    def from_frames(
        cls,
        daily: pd.DataFrame,
        contracts: pd.DataFrame,
        *,
        panel_horizon: object | None = None,
        expiry_history: pd.DataFrame | None = None,
    ) -> CffexCatalog:
        """Construct from synthetic frames with optional exact-contract as-of expiry history."""

        catalog = cls.__new__(cls)
        catalog._initialize(
            daily,
            contracts,
            None,
            None,
            panel_horizon=panel_horizon,
            expiry_history=expiry_history,
            expiry_mode=(
                "provided_asof_history"
                if expiry_history is not None
                else "master_observation"
            ),
            expiry_manifest=None,
        )
        return catalog

    def _initialize(
        self,
        daily: pd.DataFrame,
        contracts: pd.DataFrame,
        panel_manifest: Mapping[str, object] | None,
        master_manifest: Mapping[str, object] | None,
        *,
        panel_horizon: object | None,
        expiry_history: pd.DataFrame | None,
        expiry_mode: str,
        expiry_manifest: Mapping[str, object] | None,
    ) -> None:
        panel_missing = sorted(set(_PANEL_COLUMNS) - set(daily.columns))
        master_missing = sorted(set(_MASTER_COLUMNS) - set(contracts.columns))
        if panel_missing:
            raise CffexCatalogError(f"CFFEX catalog panel missing columns: {','.join(panel_missing)}")
        if master_missing:
            raise CffexCatalogError(f"CFFEX catalog master missing columns: {','.join(master_missing)}")
        if daily.empty:
            raise CffexCatalogError("CFFEX catalog panel is empty")
        if contracts.empty:
            raise CffexCatalogError("CFFEX catalog master is empty")

        frame = daily.loc[:, list(_PANEL_COLUMNS)].copy().reset_index(drop=True)
        master = contracts.loc[:, list(_MASTER_COLUMNS)].copy().reset_index(drop=True)
        try:
            frame["trade_date"] = frame["trade_date"].map(lambda value: _normalize_date(value, "trade_date"))
            master["last_trade_date"] = master["last_trade_date"].map(
                lambda value: _normalize_date(value, "last_trade_date")
            )
            frame["contract"] = frame["contract"].map(_normalize_contract)
            master["contract"] = master["contract"].map(_normalize_contract)
        except ValueError as exc:
            raise CffexCatalogError(str(exc)) from exc

        normalized_horizon: str | None = None
        if panel_horizon is not None:
            try:
                normalized_horizon = _normalize_date(panel_horizon, "panel_horizon")
            except ValueError as exc:
                raise CffexCatalogError(str(exc)) from exc
            if normalized_horizon < str(frame["trade_date"].max()):
                raise CffexCatalogError("panel_horizon precedes an observed CFFEX trade date")

        if frame.duplicated(["trade_date", "contract"]).any():
            raise CffexCatalogError("CFFEX catalog panel has duplicate trade_date/contract rows")
        if master["contract"].duplicated().any():
            raise CffexCatalogError("CFFEX catalog master has duplicate contracts")

        frame["product"] = frame["product"].astype("string").str.strip().str.upper()
        frame["instrument_type"] = frame["instrument_type"].astype("string").str.strip().str.lower()
        frame["option_type"] = frame["option_type"].fillna("").astype("string").str.strip().str.lower()
        supported_products = FUTURES_PRODUCTS | OPTION_PRODUCTS
        if not frame["product"].isin(supported_products).all():
            raise CffexCatalogError("CFFEX catalog panel contains an unsupported product")
        expected_types = frame["product"].map(
            {product: "future" for product in FUTURES_PRODUCTS}
            | {product: "option" for product in OPTION_PRODUCTS}
        )
        if not frame["instrument_type"].eq(expected_types).fillna(False).all():
            raise CffexCatalogError("CFFEX product/instrument_type metadata is inconsistent")
        option_rows = frame["instrument_type"].eq("option")
        if not frame.loc[option_rows, "option_type"].isin({"call", "put"}).all():
            raise CffexCatalogError("CFFEX option rows must identify call or put")

        for column in (
            "strike",
            "multiplier",
            "open",
            "settle",
            "volume",
            "open_interest",
            "delta",
        ):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if (~np.isfinite(frame["multiplier"]) | frame["multiplier"].le(0.0)).any():
            raise CffexCatalogError("CFFEX panel multipliers must be finite and positive")
        if (
            ~np.isfinite(frame.loc[option_rows, "strike"])
            | frame.loc[option_rows, "strike"].lt(0.0)
        ).any():
            raise CffexCatalogError("CFFEX option strikes must be finite and non-negative")
        frame["open_executable"] = _coerce_bool(frame["open_executable"], "open_executable")
        frame["settlement_mark_valid"] = _coerce_bool(
            frame["settlement_mark_valid"], "settlement_mark_valid"
        )

        master["_expiry_right_censored"] = (
            False if normalized_horizon is None else master["last_trade_date"].eq(normalized_horizon)
        )
        indexed_master = master.set_index("contract")
        last_trade_dates = indexed_master["last_trade_date"]
        missing_contracts = sorted(set(frame["contract"]) - set(last_trade_dates.index))
        if missing_contracts:
            preview = ",".join(missing_contracts[:5])
            raise CffexCatalogError(f"CFFEX panel contracts missing from master: {preview}")

        if expiry_history is None:
            frame["_last_trade_date"] = frame["contract"].map(last_trade_dates)
            frame["_expiry_snapshot_date"] = pd.NA
            frame["_expiry_right_censored"] = frame["contract"].map(
                indexed_master["_expiry_right_censored"]
            )
            frame["_expiry_history_missing"] = False
            frame["_expiry_history_invalid"] = False
            observed_dte = (
                pd.to_datetime(frame["_last_trade_date"], format="%Y%m%d")
                - pd.to_datetime(frame["trade_date"], format="%Y%m%d")
            ).dt.days
            if observed_dte.lt(0).any():
                raise CffexCatalogError(
                    "CFFEX panel contains observations after last_trade_date"
                )
            frame["_dte"] = observed_dte.where(
                ~frame["_expiry_right_censored"], np.nan
            )
            right_censored_contracts = tuple(
                sorted(
                    master.loc[
                        master["_expiry_right_censored"], "contract"
                    ].tolist()
                )
            )
        else:
            history_missing = sorted(
                set(_EXPIRY_HISTORY_COLUMNS) - set(expiry_history.columns)
            )
            if history_missing:
                raise CffexCatalogError(
                    "CFFEX expiry history missing columns: "
                    + ",".join(history_missing)
                )
            history = (
                expiry_history.loc[:, list(_EXPIRY_HISTORY_COLUMNS)]
                .copy()
                .reset_index(drop=True)
            )
            try:
                history["snapshot_date"] = history["snapshot_date"].map(
                    lambda value: _normalize_date(value, "snapshot_date")
                )
                history["official_last_trade_date"] = history[
                    "official_last_trade_date"
                ].map(
                    lambda value: _normalize_date(
                        value, "official_last_trade_date"
                    )
                )
                history["contract"] = history["contract"].map(
                    _normalize_contract
                )
            except ValueError as exc:
                raise CffexCatalogError(str(exc)) from exc
            if history.duplicated(["snapshot_date", "contract"]).any():
                raise CffexCatalogError(
                    "CFFEX expiry history has duplicate snapshot_date/contract rows"
                )
            if (
                history["official_last_trade_date"]
                .lt(history["snapshot_date"])
                .any()
            ):
                raise CffexCatalogError(
                    "CFFEX official expiry precedes its history snapshot"
                )

            if history.empty:
                frame["_expiry_snapshot_date"] = pd.NA
                frame["_last_trade_date"] = pd.NA
            else:
                left = frame.loc[:, ["trade_date", "contract"]].copy()
                left["_row_order"] = np.arange(len(left), dtype=np.int64)
                left["_asof_key"] = left["trade_date"].astype(np.int64)
                right = history.rename(
                    columns={
                        "snapshot_date": "_expiry_snapshot_date",
                        "official_last_trade_date": "_last_trade_date",
                    }
                )
                right["_asof_key"] = right[
                    "_expiry_snapshot_date"
                ].astype(np.int64)
                # Vectorized exact-contract equivalent of query_exact_contract_asof.
                resolved = pd.merge_asof(
                    left.sort_values(
                        ["_asof_key", "contract"], kind="mergesort"
                    ),
                    right.sort_values(
                        ["_asof_key", "contract"], kind="mergesort"
                    ),
                    on="_asof_key",
                    by="contract",
                    direction="backward",
                    allow_exact_matches=True,
                ).sort_values("_row_order")
                frame["_expiry_snapshot_date"] = resolved[
                    "_expiry_snapshot_date"
                ].to_numpy()
                frame["_last_trade_date"] = resolved[
                    "_last_trade_date"
                ].to_numpy()

            frame["_expiry_right_censored"] = False
            frame["_expiry_history_missing"] = frame[
                "_last_trade_date"
            ].isna()
            observed_dte = (
                pd.to_datetime(
                    frame["_last_trade_date"],
                    format="%Y%m%d",
                    errors="coerce",
                )
                - pd.to_datetime(frame["trade_date"], format="%Y%m%d")
            ).dt.days
            frame["_expiry_history_invalid"] = (
                ~frame["_expiry_history_missing"] & observed_dte.lt(0)
            )
            frame["_dte"] = observed_dte.where(
                ~frame["_expiry_history_missing"]
                & ~frame["_expiry_history_invalid"],
                np.nan,
            )
            right_censored_contracts = ()

        self._daily = frame.set_index(["trade_date", "contract"], drop=False).sort_index()
        self._available_dates = tuple(sorted(frame["trade_date"].unique().tolist()))
        self._date_set = frozenset(self._available_dates)
        self._next_dates = dict(zip(self._available_dates, self._available_dates[1:]))
        self._product_date_ranges = {
            str(product): (
                str(group["trade_date"].min()),
                str(group["trade_date"].max()),
            )
            for product, group in frame.groupby("product", sort=True)
        }
        self._panel_horizon = normalized_horizon
        self._right_censored_contracts = right_censored_contracts
        self._unresolved_expiry_contracts = tuple(
            sorted(
                frame.loc[
                    frame["_expiry_history_missing"]
                    | frame["_expiry_history_invalid"],
                    "contract",
                ]
                .astype(str)
                .unique()
                .tolist()
            )
        )
        self._expiry_mode = expiry_mode
        self._expiry_manifest = (
            dict(expiry_manifest) if expiry_manifest is not None else None
        )
        self._panel_manifest = dict(panel_manifest) if panel_manifest is not None else None
        self._master_manifest = dict(master_manifest) if master_manifest is not None else None

    @property
    def available_dates(self) -> tuple[str, ...]:
        return self._available_dates

    @property
    def product_date_ranges(self) -> Mapping[str, tuple[str, str]]:
        return dict(self._product_date_ranges)

    @property
    def available_trade_dates(self) -> tuple[str, ...]:
        return self._available_dates

    @property
    def panel_horizon(self) -> str | None:
        return self._panel_horizon

    @property
    def right_censored_contracts(self) -> tuple[str, ...]:
        return self._right_censored_contracts

    @property
    def unresolved_expiry_contracts(self) -> tuple[str, ...]:
        return self._unresolved_expiry_contracts

    @property
    def expiry_mode(self) -> str:
        return self._expiry_mode

    @property
    def official_expiry_manifest(self) -> dict[str, object] | None:
        return None if self._expiry_manifest is None else dict(self._expiry_manifest)

    @property
    def panel_manifest(self) -> dict[str, object] | None:
        return None if self._panel_manifest is None else dict(self._panel_manifest)

    @property
    def master_manifest(self) -> dict[str, object] | None:
        return None if self._master_manifest is None else dict(self._master_manifest)

    def next_trading_date(self, signal_date: object) -> str:
        normalized = _normalize_date(signal_date, "signal_date")
        if normalized not in self._date_set:
            raise CffexCatalogError(f"signal date is absent from CFFEX panel: {normalized}")
        try:
            return self._next_dates[normalized]
        except KeyError as exc:
            raise CffexCatalogError(f"no next CFFEX trading date after {normalized}") from exc

    def _rows_on(self, signal_date: object) -> tuple[str, pd.DataFrame]:
        normalized = _normalize_date(signal_date, "signal_date")
        if normalized not in self._date_set:
            raise CffexCatalogError(f"signal date is absent from CFFEX panel: {normalized}")
        rows = self._daily.xs(normalized, level="trade_date", drop_level=False).reset_index(drop=True)
        return normalized, rows

    @staticmethod
    def _liquidity_mask(rows: pd.DataFrame) -> pd.Series:
        return (
            np.isfinite(rows["open_interest"])
            & rows["open_interest"].ge(0.0)
            & np.isfinite(rows["volume"])
            & rows["volume"].gt(0.0)
        )

    @staticmethod
    def _positive_settlement_mask(rows: pd.DataFrame) -> pd.Series:
        return rows["settlement_mark_valid"] & np.isfinite(rows["settle"]) & rows["settle"].gt(0.0)

    def _raise_if_expiry_history_unavailable(
        self,
        otherwise_eligible: pd.DataFrame,
        *,
        description: str,
        signal_date: str,
    ) -> None:
        unavailable = otherwise_eligible.loc[
            otherwise_eligible["_expiry_history_missing"]
            | otherwise_eligible["_expiry_history_invalid"]
        ]
        if unavailable.empty:
            return
        contracts = sorted(unavailable["contract"].astype(str).unique())
        preview = ",".join(contracts[:5])
        if len(contracts) > 5:
            preview += f" (+{len(contracts) - 5} more)"
        raise CffexCatalogError(
            f"{description} on {signal_date} lacks usable exact-contract as-of expiry "
            f"history for {preview}; only snapshot_date <= {signal_date} is visible, "
            "and master dates, product substitutes, and future snapshots are not used"
        )

    def _raise_if_right_censored(
        self,
        otherwise_eligible: pd.DataFrame,
        *,
        description: str,
        signal_date: str,
    ) -> None:
        censored = otherwise_eligible.loc[otherwise_eligible["_expiry_right_censored"]]
        if censored.empty:
            return
        censored_contracts = sorted(censored["contract"].astype(str).unique())
        contracts = ",".join(censored_contracts[:5])
        if len(censored_contracts) > 5:
            contracts += f" (+{len(censored_contracts) - 5} more)"
        raise RightCensoredExpiryError(
            f"{description} on {signal_date} has right-censored expiry at panel horizon "
            f"{self._panel_horizon}; last_trade_date is an observation boundary, so DTE is unknown "
            f"for {contracts}"
        )

    def select_future(
        self,
        product: object,
        signal_date: object,
        *,
        min_dte: int = 0,
    ) -> FuturesSelection:
        normalized_product = _normalize_product(product, FUTURES_PRODUCTS)
        minimum = _nonnegative_int(min_dte, "min_dte")
        normalized_date, rows = self._rows_on(signal_date)
        otherwise_eligible = rows.loc[
            rows["product"].eq(normalized_product)
            & rows["instrument_type"].eq("future")
            & self._liquidity_mask(rows)
            & self._positive_settlement_mask(rows)
        ].copy()
        self._raise_if_expiry_history_unavailable(
            otherwise_eligible,
            description=f"no selectable {normalized_product} future",
            signal_date=normalized_date,
        )
        candidates = otherwise_eligible.loc[
            ~otherwise_eligible["_expiry_right_censored"]
            & otherwise_eligible["_dte"].ge(minimum)
        ].copy()
        if candidates.empty:
            self._raise_if_right_censored(
                otherwise_eligible,
                description=f"no selectable {normalized_product} future",
                signal_date=normalized_date,
            )
            raise CffexCatalogError(
                f"no valid {normalized_product} future on {normalized_date} with DTE >= {minimum}"
            )
        selected = candidates.sort_values(
            ["open_interest", "volume", "contract"],
            ascending=[False, False, True],
            kind="mergesort",
        ).iloc[0]
        return FuturesSelection(
            signal_date=normalized_date,
            product=normalized_product,
            contract=str(selected["contract"]),
            last_trade_date=str(selected["_last_trade_date"]),
            dte=int(selected["_dte"]),
            multiplier=float(selected["multiplier"]),
            open_interest=float(selected["open_interest"]),
            volume=float(selected["volume"]),
            settle=float(selected["settle"]),
            expiry_snapshot_date=_optional_text(
                selected["_expiry_snapshot_date"]
            ),
        )

    def select_option(
        self,
        product: object,
        signal_date: object,
        *,
        option_type: object,
        target_abs_delta: float,
        min_dte: int = 0,
        max_dte: int = 365,
    ) -> OptionSelection:
        normalized_product = _normalize_product(product, OPTION_PRODUCTS)
        normalized_type = _normalize_option_type(option_type)
        minimum = _nonnegative_int(min_dte, "min_dte")
        maximum = _nonnegative_int(max_dte, "max_dte")
        if minimum > maximum:
            raise ValueError("min_dte must not exceed max_dte")
        try:
            target = float(target_abs_delta)
        except (TypeError, ValueError) as exc:
            raise ValueError("target_abs_delta must be between 0 and 1") from exc
        if not math.isfinite(target) or not 0.0 <= target <= 1.0:
            raise ValueError("target_abs_delta must be between 0 and 1")

        normalized_date, rows = self._rows_on(signal_date)
        otherwise_eligible = rows.loc[
            rows["product"].eq(normalized_product)
            & rows["instrument_type"].eq("option")
            & rows["option_type"].eq(normalized_type)
            & np.isfinite(rows["delta"])
            & self._liquidity_mask(rows)
        ].copy()
        self._raise_if_expiry_history_unavailable(
            otherwise_eligible,
            description=f"no selectable {normalized_product} {normalized_type}",
            signal_date=normalized_date,
        )
        candidates = otherwise_eligible.loc[
            ~otherwise_eligible["_expiry_right_censored"]
            & otherwise_eligible["_dte"].between(minimum, maximum, inclusive="both")
        ].copy()
        if candidates.empty:
            self._raise_if_right_censored(
                otherwise_eligible,
                description=f"no selectable {normalized_product} {normalized_type}",
                signal_date=normalized_date,
            )
            raise CffexCatalogError(
                f"no valid {normalized_product} {normalized_type} on {normalized_date} "
                f"with DTE in [{minimum}, {maximum}]"
            )
        candidates["_delta_distance"] = (candidates["delta"].abs() - target).abs()
        selected = candidates.sort_values(
            ["_delta_distance", "open_interest", "volume", "contract"],
            ascending=[True, False, False, True],
            kind="mergesort",
        ).iloc[0]
        return OptionSelection(
            signal_date=normalized_date,
            product=normalized_product,
            contract=str(selected["contract"]),
            option_type=normalized_type,
            last_trade_date=str(selected["_last_trade_date"]),
            dte=int(selected["_dte"]),
            multiplier=float(selected["multiplier"]),
            strike=float(selected["strike"]),
            delta=float(selected["delta"]),
            target_abs_delta=target,
            delta_distance=float(selected["_delta_distance"]),
            open_interest=float(selected["open_interest"]),
            volume=float(selected["volume"]),
            expiry_snapshot_date=_optional_text(
                selected["_expiry_snapshot_date"]
            ),
        )

    def _exact_row(self, contract: object, trade_date: object) -> tuple[str, str, pd.Series]:
        normalized_contract = _normalize_contract(contract)
        normalized_date = _normalize_date(trade_date, "trade_date")
        try:
            row = self._daily.loc[(normalized_date, normalized_contract)]
        except KeyError as exc:
            raise CffexCatalogError(
                f"missing exact CFFEX row for {normalized_contract} on {normalized_date}"
            ) from exc
        return normalized_contract, normalized_date, row

    def next_open(self, contract: object, signal_date: object) -> OpenExecution:
        normalized_signal = _normalize_date(signal_date, "signal_date")
        execution_date = self.next_trading_date(normalized_signal)
        normalized_contract, _, row = self._exact_row(contract, execution_date)
        open_price = _optional_finite(row["open"])
        volume = _optional_finite(row["volume"])
        if (
            not bool(row["open_executable"])
            or open_price is None
            or open_price <= 0.0
            or volume is None
            or volume <= 0.0
        ):
            raise CffexCatalogError(
                f"next open is not executable for {normalized_contract} on {execution_date}"
            )
        return OpenExecution(
            signal_date=normalized_signal,
            execution_date=execution_date,
            contract=normalized_contract,
            open_price=open_price,
            volume=volume,
        )

    def next_open_price(self, contract: object, signal_date: object) -> float:
        return self.next_open(contract, signal_date).open_price

    def settlement(self, contract: object, trade_date: object) -> float:
        normalized_contract, normalized_date, row = self._exact_row(contract, trade_date)
        settle = _optional_finite(row["settle"])
        if not bool(row["settlement_mark_valid"]) or settle is None or settle < 0.0:
            raise CffexCatalogError(
                f"official settlement is invalid for {normalized_contract} on {normalized_date}"
            )
        return settle

    def last_trade_date(self, contract: object, trade_date: object) -> str:
        """Return the exact-contract expiry visible as of the requested trade date."""

        normalized_contract, normalized_date, row = self._exact_row(contract, trade_date)
        if bool(row["_expiry_history_missing"]) or bool(
            row["_expiry_history_invalid"]
        ):
            raise CffexCatalogError(
                "exact-contract as-of expiry history is unavailable for "
                f"{normalized_contract} on {normalized_date}"
            )
        if bool(row["_expiry_right_censored"]):
            raise RightCensoredExpiryError(
                f"right-censored expiry for {normalized_contract} on {normalized_date} "
                f"at panel horizon {self._panel_horizon}"
            )
        expiry = _optional_text(row["_last_trade_date"])
        if expiry is None:
            raise CffexCatalogError(
                f"official last trade date is unavailable for {normalized_contract} "
                f"on {normalized_date}"
            )
        return expiry

    def curve_snapshot(
        self,
        product: object,
        signal_date: object,
        *,
        min_dte: int = 0,
    ) -> tuple[CurvePoint, ...]:
        normalized_product = _normalize_product(product, FUTURES_PRODUCTS)
        minimum = _nonnegative_int(min_dte, "min_dte")
        normalized_date, rows = self._rows_on(signal_date)
        otherwise_eligible = rows.loc[
            rows["product"].eq(normalized_product)
            & rows["instrument_type"].eq("future")
            & self._positive_settlement_mask(rows)
        ].copy()
        self._raise_if_expiry_history_unavailable(
            otherwise_eligible,
            description=f"no selectable {normalized_product} settlement curve",
            signal_date=normalized_date,
        )
        candidates = otherwise_eligible.loc[
            ~otherwise_eligible["_expiry_right_censored"]
            & otherwise_eligible["_dte"].ge(minimum)
        ].sort_values(["_last_trade_date", "contract"], kind="mergesort")
        if candidates.empty:
            self._raise_if_right_censored(
                otherwise_eligible,
                description=f"no selectable {normalized_product} settlement curve",
                signal_date=normalized_date,
            )
            raise CffexCatalogError(
                f"no valid {normalized_product} settlement curve on {normalized_date} with DTE >= {minimum}"
            )
        return tuple(
            CurvePoint(
                signal_date=normalized_date,
                product=normalized_product,
                contract=str(row["contract"]),
                last_trade_date=str(row["_last_trade_date"]),
                dte=int(row["_dte"]),
                settle=float(row["settle"]),
                open_interest=_optional_finite(row["open_interest"]),
                volume=_optional_finite(row["volume"]),
                expiry_snapshot_date=_optional_text(
                    row["_expiry_snapshot_date"]
                ),
            )
            for _, row in candidates.iterrows()
        )

    def front_next_carry(
        self,
        product: object,
        signal_date: object,
        *,
        min_dte: int = 0,
    ) -> FrontNextCarry:
        curve = self.curve_snapshot(product, signal_date, min_dte=min_dte)
        front = curve[0]
        next_point = next((point for point in curve[1:] if point.dte > front.dte), None)
        if next_point is None:
            raise CffexCatalogError(
                f"front/next carry needs two distinct expiries for {front.product} on {front.signal_date}"
            )
        tenor_days = next_point.dte - front.dte
        settlement_spread = next_point.settle - front.settle
        relative_spread = next_point.settle / front.settle - 1.0
        annualized_carry = (front.settle / next_point.settle - 1.0) * 365.0 / tenor_days
        return FrontNextCarry(
            signal_date=front.signal_date,
            product=front.product,
            front=front,
            next=next_point,
            tenor_days=tenor_days,
            settlement_spread=settlement_spread,
            relative_spread=relative_spread,
            annualized_carry=annualized_carry,
        )

    select_futures_contract = select_future
    select_option_contract = select_option
    next_open_execution = next_open
    settlement_price = settlement
    futures_curve = curve_snapshot


__all__ = [
    "CffexCatalog",
    "CffexCatalogError",
    "CurvePoint",
    "FrontNextCarry",
    "FuturesSelection",
    "OpenExecution",
    "OptionSelection",
    "RightCensoredExpiryError",
]

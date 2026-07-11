from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pandas as pd
import pytest

from quant_proof.cffex_execution_parameters import (
    CONSERVATIVE_MAINTENANCE_POLICY,
    EVIDENCE_LABEL,
    CffexExecutionParameterError,
    CffexExecutionParameterSchedule,
    UnsupportedFeeUnitError,
)
from quant_proof.free_sources.cffex_settlement_params import (
    CffexLookupError,
    ShortOptionsDisabledError,
)


REAL_CANONICAL = Path(
    "/Volumes/PSSD1TB/量化数据/processed/phase3_derivatives/"
    "cffex_settlement_params.parquet"
)


def _row(
    snapshot_date: str,
    key: str,
    *,
    instrument_type: str = "future",
    product: str | None = None,
    long_margin_rate: float | None = 0.12,
    short_margin_rate: float | None = 0.14,
    trading_fee_value: float = 0.000023,
    trading_fee_unit: str = "notional_rate",
    settlement_fee_value: float = 0.0001,
    settlement_fee_unit: str = "notional_rate",
    close_today_fee_multiplier: float = 10.0,
    title_matches: bool = True,
) -> dict[str, object]:
    option = instrument_type == "option"
    return {
        "snapshot_date": snapshot_date,
        "instrument_type": instrument_type,
        "parameter_scope": "series" if option else "contract",
        "contract_or_series": key,
        "product": product or key[:2],
        "long_margin_rate": None if option else long_margin_rate,
        "short_margin_rate": None if option else short_margin_rate,
        "trading_fee_value": trading_fee_value,
        "trading_fee_unit": trading_fee_unit,
        "settlement_fee_value": settlement_fee_value,
        "settlement_fee_unit": settlement_fee_unit,
        "settlement_fee_kind": "exercise" if option else "delivery",
        "close_today_fee_multiplier": close_today_fee_multiplier,
        "close_today_fee_semantics": "fraction_of_trading_fee",
        "option_shorting_enabled": False,
        "source_section_title_matches_snapshot": title_matches,
        "source_sha256": ("b" if snapshot_date == "20240103" else "a") * 64,
    }


@pytest.fixture
def schedule(tmp_path: Path) -> CffexExecutionParameterSchedule:
    rows = [
        _row("20240101", "IF2401", title_matches=False),
        _row(
            "20240103",
            "IF2401",
            long_margin_rate=0.15,
            short_margin_rate=0.17,
        ),
        _row(
            "20240101",
            "IH2401",
            trading_fee_value=3.0,
            trading_fee_unit="currency_per_contract",
            settlement_fee_value=2.5,
            settlement_fee_unit="currency_per_contract",
            close_today_fee_multiplier=1.0,
        ),
        _row(
            "20240101",
            "IO2401",
            instrument_type="option",
            trading_fee_value=15.0,
            trading_fee_unit="currency_per_contract",
            settlement_fee_value=1.0,
            settlement_fee_unit="currency_per_contract",
            close_today_fee_multiplier=1.0,
        ),
    ]
    path = tmp_path / "cffex_settlement_params.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return CffexExecutionParameterSchedule(path, validate=False)


def test_exact_asof_lookup_is_cached_and_returns_frozen_evidence(
    schedule: CffexExecutionParameterSchedule,
) -> None:
    parameters = schedule.lookup("IF2401", "2024-01-02")
    assert parameters is schedule.lookup("if2401", "20240102")
    assert parameters.source_snapshot_date == "20240101"
    assert parameters.initial_margin_rate == pytest.approx(0.12)
    assert parameters.section_title_mismatch
    assert parameters.source_sha256 == "a" * 64
    assert parameters.evidence_label == EVIDENCE_LABEL
    assert schedule.cache_info().hits >= 1
    with pytest.raises(FrozenInstanceError):
        parameters.initial_margin_rate = 0.01  # type: ignore[misc]


def test_futures_long_and_short_margin_use_conservative_same_rate(
    schedule: CffexExecutionParameterSchedule,
) -> None:
    long = schedule.lookup("IF2401", "20240103", position_side="long")
    short = schedule.lookup("IF2401", "20240103", side="short")

    assert long.long_margin_rate == pytest.approx(0.15)
    assert long.short_margin_rate == pytest.approx(0.17)
    assert long.initial_margin_rate == long.maintenance_margin_rate == pytest.approx(0.15)
    assert short.initial_margin_rate == short.maintenance_margin_rate == pytest.approx(0.17)
    assert long.maintenance_margin_policy == CONSERVATIVE_MAINTENANCE_POLICY
    assert short.maintenance_margin_basis == CONSERVATIVE_MAINTENANCE_POLICY


def test_option_contract_extracts_exact_series_and_long_has_no_margin(
    schedule: CffexExecutionParameterSchedule,
) -> None:
    parameters = schedule.lookup("IO2401-C-3500", "20240102")
    assert parameters.contract == "IO2401-C-3500"
    assert parameters.parameter_key == "IO2401"
    assert parameters.instrument_type == "option"
    assert parameters.initial_margin_rate is None
    assert parameters.maintenance_margin_rate is None
    assert parameters.long_margin_rate is None
    assert parameters.short_margin_rate is None
    assert parameters.settlement_fee_kind == "exercise"


def test_fee_amount_supports_notional_and_per_contract_units(
    schedule: CffexExecutionParameterSchedule,
) -> None:
    assert schedule.fee_amount(
        "IF2401", "20240102", 2, 4000.0, 300.0, "open"
    ) == pytest.approx(55.2)
    assert schedule.fee_amount(
        "IH2401", "20240102", 3, 2500.0, 300.0, "open"
    ) == pytest.approx(9.0)
    assert schedule.settlement_fee_amount(
        "IF2401", "20240102", 2, 4000.0, 300.0
    ) == pytest.approx(240.0)
    assert schedule.settlement_fee_amount(
        "IH2401", "20240102", 3, 2500.0, 300.0
    ) == pytest.approx(7.5)


def test_same_day_close_applies_close_today_multiplier_only_that_day(
    schedule: CffexExecutionParameterSchedule,
) -> None:
    regular = schedule.fee_amount(
        "IF2401",
        "20240102",
        2,
        4000.0,
        300.0,
        "close",
        opened_date="20240101",
    )
    same_day = schedule.fee_amount(
        "IF2401",
        "20240102",
        2,
        4000.0,
        300.0,
        "close",
        opened_date="20240102",
    )
    assert regular == pytest.approx(55.2)
    assert same_day == pytest.approx(552.0)


def test_no_future_backfill_or_product_substitution(
    schedule: CffexExecutionParameterSchedule,
) -> None:
    with pytest.raises(CffexLookupError, match="future backfill"):
        schedule.lookup("IF2401", "20231231")
    with pytest.raises(CffexLookupError, match="product substitution"):
        schedule.lookup("IF", "20240103")
    with pytest.raises(CffexLookupError, match="product substitution"):
        schedule.lookup("IF2402", "20240103")
    with pytest.raises(CffexLookupError, match="product substitution"):
        schedule.lookup("IO2402-C-3500", "20240103")


def test_short_options_are_rejected_for_lookup_and_fees(
    schedule: CffexExecutionParameterSchedule,
) -> None:
    with pytest.raises(ShortOptionsDisabledError, match="short options are disabled"):
        schedule.lookup("IO2401-P-3500", "20240102", side="short")
    with pytest.raises(ShortOptionsDisabledError, match="short options are disabled"):
        schedule.fee_amount(
            "IO2401-P-3500", "20240102", 1, 20.0, 100.0, "sell_to_open"
        )


@pytest.mark.parametrize("contracts", [0, -1, 1.0, 1.5, True])
def test_contract_count_must_be_strictly_positive_whole(
    schedule: CffexExecutionParameterSchedule,
    contracts: object,
) -> None:
    with pytest.raises(CffexExecutionParameterError, match="positive whole"):
        schedule.fee_amount("IF2401", "20240102", contracts, 4000.0, 300.0, "open")


@pytest.mark.parametrize(
    ("price", "multiplier"),
    [(0.0, 300.0), (-1.0, 300.0), (float("nan"), 300.0), (4000.0, 0.0)],
)
def test_fee_inputs_must_be_positive_and_finite(
    schedule: CffexExecutionParameterSchedule,
    price: float,
    multiplier: float,
) -> None:
    with pytest.raises(CffexExecutionParameterError, match="positive|finite"):
        schedule.fee_amount("IF2401", "20240102", 1, price, multiplier, "open")


def test_close_dates_and_unknown_units_fail_closed(
    schedule: CffexExecutionParameterSchedule,
    tmp_path: Path,
) -> None:
    with pytest.raises(CffexExecutionParameterError, match="opened_date is required"):
        schedule.fee_amount("IF2401", "20240102", 1, 4000.0, 300.0, "close")
    with pytest.raises(CffexExecutionParameterError, match="cannot be later"):
        schedule.fee_amount(
            "IF2401", "20240102", 1, 4000.0, 300.0, "close", "20240103"
        )

    row = _row("20240101", "IC2401", trading_fee_unit="mystery_unit")
    path = tmp_path / "unknown_unit.parquet"
    pd.DataFrame([row]).to_parquet(path, index=False)
    unknown = CffexExecutionParameterSchedule(path, validate=False)
    with pytest.raises(UnsupportedFeeUnitError, match="fails closed|unsupported"):
        unknown.fee_amount("IC2401", "20240102", 1, 4000.0, 200.0, "open")


@pytest.mark.skipif(not REAL_CANONICAL.is_file(), reason="real canonical parquet is absent")
def test_real_canonical_can_be_validated_and_read_lightly() -> None:
    schedule = CffexExecutionParameterSchedule(
        REAL_CANONICAL,
        validate=True,
        verify_sources=False,
    )
    parameters = schedule.lookup("IF1005", "20100414")
    assert schedule.rows_loaded > 0
    assert parameters.parameter_key == "IF1005"
    assert parameters.source_snapshot_date == "20100414"
    assert parameters.evidence_label == EVIDENCE_LABEL

from __future__ import annotations

import pandas as pd
import pytest

from quant_proof.cffex_catalog import CffexCatalog
from quant_proof.phase3_derivative_signals import (
    FuturesDirectionRule,
    resolve_futures_direction_rule,
)


def _row(
    trade_date: str,
    contract: str,
    settle: float,
    *,
    open_interest: float,
    volume: float = 100.0,
) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "contract": contract,
        "product": "IF",
        "instrument_type": "future",
        "option_type": "",
        "strike": None,
        "multiplier": 300.0,
        "open": settle,
        "settle": settle,
        "volume": volume,
        "open_interest": open_interest,
        "delta": None,
        "open_executable": True,
        "settlement_mark_valid": True,
    }


def _catalog(
    rows: list[dict[str, object]],
    expiries: dict[str, str],
    *,
    expiry_history: pd.DataFrame | None = None,
) -> CffexCatalog:
    master = pd.DataFrame(
        [
            {"contract": contract, "last_trade_date": expiry}
            for contract, expiry in expiries.items()
        ]
    )
    return CffexCatalog.from_frames(
        pd.DataFrame(rows),
        master,
        expiry_history=expiry_history,
    )


def _rolling_catalog() -> CffexCatalog:
    dates = ("20260105", "20260106", "20260107")
    old_settles = (100.0, 110.0, 121.0)
    new_settles = (200.0, 220.0, 242.0)
    rows: list[dict[str, object]] = []
    for index, trade_date in enumerate(dates):
        rows.extend(
            [
                _row(
                    trade_date,
                    "IF2603",
                    old_settles[index],
                    open_interest=1_000.0 if index == 0 else 100.0,
                ),
                _row(
                    trade_date,
                    "IF2604",
                    new_settles[index],
                    open_interest=100.0 if index == 0 else 1_000.0,
                ),
            ]
        )
    return _catalog(
        rows,
        {"IF2603": "20260320", "IF2604": "20260417"},
    )


def test_continuous_return_uses_prior_selected_contract_across_roll() -> None:
    resolution = resolve_futures_direction_rule(
        _rolling_catalog(),
        "IF",
        FuturesDirectionRule(
            kind="time_series_momentum",
            position_mode="long_short_flat",
            lookback_days=1,
        ),
        min_dte=0,
    )

    first, roll_date, after_roll = resolution.observations
    assert first.selected_contract == "IF2603"
    assert roll_date.selected_contract == "IF2604"
    assert roll_date.return_contract == "IF2603"
    assert after_roll.return_contract == "IF2604"
    assert roll_date.continuous_return == pytest.approx(0.10)
    assert after_roll.continuous_return == pytest.approx(0.10)
    assert after_roll.continuous_index == pytest.approx(1.21)


def test_trend_rules_are_flat_until_their_warmup_is_complete() -> None:
    catalog = _rolling_catalog()
    momentum = resolve_futures_direction_rule(
        catalog,
        "IF",
        FuturesDirectionRule(
            kind="time_series_momentum",
            position_mode="long_short_flat",
            lookback_days=2,
        ),
        min_dte=0,
    )
    moving_average = resolve_futures_direction_rule(
        catalog,
        "IF",
        FuturesDirectionRule(
            kind="moving_average_or_breakout",
            position_mode="long_flat",
            trend_variant="moving_average",
            fast_window=1,
            slow_window=3,
        ),
        min_dte=0,
    )

    assert [row.direction for row in momentum.observations] == [
        "flat",
        "flat",
        "long",
    ]
    assert [row.direction for row in moving_average.observations] == [
        "flat",
        "flat",
        "long",
    ]


def test_breakout_rule_uses_the_roll_safe_continuous_index() -> None:
    resolution = resolve_futures_direction_rule(
        _rolling_catalog(),
        "IF",
        FuturesDirectionRule(
            kind="moving_average_or_breakout",
            position_mode="long_short_flat",
            trend_variant="breakout",
            lookback_days=1,
        ),
        min_dte=0,
    )

    assert [row.direction for row in resolution.observations] == [
        "flat",
        "long",
        "long",
    ]


def test_front_next_carry_maps_backwardation_long_and_contango_short() -> None:
    dates = ("20260105", "20260106")
    rows = [
        _row(dates[0], "IF2603", 105.0, open_interest=1_000.0),
        _row(dates[0], "IF2604", 100.0, open_interest=500.0),
        _row(dates[1], "IF2603", 95.0, open_interest=1_000.0),
        _row(dates[1], "IF2604", 100.0, open_interest=500.0),
    ]
    catalog = _catalog(
        rows,
        {"IF2603": "20260320", "IF2604": "20260417"},
    )
    long_short = resolve_futures_direction_rule(
        catalog,
        "IF",
        FuturesDirectionRule(
            kind="front_next_carry",
            position_mode="long_short_flat",
        ),
        min_dte=0,
    )
    long_flat = resolve_futures_direction_rule(
        catalog,
        "IF",
        FuturesDirectionRule(
            kind="front_next_carry",
            position_mode="long_flat",
        ),
        min_dte=0,
    )

    assert [row.direction for row in long_short.observations] == ["long", "short"]
    assert [row.direction for row in long_flat.observations] == ["long", "flat"]


def test_future_panel_and_expiry_rows_cannot_change_past_directions() -> None:
    dates = tuple(f"202601{day:02d}" for day in range(5, 11))
    base_rows = [
        _row(
            trade_date,
            "IF2606",
            100.0 + index,
            open_interest=1_000.0,
        )
        for index, trade_date in enumerate(dates)
    ]
    base_history = pd.DataFrame(
        [
            {
                "snapshot_date": dates[0],
                "contract": "IF2606",
                "official_last_trade_date": "20260619",
            },
            {
                "snapshot_date": dates[4],
                "contract": "IF2606",
                "official_last_trade_date": "20260619",
            },
        ]
    )
    changed_rows = [dict(row) for row in base_rows]
    for row in changed_rows[4:]:
        row["settle"] = float(row["settle"]) * 10.0
        row["volume"] = 1.0
        row["open_interest"] = 1.0
    changed_history = base_history.copy()
    changed_history.loc[1, "official_last_trade_date"] = "20260717"
    rule = FuturesDirectionRule(
        kind="time_series_momentum",
        position_mode="long_short_flat",
        lookback_days=2,
    )

    original = resolve_futures_direction_rule(
        _catalog(
            base_rows,
            {"IF2606": "20260619"},
            expiry_history=base_history,
        ),
        "IF",
        rule,
        min_dte=0,
    )
    changed = resolve_futures_direction_rule(
        _catalog(
            changed_rows,
            {"IF2606": "20260619"},
            expiry_history=changed_history,
        ),
        "IF",
        rule,
        min_dte=0,
    )

    cutoff = dates[3]
    assert {
        key: value
        for key, value in original.direction_by_signal_date.items()
        if key <= cutoff
    } == {
        key: value
        for key, value in changed.direction_by_signal_date.items()
        if key <= cutoff
    }

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_proof.real_strategies import (
    RealStockStrategySpec,
    compute_real_stock_scores,
    s30_real_idiosyncratic_strength_scores,
    target_symbols_by_signal_date,
)
from quant_proof.search_manager import (
    build_search_strategy_specs,
    expand_parameter_neighbors,
    load_search_config,
    parse_search_stages,
)


def _prepared_panel(
    profiles: dict[str, tuple[float, float, float]],
    periods: int = 140,
) -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-01", periods=periods)
    market_return = np.resize(np.array([0.012, -0.008], dtype=float), periods)
    orthogonal_noise = np.resize(np.array([1.0, 1.0, -1.0, -1.0]), periods)
    anchors = {
        f"90000{index}.SH": (1.0, 0.0, 0.0)
        for index in range(1, 6)
    }
    rows: list[dict[str, object]] = []
    for symbol, (beta, residual_drift, residual_noise) in sorted(
        {**anchors, **profiles}.items()
    ):
        daily_residual = residual_drift + residual_noise * orthogonal_noise
        signal_return = beta * market_return + daily_residual
        signal_price = 10.0 * np.cumprod(1.0 + signal_return)
        for index, trade_date in enumerate(dates):
            rows.append(
                {
                    "trade_date": trade_date.strftime("%Y%m%d"),
                    "ts_code": symbol,
                    "signal_return": signal_return[index],
                    "signal_price": signal_price[index],
                    "avg_amount_20d": 100_000_000.0,
                    "is_tradeable": True,
                    "is_suspended": False,
                    "is_st": False,
                    "listing_days": 1_000 + index,
                    "close": signal_price[index],
                }
            )
    return pd.DataFrame(rows)


def _spec(**overrides: object) -> RealStockStrategySpec:
    params: dict[str, object] = {
        "kind": "real_idiosyncratic_strength",
        "beta_window": 20,
        "strength_window": 20,
        "skip_recent": 4,
        "residual_vol_window": 20,
        "residual_vol_penalty": 1.0,
        "min_residual_momentum": 1e-10,
        "holding_k": 2,
        "rebalance": "weekly",
        "min_listing_days": 0,
        "min_price": 0.0,
        "min_avg_amount_20d": 1.0,
        "exclude_st": True,
    }
    params.update(overrides)
    return RealStockStrategySpec(
        name="S30_test",
        family="S30_real_idiosyncratic_strength",
        params=params,
    )


def _score_row(scores: pd.DataFrame, trade_date: str, symbol: str) -> pd.Series:
    return scores.loc[
        scores["trade_date"].eq(trade_date) & scores["ts_code"].eq(symbol)
    ].iloc[0]


def test_positive_rolling_beta_residual_strength_has_the_right_direction() -> None:
    positive = "000001.SZ"
    negative = "000002.SZ"
    systematic = "000003.SZ"
    panel = _prepared_panel(
        {
            positive: (2.0, 0.0020, 0.0004),
            negative: (0.5, -0.0020, 0.0004),
            systematic: (2.0, 0.0, 0.0004),
        }
    )
    spec = _spec()
    scores = compute_real_stock_scores(panel.copy(deep=True), spec)
    signal_date = str(scores["trade_date"].max())
    positive_row = _score_row(scores, signal_date, positive)
    negative_row = _score_row(scores, signal_date, negative)
    systematic_row = _score_row(scores, signal_date, systematic)

    expected_market = panel.loc[panel["trade_date"].eq(signal_date)].groupby(
        "trade_date"
    )["signal_return"].median().iloc[0]
    assert positive_row["market_return"] == pytest.approx(expected_market)
    assert positive_row["rolling_beta"] == pytest.approx(2.0, abs=1e-10)
    assert negative_row["rolling_beta"] == pytest.approx(0.5, abs=1e-10)
    assert positive_row["residual_momentum"] > 0.0
    assert negative_row["residual_momentum"] < 0.0
    assert np.isfinite(positive_row["rank_score"])
    assert positive_row["rank"] == 1.0
    assert pd.isna(negative_row["rank_score"])

    market_by_date = panel.groupby("trade_date", sort=False)["signal_return"].median()
    systematic_returns = panel.loc[panel["ts_code"].eq(systematic)].copy()
    fixed_beta_residual = systematic_returns["signal_return"] - systematic_returns[
        "trade_date"
    ].map(market_by_date)
    fixed_beta_momentum = fixed_beta_residual.shift(4).rolling(20).sum().iloc[-1]
    assert fixed_beta_momentum > 0.03
    assert systematic_row["residual_momentum"] == pytest.approx(0.0, abs=1e-12)
    assert pd.isna(systematic_row["rank_score"])

    first_valid_position = 20 + 4 + 20 - 1
    positive_history = scores.loc[scores["ts_code"].eq(positive)].reset_index(drop=True)
    assert positive_history.loc[: first_valid_position - 1, "rank_score"].isna().all()
    assert np.isfinite(positive_history.loc[first_valid_position, "rank_score"])
    with pytest.raises(ValueError, match="finite and non-negative"):
        compute_real_stock_scores(
            panel.copy(deep=True),
            _spec(min_residual_momentum=-0.01),
        )


def test_beta_is_lagged_and_future_perturbations_do_not_change_past_scores() -> None:
    symbol = "000001.SZ"
    panel = _prepared_panel(
        {
            symbol: (1.8, 0.0015, 0.0004),
            "000002.SZ": (0.7, -0.0010, 0.0004),
        }
    )
    spec = _spec()
    baseline = compute_real_stock_scores(panel.copy(deep=True), spec)
    dates = sorted(panel["trade_date"].unique())
    signal_date = dates[-15]
    baseline_row = _score_row(baseline, signal_date, symbol)

    current_panel = panel.copy(deep=True)
    current_mask = current_panel["trade_date"].eq(signal_date) & current_panel[
        "ts_code"
    ].eq(symbol)
    current_panel.loc[current_mask, "signal_return"] += 0.07
    current_scores = compute_real_stock_scores(current_panel, spec)
    current_row = _score_row(current_scores, signal_date, symbol)
    assert current_row["market_return"] == pytest.approx(baseline_row["market_return"])
    assert current_row["rolling_beta"] == pytest.approx(baseline_row["rolling_beta"])
    assert current_row["daily_residual"] == pytest.approx(
        baseline_row["daily_residual"] + 0.07
    )
    assert current_row["residual_momentum"] == pytest.approx(
        baseline_row["residual_momentum"]
    )

    future_panel = panel.copy(deep=True)
    future_panel.loc[future_panel["trade_date"].gt(signal_date), "signal_return"] = 0.25
    mutated = compute_real_stock_scores(future_panel, spec)
    score_columns = [
        "trade_date",
        "ts_code",
        "market_return",
        "rolling_beta",
        "daily_residual",
        "residual_momentum",
        "residual_volatility",
        "idiosyncratic_strength",
        "rank_score",
        "rank",
    ]
    through_signal = baseline["trade_date"].le(signal_date)
    pd.testing.assert_frame_equal(
        baseline.loc[through_signal, score_columns].reset_index(drop=True),
        mutated.loc[through_signal, score_columns].reset_index(drop=True),
    )


def test_skip_recent_excludes_the_current_residual_shock() -> None:
    symbol = "000001.SZ"
    panel = _prepared_panel(
        {
            symbol: (1.5, 0.0010, 0.0004),
            "000002.SZ": (0.8, -0.0010, 0.0004),
        }
    )
    signal_date = str(panel["trade_date"].max())
    shocked = panel.copy(deep=True)
    shock_mask = shocked["trade_date"].eq(signal_date) & shocked["ts_code"].eq(symbol)
    shocked.loc[shock_mask, "signal_return"] += 0.05

    skip_spec = _spec(skip_recent=5, residual_vol_penalty=0.0)
    baseline_skip = compute_real_stock_scores(panel.copy(deep=True), skip_spec)
    shocked_skip = compute_real_stock_scores(shocked.copy(deep=True), skip_spec)
    assert _score_row(shocked_skip, signal_date, symbol)[
        "idiosyncratic_strength"
    ] == pytest.approx(
        _score_row(baseline_skip, signal_date, symbol)["idiosyncratic_strength"]
    )

    no_skip_spec = _spec(skip_recent=0, residual_vol_penalty=0.0)
    baseline_no_skip = compute_real_stock_scores(panel.copy(deep=True), no_skip_spec)
    shocked_no_skip = compute_real_stock_scores(shocked, no_skip_spec)
    assert _score_row(shocked_no_skip, signal_date, symbol)[
        "idiosyncratic_strength"
    ] > _score_row(baseline_no_skip, signal_date, symbol)["idiosyncratic_strength"]


def test_residual_volatility_penalty_prefers_stable_strength() -> None:
    low_vol = "000001.SZ"
    high_vol = "000002.SZ"
    panel = _prepared_panel(
        {
            low_vol: (1.4, 0.0015, 0.0002),
            high_vol: (1.4, 0.0015, 0.0012),
        }
    )
    signal_date = str(panel["trade_date"].max())
    raw_scores = compute_real_stock_scores(
        panel.copy(deep=True),
        _spec(residual_vol_penalty=0.0),
    )
    penalized_scores = compute_real_stock_scores(panel.copy(deep=True), _spec())
    raw_low = _score_row(raw_scores, signal_date, low_vol)
    raw_high = _score_row(raw_scores, signal_date, high_vol)
    penalized_low = _score_row(penalized_scores, signal_date, low_vol)
    penalized_high = _score_row(penalized_scores, signal_date, high_vol)

    assert raw_low["residual_momentum"] == pytest.approx(raw_high["residual_momentum"])
    assert raw_low["idiosyncratic_strength"] == pytest.approx(
        raw_high["idiosyncratic_strength"]
    )
    assert penalized_low["residual_volatility"] < penalized_high["residual_volatility"]
    assert penalized_low["idiosyncratic_strength"] > penalized_high[
        "idiosyncratic_strength"
    ]
    assert penalized_low["rank"] == 1.0


def test_s30_dispatch_is_deterministic_and_ties_break_by_symbol() -> None:
    first_symbol = "000001.SZ"
    second_symbol = "000002.SZ"
    panel = _prepared_panel(
        {
            first_symbol: (1.3, 0.0010, 0.0004),
            second_symbol: (1.3, 0.0010, 0.0004),
        }
    )
    spec = _spec()
    direct = s30_real_idiosyncratic_strength_scores(panel.copy(deep=True), spec)
    dispatched = compute_real_stock_scores(panel.copy(deep=True), spec)
    repeated = compute_real_stock_scores(panel.copy(deep=True), spec)
    score_columns = [
        "trade_date",
        "ts_code",
        "market_return",
        "rolling_beta",
        "residual_momentum",
        "residual_volatility",
        "idiosyncratic_strength",
        "rank_score",
        "rank",
    ]
    pd.testing.assert_frame_equal(direct[score_columns], dispatched[score_columns])
    pd.testing.assert_frame_equal(dispatched[score_columns], repeated[score_columns])

    signal_date = str(dispatched["trade_date"].max())
    first_row = _score_row(dispatched, signal_date, first_symbol)
    second_row = _score_row(dispatched, signal_date, second_symbol)
    assert first_row["rank_score"] == pytest.approx(second_row["rank_score"])
    assert first_row["rank"] == 1.0
    assert second_row["rank"] == 2.0
    targets = target_symbols_by_signal_date(
        dispatched,
        spec,
        holding_k=1,
        end_date=signal_date,
    )
    assert targets[signal_date] == [first_symbol]


def test_search_config_is_exactly_budgeted_numeric_and_preregistered() -> None:
    config_path = ROOT / "config" / "phase3_idiosyncratic_strength_search.yaml"
    raw = load_search_config(config_path)
    first = build_search_strategy_specs(raw)
    second = build_search_strategy_specs(raw)
    family = "S30_real_idiosyncratic_strength"
    space = raw["strategy_spaces"][family]
    mechanism_dimensions = {"beta_window", "strength_window"}

    assert raw["sampling"]["method"] == "discrete_latin_hypercube"
    assert raw["sampling"]["budget_per_family"] == 24
    assert space["budget"] == 24
    assert len(first) == 24
    assert [(spec.name, spec.params) for spec in first] == [
        (spec.name, spec.params) for spec in second
    ]
    assert len({spec.name for spec in first}) == 24
    assert {spec.family for spec in first} == {family}
    assert set(space["parameters"]) == mechanism_dimensions
    assert set(space["ordered_parameters"]) == mechanism_dimensions
    assert np.prod([len(values) for values in space["parameters"].values()]) > 24
    assert all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for values in space["parameters"].values()
        for value in values
    )
    required_history = (
        max(space["parameters"]["beta_window"])
        + int(space["fixed"]["skip_recent"])
        + max(
            max(space["parameters"]["strength_window"]),
            int(space["fixed"]["residual_vol_window"]),
        )
    )
    assert space["fixed"]["lookback"] >= required_history

    stages = {stage.name: stage for stage in parse_search_stages(raw)}
    assert stages["screen"].max_windows <= 24
    assert stages["screen"].promote_per_family == 2
    assert stages["screen"].promote_global == 12
    assert stages["confirm"].expand_parameter_neighbors
    assert not stages["stress"].expand_parameter_neighbors

    center = first[0]
    neighbors = expand_parameter_neighbors([center], raw)
    changed_dimensions: set[str] = set()
    for neighbor in neighbors:
        if neighbor.name == center.name:
            continue
        changed = {
            key
            for key in set(center.params) | set(neighbor.params)
            if center.params.get(key) != neighbor.params.get(key)
        }
        assert len(changed) == 1
        assert changed <= mechanism_dimensions
        changed_dimensions.update(changed)
    assert changed_dimensions == mechanism_dimensions

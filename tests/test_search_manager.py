from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_proof.free_real_backtest import FreeRealBacktestConfig
from quant_proof.real_strategies import (
    RealStockStrategySpec,
    compute_real_stock_scores,
    stock_target_weights_by_signal_date,
    target_symbols_by_signal_date,
)
from quant_proof.search_manager import (
    ExperimentLedger,
    MultipleTestingRegistry,
    add_neighborhood_metrics,
    add_regime_metrics,
    behavior_id,
    build_search_strategy_specs,
    expand_parameter_neighbors,
    finalize_strategy_gates,
    holm_adjusted_pvalues,
    latin_hypercube_sample,
    load_search_config,
    parse_search_stages,
    promote_candidates,
    semantic_behavior_payload,
    stable_hash,
)


def test_stable_hash_ignores_mapping_order() -> None:
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})


def test_behavior_id_deduplicates_equal_effective_behavior_across_families() -> None:
    params = {
        "kind": "real_stateful_trend",
        "holding_k": 5,
        "entry_window": 55,
        "trailing_stop": 0.18,
    }
    anchor = RealStockStrategySpec(
        name="S20_named_variant",
        family="S20_real_stateful_trend",
        params={
            **params,
            "data_tier": "free_real",
            "family": "display-family-a",
            "margin_evidence_required": False,
            "name": "display-name-a",
        },
    )
    concentrated = RealStockStrategySpec(
        name="S22_other_name",
        family="S22_real_concentrated_trend",
        params={
            **params,
            "data_tier": "proxy_research",
            "family": "display-family-b",
            "margin_evidence_required": True,
            "name": "display-name-b",
        },
    )

    assert semantic_behavior_payload(anchor) == {"params": params}
    assert semantic_behavior_payload(anchor) == semantic_behavior_payload(concentrated)
    assert behavior_id(anchor) == behavior_id(concentrated)


def test_behavior_id_preserves_effective_parameter_changes() -> None:
    base = RealStockStrategySpec(
        name="base",
        family="S20_real_stateful_trend",
        params={"kind": "real_stateful_trend", "holding_k": 5, "data_tier": "free_real"},
    )
    changed_position = RealStockStrategySpec(
        name="changed",
        family="S22_real_concentrated_trend",
        params={"kind": "real_stateful_trend", "holding_k": 3, "data_tier": "free_real"},
    )
    changed_dispatch = RealStockStrategySpec(
        name="changed_kind",
        family="S22_real_concentrated_trend",
        params={"kind": "real_volatility_contraction", "holding_k": 5, "data_tier": "free_real"},
    )

    assert behavior_id(base) != behavior_id(changed_position)
    assert behavior_id(base) != behavior_id(changed_dispatch)
    assert behavior_id(base, "code-a") == behavior_id(base, "code-a")
    assert behavior_id(base, "code-a") != behavior_id(base, "code-b")


def test_latin_hypercube_sample_is_deterministic_unique_and_budgeted() -> None:
    grid = {"a": [1, 2, 3, 4], "b": ["x", "y", "z"], "c": [True, False]}

    first = latin_hypercube_sample(grid, budget=9, seed=7)
    second = latin_hypercube_sample(grid, budget=9, seed=7)

    assert first == second
    assert len(first) == 9
    assert len({stable_hash(row) for row in first}) == 9
    assert {row["a"] for row in first} == {1, 2, 3, 4}


def test_inverse_volatility_target_weights_are_causal_capped_and_cash_aware() -> None:
    dates = pd.bdate_range("2020-01-01", periods=40)
    rows: list[dict[str, object]] = []
    for symbol, volatile in [("600001.SH", False), ("600002.SH", True)]:
        price = 10.0
        for index, trade_date in enumerate(dates):
            change = (0.001 if not volatile else (0.05 if index % 2 == 0 else -0.045))
            price *= 1.0 + change
            rows.append(
                {
                    "trade_date": trade_date.strftime("%Y%m%d"),
                    "ts_code": symbol,
                    "signal_price": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "rank_score": 1.0,
                }
            )
    scores = pd.DataFrame(rows)
    signal_date = dates[-5].strftime("%Y%m%d")
    targets = {signal_date: ["600001.SH", "600002.SH"]}
    spec = RealStockStrategySpec(
        name="weighted",
        family="S2_real_stock_momentum",
        params={
            "weighting": "inverse_volatility",
            "gross_exposure": 0.75,
            "max_weight": 0.50,
            "risk_window": 10,
            "risk_floor": 0.001,
        },
    )

    baseline = stock_target_weights_by_signal_date(scores, targets, spec)
    weights = baseline[signal_date]
    assert sum(weights.values()) == pytest.approx(0.75)
    assert max(weights.values()) <= 0.50
    assert weights["600001.SH"] > weights["600002.SH"]

    mutated = scores.copy()
    future = mutated["trade_date"].gt(signal_date)
    mutated.loc[future, "signal_price"] *= 10.0
    mutated.loc[future, "high"] *= 10.0
    mutated.loc[future, "low"] *= 0.1
    assert stock_target_weights_by_signal_date(mutated, targets, spec) == baseline

    capped_equal = stock_target_weights_by_signal_date(
        scores,
        targets,
        RealStockStrategySpec(
            name="capped",
            family="S2_real_stock_momentum",
            params={"weighting": "equal", "gross_exposure": 1.0, "max_weight": 0.30},
        ),
    )[signal_date]
    assert capped_equal == {"600001.SH": 0.30, "600002.SH": 0.30}


def test_build_search_strategy_specs_and_stages() -> None:
    raw = {
        "sampling": {"seed": 11, "budget_per_family": 2},
        "strategy_spaces": {
            "S11_real_short_term_reversal": {
                "kind": "real_short_term_reversal",
                "fixed": {"rebalance": "weekly"},
                "parameters": {"holding_k": [10, 20], "reversal_days": [3, 5]},
            }
        },
        "stages": {
            "screen": {
                "max_windows": 12,
                "window_sampling": "even",
                "deposit_timings": ["beginning"],
                "promote_per_family": 1,
                "promote_global": 2,
                "allow_ungated_shortlist": True,
            }
        },
    }

    specs = build_search_strategy_specs(raw)
    stages = parse_search_stages(raw)

    assert len(specs) == 2
    assert len({spec.name for spec in specs}) == 2
    assert all(spec.params["kind"] == "real_short_term_reversal" for spec in specs)
    assert stages[0].max_windows == 12
    assert stages[0].deposit_timings == ("beginning",)
    assert stages[0].allow_ungated_shortlist


def test_build_search_strategy_specs_rejects_inert_sampling_method() -> None:
    raw = {
        "sampling": {"method": "bayesian", "budget_per_family": 1},
        "strategy_spaces": {
            "A": {
                "kind": "real_short_term_reversal",
                "parameters": {"holding_k": [10, 20]},
            }
        },
    }

    with pytest.raises(ValueError, match="sampling.method"):
        build_search_strategy_specs(raw)


def test_parse_search_stages_rejects_inert_shortlist_counts() -> None:
    raw = {
        "stages": {
            "confirm": {
                "promote_per_family": 2,
                "promote_global": 0,
                "allow_ungated_shortlist": False,
            }
        }
    }

    with pytest.raises(ValueError, match="shortlist counts require"):
        parse_search_stages(raw)


@pytest.mark.parametrize(
    "config_name",
    [
        "phase3_search.yaml",
        "phase3_stateful_search.yaml",
        "phase3_state_selector_search.yaml",
        "phase3_concentration_search.yaml",
        "phase3_gap_intraday_search.yaml",
        "phase3_momentum_acceleration_search.yaml",
        "phase3_signed_flow_search.yaml",
        "phase3_beta_residual_reversal_search.yaml",
        "phase3_risk_weighting_search.yaml",
    ],
)
def test_phase3_stage_windows_have_nonoverlapping_outcomes_and_no_formal_tuning(config_name: str) -> None:
    root = Path(__file__).resolve().parents[1]
    stages = parse_search_stages(load_search_config(root / "config" / config_name))
    by_name = {stage.name: stage for stage in stages}
    screen_end = pd.Timestamp(by_name["screen"].window_start_max) + pd.DateOffset(months=24)
    confirm_start = pd.Timestamp(by_name["confirm"].window_start_min)
    confirm_end = pd.Timestamp(by_name["confirm"].window_start_max) + pd.DateOffset(months=24)
    stress_start = pd.Timestamp(by_name["stress"].window_start_min)

    assert screen_end < confirm_start
    assert confirm_end < stress_start
    assert by_name["confirm"].expand_parameter_neighbors
    assert by_name["stress"].require_neighborhood_gate
    assert not by_name["confirm"].allow_ungated_shortlist
    assert not by_name["stress"].allow_ungated_shortlist
    assert not by_name["stress"].expand_parameter_neighbors
    assert by_name["confirm"].promotion_gate == "development"
    assert by_name["stress"].promotion_gate == "formal"
    assert by_name["stress"].evidence_role == "development_exposed"
    assert all(stage.max_daily_amount_participation == pytest.approx(0.05) for stage in stages)


@pytest.mark.parametrize(
    ("config_name", "family", "budget", "warmup_lookback"),
    [
        (
            "phase3_signed_flow_search.yaml",
            "S28_real_signed_flow_accumulation",
            36,
            40,
        ),
        (
            "phase3_beta_residual_reversal_search.yaml",
            "S29_real_beta_residual_shock_reversal",
            48,
            181,
        ),
    ],
)
def test_daily_slice_configs_are_budgeted_capped_and_development_exposed(
    config_name: str,
    family: str,
    budget: int,
    warmup_lookback: int,
) -> None:
    from scripts.run_phase3_search import _required_signal_history_days

    root = Path(__file__).resolve().parents[1]
    raw = load_search_config(root / "config" / config_name)
    stages = {stage.name: stage for stage in parse_search_stages(raw)}
    specs = build_search_strategy_specs(raw)
    space = raw["strategy_spaces"][family]

    assert len(specs) == budget
    assert {spec.family for spec in specs} == {family}
    assert all(spec.params["data_tier"] == "free_real" for spec in specs)
    assert space["budget"] == budget
    assert space["fixed"]["lookback"] == warmup_lookback
    assert _required_signal_history_days(raw) == warmup_lookback
    assert raw["holdout_registry"] == "config/phase3_holdout_registry.yaml"
    assert stages["screen"].promote_per_family > 0
    assert stages["screen"].promote_global > 0
    assert stages["confirm"].promote_per_family == 0
    assert stages["confirm"].promote_global == 0
    assert stages["stress"].promote_per_family == 0
    assert stages["stress"].promote_global == 0
    assert stages["confirm"].expand_parameter_neighbors
    assert not stages["stress"].expand_parameter_neighbors
    assert stages["stress"].slippage_multiplier == pytest.approx(3.0)
    assert all(stage.deposit_timings == ("beginning", "ending") for stage in stages.values())
    assert all(stage.max_daily_amount_participation == pytest.approx(0.05) for stage in stages.values())
    assert all(stage.evidence_role == "development_exposed" for stage in stages.values())


def test_risk_weighting_search_covers_three_phase2_engines_with_bounded_stock_gross() -> None:
    root = Path(__file__).resolve().parents[1]
    raw = load_search_config(root / "config" / "phase3_risk_weighting_search.yaml")
    specs = build_search_strategy_specs(raw)

    assert len(specs) == 144
    assert {spec.family for spec in specs} == {
        "S2_real_stock_momentum",
        "S3_real_stock_breakout",
        "S4_real_smallcap_factor",
    }
    assert {spec.params["weighting"] for spec in specs}.issubset(
        {"equal", "inverse_volatility", "atr_risk", "rank"}
    )
    assert all(0.0 <= float(spec.params["gross_exposure"]) <= 1.0 for spec in specs)
    assert all(0.0 < float(spec.params["max_weight"]) <= 1.0 for spec in specs)


def test_parameter_neighbor_expansion_varies_one_adjacent_value() -> None:
    raw = {
        "strategy_spaces": {
            "A": {
                "kind": "real_stateful_trend",
                "fixed": {"data_tier": "free_real"},
                "parameters": {"a": [1, 2, 3], "b": ["x", "y", "z"]},
                "ordered_parameters": ["b"],
            }
        }
    }
    center = RealStockStrategySpec(
        name="center",
        family="A",
        params={"kind": "real_stateful_trend", "data_tier": "free_real", "a": 2, "b": "y"},
    )

    expanded = expand_parameter_neighbors([center], raw)

    assert len(expanded) == 5
    neighbor_params = [spec.params for spec in expanded if spec.name != "center"]
    assert {(row["a"], row["b"]) for row in neighbor_params} == {(1, "y"), (3, "y"), (2, "x"), (2, "z")}


def test_neighborhood_gate_requires_stable_adjacent_parameters() -> None:
    raw = {
        "sampling": {
            "neighborhood_min_count": 2,
            "neighborhood_min_stable_share": 0.5,
        },
        "strategy_spaces": {
            "A": {
                "kind": "real_stateful_trend",
                "fixed": {"data_tier": "free_real"},
                "parameters": {"a": [1, 2, 3]},
            }
        },
    }
    center = RealStockStrategySpec(
        name="center",
        family="A",
        params={"kind": "real_stateful_trend", "data_tier": "free_real", "a": 2},
    )
    specs = expand_parameter_neighbors([center], raw)
    rows = []
    for index, spec in enumerate(specs):
        rows.append(
            {
                "strategy": spec.name,
                "family": "A",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "nonoverlap_median_w12": 500_000.0 if index != 0 else 400_000.0,
                "nonoverlap_median_w24": 1_200_000.0 if index != 0 else 900_000.0,
                "nonoverlap_p05_w24": 720_000.0,
                "nonoverlap_p95_max_drawdown": 0.30,
            }
        )

    enriched = add_neighborhood_metrics(pd.DataFrame(rows), specs, raw, FreeRealBacktestConfig())
    center_row = enriched.loc[enriched["strategy"].eq("center")].iloc[0]

    assert center_row["n_parameter_neighbors"] == 2
    assert center_row["stable_parameter_neighbors"] >= 1
    assert bool(center_row["passes_neighborhood_gate"])


def test_experiment_ledger_upserts_status(tmp_path: Path) -> None:
    ledger = ExperimentLedger(tmp_path / "ledger.csv")
    ledger.upsert(
        {
            "experiment_id": "abc",
            "stage": "screen",
            "family": "family",
            "strategy": "strategy",
            "status": "running",
        }
    )
    ledger.upsert(
        {
            "experiment_id": "abc",
            "stage": "screen",
            "family": "family",
            "strategy": "strategy",
            "status": "completed",
        }
    )

    frame = ledger.read()
    assert len(frame) == 1
    assert frame.iloc[0]["status"] == "completed"
    assert ledger.completed_ids() == {"abc"}


def test_regime_metrics_and_promotion_preserve_family_diversity() -> None:
    leaderboard = pd.DataFrame(
        [
            {
                "strategy": "a1",
                "family": "A",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "p_success": 0.2,
                "median_w24": 1_000_000.0,
                "p10_w24": 700_000.0,
                "p95_max_drawdown": 0.4,
                "score": 10.0,
            },
            {
                "strategy": "a2",
                "family": "A",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "p_success": 0.1,
                "median_w24": 900_000.0,
                "p10_w24": 650_000.0,
                "p95_max_drawdown": 0.4,
                "score": 8.0,
            },
            {
                "strategy": "b1",
                "family": "B",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "p_success": 0.0,
                "median_w24": 850_000.0,
                "p10_w24": 600_000.0,
                "p95_max_drawdown": 0.3,
                "score": 5.0,
            },
        ]
    )
    windows = pd.DataFrame(
        [
            {
                "strategy": strategy,
                "family": family,
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "market_regime": regime,
                "w12": w12,
                "w24": w24,
                "max_drawdown": drawdown,
            }
            for strategy, family, regime, w12, w24, drawdown in [
                ("a1", "A", "bull", 500_000.0, 1_200_000.0, 0.3),
                ("a1", "A", "bear", 400_000.0, 800_000.0, 0.4),
                ("a2", "A", "bull", 400_000.0, 900_000.0, 0.3),
                ("a2", "A", "bear", 400_000.0, 800_000.0, 0.4),
                ("b1", "B", "bull", 400_000.0, 850_000.0, 0.2),
                ("b1", "B", "bear", 400_000.0, 780_000.0, 0.3),
            ]
        ]
    )

    enriched, breakdown = add_regime_metrics(leaderboard, windows, FreeRealBacktestConfig())
    promoted = promote_candidates(enriched, per_family=1, global_count=1, allow_ungated_shortlist=True)

    assert not breakdown.empty
    assert "search_score" in enriched.columns
    assert set(promoted["family"]) == {"A", "B"}


def test_regime_gate_uses_one_globally_nonoverlapping_block_set() -> None:
    leaderboard = pd.DataFrame(
        [
            {
                "strategy": "s",
                "family": "A",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "p_success": 1.0,
                "median_w24": 1_300_000.0,
                "score": 10.0,
                "passes_core_candidate_gates": True,
            }
        ]
    )
    windows = pd.DataFrame(
        [
            {
                "strategy": "s",
                "family": "A",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "market_regime": regime,
                "start": start,
                "end": end,
                "w12": 550_000.0,
                "w24": 1_300_000.0,
                "max_drawdown": 0.2,
            }
            for regime, start, end in [
                ("bull", "2020-01-01", "2021-12-31"),
                ("bear", "2020-06-01", "2022-05-31"),
                ("sideways", "2021-01-01", "2022-12-31"),
            ]
        ]
    )

    enriched, breakdown = add_regime_metrics(
        leaderboard,
        windows,
        FreeRealBacktestConfig(),
        min_regime_blocks=1,
    )

    assert int(breakdown["n_nonoverlap_windows"].sum()) == 1
    assert int(enriched.iloc[0]["n_regimes_estimable"]) == 1
    assert not bool(enriched.iloc[0]["passes_regime_gate"])


def test_promotion_prioritizes_target_probability_over_composite_score() -> None:
    leaderboard = pd.DataFrame(
        [
            {
                "strategy": "safe_zero",
                "family": "A",
                "deposit_timing": "beginning",
                "p_success": 0.0,
                "p05_w24": 710_000.0,
                "median_w24": 760_000.0,
                "score": 100.0,
            },
            {
                "strategy": "target_hit",
                "family": "A",
                "deposit_timing": "beginning",
                "p_success": 0.02,
                "p05_w24": 560_000.0,
                "median_w24": 680_000.0,
                "score": 1.0,
            },
        ]
    )

    promoted = promote_candidates(leaderboard, per_family=1, global_count=0, allow_ungated_shortlist=True)

    assert promoted["strategy"].tolist() == ["target_hit"]


def test_formal_promotion_does_not_advance_ungated_rows() -> None:
    leaderboard = pd.DataFrame(
        [
            {
                "strategy": "failed",
                "family": "A",
                "deposit_timing": "beginning",
                "p_success": 0.0,
                "p05_w24": 720_000.0,
                "median_w24": 800_000.0,
                "score": 1.0,
                "passes_candidate_gates": False,
            }
        ]
    )

    promoted = promote_candidates(
        leaderboard,
        per_family=0,
        global_count=0,
        allow_ungated_shortlist=False,
    )

    assert promoted.empty


def test_promotion_shortlist_limits_use_relation_group_and_keep_hard_gates() -> None:
    leaderboard = pd.DataFrame(
        [
            {
                "strategy": "hard",
                "family": "S20",
                "relation_group": "trend",
                "deposit_timing": "beginning",
                "p_success": 0.10,
                "p05_w24": 700_000.0,
                "median_w24": 800_000.0,
                "score": 1.0,
                "passes_development_gates": True,
            },
            {
                "strategy": "s20_best_ungated",
                "family": "S20",
                "relation_group": "trend",
                "deposit_timing": "beginning",
                "p_success": 0.09,
                "p05_w24": 700_000.0,
                "median_w24": 800_000.0,
                "score": 1.0,
                "passes_development_gates": False,
            },
            {
                "strategy": "s22_same_group",
                "family": "S22",
                "relation_group": "trend",
                "deposit_timing": "beginning",
                "p_success": 0.08,
                "p05_w24": 700_000.0,
                "median_w24": 800_000.0,
                "score": 1.0,
                "passes_development_gates": False,
            },
            {
                "strategy": "contraction",
                "family": "S21",
                "relation_group": "contraction",
                "deposit_timing": "beginning",
                "p_success": 0.07,
                "p05_w24": 700_000.0,
                "median_w24": 800_000.0,
                "score": 1.0,
                "passes_development_gates": False,
            },
        ]
    )

    promoted = promote_candidates(
        leaderboard,
        per_family=1,
        global_count=0,
        allow_ungated_shortlist=True,
        gate_column="passes_development_gates",
        group_column="relation_group",
    )

    assert set(promoted["strategy"]) == {"hard", "s20_best_ungated", "contraction"}
    assert "s22_same_group" not in set(promoted["strategy"])
    assert "hard_gate" in promoted.set_index("strategy").loc["hard", "promotion_reason"]


def test_promotion_fails_closed_when_group_column_is_missing() -> None:
    leaderboard = pd.DataFrame(
        [
            {
                "strategy": "s",
                "family": "S20",
                "deposit_timing": "beginning",
                "p_success": 0.1,
                "p05_w24": 700_000.0,
                "median_w24": 800_000.0,
                "score": 1.0,
            }
        ]
    )

    with pytest.raises(ValueError, match="group column"):
        promote_candidates(
            leaderboard,
            per_family=1,
            global_count=0,
            allow_ungated_shortlist=True,
            group_column="relation_group",
        )


def test_candidate_gate_requires_all_deposit_timings(tmp_path: Path) -> None:
    leaderboard = pd.DataFrame(
        [
            {
                "strategy": "timing_sensitive",
                "family": "A",
                "data_tier": "strict_real",
                "deposit_timing": timing,
                "p_success": 0.6 if timing == "beginning" else 0.2,
                "median_w24": 1_250_000.0,
                "nonoverlap_median_w12": 520_000.0,
                "nonoverlap_median_w24": 1_250_000.0,
                "nonoverlap_worst_w24": 1_210_000.0,
                "nonoverlap_worst_max_drawdown": 0.2,
                "nonoverlap_exact_pvalue": 0.01,
                "p05_w24": 800_000.0,
                "p10_w24": 850_000.0,
                "p95_max_drawdown": 0.2,
                "passes_core_candidate_gates": timing == "beginning",
                "passes_liquidity_gate": True,
                "score": 10.0,
            }
            for timing in ["beginning", "ending"]
        ]
    )
    windows = pd.DataFrame(
        [
            {
                "strategy": "timing_sensitive",
                "family": "A",
                "data_tier": "strict_real",
                "deposit_timing": timing,
                "market_regime": regime,
                "w12": 500_000.0,
                "w24": 1_200_000.0,
                "max_drawdown": 0.2,
            }
            for timing in ["beginning", "ending"]
            for regime in ["bull", "bear", "sideways"]
        ]
    )

    enriched, _ = add_regime_metrics(
        leaderboard,
        windows,
        FreeRealBacktestConfig(),
        min_regime_blocks=1,
    )
    enriched["behavior_id"] = "timing-sensitive-behavior"
    enriched["n_nonoverlap_windows"] = 10
    enriched["n_nonoverlap_successes"] = 10
    enriched = finalize_strategy_gates(
        enriched,
        cfg=FreeRealBacktestConfig(),
        required_deposit_timings=["beginning", "ending"],
        require_neighborhood_gate=False,
        evidence_role="formal_outer",
        holdout_signature="reserved",
        multiple_testing_registry=MultipleTestingRegistry(tmp_path / "formal.csv"),
        study_id="study",
        stage="formal",
        evidence_context={"window_manifest_signature": "windows"},
    )

    assert enriched["deposit_timings_tested"].eq(2).all()
    assert enriched["deposit_timings_passed"].eq(1).all()
    assert not enriched["passes_all_deposit_timings"].any()
    assert not enriched["passes_candidate_gates"].any()


def test_holm_adjustment_is_monotone_and_controls_familywise_gate() -> None:
    adjusted = holm_adjusted_pvalues(pd.Series([0.01, 0.02, 0.40], index=["a", "b", "c"]))

    assert adjusted["a"] == pytest.approx(0.03)
    assert adjusted["b"] == pytest.approx(0.04)
    assert adjusted["c"] == pytest.approx(0.40)


def _formal_row(
    strategy: str,
    semantic_id: str,
    *,
    family: str = "A",
    timing: str = "beginning",
    pvalue: float = 0.01,
    data_tier: str = "strict_real",
) -> dict[str, object]:
    return {
        "strategy": strategy,
        "family": family,
        "behavior_id": semantic_id,
        "data_tier": data_tier,
        "deposit_timing": timing,
        "p_success": 1.0,
        "score": 10.0,
        "nonoverlap_median_w12": 520_000.0,
        "nonoverlap_median_w24": 1_250_000.0,
        "nonoverlap_worst_w24": 1_210_000.0,
        "nonoverlap_worst_max_drawdown": 0.2,
        "nonoverlap_exact_pvalue": pvalue,
        "n_nonoverlap_windows": 10,
        "n_nonoverlap_successes": 10,
        "passes_core_candidate_gates": True,
        "passes_regime_gate": True,
        "passes_liquidity_gate": True,
    }


def _finalize_formal(
    rows: list[dict[str, object]],
    registry: MultipleTestingRegistry,
    *,
    holdout: str = "outer-a",
) -> pd.DataFrame:
    return finalize_strategy_gates(
        pd.DataFrame(rows),
        cfg=FreeRealBacktestConfig(),
        required_deposit_timings=sorted({str(row["deposit_timing"]) for row in rows}),
        require_neighborhood_gate=False,
        evidence_role="formal_outer",
        holdout_signature=holdout,
        multiple_testing_registry=registry,
        study_id="study",
        stage="formal",
        evidence_context={"window_manifest_signature": f"windows-{holdout}"},
    )


def _registry_record(
    behavior: str,
    *,
    holdout: str = "outer-a",
    pvalue: float = 0.01,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "holdout_signature": holdout,
        "behavior_id": behavior,
        "deposit_timing": "beginning",
        "exact_pvalue": pvalue,
        "evidence": evidence or {"n_nonoverlap_windows": 10, "n_nonoverlap_successes": 10},
        "study_id": "study",
        "stage": "formal",
        "strategy": behavior,
        "family": "A",
    }


def test_formal_registry_deduplicates_cross_family_semantic_behavior(tmp_path: Path) -> None:
    registry = MultipleTestingRegistry(tmp_path / "formal.csv")
    out = _finalize_formal(
        [
            _formal_row("s20", "same-behavior", family="S20_real_stateful_trend"),
            _formal_row("s22", "same-behavior", family="S22_real_concentrated_trend"),
        ],
        registry,
    )

    assert len(registry.read()) == 1
    assert np.allclose(out["holm_adjusted_exact_pvalue"], 0.01)
    assert out["holm_registry_hypothesis_count"].eq(1).all()


def test_formal_finalize_requires_persistent_registry() -> None:
    with pytest.raises(ValueError, match="persistent multiple-testing registry"):
        finalize_strategy_gates(
            pd.DataFrame([_formal_row("strategy", "behavior")]),
            cfg=FreeRealBacktestConfig(),
            required_deposit_timings=["beginning"],
            require_neighborhood_gate=False,
            evidence_role="formal_outer",
            holdout_signature="outer-a",
            study_id="study",
            stage="formal",
        )


def test_formal_holm_accumulates_across_finalize_calls(tmp_path: Path) -> None:
    registry = MultipleTestingRegistry(tmp_path / "formal.csv")

    first = _finalize_formal([_formal_row("first", "behavior-1")], registry)
    second = _finalize_formal([_formal_row("second", "behavior-2")], registry)

    assert first.iloc[0]["holm_adjusted_exact_pvalue"] == pytest.approx(0.01)
    assert second.iloc[0]["holm_adjusted_exact_pvalue"] == pytest.approx(0.02)
    assert second.iloc[0]["holm_registry_hypothesis_count"] == 2
    assert second.iloc[0]["holm_adjustment_scope"] == "persistent_holdout_registry"
    assert bool(second.iloc[0]["holm_is_formal"])


def test_multiple_testing_registry_is_idempotent_for_identical_evidence(tmp_path: Path) -> None:
    registry = MultipleTestingRegistry(tmp_path / "formal.csv")
    record = _registry_record("behavior-1")

    registry.register(record)
    original = registry.path.read_bytes()
    registry.register(record)

    assert registry.path.read_bytes() == original
    assert len(registry.read()) == 1


def test_multiple_testing_registry_rejects_pvalue_and_evidence_conflicts(tmp_path: Path) -> None:
    registry = MultipleTestingRegistry(tmp_path / "formal.csv")
    registry.register(_registry_record("behavior-1"))

    with pytest.raises(ValueError, match="conflict"):
        registry.register(_registry_record("behavior-1", pvalue=0.02))
    with pytest.raises(ValueError, match="conflict"):
        registry.register(
            _registry_record(
                "behavior-1",
                evidence={"n_nonoverlap_windows": 12, "n_nonoverlap_successes": 10},
            )
        )

    assert len(registry.read()) == 1


def test_multiple_testing_registry_isolates_holdouts(tmp_path: Path) -> None:
    registry = MultipleTestingRegistry(tmp_path / "formal.csv")
    registry.register(_registry_record("behavior-1", holdout="outer-a"))
    registry.register(_registry_record("behavior-1", holdout="outer-b"))

    outer_a = registry.adjusted_pvalues("outer-a")
    outer_b = registry.adjusted_pvalues("outer-b")

    assert len(registry.read()) == 2
    assert outer_a.iloc[0]["holm_adjusted_exact_pvalue"] == pytest.approx(0.01)
    assert outer_b.iloc[0]["holm_adjusted_exact_pvalue"] == pytest.approx(0.01)
    assert outer_a.iloc[0]["registry_hypothesis_count"] == 1
    assert outer_b.iloc[0]["registry_hypothesis_count"] == 1


def test_multiple_testing_registry_csv_replacement_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = MultipleTestingRegistry(tmp_path / "formal.csv")
    registry.register(_registry_record("behavior-1"))
    original = registry.path.read_bytes()

    def fail_after_partial_write(frame, handle, *args, **kwargs):
        handle.write("partial")
        handle.flush()
        raise OSError("simulated interrupted CSV write")

    monkeypatch.setattr(pd.DataFrame, "to_csv", fail_after_partial_write)
    with pytest.raises(OSError, match="interrupted"):
        registry.register(_registry_record("behavior-2"))

    assert registry.path.read_bytes() == original
    assert not list(tmp_path.glob(".formal.csv.*.tmp"))


def test_multiple_testing_registry_fails_closed_on_malformed_csv(tmp_path: Path) -> None:
    path = tmp_path / "formal.csv"
    path.write_text("holdout_signature,behavior_id\nouter-a,behavior-1\n", encoding="utf-8")
    registry = MultipleTestingRegistry(path)

    with pytest.raises(ValueError, match="schema mismatch"):
        registry.register(_registry_record("behavior-2"))

    assert path.read_text(encoding="utf-8") == "holdout_signature,behavior_id\nouter-a,behavior-1\n"


def test_final_gate_rejects_missing_timing_and_stale_neighborhood_pass(tmp_path: Path) -> None:
    base = {
        "family": "A",
        "data_tier": "free_real",
        "p_success": 1.0,
        "score": 10.0,
        "nonoverlap_median_w12": 520_000.0,
        "nonoverlap_median_w24": 1_250_000.0,
        "nonoverlap_worst_w24": 1_210_000.0,
        "nonoverlap_worst_max_drawdown": 0.2,
        "nonoverlap_exact_pvalue": 0.01,
        "n_nonoverlap_windows": 10,
        "n_nonoverlap_successes": 10,
        "passes_core_candidate_gates": True,
        "passes_regime_gate": True,
        "passes_liquidity_gate": True,
    }
    rows = [
        {
            **base,
            "strategy": "missing",
            "behavior_id": "missing-behavior",
            "deposit_timing": "beginning",
            "passes_neighborhood_gate": True,
        },
        {
            **base,
            "strategy": "stale",
            "behavior_id": "stale-behavior",
            "deposit_timing": "beginning",
            "passes_neighborhood_gate": True,
        },
        {
            **base,
            "strategy": "stale",
            "behavior_id": "stale-behavior",
            "deposit_timing": "ending",
            "passes_neighborhood_gate": False,
        },
    ]

    out = finalize_strategy_gates(
        pd.DataFrame(rows),
        cfg=FreeRealBacktestConfig(),
        required_deposit_timings=["beginning", "ending"],
        require_neighborhood_gate=True,
        evidence_role="formal_outer",
        holdout_signature="reserved",
        multiple_testing_registry=MultipleTestingRegistry(tmp_path / "formal.csv"),
        study_id="study",
        stage="formal",
        evidence_context={"window_manifest_signature": "windows"},
    )

    assert not out.loc[out["strategy"].eq("missing"), "passes_required_deposit_timing_coverage"].any()
    assert not out.loc[out["strategy"].eq("missing"), "passes_candidate_gates"].any()
    assert not out.loc[out["strategy"].eq("stale"), "passes_all_deposit_timings"].any()
    assert not out.loc[out["strategy"].eq("stale"), "passes_candidate_gates"].any()


def test_formal_gate_rejects_proxy_tier_and_exposed_evidence(tmp_path: Path) -> None:
    rows = pd.DataFrame(
        [
            {
                "strategy": "proxy",
                "family": "A",
                "data_tier": "proxy_research",
                "deposit_timing": timing,
                "nonoverlap_median_w12": 520_000.0,
                "nonoverlap_median_w24": 1_250_000.0,
                "nonoverlap_worst_w24": 1_210_000.0,
                "nonoverlap_worst_max_drawdown": 0.2,
                "nonoverlap_exact_pvalue": 0.001,
                "n_nonoverlap_windows": 10,
                "n_nonoverlap_successes": 10,
                "behavior_id": "proxy-behavior",
                "passes_core_candidate_gates": True,
                "passes_regime_gate": True,
                "passes_liquidity_gate": True,
            }
            for timing in ["beginning", "ending"]
        ]
    )
    proxy = finalize_strategy_gates(
        rows,
        cfg=FreeRealBacktestConfig(),
        required_deposit_timings=["beginning", "ending"],
        require_neighborhood_gate=False,
        evidence_role="formal_outer",
        holdout_signature="reserved",
        multiple_testing_registry=MultipleTestingRegistry(tmp_path / "formal.csv"),
        study_id="study",
        stage="formal",
        evidence_context={"window_manifest_signature": "windows"},
    )
    exposed = finalize_strategy_gates(
        rows.assign(data_tier="free_real"),
        cfg=FreeRealBacktestConfig(),
        required_deposit_timings=["beginning", "ending"],
        require_neighborhood_gate=False,
        evidence_role="development_exposed",
        holdout_signature="dev",
    )

    assert not proxy["passes_data_tier_gate"].any()
    assert not proxy["passes_candidate_gates"].any()
    assert not exposed["formal_evidence_available"].any()
    assert not exposed["passes_candidate_gates"].any()
    assert not exposed["holm_is_formal"].any()
    assert not exposed["passes_holm_exact_gate"].any()
    assert exposed["holm_adjustment_scope"].eq("current_batch_descriptive").all()


def test_formal_gate_separates_free_real_development_from_strict_proof(
    tmp_path: Path,
) -> None:
    free_rows = [
        _formal_row(
            "free",
            "free-behavior",
            timing=timing,
            pvalue=0.001,
            data_tier="free_real",
        )
        for timing in ["beginning", "ending"]
    ]
    strict_rows = [
        _formal_row(
            "strict",
            "strict-behavior",
            timing=timing,
            pvalue=0.001,
            data_tier="strict_real",
        )
        for timing in ["beginning", "ending"]
    ]

    free_registry = MultipleTestingRegistry(tmp_path / "free.csv")
    free = _finalize_formal(
        free_rows,
        free_registry,
        holdout="outer-free",
    )
    strict = _finalize_formal(
        strict_rows,
        MultipleTestingRegistry(tmp_path / "strict.csv"),
        holdout="outer-strict",
    )

    assert free["passes_development_data_tier_gate"].all()
    assert not free["passes_strict_data_tier_gate"].any()
    assert not free["passes_candidate_gates"].any()
    assert free_registry.read().empty
    assert strict["passes_strict_data_tier_gate"].all()
    assert strict["passes_candidate_gates"].all()


def _synthetic_search_panel() -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-01", periods=280)
    rows = []
    for symbol_index in range(8):
        previous = 10.0 + symbol_index
        for day_index, dt in enumerate(dates):
            close = (10.0 + symbol_index) * (1.0 + 0.0007 * (symbol_index + 1)) ** day_index
            close *= 1.0 + 0.04 * np.sin(day_index / float(4 + symbol_index))
            open_price = previous * (1.0 + 0.002 * np.cos(day_index / 5.0))
            pct_chg = (close / previous - 1.0) * 100.0
            amount = 40_000_000.0 * (1.0 + symbol_index / 10.0)
            if day_index % 29 == 0:
                amount *= 3.0
            rows.append(
                {
                    "trade_date": dt.strftime("%Y%m%d"),
                    "ts_code": f"600{symbol_index:03d}.SH",
                    "source_code": f"sh.600{symbol_index:03d}",
                    "open": open_price,
                    "high": max(open_price, close) * 1.01,
                    "low": min(open_price, close) * 0.99,
                    "close": close,
                    "pre_close": previous,
                    "corporate_action_share_factor": 1.0,
                    "corporate_action_source": "none",
                    "volume": amount / max(close, 1.0),
                    "amount": amount,
                    "turnover_rate": 1.5 + symbol_index * 0.1,
                    "pct_chg": pct_chg,
                    "pe_ttm": 8.0 + symbol_index * 2.0,
                    "pb": 0.8 + symbol_index * 0.2,
                    "ps_ttm": 1.0 + symbol_index * 0.3,
                    "pcf_ttm": 4.0 + symbol_index,
                    "adj_close_for_signal": close,
                    "trade_status": 1,
                    "is_suspended": False,
                    "is_st": False,
                    "list_date": "20100101",
                    "delist_date": "",
                    "list_status": "1",
                    "is_last_observation": day_index == len(dates) - 1,
                    "delisting_exit_required": False,
                    "terminal_value_source": "",
                    "listing_days": 3650 + day_index,
                    "board": "main",
                    "limit_pct": 0.10,
                    "up_limit": previous * 1.10,
                    "down_limit": previous * 0.90,
                    "circ_mv_approx": 1_000_000_000.0 * (symbol_index + 1),
                    "data_tier": "free_real",
                }
            )
            previous = close
    return pd.DataFrame(rows)


def _beta_residual_search_panel() -> tuple[pd.DataFrame, str, str, str]:
    panel = _synthetic_search_panel()
    symbols = sorted(panel["ts_code"].unique())[:7]
    panel = panel.loc[panel["ts_code"].isin(symbols)].copy()
    dates = sorted(panel["trade_date"].unique())
    coefficients = dict(zip(symbols, [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0]))
    shock_date = dates[-20]
    shock_symbol = symbols[0]
    market_move_symbol = symbols[4]
    shock_day_returns = {
        symbols[0]: -0.1300,
        symbols[1]: -0.0510,
        symbols[2]: -0.0505,
        symbols[3]: -0.0500,
        symbols[4]: -0.0500,
        symbols[5]: -0.0495,
        symbols[6]: -0.0490,
    }
    date_positions = {trade_date: index for index, trade_date in enumerate(dates)}
    for symbol in symbols:
        symbol_mask = panel["ts_code"].eq(symbol)
        symbol_dates = panel.loc[symbol_mask, "trade_date"]
        positions = symbol_dates.map(date_positions).to_numpy(dtype=float)
        market_return = 0.006 * np.sin(positions / 5.0) + 0.003 * np.cos(positions / 11.0)
        idiosyncratic_scale = 0.0005 * np.cos(positions / 3.7)
        signal_return = market_return + coefficients[symbol] * idiosyncratic_scale
        panel.loc[symbol_mask, "pct_chg"] = signal_return * 100.0
        shock_mask = symbol_mask & panel["trade_date"].eq(shock_date)
        panel.loc[shock_mask, "pct_chg"] = shock_day_returns[symbol] * 100.0
    return panel, shock_date, shock_symbol, market_move_symbol


def test_signed_flow_uses_clv_turnover_not_price_momentum_and_is_causal() -> None:
    panel = _synthetic_search_panel()
    dates = sorted(panel["trade_date"].unique())
    symbols = sorted(panel["ts_code"].unique())
    signal_date = dates[-20]
    signal_position = dates.index(signal_date)
    flow_dates = dates[signal_position - 9 : signal_position + 1]
    flow_symbol = symbols[0]
    momentum_symbol = symbols[-1]
    panel.loc[panel["ts_code"].eq(flow_symbol), "pct_chg"] = 0.0
    panel.loc[panel["ts_code"].eq(momentum_symbol), "pct_chg"] = 0.5
    for symbol_index, symbol in enumerate(symbols):
        for window_index, trade_date in enumerate(flow_dates):
            row_mask = panel["ts_code"].eq(symbol) & panel["trade_date"].eq(trade_date)
            close = float(panel.loc[row_mask, "close"].iloc[0])
            if symbol == flow_symbol:
                desired_clv = 1.0 if window_index == len(flow_dates) - 1 else -1.0
                turnover = 100.0 if desired_clv > 0.0 else 1.0
            elif symbol == momentum_symbol:
                desired_clv = 0.50
                turnover = 10.0
            else:
                desired_clv = 0.40 - symbol_index * 0.08
                turnover = 10.0
            price_range = close * 0.02
            high = close + (1.0 - desired_clv) * price_range / 2.0
            low = close - (1.0 + desired_clv) * price_range / 2.0
            panel.loc[row_mask, ["high", "low", "turnover_rate"]] = [high, low, turnover]
    zero_range_symbol = symbols[-2]
    zero_range_mask = panel["ts_code"].eq(zero_range_symbol) & panel["trade_date"].eq(signal_date)
    panel.loc[zero_range_mask, ["high", "low"]] = panel.loc[
        zero_range_mask, ["close", "close"]
    ].to_numpy()
    spec = RealStockStrategySpec(
        name="S28_test",
        family="S28_real_signed_flow_accumulation",
        params={
            "kind": "real_signed_flow_accumulation",
            "flow_window": 10,
            "entry_percentile": 0.90,
            "max_abs_return_20": 0.20,
            "holding_k": 3,
            "rebalance": "2d",
            "min_listing_days": 0,
            "min_price": 0.0,
            "min_avg_amount_20d": 1.0,
        },
    )

    baseline = compute_real_stock_scores(panel.copy(deep=True), spec)
    repeated = compute_real_stock_scores(panel.copy(deep=True), spec)
    score_columns = [
        "trade_date",
        "ts_code",
        "clv",
        "flow_turnover_denominator",
        "signed_flow",
        "flow_percentile",
        "total_return_20",
        "rank_score",
        "rank",
    ]
    pd.testing.assert_frame_equal(baseline[score_columns], repeated[score_columns])
    signal_rows = baseline.loc[baseline["trade_date"].eq(signal_date)].set_index("ts_code")
    expected_weighted_flow = (100.0 - 9.0) / (100.0 + 9.0)

    assert signal_rows.loc[flow_symbol, "signed_flow"] == pytest.approx(expected_weighted_flow)
    assert signal_rows.loc[zero_range_symbol, "clv"] == 0.0
    assert signal_rows.loc[momentum_symbol, "total_return_20"] > signal_rows.loc[flow_symbol, "total_return_20"]
    assert np.isfinite(signal_rows.loc[flow_symbol, "rank_score"])
    assert pd.isna(signal_rows.loc[momentum_symbol, "rank_score"])
    assert signal_rows.loc[flow_symbol, "rank"] == 1.0

    future_panel = panel.copy(deep=True)
    future_mask = future_panel["trade_date"].gt(signal_date)
    future_panel.loc[future_mask, "high"] = future_panel.loc[future_mask, "close"] * 1.50
    future_panel.loc[future_mask, "low"] = future_panel.loc[future_mask, "close"] * 0.50
    future_panel.loc[future_mask, "turnover_rate"] = 999.0
    future_panel.loc[future_mask, "pct_chg"] = -50.0
    mutated = compute_real_stock_scores(future_panel, spec)
    through_signal = baseline["trade_date"].le(signal_date)
    pd.testing.assert_frame_equal(
        baseline.loc[through_signal, score_columns].reset_index(drop=True),
        mutated.loc[through_signal, score_columns].reset_index(drop=True),
    )
    baseline_targets = target_symbols_by_signal_date(
        baseline,
        spec,
        holding_k=3,
        end_date=signal_date,
    )
    mutated_targets = target_symbols_by_signal_date(
        mutated,
        spec,
        holding_k=3,
        end_date=signal_date,
    )
    assert baseline_targets == mutated_targets
    assert flow_symbol in baseline_targets[signal_date]


def test_beta_residual_reversal_lags_beta_sigma_and_is_causal() -> None:
    panel, shock_date, shock_symbol, market_move_symbol = _beta_residual_search_panel()
    spec = RealStockStrategySpec(
        name="S29_test",
        family="S29_real_beta_residual_shock_reversal",
        params={
            "kind": "real_beta_residual_shock_reversal",
            "beta_window": 60,
            "residual_vol_window": 20,
            "shock_horizon": 1,
            "entry_z": -2.0,
            "min_amount_ratio": 0.8,
            "holding_k": 5,
            "rebalance": "daily",
            "min_listing_days": 0,
            "min_price": 0.0,
            "min_avg_amount_20d": 1.0,
        },
    )

    baseline = compute_real_stock_scores(panel.copy(deep=True), spec)
    repeated = compute_real_stock_scores(panel.copy(deep=True), spec)
    score_columns = [
        "trade_date",
        "ts_code",
        "market_return",
        "rolling_beta",
        "daily_residual",
        "residual_sigma",
        "residual_shock_z",
        "amount_ratio",
        "rank_score",
        "rank",
    ]
    pd.testing.assert_frame_equal(baseline[score_columns], repeated[score_columns])
    shock_rows = baseline.loc[baseline["trade_date"].eq(shock_date)].set_index("ts_code")
    shock_row = shock_rows.loc[shock_symbol]
    market_move_row = shock_rows.loc[market_move_symbol]

    assert shock_row["market_return"] == pytest.approx(-0.05)
    finite_values = pd.to_numeric(
        shock_row[["rolling_beta", "residual_sigma", "residual_shock_z", "rank_score"]]
    )
    assert np.isfinite(finite_values.to_numpy(dtype=float)).all()
    assert shock_row["residual_shock_z"] < market_move_row["residual_shock_z"]
    assert shock_row["rank"] == 1.0

    current_mutation = panel.copy(deep=True)
    current_mask = current_mutation["trade_date"].eq(shock_date) & current_mutation["ts_code"].eq(
        shock_symbol
    )
    current_mutation.loc[current_mask, "pct_chg"] = -20.0
    current_scores = compute_real_stock_scores(current_mutation, spec)
    current_shock_row = current_scores.loc[
        current_scores["trade_date"].eq(shock_date) & current_scores["ts_code"].eq(shock_symbol)
    ].iloc[0]
    assert current_shock_row["market_return"] == pytest.approx(shock_row["market_return"])
    assert current_shock_row["rolling_beta"] == pytest.approx(shock_row["rolling_beta"])
    assert current_shock_row["residual_sigma"] == pytest.approx(shock_row["residual_sigma"])
    assert current_shock_row["residual_shock_z"] < shock_row["residual_shock_z"]

    future_panel = panel.copy(deep=True)
    future_mask = future_panel["trade_date"].gt(shock_date)
    future_panel.loc[future_mask, "pct_chg"] = 40.0
    future_panel.loc[future_mask, "amount"] = 1_000_000_000.0
    mutated = compute_real_stock_scores(future_panel, spec)
    through_shock = baseline["trade_date"].le(shock_date)
    pd.testing.assert_frame_equal(
        baseline.loc[through_shock, score_columns].reset_index(drop=True),
        mutated.loc[through_shock, score_columns].reset_index(drop=True),
    )
    baseline_targets = target_symbols_by_signal_date(
        baseline,
        spec,
        holding_k=5,
        end_date=shock_date,
    )
    mutated_targets = target_symbols_by_signal_date(
        mutated,
        spec,
        holding_k=5,
        end_date=shock_date,
    )
    assert baseline_targets == mutated_targets
    assert shock_symbol in baseline_targets[shock_date]


@pytest.mark.parametrize(
    ("family", "params"),
    [
        (
            "S3_real_stock_breakout",
            {
                "kind": "real_stock_breakout",
                "holding_k": 3,
                "rebalance": "daily",
                "entry_rebalance": "daily",
                "donchian": 10,
                "exit_donchian": 5,
                "min_volume_breakout": 0.0,
                "max_holding_days": 20,
                "trailing_stop": 0.0,
            },
        ),
        (
            "S10_real_regime_selector",
            {
                "kind": "real_regime_selector",
                "holding_k": 3,
                "rebalance": "weekly",
                "market_ma": 60,
                "breadth_ma": 20,
                "breadth_threshold": 0.25,
                "ranking_mode": "smallcap_momentum",
            },
        ),
        (
            "S11_real_short_term_reversal",
            {"kind": "real_short_term_reversal", "holding_k": 3, "rebalance": "weekly", "reversal_days": 5, "trend_days": 60},
        ),
        (
            "S12_real_low_volatility",
            {"kind": "real_low_volatility", "holding_k": 3, "rebalance": "weekly", "volatility_days": 20, "momentum_days": 60},
        ),
        (
            "S13_real_residual_momentum",
            {"kind": "real_residual_momentum", "holding_k": 3, "rebalance": "weekly", "lookback": 60, "skip_days": 5},
        ),
        (
            "S14_real_volume_price_shock",
            {
                "kind": "real_volume_price_shock",
                "holding_k": 3,
                "rebalance": "weekly",
                "mode": "continuation",
                "lookback": 3,
                "min_amount_ratio": 1.5,
            },
        ),
        (
            "S16_real_value_composite",
            {
                "kind": "real_value_composite",
                "holding_k": 3,
                "rebalance": "monthly",
                "mode": "deep_value",
                "valuation_lag_days": 5,
            },
        ),
        (
            "S20_real_stateful_trend",
            {
                "kind": "real_stateful_trend",
                "holding_k": 3,
                "rebalance": "daily",
                "entry_window": 20,
                "exit_window": 10,
                "trend_window": 60,
                "momentum_window": 20,
                "min_momentum": -1.0,
            },
        ),
        (
            "S21_real_volatility_contraction",
            {
                "kind": "real_volatility_contraction",
                "holding_k": 3,
                "rebalance": "daily",
                "breakout_window": 20,
                "short_vol_window": 5,
                "long_vol_window": 40,
                "max_contraction_ratio": 2.0,
                "min_amount_ratio": 0.0,
                "exit_window": 10,
            },
        ),
        (
            "S24_real_regime_contraction",
            {
                "kind": "real_regime_contraction",
                "holding_k": 3,
                "rebalance": "daily",
                "breakout_window": 20,
                "short_vol_window": 5,
                "long_vol_window": 40,
                "max_contraction_ratio": 2.0,
                "min_amount_ratio": 0.0,
                "exit_window": 10,
                "market_ma": 20,
                "breadth_ma": 10,
                "breadth_threshold": 0.0,
            },
        ),
        (
            "S26_real_gap_intraday",
            {
                "kind": "real_gap_intraday",
                "holding_k": 3,
                "rebalance": "daily",
                "entry_rebalance": "daily",
                "mode": "up_continuation",
                "min_gap": -1.0,
                "min_intraday": -1.0,
                "min_amount_ratio": 0.0,
                "exit_window": 3,
                "max_holding_days": 3,
            },
        ),
        (
            "S27_real_momentum_acceleration",
            {
                "kind": "real_momentum_acceleration",
                "holding_k": 3,
                "rebalance": "daily",
                "entry_rebalance": "daily",
                "short_window": 5,
                "medium_window": 20,
                "rank_change_window": 5,
                "trend_window": 20,
                "entry_percentile": 0.10,
                "exit_percentile": 0.0,
                "exit_window": 3,
                "max_holding_days": 20,
            },
        ),
    ],
)
def test_phase3_scorers_produce_causal_rank_rows(family: str, params: dict[str, object]) -> None:
    spec = RealStockStrategySpec(
        name=f"{family}_test",
        family=family,
        params={
            "min_listing_days": 0,
            "min_price": 0.0,
            "min_avg_amount_20d": 1.0,
            **params,
        },
    )

    scores = compute_real_stock_scores(_synthetic_search_panel(), spec)

    valid = scores.loc[scores["rank_score"].notna()]
    assert not valid.empty
    assert valid["rank_score"].between(0.0, 1.0).all()


def test_gap_intraday_scores_do_not_look_ahead() -> None:
    panel = _synthetic_search_panel()
    spec = RealStockStrategySpec(
        name="S26_causality_test",
        family="S26_real_gap_intraday",
        params={
            "kind": "real_gap_intraday",
            "holding_k": 3,
            "rebalance": "daily",
            "entry_rebalance": "daily",
            "mode": "up_continuation",
            "min_gap": -1.0,
            "min_intraday": -1.0,
            "min_amount_ratio": 0.0,
            "exit_window": 5,
            "min_listing_days": 0,
            "min_price": 0.0,
            "min_avg_amount_20d": 1.0,
        },
    )
    baseline = compute_real_stock_scores(panel, spec)
    changed_date = str(panel["trade_date"].max())
    changed_symbol = str(panel["ts_code"].min())
    mask = panel["trade_date"].eq(changed_date) & panel["ts_code"].eq(changed_symbol)
    panel.loc[mask, "open"] = panel.loc[mask, "pre_close"] * 1.08
    panel.loc[mask, "close"] = panel.loc[mask, "open"] * 1.06

    mutated = compute_real_stock_scores(panel, spec)
    compare_columns = ["trade_date", "ts_code", "rank_score", "entry_signal", "exit_signal"]
    before_changed_date = baseline["trade_date"].lt(changed_date)
    pd.testing.assert_frame_equal(
        baseline.loc[before_changed_date, compare_columns].reset_index(drop=True),
        mutated.loc[before_changed_date, compare_columns].reset_index(drop=True),
    )
    baseline_gap = baseline.loc[
        baseline["trade_date"].eq(changed_date) & baseline["ts_code"].eq(changed_symbol), "gap_return"
    ].iloc[0]
    mutated_gap = mutated.loc[
        mutated["trade_date"].eq(changed_date) & mutated["ts_code"].eq(changed_symbol), "gap_return"
    ].iloc[0]
    assert mutated_gap != baseline_gap

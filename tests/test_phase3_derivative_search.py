from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_proof.cffex_catalog import CffexCatalog
from quant_proof.cffex_execution_parameters import CffexExecutionParameterSchedule
from quant_proof.derivative_event_loop import DevelopmentResearchAssumptions
from quant_proof.free_real_backtest import FreeRealBacktestConfig
from quant_proof.phase3_derivative_search import (
    aggregate_overlay_windows,
    evaluate_overlay_spec,
    overlay_family,
    overlay_lineage_key,
    select_inherited_overlay_specs,
    select_overlay_candidates,
    select_overlay_windows,
)
from quant_proof.phase3_derivative_signals import FuturesDirectionRule
from quant_proof.phase3_overlay_coordinator import FuturesOverlaySpec
from quant_proof.phase3_overlay_factory import (
    OverlaySearchSpec,
    OverlayStageBudget,
    Phase3OverlayResources,
)
from scripts.run_phase3_derivative_search import (
    DERIVATIVE_EXECUTION_SOURCE_PATHS,
    PUBLISHED_STAGE_FILES,
    _annotate_run,
    _attach_stage_audit_columns,
    _build_candidate_manifest,
    _build_stage_manifest,
    _collect_completed_runs,
    _ensure_stage_manifest,
    _execution_source_hashes,
    _load_cached_run,
    _load_candidates,
    _load_completed_parent,
    _run_stage,
    _validate_run,
    _withhold_stage_publication,
    _write_progress,
    _write_run_artifacts,
)


def _resources(tmp_path: Path) -> Phase3OverlayResources:
    dates = pd.bdate_range("2020-01-02", "2020-04-30")
    daily = pd.DataFrame(
        [
            {
                "trade_date": trade_date.strftime("%Y%m%d"),
                "contract": "IF2006",
                "product": "IF",
                "instrument_type": "future",
                "option_type": "",
                "strike": None,
                "multiplier": 300.0,
                "open": 100.0 + index * 0.05,
                "settle": 100.0 + index * 0.05,
                "volume": 1000.0,
                "open_interest": 1000.0,
                "delta": None,
                "open_executable": True,
                "settlement_mark_valid": True,
            }
            for index, trade_date in enumerate(dates)
        ]
    )
    history = pd.DataFrame(
        {
            "snapshot_date": ["20200102"],
            "contract": ["IF2006"],
            "official_last_trade_date": ["20200619"],
        }
    )
    catalog = CffexCatalog.from_frames(
        daily,
        pd.DataFrame(
            {"contract": ["IF2006"], "last_trade_date": ["20200619"]}
        ),
        expiry_history=history,
    )
    catalog._expiry_mode = "official_asof_history"
    path = tmp_path / "settlement.parquet"
    pd.DataFrame(
        [
            {
                "snapshot_date": "20200102",
                "instrument_type": "future",
                "parameter_scope": "contract",
                "contract_or_series": "IF2006",
                "product": "IF",
                "long_margin_rate": 0.10,
                "short_margin_rate": 0.10,
                "trading_fee_value": 0.00002,
                "trading_fee_unit": "notional_rate",
                "settlement_fee_value": 0.0001,
                "settlement_fee_unit": "notional_rate",
                "settlement_fee_kind": "delivery",
                "close_today_fee_multiplier": 10.0,
                "close_today_fee_semantics": "fraction_of_trading_fee",
                "option_shorting_enabled": False,
                "source_section_title_matches_snapshot": True,
                "source_sha256": "a" * 64,
            }
        ]
    ).to_parquet(path, index=False)
    return Phase3OverlayResources(
        catalog=catalog,
        execution_parameters=CffexExecutionParameterSchedule(path, validate=False),
        assumptions=DevelopmentResearchAssumptions(
            prior_day_volume_participation=1.0
        ),
    )


def _spec() -> OverlaySearchSpec:
    return OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=FuturesOverlaySpec(
            product="IF",
            direction="long",
            fixed_contracts=1,
            min_dte=5,
            rebalance_frequency="monthly",
        ),
    )


def _budget() -> OverlayStageBudget:
    return OverlayStageBudget(
        stage="screen",
        max_combinations=2,
        max_windows=2,
        max_window_evaluations=4,
        max_promotions=1,
        futures_slippage_bps=1.0,
        option_slippage_bps=2.0,
        prior_day_volume_participation=1.0,
    )


def _config() -> FreeRealBacktestConfig:
    return FreeRealBacktestConfig(
        monthly_deposit=30_000.0,
        target_month_12=50_000.0,
        target_month_24=60_000.0,
        window_months=2,
        min_trading_days=10,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )


def _stage_manifest(
    spec: OverlaySearchSpec,
    budget: OverlayStageBudget,
    *,
    source_hash: str = "a" * 64,
    parent_overlay_ids: tuple[str, ...] = (),
    parent_candidate_manifest_signature: str = "root",
) -> dict[str, object]:
    return _build_stage_manifest(
        search_id="test_derivative_search",
        search_config_sha256="c" * 64,
        stage=spec.stage,
        budget=budget,
        artifact_hashes={"daily": "d" * 64},
        execution_source_hashes={
            path: source_hash for path in DERIVATIVE_EXECUTION_SOURCE_PATHS
        },
        backtest={"window_months": 2, "min_trading_days": 10},
        configured_specs=(spec,),
        expected_specs=(spec,),
        lineage={spec.overlay_id: parent_overlay_ids},
        parent_stage="root" if not parent_overlay_ids else "screen",
        parent_stage_signature="root" if not parent_overlay_ids else "screen-stage",
        parent_candidate_manifest_signature=parent_candidate_manifest_signature,
    )


def test_overlay_only_evaluator_uses_product_availability_and_shared_resources(
    tmp_path: Path,
) -> None:
    resources = _resources(tmp_path)
    windows = select_overlay_windows(
        resources,
        _spec(),
        _config(),
        max_windows=2,
    )

    evaluated = evaluate_overlay_spec(
        resources,
        _spec(),
        _config(),
        _budget(),
    )

    assert len(windows) == 2
    assert len(evaluated) == 4
    assert set(evaluated["deposit_timing"]) == {"beginning", "ending"}
    assert set(evaluated["data_tier"]) == {"official_exchange_daily"}
    assert evaluated["derivative_coordinator_active"].eq(1.0).all()
    assert evaluated["derivative_filled_contracts"].sum() > 0.0
    assert evaluated["futures_expiry_settlements"].ge(0.0).all()
    assert evaluated["option_expiry_settlements"].eq(0.0).all()
    assert evaluated["default_events"].eq(0.0).all()


def test_overlay_aggregation_is_two_timing_fail_closed_and_bounded(
    tmp_path: Path,
) -> None:
    evaluated = evaluate_overlay_spec(
        _resources(tmp_path),
        _spec(),
        _config(),
        _budget(),
    )
    leaderboard = aggregate_overlay_windows(evaluated, _config())
    candidates = select_overlay_candidates(leaderboard, max_promotions=1)

    assert len(leaderboard) == 2
    assert leaderboard["margin_accounting_valid"].all()
    assert leaderboard["max_missing_execution_parameter_events"].eq(0.0).all()
    assert leaderboard["passes_overlay_activity_gate"].all()
    assert leaderboard["overall_derivative_active_window_share"].gt(0.0).all()
    assert leaderboard["derivative_active_window_share"].ge(0.0).all()
    assert leaderboard["futures_expiry_settlements"].ge(0.0).all()
    assert leaderboard["option_expiry_settlements"].eq(0.0).all()
    assert leaderboard["formal_evidence_available"].eq(False).all()
    assert len(candidates) == 1
    assert candidates.iloc[0]["strategy"] == _spec().overlay_id
    assert candidates.iloc[0]["total_futures_expiry_settlements"] >= 0.0
    assert candidates.iloc[0]["total_option_expiry_settlements"] == 0.0


def test_zero_trade_spec_is_excluded_but_active_failed_spec_remains_bounded(
    tmp_path: Path,
) -> None:
    evaluated = evaluate_overlay_spec(
        _resources(tmp_path),
        _spec(),
        _config(),
        _budget(),
    )
    base = aggregate_overlay_windows(evaluated, _config())
    live = base.copy()
    live["strategy"] = "live-but-development-failed"
    live["passes_overlay_development_gates"] = False
    live["p_success"] = 0.25
    live["score"] = 1.0
    second_live = live.copy()
    second_live["strategy"] = "second-live"
    second_live["derivative_active_window_share"] = 0.5
    second_live["overall_derivative_active_window_share"] = 0.5
    second_live["p_success"] = 0.10
    second_live["score"] = 0.5
    zero = live.copy()
    zero["strategy"] = "zero-trade-high-rank"
    zero["passes_overlay_activity_gate"] = False
    zero["derivative_requested_contracts"] = 0.0
    zero["derivative_filled_contracts"] = 0.0
    zero["derivative_active_window_share"] = 0.0
    zero["overall_derivative_active_window_share"] = 0.0
    zero["p_success"] = 1.0
    zero["score"] = 999.0

    candidates = select_overlay_candidates(
        pd.concat([zero, second_live, live], ignore_index=True),
        max_promotions=1,
    )

    assert len(candidates) == 1
    assert candidates.iloc[0]["strategy"] in {
        "live-but-development-failed",
        "second-live",
    }
    assert candidates.iloc[0]["promotion_reason"] == "bounded_exploratory_rank"
    assert "zero-trade-high-rank" not in set(candidates["strategy"])


def test_activity_gate_requires_realized_trades_for_both_deposit_timings(
    tmp_path: Path,
) -> None:
    evaluated = evaluate_overlay_spec(
        _resources(tmp_path),
        _spec(),
        _config(),
        _budget(),
    )
    ending = evaluated["deposit_timing"].eq("ending")
    evaluated.loc[
        ending,
        ["derivative_requested_contracts", "derivative_filled_contracts"],
    ] = 0.0

    leaderboard = aggregate_overlay_windows(evaluated, _config())
    candidates = select_overlay_candidates(leaderboard, max_promotions=1)

    assert not leaderboard["passes_overlay_activity_gate"].any()
    assert not leaderboard.loc[
        leaderboard["deposit_timing"].eq("ending"),
        "passes_overlay_timing_activity_gate",
    ].any()
    assert candidates.empty


def test_neighborhood_specs_inherit_only_matching_parent_kernel() -> None:
    parent = _spec()
    child = OverlaySearchSpec(
        stage="neighborhood",
        composition="futures_only",
        futures=FuturesOverlaySpec(
            product="IF",
            direction="long",
            target_notional_multiple=0.20,
            min_dte=5,
            rebalance_frequency="monthly",
        ),
    )
    unrelated = OverlaySearchSpec(
        stage="neighborhood",
        composition="futures_only",
        futures=FuturesOverlaySpec(
            product="IF",
            direction="short",
            target_notional_multiple=0.20,
            min_dte=5,
            rebalance_frequency="monthly",
        ),
    )

    selected, lineage = select_inherited_overlay_specs(
        (child, unrelated),
        (parent,),
        (parent.overlay_id,),
    )

    assert overlay_lineage_key(parent) == overlay_lineage_key(child)
    assert selected == (child,)
    assert lineage == {child.overlay_id: (parent.overlay_id,)}

    budget = OverlayStageBudget(
        stage="neighborhood",
        max_combinations=2,
        max_windows=2,
        max_window_evaluations=4,
        max_promotions=1,
        futures_slippage_bps=1.0,
        option_slippage_bps=2.0,
        prior_day_volume_participation=1.0,
    )
    first_manifest = _stage_manifest(
        child,
        budget,
        parent_overlay_ids=(parent.overlay_id,),
        parent_candidate_manifest_signature="a" * 24,
    )
    changed_parent_manifest = _stage_manifest(
        child,
        budget,
        parent_overlay_ids=(parent.overlay_id,),
        parent_candidate_manifest_signature="b" * 24,
    )
    assert first_manifest["stage_signature"] != changed_parent_manifest["stage_signature"]
    assert first_manifest["lineage"] == [
        {
            "overlay_id": child.overlay_id,
            "parent_overlay_ids": [parent.overlay_id],
        }
    ]


def test_dynamic_direction_family_and_lineage_preserve_signal_kernel() -> None:
    def dynamic_spec(
        *,
        stage: str,
        kind: str,
        position_mode: str,
        lookback_days: int,
        neutral_band: float,
        trend_variant: str | None = None,
    ) -> OverlaySearchSpec:
        rule_kwargs: dict[str, object] = {
            "kind": kind,
            "position_mode": position_mode,
            "neutral_band": neutral_band,
        }
        if kind == "time_series_momentum":
            rule_kwargs["lookback_days"] = lookback_days
        else:
            rule_kwargs.update(
                trend_variant=trend_variant,
                lookback_days=lookback_days,
            )
        return OverlaySearchSpec(
            stage=stage,
            composition="futures_only",
            futures=FuturesOverlaySpec(
                product="IF",
                direction="flat",
                fixed_contracts=1,
                min_dte=5,
                rebalance_frequency="daily",
            ),
            direction_rule=FuturesDirectionRule(**rule_kwargs),
        )

    parent = dynamic_spec(
        stage="screen",
        kind="time_series_momentum",
        position_mode="long_short_flat",
        lookback_days=60,
        neutral_band=0.02,
    )
    numeric_neighbor = dynamic_spec(
        stage="neighborhood",
        kind="time_series_momentum",
        position_mode="long_short_flat",
        lookback_days=40,
        neutral_band=0.01,
    )
    wrong_mode = dynamic_spec(
        stage="neighborhood",
        kind="time_series_momentum",
        position_mode="long_flat",
        lookback_days=40,
        neutral_band=0.01,
    )
    wrong_variant = dynamic_spec(
        stage="neighborhood",
        kind="moving_average_or_breakout",
        position_mode="long_short_flat",
        lookback_days=40,
        neutral_band=0.005,
        trend_variant="breakout",
    )

    assert overlay_family(parent) == "P35_cffex_dynamic_direction_overlay"
    assert overlay_lineage_key(parent) == overlay_lineage_key(numeric_neighbor)
    assert overlay_lineage_key(parent) != overlay_lineage_key(wrong_mode)
    assert overlay_lineage_key(parent) != overlay_lineage_key(wrong_variant)

    selected, lineage = select_inherited_overlay_specs(
        (numeric_neighbor, wrong_mode, wrong_variant),
        (parent,),
        (parent.overlay_id,),
    )
    assert selected == (numeric_neighbor,)
    assert lineage == {numeric_neighbor.overlay_id: (parent.overlay_id,)}


def test_execution_source_drift_invalidates_stage_manifest_and_run_cache(
    tmp_path: Path,
) -> None:
    source_hashes = _execution_source_hashes()
    required_sources = {
        "src/quant_proof/phase3_derivative_search.py",
        "src/quant_proof/phase3_overlay_factory.py",
        "src/quant_proof/phase3_overlay_coordinator.py",
        "src/quant_proof/derivative_event_loop.py",
        "src/quant_proof/engine/combined_account.py",
        "src/quant_proof/engine/account.py",
        "src/quant_proof/engine/portfolio.py",
        "src/quant_proof/engine/cost.py",
        "src/quant_proof/engine/exchange_rules.py",
        "src/quant_proof/engine/execution.py",
        "src/quant_proof/engine/risk.py",
        "src/quant_proof/cffex_catalog.py",
        "src/quant_proof/cffex_execution_parameters.py",
        "src/quant_proof/free_sources/cffex_adapter.py",
        "src/quant_proof/free_sources/cffex_settlement_params.py",
        "src/quant_proof/free_real_backtest.py",
    }
    assert required_sources.issubset(DERIVATIVE_EXECUTION_SOURCE_PATHS)
    assert set(DERIVATIVE_EXECUTION_SOURCE_PATHS) == set(source_hashes)
    assert all(len(value) == 64 for value in source_hashes.values())

    manifest = _stage_manifest(_spec(), _budget(), source_hash="a" * 64)
    drifted = _stage_manifest(_spec(), _budget(), source_hash="b" * 64)
    assert manifest["stage_signature"] != drifted["stage_signature"]
    stage_manifest_path = tmp_path / "screen" / "stage_manifest.json"
    _ensure_stage_manifest(stage_manifest_path, manifest)
    with pytest.raises(ValueError, match="manifest drifted"):
        _ensure_stage_manifest(stage_manifest_path, drifted)

    evaluated = evaluate_overlay_spec(
        _resources(tmp_path),
        _spec(),
        _config(),
        _budget(),
    )
    annotated = _annotate_run(
        evaluated,
        _spec(),
        manifest,
        parent_overlay_ids=(),
    )
    _validate_run(annotated, _spec(), manifest, parent_overlay_ids=())
    run_path = tmp_path / "runs" / f"{_spec().overlay_id}.csv"
    _write_run_artifacts(
        run_path,
        annotated,
        _spec(),
        manifest,
        parent_overlay_ids=(),
    )
    assert len(
        _load_cached_run(
            run_path,
            _spec(),
            manifest,
            parent_overlay_ids=(),
        )
    ) == len(annotated)
    with pytest.raises(ValueError, match="stage signature mismatch"):
        _load_cached_run(
            run_path,
            _spec(),
            drifted,
            parent_overlay_ids=(),
        )

    tampered = pd.read_csv(run_path, keep_default_na=False)
    tampered.loc[0, "w12"] = float(tampered.loc[0, "w12"]) + 1.0
    tampered.to_csv(run_path, index=False)
    with pytest.raises(ValueError, match="cache manifest drifted"):
        _load_cached_run(
            run_path,
            _spec(),
            manifest,
            parent_overlay_ids=(),
        )


def test_candidate_manifest_binds_promotions_and_rejects_csv_drift(
    tmp_path: Path,
) -> None:
    manifest = _stage_manifest(_spec(), _budget())
    evaluated = evaluate_overlay_spec(
        _resources(tmp_path),
        _spec(),
        _config(),
        _budget(),
    )
    leaderboard = aggregate_overlay_windows(evaluated, _config())
    candidates = select_overlay_candidates(leaderboard, max_promotions=1)
    candidates = _attach_stage_audit_columns(
        candidates,
        (_spec(),),
        manifest,
        {_spec().overlay_id: ()},
    )
    stage_root = tmp_path / "screen"
    stage_root.mkdir()
    candidate_path = stage_root / "candidates.csv"
    candidates.to_csv(candidate_path, index=False)
    candidate_manifest = _build_candidate_manifest(
        candidate_path,
        candidates,
        manifest,
        max_promotions=1,
    )
    (stage_root / "candidates.manifest.json").write_text(
        json.dumps(candidate_manifest),
        encoding="utf-8",
    )

    loaded, signature = _load_candidates(
        stage_root,
        manifest,
        max_promotions=1,
    )
    assert len(loaded) == 1
    assert signature == candidate_manifest["manifest_signature"]

    loaded.loc[0, "worst_timing_median_w24"] = (
        float(loaded.loc[0, "worst_timing_median_w24"]) + 1.0
    )
    loaded.to_csv(candidate_path, index=False)
    with pytest.raises(ValueError, match="candidate manifest drifted"):
        _load_candidates(stage_root, manifest, max_promotions=1)


def test_incomplete_screen_blocks_parent_loading_and_withholds_publication(
    tmp_path: Path,
) -> None:
    stage_root = tmp_path / "screen"
    manifest = _stage_manifest(_spec(), _budget())
    _ensure_stage_manifest(stage_root / "stage_manifest.json", manifest)
    _write_progress(
        stage_root / "progress.json",
        {
            "stage_signature": manifest["stage_signature"],
            "stage_manifest_signature": manifest["manifest_signature"],
            "completed_specs": 0,
            "complete": False,
            "leaderboard_withheld": True,
        },
    )
    with pytest.raises(ValueError, match="not completely published"):
        _load_completed_parent(
            stage_root,
            manifest,
            max_promotions=_budget().max_promotions,
        )

    for name in PUBLISHED_STAGE_FILES:
        path = stage_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale\n", encoding="utf-8")
    withheld = _withhold_stage_publication(stage_root)
    assert set(withheld) == set(PUBLISHED_STAGE_FILES)
    assert all(not (stage_root / name).exists() for name in PUBLISHED_STAGE_FILES)


def test_completed_run_collection_ignores_appledouble_sidecars(tmp_path: Path) -> None:
    manifest = _stage_manifest(_spec(), _budget())
    evaluated = evaluate_overlay_spec(
        _resources(tmp_path),
        _spec(),
        _config(),
        _budget(),
    )
    annotated = _annotate_run(
        evaluated,
        _spec(),
        manifest,
        parent_overlay_ids=(),
    )
    runs_root = tmp_path / "runs"
    _write_run_artifacts(
        runs_root / f"{_spec().overlay_id}.csv",
        annotated,
        _spec(),
        manifest,
        parent_overlay_ids=(),
    )
    (runs_root / "._junk.csv").write_text("not,a,run\n", encoding="utf-8")
    (runs_root / "._junk.json").write_text("not-json", encoding="utf-8")

    frames, inventory = _collect_completed_runs(
        runs_root,
        (_spec(),),
        manifest,
        {_spec().overlay_id: ()},
    )

    assert len(frames) == 1
    assert [row["overlay_id"] for row in inventory] == [_spec().overlay_id]


def test_stage_resume_withholds_until_complete_then_reuses_all_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=FuturesOverlaySpec(
            product="IF",
            direction="short",
            fixed_contracts=1,
            min_dte=5,
            rebalance_frequency="monthly",
        ),
    )
    specs = (_spec(), second)
    lineage = {spec.overlay_id: () for spec in specs}
    manifest = _build_stage_manifest(
        search_id="resume_search",
        search_config_sha256="c" * 64,
        stage="screen",
        budget=_budget(),
        artifact_hashes={"daily": "d" * 64},
        execution_source_hashes=_execution_source_hashes(),
        backtest={"window_months": 1, "min_trading_days": 10},
        configured_specs=specs,
        expected_specs=specs,
        lineage=lineage,
    )
    output_root = tmp_path / "outputs"
    stage_root = output_root / "resume_search" / "screen"
    stage_root.mkdir(parents=True)
    for name in PUBLISHED_STAGE_FILES:
        (stage_root / name).write_text("stale\n", encoding="utf-8")
    (stage_root / "._junk.csv").write_text("appledouble", encoding="utf-8")
    monkeypatch.setattr(
        "scripts.run_phase3_derivative_search._write_report",
        lambda *args, **kwargs: None,
    )

    first_complete = _run_stage(
        search_id="resume_search",
        stage="screen",
        configured_specs=specs,
        specs=specs,
        resources=_resources(tmp_path),
        backtest_config=_config(),
        budget=_budget(),
        output_root=output_root,
        stage_manifest=manifest,
        lineage=lineage,
        max_specs=1,
        resume=True,
    )
    assert not first_complete
    assert all(not (stage_root / name).exists() for name in PUBLISHED_STAGE_FILES)
    assert (stage_root / "._junk.csv").exists()

    second_complete = _run_stage(
        search_id="resume_search",
        stage="screen",
        configured_specs=specs,
        specs=specs,
        resources=_resources(tmp_path),
        backtest_config=_config(),
        budget=_budget(),
        output_root=output_root,
        stage_manifest=manifest,
        lineage=lineage,
        max_specs=1,
        resume=True,
    )
    assert second_complete
    progress = json.loads((stage_root / "progress.json").read_text(encoding="utf-8"))
    assert progress["complete"] is True
    assert progress["cached_at_start"] == [_spec().overlay_id]
    assert progress["completed_specs"] == 2

    def fail_if_recomputed(*args, **kwargs):
        raise AssertionError("valid derivative cache was not reused")

    monkeypatch.setattr(
        "scripts.run_phase3_derivative_search.evaluate_overlay_spec",
        fail_if_recomputed,
    )
    assert _run_stage(
        search_id="resume_search",
        stage="screen",
        configured_specs=specs,
        specs=specs,
        resources=_resources(tmp_path),
        backtest_config=_config(),
        budget=_budget(),
        output_root=output_root,
        stage_manifest=manifest,
        lineage=lineage,
        max_specs=0,
        resume=True,
    )

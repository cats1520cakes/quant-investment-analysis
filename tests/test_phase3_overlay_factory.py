from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Callable

import pytest
import yaml

import quant_proof.phase3_overlay_factory as overlay_factory
from quant_proof.engine.account import Account
from quant_proof.engine.combined_account import CombinedAccount
from quant_proof.phase3_overlay_coordinator import FuturesOverlaySpec


ROOT = Path(__file__).parents[1]
SEARCH_CONFIG = ROOT / "config" / "phase3_derivative_overlay_grid.yaml"
TIMING_CONFIG = ROOT / "config" / "phase3_derivative_timing_grid.yaml"


def _install_factory_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expiry_mode: str = "official_asof_history",
    unresolved: tuple[str, ...] = (),
) -> dict[str, list[object]]:
    calls: dict[str, list[object]] = {
        "catalog": [],
        "schedule": [],
        "loops": [],
        "coordinators": [],
    }

    class FakeCatalog:
        def __init__(
            self,
            panel_path: str | Path,
            master_path: str | Path,
            *,
            trade_parameter_metadata_path: str | Path,
        ) -> None:
            self.expiry_mode = expiry_mode
            self.unresolved_expiry_contracts = unresolved
            calls["catalog"].append(
                (self, panel_path, master_path, trade_parameter_metadata_path)
            )

    class FakeSchedule:
        def __init__(
            self,
            path: str | Path,
            *,
            validate_artifact: bool,
            verify_artifact_sources: bool,
        ) -> None:
            calls["schedule"].append(
                (self, path, validate_artifact, verify_artifact_sources)
            )

    class FakeLoop:
        def __init__(
            self,
            *,
            account: CombinedAccount,
            catalog: FakeCatalog,
            assumptions: object,
            execution_parameters: FakeSchedule,
        ) -> None:
            self.account = account
            self.catalog = catalog
            self.assumptions = assumptions
            self.execution_parameters = execution_parameters
            self.uses_official_execution_parameters = execution_parameters is not None
            calls["loops"].append(self)

    class FakeCoordinator:
        def __init__(
            self,
            *,
            account: CombinedAccount,
            event_loop: FakeLoop,
            futures_spec: object,
            option_spec: object,
        ) -> None:
            self.account = account
            self.event_loop = event_loop
            self.futures_spec = futures_spec
            self.option_spec = option_spec
            calls["coordinators"].append(self)

    monkeypatch.setattr(overlay_factory, "CffexCatalog", FakeCatalog)
    monkeypatch.setattr(
        overlay_factory, "CffexExecutionParameterSchedule", FakeSchedule
    )
    monkeypatch.setattr(overlay_factory, "DerivativeEventLoop", FakeLoop)
    monkeypatch.setattr(overlay_factory, "Phase3OverlayCoordinator", FakeCoordinator)
    return calls


def _build_fake_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expiry_mode: str = "official_asof_history",
    unresolved: tuple[str, ...] = (),
) -> tuple[Callable[[CombinedAccount], object], dict[str, list[object]]]:
    calls = _install_factory_fakes(
        monkeypatch,
        expiry_mode=expiry_mode,
        unresolved=unresolved,
    )
    spec = overlay_factory.OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=FuturesOverlaySpec(product="IF", fixed_contracts=1),
    )
    factory = overlay_factory.build_phase3_overlay_factory(
        "panel.parquet",
        "master.parquet",
        "official_expiry_history.parquet",
        "official_settlement_parameters.parquet",
        spec,
    )
    return factory, calls


def _mutated_config(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], None],
) -> Path:
    raw = yaml.safe_load(SEARCH_CONFIG.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "overlay_search.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_factory_loads_official_resources_once_and_isolates_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, calls = _build_fake_factory(monkeypatch)

    first = factory(CombinedAccount(cash=10_000.0))
    second = factory(CombinedAccount(cash=20_000.0))

    assert len(calls["catalog"]) == 1
    assert len(calls["schedule"]) == 1
    assert len(calls["loops"]) == 2
    assert len(calls["coordinators"]) == 2
    assert first is not second
    assert first.event_loop is not second.event_loop
    assert first.account is not second.account
    assert first.event_loop.catalog is second.event_loop.catalog
    assert (
        first.event_loop.execution_parameters
        is second.event_loop.execution_parameters
    )
    assert first.event_loop.assumptions is second.event_loop.assumptions
    assert first.event_loop.execution_parameters is calls["schedule"][0][0]
    assert calls["schedule"][0][2:] == (True, False)
    assert calls["catalog"][0][3] == "official_expiry_history.parquet"
    assert first.futures_spec.fixed_contracts == 1
    assert first.option_spec is None


def test_loaded_resources_are_reused_across_distinct_overlay_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_factory_fakes(monkeypatch)
    resources = overlay_factory.load_phase3_overlay_resources(
        "panel.parquet",
        "master.parquet",
        "official_expiry_history.parquet",
        "official_settlement_parameters.parquet",
    )
    first_spec = overlay_factory.OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=FuturesOverlaySpec(product="IF", fixed_contracts=1),
    )
    second_spec = overlay_factory.OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=FuturesOverlaySpec(product="IM", fixed_contracts=1),
    )

    first = overlay_factory.build_phase3_overlay_factory_from_resources(
        resources,
        first_spec,
    )(CombinedAccount(cash=10_000.0))
    second = overlay_factory.build_phase3_overlay_factory_from_resources(
        resources,
        second_spec,
    )(CombinedAccount(cash=20_000.0))

    assert len(calls["catalog"]) == 1
    assert len(calls["schedule"]) == 1
    assert first.event_loop.catalog is second.event_loop.catalog
    assert (
        first.event_loop.execution_parameters
        is second.event_loop.execution_parameters
    )
    assert first.futures_spec.product == "IF"
    assert second.futures_spec.product == "IM"


def test_dynamic_direction_map_is_resolved_once_and_reused_across_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_factory_fakes(monkeypatch)
    resources = overlay_factory.load_phase3_overlay_resources(
        "panel.parquet",
        "master.parquet",
        "official_expiry_history.parquet",
        "official_settlement_parameters.parquet",
    )
    rule = overlay_factory.FuturesDirectionRule(
        kind="time_series_momentum",
        position_mode="long_short_flat",
        lookback_days=20,
    )
    spec = overlay_factory.OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=FuturesOverlaySpec(
            product="IF",
            direction="flat",
            fixed_contracts=1,
            rebalance_frequency="daily",
        ),
        direction_rule=rule,
    )
    resolutions: list[tuple[object, str, object, int]] = []

    def resolve_once(
        catalog: object,
        product: str,
        selected_rule: object,
        *,
        min_dte: int,
    ) -> dict[str, str]:
        resolutions.append((catalog, product, selected_rule, min_dte))
        return {"20260105": "long", "20260106": "short"}

    monkeypatch.setattr(
        overlay_factory,
        "build_futures_direction_map",
        resolve_once,
    )
    first_factory = overlay_factory.build_phase3_overlay_factory_from_resources(
        resources,
        spec,
    )
    replaced_resources = replace(resources, assumptions=resources.assumptions)
    second_factory = overlay_factory.build_phase3_overlay_factory_from_resources(
        replaced_resources,
        spec,
    )

    first = first_factory(CombinedAccount(cash=10_000.0))
    second = first_factory(CombinedAccount(cash=20_000.0))
    third = second_factory(CombinedAccount(cash=30_000.0))

    assert len(resolutions) == 1
    assert (
        resources._direction_runtime_cache
        is replaced_resources._direction_runtime_cache
    )
    assert resolutions[0] == (calls["catalog"][0][0], "IF", rule, 5)
    assert first.futures_spec is second.futures_spec is third.futures_spec
    assert first.futures_spec.direction_on("20260105") == "long"
    assert first.futures_spec.direction_on("20260107") == "flat"


def test_factory_rejects_non_combined_account_before_window_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, calls = _build_fake_factory(monkeypatch)

    with pytest.raises(ValueError, match="CombinedAccount"):
        factory(Account(cash=10_000.0))

    assert calls["loops"] == []
    assert calls["coordinators"] == []


@pytest.mark.parametrize(
    ("expiry_mode", "unresolved", "message"),
    [
        ("master_observation", (), "official_asof_history"),
        ("official_asof_history", ("IF2609",), "unresolved expiry"),
    ],
)
def test_factory_fails_before_schedule_load_for_unofficial_expiry_state(
    monkeypatch: pytest.MonkeyPatch,
    expiry_mode: str,
    unresolved: tuple[str, ...],
    message: str,
) -> None:
    calls = _install_factory_fakes(
        monkeypatch,
        expiry_mode=expiry_mode,
        unresolved=unresolved,
    )

    with pytest.raises(overlay_factory.Phase3OverlayFactoryError, match=message):
        overlay_factory.build_phase3_overlay_factory(
            "panel.parquet",
            "master.parquet",
            "official_expiry_history.parquet",
            "official_settlement_parameters.parquet",
        )

    assert len(calls["catalog"]) == 1
    assert calls["schedule"] == []
    assert calls["loops"] == []


def test_search_expansion_is_frozen_deterministic_and_machine_auditable() -> None:
    first = overlay_factory.load_overlay_search_space(SEARCH_CONFIG)
    second = overlay_factory.load_overlay_search_space(SEARCH_CONFIG)

    assert first == second
    assert first.config_sha256 == second.config_sha256
    assert [spec.overlay_id for spec in first.specs] == [
        spec.overlay_id for spec in second.specs
    ]
    assert len(first.specs) == 79
    assert {
        stage: len(first.specs_for_stage(stage))
        for stage in ("screen", "neighborhood", "stress")
    } == {"screen": 34, "neighborhood": 28, "stress": 17}
    assert first.stock_strategy_binding == "none"
    assert all(
        len(stage_specs)
        <= first.budget_for_stage(stage).max_combinations
        for stage in ("screen", "neighborhood", "stress")
        if (stage_specs := first.specs_for_stage(stage))
    )
    json.dumps(first.to_audit_manifest(), allow_nan=False, sort_keys=True)
    with pytest.raises(FrozenInstanceError):
        first.specs[0].stage = "stress"  # type: ignore[misc]


def test_overlay_id_is_semantic_and_stable_across_search_stages() -> None:
    futures = FuturesOverlaySpec(
        product="IM",
        direction="short",
        target_notional_multiple=0.25,
        cash_buffer_pct=0.2,
    )
    screen = overlay_factory.OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=futures,
        source_label="first label",
    )
    stress = overlay_factory.OverlaySearchSpec(
        stage="stress",
        composition="futures_only",
        futures=futures,
        source_label="renamed label",
    )
    changed = overlay_factory.OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=FuturesOverlaySpec(
            product="IM",
            direction="short",
            target_notional_multiple=0.35,
            cash_buffer_pct=0.2,
        ),
    )

    assert screen.overlay_id == stress.overlay_id
    assert screen.overlay_id != changed.overlay_id


def test_dynamic_overlay_id_binds_only_the_compact_direction_rule() -> None:
    rule = overlay_factory.FuturesDirectionRule(
        kind="moving_average_or_breakout",
        position_mode="long_short_flat",
        trend_variant="breakout",
        lookback_days=20,
        neutral_band=0.005,
    )
    futures = FuturesOverlaySpec(
        product="IM",
        direction="flat",
        fixed_contracts=1,
        rebalance_frequency="daily",
        cash_buffer_pct=0.25,
    )
    screen = overlay_factory.OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=futures,
        direction_rule=rule,
        source_label="screen label",
    )
    stress = overlay_factory.OverlaySearchSpec(
        stage="stress",
        composition="futures_only",
        futures=futures,
        direction_rule=rule,
        source_label="renamed label",
    )
    changed = overlay_factory.OverlaySearchSpec(
        stage="screen",
        composition="futures_only",
        futures=futures,
        direction_rule=overlay_factory.FuturesDirectionRule(
            kind="moving_average_or_breakout",
            position_mode="long_short_flat",
            trend_variant="breakout",
            lookback_days=40,
            neutral_band=0.005,
        ),
    )
    audit_json = json.dumps(screen.to_audit_dict(), sort_keys=True)

    assert screen.overlay_id == stress.overlay_id
    assert screen.overlay_id != changed.overlay_id
    assert screen.to_audit_dict()["futures"]["direction_by_signal_date"] is None
    assert "2026" not in audit_json
    assert rule.to_compact_dict() == screen.to_audit_dict()["direction_rule"]
    assert len(audit_json) < 1_000


def test_independent_timing_grid_is_small_whole_lot_and_option_free() -> None:
    timing = overlay_factory.load_overlay_search_space(TIMING_CONFIG)
    static = overlay_factory.load_overlay_search_space(SEARCH_CONFIG)

    assert timing.search_id == "phase3_derivative_timing_v1"
    assert timing.search_id != static.search_id
    assert {
        stage: len(timing.specs_for_stage(stage))
        for stage in ("screen", "neighborhood", "stress")
    } == {"screen": 24, "neighborhood": 8, "stress": 4}
    assert len(timing.specs_for_stage("screen")) <= 24
    assert {spec.composition for spec in timing.specs} == {"futures_only"}
    assert all(spec.long_option is None for spec in timing.specs)
    assert all(spec.direction_rule is not None for spec in timing.specs)
    assert {
        spec.direction_rule.kind
        for spec in timing.specs_for_stage("screen")
        if spec.direction_rule is not None
    } == {
        "time_series_momentum",
        "moving_average_or_breakout",
        "front_next_carry",
    }
    assert {
        spec.direction_rule.position_mode
        for spec in timing.specs_for_stage("screen")
        if spec.direction_rule is not None
    } == {"long_flat", "long_short_flat"}
    assert all(
        spec.futures is not None
        and spec.futures.fixed_contracts == 1
        and spec.futures.target_notional_multiple is None
        and spec.futures.margin_budget is None
        and spec.futures.max_contracts == 1
        and spec.futures.cash_buffer_pct >= 0.25
        for spec in timing.specs
    )


def test_futures_candidates_are_whole_lot_and_have_one_sizing_mode(
    tmp_path: Path,
) -> None:
    space = overlay_factory.load_overlay_search_space(SEARCH_CONFIG)
    futures_specs = [spec.futures for spec in space.specs if spec.futures is not None]
    assert futures_specs
    for futures in futures_specs:
        assert sum(
            value is not None
            for value in (
                futures.fixed_contracts,
                futures.target_notional_multiple,
                futures.margin_budget,
            )
        ) == 1
        if futures.fixed_contracts is not None:
            assert isinstance(futures.fixed_contracts, int)
            assert futures.fixed_contracts > 0

    def add_conflicting_mode(raw: dict[str, object]) -> None:
        pair = raw["stages"]["screen"]["futures_plus_long_option"]["pairs"][0]
        pair["futures"]["fixed_contracts"] = 1

    path = _mutated_config(tmp_path, add_conflicting_mode)
    with pytest.raises(
        overlay_factory.OverlaySearchConfigError,
        match="exactly one futures sizing mode",
    ):
        overlay_factory.load_overlay_search_space(path)


def test_search_budget_caps_fail_before_oversized_stage_is_accepted(
    tmp_path: Path,
) -> None:
    def lower_stage_cap(raw: dict[str, object]) -> None:
        screen = raw["stages"]["screen"]
        screen["max_combinations"] = 10
        screen["overlay_only"]["futures"]["max_combinations"] = 100_000

    stage_path = _mutated_config(tmp_path, lower_stage_cap)
    with pytest.raises(
        overlay_factory.OverlaySearchConfigError,
        match="remaining stage budget=10",
    ):
        overlay_factory.load_overlay_search_space(stage_path)

    def lower_evaluation_budget(raw: dict[str, object]) -> None:
        raw["stages"]["screen"]["budget"]["max_window_evaluations"] = 407

    budget_path = _mutated_config(tmp_path, lower_evaluation_budget)
    with pytest.raises(
        overlay_factory.OverlaySearchConfigError,
        match="window evaluations",
    ):
        overlay_factory.load_overlay_search_space(budget_path)


def test_long_options_are_long_only_and_short_option_fields_fail_closed(
    tmp_path: Path,
) -> None:
    space = overlay_factory.load_overlay_search_space(SEARCH_CONFIG)
    option_specs = [
        spec.long_option for spec in space.specs if spec.long_option is not None
    ]
    assert option_specs
    assert {option.product for option in option_specs} == {"IO", "HO", "MO"}
    assert {option.option_type for option in option_specs} == {"call", "put"}
    assert all(
        spec.to_audit_dict()["long_option"]["position_side"] == "long"
        for spec in space.specs
        if spec.long_option is not None
    )

    def inject_short_option(raw: dict[str, object]) -> None:
        pair = raw["stages"]["screen"]["futures_plus_long_option"]["pairs"][0]
        pair["long_option"]["position_side"] = "short"

    path = _mutated_config(tmp_path, inject_short_option)
    with pytest.raises(
        overlay_factory.OverlaySearchConfigError,
        match="short options are forbidden",
    ):
        overlay_factory.load_overlay_search_space(path)


def test_overlay_only_and_combo_candidates_are_explicit_without_stock_product() -> None:
    space = overlay_factory.load_overlay_search_space(SEARCH_CONFIG)
    compositions = {spec.composition for spec in space.specs}

    assert compositions == {
        "futures_only",
        "long_option_only",
        "futures_plus_long_option",
    }
    assert all(
        spec.source_label
        for spec in space.specs
        if spec.composition == "futures_plus_long_option"
    )
    manifest = space.to_audit_manifest()
    assert manifest["stock_strategy_binding"] == "none"
    assert "stock_strategy_ids" not in manifest
    assert all("stock_strategy_id" not in record for record in manifest["specs"])

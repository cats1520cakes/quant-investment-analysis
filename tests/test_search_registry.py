from __future__ import annotations

from pathlib import Path
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.search_registry import (
    RegistryValidationError,
    budget_summary,
    discover_phase3_derivative_families,
    discover_phase3_search_families,
    load_search_registry,
    relation_group_summary,
    validate_search_registry,
)


def _write_search_config(
    repo_root: Path,
    families: dict[str, int],
    name: str = "phase3_unit_search.yaml",
) -> str:
    relative = f"config/{name}"
    path = repo_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sampling": {"budget_per_family": 1},
        "strategy_spaces": {
            family_id: {
                "kind": "real_short_term_reversal",
                "budget": budget,
                "fixed": {"data_tier": "free_real"},
                "parameters": {"holding_k": [5, 10]},
            }
            for family_id, budget in families.items()
        },
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return relative


def _implemented_entry(family_id: str, config_path: str, budget: int) -> dict[str, object]:
    return {
        "family_id": family_id,
        "instrument_class": "a_share_equity",
        "signal_kernel": "short_term_reversal",
        "position_family": "cross_sectional_long_only",
        "sizing_family": "equal_weight",
        "data_tier": "free_real",
        "config_path": config_path,
        "config_binding": "strategy_space",
        "relation_group": f"fixture_{family_id.lower()}",
        "implementation_status": "implemented",
        "evidence_status": "not_tested",
        "readiness_status": "blocked",
        "blocking_dependency": "fixture corrected panel",
        "next_gate": "corrected_panel_development_funnel",
        "priority": "medium",
        "estimated_config_budget": budget,
    }


def _planned_entry(budget: int = 5, blocker: str = "fixture order interface") -> dict[str, object]:
    return {
        "family_id": "P90_fixture_atr_weighting",
        "instrument_class": "a_share_equity",
        "signal_kernel": "risk_weighting_overlay",
        "position_family": "stateful_long_only",
        "sizing_family": "atr_risk_weighted",
        "data_tier": "free_real",
        "config_path": "config/phase3_search_registry.yaml",
        "config_binding": "registry_only",
        "relation_group": "fixture_planned_atr",
        "implementation_status": "planned",
        "evidence_status": "not_tested",
        "readiness_status": "blocked",
        "blocking_dependency": blocker,
        "next_gate": "weighted_target_order_interface",
        "priority": "high",
        "estimated_config_budget": budget,
    }


def _write_registry(repo_root: Path, families: list[dict[str, object]]) -> Path:
    path = repo_root / "config" / "phase3_search_registry.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "registry_id": "fixture_registry_v1",
        "search_config_glob": "config/phase3_*search.yaml",
        "relation_group_audit": {},
        "families": families,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_registry_completeness_requires_every_config_family_once(tmp_path: Path) -> None:
    config_path = _write_search_config(tmp_path, {"S90_fixture_a": 3, "S91_fixture_b": 4})
    registry_path = _write_registry(
        tmp_path,
        [_implemented_entry("S90_fixture_a", config_path, 3)],
    )

    with pytest.raises(RegistryValidationError, match="missing from registry.*S91_fixture_b"):
        validate_search_registry(registry_path, repo_root=tmp_path)


def test_registry_rejects_duplicate_family_ids(tmp_path: Path) -> None:
    config_path = _write_search_config(tmp_path, {"S90_fixture_a": 3})
    entry = _implemented_entry("S90_fixture_a", config_path, 3)
    registry_path = _write_registry(tmp_path, [entry, dict(entry)])

    with pytest.raises(RegistryValidationError, match="duplicate registry family_id"):
        validate_search_registry(registry_path, repo_root=tmp_path)


def test_planned_registry_family_requires_nonempty_blocker(tmp_path: Path) -> None:
    config_path = _write_search_config(tmp_path, {"S90_fixture_a": 3})
    registry_path = _write_registry(
        tmp_path,
        [
            _implemented_entry("S90_fixture_a", config_path, 3),
            _planned_entry(blocker=""),
        ],
    )

    with pytest.raises(RegistryValidationError, match="planned family.*blocking_dependency"):
        validate_search_registry(registry_path, repo_root=tmp_path)


def test_budget_summary_separates_config_backed_and_planned(tmp_path: Path) -> None:
    config_path = _write_search_config(tmp_path, {"S90_fixture_a": 3})
    registry_path = _write_registry(
        tmp_path,
        [
            _implemented_entry("S90_fixture_a", config_path, 3),
            _planned_entry(budget=5),
        ],
    )

    registry = validate_search_registry(registry_path, repo_root=tmp_path)
    summary = budget_summary(registry)

    assert summary["family_count"] == 2
    assert summary["total_estimated_config_budget"] == 8
    assert summary["config_backed_budget"] == 3
    assert summary["registry_only_budget"] == 5
    assert summary["by_implementation_status"] == {"implemented": 3, "planned": 5}


def test_safe_yaml_loader_rejects_python_object_tags(tmp_path: Path) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text("!!python/object/apply:os.system ['echo unsafe']\n", encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="invalid safe YAML"):
        load_search_registry(path)


def test_real_repository_registry_validates_complete_search_space() -> None:
    registry_path = ROOT / "config" / "phase3_search_registry.yaml"
    registry = validate_search_registry(registry_path, repo_root=ROOT)
    discovered = discover_phase3_search_families(ROOT, registry.search_config_glob)
    derivative_discovered = discover_phase3_derivative_families(
        ROOT,
        registry.derivative_config_glob,
    )
    registered = {family.family_id for family in registry.config_backed_families}
    derivative_registered = {
        family.family_id for family in registry.derivative_grid_families
    }
    planned = {family.family_id for family in registry.planned_families}
    summary = budget_summary(registry)
    related = {
        row["relation_group"]: set(str(row["family_ids"]).split(", "))
        for row in relation_group_summary(registry)
    }

    assert registered == set(discovered)
    assert len(registered) == 20
    assert planned == set()
    assert derivative_registered == set(derivative_discovered) == {
        "P32_cffex_whole_lot_futures_overlay",
        "P33_cffex_long_option_convexity_budget",
        "P34_cffex_combined_derivative_overlay",
        "P35_cffex_dynamic_direction_overlay",
    }
    assert all(
        family.implementation_status == "implemented"
        for family in registry.derivative_grid_families
    )
    assert all(
        family.evidence_status == "not_tested"
        for family in registry.derivative_grid_families
    )
    assert all(
        family.readiness_status == "runnable"
        for family in registry.derivative_grid_families
    )
    by_family = {family.family_id: family for family in registry.families}
    assert by_family["P32_cffex_whole_lot_futures_overlay"].instrument_roots == (
        "IF",
        "IH",
        "IC",
        "IM",
    )
    assert by_family["P33_cffex_long_option_convexity_budget"].instrument_roots == (
        "IO",
        "HO",
        "MO",
    )
    assert by_family["P34_cffex_combined_derivative_overlay"].instrument_roots == (
        "IF",
        "IH",
        "IC",
        "IM",
        "IO",
        "HO",
        "MO",
    )
    assert by_family["P35_cffex_dynamic_direction_overlay"].estimated_config_budget == 36
    for family_id in [
        "S2_real_stock_momentum",
        "S3_real_stock_breakout",
        "S4_real_smallcap_factor",
    ]:
        assert by_family[family_id].sizing_modes == (
            "equal",
            "inverse_volatility",
            "atr_risk",
            "rank",
        )
        assert by_family[family_id].implementation_status == "implemented"
        assert by_family[family_id].evidence_status == "not_tested"
        assert by_family[family_id].readiness_status == "blocked"
    assert by_family["S30_real_idiosyncratic_strength"].estimated_config_budget == 24
    assert by_family["S31_real_post_limit_release"].data_tier == "free_real_derived_limits"
    assert by_family["S31_real_post_limit_release"].estimated_config_budget == 12
    assert summary["strategy_space_budget"] == 700
    assert summary["derivative_grid_budget"] == 115
    assert summary["config_backed_budget"] == 815
    assert summary["registry_only_budget"] == 0
    assert summary["total_estimated_config_budget"] == 815
    assert related["equity_stateful_trend_v1"] == {
        "S20_real_stateful_trend",
        "S22_real_concentrated_trend",
    }
    assert related["equity_volatility_contraction_v1"] == {
        "S21_real_volatility_contraction",
        "S23_real_concentrated_contraction",
    }

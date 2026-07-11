from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_proof.real_strategies import RealStockStrategySpec
from quant_proof.search_manager import file_sha256, stable_hash
from scripts.download_phase3_cffex_data import resolve_panel_output_path
from scripts.run_phase3_search import (
    _attach_relation_groups,
    _load_relation_group_mapping,
    _load_previous_candidates,
    _required_signal_history_days,
    _resolve_multiple_testing_registry_path,
    _validate_run_frame,
)


def _run_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "strategy": "s",
                "family": "A",
                "data_tier": "free_real",
                "deposit_timing": timing,
                "start": start,
                "end": end,
                "w12": 500_000.0,
                "w24": 1_200_000.0,
                "total_deposit": 720_000.0,
                "max_drawdown": 0.2,
            }
            for timing in ["beginning", "ending"]
            for start, end in [
                ("2020-01-01", "2021-12-31"),
                ("2022-01-01", "2023-12-31"),
            ]
        ]
    )


def test_run_frame_requires_exact_timing_window_cartesian_product() -> None:
    frame = _run_frame()
    expected = frame[["deposit_timing", "start", "end"]].copy()
    spec = RealStockStrategySpec(name="s", family="A", params={"data_tier": "free_real"})

    _validate_run_frame(frame, spec, expected)
    with pytest.raises(ValueError, match="Cartesian product"):
        _validate_run_frame(frame.iloc[:-1], spec, expected)
    with pytest.raises(ValueError, match="duplicate"):
        _validate_run_frame(pd.concat([frame, frame.iloc[[0]]], ignore_index=True), spec, expected)


def test_candidate_lineage_manifest_rejects_mutable_csv(tmp_path: Path) -> None:
    stage_dir = tmp_path / "screen"
    stage_dir.mkdir()
    candidates_path = stage_dir / "candidates.csv"
    pd.DataFrame(
        [{"strategy": "s", "family": "A", "parameters_json": "{}", "promotion_reason": "family"}]
    ).to_csv(candidates_path, index=False)
    manifest = {
        "schema_version": 1,
        "candidate_count": 1,
        "candidates_sha256": file_sha256(candidates_path),
    }
    manifest["signature"] = stable_hash(manifest, length=24)
    (stage_dir / "candidates.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    frame, signature = _load_previous_candidates(tmp_path, "screen")
    assert len(frame) == 1
    assert signature == manifest["signature"]

    candidates_path.write_text("strategy,family\nchanged,A\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        _load_previous_candidates(tmp_path, "screen")


def test_partial_cffex_build_cannot_replace_canonical_panel(tmp_path: Path) -> None:
    canonical = tmp_path / "cffex_contract_daily.parquet"

    assert resolve_panel_output_path(canonical, "20100416", "20260630", 195, True) == canonical
    partial = resolve_panel_output_path(canonical, "20240101", "20240131", 1, False)
    assert partial != canonical
    assert partial.name == "cffex_contract_daily_20240101_20240131_1m.parquet"


def test_signal_history_requirement_includes_lookback_plus_skip() -> None:
    config = {
        "strategy_spaces": {
            "A": {
                "parameters": {
                    "lookback": [60, 120, 240],
                    "skip_days": [5, 20],
                    "max_holding_days": [400],
                }
            }
        }
    }

    assert _required_signal_history_days(config) == 260


def test_multiple_testing_registry_defaults_to_shared_data_meta_path(tmp_path: Path) -> None:
    data_root = tmp_path / "external-data"
    output_root = tmp_path / "outputs"

    resolved = _resolve_multiple_testing_registry_path(
        {"output_root": str(output_root)},
        data_root,
    )

    assert resolved == (data_root / "00_meta/phase3_multiple_testing_registry.csv").resolve()
    assert output_root.resolve() not in resolved.parents


def test_multiple_testing_registry_rejects_per_output_location(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"

    with pytest.raises(ValueError, match="must not live under"):
        _resolve_multiple_testing_registry_path(
            {
                "output_root": str(output_root),
                "multiple_testing_registry_path": str(output_root / "one-study/formal.csv"),
            },
            tmp_path / "external-data",
        )


def test_runner_loads_relation_groups_and_attaches_them_fail_closed() -> None:
    mapping = _load_relation_group_mapping(ROOT / "config/phase3_search_registry.yaml")
    leaderboard = pd.DataFrame(
        [
            {"strategy": "s20", "family": "S20_real_stateful_trend"},
            {"strategy": "s22", "family": "S22_real_concentrated_trend"},
        ]
    )

    attached = _attach_relation_groups(leaderboard, mapping)

    assert attached["relation_group"].nunique() == 1
    assert attached["relation_group"].iloc[0] == "equity_stateful_trend_v1"
    with pytest.raises(ValueError, match="missing Phase 3 relation_group"):
        _attach_relation_groups(
            pd.DataFrame([{"strategy": "unknown", "family": "UNKNOWN"}]),
            mapping,
        )

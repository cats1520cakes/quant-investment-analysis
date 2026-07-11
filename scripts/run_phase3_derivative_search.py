from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_real_backtest import load_backtest_config
from quant_proof.free_sources.baostock_adapter import load_config
from quant_proof.phase3_derivative_search import (
    aggregate_overlay_windows,
    evaluate_overlay_spec,
    overlay_lineage_key,
    select_inherited_overlay_specs,
    select_overlay_candidates,
)
from quant_proof.phase3_overlay_factory import (
    OverlaySearchSpec,
    OverlayStageBudget,
    load_overlay_search_space,
    load_phase3_overlay_resources,
)
from quant_proof.search_manager import file_sha256, stable_hash, utc_now


DERIVATIVE_EXECUTION_SOURCE_PATHS = (
    "scripts/run_phase3_derivative_search.py",
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
    "src/quant_proof/simulator.py",
)
PUBLISHED_STAGE_FILES = (
    "windows.csv",
    "leaderboard.csv",
    "candidates.csv",
    "candidates.manifest.json",
)


def _atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        frame.to_csv(temp, index=False, encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _execution_source_hashes(repo_root: Path = ROOT) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in DERIVATIVE_EXECUTION_SOURCE_PATHS:
        path = repo_root / relative
        if not path.is_file():
            raise FileNotFoundError(f"missing derivative execution source: {path}")
        hashes[relative] = file_sha256(path)
    return hashes


def _sign_manifest(payload: Mapping[str, object]) -> dict[str, object]:
    signed = dict(payload)
    signed["manifest_signature"] = stable_hash(payload, length=24)
    return signed


def _validate_manifest_signature(
    manifest: Mapping[str, object],
    *,
    context: str,
) -> None:
    signature = str(manifest.get("manifest_signature", ""))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_signature"}
    if not signature or signature != stable_hash(unsigned, length=24):
        raise ValueError(f"{context} manifest signature mismatch")


def _build_stage_manifest(
    *,
    search_id: str,
    search_config_sha256: str,
    stage: str,
    budget: OverlayStageBudget,
    artifact_hashes: Mapping[str, str],
    execution_source_hashes: Mapping[str, str],
    backtest: Mapping[str, object],
    configured_specs: Sequence[OverlaySearchSpec],
    expected_specs: Sequence[OverlaySearchSpec],
    lineage: Mapping[str, Sequence[str]],
    parent_stage: str = "root",
    parent_stage_signature: str = "root",
    parent_candidate_manifest_signature: str = "root",
) -> dict[str, object]:
    configured_ids = [spec.overlay_id for spec in configured_specs]
    expected_ids = [spec.overlay_id for spec in expected_specs]
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError(f"derivative stage contains duplicate overlay IDs: {stage}")
    if not set(expected_ids).issubset(configured_ids):
        raise ValueError(f"derivative stage expected specs escape the registered grid: {stage}")
    if set(lineage) - set(expected_ids):
        raise ValueError(f"derivative stage lineage contains unknown children: {stage}")
    lineage_records = [
        {
            "overlay_id": overlay_id,
            "parent_overlay_ids": list(map(str, lineage.get(overlay_id, ()))),
        }
        for overlay_id in expected_ids
    ]
    has_parent = str(parent_stage) != "root"
    if has_parent and (
        parent_candidate_manifest_signature == "root"
        or any(not record["parent_overlay_ids"] for record in lineage_records)
    ):
        raise ValueError(f"derivative child stage has incomplete parent lineage: {stage}")
    if not has_parent and (
        parent_candidate_manifest_signature != "root"
        or any(record["parent_overlay_ids"] for record in lineage_records)
    ):
        raise ValueError(f"derivative root stage unexpectedly has parent lineage: {stage}")
    inherited_parent_ids = sorted(
        {
            parent_id
            for record in lineage_records
            for parent_id in record["parent_overlay_ids"]
        }
    )
    identity = {
        "search_config_sha256": str(search_config_sha256),
        "stage": str(stage),
        "budget": dict(budget.__dict__),
        "artifact_sha256": dict(sorted(artifact_hashes.items())),
        "execution_source_sha256": dict(sorted(execution_source_hashes.items())),
        "backtest": dict(backtest),
        "configured_overlay_ids": configured_ids,
        "expected_overlay_ids": expected_ids,
        "parent_stage": str(parent_stage),
        "parent_stage_signature": str(parent_stage_signature),
        "parent_candidate_manifest_signature": str(
            parent_candidate_manifest_signature
        ),
        "inherited_parent_overlay_ids": inherited_parent_ids,
        "lineage": lineage_records,
    }
    stage_signature = stable_hash(identity, length=24)
    return _sign_manifest(
        {
            "schema_version": 2,
            "search_id": str(search_id),
            **identity,
            "stage_signature": stage_signature,
            "configured_specs": len(configured_ids),
            "expected_specs": len(expected_ids),
            "expected_deposit_timings": ["beginning", "ending"],
            "execution_tier": "daily_settlement_no_quotes",
            "formal_evidence_available": False,
        }
    )


def _validate_stage_manifest(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> None:
    _validate_manifest_signature(current, context="derivative stage")
    if dict(current) == dict(expected):
        return
    changed = sorted(
        key
        for key in set(current) | set(expected)
        if current.get(key) != expected.get(key)
    )
    raise ValueError(
        "derivative stage manifest drifted; use a new search_id or invalidate the "
        f"audited stage outputs (changed={changed})"
    )


def _validate_execution_source_snapshot(
    stage_manifest: Mapping[str, object],
) -> None:
    expected = stage_manifest.get("execution_source_sha256", {})
    if not isinstance(expected, dict) or _execution_source_hashes() != expected:
        raise ValueError(
            "derivative execution sources changed during the stage; discard the "
            "in-flight result and restart under a new stage signature"
        )


def _ensure_stage_manifest(path: Path, expected: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                raise ValueError("derivative stage manifest must be a JSON object")
            _validate_stage_manifest(current, expected)
        else:
            _atomic_write_json(path, dict(expected))
        fcntl.flock(lock, fcntl.LOCK_UN)


def _validate_run(
    frame: pd.DataFrame,
    spec: OverlaySearchSpec,
    stage_manifest: Mapping[str, object],
    *,
    parent_overlay_ids: Sequence[str],
) -> None:
    stage_signature = str(stage_manifest["stage_signature"])
    stage_manifest_signature = str(stage_manifest["manifest_signature"])
    parent_candidate_manifest_signature = str(
        stage_manifest["parent_candidate_manifest_signature"]
    )
    required = {
        "strategy",
        "overlay_id",
        "family",
        "deposit_timing",
        "start",
        "end",
        "w12",
        "w24",
        "max_drawdown",
        "margin_call_days",
        "default_events",
        "futures_expiry_settlements",
        "option_expiry_settlements",
        "stage",
        "stage_signature",
        "stage_manifest_signature",
        "lineage_key",
        "parent_stage",
        "parent_overlay_ids_json",
        "parent_candidate_manifest_signature",
        "overlay_spec_json",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"derivative run missing columns: {missing}")
    if frame.empty or set(frame["overlay_id"].astype(str)) != {spec.overlay_id}:
        raise ValueError(f"derivative run identity mismatch: {spec.overlay_id}")
    if set(frame["strategy"].astype(str)) != {spec.overlay_id}:
        raise ValueError(f"derivative run strategy mismatch: {spec.overlay_id}")
    if set(frame["stage"].astype(str)) != {spec.stage}:
        raise ValueError(f"derivative run stage mismatch: {spec.overlay_id}")
    if set(frame["stage_signature"].astype(str)) != {stage_signature}:
        raise ValueError(f"derivative run stage signature mismatch: {spec.overlay_id}")
    if set(frame["stage_manifest_signature"].astype(str)) != {
        stage_manifest_signature
    }:
        raise ValueError(
            f"derivative run stage manifest signature mismatch: {spec.overlay_id}"
        )
    if set(frame["lineage_key"].astype(str)) != {overlay_lineage_key(spec)}:
        raise ValueError(f"derivative run lineage key mismatch: {spec.overlay_id}")
    if set(frame["parent_stage"].astype(str)) != {
        str(stage_manifest["parent_stage"])
    }:
        raise ValueError(f"derivative run parent stage mismatch: {spec.overlay_id}")
    expected_parent_json = _canonical_json(list(map(str, parent_overlay_ids)))
    if set(frame["parent_overlay_ids_json"].astype(str)) != {
        expected_parent_json
    }:
        raise ValueError(f"derivative run parent lineage mismatch: {spec.overlay_id}")
    if set(frame["parent_candidate_manifest_signature"].astype(str)) != {
        parent_candidate_manifest_signature
    }:
        raise ValueError(
            f"derivative run parent manifest signature mismatch: {spec.overlay_id}"
        )
    expected_spec_json = _canonical_json(spec.to_audit_dict())
    if set(frame["overlay_spec_json"].astype(str)) != {expected_spec_json}:
        raise ValueError(f"derivative run spec audit mismatch: {spec.overlay_id}")
    if frame.duplicated(["deposit_timing", "start", "end"]).any():
        raise ValueError(f"derivative run has duplicate windows: {spec.overlay_id}")
    if set(frame["deposit_timing"].astype(str)) != {"beginning", "ending"}:
        raise ValueError(f"derivative run lacks both deposit timings: {spec.overlay_id}")
    observed_windows = {
        timing: set(zip(group["start"].astype(str), group["end"].astype(str)))
        for timing, group in frame.groupby("deposit_timing", sort=True)
    }
    if observed_windows.get("beginning") != observed_windows.get("ending"):
        raise ValueError(
            f"derivative run deposit timings cover different windows: {spec.overlay_id}"
        )


def _run_manifest_path(run_path: Path) -> Path:
    return run_path.with_suffix(".manifest.json")


def _annotate_run(
    frame: pd.DataFrame,
    spec: OverlaySearchSpec,
    stage_manifest: Mapping[str, object],
    *,
    parent_overlay_ids: Sequence[str],
) -> pd.DataFrame:
    out = frame.copy()
    out["stage_signature"] = str(stage_manifest["stage_signature"])
    out["stage_manifest_signature"] = str(stage_manifest["manifest_signature"])
    out["lineage_key"] = overlay_lineage_key(spec)
    out["parent_stage"] = str(stage_manifest["parent_stage"])
    out["parent_overlay_ids_json"] = _canonical_json(
        list(map(str, parent_overlay_ids))
    )
    out["parent_candidate_manifest_signature"] = str(
        stage_manifest["parent_candidate_manifest_signature"]
    )
    out["overlay_spec_json"] = _canonical_json(spec.to_audit_dict())
    return out


def _build_run_manifest(
    run_path: Path,
    frame: pd.DataFrame,
    spec: OverlaySearchSpec,
    stage_manifest: Mapping[str, object],
    *,
    parent_overlay_ids: Sequence[str],
) -> dict[str, object]:
    return _sign_manifest(
        {
            "schema_version": 1,
            "search_id": str(stage_manifest["search_id"]),
            "stage": spec.stage,
            "overlay_id": spec.overlay_id,
            "stage_signature": str(stage_manifest["stage_signature"]),
            "stage_manifest_signature": str(stage_manifest["manifest_signature"]),
            "parent_candidate_manifest_signature": str(
                stage_manifest["parent_candidate_manifest_signature"]
            ),
            "parent_overlay_ids": list(map(str, parent_overlay_ids)),
            "lineage_key": overlay_lineage_key(spec),
            "rows": int(len(frame)),
            "csv_sha256": file_sha256(run_path),
        }
    )


def _write_run_artifacts(
    run_path: Path,
    frame: pd.DataFrame,
    spec: OverlaySearchSpec,
    stage_manifest: Mapping[str, object],
    *,
    parent_overlay_ids: Sequence[str],
) -> None:
    _atomic_write_csv(run_path, frame)
    manifest = _build_run_manifest(
        run_path,
        frame,
        spec,
        stage_manifest,
        parent_overlay_ids=parent_overlay_ids,
    )
    _atomic_write_json(_run_manifest_path(run_path), manifest)


def _load_cached_run(
    run_path: Path,
    spec: OverlaySearchSpec,
    stage_manifest: Mapping[str, object],
    *,
    parent_overlay_ids: Sequence[str],
) -> pd.DataFrame:
    manifest_path = _run_manifest_path(run_path)
    if not run_path.is_file() or not manifest_path.is_file():
        raise ValueError(f"derivative run cache is incomplete: {spec.overlay_id}")
    frame = pd.read_csv(run_path, keep_default_na=False)
    _validate_run(
        frame,
        spec,
        stage_manifest,
        parent_overlay_ids=parent_overlay_ids,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"derivative run manifest must be an object: {spec.overlay_id}")
    _validate_manifest_signature(manifest, context="derivative run")
    expected = _build_run_manifest(
        run_path,
        frame,
        spec,
        stage_manifest,
        parent_overlay_ids=parent_overlay_ids,
    )
    if manifest != expected:
        raise ValueError(f"derivative run cache manifest drifted: {spec.overlay_id}")
    return frame


def _collect_completed_runs(
    runs_root: Path,
    specs: Sequence[OverlaySearchSpec],
    stage_manifest: Mapping[str, object],
    lineage: Mapping[str, Sequence[str]],
) -> tuple[list[pd.DataFrame], list[dict[str, object]]]:
    frames: list[pd.DataFrame] = []
    inventory: list[dict[str, object]] = []
    for spec in specs:
        run_path = runs_root / f"{spec.overlay_id}.csv"
        parent_ids = tuple(map(str, lineage.get(spec.overlay_id, ())))
        try:
            frame = _load_cached_run(
                run_path,
                spec,
                stage_manifest,
                parent_overlay_ids=parent_ids,
            )
        except (OSError, ValueError, TypeError, pd.errors.ParserError):
            continue
        run_manifest = json.loads(
            _run_manifest_path(run_path).read_text(encoding="utf-8")
        )
        frames.append(frame)
        inventory.append(
            {
                "overlay_id": spec.overlay_id,
                "rows": int(len(frame)),
                "csv_sha256": file_sha256(run_path),
                "run_manifest_signature": str(run_manifest["manifest_signature"]),
            }
        )
    return frames, inventory


def _withhold_stage_publication(stage_root: Path) -> list[str]:
    withheld: list[str] = []
    for name in PUBLISHED_STAGE_FILES:
        path = stage_root / name
        if path.exists():
            path.unlink()
            withheld.append(name)
    return withheld


def _write_report(
    path: Path,
    leaderboard: pd.DataFrame,
    candidates: pd.DataFrame,
    *,
    search_id: str,
    stage: str,
    stage_signature: str,
) -> None:
    best = leaderboard.iloc[0] if not leaderboard.empty else None
    lines = [
        "# Phase 3 Derivative Overlay Search",
        "",
        f"Updated: {utc_now()}",
        "",
        "## Status",
        "",
        "This is development-only official-daily evidence. Bid/ask quotes are absent, so no row can be a formal candidate.",
        "",
        f"- Search: `{search_id}`",
        f"- Stage: `{stage}`",
        f"- Stage signature: `{stage_signature}`",
        f"- Leaderboard rows: {len(leaderboard)}",
        f"- Promoted exploratory centers: {len(candidates)}",
    ]
    if best is not None:
        lines.extend(
            [
                "",
                "## Best Timing Row",
                "",
                f"- Overlay: `{best['strategy']}`",
                f"- Deposit timing: `{best['deposit_timing']}`",
                f"- Joint success share: {float(best['p_success']):.4f}",
                f"- Non-overlap median W12: CNY {float(best['nonoverlap_median_w12']):,.0f}",
                f"- Non-overlap median W24: CNY {float(best['nonoverlap_median_w24']):,.0f}",
                f"- Non-overlap minimum W24: CNY {float(best['nonoverlap_min_w24']):,.0f}",
                f"- Non-overlap maximum drawdown: {float(best['nonoverlap_max_drawdown']):.2%}",
                f"- Margin-call window share: {float(best['margin_call_window_share']):.2%}",
                f"- Default window share: {float(best['default_window_share']):.2%}",
                f"- Development gate: {bool(best['passes_overlay_development_gates'])}",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _attach_stage_audit_columns(
    frame: pd.DataFrame,
    specs: Sequence[OverlaySearchSpec],
    stage_manifest: Mapping[str, object],
    lineage: Mapping[str, Sequence[str]],
) -> pd.DataFrame:
    out = frame.copy()
    spec_by_id = {spec.overlay_id: spec for spec in specs}
    if "strategy" not in out.columns:
        out["strategy"] = pd.Series(dtype="object")
    out["overlay_spec_json"] = out["strategy"].map(
        lambda value: _canonical_json(spec_by_id[str(value)].to_audit_dict())
    )
    out["stage"] = str(stage_manifest["stage"])
    out["stage_signature"] = str(stage_manifest["stage_signature"])
    out["stage_manifest_signature"] = str(stage_manifest["manifest_signature"])
    out["lineage_key"] = out["strategy"].map(
        lambda value: overlay_lineage_key(spec_by_id[str(value)])
    )
    out["parent_stage"] = str(stage_manifest["parent_stage"])
    out["parent_overlay_ids_json"] = out["strategy"].map(
        lambda value: _canonical_json(list(map(str, lineage.get(str(value), ()))))
    )
    out["parent_candidate_manifest_signature"] = str(
        stage_manifest["parent_candidate_manifest_signature"]
    )
    return out


def _validate_candidate_frame(
    candidates: pd.DataFrame,
    stage_manifest: Mapping[str, object],
    *,
    max_promotions: int,
) -> None:
    if len(candidates) > max_promotions:
        raise ValueError("derivative candidate promotion cap exceeded")
    if candidates.empty:
        return
    required = {
        "strategy",
        "passes_overlay_development_gates",
        "passes_overlay_activity_gate",
        "overall_derivative_active_window_share",
        "worst_timing_active_window_share",
        "total_derivative_requested_contracts",
        "total_derivative_filled_contracts",
        "total_futures_expiry_settlements",
        "total_option_expiry_settlements",
        "promotion_reason",
        "stage_signature",
        "stage_manifest_signature",
        "lineage_key",
        "parent_stage",
        "parent_overlay_ids_json",
        "parent_candidate_manifest_signature",
    }
    missing = sorted(required - set(candidates.columns))
    if missing:
        raise ValueError(f"derivative candidates missing columns: {missing}")
    if candidates["strategy"].astype(str).duplicated().any():
        raise ValueError("derivative candidates contain duplicate overlay IDs")
    expected_ids = set(map(str, stage_manifest["expected_overlay_ids"]))
    observed_ids = set(candidates["strategy"].astype(str))
    if not observed_ids.issubset(expected_ids):
        raise ValueError("derivative candidates escape the registered stage")
    activity = candidates["passes_overlay_activity_gate"].astype(str).str.lower()
    requested = pd.to_numeric(
        candidates["total_derivative_requested_contracts"], errors="coerce"
    )
    filled = pd.to_numeric(
        candidates["total_derivative_filled_contracts"], errors="coerce"
    )
    active_share = pd.to_numeric(
        candidates["overall_derivative_active_window_share"], errors="coerce"
    )
    if (
        not activity.eq("true").all()
        or not requested.gt(0.0).all()
        or not filled.gt(0.0).all()
        or not active_share.gt(0.0).all()
    ):
        raise ValueError("inactive derivative spec cannot enter candidates")
    reasons = set(candidates["promotion_reason"].astype(str))
    if not reasons.issubset({"development_gate", "bounded_exploratory_rank"}):
        raise ValueError("derivative candidates contain an unknown promotion reason")
    development_pass = (
        candidates["passes_overlay_development_gates"].astype(str).str.lower().eq("true")
    )
    expected_reasons = development_pass.map(
        {True: "development_gate", False: "bounded_exploratory_rank"}
    )
    if not candidates["promotion_reason"].astype(str).eq(expected_reasons).all():
        raise ValueError("derivative candidate promotion reason conflicts with its gate")
    if set(candidates["stage_signature"].astype(str)) != {
        str(stage_manifest["stage_signature"])
    }:
        raise ValueError("derivative candidate stage signature mismatch")
    if set(candidates["stage_manifest_signature"].astype(str)) != {
        str(stage_manifest["manifest_signature"])
    }:
        raise ValueError("derivative candidate stage manifest signature mismatch")
    if set(candidates["parent_candidate_manifest_signature"].astype(str)) != {
        str(stage_manifest["parent_candidate_manifest_signature"])
    }:
        raise ValueError("derivative candidate parent manifest signature mismatch")
    if set(candidates["parent_stage"].astype(str)) != {
        str(stage_manifest["parent_stage"])
    }:
        raise ValueError("derivative candidate parent stage mismatch")
    if candidates["lineage_key"].astype(str).str.strip().eq("").any():
        raise ValueError("derivative candidate lineage key is empty")
    lineage_by_id = {
        str(record["overlay_id"]): tuple(map(str, record["parent_overlay_ids"]))
        for record in stage_manifest["lineage"]
    }
    for row in candidates.itertuples(index=False):
        overlay_id = str(row.strategy)
        if str(row.parent_overlay_ids_json) != _canonical_json(
            list(lineage_by_id[overlay_id])
        ):
            raise ValueError("derivative candidate parent lineage mismatch")


def _build_candidate_manifest(
    candidate_path: Path,
    candidates: pd.DataFrame,
    stage_manifest: Mapping[str, object],
    *,
    max_promotions: int,
) -> dict[str, object]:
    _validate_candidate_frame(
        candidates,
        stage_manifest,
        max_promotions=max_promotions,
    )
    records = []
    for row in candidates.itertuples(index=False):
        records.append(
            {
                "overlay_id": str(row.strategy),
                "promotion_reason": str(row.promotion_reason),
                "passes_overlay_activity_gate": bool(
                    str(row.passes_overlay_activity_gate).lower() == "true"
                ),
                "worst_timing_active_window_share": float(
                    row.worst_timing_active_window_share
                ),
                "overall_derivative_active_window_share": float(
                    row.overall_derivative_active_window_share
                ),
                "total_derivative_requested_contracts": float(
                    row.total_derivative_requested_contracts
                ),
                "total_derivative_filled_contracts": float(
                    row.total_derivative_filled_contracts
                ),
                "total_futures_expiry_settlements": float(
                    row.total_futures_expiry_settlements
                ),
                "total_option_expiry_settlements": float(
                    row.total_option_expiry_settlements
                ),
                "parent_overlay_ids": json.loads(str(row.parent_overlay_ids_json)),
            }
        )
    return _sign_manifest(
        {
            "schema_version": 1,
            "search_id": str(stage_manifest["search_id"]),
            "stage": str(stage_manifest["stage"]),
            "stage_signature": str(stage_manifest["stage_signature"]),
            "stage_manifest_signature": str(stage_manifest["manifest_signature"]),
            "parent_stage": str(stage_manifest["parent_stage"]),
            "parent_candidate_manifest_signature": str(
                stage_manifest["parent_candidate_manifest_signature"]
            ),
            "promotion_limit": int(max_promotions),
            "candidate_count": int(len(candidates)),
            "candidate_overlay_ids": candidates.get(
                "strategy", pd.Series(dtype="object")
            )
            .astype(str)
            .tolist(),
            "candidates_sha256": file_sha256(candidate_path),
            "candidates": records,
        }
    )


def _load_candidates(
    stage_root: Path,
    stage_manifest: Mapping[str, object],
    *,
    max_promotions: int,
) -> tuple[pd.DataFrame, str]:
    candidate_path = stage_root / "candidates.csv"
    manifest_path = stage_root / "candidates.manifest.json"
    if not candidate_path.is_file() or not manifest_path.is_file():
        raise ValueError(f"candidate lineage is incomplete for stage {stage_manifest['stage']}")
    candidates = pd.read_csv(candidate_path, keep_default_na=False)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("derivative candidate manifest must be a JSON object")
    _validate_manifest_signature(manifest, context="derivative candidate")
    expected = _build_candidate_manifest(
        candidate_path,
        candidates,
        stage_manifest,
        max_promotions=max_promotions,
    )
    if manifest != expected:
        raise ValueError(
            f"derivative candidate manifest drifted: {stage_manifest['stage']}"
        )
    return candidates, str(manifest["manifest_signature"])


def _write_progress(path: Path, payload: Mapping[str, object]) -> dict[str, object]:
    manifest = _sign_manifest({"schema_version": 1, **dict(payload)})
    _atomic_write_json(path, manifest)
    return manifest


def _load_completed_parent(
    parent_root: Path,
    expected_stage_manifest: Mapping[str, object],
    *,
    max_promotions: int,
) -> tuple[pd.DataFrame, str]:
    stage_manifest_path = parent_root / "stage_manifest.json"
    progress_path = parent_root / "progress.json"
    if not stage_manifest_path.is_file() or not progress_path.is_file():
        raise ValueError(
            f"parent stage {expected_stage_manifest['stage']} is not complete"
        )
    current_stage_manifest = json.loads(
        stage_manifest_path.read_text(encoding="utf-8")
    )
    if not isinstance(current_stage_manifest, dict):
        raise ValueError("parent derivative stage manifest must be a JSON object")
    _validate_stage_manifest(current_stage_manifest, expected_stage_manifest)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    if not isinstance(progress, dict):
        raise ValueError("parent derivative progress must be a JSON object")
    _validate_manifest_signature(progress, context="derivative progress")
    if (
        progress.get("complete") is not True
        or progress.get("leaderboard_withheld") is not False
        or str(progress.get("stage_signature", ""))
        != str(expected_stage_manifest["stage_signature"])
        or str(progress.get("stage_manifest_signature", ""))
        != str(expected_stage_manifest["manifest_signature"])
        or int(progress.get("completed_specs", -1))
        != int(expected_stage_manifest["expected_specs"])
    ):
        raise ValueError(
            f"parent stage {expected_stage_manifest['stage']} is not completely published"
        )
    for name in PUBLISHED_STAGE_FILES:
        if not (parent_root / name).is_file():
            raise ValueError(
                f"parent stage publication is incomplete: {expected_stage_manifest['stage']}"
            )
    for name, progress_key in (
        ("windows.csv", "windows_sha256"),
        ("leaderboard.csv", "leaderboard_sha256"),
        ("candidates.csv", "candidates_sha256"),
    ):
        if str(progress.get(progress_key, "")) != file_sha256(parent_root / name):
            raise ValueError(
                f"parent stage publication hash mismatch: {expected_stage_manifest['stage']}"
            )
    candidates, manifest_signature = _load_candidates(
        parent_root,
        expected_stage_manifest,
        max_promotions=max_promotions,
    )
    if str(progress.get("candidate_manifest_signature", "")) != manifest_signature:
        raise ValueError("parent progress and candidate manifest signatures disagree")
    return candidates, manifest_signature


def _run_stage(
    *,
    search_id: str,
    stage: str,
    configured_specs: Sequence[OverlaySearchSpec],
    specs: Sequence[OverlaySearchSpec],
    resources,
    backtest_config,
    budget: OverlayStageBudget,
    output_root: Path,
    stage_manifest: Mapping[str, object],
    lineage: Mapping[str, Sequence[str]],
    max_specs: int,
    resume: bool,
) -> bool:
    stage_root = output_root / search_id / stage
    runs_root = stage_root / "runs"
    _ensure_stage_manifest(stage_root / "stage_manifest.json", stage_manifest)
    _validate_execution_source_snapshot(stage_manifest)

    incomplete: list[OverlaySearchSpec] = []
    cached_overlay_ids: list[str] = []
    for spec in specs:
        run_path = runs_root / f"{spec.overlay_id}.csv"
        parent_ids = tuple(map(str, lineage.get(spec.overlay_id, ())))
        if resume:
            try:
                _load_cached_run(
                    run_path,
                    spec,
                    stage_manifest,
                    parent_overlay_ids=parent_ids,
                )
            except (OSError, ValueError, TypeError, pd.errors.ParserError):
                pass
            else:
                cached_overlay_ids.append(spec.overlay_id)
                continue
        incomplete.append(spec)
    selected = incomplete[: max_specs or None]
    print(
        f"[derivative:{stage}] configured={len(configured_specs)} "
        f"expected={len(specs)} cached={len(cached_overlay_ids)} "
        f"remaining={len(incomplete)} scheduled={len(selected)}",
        flush=True,
    )
    for index, spec in enumerate(selected, start=1):
        print(
            f"[derivative:{stage}] run={index}/{len(selected)} "
            f"overlay={spec.overlay_id} composition={spec.composition}",
            flush=True,
        )
        parent_ids = tuple(map(str, lineage.get(spec.overlay_id, ())))
        frame = evaluate_overlay_spec(
            resources,
            spec,
            backtest_config,
            budget,
        )
        frame = _annotate_run(
            frame,
            spec,
            stage_manifest,
            parent_overlay_ids=parent_ids,
        )
        _validate_run(
            frame,
            spec,
            stage_manifest,
            parent_overlay_ids=parent_ids,
        )
        _write_run_artifacts(
            runs_root / f"{spec.overlay_id}.csv",
            frame,
            spec,
            stage_manifest,
            parent_overlay_ids=parent_ids,
        )

    finalize_lock_path = stage_root / ".finalize.lock"
    finalize_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with finalize_lock_path.open("w", encoding="utf-8") as finalize_lock:
        fcntl.flock(finalize_lock, fcntl.LOCK_EX)
        _validate_execution_source_snapshot(stage_manifest)
        frames, run_inventory = _collect_completed_runs(
            runs_root,
            specs,
            stage_manifest,
            lineage,
        )

        completed = len(frames)
        progress_base = {
            "search_id": search_id,
            "stage": stage,
            "stage_signature": str(stage_manifest["stage_signature"]),
            "stage_manifest_signature": str(stage_manifest["manifest_signature"]),
            "parent_stage": str(stage_manifest["parent_stage"]),
            "parent_candidate_manifest_signature": str(
                stage_manifest["parent_candidate_manifest_signature"]
            ),
            "configured_specs": len(configured_specs),
            "expected_specs": len(specs),
            "completed_specs": completed,
            "cached_at_start": cached_overlay_ids,
            "scheduled_this_invocation": [spec.overlay_id for spec in selected],
            "run_inventory": run_inventory,
            "updated_at": utc_now(),
        }
        if completed != len(specs):
            withheld = _withhold_stage_publication(stage_root)
            _write_progress(
                stage_root / "progress.json",
                {
                    **progress_base,
                    "complete": False,
                    "leaderboard_withheld": True,
                    "withheld_files": withheld,
                },
            )
            fcntl.flock(finalize_lock, fcntl.LOCK_UN)
            print(
                f"[derivative:{stage}] progress={completed}/{len(specs)}; "
                "leaderboard withheld until the registered stage is complete",
                flush=True,
            )
            return False

        windows = pd.concat(frames, ignore_index=True)
        leaderboard = aggregate_overlay_windows(windows, backtest_config)
        leaderboard = _attach_stage_audit_columns(
            leaderboard,
            specs,
            stage_manifest,
            lineage,
        )
        candidates = select_overlay_candidates(
            leaderboard,
            max_promotions=budget.max_promotions,
        )
        candidates = _attach_stage_audit_columns(
            candidates,
            specs,
            stage_manifest,
            lineage,
        )
        _validate_candidate_frame(
            candidates,
            stage_manifest,
            max_promotions=budget.max_promotions,
        )
        windows_path = stage_root / "windows.csv"
        leaderboard_path = stage_root / "leaderboard.csv"
        candidate_path = stage_root / "candidates.csv"
        _atomic_write_csv(windows_path, windows)
        _atomic_write_csv(leaderboard_path, leaderboard)
        _atomic_write_csv(candidate_path, candidates)
        candidate_manifest = _build_candidate_manifest(
            candidate_path,
            candidates,
            stage_manifest,
            max_promotions=budget.max_promotions,
        )
        _atomic_write_json(
            stage_root / "candidates.manifest.json",
            candidate_manifest,
        )
        _write_progress(
            stage_root / "progress.json",
            {
                **progress_base,
                "complete": True,
                "leaderboard_withheld": False,
                "withheld_files": [],
                "windows_sha256": file_sha256(windows_path),
                "leaderboard_sha256": file_sha256(leaderboard_path),
                "candidates_sha256": file_sha256(candidate_path),
                "candidate_manifest_signature": str(
                    candidate_manifest["manifest_signature"]
                ),
                "candidate_count": int(len(candidates)),
                "promotion_limit": int(budget.max_promotions),
            },
        )
        fcntl.flock(finalize_lock, fcntl.LOCK_UN)

    _write_report(
        ROOT / "reports" / "phase3_derivative_search_status.md",
        leaderboard,
        candidates,
        search_id=search_id,
        stage=stage,
        stage_signature=str(stage_manifest["stage_signature"]),
    )
    print(
        f"[derivative:{stage}] complete specs={len(specs)} "
        f"window_rows={len(windows)} candidates={len(candidates)} "
        f"promotion_limit={budget.max_promotions}",
        flush=True,
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the official CFFEX overlay-only Phase 3 Screen or inherited "
            "Neighborhood stage."
        )
    )
    parser.add_argument(
        "--config",
        default="config/phase3_derivative_overlay_grid.yaml",
    )
    parser.add_argument(
        "--base-config",
        default="config/phase2_free_real_data.yaml",
    )
    parser.add_argument(
        "--stage",
        choices=["screen", "neighborhood"],
        default="screen",
    )
    parser.add_argument("--output-root", default="")
    parser.add_argument(
        "--max-specs",
        type=int,
        default=0,
        help="Maximum incomplete specs this invocation; 0 runs every remaining spec.",
    )
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    if args.max_specs < 0:
        raise SystemExit("--max-specs must be non-negative")

    search_space = load_overlay_search_space(args.config)
    source_config = load_config(args.base_config)
    backtest_config = load_backtest_config(source_config.raw)
    data_root = source_config.data_root
    derivative_root = data_root / "processed" / "phase3_derivatives"
    artifacts = {
        "daily": derivative_root / "cffex_contract_daily.parquet",
        "master": derivative_root / "cffex_contract_master.parquet",
        "trade_parameters": derivative_root / "cffex_contract_trade_parameters.parquet",
        "settlement_parameters": derivative_root / "cffex_settlement_params.parquet",
    }
    for name, path in artifacts.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing CFFEX {name} artifact: {path}")

    resources = load_phase3_overlay_resources(
        artifacts["daily"],
        artifacts["master"],
        artifacts["trade_parameters"],
        artifacts["settlement_parameters"],
    )
    output_root = (
        Path(args.output_root).expanduser()
        if args.output_root
        else data_root / "reports" / "phase3_derivative_search"
    )
    artifact_hashes = {name: file_sha256(path) for name, path in artifacts.items()}
    execution_source_hashes = _execution_source_hashes()

    screen_budget = search_space.budget_for_stage("screen")
    screen_specs = search_space.specs_for_stage("screen")
    screen_lineage = {spec.overlay_id: () for spec in screen_specs}
    screen_manifest = _build_stage_manifest(
        search_id=search_space.search_id,
        search_config_sha256=search_space.config_sha256,
        stage="screen",
        budget=screen_budget,
        artifact_hashes=artifact_hashes,
        execution_source_hashes=execution_source_hashes,
        backtest=vars(backtest_config),
        configured_specs=screen_specs,
        expected_specs=screen_specs,
        lineage=screen_lineage,
    )

    if args.stage == "screen":
        _run_stage(
            search_id=search_space.search_id,
            stage="screen",
            configured_specs=screen_specs,
            specs=screen_specs,
            resources=resources,
            backtest_config=backtest_config,
            budget=screen_budget,
            output_root=output_root,
            stage_manifest=screen_manifest,
            lineage=screen_lineage,
            max_specs=args.max_specs,
            resume=not args.no_resume,
        )
        return

    screen_root = output_root / search_space.search_id / "screen"
    screen_candidates, screen_candidate_manifest_signature = _load_completed_parent(
        screen_root,
        screen_manifest,
        max_promotions=screen_budget.max_promotions,
    )
    parent_candidate_ids = screen_candidates.get(
        "strategy", pd.Series(dtype="object")
    ).astype(str)
    neighborhood_budget = search_space.budget_for_stage("neighborhood")
    configured_neighborhood_specs = search_space.specs_for_stage("neighborhood")
    neighborhood_specs, neighborhood_lineage = select_inherited_overlay_specs(
        configured_neighborhood_specs,
        screen_specs,
        parent_candidate_ids,
    )
    if not neighborhood_specs:
        raise ValueError(
            "completed Screen produced no promoted parent with a registered "
            "Neighborhood lineage"
        )
    neighborhood_manifest = _build_stage_manifest(
        search_id=search_space.search_id,
        search_config_sha256=search_space.config_sha256,
        stage="neighborhood",
        budget=neighborhood_budget,
        artifact_hashes=artifact_hashes,
        execution_source_hashes=execution_source_hashes,
        backtest=vars(backtest_config),
        configured_specs=configured_neighborhood_specs,
        expected_specs=neighborhood_specs,
        lineage=neighborhood_lineage,
        parent_stage="screen",
        parent_stage_signature=str(screen_manifest["stage_signature"]),
        parent_candidate_manifest_signature=screen_candidate_manifest_signature,
    )
    _run_stage(
        search_id=search_space.search_id,
        stage="neighborhood",
        configured_specs=configured_neighborhood_specs,
        specs=neighborhood_specs,
        resources=resources,
        backtest_config=backtest_config,
        budget=neighborhood_budget,
        output_root=output_root,
        stage_manifest=neighborhood_manifest,
        lineage=neighborhood_lineage,
        max_specs=args.max_specs,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()

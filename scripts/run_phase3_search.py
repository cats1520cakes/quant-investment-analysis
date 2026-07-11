from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import resource
import sys
import time
from typing import Iterable, Mapping

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_real_backtest import (
    _date_index,
    _prepare_daily_panel,
    aggregate_free_real_windows,
    evaluate_free_real_strategy,
    filter_rolling_windows_by_start,
    load_backtest_config,
    select_rolling_windows,
)
from quant_proof.free_sources.baostock_adapter import load_config
from quant_proof.free_sources.validators import strategy_allowed_in_tier
from quant_proof.real_strategies import (
    RealStockStrategySpec,
    load_free_real_analysis_panel,
    prepare_real_stock_features,
    require_real_stock_panel,
)
from quant_proof.realdata.free_panel_builder import validate_panel_manifest
from quant_proof.search_manager import (
    ExperimentLedger,
    MultipleTestingRegistry,
    SearchStage,
    add_neighborhood_metrics,
    add_regime_metrics,
    annotate_market_regimes,
    behavior_id,
    build_search_strategy_specs,
    experiment_id,
    expand_parameter_neighbors,
    file_sha256,
    finalize_strategy_gates,
    load_search_config,
    panel_signature_from_manifest,
    parse_search_stages,
    promote_candidates,
    repository_code_signature,
    stable_hash,
    study_id,
    utc_now,
)
from quant_proof.simulator import rolling_windows


@dataclass(frozen=True)
class StageResult:
    stage: str
    expected_experiments: int
    completed_experiments: int
    windows: pd.DataFrame
    leaderboard: pd.DataFrame
    candidates: pd.DataFrame
    complete: bool
    candidate_manifest_signature: str = ""


def _fmt_pct(value: float) -> str:
    return f"{float(value) * 100:.2f}%"


def _fmt_money(value: float) -> str:
    return f"{float(value):,.0f}"


def _fmt_pvalue(value: float) -> str:
    return f"{float(value):.4g}"


def _stage_dir(output_root: Path, stage: str) -> Path:
    return output_root / stage


def _run_path(output_root: Path, stage: str, run_id: str) -> Path:
    return output_root / "runs" / stage / f"{run_id}.csv"


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        frame.to_csv(temp, index=False, encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _resolve_multiple_testing_registry_path(
    search_config: Mapping[str, object],
    data_root: Path,
) -> Path:
    configured_values = [
        str(search_config.get(key, "")).strip()
        for key in ["multiple_testing_registry_path", "multiple_testing_registry"]
        if str(search_config.get(key, "")).strip()
    ]
    if len(set(configured_values)) > 1:
        raise ValueError("conflicting multiple-testing registry paths in search config")
    if configured_values:
        path = Path(configured_values[0]).expanduser()
        if not path.is_absolute():
            path = ROOT / path
    else:
        path = Path(data_root).expanduser() / "00_meta" / "phase3_multiple_testing_registry.csv"
    path = path.resolve()
    output_value = str(search_config.get("output_root", "")).strip()
    if output_value:
        output_root = Path(output_value).expanduser()
        if not output_root.is_absolute():
            output_root = ROOT / output_root
        output_root = output_root.resolve()
        if path == output_root or output_root in path.parents:
            raise ValueError("multiple-testing registry must not live under a search output_root")
    return path


def _load_relation_group_mapping(
    path: str | Path = ROOT / "config/phase3_search_registry.yaml",
) -> dict[str, str]:
    registry = load_search_config(path)
    if int(registry.get("version", 0)) != 1:
        raise ValueError("unsupported Phase 3 search registry version")
    families = registry.get("families", [])
    if not isinstance(families, list) or not families:
        raise ValueError("Phase 3 search registry requires family entries")
    mapping: dict[str, str] = {}
    for entry in families:
        if not isinstance(entry, dict):
            raise ValueError("Phase 3 search registry family entry must be a mapping")
        family = str(entry.get("family_id", "")).strip()
        relation_group = str(entry.get("relation_group", "")).strip()
        if not family or not relation_group:
            raise ValueError("Phase 3 search registry family mapping is incomplete")
        if family in mapping and mapping[family] != relation_group:
            raise ValueError(f"conflicting relation_group mapping for family {family}")
        mapping[family] = relation_group
    return mapping


def _attach_relation_groups(
    leaderboard: pd.DataFrame,
    relation_groups: Mapping[str, str],
) -> pd.DataFrame:
    out = leaderboard.copy()
    if out.empty:
        out["relation_group"] = pd.Series(dtype=str)
        return out
    if "family" not in out.columns:
        raise ValueError("Phase 3 leaderboard is missing family for relation-group governance")
    families = out["family"].fillna("").astype(str)
    missing = sorted(
        family
        for family in families.unique()
        if not family or not str(relation_groups.get(family, "")).strip()
    )
    if missing:
        raise ValueError(f"missing Phase 3 relation_group mapping: {missing}")
    out["relation_group"] = families.map(relation_groups)
    return out


def _peak_rss_gib() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    bytes_value = value if sys.platform == "darwin" else value * 1024.0
    return bytes_value / float(1024**3)


def _require_memory_budget(search_config: dict[str, object], context: str) -> float:
    compute = search_config.get("compute", {}) if isinstance(search_config.get("compute", {}), dict) else {}
    limit = float(compute.get("max_peak_rss_gib", 10.0))
    if limit <= 0.0:
        raise ValueError("compute.max_peak_rss_gib must be positive")
    observed = _peak_rss_gib()
    if observed >= limit:
        raise MemoryError(f"Phase 3 peak RSS {observed:.2f} GiB reached limit {limit:.2f} GiB at {context}")
    return observed


def _required_signal_history_days(search_config: dict[str, object]) -> int:
    lookback_keys = {
        "donchian",
        "exit_donchian",
        "entry_window",
        "exit_window",
        "trend_window",
        "momentum_window",
        "short_vol_window",
        "long_vol_window",
        "breakout_window",
        "market_ma",
        "breadth_ma",
        "lookback",
        "volatility_days",
        "momentum_days",
        "reversal_days",
        "trend_days",
        "short_window",
        "medium_window",
        "rank_change_window",
    }
    maximum = 20
    maximum_skip = 0
    spaces = search_config.get("strategy_spaces", {})
    if not isinstance(spaces, dict):
        return maximum
    for space in spaces.values():
        if not isinstance(space, dict):
            continue
        for section_name in ["fixed", "parameters"]:
            section = space.get(section_name, {})
            if not isinstance(section, dict):
                continue
            for key, raw_value in section.items():
                values = raw_value if isinstance(raw_value, list) else [raw_value]
                numeric = [int(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
                if not numeric:
                    continue
                if str(key) in lookback_keys:
                    maximum = max(maximum, max(numeric))
                if str(key) == "skip_days":
                    maximum_skip = max(maximum_skip, max(numeric))
    return maximum + maximum_skip


def _expected_stage_windows(panel: pd.DataFrame, cfg, stage: SearchStage) -> pd.DataFrame:
    selected = rolling_windows(
        _date_index(panel),
        window_months=cfg.window_months,
        min_trading_days=cfg.min_trading_days,
    )
    selected = filter_rolling_windows_by_start(
        selected,
        start_min=stage.window_start_min,
        start_max=stage.window_start_max,
    )
    selected = select_rolling_windows(selected, max_windows=stage.max_windows, sampling=stage.window_sampling)
    rows = [
        {
            "deposit_timing": timing,
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
        }
        for timing in stage.deposit_timings
        for start, end in selected
    ]
    if not rows:
        raise ValueError(f"stage {stage.name} has no prescribed rolling windows")
    return pd.DataFrame(rows).sort_values(["deposit_timing", "start", "end"]).reset_index(drop=True)


def _validate_run_frame(
    frame: pd.DataFrame,
    spec: RealStockStrategySpec,
    expected_windows: pd.DataFrame,
) -> None:
    required = {
        "strategy",
        "family",
        "data_tier",
        "deposit_timing",
        "start",
        "end",
        "w12",
        "w24",
        "total_deposit",
        "max_drawdown",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"run frame missing columns: {missing}")
    key_columns = ["deposit_timing", "start", "end"]
    if frame.duplicated(key_columns).any():
        raise ValueError(f"run frame has duplicate timing/window keys: {spec.name}")
    observed = frame.loc[:, key_columns].astype(str).sort_values(key_columns).reset_index(drop=True)
    expected = expected_windows.loc[:, key_columns].astype(str).sort_values(key_columns).reset_index(drop=True)
    if not observed.equals(expected):
        raise ValueError(
            f"run frame does not match prescribed timing/window Cartesian product: "
            f"strategy={spec.name} observed={len(observed)} expected={len(expected)}"
        )
    if set(frame["strategy"].astype(str)) != {spec.name}:
        raise ValueError(f"run frame strategy identity mismatch: {spec.name}")
    if set(frame["family"].astype(str)) != {spec.family}:
        raise ValueError(f"run frame family identity mismatch: {spec.name}")
    expected_tier = str(spec.params.get("data_tier", "free_real"))
    if set(frame["data_tier"].astype(str)) != {expected_tier}:
        raise ValueError(f"run frame data tier mismatch: {spec.name}")
    if frame[["w12", "w24", "total_deposit", "max_drawdown"]].isna().any().any():
        raise ValueError(f"run frame has missing proof metrics: {spec.name}")


def _stage_cfg(base_cfg, stage: SearchStage):
    return replace(
        base_cfg,
        max_daily_amount_participation=stage.max_daily_amount_participation,
        slippage_bps=base_cfg.slippage_bps * stage.slippage_multiplier,
    )


def _select_stage_specs(
    stage_index: int,
    all_specs: list[RealStockStrategySpec],
    previous_candidates: pd.DataFrame | None,
) -> list[RealStockStrategySpec]:
    if stage_index == 0:
        return all_specs
    if previous_candidates is None or previous_candidates.empty:
        return []
    selected = set(previous_candidates["strategy"].astype(str))
    selected_specs = [spec for spec in all_specs if spec.name in selected]
    known = {spec.name for spec in selected_specs}
    if "parameters_json" in previous_candidates.columns:
        for row in previous_candidates.itertuples(index=False):
            strategy = str(row.strategy)
            if strategy in known:
                continue
            params = json.loads(str(row.parameters_json))
            selected_specs.append(
                RealStockStrategySpec(
                    name=strategy,
                    family=str(row.family),
                    params=params,
                )
            )
            known.add(strategy)
    return selected_specs


def _best_record(frame: pd.DataFrame) -> dict[str, object]:
    if frame.empty:
        return {}
    order = [column for column in ["passes_core_candidate_gates", "nonoverlap_hit_share_lower95", "p_success", "score"] if column in frame.columns]
    best = frame.sort_values(order, ascending=False).iloc[0]
    return {
        "best_deposit_timing": str(best["deposit_timing"]),
        "rolling_start_hit_share": float(best["rolling_start_hit_share"]),
        "nonoverlap_hit_share": float(best["nonoverlap_hit_share"]),
        "nonoverlap_hit_share_lower95": float(best["nonoverlap_hit_share_lower95"]),
        "median_w12": float(best["median_w12"]),
        "median_w24": float(best["median_w24"]),
        "p05_w24": float(best["p05_w24"]),
        "p10_w24": float(best["p10_w24"]),
        "p95_max_drawdown": float(best["p95_max_drawdown"]),
        "score": float(best["score"]),
    }


def _write_stage_report(
    path: Path,
    result: StageResult,
    search_id: str,
    active_study_id: str,
    panel_info: dict[str, object],
    code_info: dict[str, str],
    stage_cfg: SearchStage,
    base_config_path: str,
    search_config_path: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Phase 3 Search: {result.stage}",
        "",
        f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Scope",
        "",
        f"- Search id: `{search_id}`",
        f"- Study id: `{active_study_id}`",
        f"- Code signature: `{code_info['signature']}` (`{code_info['git_commit'][:12]}`).",
        f"- Base config: `{base_config_path}`",
        f"- Search config: `{search_config_path}`",
        f"- Panel signature: `{panel_info['signature']}`",
        f"- Panel: rows=`{panel_info['rows']}`, symbols=`{panel_info['symbols']}`, date range=`{panel_info['date_min']}..{panel_info['date_max']}`.",
        f"- Completion: `{result.completed_experiments}/{result.expected_experiments}` experiments.",
        f"- Window sampling: `{stage_cfg.window_sampling}`, max windows per timing=`{stage_cfg.max_windows or 'all'}`.",
        f"- Window-start development range: `{stage_cfg.window_start_min or 'unbounded'}..{stage_cfg.window_start_max or 'unbounded'}`.",
        f"- Deposit timings: `{', '.join(stage_cfg.deposit_timings)}`.",
        f"- Daily amount participation cap: `{stage_cfg.max_daily_amount_participation}`.",
        f"- Slippage multiplier: `{stage_cfg.slippage_multiplier}`.",
        "- This is a free-real execution research funnel with separately labeled proxy-research strategy lanes, not a final trade recommendation or strict-real leaderboard.",
        "- The panel is bound to the configured frozen point-in-time universe; claims are limited to its reported rows, symbols, dates, and free-real field quality.",
        "- Signals formed at a close are executed no earlier than the next trading-day open.",
        f"- Market regimes use a cross-sectional median-return proxy built from the frozen {panel_info['symbols']}-stock panel.",
        "",
        "## Candidate Funnel",
        "",
    ]
    if result.leaderboard.empty:
        lines.append("No completed leaderboard rows are available.")
    else:
        table = result.leaderboard.head(25).copy()
        table["p_success"] = table["p_success"].map(_fmt_pct)
        table["nonoverlap_hit_share"] = table["nonoverlap_hit_share"].map(_fmt_pct)
        table["nonoverlap_hit_share_lower95"] = table["nonoverlap_hit_share_lower95"].map(_fmt_pct)
        if "nonoverlap_binomial_pvalue" in table.columns:
            table["nonoverlap_binomial_pvalue"] = table["nonoverlap_binomial_pvalue"].map(_fmt_pvalue)
        if "holm_adjusted_exact_pvalue" in table.columns:
            table["holm_adjusted_exact_pvalue"] = table["holm_adjusted_exact_pvalue"].map(_fmt_pvalue)
        if "parameter_neighbor_stable_share" in table.columns:
            table["parameter_neighbor_stable_share"] = table["parameter_neighbor_stable_share"].map(_fmt_pct)
        table["median_w12"] = table["median_w12"].map(_fmt_money)
        table["median_w24"] = table["median_w24"].map(_fmt_money)
        table["p05_w24"] = table["p05_w24"].map(_fmt_money)
        table["p10_w24"] = table["p10_w24"].map(_fmt_money)
        table["p95_max_drawdown"] = table["p95_max_drawdown"].map(_fmt_pct)
        if "nonoverlap_min_w24" in table.columns:
            table["nonoverlap_min_w24"] = table["nonoverlap_min_w24"].map(_fmt_money)
        if "nonoverlap_max_drawdown" in table.columns:
            table["nonoverlap_max_drawdown"] = table["nonoverlap_max_drawdown"].map(_fmt_pct)
        keep = [
            "strategy",
            "family",
            "data_tier",
            "deposit_timing",
            "n_windows",
            "n_nonoverlap_windows",
            "n_nonoverlap_successes",
            "p_success",
            "nonoverlap_hit_share",
            "nonoverlap_hit_share_lower95",
            "nonoverlap_binomial_pvalue",
            "holm_adjusted_exact_pvalue",
            "passes_holm_exact_gate",
            "median_w12",
            "median_w24",
            "p05_w24",
            "p10_w24",
            "p95_max_drawdown",
            "nonoverlap_min_w24",
            "nonoverlap_max_drawdown",
            "passes_core_candidate_gates",
            "passes_regime_gate",
            "passes_all_deposit_timings",
            "n_parameter_neighbors",
            "parameter_neighbor_stable_share",
            "passes_neighborhood_gate",
            "passes_candidate_gates",
            "regimes_survived",
            "n_regimes",
            "search_score",
        ]
        lines.append(table[[column for column in keep if column in table.columns]].to_markdown(index=False))
        best = result.leaderboard.iloc[0]
        lines.extend(
            [
                "",
                "## Readout",
                "",
                f"- Current best: `{best['strategy']}` / `{best['deposit_timing']}`.",
                f"- Rolling-start hit share: `{_fmt_pct(best['p_success'])}`; non-overlap successes: `{int(best['n_nonoverlap_successes'])}/{int(best['n_nonoverlap_windows'])}`; exact/Holm p-values: `{_fmt_pvalue(best['nonoverlap_binomial_pvalue'])}` / `{_fmt_pvalue(best['holm_adjusted_exact_pvalue'])}`.",
                f"- Descriptive Wilson lower bound: `{_fmt_pct(best['nonoverlap_hit_share_lower95'])}`. Hard tails use minimum non-overlap W24 `{_fmt_money(best['nonoverlap_min_w24'])}` and maximum non-overlap drawdown `{_fmt_pct(best['nonoverlap_max_drawdown'])}`.",
                f"- Formal candidate gates passed: `{bool(best.get('passes_candidate_gates', False))}`.",
                f"- Strategies advanced to the next fidelity: `{len(result.candidates) if result.complete else 0}`.",
            ]
        )
    if not result.complete:
        lines.extend(["", "The stage is partial. Resume it before using its promotion list."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_repo_status(
    path: Path,
    search_id: str,
    active_study_id: str,
    search_config_path: str,
    panel_info: dict[str, object],
    code_info: dict[str, str],
    results: list[StageResult],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Phase 3 Search Status",
        "",
        f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Goal",
        "",
        "Systematically search distinct A-share strategy families under monthly deposits of 30,000 and the joint targets W12 >= 500,000 and W24 >= 1,200,000. Candidates must survive execution, liquidity, drawdown, tail-wealth, regime, and parameter-stability gates before they can be treated as evidence.",
        "",
        "## Search State",
        "",
        f"- Search id: `{search_id}`",
        f"- Study id: `{active_study_id}`",
        f"- Code signature: `{code_info['signature']}` (`{code_info['git_commit'][:12]}`).",
        f"- Config: `{search_config_path}`",
        f"- Frozen panel signature: `{panel_info['signature']}`",
        f"- Frozen panel: `{panel_info['rows']}` rows, `{panel_info['symbols']}` symbols, `{panel_info['date_min']}..{panel_info['date_max']}`.",
        "- Search method: deterministic discrete Latin-hypercube screen, family-preserving promotion, full rolling-window confirmation, then liquidity/slippage stress.",
        "- Evidence tiers: `free_real` and clearly separated `proxy_research`; strict-real admission remains separate.",
        "- The admitted panel is bound to the configured frozen point-in-time universe; its reported symbol count and date range define the evidence scope.",
        "- The run is admitted only after panel, frozen-universe, and data-config provenance hashes match.",
        "- Corporate actions adjust integer lots before the open with fractional cash-in-lieu; risk metrics use flow-adjusted NAV while W12/W24 use actual wealth.",
        "- `p_success` and Wilson intervals are descriptive. Hard statistical gates use at least five non-overlapping blocks, a one-sided exact binomial test against hit share 0.5, and Holm correction across every tested row.",
        "- A formal candidate must also pass joint W12/W24, worst-block wealth/drawdown, regime, liquidity, margin/default, neighborhood, lineage, every configured deposit timing, and a registered unseen outer-period gate.",
        "- Large run files and experiment ledger remain under the external data root.",
        "",
        "## Stage Summary",
        "",
        "| stage | completed | expected | windows | leaderboard_rows | advanced | status |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.stage} | {result.completed_experiments} | {result.expected_experiments} | {len(result.windows)} | {len(result.leaderboard)} | {len(result.candidates) if result.complete else 0} | {'complete' if result.complete else 'partial'} |"
        )
    complete_results = [result for result in results if result.complete and not result.leaderboard.empty]
    if complete_results:
        latest = complete_results[-1]
        best = latest.leaderboard.iloc[0]
        lines.extend(
            [
                "",
                "## Current Readout",
                "",
                f"- Latest complete stage: `{latest.stage}`.",
                f"- Best Phase 3 row: `{best['strategy']}` / `{best['deposit_timing']}`, rolling-start hit share `{_fmt_pct(best['p_success'])}`, exact/Holm p-values `{_fmt_pvalue(best['nonoverlap_binomial_pvalue'])}` / `{_fmt_pvalue(best['holm_adjusted_exact_pvalue'])}`, minimum non-overlap W24 `{_fmt_money(best['nonoverlap_min_w24'])}`.",
                "- Superseded 505-stock and post-hoc overlay rates are intentionally excluded from current reference comparisons.",
                "- A promoted row is a deeper-research candidate, not an approved strategy. The goal remains active until a candidate passes all hard gates or the reasonable search space is exhausted.",
            ]
        )
    else:
        lines.extend(["", "No stage is complete yet; the goal remains active."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_stage(
    search_id: str,
    active_study_id: str,
    stage: SearchStage,
    specs: list[RealStockStrategySpec],
    panel: pd.DataFrame,
    panel_by_date,
    panel_info: dict[str, object],
    code_info: dict[str, str],
    base_cfg,
    output_root: Path,
    ledger: ExperimentLedger,
    work_slice: slice,
    resume: bool,
    base_config_path: str,
    search_config_path: str,
    search_config: dict[str, object],
    parent_lineage_signature: str,
    lineage_roles: dict[str, str],
    relation_groups: Mapping[str, str],
    multiple_testing_registry: MultipleTestingRegistry,
) -> StageResult:
    cfg = _stage_cfg(base_cfg, stage)
    stage_dir = _stage_dir(output_root, stage.name)
    stage_dir.mkdir(parents=True, exist_ok=True)
    expected_windows = _expected_stage_windows(panel, cfg, stage)
    window_records = expected_windows.to_dict(orient="records")
    window_manifest_payload = {
        "schema_version": 1,
        "study_id": active_study_id,
        "stage": stage.name,
        "window_count_per_strategy": len(expected_windows),
        "required_deposit_timings": list(stage.deposit_timings),
        "windows": window_records,
        "signature": stable_hash(window_records, length=24),
    }
    window_manifest_path = stage_dir / "window_manifest.json"
    window_lock_path = stage_dir / ".window_manifest.lock"
    with window_lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if window_manifest_path.exists():
            current = json.loads(window_manifest_path.read_text(encoding="utf-8"))
            if current.get("signature") != window_manifest_payload["signature"]:
                raise ValueError(f"stage window manifest drifted: {stage.name}")
        else:
            _atomic_write_json(window_manifest_path, window_manifest_payload)
        fcntl.flock(lock, fcntl.LOCK_UN)

    if stage.promotion_gate == "formal":
        unexpected_roles = sorted(
            name for name, role in lineage_roles.items() if role != "center"
        )
        if unexpected_roles:
            raise ValueError(
                f"formal stage contains fresh diagnostic neighbors: {unexpected_roles[:3]}"
            )
    expected: list[tuple[RealStockStrategySpec, str, Path]] = []
    for spec in specs:
        run_id = experiment_id(
            active_study_id,
            spec,
            stage,
            cfg,
            lineage_signature=parent_lineage_signature,
        )
        expected.append((spec, run_id, _run_path(output_root, stage.name, run_id)))
    work_ids = {item[1] for item in expected[work_slice]}
    ledger_before = ledger.read() if resume else pd.DataFrame()
    ledger_by_id = (
        {str(row.experiment_id): row for row in ledger_before.itertuples(index=False)}
        if not ledger_before.empty and "experiment_id" in ledger_before.columns
        else {}
    )
    for index, (spec, run_id, run_path) in enumerate(expected, start=1):
        if run_id not in work_ids:
            continue
        cached_record = ledger_by_id.get(run_id)
        if resume and cached_record is not None and str(getattr(cached_record, "status", "")) == "completed":
            try:
                cached = pd.read_csv(run_path)
                _validate_run_frame(cached, spec, expected_windows)
                expected_hash = str(getattr(cached_record, "windows_sha256", ""))
                expected_rows = int(getattr(cached_record, "n_window_rows", -1))
                if not expected_hash or expected_hash == "nan" or file_sha256(run_path) != expected_hash:
                    raise ValueError("cached run hash mismatch")
                if len(cached) != expected_rows:
                    raise ValueError("cached run row count mismatch")
            except (OSError, ValueError, TypeError):
                pass
            else:
                print(f"[phase3:{stage.name}] cached {index}/{len(expected)} {spec.name}", flush=True)
                continue
        started_at = utc_now()
        ledger.upsert(
            {
                "experiment_id": run_id,
                "study_id": active_study_id,
                "behavior_id": behavior_id(spec, code_info["signature"]),
                "search_id": search_id,
                "stage": stage.name,
                "status": "running",
                "family": spec.family,
                "strategy": spec.name,
                "lineage_role": lineage_roles.get(spec.name, "center"),
                "parent_lineage_signature": parent_lineage_signature,
                "parameters_json": json.dumps(spec.params, sort_keys=True, ensure_ascii=True),
                "started_at": started_at,
                "completed_at": "",
                "windows_path": str(run_path),
            }
        )
        print(f"[phase3:{stage.name}] run {index}/{len(expected)} {spec.name}", flush=True)
        try:
            frame = evaluate_free_real_strategy(
                panel,
                spec,
                cfg=cfg,
                deposit_timings=stage.deposit_timings,
                max_windows=stage.max_windows,
                window_sampling=stage.window_sampling,
                window_start_min=stage.window_start_min,
                window_start_max=stage.window_start_max,
                panel_by_date=panel_by_date,
            )
            peak_rss_gib = _require_memory_budget(
                search_config,
                context=f"{stage.name}:{spec.name}",
            )
            _validate_run_frame(frame, spec, expected_windows)
            run_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_csv(run_path, frame)
            aggregate = aggregate_free_real_windows(frame, cfg)
            ledger.upsert(
                {
                    "experiment_id": run_id,
                    "study_id": active_study_id,
                    "behavior_id": behavior_id(spec, code_info["signature"]),
                    "search_id": search_id,
                    "stage": stage.name,
                    "status": "completed",
                    "family": spec.family,
                    "strategy": spec.name,
                    "lineage_role": lineage_roles.get(spec.name, "center"),
                    "parent_lineage_signature": parent_lineage_signature,
                    "parameters_json": json.dumps(spec.params, sort_keys=True, ensure_ascii=True),
                    "started_at": started_at,
                    "completed_at": utc_now(),
                    "windows_path": str(run_path),
                    "n_window_rows": int(len(frame)),
                    "windows_sha256": file_sha256(run_path),
                    "window_manifest_signature": window_manifest_payload["signature"],
                    "peak_rss_gib": peak_rss_gib,
                    **_best_record(aggregate),
                }
            )
        except Exception as exc:
            ledger.upsert(
                {
                    "experiment_id": run_id,
                    "study_id": active_study_id,
                    "behavior_id": behavior_id(spec, code_info["signature"]),
                    "search_id": search_id,
                    "stage": stage.name,
                    "status": "failed",
                    "family": spec.family,
                    "strategy": spec.name,
                    "lineage_role": lineage_roles.get(spec.name, "center"),
                    "parent_lineage_signature": parent_lineage_signature,
                    "parameters_json": json.dumps(spec.params, sort_keys=True, ensure_ascii=True),
                    "started_at": started_at,
                    "completed_at": utc_now(),
                    "windows_path": str(run_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise

    frames: list[pd.DataFrame] = []
    completed_count = 0
    current_ledger = ledger.read()
    current_by_id = (
        {str(row.experiment_id): row for row in current_ledger.itertuples(index=False)}
        if not current_ledger.empty and "experiment_id" in current_ledger.columns
        else {}
    )
    for spec, run_id, run_path in expected:
        record = current_by_id.get(run_id)
        if record is None or str(getattr(record, "status", "")) != "completed" or not run_path.exists():
            continue
        try:
            frame = pd.read_csv(run_path)
            _validate_run_frame(frame, spec, expected_windows)
            if file_sha256(run_path) != str(getattr(record, "windows_sha256", "")):
                raise ValueError("run hash does not match completed ledger record")
            if len(frame) != int(getattr(record, "n_window_rows", -1)):
                raise ValueError("run row count does not match completed ledger record")
        except (OSError, ValueError, TypeError):
            continue
        frames.append(frame)
        completed_count += 1
    complete = completed_count == len(expected)
    if not complete:
        progress_path = stage_dir / "shards" / (
            f"progress_{work_slice.start or 0}_{work_slice.stop or len(expected)}.json"
        )
        _atomic_write_json(
            progress_path,
            {
                "study_id": active_study_id,
                "stage": stage.name,
                "completed_experiments": completed_count,
                "expected_experiments": len(expected),
                "window_manifest_signature": window_manifest_payload["signature"],
            },
        )
        return StageResult(
            stage=stage.name,
            expected_experiments=len(expected),
            completed_experiments=completed_count,
            windows=pd.DataFrame(),
            leaderboard=pd.DataFrame(),
            candidates=pd.DataFrame(),
            complete=False,
        )

    finalize_lock_path = stage_dir / ".finalize.lock"
    with finalize_lock_path.open("w", encoding="utf-8") as finalize_lock:
        fcntl.flock(finalize_lock, fcntl.LOCK_EX)
        windows = pd.concat(frames, ignore_index=True)
        windows = annotate_market_regimes(panel, windows)
        leaderboard = aggregate_free_real_windows(windows, cfg)
        leaderboard, regime_breakdown = add_regime_metrics(
            leaderboard,
            windows,
            cfg,
            min_regime_blocks=stage.min_regime_blocks,
        )
        if not leaderboard.empty:
            params_by_strategy = {
                spec.name: json.dumps(spec.params, sort_keys=True, ensure_ascii=True)
                for spec in specs
            }
            margin_by_strategy = {
                spec.name: bool(spec.params.get("margin_evidence_required", False))
                for spec in specs
            }
            behavior_by_strategy = {
                spec.name: behavior_id(spec, code_info["signature"])
                for spec in specs
            }
            leaderboard["parameters_json"] = leaderboard["strategy"].map(params_by_strategy)
            leaderboard["lineage_role"] = leaderboard["strategy"].map(lineage_roles).fillna("center")
            leaderboard["margin_evidence_required"] = (
                leaderboard["strategy"].map(margin_by_strategy).fillna(False).astype(bool)
            )
            leaderboard["behavior_id"] = leaderboard["strategy"].map(behavior_by_strategy)
            if leaderboard["behavior_id"].fillna("").astype(str).str.strip().eq("").any():
                raise ValueError("leaderboard strategy is missing a semantic behavior_id")
        leaderboard = _attach_relation_groups(leaderboard, relation_groups)
        if stage.require_neighborhood_gate:
            leaderboard = add_neighborhood_metrics(
                leaderboard,
                specs=specs,
                search_config=search_config,
                cfg=cfg,
            )
        sampling = search_config.get("sampling", {}) if isinstance(search_config.get("sampling", {}), dict) else {}
        leaderboard = finalize_strategy_gates(
            leaderboard,
            cfg=cfg,
            required_deposit_timings=stage.deposit_timings,
            require_neighborhood_gate=stage.require_neighborhood_gate,
            evidence_role=stage.evidence_role,
            holdout_signature=stage.holdout_signature,
            development_min_target_ratio=float(sampling.get("development_min_target_ratio", 0.75)),
            development_max_drawdown=float(sampling.get("development_max_drawdown", 0.60)),
            multiple_testing_registry=multiple_testing_registry,
            study_id=active_study_id,
            stage=stage.name,
            evidence_context={
                "window_manifest_signature": str(window_manifest_payload["signature"]),
                "panel_signature": str(panel_info["signature"]),
                "code_signature": str(code_info["signature"]),
                "backtest_config_signature": stable_hash(vars(cfg), length=24),
            },
        )
        gate_column = {
            "exploratory": "passes_development_gates",
            "development": "passes_development_gates",
            "formal": "passes_candidate_gates",
        }[stage.promotion_gate]
        candidates = promote_candidates(
            leaderboard,
            stage.promote_per_family,
            stage.promote_global,
            allow_ungated_shortlist=stage.allow_ungated_shortlist,
            gate_column=gate_column,
            group_column="relation_group",
        )

        _atomic_write_csv(stage_dir / "windows.csv", windows)
        _atomic_write_csv(stage_dir / "leaderboard.csv", leaderboard)
        _atomic_write_csv(stage_dir / "regime_breakdown.csv", regime_breakdown)
        candidate_path = stage_dir / "candidates.csv"
        _atomic_write_csv(candidate_path, candidates)
        candidate_manifest = {
            "schema_version": 1,
            "study_id": active_study_id,
            "stage": stage.name,
            "parent_lineage_signature": parent_lineage_signature,
            "window_manifest_signature": window_manifest_payload["signature"],
            "promotion_gate": stage.promotion_gate,
            "evidence_role": stage.evidence_role,
            "candidates_sha256": file_sha256(candidate_path),
            "candidate_count": int(len(candidates)),
            "behaviors": [
                {
                    "strategy": str(row.strategy),
                    "behavior_id": behavior_id(
                        RealStockStrategySpec(
                            name=str(row.strategy),
                            family=str(row.family),
                            params=json.loads(str(row.parameters_json)),
                        ),
                        code_info["signature"],
                    ),
                    "lineage_role": str(getattr(row, "lineage_role", "center")),
                    "promotion_reason": str(row.promotion_reason),
                }
                for row in candidates.itertuples(index=False)
            ],
        }
        candidate_manifest["signature"] = stable_hash(candidate_manifest, length=24)
        _atomic_write_json(stage_dir / "candidates.manifest.json", candidate_manifest)
        fcntl.flock(finalize_lock, fcntl.LOCK_UN)

    result = StageResult(
        stage=stage.name,
        expected_experiments=len(expected),
        completed_experiments=completed_count,
        windows=windows,
        leaderboard=leaderboard,
        candidates=candidates,
        complete=complete,
        candidate_manifest_signature=str(candidate_manifest["signature"]),
    )
    _write_stage_report(
        stage_dir / "report.md",
        result=result,
        search_id=search_id,
        active_study_id=active_study_id,
        panel_info=panel_info,
        code_info=code_info,
        stage_cfg=stage,
        base_config_path=base_config_path,
        search_config_path=search_config_path,
    )
    return result


def _load_previous_candidates(output_root: Path, stage: str) -> tuple[pd.DataFrame, str]:
    path = _stage_dir(output_root, stage) / "candidates.csv"
    manifest_path = _stage_dir(output_root, stage) / "candidates.manifest.json"
    if not path.exists() and not manifest_path.exists():
        return pd.DataFrame(), ""
    if not path.exists() or not manifest_path.exists():
        raise ValueError(f"candidate lineage is incomplete for stage {stage}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("schema_version", 0)) != 1:
        raise ValueError(f"unsupported candidate manifest for stage {stage}")
    if manifest.get("candidates_sha256") != file_sha256(path):
        raise ValueError(f"candidate CSV hash mismatch for stage {stage}")
    signature = str(manifest.get("signature", ""))
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    if not signature or signature != stable_hash(unsigned, length=24):
        raise ValueError(f"candidate manifest signature mismatch for stage {stage}")
    frame = pd.read_csv(path)
    if int(manifest.get("candidate_count", -1)) != len(frame):
        raise ValueError(f"candidate count mismatch for stage {stage}")
    return frame, signature


def _validate_panel(panel: pd.DataFrame, cfg, enforce_symbol_count: bool = True) -> None:
    require_real_stock_panel(panel)
    tiers = sorted(panel["data_tier"].astype(str).dropna().unique().tolist())
    if tiers != ["free_real"]:
        raise ValueError(f"Phase 3 search requires free_real panel only; observed={tiers}")
    symbols = int(panel["ts_code"].astype(str).nunique())
    if enforce_symbol_count and cfg.min_symbols > 0 and symbols < cfg.min_symbols:
        raise ValueError(f"Phase 3 search requires at least {cfg.min_symbols} symbols; observed={symbols}")


def _validate_holdout_registry(search_config: dict[str, object], stages: list[SearchStage]) -> None:
    registry_value = str(search_config.get("holdout_registry", ""))
    if not registry_value:
        raise ValueError("Phase 3 search config must bind a holdout_registry")
    registry_path = Path(registry_value)
    if not registry_path.is_absolute():
        registry_path = ROOT / registry_path
    registry = load_search_config(registry_path)
    if int(registry.get("schema_version", 0)) != 1:
        raise ValueError("unsupported holdout registry schema")
    periods = registry.get("periods", {})
    if not isinstance(periods, dict):
        raise ValueError("holdout registry periods must be a mapping")
    for stage in stages:
        entry = periods.get(stage.holdout_signature)
        if not isinstance(entry, dict):
            raise ValueError(f"stage holdout signature is not registered: {stage.holdout_signature}")
        if str(entry.get("window_start_min", "")) != stage.window_start_min:
            raise ValueError(f"holdout registry start mismatch: {stage.name}")
        if str(entry.get("window_start_max", "")) != stage.window_start_max:
            raise ValueError(f"holdout registry end mismatch: {stage.name}")
        if stage.evidence_role == "formal_outer" and str(entry.get("status", "")) != "reserved_unseen":
            raise ValueError(f"formal stage is not bound to a reserved unseen holdout: {stage.name}")
    if any(stage.evidence_role == "formal_outer" for stage in stages):
        outer = registry.get("formal_outer", {})
        if not isinstance(outer, dict) or str(outer.get("status", "")) != "reserved_unseen":
            raise ValueError("formal outer holdout is not available")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run resumable multi-fidelity Phase 3 strategy search.")
    parser.add_argument("--search-config", default="config/phase3_search.yaml")
    parser.add_argument("--base-config", default="", help="Override base config declared by the search config.")
    parser.add_argument("--stage", choices=["all", "screen", "confirm", "stress"], default="all")
    parser.add_argument("--family", action="append", default=[], help="Optional family filter; repeat for multiple families.")
    parser.add_argument("--max-strategies", type=int, default=0, help="Debug cap after family filtering; 0 means all.")
    parser.add_argument("--start-index", type=int, default=0, help="Inclusive experiment index for sharded execution.")
    parser.add_argument("--end-index", type=int, default=0, help="Exclusive experiment index; 0 means the end.")
    parser.add_argument("--no-resume", action="store_true", help="Recompute selected experiments even if cached.")
    args = parser.parse_args()

    search_raw = load_search_config(args.search_config)
    search_id = str(search_raw.get("search_id", "phase3_search"))
    base_config_path = args.base_config or str(search_raw.get("base_config", "config/phase2_free_real_data.yaml"))
    base_config = load_config(base_config_path)
    cfg = load_backtest_config(base_config.raw)
    panel_path = base_config.data_root / "processed/phase2_free/stock_panel.parquet"
    if not panel_path.exists():
        raise SystemExit(f"missing free-real panel: {panel_path}")
    panel_manifest = validate_panel_manifest(
        panel_path,
        expected_symbols=int(base_config.raw.get("panel_build", {}).get("expected_symbols", 0)),
        config_path=base_config.path,
    )
    panel_info = panel_signature_from_manifest(panel_path, panel_manifest)
    if cfg.min_symbols > 0 and int(panel_info["symbols"]) < cfg.min_symbols:
        raise SystemExit(
            f"Phase 3 search requires at least {cfg.min_symbols} manifest symbols; observed={panel_info['symbols']}"
        )
    panel_info["universe_scope"] = str(panel_manifest.get("universe_scope", ""))
    panel_info["universe_start_date"] = str(panel_manifest.get("universe_start_date", ""))
    panel_info["universe_end_date"] = str(panel_manifest.get("universe_end_date", ""))
    code_info = repository_code_signature(ROOT)
    runtime_scope = {
        "family_filters": sorted(set(map(str, args.family))),
        "max_strategies": int(args.max_strategies),
    }
    identity_search_config = {**search_raw, "_runtime_scope": runtime_scope}
    active_study_id = study_id(
        search_id=search_id,
        search_config=identity_search_config,
        base_config=base_config.raw,
        panel_info=panel_info,
        code_info=code_info,
    )
    specs = build_search_strategy_specs(search_raw)
    specs = [
        spec
        for spec in specs
        if (
            str(spec.params.get("data_tier", "free_real")) == "proxy_research"
            or strategy_allowed_in_tier(spec.family, "free_real").allowed
        )
    ]
    if args.family:
        wanted = set(args.family)
        specs = [spec for spec in specs if spec.family in wanted]
    if args.max_strategies > 0:
        specs = specs[: args.max_strategies]
    if not specs:
        raise SystemExit("no admitted Phase 3 strategy specs")
    relation_groups = _load_relation_group_mapping()
    missing_relation_groups = sorted(
        {
            spec.family
            for spec in specs
            if not str(relation_groups.get(spec.family, "")).strip()
        }
    )
    if missing_relation_groups:
        raise ValueError(f"missing Phase 3 relation_group mapping: {missing_relation_groups}")

    output_base = Path(str(search_raw["output_root"])).expanduser()
    output_root = output_base / active_study_id
    output_root.mkdir(parents=True, exist_ok=True)
    ledger = ExperimentLedger(output_base / "experiment_ledger.csv")
    multiple_testing_registry = MultipleTestingRegistry(
        _resolve_multiple_testing_registry_path(search_raw, base_config.data_root)
    )
    stages = parse_search_stages(search_raw)
    _validate_holdout_registry(search_raw, stages)
    stage_names = [stage.name for stage in stages]
    if args.stage != "all":
        if args.stage not in stage_names:
            raise SystemExit(f"stage {args.stage} is not configured")
        start_stage = stage_names.index(args.stage)
        stages_to_run = [stages[start_stage]]
    else:
        stages_to_run = stages

    results: list[StageResult] = []
    previous_candidates: pd.DataFrame | None = None
    previous_lineage_signature = "root"
    known_specs: dict[str, RealStockStrategySpec] = {spec.name: spec for spec in specs}
    for stage_index, stage in enumerate(stages):
        if stage not in stages_to_run:
            previous_candidates, previous_lineage_signature = _load_previous_candidates(
                output_root,
                stage.name,
            )
            continue
        stage_specs = _select_stage_specs(stage_index, list(known_specs.values()), previous_candidates)
        center_names = {spec.name for spec in stage_specs}
        if stage.expand_parameter_neighbors:
            stage_specs = expand_parameter_neighbors(stage_specs, search_raw)
        lineage_roles = {
            spec.name: ("center" if spec.name in center_names else "diagnostic_neighbor")
            for spec in stage_specs
        }
        for spec in stage_specs:
            known_specs[spec.name] = spec
        if not stage_specs:
            raise SystemExit(f"stage {stage.name} has no candidates; complete the previous stage first")
        required_history_days = _required_signal_history_days(search_raw)
        derived_warmup_days = int(math.ceil(required_history_days * 1.6) + 30)
        warmup_days = max(int(search_raw.get("signal_warmup_calendar_days", 0)), derived_warmup_days)
        if warmup_days < 0:
            raise SystemExit("signal_warmup_calendar_days must be non-negative")
        stage_start = (
            (pd.Timestamp(stage.window_start_min) - pd.Timedelta(days=warmup_days)).strftime("%Y%m%d")
            if stage.window_start_min
            else ""
        )
        configured_end = pd.Timestamp(base_config.end_date)
        stage_end_bound = (
            pd.Timestamp(stage.window_start_max) + pd.DateOffset(months=cfg.window_months, days=62)
            if stage.window_start_max
            else configured_end
        )
        stage_end = min(configured_end, stage_end_bound).strftime("%Y%m%d")
        panel = load_free_real_analysis_panel(panel_path, start_date=stage_start, end_date=stage_end)
        _validate_panel(panel, cfg, enforce_symbol_count=False)
        prehistory_dates = panel.loc[
            panel["trade_date"].astype(str).lt(str(stage.window_start_min).replace("-", "")),
            "trade_date",
        ].nunique()
        if int(prehistory_dates) < required_history_days:
            raise SystemExit(
                f"stage {stage.name} has insufficient signal prehistory: "
                f"observed={prehistory_dates} required={required_history_days}"
            )
        panel = prepare_real_stock_features(panel)
        panel_by_date = _prepare_daily_panel(panel)
        peak_rss_gib = _require_memory_budget(search_raw, context=f"{stage.name}:panel_prepare")
        print(
            f"[phase3:{stage.name}] panel rows={len(panel)} symbols={panel['ts_code'].nunique()} "
            f"dates={panel['trade_date'].min()}..{panel['trade_date'].max()} peak_rss_gib={peak_rss_gib:.2f}",
            flush=True,
        )
        end_index = args.end_index if args.end_index > 0 else len(stage_specs)
        work_slice = slice(max(args.start_index, 0), min(end_index, len(stage_specs)))
        result = _run_stage(
            search_id=search_id,
            active_study_id=active_study_id,
            stage=stage,
            specs=stage_specs,
            panel=panel,
            panel_by_date=panel_by_date,
            panel_info=panel_info,
            code_info=code_info,
            base_cfg=cfg,
            output_root=output_root,
            ledger=ledger,
            work_slice=work_slice,
            resume=not args.no_resume,
            base_config_path=base_config_path,
            search_config_path=args.search_config,
            search_config=search_raw,
            parent_lineage_signature=previous_lineage_signature,
            lineage_roles=lineage_roles,
            relation_groups=relation_groups,
            multiple_testing_registry=multiple_testing_registry,
        )
        results.append(result)
        previous_candidates = result.candidates
        if result.candidate_manifest_signature:
            previous_lineage_signature = result.candidate_manifest_signature
        del panel_by_date
        del panel
        if not result.complete:
            break

    scoped_run = bool(runtime_scope["family_filters"] or runtime_scope["max_strategies"] > 0)
    repo_status = (
        output_root / "scoped_run_status.md"
        if scoped_run
        else ROOT / str(search_raw.get("repo_status_report", "reports/phase3_search_status.md"))
    )
    _write_repo_status(
        path=repo_status,
        search_id=search_id,
        active_study_id=active_study_id,
        search_config_path=args.search_config,
        panel_info=panel_info,
        code_info=code_info,
        results=results,
    )
    print(f"search_id={search_id}")
    print(f"study_id={active_study_id}")
    print(f"strategies={len(specs)}")
    print(f"output_root={output_root}")
    print(f"ledger={ledger.path}")
    print(f"multiple_testing_registry={multiple_testing_registry.path}")
    print(f"repo_status={repo_status}")
    for result in results:
        print(
            f"stage={result.stage} completed={result.completed_experiments}/{result.expected_experiments} "
            f"windows={len(result.windows)} candidates={len(result.candidates)}"
        )


if __name__ == "__main__":
    main()

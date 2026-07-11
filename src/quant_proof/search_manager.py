from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from itertools import product
from importlib.metadata import PackageNotFoundError, version as package_version
import json
from math import prod
import os
import platform
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterable

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml

from .free_real_backtest import FreeRealBacktestConfig, select_nonoverlapping_windows
from .real_strategies import RealStockStrategySpec


@dataclass(frozen=True)
class SearchStage:
    name: str
    max_windows: int
    window_sampling: str
    window_start_min: str
    window_start_max: str
    deposit_timings: tuple[str, ...]
    max_daily_amount_participation: float | None
    slippage_multiplier: float
    promote_per_family: int
    promote_global: int
    allow_ungated_shortlist: bool
    expand_parameter_neighbors: bool
    require_neighborhood_gate: bool
    promotion_gate: str
    evidence_role: str
    holdout_signature: str
    min_regime_blocks: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(payload: Any, length: int = 16) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
    return sha256(encoded).hexdigest()[:length]


def file_sha256(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_code_signature(repo_root: str | Path) -> dict[str, str]:
    root = Path(repo_root).resolve()
    digest = sha256()
    files: list[Path] = []
    for directory in [root / "src", root / "scripts", root / "config"]:
        if directory.exists():
            files.extend(
                path
                for path in directory.rglob("*")
                if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".toml"}
            )
    if (root / "pyproject.toml").exists():
        files.append(root / "pyproject.toml")
    if (root / "uv.lock").exists():
        files.append(root / "uv.lock")
    for path in sorted(set(files)):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    versions = {}
    for package in ["numpy", "pandas", "pyarrow", "pyyaml"]:
        try:
            versions[package] = package_version(package)
        except PackageNotFoundError:
            versions[package] = "missing"
    payload = {
        "git_commit": commit or "unknown",
        "tree_sha256": digest.hexdigest(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": json.dumps(versions, sort_keys=True, separators=(",", ":")),
    }
    return {**payload, "signature": stable_hash(payload, length=24)}


def load_search_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("search config must be a mapping")
    return raw


def parse_search_stages(raw: dict[str, Any]) -> list[SearchStage]:
    stages = raw.get("stages", {})
    if not isinstance(stages, dict) or not stages:
        raise ValueError("search config requires non-empty stages")
    out: list[SearchStage] = []
    for name, value in stages.items():
        if not isinstance(value, dict):
            raise ValueError(f"search stage {name} must be a mapping")
        participation = value.get("max_daily_amount_participation")
        promotion_gate = str(value.get("promotion_gate", "exploratory" if value.get("allow_ungated_shortlist") else "formal"))
        evidence_role = str(value.get("evidence_role", "development_exposed"))
        if promotion_gate not in {"exploratory", "development", "formal"}:
            raise ValueError(f"unsupported promotion_gate for {name}: {promotion_gate}")
        if evidence_role not in {"development_exposed", "formal_outer"}:
            raise ValueError(f"unsupported evidence_role for {name}: {evidence_role}")
        allow_ungated_shortlist = bool(value.get("allow_ungated_shortlist", False))
        promote_per_family = int(value.get("promote_per_family", 0))
        promote_global = int(value.get("promote_global", 0))
        if promote_per_family < 0 or promote_global < 0:
            raise ValueError(f"promotion shortlist counts must be non-negative for {name}")
        if name != "screen" and allow_ungated_shortlist:
            raise ValueError(f"only screen may allow an ungated shortlist: {name}")
        if not allow_ungated_shortlist and (promote_per_family or promote_global):
            raise ValueError(
                f"promotion shortlist counts require allow_ungated_shortlist=true for {name}"
            )
        if promotion_gate == "formal" and bool(value.get("expand_parameter_neighbors", False)):
            raise ValueError(f"formal stage must not expand fresh parameter neighbors: {name}")
        min_regime_blocks = int(value.get("min_regime_blocks", 2))
        if min_regime_blocks <= 0:
            raise ValueError(f"min_regime_blocks must be positive for {name}")
        out.append(
            SearchStage(
                name=str(name),
                max_windows=int(value.get("max_windows", 0)),
                window_sampling=str(value.get("window_sampling", "even")),
                window_start_min=str(value.get("window_start_min", "")),
                window_start_max=str(value.get("window_start_max", "")),
                deposit_timings=tuple(str(item) for item in value.get("deposit_timings", ["beginning", "ending"])),
                max_daily_amount_participation=None if participation is None else float(participation),
                slippage_multiplier=float(value.get("slippage_multiplier", 1.0)),
                promote_per_family=promote_per_family,
                promote_global=promote_global,
                allow_ungated_shortlist=allow_ungated_shortlist,
                expand_parameter_neighbors=bool(value.get("expand_parameter_neighbors", False)),
                require_neighborhood_gate=bool(value.get("require_neighborhood_gate", False)),
                promotion_gate=promotion_gate,
                evidence_role=evidence_role,
                holdout_signature=str(value.get("holdout_signature", "")),
                min_regime_blocks=min_regime_blocks,
            )
        )
    return out


def latin_hypercube_sample(
    parameter_grid: dict[str, Iterable[Any]],
    budget: int,
    seed: int,
) -> list[dict[str, Any]]:
    keys = sorted(parameter_grid)
    choices = {key: list(parameter_grid[key]) for key in keys}
    if any(not values for values in choices.values()):
        empty = [key for key, values in choices.items() if not values]
        raise ValueError(f"parameter grid has empty choices: {empty}")
    if not keys:
        return [{}]
    total = prod(len(choices[key]) for key in keys)
    if budget <= 0 or budget >= total:
        return [dict(zip(keys, values)) for values in product(*(choices[key] for key in keys))]

    rng = np.random.default_rng(seed)
    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    strata: dict[str, np.ndarray] = {}
    for key in keys:
        values = (np.arange(budget, dtype=float) + rng.random(budget)) / float(budget)
        rng.shuffle(values)
        strata[key] = values
    for row_index in range(budget):
        row = {}
        for key in keys:
            value_index = min(int(strata[key][row_index] * len(choices[key])), len(choices[key]) - 1)
            row[key] = choices[key][value_index]
        marker = stable_hash(row, length=64)
        if marker not in seen:
            seen.add(marker)
            samples.append(row)

    if len(samples) < budget:
        remaining = [dict(zip(keys, values)) for values in product(*(choices[key] for key in keys))]
        remaining.sort(key=lambda row: stable_hash({"seed": seed, "row": row}, length=64))
        for row in remaining:
            marker = stable_hash(row, length=64)
            if marker in seen:
                continue
            seen.add(marker)
            samples.append(row)
            if len(samples) >= budget:
                break
    return samples[:budget]


def build_search_strategy_specs(raw: dict[str, Any]) -> list[RealStockStrategySpec]:
    spaces = raw.get("strategy_spaces", {})
    if not isinstance(spaces, dict) or not spaces:
        raise ValueError("search config requires non-empty strategy_spaces")
    sampling = raw.get("sampling", {}) if isinstance(raw.get("sampling", {}), dict) else {}
    method = str(sampling.get("method", "latin_hypercube")).strip().lower()
    if method not in {"latin_hypercube", "discrete_latin_hypercube"}:
        raise ValueError(
            "sampling.method must be latin_hypercube or discrete_latin_hypercube"
        )
    base_seed = int(sampling.get("seed", 20260710))
    default_budget = int(sampling.get("budget_per_family", 0))
    specs: list[RealStockStrategySpec] = []
    for family, value in spaces.items():
        if not isinstance(value, dict):
            raise ValueError(f"strategy space {family} must be a mapping")
        kind = str(value.get("kind", ""))
        if not kind:
            raise ValueError(f"strategy space {family} requires kind")
        fixed = value.get("fixed", {}) if isinstance(value.get("fixed", {}), dict) else {}
        parameters = value.get("parameters", {}) if isinstance(value.get("parameters", {}), dict) else {}
        budget = int(value.get("budget", default_budget))
        family_seed = int(stable_hash({"base_seed": base_seed, "family": family}, length=8), 16)
        for sampled in latin_hypercube_sample(parameters, budget=budget, seed=family_seed):
            params = {"kind": kind, **fixed, **sampled}
            spec_hash = stable_hash({"family": family, "params": params}, length=12)
            specs.append(
                RealStockStrategySpec(
                    name=f"{family}_{spec_hash}",
                    family=str(family),
                    params=params,
                )
            )
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError("search strategy specs are not unique")
    return specs


def expand_parameter_neighbors(
    specs: Iterable[RealStockStrategySpec],
    search_config: dict[str, Any],
) -> list[RealStockStrategySpec]:
    spaces = search_config.get("strategy_spaces", {})
    expanded: dict[str, RealStockStrategySpec] = {}
    for spec in specs:
        expanded[spec.name] = spec
        space = spaces.get(spec.family, {}) if isinstance(spaces, dict) else {}
        parameters = space.get("parameters", {}) if isinstance(space, dict) else {}
        ordered_parameters = set(map(str, space.get("ordered_parameters", []))) if isinstance(space, dict) else set()
        if not isinstance(parameters, dict):
            continue
        for parameter, raw_choices in parameters.items():
            choices = list(raw_choices)
            if not choices or parameter not in spec.params:
                continue
            numeric_choices = all(
                isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool)
                for value in choices
            )
            if not numeric_choices and parameter not in ordered_parameters:
                continue
            try:
                position = choices.index(spec.params[parameter])
            except ValueError:
                continue
            for neighbor_position in [position - 1, position + 1]:
                if not 0 <= neighbor_position < len(choices):
                    continue
                params = dict(spec.params)
                params[parameter] = choices[neighbor_position]
                spec_hash = stable_hash({"family": spec.family, "params": params}, length=12)
                neighbor = RealStockStrategySpec(
                    name=f"{spec.family}_{spec_hash}",
                    family=spec.family,
                    params=params,
                )
                expanded[neighbor.name] = neighbor
    return sorted(expanded.values(), key=lambda item: (item.family, item.name))


def add_neighborhood_metrics(
    leaderboard: pd.DataFrame,
    specs: Iterable[RealStockStrategySpec],
    search_config: dict[str, Any],
    cfg: FreeRealBacktestConfig,
) -> pd.DataFrame:
    if leaderboard.empty:
        return leaderboard.copy()
    sampling = search_config.get("sampling", {}) if isinstance(search_config.get("sampling", {}), dict) else {}
    min_count = int(sampling.get("neighborhood_min_count", 2))
    min_stable_share = float(sampling.get("neighborhood_min_stable_share", 0.50))
    min_dimensions = int(sampling.get("neighborhood_min_dimensions", 1))
    min_median_ratio = float(sampling.get("neighborhood_min_median_target_ratio", 0.90))
    max_drawdown = float(sampling.get("neighborhood_max_p95_drawdown", 0.45))
    if min_count < 0 or min_dimensions < 1 or not 0.0 <= min_stable_share <= 1.0 or min_median_ratio < 0.0:
        raise ValueError("invalid parameter-neighborhood gate configuration")
    if not 0.0 <= max_drawdown <= 1.0:
        raise ValueError("neighborhood_max_p95_drawdown must be between 0 and 1")

    spec_by_name = {spec.name: spec for spec in specs}
    row_by_key = {
        (str(row.strategy), str(row.deposit_timing)): row
        for row in leaderboard.itertuples(index=False)
    }
    records = []
    for row in leaderboard.itertuples(index=False):
        spec = spec_by_name.get(str(row.strategy))
        neighbor_names: set[str] = set()
        if spec is not None:
            neighbor_names = {
                neighbor.name
                for neighbor in expand_parameter_neighbors([spec], search_config)
                if neighbor.name != spec.name and neighbor.name in spec_by_name
            }
        available = [
            row_by_key[(name, str(row.deposit_timing))]
            for name in sorted(neighbor_names)
            if (name, str(row.deposit_timing)) in row_by_key
        ]
        stable: list[bool] = []
        stable_dimensions: set[str] = set()
        for neighbor in available:
            median_w12 = float(getattr(neighbor, "nonoverlap_median_w12", getattr(neighbor, "median_w12", 0.0)))
            median_w24 = float(getattr(neighbor, "nonoverlap_median_w24", getattr(neighbor, "median_w24", 0.0)))
            worst_w24 = float(
                getattr(
                    neighbor,
                    "nonoverlap_min_w24",
                    getattr(neighbor, "nonoverlap_worst_w24", getattr(neighbor, "nonoverlap_p05_w24", 0.0)),
                )
            )
            worst_drawdown = float(
                getattr(
                    neighbor,
                    "nonoverlap_max_drawdown",
                    getattr(
                        neighbor,
                        "nonoverlap_worst_max_drawdown",
                        getattr(neighbor, "nonoverlap_p95_max_drawdown", 1.0),
                    ),
                )
            )
            is_stable = (
                median_w12 >= cfg.target_month_12 * min_median_ratio
                and median_w24 >= cfg.target_month_24 * min_median_ratio
                and worst_w24 >= cfg.candidate_min_p05_w24
                and worst_drawdown <= max_drawdown
            )
            stable.append(is_stable)
            if is_stable and spec is not None:
                neighbor_spec = spec_by_name.get(str(neighbor.strategy))
                if neighbor_spec is not None:
                    differing = {
                        key
                        for key in set(spec.params) | set(neighbor_spec.params)
                        if spec.params.get(key) != neighbor_spec.params.get(key)
                    }
                    if len(differing) == 1:
                        stable_dimensions.update(differing)
        n_available = len(available)
        stable_count = int(sum(stable))
        stable_share = stable_count / float(n_available) if n_available else 0.0
        records.append(
            {
                "strategy": str(row.strategy),
                "deposit_timing": str(row.deposit_timing),
                "n_parameter_neighbors": n_available,
                "stable_parameter_neighbors": stable_count,
                "stable_parameter_dimensions": len(stable_dimensions),
                "parameter_neighbor_stable_share": stable_share,
                "passes_neighborhood_gate": (
                    n_available >= min_count
                    and stable_share >= min_stable_share
                    and len(stable_dimensions) >= min_dimensions
                ),
            }
        )
    metrics = pd.DataFrame(records)
    return leaderboard.merge(metrics, on=["strategy", "deposit_timing"], how="left", validate="one_to_one")


def panel_signature(panel: pd.DataFrame, panel_path: str | Path) -> dict[str, Any]:
    path = Path(panel_path)
    stat = path.stat()
    dates = panel["trade_date"].astype(str)
    payload = {
        "path": str(path.resolve()),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "content_sha256": file_sha256(path),
        "rows": int(len(panel)),
        "symbols": int(panel["ts_code"].astype(str).nunique()),
        "date_min": str(dates.min()),
        "date_max": str(dates.max()),
        "columns": list(panel.columns),
    }
    return {**payload, "signature": stable_hash(payload, length=24)}


def panel_signature_from_manifest(panel_path: str | Path, manifest: dict[str, Any]) -> dict[str, Any]:
    path = Path(panel_path)
    stat = path.stat()
    payload = {
        "path": str(path.resolve()),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "content_sha256": str(manifest["panel_sha256"]),
        "rows": int(manifest["rows"]),
        "symbols": int(manifest["symbols"]),
        "date_min": str(manifest["date_min"]),
        "date_max": str(manifest["date_max"]),
        "columns": list(pq.ParquetFile(path).schema.names),
    }
    return {**payload, "signature": stable_hash(payload, length=24)}


def study_id(
    search_id: str,
    search_config: dict[str, Any],
    base_config: dict[str, Any],
    panel_info: dict[str, Any],
    code_info: dict[str, str],
) -> str:
    payload = {
        "schema_version": 1,
        "search_id": search_id,
        "search_config": search_config,
        "base_config": base_config,
        "panel_signature": panel_info["signature"],
        "code_signature": code_info["signature"],
    }
    return stable_hash(payload, length=24)


BEHAVIOR_METADATA_PARAM_KEYS = frozenset(
    {"data_tier", "family", "margin_evidence_required", "name"}
)


def semantic_behavior_payload(
    spec: RealStockStrategySpec,
    implementation_signature: str = "",
) -> dict[str, Any]:
    """Return only parameters that can affect scorer, position, or execution behavior."""
    effective_params = {
        str(key): value
        for key, value in spec.params.items()
        if str(key) not in BEHAVIOR_METADATA_PARAM_KEYS
    }
    payload: dict[str, Any] = {"params": effective_params}
    signature = str(implementation_signature).strip()
    if signature:
        payload["implementation_signature"] = signature
    return payload


def behavior_id(
    spec: RealStockStrategySpec,
    implementation_signature: str = "",
) -> str:
    return stable_hash(
        semantic_behavior_payload(spec, implementation_signature),
        length=24,
    )


def experiment_id(
    active_study_id: str,
    spec: RealStockStrategySpec,
    stage: SearchStage,
    cfg: FreeRealBacktestConfig,
    lineage_signature: str = "root",
) -> str:
    payload = {
        "study_id": active_study_id,
        "strategy": spec.name,
        "family": spec.family,
        "params": spec.params,
        "stage": asdict(stage),
        "backtest_config": asdict(cfg),
        "lineage_signature": str(lineage_signature),
    }
    return stable_hash(payload, length=24)


class ExperimentLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame()
        return pd.read_csv(self.path)

    def completed_ids(self) -> set[str]:
        frame = self.read()
        if frame.empty or "status" not in frame.columns:
            return set()
        return set(frame.loc[frame["status"] == "completed", "experiment_id"].astype(str))

    def upsert(self, record: dict[str, Any]) -> None:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.read()
            update = pd.DataFrame([record])
            frame = pd.concat([current, update], ignore_index=True) if not current.empty else update
            frame = frame.drop_duplicates("experiment_id", keep="last").sort_values(
                ["stage", "family", "strategy"]
            )
            temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp")
            try:
                frame.to_csv(temp, index=False, encoding="utf-8")
                temp.replace(self.path)
            finally:
                if temp.exists():
                    temp.unlink()
            fcntl.flock(lock, fcntl.LOCK_UN)


def annotate_market_regimes(panel: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    if windows.empty:
        return windows.copy()
    required = {"trade_date", "ts_code", "pct_chg"}
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(f"market regime annotation missing panel columns: {missing}")
    returns = panel.loc[:, ["trade_date", "ts_code", "pct_chg"]].copy()
    returns["trade_date"] = returns["trade_date"].astype(str)
    returns["pct_chg"] = pd.to_numeric(returns["pct_chg"], errors="coerce") / 100.0
    market_return = returns["pct_chg"].clip(-0.20, 0.20).groupby(returns["trade_date"], sort=True).median().fillna(0.0)
    market_level = (1.0 + market_return).cumprod()
    market_level.index = pd.to_datetime(market_level.index, format="%Y%m%d")

    rows = []
    pairs = windows.loc[:, ["start", "end"]].drop_duplicates().sort_values(["start", "end"])
    for row in pairs.itertuples(index=False):
        start = pd.Timestamp(row.start)
        end = pd.Timestamp(row.end)
        path = market_level.loc[(market_level.index >= start) & (market_level.index <= end)]
        if len(path) < 2:
            total_return = 0.0
            max_drawdown = 0.0
            recovery = 0.0
        else:
            normalized = path / float(path.iloc[0])
            total_return = float(normalized.iloc[-1] - 1.0)
            drawdown = normalized / normalized.cummax() - 1.0
            max_drawdown = float(-drawdown.min())
            trough = int(np.argmin(drawdown.to_numpy()))
            recovery = float(normalized.iloc[-1] / normalized.iloc[trough] - 1.0)
        if max_drawdown >= 0.30 and recovery >= 0.25:
            regime = "crash_rebound"
        elif max_drawdown >= 0.30:
            regime = "crash"
        elif total_return >= 0.25:
            regime = "bull"
        elif total_return <= -0.15:
            regime = "bear"
        elif abs(total_return) < 0.10:
            regime = "sideways"
        else:
            regime = "mixed"
        rows.append(
            {
                "start": start.date().isoformat(),
                "end": end.date().isoformat(),
                "market_regime": regime,
                "market_proxy_return": total_return,
                "market_proxy_max_drawdown": max_drawdown,
            }
        )
    regimes = pd.DataFrame(rows)
    return windows.merge(regimes, on=["start", "end"], how="left", validate="many_to_one")


def add_regime_metrics(
    leaderboard: pd.DataFrame,
    windows: pd.DataFrame,
    cfg: FreeRealBacktestConfig,
    min_regime_blocks: int = 2,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if leaderboard.empty or windows.empty or "market_regime" not in windows.columns:
        return leaderboard.copy(), pd.DataFrame()
    if min_regime_blocks <= 0:
        raise ValueError("min_regime_blocks must be positive")
    base_cols = ["strategy", "family", "data_tier", "deposit_timing"]
    rows = []
    for base_key, all_windows in windows.groupby(base_cols, sort=True):
        globally_nonoverlap = select_nonoverlapping_windows(all_windows)
        for regime, group in all_windows.groupby("market_regime", sort=True):
            nonoverlap = globally_nonoverlap.loc[
                globally_nonoverlap["market_regime"].astype(str).eq(str(regime))
            ]
            success = (group["w12"] >= cfg.target_month_12) & (group["w24"] >= cfg.target_month_24)
            nonoverlap_success = (
                (nonoverlap["w12"] >= cfg.target_month_12)
                & (nonoverlap["w24"] >= cfg.target_month_24)
            )
            estimable = len(nonoverlap) >= min_regime_blocks
            rows.append(
                {
                    **dict(zip(base_cols, base_key)),
                    "market_regime": str(regime),
                    "n_windows": int(len(group)),
                    "p_success": float(success.mean()),
                    "median_w24": float(group["w24"].median()),
                    "p05_w24": float(group["w24"].quantile(0.05)),
                    "p10_w24": float(group["w24"].quantile(0.10)),
                    "p95_max_drawdown": float(group["max_drawdown"].quantile(0.95)),
                    "n_nonoverlap_windows": int(len(nonoverlap)),
                    "regime_estimable": estimable,
                    "nonoverlap_hit_share": float(nonoverlap_success.mean()) if len(nonoverlap) else np.nan,
                    "nonoverlap_median_w24": float(nonoverlap["w24"].median()) if len(nonoverlap) else np.nan,
                    "nonoverlap_worst_w24": float(nonoverlap["w24"].min()) if len(nonoverlap) else np.nan,
                    "nonoverlap_worst_max_drawdown": (
                        float(nonoverlap["max_drawdown"].max()) if len(nonoverlap) else np.nan
                    ),
                }
            )
    breakdown = pd.DataFrame(rows)
    summary_rows = []
    for key, group in breakdown.groupby(base_cols, sort=True):
        estimable = group.loc[group["regime_estimable"].fillna(False)]
        survived = (
            (estimable["nonoverlap_worst_w24"] >= 720_000.0)
            & (estimable["nonoverlap_worst_max_drawdown"] <= 0.50)
        )
        summary_rows.append(
            {
                **dict(zip(base_cols, key)),
                "n_regimes": int(len(group)),
                "n_regimes_estimable": int(len(estimable)),
                "regimes_with_success": int((estimable["nonoverlap_hit_share"] > 0.0).sum()),
                "regimes_survived": int(survived.sum()),
                "min_regime_worst_w24": (
                    float(estimable["nonoverlap_worst_w24"].min()) if len(estimable) else np.nan
                ),
                "worst_regime_drawdown": (
                    float(estimable["nonoverlap_worst_max_drawdown"].max()) if len(estimable) else np.nan
                ),
            }
        )
    summary = pd.DataFrame(summary_rows)
    enriched = leaderboard.merge(summary, on=base_cols, how="left", validate="one_to_one")
    regime_share = enriched["regimes_with_success"] / enriched["n_regimes_estimable"].clip(lower=1)
    wealth_term = np.log((enriched["min_regime_worst_w24"] / cfg.target_month_24).clip(lower=1e-9)).fillna(-2.0)
    drawdown_penalty = (enriched["worst_regime_drawdown"] - 0.50).clip(lower=0.0).fillna(0.5)
    enriched["search_score"] = enriched["score"] + 5.0 * regime_share + 3.0 * wealth_term - 10.0 * drawdown_penalty
    enriched["passes_regime_gate"] = (
        enriched["n_regimes_estimable"].ge(3) & enriched["regimes_survived"].ge(3)
    )
    core_gate = enriched.get("passes_core_candidate_gates", False)
    if not isinstance(core_gate, pd.Series):
        core_gate = pd.Series(bool(core_gate), index=enriched.index)
    enriched["passes_row_candidate_gates"] = core_gate.fillna(False).astype(bool) & enriched["passes_regime_gate"]
    leaderboard_order = ["passes_row_candidate_gates"]
    if "nonoverlap_hit_share_lower95" in enriched.columns:
        leaderboard_order.append("nonoverlap_hit_share_lower95")
    leaderboard_order.extend(["p_success", "search_score", "median_w24"])
    enriched = enriched.sort_values(leaderboard_order, ascending=False).reset_index(drop=True)
    return enriched, breakdown


def holm_adjusted_pvalues(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").fillna(1.0).clip(lower=0.0, upper=1.0)
    if numeric.empty:
        return numeric
    ordered = numeric.sort_values(kind="mergesort")
    total = len(ordered)
    running = 0.0
    adjusted: dict[object, float] = {}
    for rank, (index, value) in enumerate(ordered.items()):
        running = max(running, min(1.0, float(value) * (total - rank)))
        adjusted[index] = running
    return pd.Series(adjusted, index=numeric.index, dtype=float)


class MultipleTestingRegistry:
    """Persistent, fail-closed hypothesis registry for formal outer holdouts."""

    KEY_COLUMNS = ("holdout_signature", "behavior_id", "deposit_timing")
    COLUMNS = (
        "schema_version",
        *KEY_COLUMNS,
        "exact_pvalue",
        "evidence_signature",
        "evidence_json",
        "study_id",
        "stage",
        "strategy",
        "family",
        "registered_at",
    )

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    @classmethod
    def _empty_frame(cls) -> pd.DataFrame:
        return pd.DataFrame(columns=list(cls.COLUMNS), dtype=str)

    @staticmethod
    def _pvalue_text(value: Any) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"exact pvalue is not numeric: {value!r}") from exc
        if not np.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
            raise ValueError(f"exact pvalue must be finite and in [0, 1]: {value!r}")
        if numeric == 0.0:
            numeric = 0.0
        return repr(numeric)

    @staticmethod
    def _evidence_text(value: Any) -> tuple[str, str]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("multiple-testing evidence_json is invalid") from exc
        if not isinstance(value, Mapping) or not value:
            raise ValueError("multiple-testing evidence must be a non-empty mapping")
        try:
            evidence_json = json.dumps(
                dict(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
                default=str,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("multiple-testing evidence is not JSON serializable") from exc
        return evidence_json, stable_hash(json.loads(evidence_json), length=64)

    @classmethod
    def _validate_frame(cls, frame: pd.DataFrame) -> pd.DataFrame:
        if list(frame.columns) != list(cls.COLUMNS):
            raise ValueError(
                "multiple-testing registry schema mismatch: "
                f"expected={list(cls.COLUMNS)} observed={list(frame.columns)}"
            )
        out = frame.fillna("").astype(str)
        if out.empty:
            return out
        required_nonempty = [
            "holdout_signature",
            "behavior_id",
            "deposit_timing",
            "exact_pvalue",
            "evidence_signature",
            "evidence_json",
            "study_id",
            "stage",
            "strategy",
            "family",
            "registered_at",
        ]
        for column in required_nonempty:
            if out[column].str.strip().eq("").any():
                raise ValueError(f"multiple-testing registry has empty {column}")
        if not out["schema_version"].eq("1").all():
            raise ValueError("unsupported multiple-testing registry schema version")
        if out.duplicated(list(cls.KEY_COLUMNS), keep=False).any():
            raise ValueError("multiple-testing registry contains duplicate hypothesis keys")
        for row in out.itertuples(index=False):
            if cls._pvalue_text(row.exact_pvalue) != row.exact_pvalue:
                raise ValueError("multiple-testing registry has non-canonical exact pvalue")
            evidence_json, evidence_signature = cls._evidence_text(row.evidence_json)
            if evidence_json != row.evidence_json or evidence_signature != row.evidence_signature:
                raise ValueError("multiple-testing registry evidence signature mismatch")
        return out

    def _read_unlocked(self) -> pd.DataFrame:
        if not self.path.exists():
            return self._empty_frame()
        try:
            frame = pd.read_csv(self.path, dtype=str, keep_default_na=False)
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise ValueError(f"cannot read multiple-testing registry: {self.path}") from exc
        return self._validate_frame(frame)

    def _write_unlocked(self, frame: pd.DataFrame) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="") as handle:
                frame.to_csv(handle, index=False, lineterminator="\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = -1
            if directory_fd >= 0:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temp.exists():
                temp.unlink()

    @classmethod
    def _normalize_records(
        cls,
        records: Mapping[str, Any] | Iterable[Mapping[str, Any]] | pd.DataFrame,
    ) -> pd.DataFrame:
        if isinstance(records, pd.DataFrame):
            raw_records = records.to_dict(orient="records")
        elif isinstance(records, Mapping):
            raw_records = [records]
        else:
            raw_records = list(records)
        normalized: dict[tuple[str, str, str], dict[str, str]] = {}
        batch_time = utc_now()
        for raw in raw_records:
            if not isinstance(raw, Mapping):
                raise ValueError("multiple-testing registry records must be mappings")
            key_values = tuple(str(raw.get(column, "")).strip() for column in cls.KEY_COLUMNS)
            if any(not value for value in key_values):
                raise ValueError("multiple-testing registry key fields must be non-empty")
            exact_pvalue = cls._pvalue_text(raw.get("exact_pvalue"))
            evidence_value = raw.get("evidence", raw.get("evidence_json"))
            evidence_json, evidence_signature = cls._evidence_text(evidence_value)
            provided_signature = str(raw.get("evidence_signature", "")).strip()
            if provided_signature and provided_signature != evidence_signature:
                raise ValueError("provided multiple-testing evidence signature does not match evidence")
            metadata = {
                column: str(raw.get(column, "")).strip()
                for column in ["study_id", "stage", "strategy", "family"]
            }
            if any(not value for value in metadata.values()):
                raise ValueError("multiple-testing provenance fields must be non-empty")
            record = {
                "schema_version": "1",
                **dict(zip(cls.KEY_COLUMNS, key_values)),
                "exact_pvalue": exact_pvalue,
                "evidence_signature": evidence_signature,
                "evidence_json": evidence_json,
                **metadata,
                "registered_at": str(raw.get("registered_at", "")).strip() or batch_time,
            }
            existing = normalized.get(key_values)
            if existing is not None:
                if (
                    existing["exact_pvalue"] != exact_pvalue
                    or existing["evidence_signature"] != evidence_signature
                ):
                    raise ValueError(f"conflicting duplicate hypothesis in registration batch: {key_values}")
                continue
            normalized[key_values] = record
        if not normalized:
            return cls._empty_frame()
        return pd.DataFrame(normalized.values(), columns=cls.COLUMNS, dtype=str)

    @classmethod
    def _merge_records(cls, current: pd.DataFrame, updates: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
        if updates.empty:
            return current, False
        current_by_key = {
            tuple(str(getattr(row, column)) for column in cls.KEY_COLUMNS): row
            for row in current.itertuples(index=False)
        }
        additions: list[dict[str, str]] = []
        for row in updates.itertuples(index=False):
            key = tuple(str(getattr(row, column)) for column in cls.KEY_COLUMNS)
            existing = current_by_key.get(key)
            if existing is not None:
                if (
                    str(existing.exact_pvalue) != str(row.exact_pvalue)
                    or str(existing.evidence_signature) != str(row.evidence_signature)
                ):
                    raise ValueError(f"multiple-testing registry conflict for hypothesis key: {key}")
                continue
            additions.append({column: str(getattr(row, column)) for column in cls.COLUMNS})
        if not additions:
            return current, False
        merged = pd.concat(
            [current, pd.DataFrame(additions, columns=cls.COLUMNS, dtype=str)],
            ignore_index=True,
        )
        merged = merged.sort_values(list(cls.KEY_COLUMNS), kind="mergesort").reset_index(drop=True)
        return cls._validate_frame(merged), True

    @classmethod
    def _adjusted_for_holdout(cls, frame: pd.DataFrame, holdout_signature: str) -> pd.DataFrame:
        signature = str(holdout_signature).strip()
        if not signature:
            raise ValueError("holdout_signature must be non-empty")
        subset = frame.loc[frame["holdout_signature"].eq(signature)].copy()
        subset = subset.sort_values(["behavior_id", "deposit_timing"], kind="mergesort").reset_index(drop=True)
        if subset.empty:
            return pd.DataFrame(
                columns=[*cls.KEY_COLUMNS, "exact_pvalue", "holm_adjusted_exact_pvalue", "registry_hypothesis_count"]
            )
        subset["exact_pvalue"] = pd.to_numeric(subset["exact_pvalue"], errors="raise")
        subset["holm_adjusted_exact_pvalue"] = holm_adjusted_pvalues(subset["exact_pvalue"])
        subset["registry_hypothesis_count"] = int(len(subset))
        return subset.loc[
            :,
            [*cls.KEY_COLUMNS, "exact_pvalue", "holm_adjusted_exact_pvalue", "registry_hypothesis_count"],
        ]

    @staticmethod
    def _public_frame(frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        if not out.empty:
            out["exact_pvalue"] = pd.to_numeric(out["exact_pvalue"], errors="raise")
        return out

    def read(self) -> pd.DataFrame:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            frame = self._read_unlocked()
            fcntl.flock(lock, fcntl.LOCK_UN)
        return self._public_frame(frame)

    def register(
        self,
        records: Mapping[str, Any] | Iterable[Mapping[str, Any]] | pd.DataFrame,
    ) -> pd.DataFrame:
        import fcntl

        updates = self._normalize_records(records)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self._read_unlocked()
            merged, changed = self._merge_records(current, updates)
            if changed:
                self._write_unlocked(merged)
            fcntl.flock(lock, fcntl.LOCK_UN)
        return self._public_frame(merged)

    def adjusted_pvalues(self, holdout_signature: str) -> pd.DataFrame:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            adjusted = self._adjusted_for_holdout(self._read_unlocked(), holdout_signature)
            fcntl.flock(lock, fcntl.LOCK_UN)
        return adjusted

    def register_and_adjust(
        self,
        records: Mapping[str, Any] | Iterable[Mapping[str, Any]] | pd.DataFrame,
        holdout_signature: str,
    ) -> pd.DataFrame:
        import fcntl

        updates = self._normalize_records(records)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self._read_unlocked()
            merged, changed = self._merge_records(current, updates)
            if changed:
                self._write_unlocked(merged)
            adjusted = self._adjusted_for_holdout(merged, holdout_signature)
            fcntl.flock(lock, fcntl.LOCK_UN)
        return adjusted


def finalize_strategy_gates(
    leaderboard: pd.DataFrame,
    cfg: FreeRealBacktestConfig,
    required_deposit_timings: Iterable[str],
    require_neighborhood_gate: bool,
    evidence_role: str,
    holdout_signature: str = "",
    development_min_target_ratio: float = 0.75,
    development_max_drawdown: float = 0.60,
    multiple_testing_registry: MultipleTestingRegistry | None = None,
    study_id: str = "",
    stage: str = "",
    evidence_context: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    if leaderboard.empty:
        return leaderboard.copy()
    if evidence_role not in {"development_exposed", "formal_outer"}:
        raise ValueError(f"unsupported evidence_role: {evidence_role}")
    required = frozenset(map(str, required_deposit_timings))
    if not required:
        raise ValueError("required_deposit_timings must not be empty")
    out = leaderboard.copy()
    out["passes_development_data_tier_gate"] = out["data_tier"].astype(str).isin(
        {"free_real", "free_real_derived_limits", "strict_real"}
    )
    out["passes_strict_data_tier_gate"] = out["data_tier"].astype(str).eq(
        "strict_real"
    )
    # Backward-compatible alias for development admission. Formal proof is stricter.
    out["passes_data_tier_gate"] = out["passes_development_data_tier_gate"]
    exact_pvalue = out.get(
        "nonoverlap_binomial_pvalue",
        out.get("nonoverlap_exact_pvalue", pd.Series(np.nan, index=out.index)),
    )
    exact_pvalue = pd.to_numeric(exact_pvalue, errors="coerce")
    out["descriptive_batch_holm_adjusted_exact_pvalue"] = holm_adjusted_pvalues(exact_pvalue)
    out["holm_adjusted_exact_pvalue"] = out["descriptive_batch_holm_adjusted_exact_pvalue"]
    out["holm_adjustment_scope"] = "current_batch_descriptive"
    out["holm_registry_hypothesis_count"] = 0
    out["holm_is_formal"] = False
    if evidence_role == "formal_outer":
        if not holdout_signature:
            raise ValueError("formal outer evidence requires a holdout_signature")
        if multiple_testing_registry is None:
            raise ValueError("formal outer evidence requires a persistent multiple-testing registry")
        if not str(study_id).strip() or not str(stage).strip():
            raise ValueError("formal outer evidence requires study_id and stage provenance")
        required_columns = {
            "behavior_id",
            "strategy",
            "family",
            "deposit_timing",
            "n_nonoverlap_windows",
            "n_nonoverlap_successes",
        }
        missing = sorted(required_columns - set(out.columns))
        if missing:
            raise ValueError(f"formal outer evidence is missing registry columns: {missing}")
        context = dict(evidence_context or {})
        strict_rows = out.loc[out["passes_strict_data_tier_gate"]]
        records = []
        for index, row in strict_rows.iterrows():
            pvalue = exact_pvalue.loc[index]
            if not np.isfinite(pvalue) or not 0.0 <= float(pvalue) <= 1.0:
                raise ValueError(f"formal outer exact pvalue is invalid at row {index}")
            trials_value = pd.to_numeric(pd.Series([row["n_nonoverlap_windows"]]), errors="coerce").iloc[0]
            successes_value = pd.to_numeric(pd.Series([row["n_nonoverlap_successes"]]), errors="coerce").iloc[0]
            if (
                not np.isfinite(trials_value)
                or not np.isfinite(successes_value)
                or float(trials_value) < 0.0
                or float(successes_value) < 0.0
                or not float(trials_value).is_integer()
                or not float(successes_value).is_integer()
                or float(successes_value) > float(trials_value)
            ):
                raise ValueError(f"formal outer binomial evidence is invalid at row {index}")
            records.append(
                {
                    "holdout_signature": str(holdout_signature),
                    "behavior_id": str(row["behavior_id"]),
                    "deposit_timing": str(row["deposit_timing"]),
                    "exact_pvalue": float(pvalue),
                    "evidence": {
                        **context,
                        "n_nonoverlap_windows": int(trials_value),
                        "n_nonoverlap_successes": int(successes_value),
                    },
                    "study_id": str(study_id),
                    "stage": str(stage),
                    "strategy": str(row["strategy"]),
                    "family": str(row["family"]),
                }
            )
        if records:
            adjusted = multiple_testing_registry.register_and_adjust(
                records,
                holdout_signature,
            )
            adjusted_by_key = adjusted.set_index(["behavior_id", "deposit_timing"])
            row_keys = list(
                zip(
                    strict_rows["behavior_id"].astype(str),
                    strict_rows["deposit_timing"].astype(str),
                )
            )
            try:
                adjusted_values = [
                    float(adjusted_by_key.loc[key, "holm_adjusted_exact_pvalue"])
                    for key in row_keys
                ]
            except KeyError as exc:
                raise RuntimeError(
                    "formal hypothesis was not present after atomic registry update"
                ) from exc
            out.loc[
                strict_rows.index,
                "holm_adjusted_exact_pvalue",
            ] = adjusted_values
            out.loc[strict_rows.index, "holm_adjustment_scope"] = (
                "persistent_holdout_registry"
            )
            out.loc[strict_rows.index, "holm_registry_hypothesis_count"] = int(
                len(adjusted)
            )
            out.loc[strict_rows.index, "holm_is_formal"] = True
    alpha = float(getattr(cfg, "candidate_joint_success_alpha", getattr(cfg, "candidate_alpha", 0.05)))
    out["passes_holm_exact_gate"] = out["holm_is_formal"] & out["holm_adjusted_exact_pvalue"].le(alpha)
    liquidity = out.get("passes_liquidity_gate", False)
    if not isinstance(liquidity, pd.Series):
        liquidity = pd.Series(bool(liquidity), index=out.index)
    out["passes_liquidity_gate"] = liquidity.fillna(False).astype(bool)
    margin_required = out.get("margin_evidence_required", False)
    if not isinstance(margin_required, pd.Series):
        margin_required = pd.Series(bool(margin_required), index=out.index)
    if {"margin_call_count", "defaulted"}.issubset(out.columns):
        margin_observed = out["margin_call_count"].fillna(np.inf).eq(0) & ~out["defaulted"].fillna(True).astype(bool)
    else:
        margin_observed = pd.Series(False, index=out.index)
    out["passes_margin_gate"] = ~margin_required.fillna(False).astype(bool) | margin_observed

    neighborhood = out.get("passes_neighborhood_gate", not require_neighborhood_gate)
    if not isinstance(neighborhood, pd.Series):
        neighborhood = pd.Series(bool(neighborhood), index=out.index)
    out["passes_required_neighborhood_gate"] = (
        neighborhood.fillna(False).astype(bool) if require_neighborhood_gate else True
    )
    worst_w24 = pd.to_numeric(
        out.get(
            "nonoverlap_min_w24",
            out.get("nonoverlap_worst_w24", out.get("nonoverlap_p05_w24", 0.0)),
        ),
        errors="coerce",
    ).fillna(0.0)
    worst_drawdown = pd.to_numeric(
        out.get(
            "nonoverlap_max_drawdown",
            out.get("nonoverlap_worst_max_drawdown", out.get("nonoverlap_p95_max_drawdown", 1.0)),
        ),
        errors="coerce",
    ).fillna(1.0)
    development_wealth = (
        out["nonoverlap_median_w12"].ge(cfg.target_month_12 * development_min_target_ratio)
        & out["nonoverlap_median_w24"].ge(cfg.target_month_24 * development_min_target_ratio)
        & worst_w24.ge(cfg.candidate_min_p05_w24 * development_min_target_ratio)
        & worst_drawdown.le(development_max_drawdown)
    )
    out["passes_development_row_gates"] = (
        development_wealth
        & out["passes_development_data_tier_gate"]
        & out["passes_required_neighborhood_gate"]
    )
    formal_core = out.get("passes_core_candidate_gates", False)
    if not isinstance(formal_core, pd.Series):
        formal_core = pd.Series(bool(formal_core), index=out.index)
    regime_gate = out.get("passes_regime_gate", False)
    if not isinstance(regime_gate, pd.Series):
        regime_gate = pd.Series(bool(regime_gate), index=out.index)
    out["passes_formal_row_gates"] = (
        formal_core.fillna(False).astype(bool)
        & regime_gate.fillna(False).astype(bool)
        & out["passes_holm_exact_gate"]
        & out["passes_strict_data_tier_gate"]
        & out["passes_liquidity_gate"]
        & out["passes_margin_gate"]
        & out["passes_required_neighborhood_gate"]
    )

    strategy_cols = ["strategy", "family", "data_tier"]
    timing_records = []
    for key, group in out.groupby(strategy_cols, sort=True):
        observed = frozenset(group["deposit_timing"].astype(str))
        development_passed = frozenset(
            group.loc[group["passes_development_row_gates"], "deposit_timing"].astype(str)
        )
        formal_passed = frozenset(
            group.loc[group["passes_formal_row_gates"], "deposit_timing"].astype(str)
        )
        timing_records.append(
            {
                **dict(zip(strategy_cols, key)),
                "deposit_timings_tested": len(observed),
                "deposit_timings_passed": len(formal_passed),
                "observed_deposit_timings": ",".join(sorted(observed)),
                "required_deposit_timings": ",".join(sorted(required)),
                "passes_required_deposit_timing_coverage": observed == required,
                "passes_all_deposit_timings_development": (
                    observed == required and development_passed == required
                ),
                "passes_all_deposit_timings": observed == required and formal_passed == required,
            }
        )
    timing = pd.DataFrame(timing_records)
    out = out.merge(timing, on=strategy_cols, how="left", validate="many_to_one")
    out["passes_development_gates"] = (
        out["passes_development_row_gates"]
        & out["passes_all_deposit_timings_development"].fillna(False).astype(bool)
    )
    formal_evidence_available = (
        evidence_role == "formal_outer"
        and bool(holdout_signature)
        and multiple_testing_registry is not None
    )
    out["formal_evidence_available"] = formal_evidence_available
    out["passes_candidate_gates"] = (
        out["passes_formal_row_gates"]
        & out["passes_all_deposit_timings"].fillna(False).astype(bool)
        & formal_evidence_available
    )
    out["candidate_gate_status"] = np.select(
        [out["passes_candidate_gates"], out["passes_development_gates"]],
        ["formal_pass", "development_pass"],
        default=("formal_evidence_unavailable" if not formal_evidence_available else "failed"),
    )
    return out


def promote_candidates(
    leaderboard: pd.DataFrame,
    per_family: int,
    global_count: int,
    allow_ungated_shortlist: bool = False,
    gate_column: str | None = None,
    group_column: str = "family",
) -> pd.DataFrame:
    if leaderboard.empty:
        return pd.DataFrame()
    score_column = "search_score" if "search_score" in leaderboard.columns else "score"
    probability_column = (
        "nonoverlap_hit_share_lower95"
        if "nonoverlap_hit_share_lower95" in leaderboard.columns
        else "p_success"
    )
    tail_column = (
        "nonoverlap_p05_w24"
        if "nonoverlap_p05_w24" in leaderboard.columns
        else ("p05_w24" if "p05_w24" in leaderboard.columns else "p10_w24")
    )
    median_column = "nonoverlap_median_w24" if "nonoverlap_median_w24" in leaderboard.columns else "median_w24"
    if gate_column is None:
        gate_column = "passes_candidate_gates" if "passes_candidate_gates" in leaderboard.columns else None
    elif gate_column not in leaderboard.columns:
        raise ValueError(f"promotion gate column is missing: {gate_column}")
    order_columns = ([gate_column] if gate_column else []) + [
        probability_column,
        "p_success",
        tail_column,
        median_column,
        score_column,
    ]
    order_columns = list(dict.fromkeys(order_columns))
    ordered = leaderboard.sort_values(order_columns, ascending=False)
    best_per_strategy = ordered.drop_duplicates("strategy", keep="first")
    if allow_ungated_shortlist:
        if group_column not in best_per_strategy.columns:
            raise ValueError(f"promotion group column is missing: {group_column}")
        if best_per_strategy[group_column].fillna("").astype(str).str.strip().eq("").any():
            raise ValueError(f"promotion group column has missing values: {group_column}")
    selected: dict[str, set[str]] = {}
    gated = pd.Series(False, index=best_per_strategy.index)
    if gate_column:
        gated = best_per_strategy[gate_column].fillna(False).astype(bool)
        for row in best_per_strategy.loc[gated].itertuples(index=False):
            selected.setdefault(str(row.strategy), set()).add("hard_gate")
    ungated = best_per_strategy.loc[~gated]
    if allow_ungated_shortlist and per_family > 0:
        for _, group in ungated.groupby(group_column, sort=True):
            for row in group.head(per_family).itertuples(index=False):
                selected.setdefault(str(row.strategy), set()).add(group_column)
    if allow_ungated_shortlist and global_count > 0:
        for row in ungated.head(global_count).itertuples(index=False):
            selected.setdefault(str(row.strategy), set()).add("global")
    if not selected:
        return pd.DataFrame(columns=[*best_per_strategy.columns, "promotion_reason"])
    out = best_per_strategy.loc[best_per_strategy["strategy"].isin(selected)].copy()
    out["promotion_reason"] = out["strategy"].map(lambda name: "+".join(sorted(selected[str(name)])))
    return out.sort_values(order_columns, ascending=False).reset_index(drop=True)

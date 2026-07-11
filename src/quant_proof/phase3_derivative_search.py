from __future__ import annotations

from dataclasses import replace
import json
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from quant_proof.engine.combined_account import CombinedAccount
from quant_proof.free_real_backtest import (
    FreeRealBacktestConfig,
    aggregate_free_real_windows,
    select_rolling_windows,
    simulate_free_real_window,
)
from quant_proof.phase3_overlay_factory import (
    OverlaySearchSpec,
    OverlayStageBudget,
    Phase3OverlayResources,
    build_phase3_overlay_factory_from_resources,
)
from quant_proof.simulator import rolling_windows


DERIVATIVE_EXECUTION_TIER = "daily_settlement_no_quotes"
DERIVATIVE_COUNTER_COLUMNS = (
    "derivative_requested_contracts",
    "derivative_filled_contracts",
    "derivative_clipped_contracts",
    "derivative_rejected_contracts",
    "derivative_fees",
    "derivative_missing_catalog_events",
    "derivative_missing_execution_parameter_events",
    "futures_margin_rate_updates",
    "futures_margin_transfer",
    "futures_expiry_settlements",
    "option_expiry_settlements",
)


def overlay_family(spec: OverlaySearchSpec) -> str:
    if spec.composition == "futures_only" and spec.direction_rule is not None:
        return "P35_cffex_dynamic_direction_overlay"
    return {
        "futures_only": "P32_cffex_whole_lot_futures_overlay",
        "long_option_only": "P33_cffex_long_option_convexity_budget",
        "futures_plus_long_option": "P34_cffex_combined_derivative_overlay",
    }[spec.composition]


def overlay_products(spec: OverlaySearchSpec) -> tuple[str, ...]:
    products: list[str] = []
    if spec.futures is not None:
        products.append(spec.futures.product)
    if spec.long_option is not None:
        products.append(spec.long_option.product)
    return tuple(products)


def overlay_lineage_key(spec: OverlaySearchSpec) -> str:
    """Return the categorical derivative kernel preserved across search stages."""

    direction_rule = None
    if spec.direction_rule is not None:
        direction_rule = {
            "kind": spec.direction_rule.kind,
            "position_mode": spec.direction_rule.position_mode,
            "trend_variant": spec.direction_rule.trend_variant,
        }
    payload = {
        "composition": spec.composition,
        "direction_rule": direction_rule,
        "futures": (
            None
            if spec.futures is None
            else {
                "product": spec.futures.product,
                "direction": spec.futures.direction,
            }
        ),
        "long_option": (
            None
            if spec.long_option is None
            else {
                "product": spec.long_option.product,
                "option_type": spec.long_option.option_type,
            }
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def select_inherited_overlay_specs(
    stage_specs: Sequence[OverlaySearchSpec],
    parent_specs: Sequence[OverlaySearchSpec],
    parent_candidate_ids: Iterable[str],
) -> tuple[tuple[OverlaySearchSpec, ...], dict[str, tuple[str, ...]]]:
    """Restrict a registered stage grid to categorical neighbors of its parents."""

    candidate_ids = tuple(map(str, parent_candidate_ids))
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("parent candidate IDs must be unique")
    parent_by_id = {spec.overlay_id: spec for spec in parent_specs}
    missing = sorted(set(candidate_ids) - set(parent_by_id))
    if missing:
        raise ValueError(f"parent candidates are absent from the registered stage: {missing}")

    parents_by_key: dict[str, list[str]] = {}
    for candidate_id in candidate_ids:
        key = overlay_lineage_key(parent_by_id[candidate_id])
        parents_by_key.setdefault(key, []).append(candidate_id)

    selected: list[OverlaySearchSpec] = []
    lineage: dict[str, tuple[str, ...]] = {}
    for spec in stage_specs:
        parent_ids = tuple(sorted(parents_by_key.get(overlay_lineage_key(spec), ())))
        if not parent_ids:
            continue
        selected.append(spec)
        lineage[spec.overlay_id] = parent_ids
    return tuple(selected), lineage


def stage_resources(
    resources: Phase3OverlayResources,
    budget: OverlayStageBudget,
) -> Phase3OverlayResources:
    assumptions = replace(
        resources.assumptions,
        futures_slippage_bps=budget.futures_slippage_bps,
        option_slippage_bps=budget.option_slippage_bps,
        prior_day_volume_participation=budget.prior_day_volume_participation,
    )
    return replace(resources, assumptions=assumptions)


def select_overlay_windows(
    resources: Phase3OverlayResources,
    spec: OverlaySearchSpec,
    cfg: FreeRealBacktestConfig,
    *,
    max_windows: int,
    sampling: str = "even",
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    ranges = resources.catalog.product_date_ranges
    products = overlay_products(spec)
    missing = sorted(set(products) - set(ranges))
    if missing:
        raise ValueError(f"overlay products are absent from the CFFEX panel: {missing}")
    first_eligible = max(ranges[product][0] for product in products)
    last_eligible = min(ranges[product][1] for product in products)
    dates = pd.DatetimeIndex(
        pd.to_datetime(resources.catalog.available_dates, format="%Y%m%d")
    )
    windows = rolling_windows(
        dates,
        window_months=cfg.window_months,
        min_trading_days=cfg.min_trading_days,
    )
    windows = [
        (start, end)
        for start, end in windows
        if start >= pd.Timestamp(first_eligible) and end <= pd.Timestamp(last_eligible)
    ]
    return select_rolling_windows(
        windows,
        max_windows=max_windows,
        sampling=sampling,
    )


def evaluate_overlay_spec(
    resources: Phase3OverlayResources,
    spec: OverlaySearchSpec,
    cfg: FreeRealBacktestConfig,
    budget: OverlayStageBudget,
    *,
    deposit_timings: Iterable[str] = ("beginning", "ending"),
    window_sampling: str = "even",
    panel_by_date: Mapping[str, Mapping[str, Mapping[str, object]]] | None = None,
) -> pd.DataFrame:
    if spec.stage != budget.stage:
        raise ValueError("overlay spec and stage budget must share one stage")
    selected_resources = stage_resources(resources, budget)
    windows = select_overlay_windows(
        selected_resources,
        spec,
        cfg,
        max_windows=budget.max_windows,
        sampling=window_sampling,
    )
    if not windows:
        return pd.DataFrame()
    trading_dates = list(selected_resources.catalog.available_dates)
    empty_panel = panel_by_date or {trade_date: {} for trade_date in trading_dates}
    coordinator_factory = build_phase3_overlay_factory_from_resources(
        selected_resources,
        spec,
    )
    rows: list[dict[str, object]] = []
    for deposit_timing in tuple(map(str, deposit_timings)):
        for start, end in windows:
            _, metrics = simulate_free_real_window(
                panel_by_date=empty_panel,
                trading_dates=trading_dates,
                top_by_signal_date={},
                rebalance_dates=set(),
                start=start,
                end=end,
                deposit_timing=deposit_timing,
                cfg=cfg,
                account_factory=CombinedAccount,
                derivative_coordinator_factory=coordinator_factory,
            )
            if not metrics:
                continue
            rows.append(
                {
                    "strategy": spec.overlay_id,
                    "overlay_id": spec.overlay_id,
                    "family": overlay_family(spec),
                    "composition": spec.composition,
                    "products": ",".join(overlay_products(spec)),
                    "data_tier": "official_exchange_daily",
                    "execution_tier": DERIVATIVE_EXECUTION_TIER,
                    "stage": spec.stage,
                    "deposit_timing": deposit_timing,
                    "start": start.date().isoformat(),
                    "end": end.date().isoformat(),
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def aggregate_overlay_windows(
    windows: pd.DataFrame,
    cfg: FreeRealBacktestConfig,
    *,
    required_deposit_timings: Iterable[str] = ("beginning", "ending"),
) -> pd.DataFrame:
    if windows.empty:
        return pd.DataFrame()
    base = aggregate_free_real_windows(windows, cfg)
    group_columns = ["strategy", "family", "data_tier", "deposit_timing"]
    rows: list[dict[str, object]] = []
    for key, group in windows.groupby(group_columns, sort=True):
        def values(column: str) -> pd.Series:
            if column not in group.columns:
                return pd.Series(0.0, index=group.index, dtype=float)
            return pd.to_numeric(group[column], errors="coerce").fillna(0.0)

        requested = float(values("derivative_requested_contracts").sum())
        filled = float(values("derivative_filled_contracts").sum())
        clipped = float(values("derivative_clipped_contracts").sum())
        rejected = float(values("derivative_rejected_contracts").sum())
        requested_by_window = values("derivative_requested_contracts")
        filled_by_window = values("derivative_filled_contracts")
        requested_active_windows = int(requested_by_window.gt(0.0).sum())
        filled_active_windows = int(filled_by_window.gt(0.0).sum())
        active_windows = int(
            (requested_by_window.gt(0.0) & filled_by_window.gt(0.0)).sum()
        )
        window_count = int(len(group))
        rows.append(
            {
                **dict(zip(group_columns, key)),
                "derivative_requested_contracts": requested,
                "derivative_filled_contracts": filled,
                "derivative_clipped_contracts": clipped,
                "derivative_rejected_contracts": rejected,
                "derivative_fill_share": filled / requested if requested > 0.0 else 0.0,
                "derivative_clipped_share": clipped / requested if requested > 0.0 else 0.0,
                "derivative_rejected_share": rejected / requested if requested > 0.0 else 0.0,
                "derivative_window_count": window_count,
                "derivative_requested_active_windows": requested_active_windows,
                "derivative_filled_active_windows": filled_active_windows,
                "derivative_active_windows": active_windows,
                "derivative_requested_active_window_share": (
                    requested_active_windows / window_count if window_count else 0.0
                ),
                "derivative_filled_active_window_share": (
                    filled_active_windows / window_count if window_count else 0.0
                ),
                "derivative_active_window_share": (
                    active_windows / window_count if window_count else 0.0
                ),
                "passes_overlay_timing_activity_gate": (
                    requested > 0.0 and filled > 0.0 and active_windows > 0
                ),
                "avg_derivative_fees": float(values("derivative_fees").mean()),
                "max_missing_catalog_events": float(
                    values("derivative_missing_catalog_events").max()
                ),
                "max_missing_execution_parameter_events": float(
                    values("derivative_missing_execution_parameter_events").max()
                ),
                "avg_futures_margin_rate_updates": float(
                    values("futures_margin_rate_updates").mean()
                ),
                "max_abs_futures_margin_transfer": float(
                    values("futures_margin_transfer").abs().max()
                ),
                "futures_expiry_settlements": float(
                    values("futures_expiry_settlements").sum()
                ),
                "option_expiry_settlements": float(
                    values("option_expiry_settlements").sum()
                ),
            }
        )
    execution = pd.DataFrame(rows)
    out = base.merge(execution, on=group_columns, how="left", validate="one_to_one")
    activity_group_columns = ["strategy", "family", "data_tier"]
    activity = (
        execution.groupby(activity_group_columns, sort=True, as_index=False)
        .agg(
            overall_derivative_requested_contracts=(
                "derivative_requested_contracts",
                "sum",
            ),
            overall_derivative_filled_contracts=(
                "derivative_filled_contracts",
                "sum",
            ),
            overall_derivative_active_windows=("derivative_active_windows", "sum"),
            overall_derivative_window_count=("derivative_window_count", "sum"),
            passes_all_deposit_timings_activity=(
                "passes_overlay_timing_activity_gate",
                "all",
            ),
        )
    )
    activity["overall_derivative_active_window_share"] = (
        activity["overall_derivative_active_windows"]
        / activity["overall_derivative_window_count"].replace(0, np.nan)
    ).fillna(0.0)
    activity["passes_overlay_activity_gate"] = (
        activity["overall_derivative_requested_contracts"].gt(0.0)
        & activity["overall_derivative_filled_contracts"].gt(0.0)
        & activity["overall_derivative_active_window_share"].gt(0.0)
        & activity["passes_all_deposit_timings_activity"].fillna(False)
    )
    out = out.merge(
        activity,
        on=activity_group_columns,
        how="left",
        validate="many_to_one",
    )
    out["passes_overlay_execution_gate"] = (
        out["data_tier"].eq("official_exchange_daily")
        & out["passes_overlay_activity_gate"].fillna(False)
        & out["passes_overlay_timing_activity_gate"].fillna(False)
        & out["margin_accounting_valid"].fillna(False)
        & out["margin_call_window_share"].eq(0.0)
        & out["default_window_share"].eq(0.0)
        & out["max_missing_execution_parameter_events"].eq(0.0)
        & out["derivative_rejected_share"].le(0.20)
    )
    out["passes_overlay_development_row_gate"] = (
        out["nonoverlap_median_w12"].ge(cfg.target_month_12 * 0.75)
        & out["nonoverlap_median_w24"].ge(cfg.target_month_24 * 0.75)
        & out["nonoverlap_min_w24"].ge(cfg.candidate_min_p05_w24 * 0.75)
        & out["nonoverlap_max_drawdown"].le(0.60)
        & out["passes_overlay_execution_gate"]
    )
    required = frozenset(map(str, required_deposit_timings))
    timing_rows: list[dict[str, object]] = []
    for key, group in out.groupby(["strategy", "family", "data_tier"], sort=True):
        observed = frozenset(group["deposit_timing"].astype(str))
        passed = frozenset(
            group.loc[
                group["passes_overlay_development_row_gate"],
                "deposit_timing",
            ].astype(str)
        )
        timing_rows.append(
            {
                "strategy": key[0],
                "family": key[1],
                "data_tier": key[2],
                "passes_all_deposit_timings_overlay_development": (
                    observed == required and passed == required
                ),
            }
        )
    out = out.merge(
        pd.DataFrame(timing_rows),
        on=["strategy", "family", "data_tier"],
        how="left",
        validate="many_to_one",
    )
    out["passes_overlay_development_gates"] = (
        out["passes_overlay_development_row_gate"]
        & out["passes_all_deposit_timings_overlay_development"].fillna(False)
    )
    out["formal_evidence_available"] = False
    out["candidate_gate_status"] = np.where(
        out["passes_overlay_development_gates"],
        "development_pass",
        "failed",
    )
    return out.sort_values(
        [
            "passes_overlay_development_gates",
            "nonoverlap_hit_share_lower95",
            "p_success",
            "nonoverlap_p05_w24",
            "score",
        ],
        ascending=False,
    ).reset_index(drop=True)


def select_overlay_candidates(
    leaderboard: pd.DataFrame,
    *,
    max_promotions: int,
) -> pd.DataFrame:
    if leaderboard.empty or max_promotions <= 0:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for strategy, group in leaderboard.groupby("strategy", sort=True):
        rows.append(
            {
                "strategy": strategy,
                "family": str(group["family"].iloc[0]),
                "passes_overlay_development_gates": bool(
                    group["passes_overlay_development_gates"].all()
                ),
                "passes_overlay_activity_gate": bool(
                    group.get(
                        "passes_overlay_activity_gate",
                        pd.Series(False, index=group.index),
                    )
                    .fillna(False)
                    .all()
                ),
                "worst_timing_active_window_share": float(
                    pd.to_numeric(
                        group.get(
                            "derivative_active_window_share",
                            pd.Series(0.0, index=group.index),
                        ),
                        errors="coerce",
                    )
                    .fillna(0.0)
                    .min()
                ),
                "overall_derivative_active_window_share": float(
                    pd.to_numeric(
                        group.get(
                            "overall_derivative_active_window_share",
                            pd.Series(0.0, index=group.index),
                        ),
                        errors="coerce",
                    )
                    .fillna(0.0)
                    .min()
                ),
                "total_derivative_requested_contracts": float(
                    pd.to_numeric(
                        group.get(
                            "derivative_requested_contracts",
                            pd.Series(0.0, index=group.index),
                        ),
                        errors="coerce",
                    )
                    .fillna(0.0)
                    .sum()
                ),
                "total_derivative_filled_contracts": float(
                    pd.to_numeric(
                        group.get(
                            "derivative_filled_contracts",
                            pd.Series(0.0, index=group.index),
                        ),
                        errors="coerce",
                    )
                    .fillna(0.0)
                    .sum()
                ),
                "total_futures_expiry_settlements": float(
                    pd.to_numeric(
                        group.get(
                            "futures_expiry_settlements",
                            pd.Series(0.0, index=group.index),
                        ),
                        errors="coerce",
                    )
                    .fillna(0.0)
                    .sum()
                ),
                "total_option_expiry_settlements": float(
                    pd.to_numeric(
                        group.get(
                            "option_expiry_settlements",
                            pd.Series(0.0, index=group.index),
                        ),
                        errors="coerce",
                    )
                    .fillna(0.0)
                    .sum()
                ),
                "worst_timing_p_success": float(group["p_success"].min()),
                "worst_timing_nonoverlap_p05_w24": float(
                    group["nonoverlap_p05_w24"].min()
                ),
                "worst_timing_median_w24": float(
                    group["nonoverlap_median_w24"].min()
                ),
                "worst_timing_max_drawdown": float(
                    group["nonoverlap_max_drawdown"].max()
                ),
                "worst_timing_rejected_share": float(
                    group["derivative_rejected_share"].max()
                ),
                "worst_timing_score": float(group["score"].min()),
            }
        )
    candidates = pd.DataFrame(rows)
    candidates = candidates.loc[
        candidates["passes_overlay_activity_gate"]
        & candidates["total_derivative_requested_contracts"].gt(0.0)
        & candidates["total_derivative_filled_contracts"].gt(0.0)
        & candidates["overall_derivative_active_window_share"].gt(0.0)
        & candidates["worst_timing_active_window_share"].gt(0.0)
    ].copy()
    if candidates.empty:
        candidates["promotion_reason"] = pd.Series(dtype="object")
        return candidates.reset_index(drop=True)
    candidates = candidates.sort_values(
        [
            "passes_overlay_development_gates",
            "worst_timing_p_success",
            "worst_timing_nonoverlap_p05_w24",
            "worst_timing_median_w24",
            "worst_timing_score",
            "overall_derivative_active_window_share",
            "worst_timing_active_window_share",
            "strategy",
        ],
        ascending=[False, False, False, False, False, False, False, True],
        kind="mergesort",
    )
    candidates = candidates.head(max_promotions).reset_index(drop=True)
    candidates["promotion_reason"] = np.where(
        candidates["passes_overlay_development_gates"],
        "development_gate",
        "bounded_exploratory_rank",
    )
    return candidates


__all__ = [
    "DERIVATIVE_COUNTER_COLUMNS",
    "DERIVATIVE_EXECUTION_TIER",
    "aggregate_overlay_windows",
    "evaluate_overlay_spec",
    "overlay_family",
    "overlay_lineage_key",
    "overlay_products",
    "select_inherited_overlay_specs",
    "select_overlay_candidates",
    "select_overlay_windows",
    "stage_resources",
]

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CrowdingGateSpec:
    metric: str
    lookback_days: int = 20
    lower_quantile: float = 0.2
    upper_quantile: float = 0.8
    expanding_min_periods: int = 60
    mode: str = "avoid_high"

    def __post_init__(self) -> None:
        if self.metric not in {"lagged_oi_change", "volume_oi", "oi_concentration"}:
            raise ValueError("unsupported crowding metric")
        if self.lookback_days <= 0 or self.expanding_min_periods <= 1:
            raise ValueError("lookbacks must be positive")
        if not 0 < self.lower_quantile < self.upper_quantile < 1:
            raise ValueError("invalid causal quantiles")
        if self.mode not in {"avoid_high", "require_high", "avoid_extremes"}:
            raise ValueError("unsupported crowding gate mode")


def build_causal_crowding_features(panel: pd.DataFrame, product: str) -> pd.DataFrame:
    required = {"trade_date", "product", "contract", "volume", "open_interest"}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"crowding panel missing columns: {sorted(missing)}")
    selected = panel.loc[panel["product"].astype(str).eq(product)].copy()
    selected["trade_date"] = selected["trade_date"].astype(str)
    selected["volume"] = pd.to_numeric(selected["volume"], errors="coerce")
    selected["open_interest"] = pd.to_numeric(selected["open_interest"], errors="coerce")
    selected = selected.loc[selected["volume"].ge(0) & selected["open_interest"].gt(0)]
    grouped = selected.groupby("trade_date", sort=True)
    features = grouped.agg(total_volume=("volume", "sum"), total_oi=("open_interest", "sum"), max_contract_oi=("open_interest", "max"))
    features["volume_oi"] = features["total_volume"] / features["total_oi"]
    features["oi_concentration"] = features["max_contract_oi"] / features["total_oi"]
    features.index.name = "signal_date"
    return features.reset_index()


def causal_crowding_gate(features: pd.DataFrame, spec: CrowdingGateSpec) -> pd.DataFrame:
    frame = features.sort_values("signal_date").reset_index(drop=True).copy()
    if spec.metric == "lagged_oi_change":
        value = frame["total_oi"] / frame["total_oi"].shift(spec.lookback_days) - 1.0
    else:
        value = frame[spec.metric].astype(float)
    history = value.shift(1).expanding(min_periods=spec.expanding_min_periods)
    lower = history.quantile(spec.lower_quantile)
    upper = history.quantile(spec.upper_quantile)
    if spec.mode == "avoid_high":
        allowed = value.le(upper)
    elif spec.mode == "require_high":
        allowed = value.ge(upper)
    else:
        allowed = value.between(lower, upper)
    frame["crowding_value"] = value
    frame["causal_lower"] = lower
    frame["causal_upper"] = upper
    frame["gate_allowed"] = allowed.fillna(False)
    frame["evidence_tier"] = "official_exchange_daily_signal_date"
    return frame


def apply_crowding_gate(base_directions: pd.Series, gate: pd.DataFrame) -> pd.Series:
    allowed = gate.set_index("signal_date")["gate_allowed"].astype(bool)
    aligned = allowed.reindex(base_directions.index.astype(str)).fillna(False).to_numpy()
    values = np.where(aligned, base_directions.astype(str).to_numpy(), "flat")
    return pd.Series(values, index=base_directions.index, name="direction")

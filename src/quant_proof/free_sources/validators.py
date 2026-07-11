from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DataTier(str, Enum):
    STRICT_REAL = "strict_real"
    FREE_REAL = "free_real"
    FREE_REAL_DERIVED_LIMITS = "free_real_derived_limits"
    PROXY_RESEARCH = "proxy_research"


@dataclass(frozen=True)
class StrategyAdmission:
    strategy: str
    data_tier: DataTier
    allowed: bool
    reason: str = ""


def strategy_allowed_in_tier(strategy: str, data_tier: str) -> StrategyAdmission:
    tier = DataTier(data_tier)
    family = str(strategy)
    if tier == DataTier.STRICT_REAL:
        return StrategyAdmission(strategy=family, data_tier=tier, allowed=True)
    if tier == DataTier.PROXY_RESEARCH:
        return StrategyAdmission(
            strategy=family,
            data_tier=tier,
            allowed=False,
            reason="proxy_research cannot enter real leaderboards",
        )
    if family.startswith("S31_real_post_limit_release") and tier != DataTier.FREE_REAL_DERIVED_LIMITS:
        return StrategyAdmission(
            strategy=family,
            data_tier=tier,
            allowed=False,
            reason="post-limit release requires the explicitly labeled derived-limit evidence tier",
        )
    if family.startswith(
        (
            "S2_real_stock_momentum",
            "S3_real_stock_breakout",
            "S4_real_smallcap_factor",
            "S11_real_short_term_reversal",
            "S12_real_low_volatility",
            "S13_real_residual_momentum",
            "S14_real_volume_price_shock",
            "S20_real_stateful_trend",
            "S21_real_volatility_contraction",
            "S22_real_concentrated_trend",
            "S23_real_concentrated_contraction",
            "S24_real_regime_contraction",
            "S26_real_gap_intraday",
            "S27_real_momentum_acceleration",
            "S28_real_signed_flow_accumulation",
            "S29_real_beta_residual_shock_reversal",
            "S30_real_idiosyncratic_strength",
            "S31_real_post_limit_release",
        )
    ):
        return StrategyAdmission(strategy=family, data_tier=tier, allowed=True)
    if family.startswith("S5"):
        return StrategyAdmission(
            strategy=family,
            data_tier=tier,
            allowed=False,
            reason="free_real lacks official queue/limit-order evidence for strict limit-up board strategies",
        )
    if "futures" in family or "options" in family:
        return StrategyAdmission(
            strategy=family,
            data_tier=tier,
            allowed=False,
            reason="free_real does not admit futures or options overlays",
        )
    return StrategyAdmission(strategy=family, data_tier=tier, allowed=False, reason="strategy is not whitelisted for free_real")


def field_source_disclaimer(data_tier: str, field: str, source: str) -> str:
    if data_tier in {
        DataTier.FREE_REAL.value,
        DataTier.FREE_REAL_DERIVED_LIMITS.value,
    } and source in {"derived", "baostock_tradestatus", "baostock_isST"}:
        return f"{field} uses {source}; treat as free_real proxy evidence, not strict official data"
    return ""

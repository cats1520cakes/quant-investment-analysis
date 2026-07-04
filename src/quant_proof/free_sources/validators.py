from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DataTier(str, Enum):
    STRICT_REAL = "strict_real"
    FREE_REAL = "free_real"
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
    if family.startswith(("S2_real_stock_momentum", "S3_real_stock_breakout", "S4_real_smallcap_factor")):
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
    if data_tier == DataTier.FREE_REAL.value and source in {"derived", "baostock_tradestatus", "baostock_isST"}:
        return f"{field} uses {source}; treat as free_real proxy evidence, not strict official data"
    return ""

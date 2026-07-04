from __future__ import annotations

import numpy as np
import pandas as pd


def derive_circ_mv_from_amount_turnover(amount: pd.Series, turnover_rate: pd.Series) -> pd.Series:
    amount_values = pd.to_numeric(amount, errors="coerce")
    turnover_values = pd.to_numeric(turnover_rate, errors="coerce")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = amount_values / (turnover_values / 100.0)
    out = out.where((amount_values > 0) & (turnover_values > 0))
    return out


def add_circ_mv_approx(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["circ_mv_approx"] = derive_circ_mv_from_amount_turnover(out["amount"], out["turnover_rate"])
    out["market_cap_source"] = np.where(out["circ_mv_approx"].notna(), "derived_from_amount_turnover", "unavailable")
    return out

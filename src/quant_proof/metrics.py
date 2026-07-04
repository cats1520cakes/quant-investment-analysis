from __future__ import annotations

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(-drawdown.min())


def ulcer_index(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    peak = equity.cummax()
    drawdown_pct = np.minimum(equity / peak - 1.0, 0.0) * 100.0
    return float(np.sqrt(np.mean(np.square(drawdown_pct))))


def expected_shortfall(values: pd.Series, level: float = 0.95) -> float:
    clean = values.dropna()
    if clean.empty:
        return float("nan")
    threshold = clean.quantile(1.0 - level)
    tail = clean[clean <= threshold]
    return float(tail.mean()) if not tail.empty else float(threshold)


def recovery_days(equity: pd.Series) -> int:
    if equity.empty:
        return 0
    peak = equity.cummax()
    under_water = equity < peak
    longest = current = 0
    for flag in under_water:
        if flag:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return int(longest)


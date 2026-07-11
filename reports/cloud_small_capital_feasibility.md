# Small-capital derivative feasibility

Scope: official 2026H1 daily opens, whole contracts, initial capital 0, monthly deposit 30,000, 15% cash buffer. Futures margin uses the official 2026-06-23 12% snapshot; therefore this is a bounded feasibility screen, not a full historical margin backtest.

| Product | Beginning infeasible | Ending infeasible | Median one-contract cash |
|---|---:|---:|---:|
| IF | 100.00% | 100.00% | 166,532 |
| IH | 60.34% | 81.90% | 104,252 |
| IC | 100.00% | 100.00% | 191,940 |
| IM | 100.00% | 100.00% | 188,964 |

At a 0.5% NAV long-option budget, the lower-bound infeasible shares are 96.55%/93.10%/98.28% for IO/HO/MO with beginning deposits and 98.28%/97.41%/98.28% with ending deposits. The screen already restricts absolute delta to 0.20–0.50, but does not yet impose DTE or exact delta selection; the real executable rate cannot be better than this lower bound.

The segment is six months, so no W12/W24, drawdown or candidate claim is permitted. Strict candidates: **0**.

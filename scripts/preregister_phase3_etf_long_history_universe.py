from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import pandas as pd
import yaml


PANEL = Path("artifacts/runtime_data/processed/phase3_etf/sse_u4_canonical.parquet")
OUT = Path("artifacts/derived/phase3_etf_long_history_preregister")
U4 = ["510050", "510300", "510500", "512100"]


def nonoverlap_blocks(first_signal: pd.Timestamp, end: pd.Timestamp) -> int:
    months = (end.year - first_signal.year) * 12 + end.month - first_signal.month + 1
    return months // 24


def main() -> None:
    cfg = yaml.safe_load(Path("config/phase3_etf_strict_new_families.yaml").read_text())
    panel_hash = hashlib.sha256(PANEL.read_bytes()).hexdigest()
    if panel_hash != cfg["panel_sha256"]:
        raise RuntimeError("frozen U4 panel hash mismatch")
    frame = pd.read_parquet(PANEL)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d")
    inception = {c: frame.loc[(frame.code == c) & frame.tradable, "trade_date"].min() for c in U4}
    rows = []
    for name, codes in (("U4", U4), ("U3", U4[:3]), ("U2", U4[:2])):
        common = max(inception[c] for c in codes)
        dates = sorted(frame.loc[frame.code.isin(codes) & (frame.trade_date >= common), "trade_date"].unique())
        # Signals begin only after 120 complete observations are available.
        first_signal = pd.Timestamp(dates[120])
        end = pd.Timestamp("2025-12-31")
        rows.append({
            "universe": name,
            "codes": ",".join(codes),
            "common_inception": common.date().isoformat(),
            "warmup_trading_days": 120,
            "first_signal_date": first_signal.date().isoformat(),
            "evaluation_end": end.date().isoformat(),
            "nonoverlap_w24_blocks": nonoverlap_blocks(first_signal, end),
            "five_block_gate": nonoverlap_blocks(first_signal, end) >= 5,
        })
    reach = pd.DataFrame(rows)

    domains = {
        "A": list(itertools.product([20, 60], [20, 60], [.4, .7, 1.], [0, .3], [5, 20])),
        "B": list(itertools.product([1, 2], [20, 60, 120], [20, 60, 120], ["cash", "510050"], [5, 20])),
        "C": list(itertools.product([.05, .10, .15], [5, 20], [3, 10], [.3, .6, 1.])),
        "D": list(itertools.product([.10, .15, .20], [.3, .6, 1.], ["inverse_volatility", "minimum_variance"])),
    }
    specs = []
    for universe, size in (("U3", 3), ("U2", 2)):
        for family, values in domains.items():
            for i, value in enumerate(values):
                if family == "B" and value[0] > size:
                    continue
                specs.append({"universe": universe, "family": family, "spec_id": f"{universe}-{family}{i:03d}", "parameters": json.dumps(value)})
    OUT.mkdir(parents=True, exist_ok=True)
    reach.to_csv(OUT / "universe_reachability.csv", index=False)
    pd.DataFrame(specs).to_csv(OUT / "legal_grid.csv", index=False)
    manifest = {
        "schema_version": 1,
        "preregistered": True,
        "selection_rule": "purely ex-ante: current official U4 members listed by common inception, tradable over the frozen panel, and passing the existing liquidity gate; remove only later inception; never select on returns",
        "priority": ["U3", "U2_if_U3_fails_five_blocks"],
        "inception": {k: v.date().isoformat() for k, v in inception.items()},
        "excluded_512100_reason": "later inception only; no return information used",
        "liquidity_gate": "existing strict prior-signal-day amount/capacity gate; must be rerun point-in-time before evaluation",
        "survivorship_bias": "current-only U4 parent universe; strict ranking is conditional on this frozen survivor-biased parent and is not an unbiased historical ETF universe",
        "panel_sha256": panel_hash,
        "target_w12": 500000,
        "target_w24": 1200000,
        "minimum_nonoverlap_w24_blocks": 5,
        "spec_count_by_universe": pd.DataFrame(specs).groupby("universe").size().to_dict(),
        "results_seen_before_freeze": False,
        "strict_candidates": 0,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(reach.to_string(index=False))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

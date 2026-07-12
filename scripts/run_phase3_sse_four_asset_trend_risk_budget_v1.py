from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from quant_proof.complete_overlay import SharedPortfolioLedger, capped_inverse_volatility


ROOT = Path("artifacts/derived/phase3_sse_four_asset_trend_risk_budget_v1")
GRID = ROOT / "grid.csv"
EP = Path("artifacts/runtime_data/processed/phase3_etf/multi_asset_sse_canonical.parquet")
FP = Path("artifacts/runtime_data/processed/phase3_derivatives/cffex_contract_daily_20240101_20251231_24m.parquet")
MP = Path("artifacts/runtime_data/processed/phase3_derivatives/cffex_trade_parameter_history_20240101_20251231_24m.parquet")
EVENTS = Path("artifacts/derived/phase3_multi_asset_official_actions_sse/event_ledger.csv")
CODES = ["510300", "510880", "518880", "511010"]


def atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        frame.to_csv(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def causal_signal_panel(etf: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Build a causal total-return signal series from official raw and effective events."""
    out = []
    cash = {(str(x.code), str(x.ex_date).replace("-", "")): float(x.cash_per_share) for x in events.itertuples() if pd.notna(x.cash_per_share)}
    factors = {(str(x.code), str(x.ex_date).replace("-", "")): float(x.share_factor) for x in events.itertuples() if pd.notna(x.share_factor)}
    for code, group in etf.sort_values("date").groupby("code"):
        level = 1.0
        prev = None
        vals = []
        for row in group.itertuples():
            ds = row.date.strftime("%Y%m%d")
            if prev is not None and pd.notna(row.close) and prev > 0:
                level *= ((float(row.close) * factors.get((str(code), ds), 1.0)) + cash.get((str(code), ds), 0.0)) / prev
            vals.append(level)
            if pd.notna(row.close):
                prev = float(row.close)
        x = group.copy()
        x["signal_price"] = vals
        out.append(x)
    return pd.concat(out, ignore_index=True)


def one(spec, timing, etf, futures, meta, events, capture_daily=True):
    p = json.loads(spec.parameters)
    ledger = SharedPortfolioLedger()
    daily = []
    nav = []
    previous_volume = {}
    current = None
    last_month = None
    first = ""
    attempts = feasible = rolls = identity = 0
    futures_pnl = 0.0
    margin_values, margin_ratios = [], []
    rejects = defaultdict(int)
    monthly = defaultdict(lambda: defaultdict(int))
    daily_futures = {d: x.set_index("contract") for d, x in futures.groupby("date")}
    daily_etf = {d: x.set_index("code") for d, x in etf.groupby("date")}
    signal = futures.groupby("date").settle.mean()
    trend = signal / signal.shift(p["trend_window"]) - 1
    event_by_record = defaultdict(list)
    event_by_effective = defaultdict(list)
    for event in events.itertuples(index=False):
        event_by_record[str(event.record_date).replace("-", "")].append(event)
        if pd.notna(event.share_factor) and float(event.share_factor) != 1:
            event_by_effective[str(event.ex_date).replace("-", "")].append(event)

    for d in sorted(set(daily_etf) & set(daily_futures)):
        ds, month, month_key = d.strftime("%Y%m%d"), d.strftime("%Y%m"), (d.year, d.month)
        bars, day = daily_etf[d], daily_futures[d]
        if timing == "beginning" and month_key != last_month:
            ledger.deposit(30000)
        ledger.pay_dividends(ds)
        for event in event_by_record.get(ds, []):
            ledger.register_dividend(event.event_id, str(event.code), ds, str(event.pay_date).replace("-", ""), float(event.cash_per_share) if pd.notna(event.cash_per_share) else 0.0, ds)
        for event in event_by_effective.get(ds, []):
            ledger.apply_share_factor(str(event.code), float(event.share_factor))

        if current and current in day.index:
            row = day.loc[current]
            futures_pnl += ledger.settle_future(float(row.settle), float(row.multiplier))
            remaining = sum(pd.Timestamp(x) > d for x in sorted(set(futures.date[futures.contract.eq(current)])))
            if remaining <= p["roll_lead_days"]:
                ledger.close_future(float(row.open), float(row.multiplier))
                current = None
                rolls += 1

        executables = day[day.open_executable].copy()
        candidates = executables.copy()
        candidates["pv"] = [previous_volume.get(x, 0) for x in candidates.index]
        candidates = candidates[candidates.pv > 0].sort_values(["contract_month", "pv"], ascending=[True, False])
        active = bool(pd.notna(trend.get(d)) and trend.get(d) > 0)
        reserve = 0.0
        if active and not ledger.futures_qty:
            attempts += 1
            if executables.empty:
                rejects["contract_unavailable"] += 1; monthly[month]["contract_unavailable"] += 1
            elif candidates.empty:
                rejects["prior_day_volume"] += 1; monthly[month]["prior_day_volume"] += 1
            else:
                row = candidates.iloc[0]
                reserve = float(row.open) * float(row.multiplier) * .20 * p["nav_margin_multiple"] + 8

        opens = {c: float(bars.loc[c, "open"]) for c in CODES}
        closes = {c: float(bars.loc[c, "close"]) for c in CODES}
        tradable = {c: bool(bars.loc[c, "tradable"]) and pd.notna(bars.loc[c, "open"]) for c in CODES}
        if month_key != last_month:
            weights = {}
            for code in CODES:
                hist = etf[(etf.code.eq(code)) & (etf.date < d)].sort_values("date").tail(120)
                if len(hist) >= 120 and hist.signal_price.iloc[-1] > hist.signal_price.mean():
                    returns = hist.signal_price.pct_change().tail(60)
                    vol = returns.std(ddof=1)
                    if pd.notna(vol) and vol > 0:
                        weights[code] = float(vol)
            targets = capped_inverse_volatility({c: weights.get(c, np.nan) for c in CODES}, .40)
            ledger.rebalance_target_weights(opens, tradable, targets, reserve)

        if active and not ledger.futures_qty and len(candidates):
            row = candidates.iloc[0]
            up = meta[(meta.snapshot_date.eq(ds)) & meta.contract.eq(row.name)].upper_limit_price
            lo = meta[(meta.snapshot_date.eq(ds)) & meta.contract.eq(row.name)].lower_limit_price
            blocked = (len(up) and float(row.open) >= float(up.iloc[0])) or (len(lo) and float(row.open) <= float(lo.iloc[0]))
            need = float(row.open) * float(row.multiplier) * .20
            equity = ledger.nav(closes)
            reason = None
            if blocked: reason = "limit_price"
            elif equity < need * p["nav_margin_multiple"] + 8: reason = "nav_multiple_gate"
            elif ledger.cash < need * p["nav_margin_multiple"] + 8: reason = "free_cash_insufficient"
            elif ledger.open_future(float(row.open), float(row.multiplier), .20, p["nav_margin_multiple"]):
                current = row.name; feasible += 1; first = first or d.strftime("%Y-%m")
            else: reason = "other"
            if reason:
                rejects[reason] += 1; monthly[month][reason] += 1

        value = ledger.nav(closes)
        nav.append((d, value))
        margin_values.append(ledger.margin)
        margin_ratios.append(ledger.margin / value if value > 0 else np.nan)
        residual = value - (ledger.cash + ledger.margin + sum(ledger.shares[c] * closes[c] for c in CODES))
        daily.append({"date": ds, "cash": ledger.cash, "margin": ledger.margin, "futures_qty": ledger.futures_qty,
                      "futures_contract": current or "", **{f"shares_{c}": ledger.shares[c] for c in CODES},
                      "etf_market_value": sum(ledger.shares[c] * closes[c] for c in CODES), "nav": value,
                      "fees_cumulative": ledger.fees, "futures_pnl_cumulative": futures_pnl, "asset_identity_residual": residual})
        try: ledger.assert_identity(closes)
        except AssertionError: identity += 1
        previous_volume.update(day.volume.fillna(0).astype(float).to_dict())
        if timing == "ending" and month_key != last_month:
            ledger.deposit(30000)
        last_month = month_key

    series = pd.Series(dict(nav))
    w12 = float(series.iloc[min(251, len(series) - 1)])
    w24 = float(series.iloc[-1])
    result = {"spec_id": spec.spec_id, "deposit_timing": timing, "first_feasible_month": first,
              "feasible_date_rate": feasible / max(attempts, 1), "W12": w12, "W24": w24,
              "futures_pnl": futures_pnl, "margin_peak": max(margin_values, default=0),
              "margin_mean": float(np.nanmean(margin_values)),
              "margin_to_nav_peak": float(np.nanmax(margin_ratios)) if any(pd.notna(x) for x in margin_ratios) else 0,
              **{f"reject_{k}": rejects[k] for k in ["free_cash_insufficient", "nav_multiple_gate", "prior_day_volume", "limit_price", "contract_unavailable", "expiry_roll_unavailable", "other"]},
              "monthly_reject_json": json.dumps(monthly, separators=(",", ":")), "margin_calls": ledger.margin_calls,
              "forced_liquidations": ledger.forced_liquidations, "rolls": rolls, "fees": ledger.fees,
              "max_drawdown": float((series / series.cummax() - 1).min()), "asset_identity_failures": identity,
              "etf_codes_traded": ",".join(c for c in CODES if ledger.shares[c] > 0),
              "dual_target_pass": w12 >= 500000 and w24 >= 1200000}
    return result, pd.DataFrame(daily)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", required=True, choices=["IH", "IF", "IC", "IM"])
    parser.add_argument("--spec-ids", nargs="*")
    args = parser.parse_args()
    family = f"sse_four_asset_trend_risk_budget_v1__{args.product}"
    grid = pd.read_csv(GRID)
    grid = grid[grid.family.eq(family)]
    if args.spec_ids: grid = grid[grid.spec_id.isin(args.spec_ids)]
    etf = pd.read_parquet(EP); etf["code"] = etf.code.astype(str); etf["date"] = pd.to_datetime(etf.trade_date.astype(str))
    events = pd.read_csv(EVENTS); events["code"] = events.code.astype(str)
    etf = causal_signal_panel(etf[etf.code.isin(CODES)].copy(), events)
    futures = pd.read_parquet(FP); futures = futures[futures.instrument_type.eq("future") & futures["product"].eq(args.product)].copy(); futures["date"] = pd.to_datetime(futures.trade_date)
    meta = pd.read_parquet(MP)
    out = ROOT / family; parts = out / "parts"; parts.mkdir(parents=True, exist_ok=True)
    for i, spec in enumerate(grid.itertuples(index=False), 1):
        part = parts / f"{spec.spec_id}.csv"
        if part.exists(): continue
        rows = []
        for timing in ("beginning", "ending"):
            row, ledger = one(spec, timing, etf, futures, meta, events)
            ledger_path = out / "daily_ledgers" / f"{spec.spec_id}_{timing}.parquet"
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = ledger_path.with_suffix(".tmp"); ledger.to_parquet(tmp, index=False, compression="zstd"); os.replace(tmp, ledger_path)
            row["daily_ledger_sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest(); row["daily_ledger_rows"] = len(ledger)
            rows.append(row)
        atomic_csv(pd.DataFrame(rows), part)
        atomic_csv(pd.DataFrame([{"spec_id": x.stem, "status": "complete"} for x in sorted(parts.glob("*.csv"))]), out / "attempt_ledger.csv")
        if i % 6 == 0: print(f"{family} {i}/{len(grid)}", flush=True)
    results = pd.concat([pd.read_csv(x) for x in sorted(parts.glob("*.csv"))])
    atomic_csv(results, out / "results.csv")
    worst = results.groupby("spec_id").agg(W12=("W12", "min"), W24=("W24", "min"), margin_peak=("margin_peak", "max"), asset_identity_failures=("asset_identity_failures", "sum")).reset_index()
    atomic_csv(worst.sort_values("W24", ascending=False), out / "pareto.csv")
    manifest = {"schema_version": 4, "family": family, "specifications": len(grid), "completed": int(results.spec_id.nunique()),
                "timing_rows": len(results), "grid_sha256": hashlib.sha256(GRID.read_bytes()).hexdigest(),
                "etf_panel_sha256": hashlib.sha256(EP.read_bytes()).hexdigest(), "daily_ledger": True,
                "economic_dual_target_specs": int(worst.eval("W12>=500000 and W24>=1200000 and asset_identity_failures==0").sum()),
                "evidence_tier": "official_exchange_etf_canonical_plus_free_real_approx_conservative_futures_margin",
                "strict_blockers": ["official_point_in_time_daily_margin"], "strict_candidates": 0}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()

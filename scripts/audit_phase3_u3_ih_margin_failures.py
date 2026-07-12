"""Reproduce the first negative-cash event in the frozen U3 x IH v3 run.

This is intentionally read-only with respect to the frozen result parts.  It
classifies an exact accounting residual separately from an unfunded variation
margin balance; the latter must not be reported as an identity equation error.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

from quant_proof.complete_overlay import SharedPortfolioLedger

ROOT = Path("artifacts/derived/phase3_complete_futures_overlay_v3/u3_equal_weight__IH")
OUT = ROOT / "margin_failure_forensics.json"


class TraceLedger(SharedPortfolioLedger):
    failures: list[dict] = []
    last_settlement: dict | None = None

    def settle_future(self, settle, multiplier, maintenance_rate=.75):
        if not self.futures_qty:
            return 0.0
        before = {
            "cash_before": self.cash,
            "margin_before": self.margin,
            "last_settle": self.futures_last_settle,
            "settle": settle,
            "multiplier": multiplier,
        }
        # Reproduce the frozen v3 predicate exactly, even after the live
        # implementation is corrected.
        out = (settle - self.futures_last_settle) * multiplier
        self.cash += out
        self.futures_last_settle = settle
        if self.cash + self.margin < self.margin * maintenance_rate:
            self.margin_calls += 1
            self.close_future(settle, multiplier, forced=True)
        before.update(
            mtm=out,
            cash_after=self.cash,
            margin_after=self.margin,
            margin_calls_after=self.margin_calls,
            forced_after=self.forced_liquidations,
        )
        self.last_settlement = before
        return out

    def assert_identity(self, closes):
        holdings = sum(self.shares[c] * closes[c] for c in closes)
        nav = self.nav(closes)
        residual = nav - (self.cash + self.margin + holdings)
        if self.cash < -1e-8 or abs(residual) > 1e-7:
            self.failures.append(
                {
                    "cash": self.cash,
                    "margin": self.margin,
                    "holdings_market_value": holdings,
                    "nav": nav,
                    "equation_residual": residual,
                    "last_settlement": self.last_settlement,
                }
            )
            raise AssertionError("portfolio identity/funding gate failed")


def main() -> None:
    result = pd.concat(pd.read_csv(p) for p in sorted((ROOT / "parts").glob("*.csv")))
    failed = result.loc[result.asset_identity_failures.gt(0)]
    failed_specs = sorted(failed.spec_id.unique())

    spec = importlib.util.spec_from_file_location("overlay_v3", "scripts/run_phase3_complete_futures_overlay_v3.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    module.SharedPortfolioLedger = TraceLedger

    grid = pd.read_csv(module.GRID)
    etf = pd.read_parquet(module.EP)
    etf = etf[etf.code.astype(str).isin(module.CODES)].copy()
    etf["date"] = pd.to_datetime(etf.trade_date)
    futures = pd.read_parquet(module.FP)
    futures = futures[futures.instrument_type.eq("future") & futures["product"].eq("IH")].copy()
    futures["date"] = pd.to_datetime(futures.trade_date)
    metadata = pd.read_parquet(module.MP)
    events = pd.read_csv(module.DIV)
    events = events[events.code.astype(str).isin(module.CODES)]

    TraceLedger.failures = []
    row = next(grid[grid.spec_id.eq(failed_specs[0])].itertuples(index=False))
    module.one(row, "beginning", etf, futures, metadata, events)
    first = TraceLedger.failures[0]
    closes = etf.pivot(index="trade_date", columns="code", values="close")
    match = closes.index[
        (closes["510050"].eq(2.716))
        & (closes["510300"].eq(3.979))
        & (closes["510500"].eq(5.893))
    ]
    payload = {
        "schema_version": 1,
        "frozen_failed_specs": failed_specs,
        "failed_spec_count": len(failed_specs),
        "failed_timing_rows": int(len(failed)),
        "all_failed_timings": sorted(failed.deposit_timing.unique()),
        "representative_spec": failed_specs[0],
        "first_failure_date": str(match[0]),
        "first_failure": first,
        "classification": "margin_call_trigger_bug_negative_variation_cash_not_caught",
        "accounting_equation_exact": abs(first["equation_residual"]) <= 1e-7,
        "required_action": "retain frozen v3; fix negative-cash margin trigger; rerun exactly the 20 listed specs",
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()

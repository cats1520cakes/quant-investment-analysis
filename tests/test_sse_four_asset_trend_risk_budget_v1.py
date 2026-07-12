import importlib.util
from pathlib import Path

import pandas as pd

from quant_proof.complete_overlay import SharedPortfolioLedger, capped_inverse_volatility


SCRIPT = Path("scripts/run_phase3_sse_four_asset_trend_risk_budget_v1.py")
SPEC = importlib.util.spec_from_file_location("sse_four_asset_runner", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_causal_signal_adjusts_only_on_effective_date():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    panel = pd.DataFrame({"code": ["510300"] * 3, "date": dates, "close": [10.0, 9.0, 9.9]})
    events = pd.DataFrame([{"code": "510300", "ex_date": "2024-01-03", "cash_per_share": 1.0, "share_factor": 1.0}])
    got = MODULE.causal_signal_panel(panel, events)
    assert got.signal_price.iloc[0] == 1.0
    assert got.signal_price.iloc[1] == 1.0
    assert abs(got.signal_price.iloc[2] - 1.1) < 1e-12


def test_four_asset_capped_inverse_volatility_and_cash_residual():
    weights = capped_inverse_volatility({"510300": .1, "510880": .2, "518880": .4, "511010": float("nan")}, .4)
    assert max(weights.values()) <= .4 and sum(weights.values()) == 1.0
    ledger = SharedPortfolioLedger(100000)
    ledger.rebalance_target_weights({c: 10.0 for c in weights}, {c: True for c in weights}, weights)
    ledger.assert_identity({c: 10.0 for c in weights})


def test_suspended_asset_is_retained_and_target_not_redistributed():
    ledger = SharedPortfolioLedger(100000)
    ledger.shares["510880"] = 1000
    before = ledger.shares["510880"]
    ledger.rebalance_target_weights(
        {"510300": 10.0, "510880": 10.0, "518880": 10.0, "511010": 10.0},
        {"510300": True, "510880": False, "518880": True, "511010": True},
        {"510300": .2, "510880": .4, "518880": .2, "511010": .2},
    )
    assert ledger.shares["510880"] == before


def test_shared_cash_cannot_fund_etf_and_margin_twice():
    ledger = SharedPortfolioLedger(30000)
    weights = {c: .25 for c in MODULE.CODES}
    ledger.rebalance_target_weights({c: 10.0 for c in MODULE.CODES}, {c: True for c in MODULE.CODES}, weights)
    assert not ledger.open_future(3000, 300, .20, 1.25)

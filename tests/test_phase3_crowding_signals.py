import pandas as pd

from quant_proof.phase3_crowding_signals import CrowdingGateSpec, build_causal_crowding_features, causal_crowding_gate


def _panel(days: int = 100) -> pd.DataFrame:
    rows = []
    for day in range(days):
        date = f"2024{day // 28 + 1:02d}{day % 28 + 1:02d}"
        rows.extend([
            {"trade_date": date, "product": "IF", "contract": "IF1", "volume": 100 + day, "open_interest": 1000 + 10 * day},
            {"trade_date": date, "product": "IF", "contract": "IF2", "volume": 50, "open_interest": 500},
        ])
    return pd.DataFrame(rows)


def test_crowding_features_have_exact_daily_definitions() -> None:
    features = build_causal_crowding_features(_panel(2), "IF")
    assert features.loc[0, "total_oi"] == 1500
    assert features.loc[0, "volume_oi"] == 150 / 1500
    assert features.loc[0, "oi_concentration"] == 1000 / 1500


def test_gate_thresholds_do_not_use_current_or_future_observation() -> None:
    features = build_causal_crowding_features(_panel(), "IF")
    spec = CrowdingGateSpec("volume_oi", expanding_min_periods=20)
    original = causal_crowding_gate(features, spec)
    mutated = features.copy()
    mutated.loc[mutated.index[-1], "volume_oi"] = 999.0
    changed = causal_crowding_gate(mutated, spec)
    assert original.loc[original.index[-1], "causal_upper"] == changed.loc[changed.index[-1], "causal_upper"]


def test_lagged_oi_change_has_no_warmup_execution() -> None:
    features = build_causal_crowding_features(_panel(), "IF")
    gate = causal_crowding_gate(features, CrowdingGateSpec("lagged_oi_change", lookback_days=20, expanding_min_periods=20))
    assert not gate.iloc[:40]["gate_allowed"].any()
    assert set(gate["evidence_tier"]) == {"official_exchange_daily_signal_date"}

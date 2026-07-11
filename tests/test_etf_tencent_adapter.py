from quant_proof.free_sources.etf_tencent_adapter import parse_tencent_day


def test_tencent_raw_and_hfq_have_separate_permissions() -> None:
    raw = {"data": {"sz159915": {"day": [["2026-01-01", "1", "2", "3", "0.5", "100"]]}}}
    hfq = {"data": {"sz159915": {"hfqday": [["2026-01-01", "2", "4", "6", "1", "100"]]}}}
    raw_frame = parse_tencent_day(raw, "159915", "raw")
    hfq_frame = parse_tencent_day(hfq, "159915", "hfq")
    assert bool(raw_frame.execution_allowed.iloc[0]) and not bool(raw_frame.signal_allowed.iloc[0])
    assert bool(hfq_frame.signal_allowed.iloc[0]) and not bool(hfq_frame.execution_allowed.iloc[0])
    assert raw_frame.volume_unit.iloc[0] == "vendor_quantity_units_undocumented"

from pathlib import Path

from scripts.audit_phase3_etf_strict_lineage import build


def test_audit_keeps_frozen_u3_separate_from_runtime_rerun() -> None:
    manifest, rows = build(Path("."))
    assert manifest["u3"]["frozen_specs"] == 174
    assert manifest["u3"]["nonoverlap_w24_blocks"] == 6
    assert manifest["u3"]["frozen_result_status"] == "completed_auditable_elimination"
    assert not manifest["u3"]["current_runtime_rerun_permitted"]
    assert {r["code"] for r in rows if r["universe"] == "U3"} == {"510050", "510300", "510500"}


def test_audit_fails_closed_for_incomplete_159915_official_history() -> None:
    manifest, rows = build(Path("."))
    assert manifest["high_elasticity"]["159915_official_announcement_records"] == 460
    assert manifest["high_elasticity"]["159915_announcement_terminal_page"]
    assert not manifest["high_elasticity"]["official_history_complete"]
    assert not manifest["high_elasticity"]["strict_run_permitted"]
    row = next(r for r in rows if r["code"] == "159915")
    assert row["second_source"].endswith("crosscheck_only")

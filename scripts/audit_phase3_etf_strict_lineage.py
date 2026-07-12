from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


U3 = ("510050", "510300", "510500")
E = ("159915", "510500", "510300")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def build(repo: Path) -> tuple[dict, list[dict]]:
    runtime = load(repo / "artifacts/runtime_data/processed/phase3_etf/sse_u4_canonical.parquet.manifest.json")
    frozen = load(repo / "artifacts/derived/manifests/etf_u4_official_recovery_20260712.json")
    u3_actions = load(repo / "artifacts/derived/phase3_etf_corporate_actions_u3_history/manifest.json")
    u4_actions = load(repo / "artifacts/derived/phase3_etf_corporate_actions_u4/manifest.json")
    sz_actions = load(repo / "artifacts/derived/phase3_etf_corporate_actions_159915/manifest.json")
    sz_gap = load(repo / "artifacts/derived/phase3_etf_high_elasticity_preregister/source_gap_manifest.json")
    comparison = load(repo / "artifacts/derived/phase3_etf_u3_family_comparison/manifest.json")

    frozen_hash = frozen["panel_sha256"]
    runtime_hash = runtime["panel_sha256"]
    u3_bound = {
        load(repo / f)["panel_sha256"]
        for f in (
            "artifacts/derived/phase3_etf_u3_family_a_history/manifest.json",
            "artifacts/derived/phase3_etf_u3_rotation_history/manifest.json",
            "artifacts/derived/phase3_etf_u3_family_c_history/manifest.json",
            "artifacts/derived/phase3_etf_u3_family_d_history/manifest.json",
        )
    }
    rows = []
    for code in U3:
        src = next(x for x in runtime["source_files"] if x["code"] == code)
        rows.append({
            "universe": "U3", "code": code, "official_ohlcv": "complete",
            "start": src["first_date"], "end": src["last_date"], "rows": src["rows"],
            "calendar_suspension_gate": "complete_for_frozen_evaluation",
            "units": "shares;CNY", "company_actions": "complete_2013-03-15_to_2025-12-31",
            "second_source": "Tencent_crosscheck_only", "strict_rerun": "blocked_runtime_panel_hash",
        })
    rows.append({
        "universe": "E1/E2/E3", "code": "159915", "official_ohlcv": "incomplete",
        "start": "2011-12-09", "end": "2025-12-31", "rows": sz_gap["official_history_attempt"]["validated_cached_responses"],
        "calendar_suspension_gate": "blocked_until_official_history_complete",
        "units": "official_labels_identified_not_full_series_validated",
        "company_actions": "complete_index_and_bodies",
        "second_source": "Tencent_3539_rows_crosscheck_only", "strict_rerun": "blocked_official_ohlcv",
    })
    manifest = {
        "schema_version": 1,
        "scope": "ETF strict lineage and frozen-family readiness",
        "u3": {
            "official_sse_source_rows": {x["code"]: x["rows"] for x in runtime["source_files"] if x["code"] in U3},
            "official_source_date_end": runtime["last_date"],
            "confirmed_suspension_rows_u4": frozen["confirmed_suspension_rows"],
            "volume_unit": runtime["volume_unit"], "amount_unit": runtime["amount_unit"],
            "company_action_index_records_2013_2023": u3_actions["index_records"],
            "company_action_events": u3_actions["stable_events"],
            "cash_dividends": u3_actions["cash_dividends"],
            "share_factor_events": u3_actions["share_factor_events"],
            "company_action_events_2024_2025": u4_actions["unique_economic_events"],
            "company_action_candidates_2024_2025": u4_actions["candidate_count"],
            "company_action_gate": u3_actions["coverage_complete"] and u3_actions["body_gate_passed"],
            "frozen_panel_sha256": frozen_hash,
            "frozen_results_panel_hashes": sorted(u3_bound),
            "current_runtime_panel_sha256": runtime_hash,
            "current_runtime_rerun_permitted": runtime_hash == frozen_hash and u3_bound == {frozen_hash},
            "frozen_result_status": "completed_auditable_elimination",
            "frozen_specs": comparison["total_specifications"],
            "monthly_cohorts_per_spec": comparison["monthly_cohorts_per_spec"],
            "nonoverlap_w24_blocks": comparison["nonoverlap_w24_blocks"],
            "strict_candidates": comparison["strict_candidates"],
        },
        "high_elasticity": {
            "159915_official_announcement_records": sz_actions["announce_count"],
            "159915_announcement_pages": sz_actions["pages"],
            "159915_announcement_terminal_page": sz_actions["terminal_page_reached"],
            "159915_bodies_read": sz_actions["bodies_read"],
            "159915_stable_events": sz_actions["stable_events"],
            "159915_unresolved_candidates": sz_actions["unresolved_candidates"],
            "159915_official_history_validated_responses": sz_gap["official_history_attempt"]["validated_cached_responses"],
            "official_history_complete": sz_gap["official_history_attempt"]["coverage_proven"],
            "strict_run_permitted": False,
            "blocked_universes": ["E1", "E2", "E3"],
            "minimum_evidence": sz_gap["minimum_strict_evidence_set"],
            "strict_candidates": 0,
        },
        "raw_committed": False,
        "strict_candidates": 0,
    }
    return manifest, rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--output", type=Path, default=Path("artifacts/derived/phase3_etf_strict_lineage_audit"))
    args = ap.parse_args()
    manifest, rows = build(args.repo)
    out = args.repo / args.output
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    with (out / "coverage_matrix.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    manifest["coverage_matrix_sha256"] = sha256(out / "coverage_matrix.csv")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

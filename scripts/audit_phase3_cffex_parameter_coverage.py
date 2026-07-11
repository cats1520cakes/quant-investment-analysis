from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from quant_proof.free_sources.cffex_trade_parameters import (
    TRADE_PARAMETER_COLUMNS,
    parse_cffex_trade_parameters_csv,
)

STATIC_FIELDS = ("contract_month", "open_date", "official_last_trade_date")
DAILY_FIELDS = (
    "upper_limit_percentage", "lower_limit_percentage", "upper_limit_price",
    "lower_limit_price", "position_limit_raw",
)
RULE_FIELDS = ("initial_margin_rate", "maintenance_margin_rate", "multiplier", "minimum_price_tick")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="artifacts/runtime_data")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    root, output = Path(args.data_root), Path(args.output_root)
    start, end = args.start_date.replace("-", ""), args.end_date.replace("-", "")
    rows: list[dict[str, object]] = []
    source_hashes: list[str] = []
    for path in sorted((root / "raw/cffex/trade_parameters").glob("*/*/*_1.csv")):
        date = path.stem.split("_")[0]
        if not start <= date <= end:
            continue
        frame = parse_cffex_trade_parameters_csv(path.read_bytes(), date, source_file=path.name, source_url="official_manifest_bound")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        source_hashes.append(digest)
        for product, group in frame.groupby("product", observed=True):
            item: dict[str, object] = {"snapshot_date": date, "product": str(product), "contracts": int(group.contract.nunique()), "source_sha256": digest, "evidence_tier": "official_cffex_trade_parameter_snapshot", "panel_bound": False}
            for field in (*STATIC_FIELDS, *DAILY_FIELDS):
                item[f"{field}_coverage"] = float(group[field].notna().mean())
            for field in RULE_FIELDS:
                item[f"{field}_coverage"] = 0.0
            rows.append(item)
    matrix = pd.DataFrame(rows).sort_values(["snapshot_date", "product"])
    output.mkdir(parents=True, exist_ok=True)
    matrix.to_csv(output / "coverage_matrix.csv", index=False)
    manifest = {"schema_version": 1, "start_date": start, "end_date": end, "valid_snapshot_dates": int(matrix.snapshot_date.nunique()), "products": sorted(matrix["product"].unique().tolist()), "rows": len(matrix), "panel_bound": False, "execution_gate_passed": False, "strict_candidates": 0, "source_set_sha256": hashlib.sha256("".join(sorted(source_hashes)).encode()).hexdigest(), "static_fields": list(STATIC_FIELDS), "daily_fields": list(DAILY_FIELDS), "official_rule_fields_pending": list(RULE_FIELDS), "schema_columns_checked": list(TRADE_PARAMETER_COLUMNS)}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()

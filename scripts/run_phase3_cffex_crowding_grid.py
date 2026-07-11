from __future__ import annotations

import argparse
import hashlib
import json
from itertools import product
from pathlib import Path

import pandas as pd
import yaml

from quant_proof.cffex_catalog import CffexCatalog
from quant_proof.phase3_crowding_signals import CrowdingGateSpec, apply_crowding_gate, build_causal_crowding_features, causal_crowding_gate
from quant_proof.phase3_derivative_signals import FuturesDirectionRule, FuturesDirectionSignalError, build_futures_direction_map


def main() -> None:
    parser = argparse.ArgumentParser(description="Build causal CFFEX OI/volume crowding gates")
    parser.add_argument("--panel", default="artifacts/runtime_data/processed/phase3_derivatives/cffex_contract_daily.parquet")
    parser.add_argument("--config", default="config/phase3_cffex_crowding_grid.yaml")
    parser.add_argument("--output", default="artifacts/derived/phase3_cffex_crowding")
    parser.add_argument("--master", default="")
    parser.add_argument("--trade-parameter-metadata", default="")
    args = parser.parse_args()
    panel_path, config_path = Path(args.panel), Path(args.config)
    panel = pd.read_parquet(panel_path, columns=["trade_date", "product", "contract", "volume", "open_interest"])
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    rows, maps, combined_maps, integration_errors = [], [], [], []
    catalog = CffexCatalog(panel_path, args.master, trade_parameter_metadata_path=(args.trade_parameter_metadata or None)) if args.master else None
    gates = config["crowding_gates"]
    for product_name, metric, lookback, mode, quantiles in product(
        config["products"], gates["metrics"], gates["lookback_days"], gates["modes"], gates["quantile_pairs"]
    ):
        features = build_causal_crowding_features(panel, product_name)
        if features.empty:
            gate_id = f"{product_name}_{metric}_lb{lookback}_{mode}_q{quantiles[0]}_{quantiles[1]}"
            rows.append({"gate_id": gate_id, "product": product_name, "metric": metric, "lookback": lookback, "mode": mode, "observations": 0, "active_rate": 0.0, "first_active_date": "", "status": "product_absent"})
            continue
        spec = CrowdingGateSpec(metric, int(lookback), float(quantiles[0]), float(quantiles[1]), int(gates["expanding_min_periods"]), mode)
        resolved = causal_crowding_gate(features, spec)
        gate_id = f"{product_name}_{metric}_lb{lookback}_{mode}_q{quantiles[0]}_{quantiles[1]}"
        resolved.insert(0, "gate_id", gate_id)
        resolved.insert(1, "product", product_name)
        maps.append(resolved[["gate_id", "product", "signal_date", "crowding_value", "causal_lower", "causal_upper", "gate_allowed", "evidence_tier"]])
        rows.append({"gate_id": gate_id, "product": product_name, "metric": metric, "lookback": lookback, "mode": mode, "observations": len(resolved), "active_rate": float(resolved.gate_allowed.astype(bool).mean()), "first_active_date": next(iter(resolved.loc[resolved.gate_allowed.astype(bool), "signal_date"]), ""), "status": "resolved"})
        if catalog is not None:
            for base_index, base_raw in enumerate(config["base_rules"]):
                base_rule = FuturesDirectionRule(**base_raw)
                try:
                    base_map = build_futures_direction_map(catalog, product_name, base_rule)
                except FuturesDirectionSignalError as exc:
                    integration_errors.append({"product": product_name, "gate_id": gate_id, "base_index": base_index, "error": str(exc)})
                    continue
                base_series = pd.Series(base_map, name="base_direction")
                combined = apply_crowding_gate(base_series, resolved)
                combined_maps.append(pd.DataFrame({"combined_id": f"base{base_index}_{gate_id}", "product": product_name, "signal_date": combined.index.astype(str), "base_direction": base_series.astype(str).to_numpy(), "direction": combined.to_numpy(), "gate_id": gate_id}))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output / "gate_registry.csv", index=False)
    if maps:
        pd.concat(maps, ignore_index=True).to_parquet(output / "causal_gate_maps.parquet", index=False)
    if combined_maps:
        pd.concat(combined_maps, ignore_index=True).to_parquet(output / "gated_direction_maps.parquet", index=False)
    manifest = {"schema_version": 1, "panel_sha256": hashlib.sha256(panel_path.read_bytes()).hexdigest(), "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(), "gate_specs": len(rows), "gated_direction_specs": len(combined_maps), "integration_errors": integration_errors, "strict_candidates": 0, "execution_lag": "signal close to next open", "evidence_tier": config["evidence_tier"]}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"[crowding-grid] gate_specs={len(rows)} maps={sum(len(frame) for frame in maps)} strict_candidates=0")


if __name__ == "__main__":
    main()

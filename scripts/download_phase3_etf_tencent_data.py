from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
import yaml

from quant_proof.free_sources.etf_sse_adapter import file_sha256
from quant_proof.free_sources.etf_tencent_adapter import TENCENT_FQ_URL, TENCENT_RAW_URL, TencentEtfDataError, download_tencent_day, parse_tencent_day


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/phase3_etf_data.yaml")
    parser.add_argument("--data-root", default="")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--backoff-seconds", type=float, default=5.0)
    parser.add_argument("--max-consecutive-failures", type=int, default=2)
    args = parser.parse_args()
    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = Path(args.data_root or config["data_root"])
    codes = [str(code) for code in config["universe"]["sse_official_u4"] + config["universe"]["szse_research_u2"]]
    raw_root = root / "raw" / "tencent_etf"
    frames, records, failures = [], [], []
    consecutive_failures = 0
    attempt_path = root / "00_meta" / "manifests" / "tencent_u6_attempts.json"
    attempt_path.parent.mkdir(parents=True, exist_ok=True)
    attempt_log = json.loads(attempt_path.read_text(encoding="utf-8")) if attempt_path.exists() else {}
    for code in codes:
        for adjustment in ("raw", "hfq"):
            key = f"{code}_{adjustment}"
            try:
                path = download_tencent_day(code, adjustment, raw_root / f"{key}.json", timeout=args.timeout, attempts=args.attempts, backoff_seconds=args.backoff_seconds)
            except TencentEtfDataError as exc:
                failures.append(key)
                consecutive_failures += 1
                attempt_log[key] = {"status": "failed", "error": str(exc), "attempts": args.attempts}
                print(f"[tencent-etf] code={code} adjustment={adjustment} status=failed", flush=True)
                if consecutive_failures >= args.max_consecutive_failures:
                    break
                continue
            consecutive_failures = 0
            frame = parse_tencent_day(json.loads(path.read_text(encoding="utf-8")), code, adjustment)
            frames.append(frame)
            market = "sh" if code.startswith("5") else "sz"
            url = (TENCENT_RAW_URL if adjustment == "raw" else TENCENT_FQ_URL).format(market=market, code=code, end="", total=2000, adjustment=adjustment)
            records.append({"code": code, "adjustment": adjustment, "rows": len(frame), "first_date": frame.trade_date.min(), "last_date": frame.trade_date.max(), "sha256": file_sha256(path), "source_url": url})
            attempt_log[key] = {"status": "cached_or_downloaded_valid", "rows": len(frame), "sha256": file_sha256(path)}
            print(f"[tencent-etf] code={code} adjustment={adjustment} rows={len(frame)}", flush=True)
        if consecutive_failures >= args.max_consecutive_failures:
            break
    temporary_attempt = attempt_path.with_suffix(".json.tmp")
    temporary_attempt.write_text(json.dumps(attempt_log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_attempt.replace(attempt_path)
    if len(records) != len(codes) * 2:
        raise SystemExit(f"Tencent U6 incomplete: valid={len(records)}/{len(codes) * 2} failures={len(failures)}; panel not promoted")
    panel = pd.concat(frames, ignore_index=True).sort_values(["code", "adjustment", "trade_date"])
    output = root / "processed" / "phase3_etf" / "tencent_u6_layered.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(output, index=False)
    manifest = {
        "schema_version": 1, "panel_sha256": file_sha256(output), "config_hash": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "rows": len(panel), "source_files": records, "data_tier": "free_vendor_crosscheck",
        "raw_usage": "execution research only; U2 never strict_real", "hfq_usage": "signals only; never wealth accounting",
        "volume_unit": "vendor_quantity_units_undocumented", "amount_available": False,
        "survivorship_bias": "current-only ETF master", "corporate_action_ledger": "incomplete_blocking_gate",
    }
    output.with_suffix(output.suffix + ".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pd.DataFrame(records).to_csv(root / "00_meta" / "manifests" / "tencent_u6_sources.csv", index=False)
    print(f"[tencent-etf] panel={output} rows={len(panel)}")


if __name__ == "__main__":
    main()

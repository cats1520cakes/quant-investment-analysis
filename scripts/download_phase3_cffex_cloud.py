from __future__ import annotations

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from quant_proof.free_sources.cffex_adapter import (
    build_cffex_contract_master,
    build_cffex_contract_panel,
    cffex_month_url,
    cffex_months,
    validate_cffex_month_zip,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable cloud CFFEX official archive acquisition")
    parser.add_argument("--config", default="config/phase3_cffex_data.yaml")
    parser.add_argument("--data-root", default="artifacts/runtime_data")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-months", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--backoff-seconds", type=float, default=5.0)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    start = args.start_date or str(config["date_range"]["start_date"])
    end = args.end_date or str(config["date_range"]["end_date"])
    months = cffex_months(start, end)
    if args.max_months:
        months = months[: args.max_months]
    root = Path(args.data_root)
    archive_root = root / "raw" / "cffex" / "monthly"
    archive_root.mkdir(parents=True, exist_ok=True)
    attempt_path = root / "00_meta" / "manifests" / "cffex_cloud_attempts.json"
    attempt_path.parent.mkdir(parents=True, exist_ok=True)
    attempts_log = json.loads(attempt_path.read_text(encoding="utf-8")) if attempt_path.exists() else {}

    def acquire(month: str) -> tuple[str, Path, str, dict[str, object]]:
        path = archive_root / f"{month}.zip"
        try:
            summary = validate_cffex_month_zip(path, month)
            status = "cached_valid"
        except Exception:
            temporary = path.with_suffix(".zip.tmp")
            error = ""
            for attempt in range(1, max(1, args.attempts) + 1):
                try:
                    subprocess.run(["curl", "-fsSL", "--max-time", str(args.timeout), "-o", str(temporary), cffex_month_url(month)], check=True)
                    summary = validate_cffex_month_zip(temporary, month)
                    temporary.replace(path)
                    status = "downloaded"
                    break
                except Exception as exc:
                    error = str(exc)
                    if attempt < args.attempts:
                        time.sleep(args.backoff_seconds * (2 ** (attempt - 1)))
            else:
                raise RuntimeError(f"CFFEX month {month} exhausted retries: {error}")
        return month, path, status, summary

    completed_paths: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(acquire, month): month for month in months}
        failures = []
        consecutive_failures = 0
        for index, future in enumerate(as_completed(futures), 1):
            try:
                month, path, status, summary = future.result()
            except Exception as exc:
                month = futures[future]
                failures.append(month)
                consecutive_failures += 1
                attempts_log[month] = {"status": "failed", "error": str(exc), "attempts": args.attempts}
                print(f"[cffex-cloud] {index}/{len(months)} month={month} status=failed", flush=True)
                if consecutive_failures >= args.max_consecutive_failures:
                    for pending in futures:
                        pending.cancel()
                    break
                continue
            completed_paths[month] = path
            consecutive_failures = 0
            attempts_log[month] = {"status": status, "rows": int(summary["rows"])}
            print(f"[cffex-cloud] {index}/{len(months)} month={month} status={status} rows={summary['rows']}", flush=True)
    temporary_attempt = attempt_path.with_suffix(".json.tmp")
    temporary_attempt.write_text(json.dumps(attempts_log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_attempt.replace(attempt_path)
    if failures:
        raise SystemExit(f"CFFEX acquisition incomplete: valid={len(completed_paths)}/{len(months)} failed={len(failures)}")
    paths = [completed_paths[month] for month in months]
    panel = build_cffex_contract_panel(paths, root / "processed" / "phase3_derivatives" / "cffex_contract_daily.parquet")
    master = build_cffex_contract_master(panel, root / "processed" / "phase3_derivatives" / "cffex_contract_master.parquet")
    print(f"[cffex-cloud] panel={panel} master={master}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import subprocess
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
    def acquire(month: str) -> tuple[str, Path, str, dict[str, object]]:
        path = archive_root / f"{month}.zip"
        try:
            summary = validate_cffex_month_zip(path, month)
            status = "cached_valid"
        except Exception:
            temporary = path.with_suffix(".zip.tmp")
            subprocess.run(["curl", "-fsSL", "--retry", "3", "--max-time", "60", "-o", str(temporary), cffex_month_url(month)], check=True)
            summary = validate_cffex_month_zip(temporary, month)
            temporary.replace(path)
            status = "downloaded"
        return month, path, status, summary

    completed_paths: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(acquire, month): month for month in months}
        for index, future in enumerate(as_completed(futures), 1):
            month, path, status, summary = future.result()
            completed_paths[month] = path
            print(f"[cffex-cloud] {index}/{len(months)} month={month} status={status} rows={summary['rows']}", flush=True)
    paths = [completed_paths[month] for month in months]
    panel = build_cffex_contract_panel(paths, root / "processed" / "phase3_derivatives" / "cffex_contract_daily.parquet")
    master = build_cffex_contract_master(panel, root / "processed" / "phase3_derivatives" / "cffex_contract_master.parquet")
    print(f"[cffex-cloud] panel={panel} master={master}")


if __name__ == "__main__":
    main()

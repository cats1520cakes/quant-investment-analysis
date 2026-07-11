from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pandas as pd

from quant_proof.free_sources.cffex_trade_parameters import (
    CffexTradeParameterError,
    CffexTradeParameterDownload,
    build_cffex_trade_parameter_metadata,
    cffex_trade_parameter_path,
    cffex_trade_parameters_url,
    derive_required_snapshot_dates,
    parse_cffex_trade_parameters_csv,
    validate_cffex_trade_parameter_csv,
    validate_cffex_trade_parameter_download_manifest,
    write_cffex_trade_parameter_download_manifest,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with temporary.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _calendar_dates(start_date: str, end_date: str) -> list[str]:
    start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
    if start > end:
        raise ValueError("start-date must not exceed end-date")
    return [value.strftime("%Y%m%d") for value in pd.date_range(start, end, freq="D")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable cloud official CFFEX expiry/trade-parameter acquisition")
    parser.add_argument("--data-root", default="artifacts/runtime_data")
    parser.add_argument("--panel")
    parser.add_argument("--master")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--calendar-crawl", action="store_true")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--backoff-seconds", type=float, default=3.0)
    parser.add_argument("--max-snapshots", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.data_root)
    bound_mode = bool(args.panel and args.master)
    if bound_mode:
        panel, master = Path(args.panel), Path(args.master)
        dates = derive_required_snapshot_dates(panel, master)
    elif args.calendar_crawl and args.start_date and args.end_date:
        panel = master = None
        dates = _calendar_dates(args.start_date, args.end_date)
    else:
        parser.error("provide --panel/--master or explicit --calendar-crawl --start-date/--end-date")
    if args.max_snapshots:
        dates = dates[: args.max_snapshots]
    records, failures = [], []
    scope = (panel.stem.replace("cffex_contract_daily", "").strip("_") if panel else f"calendar_{dates[0]}_{dates[-1]}") or "canonical"
    attempt_path = root / "00_meta" / "manifests" / f"cffex_trade_parameter_attempts_{scope}.json"
    try:
        attempts_ledger = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        attempts_ledger = {}
    for index, date in enumerate(dates, 1):
        url = cffex_trade_parameters_url(date)
        path = cffex_trade_parameter_path(root, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "cached_valid"
        try:
            frame = validate_cffex_trade_parameter_csv(path, date)
        except Exception:
            temporary = path.with_suffix(".csv.tmp")
            error = ""
            for attempt in range(1, args.attempts + 1):
                try:
                    completed = subprocess.run(["curl", "-fsSL", "-C", "-", "--max-time", str(args.timeout), "--write-out", "%{http_code}", "-o", str(temporary), url], check=True, capture_output=True, text=True)
                    frame = parse_cffex_trade_parameters_csv(temporary.read_bytes(), date, source_file=f"{date}_1.csv", source_url=url)
                    temporary.replace(path)
                    frame = validate_cffex_trade_parameter_csv(path, date)
                    status = "downloaded"
                    attempts_ledger[date] = {"status": status, "url": url, "http_code": completed.stdout[-3:], "bytes": path.stat().st_size, "sha256": _sha256(path), "attempt": attempt, "evidence_tier": "official_cffex_trade_parameter_snapshot"}
                    _atomic_json(attempt_path, attempts_ledger)
                    break
                except Exception as exc:
                    error = str(exc)
                    if isinstance(exc, CffexTradeParameterError) and temporary.exists():
                        temporary.unlink()
                    if attempt < args.attempts:
                        time.sleep(args.backoff_seconds * 2 ** (attempt - 1))
            else:
                failures.append((date, error))
                attempts_ledger[date] = {"status": "unavailable", "url": url, "error": error, "attempts": args.attempts, "evidence_tier": "official_endpoint_no_valid_snapshot"}
                _atomic_json(attempt_path, attempts_ledger)
                print(f"[cffex-expiry-cloud] {index}/{len(dates)} date={date} status=failed", flush=True)
                continue
        record = CffexTradeParameterDownload(
            snapshot_date=date, url=url, path=str(path), status=status, bytes=path.stat().st_size,
            sha256=_sha256(path), rows=len(frame), contracts=frame.contract.nunique(),
            products=",".join(sorted(frame["product"].astype(str).unique())), title_date=date,
            local_ip="", remote_ip="", resolved_ip="", interface="cloud_proxy",
            interface_ipv4="", dns_server="", route_interface="",
        )
        records.append(record)
        attempts_ledger[date] = {"status": status, "url": url, "bytes": path.stat().st_size, "sha256": record.sha256, "rows": record.rows, "contracts": record.contracts, "evidence_tier": "official_cffex_trade_parameter_snapshot"}
        _atomic_json(attempt_path, attempts_ledger)
        print(f"[cffex-expiry-cloud] {index}/{len(dates)} date={date} status={status} contracts={record.contracts}", flush=True)
    manifest = root / "00_meta" / "manifests" / f"cffex_trade_parameters_{scope}.csv"
    write_cffex_trade_parameter_download_manifest(manifest, records)
    if not bound_mode:
        print(f"[cffex-expiry-cloud] unbound_calendar_crawl valid={len(records)} unavailable={len(failures)} manifest={manifest} attempts={attempt_path}")
        return
    if failures or len(records) != len(dates):
        raise SystemExit(f"CFFEX expiry acquisition incomplete: valid={len(records)}/{len(dates)} failures={len(failures)}")
    validate_cffex_trade_parameter_download_manifest(manifest, required_snapshot_dates=dates)
    output_root = root / "processed" / "phase3_derivatives"
    metadata = output_root / f"cffex_trade_parameter_metadata_{scope}.parquet"
    history = output_root / f"cffex_trade_parameter_history_{scope}.parquet"
    build_cffex_trade_parameter_metadata(
        panel, master, manifest, metadata, history_path=history, required_snapshot_dates=dates,
        canonical_output_path=metadata, canonical_history_path=history,
    )
    print(f"[cffex-expiry-cloud] manifest={manifest} metadata={metadata} history={history}")


if __name__ == "__main__":
    main()

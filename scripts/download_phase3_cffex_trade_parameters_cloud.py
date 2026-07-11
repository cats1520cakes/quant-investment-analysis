from __future__ import annotations

import argparse
import hashlib
import subprocess
import time
from pathlib import Path

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable cloud official CFFEX expiry/trade-parameter acquisition")
    parser.add_argument("--data-root", default="artifacts/runtime_data")
    parser.add_argument("--panel", required=True)
    parser.add_argument("--master", required=True)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--backoff-seconds", type=float, default=3.0)
    parser.add_argument("--max-snapshots", type=int, default=0)
    args = parser.parse_args()
    root, panel, master = Path(args.data_root), Path(args.panel), Path(args.master)
    dates = derive_required_snapshot_dates(panel, master)
    if args.max_snapshots:
        dates = dates[: args.max_snapshots]
    records, failures = [], []
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
                    subprocess.run(["curl", "-fsSL", "-C", "-", "--max-time", str(args.timeout), "-o", str(temporary), url], check=True)
                    frame = parse_cffex_trade_parameters_csv(temporary.read_bytes(), date, source_file=f"{date}_1.csv", source_url=url)
                    temporary.replace(path)
                    frame = validate_cffex_trade_parameter_csv(path, date)
                    status = "downloaded"
                    break
                except Exception as exc:
                    error = str(exc)
                    if isinstance(exc, CffexTradeParameterError) and temporary.exists():
                        temporary.unlink()
                    if attempt < args.attempts:
                        time.sleep(args.backoff_seconds * 2 ** (attempt - 1))
            else:
                failures.append((date, error))
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
        print(f"[cffex-expiry-cloud] {index}/{len(dates)} date={date} status={status} contracts={record.contracts}", flush=True)
    scope = panel.stem.replace("cffex_contract_daily", "").strip("_") or "canonical"
    manifest = root / "00_meta" / "manifests" / f"cffex_trade_parameters_{scope}.csv"
    write_cffex_trade_parameter_download_manifest(manifest, records)
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

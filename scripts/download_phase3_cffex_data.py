from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from quant_proof.free_sources.cffex_adapter import (
    CffexDataError,
    build_cffex_contract_master,
    build_cffex_contract_panel,
    cffex_months,
    download_cffex_month,
    write_cffex_download_manifest,
)
from quant_proof.network_guard import direct_network_message, require_direct_network


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download official CFFEX monthly contract data over a bound physical route")
    parser.add_argument("--config", default="config/phase3_cffex_data.yaml")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-months", type=int, default=0)
    parser.add_argument("--build-panel", action="store_true")
    return parser.parse_args()


def resolve_panel_output_path(
    canonical_panel_path: Path,
    start_date: str,
    end_date: str,
    month_count: int,
    canonical_scope: bool,
) -> Path:
    if canonical_scope:
        return canonical_panel_path
    scope = f"{start_date.replace('-', '')}_{end_date.replace('-', '')}_{month_count}m"
    return canonical_panel_path.with_name(
        f"{canonical_panel_path.stem}_{scope}{canonical_panel_path.suffix}"
    )


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data_root = Path(raw["data_root"])
    date_range = raw.get("date_range", {})
    network = raw.get("network", {})
    paths = raw.get("paths", {})
    start_date = args.start_date or str(date_range["start_date"])
    end_date = args.end_date or str(date_range["end_date"])
    canonical_scope = (
        not args.start_date
        and not args.end_date
        and args.max_months == 0
    )
    months = cffex_months(start_date, end_date)
    if args.max_months < 0:
        raise SystemExit("--max-months must be non-negative")
    if args.max_months:
        months = months[: args.max_months]

    interface = str(network.get("physical_interface", "en0"))
    dns_server = str(network.get("physical_dns_server", "")) or None
    timeout_seconds = float(network.get("timeout_seconds", 60.0))
    visible = require_direct_network(socket_timeout_seconds=timeout_seconds)
    print(f"[network] {direct_network_message(visible)}", flush=True)

    manifest_path = data_root / str(paths.get("download_manifest", "00_meta/manifests/cffex_monthly_manifest.csv"))
    archive_root = data_root / str(paths.get("monthly_archives", "raw/cffex/monthly"))
    failures: list[tuple[str, str]] = []
    for index, month in enumerate(months, start=1):
        try:
            record = download_cffex_month(
                data_root,
                month,
                interface=interface,
                dns_server=dns_server,
                timeout_seconds=timeout_seconds,
            )
        except (CffexDataError, OSError, ValueError) as exc:
            failures.append((month, str(exc)))
            print(f"[cffex] {index}/{len(months)} month={month} status=failed error={exc}", flush=True)
            continue
        write_cffex_download_manifest(manifest_path, [record])
        route = ""
        if record.local_ip or record.remote_ip:
            route = f" local={record.local_ip} remote={record.remote_ip} interface={record.interface}"
        print(
            f"[cffex] {index}/{len(months)} month={month} status={record.status} "
            f"days={record.entries} rows={record.rows} bytes={record.bytes}{route}",
            flush=True,
        )

    if failures:
        detail = "; ".join(f"{month}:{error}" for month, error in failures[:5])
        raise SystemExit(f"CFFEX download incomplete: failures={len(failures)} {detail}")

    if args.build_panel:
        archive_paths = [archive_root / f"{month}.zip" for month in months]
        canonical_panel_path = data_root / str(
            paths.get("contract_panel", "processed/phase3_derivatives/cffex_contract_daily.parquet")
        )
        panel_path = resolve_panel_output_path(
            canonical_panel_path,
            start_date=start_date,
            end_date=end_date,
            month_count=len(months),
            canonical_scope=canonical_scope,
        )
        output = build_cffex_contract_panel(archive_paths, panel_path)
        print(f"[cffex] panel={output}", flush=True)
        canonical_master_path = data_root / str(
            paths.get("contract_master", "processed/phase3_derivatives/cffex_contract_master.parquet")
        )
        master_path = resolve_panel_output_path(
            canonical_master_path,
            start_date=start_date,
            end_date=end_date,
            month_count=len(months),
            canonical_scope=canonical_scope,
        )
        master = build_cffex_contract_master(output, master_path)
        print(f"[cffex] contract_master={master}", flush=True)
    print(f"[cffex] manifest={manifest_path}", flush=True)


if __name__ == "__main__":
    main()

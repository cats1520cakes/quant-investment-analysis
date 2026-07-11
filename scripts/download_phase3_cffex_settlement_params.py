from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from quant_proof.free_sources.cffex_settlement_params import (
    CffexSettlementError,
    build_cffex_settlement_artifact,
    discover_cffex_settlement_csvs,
    download_cffex_settlement_csv,
    normalize_snapshot_date,
    settlement_artifact_manifest_path,
    validate_page_numbers,
)
from quant_proof.network_guard import DirectRouteError, direct_network_message, require_direct_network


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and build official CFFEX settlement parameters over a bound physical route"
    )
    parser.add_argument("--config", default="config/phase3_cffex_settlement_params.yaml")
    parser.add_argument("--pages", default="", help="Continuous page range such as 1-36 or 4-6")
    parser.add_argument("--start-date", default="", help="Optional inclusive snapshot filter (YYYYMMDD)")
    parser.add_argument("--end-date", default="", help="Optional inclusive snapshot filter (YYYYMMDD)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--discover-only", action="store_true")
    mode.add_argument("--download-only", action="store_true")
    return parser.parse_args()


def parse_page_selection(value: str, configured_pages: list[int]) -> tuple[int, ...]:
    if not value.strip():
        return tuple(configured_pages)
    pages: list[int] = []
    for token in value.split(","):
        part = token.strip()
        if not part:
            raise ValueError("empty page token")
        if "-" in part:
            first_text, last_text = part.split("-", 1)
            first = int(first_text)
            last = int(last_text)
            if first > last:
                raise ValueError(f"descending page range: {part}")
            pages.extend(range(first, last + 1))
        else:
            pages.append(int(part))
    selected = validate_page_numbers(pages, expected_total_pages=max(configured_pages))
    if not set(selected).issubset(configured_pages):
        raise ValueError("selected pages are outside the configured canonical page set")
    return selected


def _load_config(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    source = raw.get("source", {})
    pages = [int(page) for page in source.get("page_numbers", [])]
    expected_page_count = int(source.get("expected_page_count", 0))
    if expected_page_count != 36:
        raise ValueError("CFFEX settlement config must declare exactly 36 canonical pages")
    if tuple(pages) != tuple(range(1, expected_page_count + 1)):
        raise ValueError("CFFEX settlement config page_numbers must be the continuous range 1..36")
    if int(source.get("expected_unique_csvs", 0)) != 360:
        raise ValueError("CFFEX settlement config must declare 360 canonical source CSVs")
    if bool(raw.get("policy", {}).get("short_options_enabled", True)):
        raise ValueError("short options must remain disabled in the settlement-parameter policy")
    return raw


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    config = _load_config(config_path)
    data_root = Path(config["data_root"])
    source = config["source"]
    network = config.get("network", {})
    paths = config.get("paths", {})

    configured_pages = [int(page) for page in source["page_numbers"]]
    selected_pages = parse_page_selection(args.pages, configured_pages)
    expected_page_count = int(source["expected_page_count"])
    expected_unique_csvs = int(source["expected_unique_csvs"])
    expected_first = str(source["expected_first_snapshot"])
    expected_last = str(source["expected_last_snapshot"])

    start_date = normalize_snapshot_date(args.start_date) if args.start_date else None
    end_date = normalize_snapshot_date(args.end_date) if args.end_date else None
    if start_date and end_date and start_date > end_date:
        raise SystemExit("--start-date must not exceed --end-date")

    interface = str(network.get("physical_interface", "en0"))
    configured_dns = network.get("physical_dns_server")
    dns_server = None if configured_dns is None or str(configured_dns).strip().lower() == "auto" else str(configured_dns)
    timeout_seconds = float(network.get("timeout_seconds", 60.0))
    max_page_bytes = int(network.get("max_page_bytes", 2 * 1024 * 1024))
    max_csv_bytes = int(network.get("max_csv_bytes", 5 * 1024 * 1024))
    visible = require_direct_network(socket_timeout_seconds=timeout_seconds)
    print(f"[network] {direct_network_message(visible)}", flush=True)

    try:
        discovered = discover_cffex_settlement_csvs(
            page_numbers=selected_pages,
            expected_total_pages=expected_page_count,
            expected_unique_csvs=expected_unique_csvs,
            expected_first_snapshot=expected_first,
            expected_last_snapshot=expected_last,
            interface=interface,
            dns_server=dns_server,
            timeout_seconds=timeout_seconds,
            max_page_bytes=max_page_bytes,
        )
    except (CffexSettlementError, DirectRouteError) as exc:
        raise SystemExit(f"CFFEX settlement discovery failed: {exc}") from exc

    print(
        f"[cffex-settlement] pages={selected_pages[0]}..{selected_pages[-1]} "
        f"unique_csvs={len(discovered)} first={discovered[0].snapshot_date} "
        f"last={discovered[-1].snapshot_date}",
        flush=True,
    )
    if args.discover_only:
        return

    selected = [
        item
        for item in discovered
        if (start_date is None or item.snapshot_date >= start_date)
        and (end_date is None or item.snapshot_date <= end_date)
    ]
    if not selected:
        raise SystemExit("CFFEX settlement date/page scope selected no source CSVs")

    raw_root = data_root / str(paths["raw_settlement_params"])
    download_manifest = data_root / str(paths["download_manifest"])
    records = []
    for index, item in enumerate(selected, start=1):
        try:
            record = download_cffex_settlement_csv(
                item,
                raw_root=raw_root,
                manifest_path=download_manifest,
                interface=interface,
                dns_server=dns_server,
                timeout_seconds=timeout_seconds,
                max_csv_bytes=max_csv_bytes,
                expected_pages=configured_pages,
                expected_unique_csvs=expected_unique_csvs,
            )
        except CffexSettlementError as exc:
            raise SystemExit(
                f"CFFEX settlement download failed at {item.snapshot_date} ({index}/{len(selected)}): {exc}"
            ) from exc
        records.append(record)
        print(
            f"[cffex-settlement] {index}/{len(selected)} date={record.snapshot_date} "
            f"status={record.status} bytes={record.bytes} sha256={record.sha256[:12]} "
            f"local={record.local_ip or '-'} remote={record.remote_ip or '-'} interface={record.interface}",
            flush=True,
        )
    print(f"[cffex-settlement] download_manifest={download_manifest}", flush=True)
    if args.download_only:
        return

    canonical_parquet = data_root / str(paths["canonical_parquet"])
    configured_manifest = data_root / str(paths["canonical_manifest"])
    if configured_manifest != settlement_artifact_manifest_path(canonical_parquet):
        raise SystemExit("configured canonical_manifest must be the sidecar for canonical_parquet")
    try:
        artifact = build_cffex_settlement_artifact(
            records,
            canonical_parquet,
            selected_pages=selected_pages,
            expected_pages=configured_pages,
            expected_unique_csvs=expected_unique_csvs,
            expected_first_snapshot=expected_first,
            expected_last_snapshot=expected_last,
            start_date=start_date,
            end_date=end_date,
        )
    except CffexSettlementError as exc:
        raise SystemExit(f"CFFEX settlement artifact build failed: {exc}") from exc
    print(
        f"[cffex-settlement] parquet={artifact.parquet_path} manifest={artifact.manifest_path} "
        f"rows={artifact.rows} sources={artifact.sources} canonical={artifact.canonical}",
        flush=True,
    )


if __name__ == "__main__":
    main()

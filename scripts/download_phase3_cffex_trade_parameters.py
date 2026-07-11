from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd
import yaml

from quant_proof.free_sources.cffex_trade_parameters import (
    CFFEX_TRADE_PARAMETERS_URL,
    CffexTradeParameterError,
    build_cffex_trade_parameter_metadata,
    derive_required_snapshot_dates,
    download_cffex_trade_parameters,
    reconcile_cffex_last_trade_dates,
    scoped_artifact_path,
    validate_cffex_trade_parameter_download_manifest,
    validate_cffex_trade_parameter_metadata_manifest,
    write_cffex_trade_parameter_download_manifest,
)
from quant_proof.network_guard import DirectRouteError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download official CFFEX trade-parameter snapshots over a bound "
            "physical interface and build exact contract metadata"
        )
    )
    parser.add_argument(
        "--config", default="config/phase3_cffex_trade_parameters.yaml"
    )
    parser.add_argument(
        "--snapshot-date",
        action="append",
        default=[],
        help="Debug scope; repeat only dates already in the canonical requirement set",
    )
    parser.add_argument(
        "--max-date",
        default="",
        help="Debug scope ending on a date in the validated panel calendar",
    )
    parser.add_argument(
        "--max-snapshots",
        type=int,
        default=0,
        help="Debug scope containing the first N canonical required snapshots",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Finite retries on the same bound physical route; never enables fallback",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--download-only", action="store_true", help="Skip metadata/reconciliation"
    )
    mode.add_argument(
        "--build-only", action="store_true", help="Use an already complete source manifest"
    )
    return parser.parse_args()


def _external_path(data_root: Path, configured: object, field: str) -> Path:
    relative = Path(str(configured))
    if relative.is_absolute():
        raise CffexTradeParameterError(
            f"{field} must be relative to the configured external data_root"
        )
    return data_root / relative


def _atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        frame.to_csv(tmp_path, index=False, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def resolve_run_paths(
    canonical_manifest: Path,
    canonical_metadata: Path,
    canonical_history: Path,
    canonical_reconciliation: Path,
    snapshot_dates: list[str],
    *,
    scoped: bool,
) -> tuple[Path, Path, Path, Path]:
    if not scoped:
        return (
            canonical_manifest,
            canonical_metadata,
            canonical_history,
            canonical_reconciliation,
        )
    return (
        scoped_artifact_path(canonical_manifest, snapshot_dates),
        scoped_artifact_path(canonical_metadata, snapshot_dates),
        scoped_artifact_path(canonical_history, snapshot_dates),
        scoped_artifact_path(canonical_reconciliation, snapshot_dates),
    )


def main() -> None:
    args = parse_args()
    if args.max_snapshots < 0:
        raise SystemExit("--max-snapshots must be non-negative")
    if args.retries < 1:
        raise SystemExit("--retries must be at least 1")
    config_path = Path(args.config)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data_root = Path(str(raw["data_root"])).expanduser()
    if not data_root.is_absolute():
        raise SystemExit("data_root must be an absolute external path")
    source = raw.get("source", {})
    configured_url = str(source.get("url_pattern", ""))
    if configured_url != CFFEX_TRADE_PARAMETERS_URL:
        raise SystemExit(
            "source.url_pattern must equal the vetted official CFFEX endpoint"
        )
    network = raw.get("network", {})
    paths = raw.get("paths", {})
    panel_path = _external_path(data_root, paths["contract_panel"], "contract_panel")
    master_path = _external_path(
        data_root, paths["contract_master"], "contract_master"
    )
    canonical_manifest = _external_path(
        data_root, paths["download_manifest"], "download_manifest"
    )
    canonical_metadata = _external_path(
        data_root, paths["contract_metadata"], "contract_metadata"
    )
    canonical_history = _external_path(
        data_root,
        paths["contract_metadata_history"],
        "contract_metadata_history",
    )
    canonical_reconciliation = _external_path(
        data_root, paths["reconciliation_report"], "reconciliation_report"
    )

    canonical_dates = derive_required_snapshot_dates(panel_path, master_path)
    scoped = bool(args.snapshot_date or args.max_date or args.max_snapshots)
    dates = derive_required_snapshot_dates(
        panel_path,
        master_path,
        scoped_dates=args.snapshot_date or None,
        max_date=args.max_date or None,
        max_snapshots=args.max_snapshots or None,
    )
    manifest_path, metadata_path, history_path, reconciliation_path = resolve_run_paths(
        canonical_manifest,
        canonical_metadata,
        canonical_history,
        canonical_reconciliation,
        dates,
        scoped=scoped,
    )
    if scoped and any(
        candidate.resolve(strict=False) == canonical.resolve(strict=False)
        for candidate, canonical in (
            (manifest_path, canonical_manifest),
            (metadata_path, canonical_metadata),
            (history_path, canonical_history),
            (reconciliation_path, canonical_reconciliation),
        )
    ):
        raise SystemExit("scoped CFFEX runs must not target canonical artifacts")

    interface = str(network.get("physical_interface", "en0"))
    configured_dns = str(network.get("physical_dns_server", "")).strip()
    dns_server = (
        None
        if configured_dns.lower() in {"", "auto", "dhcp"}
        else configured_dns
    )
    timeout_seconds = float(network.get("timeout_seconds", 60.0))
    max_bytes = int(network.get("max_response_bytes", 2 * 1024 * 1024))
    raw_relative_root = str(paths["raw_trade_parameters"])
    if not args.build_only:
        failures: list[tuple[str, str]] = []
        for index, snapshot_date in enumerate(dates, start=1):
            record = None
            error = ""
            for attempt in range(1, args.retries + 1):
                try:
                    record = download_cffex_trade_parameters(
                        data_root,
                        snapshot_date,
                        raw_relative_root=raw_relative_root,
                        interface=interface,
                        dns_server=dns_server,
                        timeout_seconds=timeout_seconds,
                        max_bytes=max_bytes,
                    )
                    write_cffex_trade_parameter_download_manifest(
                        manifest_path, [record]
                    )
                    break
                except (
                    CffexTradeParameterError,
                    DirectRouteError,
                    OSError,
                    ValueError,
                ) as exc:
                    error = str(exc)
                    if attempt < args.retries:
                        print(
                            f"[cffex-jycs] {index}/{len(dates)} date={snapshot_date} "
                            f"status=retry attempt={attempt}/{args.retries} error={exc}",
                            flush=True,
                        )
                        time.sleep(min(2.0**attempt, 8.0))
            if record is None:
                failures.append((snapshot_date, error))
                print(
                    f"[cffex-jycs] {index}/{len(dates)} date={snapshot_date} "
                    f"status=failed error={error}",
                    flush=True,
                )
                continue
            route = ""
            if record.local_ip or record.remote_ip:
                route = (
                    f" local={record.local_ip} remote={record.remote_ip} "
                    f"interface={record.interface}"
                )
            print(
                f"[cffex-jycs] {index}/{len(dates)} date={snapshot_date} "
                f"status={record.status} contracts={record.contracts} "
                f"bytes={record.bytes}{route}",
                flush=True,
            )
        if failures:
            preview = "; ".join(
                f"{date_value}:{error}" for date_value, error in failures[:5]
            )
            raise SystemExit(
                f"CFFEX trade-parameter download incomplete: "
                f"failures={len(failures)} {preview}"
            )

    validate_cffex_trade_parameter_download_manifest(
        manifest_path, required_snapshot_dates=dates
    )
    print(
        f"[cffex-jycs] manifest={manifest_path} snapshots={len(dates)} "
        f"canonical_expected={len(canonical_dates)} scoped={scoped}",
        flush=True,
    )
    if args.download_only:
        return

    output = build_cffex_trade_parameter_metadata(
        panel_path,
        master_path,
        manifest_path,
        metadata_path,
        history_path=history_path,
        required_snapshot_dates=dates,
        canonical_output_path=canonical_metadata,
        canonical_history_path=canonical_history,
    )
    metadata_manifest = validate_cffex_trade_parameter_metadata_manifest(
        output,
        panel_path,
        master_path,
        manifest_path,
    )
    panel_manifest_path = panel_path.with_suffix(panel_path.suffix + ".manifest.json")
    panel_manifest = yaml.safe_load(panel_manifest_path.read_text(encoding="utf-8"))
    reconciliation = reconcile_cffex_last_trade_dates(
        master_path,
        output,
        panel_last_date=panel_manifest["last_date"],
    )
    _atomic_write_csv(reconciliation_path, reconciliation.to_frame())
    print(
        f"[cffex-jycs] metadata={output} rows={metadata_manifest['rows']} "
        f"canonical={metadata_manifest['canonical']}",
        flush=True,
    )
    print(
        f"[cffex-jycs] history={metadata_manifest['history_path']} "
        f"rows={metadata_manifest['history_rows']} "
        f"sha256={metadata_manifest['history_sha256']} "
        f"revised_contracts={metadata_manifest['expiry_revised_contracts']} "
        f"revision_events={metadata_manifest['expiry_revision_events']}",
        flush=True,
    )
    print(
        f"[cffex-jycs] reconciliation={reconciliation_path} "
        f"summary={dict(reconciliation.summary)}",
        flush=True,
    )
    if bool(metadata_manifest["canonical"]) and not reconciliation.is_complete:
        raise SystemExit("canonical CFFEX expiry reconciliation is incomplete")


if __name__ == "__main__":
    main()

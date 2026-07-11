from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_sources.baostock_adapter import load_config
from quant_proof.free_sources.daily_integrity import DailyPairIntegrity, inspect_daily_pairs
from quant_proof.free_sources.download_planner import refresh_download_plan


ISOLATION_MANIFEST_NAME = "isolated_codes.csv"
ISOLATION_STATUSES = frozenset({"isolated", "resolved"})
ISOLATION_COLUMNS: tuple[str, ...] = (
    "plan_id",
    "shard_id",
    "source_code",
    "status",
    "attempt_count",
    "first_failed_at",
    "last_attempt_at",
    "last_checked_at",
    "resolved_at",
    "last_exit_code",
    "last_pair_error",
    "last_integrity_error",
)
PAIR_ERROR_RE = re.compile(
    r"^\S+\s+(?P<source_code>[A-Za-z]+\.[A-Za-z0-9]+):\s+(?P<message>.+)$"
)

RunCommand = Callable[..., subprocess.CompletedProcess]


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_codes(path: str | Path) -> list[str]:
    return [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _atomic_write_lines(path: Path, values: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.to_csv(tmp_path, index=False, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _empty_isolation_manifest() -> pd.DataFrame:
    return pd.DataFrame(columns=ISOLATION_COLUMNS)


def _load_isolation_manifest(path: Path, plan_id: str) -> pd.DataFrame:
    if not path.exists():
        return _empty_isolation_manifest()
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, pd.errors.ParserError) as exc:
        raise RuntimeError(f"isolation manifest is unreadable: {path}") from exc
    missing = sorted(set(ISOLATION_COLUMNS) - set(frame.columns))
    if missing:
        raise RuntimeError(f"isolation manifest is missing fields: {','.join(missing)}")
    frame = frame.loc[:, list(ISOLATION_COLUMNS)].copy()
    if frame.empty:
        return frame
    if set(frame["plan_id"]) != {plan_id}:
        raise RuntimeError("isolation manifest plan_id does not match the download plan")
    invalid_statuses = sorted(set(frame["status"]) - ISOLATION_STATUSES)
    if invalid_statuses:
        raise RuntimeError(
            f"isolation manifest has invalid statuses: {','.join(invalid_statuses)}"
        )
    if frame["source_code"].duplicated().any():
        raise RuntimeError("isolation manifest has duplicate source_code rows")
    attempts = pd.to_numeric(frame["attempt_count"], errors="coerce")
    shard_ids = pd.to_numeric(frame["shard_id"], errors="coerce")
    exit_codes = pd.to_numeric(frame["last_exit_code"], errors="coerce")
    if attempts.isna().any() or (attempts < 1).any():
        raise RuntimeError("isolation manifest has invalid attempt_count values")
    if shard_ids.isna().any() or (shard_ids < 1).any():
        raise RuntimeError("isolation manifest has invalid shard_id values")
    if exit_codes.isna().any():
        raise RuntimeError("isolation manifest has invalid last_exit_code values")
    frame["attempt_count"] = attempts.astype(int)
    frame["shard_id"] = shard_ids.astype(int)
    frame["last_exit_code"] = exit_codes.astype(int)
    return frame


def _write_isolation_manifest(path: Path, frame: pd.DataFrame) -> None:
    ordered = frame.loc[:, list(ISOLATION_COLUMNS)].copy()
    if not ordered.empty:
        ordered = ordered.sort_values(["shard_id", "source_code"], kind="mergesort")
    _atomic_write_csv(path, ordered)


def _plan_codes(manifest: pd.DataFrame) -> tuple[dict[int, list[str]], dict[str, int]]:
    codes_by_shard: dict[int, list[str]] = {}
    shard_by_code: dict[str, int] = {}
    for row in manifest.itertuples(index=False):
        shard_id = int(row.shard_id)
        codes = _read_codes(row.codes_file)
        codes_by_shard[shard_id] = codes
        for source_code in codes:
            if source_code in shard_by_code:
                raise RuntimeError(f"download plan contains duplicate source_code: {source_code}")
            shard_by_code[source_code] = shard_id
    return codes_by_shard, shard_by_code


def _inspect_plan_codes(
    data_root: Path,
    codes_by_shard: dict[int, list[str]],
) -> tuple[dict[str, DailyPairIntegrity], dict[int, list[str]]]:
    all_codes = [code for codes in codes_by_shard.values() for code in codes]
    integrity = inspect_daily_pairs(data_root, all_codes)
    pending_by_shard = {
        shard_id: [code for code in codes if not integrity[code].complete]
        for shard_id, codes in codes_by_shard.items()
    }
    return integrity, pending_by_shard


def _validate_isolation_scope(
    isolation: pd.DataFrame,
    shard_by_code: dict[str, int],
) -> None:
    for row in isolation.itertuples(index=False):
        expected_shard = shard_by_code.get(str(row.source_code))
        if expected_shard is None:
            raise RuntimeError(
                f"isolation manifest code is absent from the download plan: {row.source_code}"
            )
        if expected_shard != int(row.shard_id):
            raise RuntimeError(
                f"isolation manifest shard mismatch for {row.source_code}: "
                f"expected {expected_shard}, found {row.shard_id}"
            )


def _reconcile_isolation(
    isolation: pd.DataFrame,
    integrity: dict[str, DailyPairIntegrity],
    checked_at: str,
) -> pd.DataFrame:
    reconciled = isolation.copy()
    for index, row in reconciled.iterrows():
        source_code = str(row["source_code"])
        result = integrity[source_code]
        reconciled.at[index, "last_checked_at"] = checked_at
        reconciled.at[index, "last_integrity_error"] = result.error_summary
        if result.complete:
            reconciled.at[index, "status"] = "resolved"
            if not str(row["resolved_at"]):
                reconciled.at[index, "resolved_at"] = checked_at
        else:
            reconciled.at[index, "status"] = "isolated"
            reconciled.at[index, "resolved_at"] = ""
    return reconciled


def _error_log_offset(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _read_new_pair_errors(
    path: Path,
    offset: int,
    attempted_codes: set[str],
) -> dict[str, str]:
    try:
        with path.open("rb") as handle:
            if handle.seek(0, os.SEEK_END) < offset:
                offset = 0
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    errors: dict[str, str] = {}
    for line in text.splitlines():
        match = PAIR_ERROR_RE.match(line.strip())
        if not match:
            continue
        source_code = match.group("source_code")
        if source_code in attempted_codes:
            errors[source_code] = match.group("message")
    return errors


def _record_attempt(
    isolation: pd.DataFrame,
    *,
    plan_id: str,
    shard_id: int,
    attempted_codes: list[str],
    integrity: dict[str, DailyPairIntegrity],
    pair_errors: dict[str, str],
    returncode: int,
    attempted_at: str,
) -> tuple[pd.DataFrame, list[str]]:
    updated = isolation.copy()
    index_by_code = {
        str(source_code): int(index)
        for index, source_code in updated["source_code"].items()
    }
    unaccounted: list[str] = []
    for source_code in attempted_codes:
        result = integrity[source_code]
        existing_index = index_by_code.get(source_code)
        pair_error = pair_errors.get(source_code, "")
        if existing_index is None and result.complete:
            continue
        if existing_index is None and not pair_error:
            unaccounted.append(source_code)
            continue
        if existing_index is None:
            record = {
                "plan_id": plan_id,
                "shard_id": shard_id,
                "source_code": source_code,
                "status": "isolated",
                "attempt_count": 1,
                "first_failed_at": attempted_at,
                "last_attempt_at": attempted_at,
                "last_checked_at": attempted_at,
                "resolved_at": "",
                "last_exit_code": int(returncode),
                "last_pair_error": pair_error,
                "last_integrity_error": result.error_summary,
            }
            updated.loc[len(updated)] = record
            index_by_code[source_code] = int(updated.index[-1])
            continue

        index = existing_index
        updated.at[index, "attempt_count"] = int(updated.at[index, "attempt_count"]) + 1
        updated.at[index, "last_attempt_at"] = attempted_at
        updated.at[index, "last_checked_at"] = attempted_at
        updated.at[index, "last_exit_code"] = int(returncode)
        updated.at[index, "last_integrity_error"] = result.error_summary
        if pair_error:
            updated.at[index, "last_pair_error"] = pair_error
        if result.complete:
            updated.at[index, "status"] = "resolved"
            updated.at[index, "resolved_at"] = attempted_at
        else:
            updated.at[index, "status"] = "isolated"
            updated.at[index, "resolved_at"] = ""
            if not pair_error:
                unaccounted.append(source_code)
    return updated, unaccounted


def _build_command(
    config_path: str | Path,
    codes_file: Path,
    network_interface: str,
    dns_server: str,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "download_phase2_free_real_data.py"),
        "--config",
        str(config_path),
        "--codes-file",
        str(codes_file),
    ]
    if network_interface:
        command.extend(["--network-interface", network_interface])
    if dns_server:
        command.extend(["--dns-server", dns_server])
    return command


def run_download_plan(
    *,
    config_path: str | Path,
    plan_path: str | Path,
    max_shards: int = 0,
    network_interface: str = "",
    dns_server: str = "",
    run_command: RunCommand | None = None,
) -> int:
    if max_shards < 0:
        raise ValueError("max_shards must be non-negative")
    config = load_config(config_path)
    resolved_plan_path = Path(plan_path)
    superseded_path = resolved_plan_path.parent / "SUPERSEDED"
    if superseded_path.exists():
        replacement = superseded_path.read_text(encoding="utf-8").strip()
        raise RuntimeError(f"download plan is superseded; use {replacement}")

    manifest = refresh_download_plan(config.data_root, resolved_plan_path)
    plan_ids = set(manifest["plan_id"].astype(str))
    if len(plan_ids) != 1:
        raise RuntimeError("download plan must contain exactly one plan_id")
    plan_id = next(iter(plan_ids))
    codes_by_shard, shard_by_code = _plan_codes(manifest)
    integrity, pending_by_shard = _inspect_plan_codes(config.data_root, codes_by_shard)

    isolation_path = resolved_plan_path.parent / ISOLATION_MANIFEST_NAME
    isolation = _load_isolation_manifest(isolation_path, plan_id)
    _validate_isolation_scope(isolation, shard_by_code)
    isolation = _reconcile_isolation(isolation, integrity, _timestamp())
    if isolation_path.exists() or not isolation.empty:
        _write_isolation_manifest(isolation_path, isolation)

    active_at_start = set(
        isolation.loc[isolation["status"] == "isolated", "source_code"].astype(str)
    )
    normal_jobs: list[tuple[int, list[str]]] = []
    for row in manifest.itertuples(index=False):
        shard_id = int(row.shard_id)
        codes = [code for code in pending_by_shard[shard_id] if code not in active_at_start]
        if codes:
            normal_jobs.append((shard_id, codes))
    if max_shards:
        normal_jobs = normal_jobs[:max_shards]

    isolated_jobs: list[tuple[int, list[str]]] = []
    for shard_id in sorted(codes_by_shard):
        codes = [code for code in pending_by_shard[shard_id] if code in active_at_start]
        if codes:
            isolated_jobs.append((shard_id, codes))

    print(
        f"[plan] shards_total={len(manifest)} "
        f"complete={int((manifest['status'] == 'complete').sum())} "
        f"scheduled_normal={len(normal_jobs)} retry_shards={len(isolated_jobs)} "
        f"remaining_codes={int(manifest['remaining_codes'].sum())} "
        f"isolated_codes={len(active_at_start)}",
        flush=True,
    )

    runner = run_command or subprocess.run
    attempts_root = resolved_plan_path.parent / "attempts"
    error_log = config.data_root / "00_meta" / "errors" / "phase2_free_daily.log"
    failed_processes = 0
    unaccounted_failures = 0
    jobs = [
        ("normal", position, len(normal_jobs), shard_id, codes)
        for position, (shard_id, codes) in enumerate(normal_jobs, start=1)
    ] + [
        ("isolated_retry", position, len(isolated_jobs), shard_id, codes)
        for position, (shard_id, codes) in enumerate(isolated_jobs, start=1)
    ]
    for lane, position, lane_total, shard_id, attempted_codes in jobs:
        attempts_file = attempts_root / f"shard_{shard_id:04d}_{lane}.txt"
        _atomic_write_lines(attempts_file, attempted_codes)
        command = _build_command(
            config_path,
            attempts_file,
            network_interface,
            dns_server,
        )
        print(
            f"[plan] lane={lane} run={position}/{lane_total} shard={shard_id:04d} "
            f"codes={len(attempted_codes)}",
            flush=True,
        )
        log_offset = _error_log_offset(error_log)
        result = runner(command, cwd=ROOT, check=False)
        attempted_at = _timestamp()
        if result.returncode != 0:
            failed_processes += 1
        pair_errors = _read_new_pair_errors(
            error_log,
            log_offset,
            set(attempted_codes),
        )
        manifest = refresh_download_plan(config.data_root, resolved_plan_path)
        integrity = inspect_daily_pairs(config.data_root, attempted_codes)
        isolation, unaccounted_codes = _record_attempt(
            isolation,
            plan_id=plan_id,
            shard_id=shard_id,
            attempted_codes=attempted_codes,
            integrity=integrity,
            pair_errors=pair_errors,
            returncode=int(result.returncode),
            attempted_at=attempted_at,
        )
        _write_isolation_manifest(isolation_path, isolation)
        refreshed = manifest.loc[manifest["shard_id"] == shard_id].iloc[0]
        incomplete_attempted = [
            code for code in attempted_codes if not integrity[code].complete
        ]
        isolated_attempted = set(
            isolation.loc[
                isolation["status"] == "isolated", "source_code"
            ].astype(str)
        ).intersection(attempted_codes)
        if unaccounted_codes or (result.returncode != 0 and not incomplete_attempted):
            unaccounted_failures += 1
        print(
            f"[plan] lane={lane} shard={shard_id:04d} exit={result.returncode} "
            f"status={refreshed['status']} remaining_after={int(refreshed['remaining_codes'])} "
            f"isolated_after={len(isolated_attempted)} "
            f"unaccounted_after={len(unaccounted_codes)}",
            flush=True,
        )

    final = refresh_download_plan(config.data_root, resolved_plan_path)
    codes_by_shard, shard_by_code = _plan_codes(final)
    final_integrity, final_pending = _inspect_plan_codes(config.data_root, codes_by_shard)
    _validate_isolation_scope(isolation, shard_by_code)
    isolation = _reconcile_isolation(isolation, final_integrity, _timestamp())
    if isolation_path.exists() or not isolation.empty:
        _write_isolation_manifest(isolation_path, isolation)
    active_final = set(
        isolation.loc[isolation["status"] == "isolated", "source_code"].astype(str)
    )
    normal_remaining = sum(
        code not in active_final
        for codes in final_pending.values()
        for code in codes
    )
    total_remaining = int(final["remaining_codes"].sum())
    print(
        f"[plan] complete_shards={int((final['status'] == 'complete').sum())}/{len(final)} "
        f"remaining_codes={total_remaining} isolated_codes={len(active_final)} "
        f"normal_remaining={normal_remaining} failed_processes={failed_processes} "
        f"unaccounted_failures={unaccounted_failures} "
        f"isolation_manifest={isolation_path}",
        flush=True,
    )

    if unaccounted_failures:
        return 1
    if total_remaining == 0:
        return 0
    if normal_remaining and max_shards and len(normal_jobs) == max_shards:
        return 0
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a resumable BaoStock shard download plan serially."
    )
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    parser.add_argument("--plan", required=True)
    parser.add_argument(
        "--max-shards",
        type=int,
        default=0,
        help=(
            "Maximum normal incomplete shards for this invocation; 0 means all. "
            "Isolated retry shards do not consume this limit."
        ),
    )
    parser.add_argument("--network-interface", default="")
    parser.add_argument("--dns-server", default="")
    args = parser.parse_args()

    try:
        exit_code = run_download_plan(
            config_path=args.config,
            plan_path=args.plan,
            max_shards=args.max_shards,
            network_interface=args.network_interface,
            dns_server=args.dns_server,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"[plan] fatal={type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

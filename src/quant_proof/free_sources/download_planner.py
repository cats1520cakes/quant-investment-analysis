from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from .daily_integrity import inspect_daily_pairs


PLAN_COLUMNS: tuple[str, ...] = (
    "plan_id",
    "shard_id",
    "codes",
    "first_code",
    "last_code",
    "codes_file",
    "status_at_plan_time",
)
PLAN_COMPLETION_COLUMNS: tuple[str, ...] = (
    "complete_codes",
    "remaining_codes",
    "status",
    "checked_at",
)
ISOLATION_MANIFEST_NAME = "isolated_codes.csv"
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


class DownloadPlanProvenanceError(RuntimeError):
    """Raised when a download plan cannot prove canonical input completion."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_codes_file(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _resolve_codes_file(value: object, manifest_path: Path) -> Path:
    candidate = Path(str(value)).expanduser()
    if candidate.is_absolute() or candidate.exists():
        return candidate.resolve()
    sibling = manifest_path.parent / "shards" / candidate.name
    return sibling.resolve() if sibling.exists() else candidate.resolve()


def _as_positive_int(value: object, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DownloadPlanProvenanceError(
            f"download plan has invalid {field}: {value}"
        ) from exc
    if parsed <= 0:
        raise DownloadPlanProvenanceError(
            f"download plan has non-positive {field}: {parsed}"
        )
    return parsed


def read_download_plan_provenance(
    manifest_path: str | Path,
    *,
    expected_codes: set[str] | None = None,
    require_complete: bool = True,
) -> dict[str, object]:
    """Validate and summarize immutable plan, completion, and retry semantics."""

    path = Path(manifest_path).expanduser().resolve()
    if not path.exists():
        raise DownloadPlanProvenanceError(f"download plan is missing: {path}")
    try:
        manifest = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, pd.errors.ParserError) as exc:
        raise DownloadPlanProvenanceError(
            f"download plan is unreadable: {path}"
        ) from exc
    required = set(PLAN_COLUMNS)
    if require_complete:
        required.update(PLAN_COMPLETION_COLUMNS)
    missing_columns = sorted(required - set(manifest.columns))
    if missing_columns:
        raise DownloadPlanProvenanceError(
            f"download plan is missing fields: {','.join(missing_columns)}"
        )
    if manifest.empty:
        prefix = "phase2_free_"
        if not path.parent.name.startswith(prefix) or len(path.parent.name) <= len(prefix):
            raise DownloadPlanProvenanceError(
                "empty download plan cannot infer plan_id from its directory"
            )
        plan_id = path.parent.name[len(prefix) :]
    else:
        plan_ids = set(manifest["plan_id"].astype(str)) - {""}
        if len(plan_ids) != 1:
            raise DownloadPlanProvenanceError(
                "download plan must contain exactly one non-empty plan_id"
            )
        plan_id = next(iter(plan_ids))
    manifest = manifest.copy()
    manifest["shard_id"] = pd.to_numeric(manifest["shard_id"], errors="coerce")
    if manifest["shard_id"].isna().any():
        raise DownloadPlanProvenanceError("download plan has invalid shard_id values")
    manifest["shard_id"] = manifest["shard_id"].astype(int)
    if (manifest["shard_id"] <= 0).any() or manifest["shard_id"].duplicated().any():
        raise DownloadPlanProvenanceError(
            "download plan shard_id values must be positive and unique"
        )

    definition_rows: list[dict[str, object]] = []
    completion_rows: list[dict[str, object]] = []
    shard_by_code: dict[str, int] = {}
    for row in manifest.sort_values("shard_id", kind="mergesort").to_dict("records"):
        shard_id = int(row["shard_id"])
        codes_path = _resolve_codes_file(row["codes_file"], path)
        if not codes_path.exists():
            raise DownloadPlanProvenanceError(
                f"download plan codes file is missing: {codes_path}"
            )
        codes = _read_codes_file(codes_path)
        declared_codes = _as_positive_int(row["codes"], "codes")
        if len(codes) != declared_codes:
            raise DownloadPlanProvenanceError(
                f"download plan shard {shard_id} code count mismatch: "
                f"declared={declared_codes}, actual={len(codes)}"
            )
        if codes[0] != str(row["first_code"]) or codes[-1] != str(row["last_code"]):
            raise DownloadPlanProvenanceError(
                f"download plan shard {shard_id} first/last code metadata is stale"
            )
        for source_code in codes:
            if source_code in shard_by_code:
                raise DownloadPlanProvenanceError(
                    f"download plan contains duplicate source_code: {source_code}"
                )
            shard_by_code[source_code] = shard_id
        definition_rows.append(
            {
                "shard_id": shard_id,
                "codes": codes,
                "codes_file": str(codes_path),
                "codes_file_sha256": _sha256(codes_path),
                "status_at_plan_time": str(row["status_at_plan_time"]),
            }
        )
        if require_complete:
            try:
                complete_codes = int(row["complete_codes"])
                remaining_codes = int(row["remaining_codes"])
            except (TypeError, ValueError) as exc:
                raise DownloadPlanProvenanceError(
                    f"download plan shard {shard_id} has invalid completion counts"
                ) from exc
            status = str(row["status"])
            if complete_codes < 0 or remaining_codes < 0:
                raise DownloadPlanProvenanceError(
                    f"download plan shard {shard_id} has negative completion counts"
                )
            if complete_codes + remaining_codes != declared_codes:
                raise DownloadPlanProvenanceError(
                    f"download plan shard {shard_id} completion counts do not sum to codes"
                )
            if status == "complete" and remaining_codes != 0:
                raise DownloadPlanProvenanceError(
                    f"download plan shard {shard_id} claims complete with remaining codes"
                )
            completion_rows.append(
                {
                    "shard_id": shard_id,
                    "codes": declared_codes,
                    "complete_codes": complete_codes,
                    "remaining_codes": remaining_codes,
                    "status": status,
                }
            )

    plan_codes = set(shard_by_code)
    if expected_codes is not None:
        unexpected_codes = sorted(plan_codes - set(expected_codes))
        if unexpected_codes:
            preview = ", ".join(unexpected_codes[:10])
            raise DownloadPlanProvenanceError(
                f"download plan contains codes outside frozen universe: {preview}"
            )

    missing_universe_path = path.parent / "missing_universe.csv"
    if not missing_universe_path.exists():
        raise DownloadPlanProvenanceError(
            f"download plan missing-universe definition is missing: {missing_universe_path}"
        )
    try:
        missing_universe = pd.read_csv(
            missing_universe_path,
            dtype=str,
            keep_default_na=False,
        )
    except (OSError, pd.errors.ParserError) as exc:
        raise DownloadPlanProvenanceError(
            f"download plan missing-universe definition is unreadable: {missing_universe_path}"
        ) from exc
    if "source_code" not in missing_universe.columns:
        raise DownloadPlanProvenanceError(
            "download plan missing-universe definition lacks source_code"
        )
    missing_codes = missing_universe["source_code"].astype(str).tolist()
    if len(missing_codes) != len(set(missing_codes)) or set(missing_codes) != plan_codes:
        raise DownloadPlanProvenanceError(
            "download plan shard codes do not match missing_universe.csv"
        )

    definition_payload = {
        "plan_id": plan_id,
        "manifest_path": str(path),
        "missing_universe_path": str(missing_universe_path.resolve()),
        "missing_universe_sha256": _sha256(missing_universe_path),
        "shards": definition_rows,
    }
    if require_complete:
        incomplete = [
            row
            for row in completion_rows
            if row["status"] != "complete"
            or row["remaining_codes"] != 0
            or row["complete_codes"] != row["codes"]
        ]
        if incomplete:
            preview = ", ".join(str(row["shard_id"]) for row in incomplete[:10])
            raise DownloadPlanProvenanceError(
                f"download plan is incomplete; incomplete shards: {preview}"
            )
    completion_payload = {"plan_id": plan_id, "shards": completion_rows}

    isolation_path = path.parent / ISOLATION_MANIFEST_NAME
    isolation_records: list[dict[str, object]] = []
    isolation_file_sha256 = ""
    if isolation_path.exists():
        isolation_file_sha256 = _sha256(isolation_path)
        try:
            isolation = pd.read_csv(
                isolation_path,
                dtype=str,
                keep_default_na=False,
            )
        except (OSError, pd.errors.ParserError) as exc:
            raise DownloadPlanProvenanceError(
                f"download isolation manifest is unreadable: {isolation_path}"
            ) from exc
        missing_isolation_columns = sorted(set(ISOLATION_COLUMNS) - set(isolation.columns))
        if missing_isolation_columns:
            raise DownloadPlanProvenanceError(
                "download isolation manifest is missing fields: "
                + ",".join(missing_isolation_columns)
            )
        isolation = isolation.loc[:, list(ISOLATION_COLUMNS)].copy()
        if isolation["source_code"].duplicated().any():
            raise DownloadPlanProvenanceError(
                "download isolation manifest has duplicate source_code rows"
            )
        for record in isolation.to_dict("records"):
            source_code = str(record["source_code"])
            if str(record["plan_id"]) != plan_id:
                raise DownloadPlanProvenanceError(
                    "download isolation manifest plan_id does not match download plan"
                )
            expected_shard = shard_by_code.get(source_code)
            try:
                shard_id = int(record["shard_id"])
                attempt_count = int(record["attempt_count"])
                last_exit_code = int(record["last_exit_code"])
            except (TypeError, ValueError) as exc:
                raise DownloadPlanProvenanceError(
                    f"download isolation record has invalid numeric fields: {source_code}"
                ) from exc
            if expected_shard is None or shard_id != expected_shard:
                raise DownloadPlanProvenanceError(
                    f"download isolation record is outside its plan shard: {source_code}"
                )
            if attempt_count <= 0:
                raise DownloadPlanProvenanceError(
                    f"download isolation record has invalid attempt_count: {source_code}"
                )
            status = str(record["status"])
            if status not in {"isolated", "resolved"}:
                raise DownloadPlanProvenanceError(
                    f"download isolation record has invalid status: {source_code}={status}"
                )
            if require_complete and status != "resolved":
                raise DownloadPlanProvenanceError(
                    f"download isolation retry is unresolved: {source_code}"
                )
            if status == "resolved" and not str(record["resolved_at"]):
                raise DownloadPlanProvenanceError(
                    f"resolved download isolation record lacks resolved_at: {source_code}"
                )
            normalized = {column: str(record[column]) for column in ISOLATION_COLUMNS}
            normalized["shard_id"] = shard_id
            normalized["attempt_count"] = attempt_count
            normalized["last_exit_code"] = last_exit_code
            isolation_records.append(normalized)
        isolation_records.sort(key=lambda item: (int(item["shard_id"]), str(item["source_code"])))

    return {
        "plan_id": plan_id,
        "manifest_path": str(path),
        "manifest_sha256_at_snapshot": _sha256(path),
        "definition_sha256": _canonical_sha256(definition_payload),
        "completion_sha256": _canonical_sha256(completion_payload),
        "shards": int(len(definition_rows)),
        "planned_codes": int(len(plan_codes)),
        "complete_codes": int(sum(int(row["complete_codes"]) for row in completion_rows)),
        "remaining_codes": int(sum(int(row["remaining_codes"]) for row in completion_rows)),
        "status": "complete" if require_complete else "unchecked",
        "missing_universe_path": str(missing_universe_path.resolve()),
        "missing_universe_sha256": _sha256(missing_universe_path),
        "isolation_manifest_path": str(isolation_path.resolve()),
        "isolation_manifest_sha256_at_snapshot": isolation_file_sha256,
        "isolation_records_sha256": _canonical_sha256(isolation_records),
        "isolation_records": int(len(isolation_records)),
        "resolved_isolation_records": int(
            sum(record["status"] == "resolved" for record in isolation_records)
        ),
    }


def source_code_from_daily_path(path: Path) -> str:
    exchange, symbol = path.stem.split("_", 1)
    return f"{exchange}.{symbol}"


def existing_daily_codes(root: Path, table: str) -> set[str]:
    table_root = root / "raw" / "baostock" / table
    return {
        source_code_from_daily_path(path)
        for path in table_root.glob("*.parquet")
        if not path.name.startswith(".") and not path.name.startswith("._")
    }


def missing_daily_codes(data_root: Path, universe: pd.DataFrame) -> pd.DataFrame:
    frame = universe.copy()
    frame["source_code"] = frame["source_code"].astype(str)
    integrity = inspect_daily_pairs(data_root, frame["source_code"].tolist())
    results = [integrity[code] for code in frame["source_code"]]
    frame["has_daily_raw"] = [result.raw_valid for result in results]
    frame["has_daily_qfq"] = [result.qfq_valid for result in results]
    frame["daily_keys_match"] = [result.keys_match for result in results]
    frame["download_complete"] = [result.complete for result in results]
    frame["integrity_errors"] = [result.error_summary for result in results]
    return frame.loc[~frame["download_complete"]].sort_values("source_code").reset_index(drop=True)


def _atomic_write_lines(path: Path, codes: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text("".join(f"{code}\n" for code in codes), encoding="utf-8")
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


def write_download_plan(
    data_root: Path,
    universe_path: Path,
    shard_size: int = 50,
) -> tuple[Path, Path, pd.DataFrame]:
    if shard_size <= 0:
        raise ValueError("shard_size must be positive")
    universe = pd.read_csv(universe_path)
    missing = missing_daily_codes(data_root, universe)
    identity = "\n".join(missing["source_code"].astype(str)) + f"\nshard_size={shard_size}\n"
    plan_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    plan_root = data_root / "00_meta" / "download_plans" / f"phase2_free_{plan_id}"
    shard_root = plan_root / "shards"
    records: list[dict] = []
    codes = missing["source_code"].astype(str).tolist()
    for offset in range(0, len(codes), shard_size):
        shard_codes = codes[offset : offset + shard_size]
        shard_id = offset // shard_size + 1
        shard_path = shard_root / f"shard_{shard_id:04d}.txt"
        _atomic_write_lines(shard_path, shard_codes)
        records.append(
            {
                "plan_id": plan_id,
                "shard_id": shard_id,
                "codes": len(shard_codes),
                "first_code": shard_codes[0],
                "last_code": shard_codes[-1],
                "codes_file": str(shard_path),
                "status_at_plan_time": "pending",
            }
        )
    manifest = pd.DataFrame(records, columns=PLAN_COLUMNS)
    manifest_path = plan_root / "download_plan.csv"
    _atomic_write_csv(manifest_path, manifest)
    missing_path = plan_root / "missing_universe.csv"
    _atomic_write_csv(missing_path, missing)
    return manifest_path, missing_path, manifest


def refresh_download_plan(data_root: Path, manifest_path: Path) -> pd.DataFrame:
    manifest = pd.read_csv(manifest_path)
    codes_by_shard: list[list[str]] = []
    all_codes: list[str] = []
    for codes_file in manifest["codes_file"]:
        codes = [line.strip() for line in Path(codes_file).read_text(encoding="utf-8").splitlines() if line.strip()]
        codes_by_shard.append(codes)
        all_codes.extend(codes)
    integrity = inspect_daily_pairs(data_root, all_codes)
    complete_counts: list[int] = []
    remaining_counts: list[int] = []
    statuses: list[str] = []
    for codes in codes_by_shard:
        complete = sum(integrity[code].complete for code in codes)
        remaining = len(codes) - complete
        complete_counts.append(complete)
        remaining_counts.append(remaining)
        statuses.append("complete" if remaining == 0 else ("partial" if complete else "pending"))
    manifest["complete_codes"] = complete_counts
    manifest["remaining_codes"] = remaining_counts
    manifest["status"] = statuses
    manifest["checked_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_write_csv(manifest_path, manifest)
    return manifest

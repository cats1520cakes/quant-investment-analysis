from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
import shutil
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from quant_proof.free_sources.baostock_adapter import (
    FreeRealConfig,
    iter_existing_daily_files,
    load_config,
    select_stock_universe,
)
from quant_proof.free_sources.daily_integrity import CACHE_VERSION as DAILY_INTEGRITY_CACHE_VERSION
from quant_proof.free_sources.daily_integrity import inspect_daily_pairs
from quant_proof.free_sources.download_planner import (
    DownloadPlanProvenanceError,
    read_download_plan_provenance,
    refresh_download_plan,
)
from quant_proof.realdata.derived_limits import add_derived_limit_prices
from quant_proof.realdata.derived_market_cap import add_circ_mv_approx


FREE_PANEL_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "ts_code",
    "source_code",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "turnover_rate",
    "pct_chg",
    "pe_ttm",
    "pb",
    "ps_ttm",
    "pcf_ttm",
    "adj_close_for_signal",
    "corporate_action_share_factor",
    "corporate_action_source",
    "trade_status",
    "is_suspended",
    "is_st",
    "list_date",
    "delist_date",
    "list_status",
    "is_last_observation",
    "delisting_exit_required",
    "terminal_value_source",
    "listing_days",
    "listing_trading_days",
    "board",
    "limit_pct",
    "up_limit",
    "down_limit",
    "up_limit_source",
    "down_limit_source",
    "circ_mv_approx",
    "market_cap_source",
    "data_tier",
)
PANEL_MANIFEST_SCHEMA_VERSION = 3
RAW_QFQ_INPUT_SCHEMA_VERSION = 1
DATE_COVERAGE_POLICY = "listing_inclusive_delist_exclusive_research_end_inclusive_v1"
CORPORATE_ACTION_MODEL = "previous_raw_close_over_official_preclose_share_factor"


class FreePanelBuildError(RuntimeError):
    """Raised when free-real raw data is unavailable or inconsistent."""


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


def _panel_config_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    payload = {
        key: raw.get(key)
        for key in ["data_tier", "primary_source", "data_root", "date_range", "download", "panel_build", "sources", "field_sources"]
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def panel_manifest_path(panel_path: Path) -> Path:
    return panel_path.with_name(f"{panel_path.stem}.manifest.json")


def _frozen_universe_path(config: FreeRealConfig) -> Path | None:
    value = config.raw.get("download", {}).get("frozen_universe_path")
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else config.data_root / path


def _configured_download_plan_path(config: FreeRealConfig) -> Path | None:
    download = config.raw.get("download", {})
    if not isinstance(download, dict):
        return None
    value = next(
        (
            download.get(key)
            for key in ("download_plan_path", "plan_manifest_path", "plan_path")
            if download.get(key)
        ),
        None,
    )
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else config.data_root / path


def _resolve_download_plan_path(config: FreeRealConfig) -> Path:
    configured = _configured_download_plan_path(config)
    if configured is not None:
        if not configured.exists():
            raise FreePanelBuildError(f"configured download plan is missing: {configured}")
        return configured.resolve()
    plan_root = config.data_root / "00_meta" / "download_plans"
    candidates = sorted(
        path.resolve()
        for path in plan_root.glob("phase2_free_*/download_plan.csv")
        if not (path.parent / "SUPERSEDED").exists()
    )
    if not candidates:
        raise FreePanelBuildError(
            "canonical frozen-universe panel requires a non-superseded download plan"
        )
    if len(candidates) != 1:
        preview = ", ".join(str(path) for path in candidates[:5])
        raise FreePanelBuildError(
            "canonical frozen-universe panel has ambiguous active download plans; "
            f"configure download.download_plan_path explicitly: {preview}"
        )
    return candidates[0]


def _download_plan_provenance(
    config: FreeRealConfig,
    selected_codes: set[str],
    *,
    refresh: bool,
) -> dict[str, object]:
    if _frozen_universe_path(config) is None:
        return {"required": False}
    path = _resolve_download_plan_path(config)
    try:
        if refresh:
            refresh_download_plan(config.data_root, path)
        provenance = read_download_plan_provenance(
            path,
            expected_codes=selected_codes,
            require_complete=True,
        )
    except (DownloadPlanProvenanceError, OSError, KeyError, ValueError) as exc:
        raise FreePanelBuildError(f"download-plan provenance gate failed: {exc}") from exc
    return {"required": True, **provenance}


def _assert_download_plan_binding(
    expected: dict[str, object],
    actual: dict[str, object],
) -> None:
    semantic_fields = (
        "required",
        "plan_id",
        "manifest_path",
        "definition_sha256",
        "completion_sha256",
        "shards",
        "planned_codes",
        "complete_codes",
        "remaining_codes",
        "status",
        "missing_universe_path",
        "missing_universe_sha256",
        "isolation_manifest_path",
        "isolation_records_sha256",
        "isolation_records",
        "resolved_isolation_records",
    )
    mismatched = [field for field in semantic_fields if expected.get(field) != actual.get(field)]
    if mismatched:
        raise FreePanelBuildError(
            "download-plan provenance changed after panel build: " + ", ".join(mismatched)
        )


def _build_raw_qfq_input_provenance(
    pairs: list[tuple[Path, Path]],
    integrity: dict[str, object],
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for raw_path, qfq_path in pairs:
        source_code = raw_path.stem.replace("_", ".", 1)
        result = integrity[source_code]
        records.append(
            {
                "source_code": source_code,
                "raw_path": str(raw_path.resolve()),
                "raw_sha256": _sha256(raw_path),
                "raw_bytes": int(raw_path.stat().st_size),
                "raw_rows": int(result.raw.rows),
                "qfq_path": str(qfq_path.resolve()),
                "qfq_sha256": _sha256(qfq_path),
                "qfq_bytes": int(qfq_path.stat().st_size),
                "qfq_rows": int(result.qfq.rows),
            }
        )
    records.sort(key=lambda record: str(record["source_code"]))
    return {
        "schema_version": RAW_QFQ_INPUT_SCHEMA_VERSION,
        "aggregate_sha256": _canonical_sha256(records),
        "symbols": int(len(records)),
        "raw_files": int(len(records)),
        "qfq_files": int(len(records)),
        "records": records,
    }


def _validate_raw_qfq_input_provenance(
    payload: object,
    *,
    expected_codes: set[str] | None = None,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise FreePanelBuildError("panel provenance raw/qfq input inventory is missing")
    if int(payload.get("schema_version", -1)) != RAW_QFQ_INPUT_SCHEMA_VERSION:
        raise FreePanelBuildError("panel provenance raw/qfq input inventory uses an unsupported schema")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise FreePanelBuildError("panel provenance raw/qfq input inventory is empty")
    required = {
        "source_code",
        "raw_path",
        "raw_sha256",
        "raw_bytes",
        "raw_rows",
        "qfq_path",
        "qfq_sha256",
        "qfq_bytes",
        "qfq_rows",
    }
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise FreePanelBuildError("panel provenance raw/qfq input record is malformed")
        missing = sorted(required - set(record))
        if missing:
            raise FreePanelBuildError(
                f"panel provenance raw/qfq input record is missing fields: {missing}"
            )
        source_code = str(record["source_code"])
        if not source_code or source_code in seen:
            raise FreePanelBuildError(
                f"panel provenance raw/qfq input inventory has duplicate/blank code: {source_code}"
            )
        seen.add(source_code)
        normalized_record = {
            "source_code": source_code,
            "raw_path": str(Path(str(record["raw_path"])).expanduser().resolve()),
            "raw_sha256": str(record["raw_sha256"]),
            "raw_bytes": int(record["raw_bytes"]),
            "raw_rows": int(record["raw_rows"]),
            "qfq_path": str(Path(str(record["qfq_path"])).expanduser().resolve()),
            "qfq_sha256": str(record["qfq_sha256"]),
            "qfq_bytes": int(record["qfq_bytes"]),
            "qfq_rows": int(record["qfq_rows"]),
        }
        for table in ("raw", "qfq"):
            input_path = Path(str(normalized_record[f"{table}_path"]))
            if not input_path.exists():
                raise FreePanelBuildError(
                    f"panel provenance {table} input is missing: {input_path}"
                )
            if int(input_path.stat().st_size) != int(normalized_record[f"{table}_bytes"]):
                raise FreePanelBuildError(
                    f"panel provenance {table} input size changed: {source_code}"
                )
            if _sha256(input_path) != str(normalized_record[f"{table}_sha256"]):
                raise FreePanelBuildError(
                    f"panel provenance {table} input hash changed: {source_code}"
                )
        normalized.append(normalized_record)
    normalized.sort(key=lambda record: str(record["source_code"]))
    if expected_codes is not None and seen != set(expected_codes):
        missing_codes = sorted(set(expected_codes) - seen)
        extra_codes = sorted(seen - set(expected_codes))
        raise FreePanelBuildError(
            "panel provenance raw/qfq input universe mismatch: "
            f"missing={missing_codes[:10]}, extra={extra_codes[:10]}"
        )
    if int(payload.get("symbols", -1)) != len(normalized):
        raise FreePanelBuildError("panel provenance raw/qfq input symbol count is stale")
    if int(payload.get("raw_files", -1)) != len(normalized) or int(
        payload.get("qfq_files", -1)
    ) != len(normalized):
        raise FreePanelBuildError("panel provenance raw/qfq input file counts are stale")
    if str(payload.get("aggregate_sha256", "")) != _canonical_sha256(normalized):
        raise FreePanelBuildError("panel provenance raw/qfq aggregate hash is stale")
    return {**payload, "records": normalized}


def _write_panel_manifest(
    config: FreeRealConfig,
    panel_path: Path,
    rows: int,
    symbols: int,
    date_min: str,
    date_max: str,
    *,
    trade_calendar_path: Path,
    download_plan: dict[str, object],
    raw_qfq_inputs: dict[str, object],
    date_coverage: dict[str, object],
) -> Path:
    universe_path = _frozen_universe_path(config)
    download_config = config.raw.get("download", {}) if isinstance(config.raw.get("download", {}), dict) else {}
    payload = {
        "schema_version": PANEL_MANIFEST_SCHEMA_VERSION,
        "data_tier": "free_real",
        "panel_path": str(panel_path.resolve()),
        "panel_sha256": _sha256(panel_path),
        "rows": int(rows),
        "symbols": int(symbols),
        "date_min": str(date_min),
        "date_max": str(date_max),
        "expected_symbols": int(config.raw.get("panel_build", {}).get("expected_symbols", 0)),
        "universe_scope": str(download_config.get("universe_scope", "unspecified")),
        "universe_start_date": str(download_config.get("universe_start_date", config.start_date)),
        "universe_end_date": str(download_config.get("universe_end_date", config.end_date)),
        "universe_path": str(universe_path.resolve()) if universe_path else "",
        "universe_sha256": _sha256(universe_path) if universe_path and universe_path.exists() else "",
        "config_path": str(config.path.resolve()),
        "config_sha256": _sha256(config.path) if config.path.exists() else "",
        "panel_config_sha256": _panel_config_sha256(config.path),
        "trade_calendar": {
            "path": str(trade_calendar_path.resolve()),
            "sha256": _sha256(trade_calendar_path),
        },
        "download_plan": download_plan,
        "raw_qfq_inputs": raw_qfq_inputs,
        "date_coverage": date_coverage,
        "corporate_action_model": CORPORATE_ACTION_MODEL,
        "daily_pair_integrity_cache_version": DAILY_INTEGRITY_CACHE_VERSION,
        "builder_source_sha256": _sha256(Path(__file__)),
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }
    path = panel_manifest_path(panel_path)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return path


def validate_panel_manifest(
    panel_path: Path,
    expected_symbols: int = 0,
    config_path: Path | None = None,
) -> dict[str, object]:
    panel_path = Path(panel_path)
    if not panel_path.exists():
        raise FreePanelBuildError(f"panel is missing: {panel_path}")
    path = panel_manifest_path(panel_path)
    if not path.exists():
        raise FreePanelBuildError(f"panel provenance manifest is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("data_tier") != "free_real":
        raise FreePanelBuildError("panel provenance manifest has wrong data tier")
    if int(payload.get("schema_version", -1)) != PANEL_MANIFEST_SCHEMA_VERSION:
        raise FreePanelBuildError("panel provenance manifest uses an unsupported schema")
    if Path(str(payload.get("panel_path", ""))).resolve() != panel_path.resolve():
        raise FreePanelBuildError("panel provenance manifest points to a different panel")
    if str(payload.get("panel_sha256", "")) != _sha256(panel_path):
        raise FreePanelBuildError("panel provenance hash does not match panel contents")
    if int(payload.get("daily_pair_integrity_cache_version", -1)) != DAILY_INTEGRITY_CACHE_VERSION:
        raise FreePanelBuildError("panel provenance uses a stale daily-pair integrity version")
    if str(payload.get("corporate_action_model", "")) != CORPORATE_ACTION_MODEL:
        raise FreePanelBuildError("panel provenance uses a stale corporate-action model")
    if str(payload.get("builder_source_sha256", "")) != _sha256(Path(__file__)):
        raise FreePanelBuildError("panel provenance uses a stale panel builder")
    universe_path_value = str(payload.get("universe_path", ""))
    universe: pd.DataFrame | None = None
    universe_codes: set[str] | None = None
    if universe_path_value:
        universe_path = Path(universe_path_value)
        if not universe_path.exists() or str(payload.get("universe_sha256", "")) != _sha256(universe_path):
            raise FreePanelBuildError("panel provenance frozen-universe hash is missing or stale")
        universe = pd.read_csv(universe_path, dtype=str, keep_default_na=False)
        if "source_code" not in universe.columns or universe["source_code"].duplicated().any():
            raise FreePanelBuildError("panel provenance frozen universe has invalid source_code rows")
        universe_codes = set(universe["source_code"].astype(str))
    if config_path is not None:
        resolved_config = Path(config_path)
        expected_config_hash = str(payload.get("panel_config_sha256", ""))
        if not resolved_config.exists() or not expected_config_hash or expected_config_hash != _panel_config_sha256(resolved_config):
            raise FreePanelBuildError("panel provenance data-config hash is missing or stale")
    if expected_symbols and int(payload.get("symbols", 0)) != int(expected_symbols):
        raise FreePanelBuildError(
            f"panel provenance symbol count mismatch: expected={expected_symbols}, actual={payload.get('symbols')}"
        )
    if expected_symbols and str(payload.get("universe_scope", "")) != "point_in_time":
        raise FreePanelBuildError("full free-real panel provenance must use universe_scope=point_in_time")

    raw_qfq_inputs = _validate_raw_qfq_input_provenance(
        payload.get("raw_qfq_inputs"),
        expected_codes=universe_codes,
    )
    if int(raw_qfq_inputs["symbols"]) != int(payload.get("symbols", -1)):
        raise FreePanelBuildError("panel provenance input and panel symbol counts differ")

    calendar_payload = payload.get("trade_calendar")
    if not isinstance(calendar_payload, dict):
        raise FreePanelBuildError("panel provenance trade-calendar binding is missing")
    trade_calendar_path = Path(str(calendar_payload.get("path", "")))
    if not trade_calendar_path.exists() or str(calendar_payload.get("sha256", "")) != _sha256(
        trade_calendar_path
    ):
        raise FreePanelBuildError("panel provenance trade-calendar hash is missing or stale")
    trade_calendar = pd.read_parquet(trade_calendar_path)

    download_plan = payload.get("download_plan")
    if not isinstance(download_plan, dict):
        raise FreePanelBuildError("panel provenance download-plan binding is missing")
    if bool(download_plan.get("required")):
        try:
            current_download_plan = {
                "required": True,
                **read_download_plan_provenance(
                    str(download_plan.get("manifest_path", "")),
                    expected_codes=universe_codes,
                    require_complete=True,
                ),
            }
        except (DownloadPlanProvenanceError, OSError, KeyError, ValueError) as exc:
            raise FreePanelBuildError(
                f"panel provenance download-plan validation failed: {exc}"
            ) from exc
        _assert_download_plan_binding(download_plan, current_download_plan)
    elif universe_path_value:
        raise FreePanelBuildError(
            "canonical frozen-universe panel provenance lacks a required download plan"
        )

    panel_dates = pd.read_parquet(
        panel_path,
        columns=["trade_date", "source_code", "trade_status", "list_date", "delist_date"],
    )
    actual_rows = int(len(panel_dates))
    actual_symbols = int(panel_dates["source_code"].astype(str).nunique())
    panel_codes = set(panel_dates["source_code"].astype(str))
    input_codes = {
        str(record["source_code"])
        for record in raw_qfq_inputs["records"]
    }
    if panel_codes != input_codes:
        raise FreePanelBuildError("panel provenance panel and raw/qfq input symbols differ")
    actual_date_min = str(panel_dates["trade_date"].astype(str).min())
    actual_date_max = str(panel_dates["trade_date"].astype(str).max())
    if (
        actual_rows != int(payload.get("rows", -1))
        or actual_symbols != int(payload.get("symbols", -1))
        or actual_date_min != str(payload.get("date_min", ""))
        or actual_date_max != str(payload.get("date_max", ""))
    ):
        raise FreePanelBuildError("panel provenance row/symbol/date metadata is stale")
    coverage_universe = (
        universe
        if universe is not None
        else panel_dates.loc[:, ["source_code", "list_date", "delist_date"]].drop_duplicates(
            "source_code"
        )
    )
    actual_coverage = _validate_daily_date_coverage(
        coverage_universe,
        panel_dates,
        trade_calendar,
        str(payload.get("universe_start_date", "")),
        str(payload.get("universe_end_date", "")),
    )
    expected_coverage = payload.get("date_coverage")
    if not isinstance(expected_coverage, dict) or str(
        expected_coverage.get("summary_sha256", "")
    ) != str(actual_coverage.get("summary_sha256", "")):
        raise FreePanelBuildError("panel provenance date-coverage summary is missing or stale")
    return payload


def _read_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FreePanelBuildError(f"missing required free-real file: {path}")
    frame = pd.read_parquet(path)
    if frame.empty:
        raise FreePanelBuildError(f"required free-real file is empty: {path}")
    return frame


def _read_daily_dir(config: FreeRealConfig, table: str) -> pd.DataFrame:
    files = list(iter_existing_daily_files(config, table))
    if not files:
        raise FreePanelBuildError(f"missing BaoStock {table} parquet files under {config.data_root / 'raw/baostock' / table}")
    frames = [pd.read_parquet(path) for path in files]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        raise FreePanelBuildError(f"all BaoStock {table} parquet files are empty")
    return pd.concat(frames, ignore_index=True, sort=False)


def normalize_date_column(frame: pd.DataFrame, column: str) -> pd.Series:
    return (
        frame[column]
        .astype("string")
        .str.strip()
        .str.replace("-", "", regex=False)
        .str.replace("/", "", regex=False)
        .str.slice(0, 8)
    )


def _prepare_trade_calendar(
    trade_calendar: pd.DataFrame,
    research_start_date: str,
    research_end_date: str,
) -> pd.DataFrame:
    required = {"trade_date", "is_open"}
    missing = sorted(required - set(trade_calendar.columns))
    if missing:
        raise FreePanelBuildError(f"trade calendar missing columns: {missing}")
    calendar = trade_calendar.loc[:, ["trade_date", "is_open"]].copy()
    calendar["trade_date"] = normalize_date_column(calendar, "trade_date")
    parsed_dates = pd.to_datetime(calendar["trade_date"], format="%Y%m%d", errors="coerce")
    calendar["is_open"] = pd.to_numeric(calendar["is_open"], errors="coerce")
    if parsed_dates.isna().any() or calendar["is_open"].isna().any():
        raise FreePanelBuildError("trade calendar contains invalid dates or is_open values")
    if not calendar["is_open"].isin({0, 1}).all():
        raise FreePanelBuildError("trade calendar is_open values must be binary")
    if calendar["trade_date"].duplicated().any():
        raise FreePanelBuildError("trade calendar contains duplicate dates")
    try:
        start = pd.Timestamp(str(research_start_date))
        end = pd.Timestamp(str(research_end_date))
    except ValueError as exc:
        raise FreePanelBuildError("research date range contains invalid dates") from exc
    if start > end:
        raise FreePanelBuildError("research start date is after research end date")
    expected_calendar_dates = set(pd.date_range(start, end, freq="D").strftime("%Y%m%d"))
    available_dates = set(calendar["trade_date"].astype(str))
    missing_calendar_dates = sorted(expected_calendar_dates - available_dates)
    if missing_calendar_dates:
        preview = ", ".join(missing_calendar_dates[:10])
        raise FreePanelBuildError(
            "trade calendar does not cover the full research date range; "
            f"missing={len(missing_calendar_dates)} ({preview})"
        )
    return calendar.sort_values("trade_date", kind="mergesort").reset_index(drop=True)


def _validate_daily_date_coverage(
    stock_basic: pd.DataFrame,
    daily_raw: pd.DataFrame,
    trade_calendar: pd.DataFrame,
    research_start_date: str,
    research_end_date: str,
) -> dict[str, object]:
    required_daily = {"trade_date", "source_code", "trade_status"}
    missing_daily = sorted(required_daily - set(daily_raw.columns))
    if missing_daily:
        raise FreePanelBuildError(
            f"daily raw data missing date-coverage columns: {missing_daily}"
        )
    required_basic = {"source_code", "list_date", "delist_date"}
    missing_basic = sorted(required_basic - set(stock_basic.columns))
    if missing_basic:
        raise FreePanelBuildError(
            f"stock universe missing date-coverage columns: {missing_basic}"
        )

    calendar = _prepare_trade_calendar(
        trade_calendar,
        research_start_date,
        research_end_date,
    )
    open_dates = calendar.loc[calendar["is_open"].eq(1), "trade_date"].astype(str).tolist()
    open_date_set = set(open_dates)
    raw = daily_raw.loc[:, ["trade_date", "source_code", "trade_status"]].copy()
    raw["trade_date"] = normalize_date_column(raw, "trade_date")
    raw["source_code"] = raw["source_code"].astype(str)
    raw["trade_status"] = pd.to_numeric(raw["trade_status"], errors="coerce")
    if raw["trade_date"].isna().any() or not raw["trade_date"].str.fullmatch(
        r"\d{8}", na=False
    ).all():
        raise FreePanelBuildError("daily raw data has invalid trade_date values")
    if raw["trade_status"].isna().any() or not raw["trade_status"].isin({0, 1}).all():
        raise FreePanelBuildError("daily raw trade_status values must be complete and binary")
    if raw.duplicated(["trade_date", "source_code"]).any():
        raise FreePanelBuildError("daily raw data has duplicate date/source_code coverage keys")

    universe = stock_basic.loc[:, ["source_code", "list_date", "delist_date"]].copy()
    universe["source_code"] = universe["source_code"].astype(str)
    universe["list_date"] = normalize_date_column(universe, "list_date")
    universe["delist_date"] = normalize_date_column(universe, "delist_date")
    if universe["source_code"].duplicated().any():
        raise FreePanelBuildError("stock universe has duplicate source_code rows")
    rows_by_code = {
        str(source_code): rows
        for source_code, rows in raw.groupby("source_code", sort=False)
    }
    observed_codes = set(rows_by_code)
    metadata_codes = set(universe["source_code"])
    unknown_codes = sorted(observed_codes - metadata_codes)
    if unknown_codes:
        raise FreePanelBuildError(
            f"daily raw coverage contains codes outside stock universe: {unknown_codes[:10]}"
        )
    universe = universe.loc[universe["source_code"].isin(observed_codes)].copy()
    if universe.empty:
        raise FreePanelBuildError("daily raw coverage has no stock-universe rows")

    start = str(research_start_date)
    end = str(research_end_date)
    expected_open_rows = 0
    observed_rows = 0
    non_trading_rows = 0
    delisting_terminal_rows = 0
    coverage_failures: list[str] = []
    for row in universe.itertuples(index=False):
        source_code = str(row.source_code)
        list_date = "" if pd.isna(row.list_date) else str(row.list_date)
        delist_date = "" if pd.isna(row.delist_date) else str(row.delist_date)
        if not list_date or not pd.notna(
            pd.to_datetime(list_date, format="%Y%m%d", errors="coerce")
        ):
            coverage_failures.append(f"{source_code}:invalid_list_date={list_date}")
            continue
        active_start = max(start, list_date)
        delists_in_window = bool(delist_date and delist_date <= end)
        expected = {
            date
            for date in open_dates
            if date >= active_start
            and (date < delist_date if delists_in_window else date <= end)
        }
        rows = rows_by_code[source_code]
        observed = set(rows["trade_date"].astype(str))
        missing = sorted(expected - observed)
        if missing:
            coverage_failures.append(
                f"{source_code}:missing={len(missing)}[{missing[0]}..{missing[-1]}]"
            )
            continue
        before_listing = sorted(date for date in observed if date < active_start)
        after_research = sorted(date for date in observed if date > end)
        after_delisting = sorted(
            date
            for date in observed
            if delists_in_window and date > delist_date
        )
        if before_listing or after_research or after_delisting:
            coverage_failures.append(
                f"{source_code}:outside_active_interval="
                f"{(before_listing + after_research + after_delisting)[:5]}"
            )
            continue
        closed_dates = sorted(observed - open_date_set)
        illegal_closed = []
        for date in closed_dates:
            status = rows.loc[rows["trade_date"].eq(date), "trade_status"].iloc[0]
            if not (delists_in_window and date == delist_date and status == 0):
                illegal_closed.append(date)
        if illegal_closed:
            coverage_failures.append(
                f"{source_code}:unexpected_closed_dates={illegal_closed[:5]}"
            )
            continue
        expected_open_rows += len(expected)
        observed_rows += len(rows)
        non_trading_rows += int(rows["trade_status"].eq(0).sum())
        if delists_in_window:
            terminal = rows["trade_date"].eq(delist_date) & rows["trade_status"].eq(0)
            delisting_terminal_rows += int(terminal.sum())

    if coverage_failures:
        preview = "; ".join(coverage_failures[:10])
        raise FreePanelBuildError(
            "frozen-universe date coverage gate failed for "
            f"{len(coverage_failures)} symbols: {preview}"
        )
    summary: dict[str, object] = {
        "policy": DATE_COVERAGE_POLICY,
        "research_start_date": start,
        "research_end_date": end,
        "symbols": int(len(universe)),
        "calendar_open_dates": int(len(open_dates)),
        "expected_open_rows": int(expected_open_rows),
        "observed_rows": int(observed_rows),
        "non_trading_rows": int(non_trading_rows),
        "delisting_terminal_rows": int(delisting_terminal_rows),
        "passed": True,
    }
    summary["summary_sha256"] = _canonical_sha256(summary)
    return summary


def _aggregate_date_coverage(
    summaries: list[dict[str, object]],
) -> dict[str, object]:
    if not summaries:
        raise FreePanelBuildError("panel build produced no date-coverage summaries")
    stable_fields = ("policy", "research_start_date", "research_end_date", "calendar_open_dates")
    for field in stable_fields:
        if len({str(summary[field]) for summary in summaries}) != 1:
            raise FreePanelBuildError(f"panel date-coverage batches disagree on {field}")
    aggregate: dict[str, object] = {
        field: summaries[0][field]
        for field in stable_fields
    }
    for field in (
        "symbols",
        "expected_open_rows",
        "observed_rows",
        "non_trading_rows",
        "delisting_terminal_rows",
    ):
        aggregate[field] = int(sum(int(summary[field]) for summary in summaries))
    aggregate["passed"] = True
    aggregate["summary_sha256"] = _canonical_sha256(aggregate)
    return aggregate


def _prepare_stock_basic(config: FreeRealConfig, stock_basic: pd.DataFrame) -> pd.DataFrame:
    frozen_path = _frozen_universe_path(config)
    if frozen_path:
        if not frozen_path.exists():
            raise FreePanelBuildError(f"configured frozen universe is missing: {frozen_path}")
        frozen = pd.read_csv(frozen_path)
        required = {"ts_code", "source_code", "list_date", "delist_date", "list_status"}
        missing = sorted(required - set(frozen.columns))
        if missing:
            raise FreePanelBuildError(f"frozen universe missing columns: {missing}")
        if frozen["source_code"].astype(str).duplicated().any():
            raise FreePanelBuildError("frozen universe has duplicate source_code rows")
        for column, expected in [
            ("universe_scope", str(config.raw.get("download", {}).get("universe_scope", "point_in_time"))),
            ("universe_start_date", str(config.start_date)),
            ("universe_end_date", str(config.end_date)),
        ]:
            if column in frozen.columns and set(frozen[column].astype(str)) != {expected}:
                raise FreePanelBuildError(f"frozen universe {column} does not match config value {expected}")
        expected_symbols = int(config.raw.get("panel_build", {}).get("expected_symbols", 0))
        if expected_symbols and len(frozen) != expected_symbols:
            raise FreePanelBuildError(
                f"frozen universe symbol count mismatch: expected={expected_symbols}, actual={len(frozen)}"
            )
        stock_basic = frozen.copy()
    for frame, column in [(stock_basic, "list_date"), (stock_basic, "delist_date")]:
        if column in frame.columns:
            frame[column] = normalize_date_column(frame, column)
    universe_scope = str(config.raw.get("download", {}).get("universe_scope", "point_in_time"))
    stock_basic = select_stock_universe(
        stock_basic,
        universe_scope=universe_scope,
        universe_start_date=config.start_date,
        universe_end_date=config.end_date,
    )
    if stock_basic.empty:
        raise FreePanelBuildError(f"stock_basic has no A-share rows for universe_scope={universe_scope}")
    return stock_basic


def _infer_board_series(ts_code: pd.Series) -> pd.Series:
    symbol = ts_code.astype(str).str.split(".").str[0]
    values = np.select(
        [
            symbol.str.startswith("688"),
            symbol.str.startswith(("300", "301")),
            symbol.str.startswith(("43", "83", "87", "92")),
            symbol.str.startswith(("60", "00")),
        ],
        ["科创板", "创业板", "北交所", "主板"],
        default="",
    )
    return pd.Series(values, index=ts_code.index, dtype="string")


def _build_panel_frame(
    stock_basic: pd.DataFrame,
    daily_raw: pd.DataFrame,
    daily_qfq: pd.DataFrame,
    trade_calendar: pd.DataFrame,
    research_start_date: str,
    research_end_date: str,
    coverage_sink: list[dict[str, object]] | None = None,
) -> pd.DataFrame:
    daily_raw = daily_raw.copy()
    daily_qfq = daily_qfq.copy()

    selected_codes = set(stock_basic["source_code"].astype(str))
    if "source_code" not in daily_raw.columns or "source_code" not in daily_qfq.columns:
        raise FreePanelBuildError("raw/qfq daily frames must carry source_code")
    daily_raw = daily_raw.loc[daily_raw["source_code"].astype(str).isin(selected_codes)].copy()
    daily_qfq = daily_qfq.loc[daily_qfq["source_code"].astype(str).isin(selected_codes)].copy()

    daily_raw["trade_date"] = normalize_date_column(daily_raw, "trade_date")
    daily_qfq["trade_date"] = normalize_date_column(daily_qfq, "trade_date")
    key_columns = ["trade_date", "ts_code"]
    if daily_raw.empty or daily_qfq.empty:
        raise FreePanelBuildError("raw/qfq daily frames must both be non-empty")
    if daily_raw.duplicated(key_columns).any() or daily_qfq.duplicated(key_columns).any():
        raise FreePanelBuildError("raw/qfq daily frames contain duplicate keys")
    raw_keys = pd.MultiIndex.from_frame(daily_raw[key_columns])
    qfq_keys = pd.MultiIndex.from_frame(daily_qfq[key_columns])
    if len(raw_keys) != len(qfq_keys) or not raw_keys.sort_values().equals(qfq_keys.sort_values()):
        raise FreePanelBuildError(
            f"raw/qfq daily keys differ: raw_rows={len(raw_keys)}, qfq_rows={len(qfq_keys)}"
        )
    coverage = _validate_daily_date_coverage(
        stock_basic,
        daily_raw,
        trade_calendar,
        research_start_date,
        research_end_date,
    )
    if coverage_sink is not None:
        coverage_sink.append(coverage)

    panel = daily_raw.merge(
        daily_qfq[["trade_date", "ts_code", "adj_close_for_signal"]],
        on=["trade_date", "ts_code"],
        how="left",
    )
    panel = panel.merge(
        stock_basic[["ts_code", "source_code", "list_date", "delist_date", "list_status"]],
        on="ts_code",
        how="left",
        suffixes=("", "_basic"),
    )
    if "source_code_basic" in panel.columns:
        panel["source_code"] = panel["source_code"].fillna(panel["source_code_basic"])
        panel = panel.drop(columns=["source_code_basic"])
    panel = panel.dropna(subset=["list_date"]).copy()
    if panel.empty:
        raise FreePanelBuildError("BaoStock daily files did not match stock_basic stock rows; check type/list_status filters and source_code mapping")

    numeric = [
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "amount",
        "turnover_rate",
        "pct_chg",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "pcf_ttm",
        "adj_close_for_signal",
        "trade_status",
        "is_st_raw",
    ]
    for column in numeric:
        panel[column] = pd.to_numeric(panel[column], errors="coerce")

    panel = panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    trade_dt = pd.to_datetime(panel["trade_date"], format="%Y%m%d", errors="coerce")
    list_dt = pd.to_datetime(panel["list_date"], format="%Y%m%d", errors="coerce")
    if trade_dt.isna().any() or list_dt.isna().any():
        raise FreePanelBuildError("panel contains invalid trade_date or list_date values")
    panel["listing_days"] = (trade_dt - list_dt).dt.days.clip(lower=0).astype("Int64")
    research_start = pd.Timestamp(str(research_start_date))
    observed_days = panel.groupby("ts_code", sort=False).cumcount() + 1
    listed_before_research = list_dt.lt(research_start)
    pre_research_offset = np.zeros(len(panel), dtype=np.int64)
    if listed_before_research.any():
        list_values = list_dt.loc[listed_before_research].to_numpy(dtype="datetime64[D]")
        start_value = np.datetime64(research_start.date(), "D")
        pre_research_offset[listed_before_research.to_numpy()] = np.maximum(
            np.busday_count(list_values, start_value),
            0,
        )
    panel["listing_trading_days"] = (observed_days.to_numpy() + pre_research_offset).astype("int64")
    panel["board"] = _infer_board_series(panel["ts_code"])
    panel["is_suspended"] = (panel["trade_status"] != 1) | panel[["open", "high", "low", "close"]].isna().any(axis=1)
    panel["is_st"] = panel["is_st_raw"].fillna(0).astype(float).astype(int) == 1
    last_date = panel.groupby("ts_code", sort=False)["trade_date"].transform("max")
    panel["is_last_observation"] = panel["trade_date"].eq(last_date)
    delist_date = panel["delist_date"].astype("string")
    known_delisting = delist_date.notna() & delist_date.ne("") & delist_date.le(str(research_end_date))
    panel["delisting_exit_required"] = panel["is_last_observation"] & known_delisting
    panel["terminal_value_source"] = np.where(
        panel["delisting_exit_required"],
        "delisting_last_observation",
        "",
    )

    previous_close = panel.groupby("ts_code", sort=False)["close"].shift(1)
    official_pre_close = panel["pre_close"]
    valid_action = previous_close.gt(0.0) & official_pre_close.gt(0.0)
    share_factor = (previous_close / official_pre_close).where(valid_action, 1.0)
    invalid_factor = ~np.isfinite(share_factor) | share_factor.le(0.0) | share_factor.gt(100.0)
    if invalid_factor.any():
        sample = panel.loc[invalid_factor, ["trade_date", "ts_code", "close", "pre_close"]].iloc[0].to_dict()
        raise FreePanelBuildError(f"invalid corporate-action share factor: {sample}")
    share_factor = share_factor.where((share_factor - 1.0).abs().gt(1e-9), 1.0)
    panel["corporate_action_share_factor"] = share_factor.astype(float)
    panel["corporate_action_source"] = np.where(
        panel["corporate_action_share_factor"].ne(1.0),
        "raw_previous_close_over_official_preclose",
        "none",
    )

    panel = add_derived_limit_prices(panel)
    panel = add_circ_mv_approx(panel)
    panel["data_tier"] = "free_real"

    for column in FREE_PANEL_COLUMNS:
        if column not in panel.columns:
            panel[column] = pd.NA
    out = panel.loc[:, list(FREE_PANEL_COLUMNS)].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    validate_free_stock_panel(out)
    return out


def build_free_stock_panel(config: FreeRealConfig) -> pd.DataFrame:
    stock_basic = _prepare_stock_basic(
        config,
        _read_parquet(config.data_root / "raw/baostock/stock_basic.parquet"),
    )
    trade_calendar = _read_parquet(config.data_root / "raw/baostock/trade_calendar.parquet")
    daily_raw = _read_daily_dir(config, "daily_raw")
    daily_qfq = _read_daily_dir(config, "daily_qfq")
    return _build_panel_frame(
        stock_basic,
        daily_raw,
        daily_qfq,
        trade_calendar,
        research_start_date=config.start_date,
        research_end_date=config.end_date,
    )


def validate_free_stock_panel(panel: pd.DataFrame) -> None:
    missing = [column for column in FREE_PANEL_COLUMNS if column not in panel.columns]
    if missing:
        raise ValueError(f"free stock_panel missing columns: {missing}")
    if panel.empty:
        raise ValueError("free stock_panel is empty")
    duplicated = panel.duplicated(["trade_date", "ts_code"]).sum()
    if duplicated:
        raise ValueError(f"free stock_panel has duplicated keys: {int(duplicated)}")
    if not (panel["data_tier"] == "free_real").all():
        raise ValueError("free stock_panel must carry data_tier=free_real")
    adjusted_close = pd.to_numeric(panel["adj_close_for_signal"], errors="coerce")
    trade_status = pd.to_numeric(panel["trade_status"], errors="coerce")
    if trade_status.isna().any() or not trade_status.isin({0, 1}).all():
        raise ValueError("free stock_panel trade_status values must be complete and binary")
    invalid_signal = (
        (adjusted_close.notna() & adjusted_close.le(0.0))
        | (trade_status.eq(1) & adjusted_close.isna())
    )
    if invalid_signal.any():
        raise ValueError(
            "free stock_panel requires positive qfq signal coverage on executable rows"
        )
    share_factor = pd.to_numeric(panel["corporate_action_share_factor"], errors="coerce")
    if share_factor.isna().any() or share_factor.le(0.0).any() or share_factor.gt(100.0).any():
        raise ValueError("free stock_panel has invalid corporate_action_share_factor values")
    if panel["up_limit_source"].ne("derived").any() or panel["down_limit_source"].ne("derived").any():
        raise ValueError("free stock_panel limit sources must be derived")


def write_free_stock_panel(config: FreeRealConfig, panel: pd.DataFrame) -> Path:
    validate_free_stock_panel(panel)
    stock_basic = _prepare_stock_basic(
        config,
        _read_parquet(config.data_root / "raw/baostock/stock_basic.parquet"),
    )
    trade_calendar_path = config.data_root / "raw/baostock/trade_calendar.parquet"
    trade_calendar = _read_parquet(trade_calendar_path)
    panel_codes = set(panel["source_code"].astype(str))
    selected_codes = set(stock_basic["source_code"].astype(str))
    if panel_codes != selected_codes:
        raise FreePanelBuildError(
            "refusing to write panel whose symbols differ from the selected universe"
        )
    integrity = inspect_daily_pairs(config.data_root, sorted(selected_codes))
    pairs = _daily_file_pairs(config, selected_codes)
    incomplete = sorted(code for code in selected_codes if not integrity[code].complete)
    if incomplete:
        raise FreePanelBuildError(
            f"refusing to write panel with incomplete raw/qfq inputs: {incomplete[:10]}"
        )
    download_plan = _download_plan_provenance(
        config,
        selected_codes,
        refresh=True,
    )
    raw_qfq_inputs = _build_raw_qfq_input_provenance(pairs, integrity)
    date_coverage = _validate_daily_date_coverage(
        stock_basic,
        panel,
        trade_calendar,
        config.start_date,
        config.end_date,
    )
    out_dir = config.data_root / "processed" / "phase2_free"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "stock_panel.parquet"
    tmp_path = out_dir / f".stock_panel.{os.getpid()}.tmp.parquet"
    try:
        panel.to_parquet(tmp_path, index=False)
        _validate_raw_qfq_input_provenance(
            raw_qfq_inputs,
            expected_codes=selected_codes,
        )
        current_plan = _download_plan_provenance(
            config,
            selected_codes,
            refresh=False,
        )
        _assert_download_plan_binding(download_plan, current_plan)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    _write_panel_manifest(
        config,
        path,
        rows=len(panel),
        symbols=panel["ts_code"].astype(str).nunique(),
        date_min=str(panel["trade_date"].astype(str).min()),
        date_max=str(panel["trade_date"].astype(str).max()),
        trade_calendar_path=trade_calendar_path,
        download_plan=download_plan,
        raw_qfq_inputs=raw_qfq_inputs,
        date_coverage=date_coverage,
    )
    return path


def _daily_file_pairs(
    config: FreeRealConfig,
    selected_codes: set[str] | None = None,
) -> list[tuple[Path, Path]]:
    raw = {path.stem: path for path in iter_existing_daily_files(config, "daily_raw")}
    qfq = {path.stem: path for path in iter_existing_daily_files(config, "daily_qfq")}
    if selected_codes is not None:
        selected_stems = {code.replace(".", "_", 1) for code in selected_codes}
        raw = {stem: path for stem, path in raw.items() if stem in selected_stems}
        qfq = {stem: path for stem, path in qfq.items() if stem in selected_stems}
    unmatched = sorted(set(raw) ^ set(qfq))
    if unmatched:
        preview = ", ".join(unmatched[:10])
        raise FreePanelBuildError(f"raw/qfq daily file mismatch for {len(unmatched)} codes: {preview}")
    if not raw:
        raise FreePanelBuildError("no paired BaoStock daily files are available")
    return [(raw[key], qfq[key]) for key in sorted(raw)]


def _quote_duckdb_path(path: Path) -> str:
    return str(path).replace("'", "''")


def build_and_write_free_stock_panel_streaming(config: FreeRealConfig, batch_codes: int = 100) -> Path:
    if batch_codes <= 0:
        raise ValueError("batch_codes must be positive")
    stock_basic = _prepare_stock_basic(
        config,
        _read_parquet(config.data_root / "raw/baostock/stock_basic.parquet"),
    )
    trade_calendar_path = config.data_root / "raw/baostock/trade_calendar.parquet"
    trade_calendar = _read_parquet(trade_calendar_path)
    _prepare_trade_calendar(trade_calendar, config.start_date, config.end_date)
    selected_codes = set(stock_basic["source_code"].astype(str))
    integrity = inspect_daily_pairs(config.data_root, sorted(selected_codes))
    valid_codes = {code for code, result in integrity.items() if result.complete}
    pairs = [
        pair
        for pair in _daily_file_pairs(config, selected_codes)
        if pair[0].stem.replace("_", ".", 1) in valid_codes
    ]
    if not pairs:
        raise FreePanelBuildError("no paired daily files match the selected point-in-time universe")
    paired_codes = {raw_path.stem.replace("_", ".", 1) for raw_path, _ in pairs}
    missing_codes = sorted(selected_codes - paired_codes)
    allow_partial = bool(config.raw.get("panel_build", {}).get("allow_partial", False))
    canonical_frozen = _frozen_universe_path(config) is not None
    if missing_codes and (canonical_frozen or not allow_partial):
        preview = ", ".join(
            f"{code} ({integrity[code].error_summary})"
            for code in missing_codes[:10]
        )
        raise FreePanelBuildError(
            f"refusing to replace canonical panel with incomplete universe: "
            f"paired={len(paired_codes)}/{len(selected_codes)}, missing={len(missing_codes)} ({preview})"
        )
    download_plan = _download_plan_provenance(
        config,
        selected_codes,
        refresh=True,
    )
    raw_qfq_inputs = _build_raw_qfq_input_provenance(pairs, integrity)

    out_dir = config.data_root / "processed" / "phase2_free"
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "stock_panel.parquet"
    tmp_path = out_dir / f".stock_panel.{os.getpid()}.tmp.parquet"
    parts_root = out_dir / f".stock_panel_parts_{os.getpid()}"
    parts_root.mkdir(parents=True, exist_ok=False)
    coverage_summaries: list[dict[str, object]] = []
    try:
        for offset in range(0, len(pairs), batch_codes):
            batch = pairs[offset : offset + batch_codes]
            daily_raw = pd.concat([pd.read_parquet(raw_path) for raw_path, _ in batch], ignore_index=True, sort=False)
            daily_qfq = pd.concat([pd.read_parquet(qfq_path) for _, qfq_path in batch], ignore_index=True, sort=False)
            panel_part = _build_panel_frame(
                stock_basic,
                daily_raw,
                daily_qfq,
                trade_calendar,
                research_start_date=config.start_date,
                research_end_date=config.end_date,
                coverage_sink=coverage_summaries,
            )
            panel_part.to_parquet(parts_root / f"part_{offset // batch_codes:05d}.parquet", index=False)
            print(
                f"[panel] codes={min(offset + len(batch), len(pairs))}/{len(pairs)} rows_part={len(panel_part)}",
                flush=True,
            )
        parts_glob = _quote_duckdb_path(parts_root / "part_*.parquet")
        quoted_tmp = _quote_duckdb_path(tmp_path)
        with duckdb.connect() as connection:
            connection.execute(
                f"COPY (SELECT * FROM read_parquet('{parts_glob}') ORDER BY trade_date, ts_code) "
                f"TO '{quoted_tmp}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 250000)"
            )
            duplicate = connection.execute(
                f"SELECT trade_date, ts_code, COUNT(*) AS n FROM read_parquet('{quoted_tmp}') "
                "GROUP BY trade_date, ts_code HAVING COUNT(*) > 1 LIMIT 1"
            ).fetchone()
            if duplicate:
                raise FreePanelBuildError(f"streamed free stock panel has duplicate key: {duplicate}")
            rows = int(connection.execute(f"SELECT COUNT(*) FROM read_parquet('{quoted_tmp}')").fetchone()[0])
            symbols = int(
                connection.execute(
                    f"SELECT COUNT(DISTINCT ts_code) FROM read_parquet('{quoted_tmp}')"
                ).fetchone()[0]
            )
        if rows <= 0:
            raise FreePanelBuildError("streamed free stock panel is empty")
        if symbols != len(paired_codes):
            raise FreePanelBuildError(
                f"streamed panel symbol count differs from inputs: panel={symbols}, inputs={len(paired_codes)}"
            )
        date_coverage = _aggregate_date_coverage(coverage_summaries)
        if int(date_coverage["symbols"]) != symbols:
            raise FreePanelBuildError("streamed panel date-coverage symbol count is stale")
        with duckdb.connect() as connection:
            date_min, date_max = connection.execute(
                f"SELECT MIN(trade_date), MAX(trade_date) FROM read_parquet('{quoted_tmp}')"
            ).fetchone()
        _validate_raw_qfq_input_provenance(
            raw_qfq_inputs,
            expected_codes=paired_codes,
        )
        current_plan = _download_plan_provenance(
            config,
            selected_codes,
            refresh=False,
        )
        _assert_download_plan_binding(download_plan, current_plan)
        tmp_path.replace(final_path)
        _write_panel_manifest(
            config,
            final_path,
            rows,
            symbols,
            str(date_min),
            str(date_max),
            trade_calendar_path=trade_calendar_path,
            download_plan=download_plan,
            raw_qfq_inputs=raw_qfq_inputs,
            date_coverage=date_coverage,
        )
        print(f"[panel] completed rows={rows} symbols={symbols}", flush=True)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
        shutil.rmtree(parts_root, ignore_errors=True)
    return final_path


def build_and_write_free_stock_panel(config_path: str | Path) -> Path:
    config = load_config(config_path)
    batch_codes = int(config.raw.get("panel_build", {}).get("batch_codes", 100))
    return build_and_write_free_stock_panel_streaming(config, batch_codes=batch_codes)

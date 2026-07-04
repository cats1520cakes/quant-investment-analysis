from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml

from .schema import Phase2RealDataConfig, REQUIRED_RAW_TABLES


class RawDataError(RuntimeError):
    """Base class for Phase 2 raw-data loading failures."""


class RawTableMissingError(RawDataError):
    """Raised when a required raw table has no readable files."""


class RawTableEmptyError(RawDataError):
    """Raised when a required raw table exists but contains no rows."""


def load_config(path: str | Path) -> Phase2RealDataConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return Phase2RealDataConfig(raw=raw, path=config_path)


def raw_table_candidates(config: Phase2RealDataConfig, table: str) -> list[Path]:
    candidates = [
        config.raw_phase2_dir / table,
        config.data_root / "raw" / config.primary_source / table,
    ]
    if config.primary_source != "tushare":
        candidates.append(config.data_root / "raw" / "tushare" / table)

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def list_table_files(table_dir: Path) -> list[Path]:
    if not table_dir.exists() or not table_dir.is_dir():
        return []
    files = []
    for path in sorted(table_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith(".") or path.name.startswith("._"):
            continue
        if path.suffix.lower() in {".parquet", ".csv"} or path.name.endswith(".csv.gz"):
            files.append(path)
    return files


def discover_table_files(config: Phase2RealDataConfig, table: str) -> list[Path]:
    scanned = raw_table_candidates(config, table)
    for candidate in scanned:
        files = list_table_files(candidate)
        if files:
            return files
    scanned_text = ", ".join(str(path) for path in scanned)
    raise RawTableMissingError(f"{table}: no parquet/csv files found under any candidate raw table directory: {scanned_text}")


def _read_one(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    try:
        return pd.read_csv(path, dtype=str, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def read_raw_table(config: Phase2RealDataConfig, table: str) -> pd.DataFrame:
    files = discover_table_files(config, table)
    frames = []
    for path in files:
        frame = _read_one(path)
        if not frame.empty:
            frame = frame.copy()
            frame["_raw_path"] = str(path)
            frames.append(frame)
    if not frames:
        raise RawTableEmptyError(f"{table}: files exist but all are empty: {', '.join(str(path) for path in files[:10])}")
    return pd.concat(frames, ignore_index=True, sort=False)


def read_required_tables(config: Phase2RealDataConfig, tables: Iterable[str] = REQUIRED_RAW_TABLES) -> dict[str, pd.DataFrame]:
    loaded: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    for table in tables:
        try:
            loaded[table] = read_raw_table(config, table)
        except RawDataError as exc:
            failures.append(str(exc))
    if failures:
        details = "\n- ".join(failures)
        raise RawDataError(
            "Missing required Phase 2 real-data raw tables; refusing to build stock_panel and not falling back to index/proxy data.\n"
            f"- {details}"
        )
    return loaded


def write_processed_table(config: Phase2RealDataConfig, table_name: str, frame: pd.DataFrame) -> Path:
    config.processed_dir.mkdir(parents=True, exist_ok=True)
    path = config.processed_dir / f"{table_name}.parquet"
    frame.to_parquet(path, index=False)
    return path


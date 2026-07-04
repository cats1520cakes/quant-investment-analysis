from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable

import pandas as pd


REQUIRED_RAW_TABLES: tuple[str, ...] = (
    "trade_cal",
    "stock_basic",
    "daily",
    "adj_factor",
    "daily_basic",
    "stk_limit",
    "suspend_d",
    "namechange",
)

OUTPUT_TABLES: dict[str, str] = {
    "trade_calendar": "trade_calendar.parquet",
    "stock_basic": "stock_basic.parquet",
    "stock_daily": "stock_daily.parquet",
    "stock_adj_factor": "stock_adj_factor.parquet",
    "stock_daily_basic": "stock_daily_basic.parquet",
    "stock_limit": "stock_limit.parquet",
    "stock_suspend": "stock_suspend.parquet",
    "stock_namechange": "stock_namechange.parquet",
    "stock_panel": "stock_panel.parquet",
}

PANEL_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "ts_code",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "adj_factor",
    "adj_close_for_signal",
    "turnover_rate",
    "total_mv",
    "circ_mv",
    "up_limit",
    "down_limit",
    "is_suspended",
    "is_st",
    "list_date",
    "delist_date",
    "list_status",
    "listing_days",
    "board",
    "exchange",
)

PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "pre_close")
NUMERIC_PANEL_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "adj_factor",
    "adj_close_for_signal",
    "turnover_rate",
    "total_mv",
    "circ_mv",
    "up_limit",
    "down_limit",
)


@dataclass(frozen=True)
class Phase2RealDataConfig:
    raw: Dict[str, Any]
    path: Path

    @property
    def data_root(self) -> Path:
        try:
            root = self.raw["data_root"]
        except KeyError as exc:
            raise KeyError(f"{self.path} missing required key: data_root") from exc
        return Path(root).expanduser()

    @property
    def start_date(self) -> str:
        return str(self.raw.get("start_date") or "20100101")

    @property
    def end_date(self) -> str:
        configured = self.raw.get("end_date")
        if configured:
            return str(configured)
        return datetime.now().strftime("%Y%m%d")

    @property
    def primary_source(self) -> str:
        return str(self.raw.get("primary_source") or "tushare")

    @property
    def processed_dir(self) -> Path:
        return self.data_root / "processed" / "phase2"

    @property
    def raw_phase2_dir(self) -> Path:
        configured = self.raw.get("raw_phase2_dir")
        if configured:
            return Path(str(configured)).expanduser()
        return self.data_root / "raw" / "phase2"


def normalize_date_value(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null"}:
        return None
    if text.endswith(".0"):
        text = text[:-2]
    text = text.replace("-", "").replace("/", "")
    if len(text) < 8:
        return None
    return text[:8]


def normalize_date_series(series: pd.Series) -> pd.Series:
    values = series.astype("string").str.strip()
    values = values.mask(values.str.lower().isin(["", "nan", "nat", "none", "null"]))
    values = values.str.replace(r"\.0$", "", regex=True).str.replace("-", "", regex=False).str.replace("/", "", regex=False)
    values = values.str.slice(0, 8)
    return values.mask(values.str.len() < 8)


def date_to_datetime(series: pd.Series) -> pd.Series:
    return pd.to_datetime(normalize_date_series(series), format="%Y%m%d", errors="coerce")


def filter_date_range(frame: pd.DataFrame, column: str, start_date: str, end_date: str) -> pd.DataFrame:
    if column not in frame.columns:
        return frame
    dates = normalize_date_series(frame[column])
    mask = dates.notna() & (dates >= start_date) & (dates <= end_date)
    return frame.loc[mask].copy()


def require_columns(frame: pd.DataFrame, table: str, columns: Iterable[str]) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{table} missing required columns: {missing}; got {list(frame.columns)}")


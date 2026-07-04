from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant_proof.free_sources.baostock_adapter import FreeRealConfig, iter_existing_daily_files, load_config
from quant_proof.realdata.derived_limits import add_derived_limit_prices, infer_free_board
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
    "trade_status",
    "is_suspended",
    "is_st",
    "list_date",
    "delist_date",
    "list_status",
    "listing_days",
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


class FreePanelBuildError(RuntimeError):
    """Raised when free-real raw data is unavailable or inconsistent."""


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


def build_free_stock_panel(config: FreeRealConfig) -> pd.DataFrame:
    stock_basic = _read_parquet(config.data_root / "raw/baostock/stock_basic.parquet")
    trade_calendar = _read_parquet(config.data_root / "raw/baostock/trade_calendar.parquet")
    daily_raw = _read_daily_dir(config, "daily_raw")
    daily_qfq = _read_daily_dir(config, "daily_qfq")

    for frame, column in [(stock_basic, "list_date"), (stock_basic, "delist_date")]:
        if column in frame.columns:
            frame[column] = normalize_date_column(frame, column)
    if "type" in stock_basic.columns:
        stock_basic = stock_basic.loc[stock_basic["type"].astype(str) == "1"].copy()
    if "list_status" in stock_basic.columns:
        stock_basic = stock_basic.loc[stock_basic["list_status"].astype(str) == "1"].copy()
    if stock_basic.empty:
        raise FreePanelBuildError("stock_basic has no listed A-share stock rows after type/list_status filtering")

    daily_raw["trade_date"] = normalize_date_column(daily_raw, "trade_date")
    daily_qfq["trade_date"] = normalize_date_column(daily_qfq, "trade_date")
    trade_calendar["trade_date"] = normalize_date_column(trade_calendar, "trade_date")

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

    trade_dt = pd.to_datetime(panel["trade_date"], format="%Y%m%d", errors="coerce")
    list_dt = pd.to_datetime(panel["list_date"], format="%Y%m%d", errors="coerce")
    panel["listing_days"] = (trade_dt - list_dt).dt.days.astype("Int64")
    panel["board"] = [infer_free_board(ts_code) for ts_code in panel["ts_code"]]
    panel["is_suspended"] = (panel["trade_status"] != 1) | panel[["open", "high", "low", "close"]].isna().any(axis=1)
    panel["is_st"] = panel["is_st_raw"].fillna(0).astype(float).astype(int) == 1

    panel = add_derived_limit_prices(panel)
    panel = add_circ_mv_approx(panel)
    panel["data_tier"] = "free_real"

    for column in FREE_PANEL_COLUMNS:
        if column not in panel.columns:
            panel[column] = pd.NA
    out = panel.loc[:, list(FREE_PANEL_COLUMNS)].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    validate_free_stock_panel(out)
    return out


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
    if panel["up_limit_source"].ne("derived").any() or panel["down_limit_source"].ne("derived").any():
        raise ValueError("free stock_panel limit sources must be derived")


def write_free_stock_panel(config: FreeRealConfig, panel: pd.DataFrame) -> Path:
    out_dir = config.data_root / "processed" / "phase2_free"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "stock_panel.parquet"
    panel.to_parquet(path, index=False)
    return path


def build_and_write_free_stock_panel(config_path: str | Path) -> Path:
    config = load_config(config_path)
    panel = build_free_stock_panel(config)
    return write_free_stock_panel(config, panel)

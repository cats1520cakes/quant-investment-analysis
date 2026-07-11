from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_sources.baostock_adapter import load_config, select_stock_universe
from quant_proof.free_sources.daily_integrity import inspect_daily_pairs
from quant_proof.free_sources.validators import strategy_allowed_in_tier
from quant_proof.realdata.free_panel_builder import (
    FREE_PANEL_COLUMNS,
    validate_free_stock_panel,
    validate_panel_manifest,
)


REQUIRED_RAW = {
    "stock_basic": ["raw/baostock/stock_basic.parquet"],
    "trade_calendar": ["raw/baostock/trade_calendar.parquet"],
    "daily_raw": ["raw/baostock/daily_raw"],
    "daily_qfq": ["raw/baostock/daily_qfq"],
}


def table_status(data_root: Path, table: str, rels: list[str]) -> dict:
    files = []
    rows = 0
    columns: set[str] = set()
    for rel in rels:
        path = data_root / rel
        if path.is_dir():
            candidates = [candidate for candidate in sorted(path.glob("*.parquet")) if not candidate.name.startswith((".", "._"))]
        else:
            candidates = [path] if path.exists() else []
        for candidate in candidates:
            try:
                frame = pd.read_parquet(candidate)
            except Exception:
                frame = pd.DataFrame()
            files.append(candidate)
            rows += int(len(frame))
            columns.update(map(str, frame.columns))
    return {
        "table": table,
        "files": len(files),
        "rows": rows,
        "present": len(files) > 0 and rows > 0,
        "columns": ", ".join(sorted(columns)),
    }


def frozen_stock_source_codes(config) -> set[str]:
    value = config.raw.get("download", {}).get("frozen_universe_path")
    if value:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = config.data_root / path
    else:
        path = config.data_root / "raw/baostock/stock_basic.parquet"
    if not path.exists():
        return set()
    frame = pd.read_csv(path) if path.suffix.lower() == ".csv" else pd.read_parquet(path)
    download = config.raw.get("download", {}) if isinstance(config.raw.get("download", {}), dict) else {}
    frame = select_stock_universe(
        frame,
        universe_scope=str(download.get("universe_scope", "current")),
        universe_start_date=str(download.get("universe_start_date", config.start_date)),
        universe_end_date=str(download.get("universe_end_date", config.end_date)),
    )
    return set(frame["source_code"].dropna().astype(str))


def daily_file_source_codes(data_root: Path, table: str) -> set[str]:
    root = data_root / "raw" / "baostock" / table
    if not root.exists():
        return set()
    codes = set()
    for path in root.glob("*.parquet"):
        if path.name.startswith((".", "._")):
            continue
        codes.add(path.stem.replace("_", ".", 1))
    return codes


def validate_panel(path: Path, config) -> tuple[bool, str]:
    if not path.exists():
        return False, f"missing panel: {path}"
    try:
        validate_panel_manifest(
            path,
            expected_symbols=int(config.raw.get("panel_build", {}).get("expected_symbols", 0)),
            config_path=config.path,
        )
        frame = pd.read_parquet(path)
        validate_free_stock_panel(frame)
    except Exception as exc:  # noqa: BLE001 - validation reports the exact gate failure.
        return False, f"panel validation failed: {type(exc).__name__}: {exc}"
    missing = sorted(set(FREE_PANEL_COLUMNS) - set(frame.columns))
    if missing:
        return False, f"panel missing columns: {missing}"
    if frame.empty:
        return False, "panel is empty"
    if frame["up_limit_source"].ne("derived").any() or frame["down_limit_source"].ne("derived").any():
        return False, "free_real panel must mark limit prices as derived"
    stock_codes = frozen_stock_source_codes(config)
    if stock_codes:
        non_stock = sorted(set(frame["source_code"].dropna().astype(str)) - stock_codes)
        if non_stock:
            return False, f"panel contains non-stock source_code rows, e.g. {non_stock[:5]}"
    n_stocks = frame["ts_code"].dropna().astype(str).nunique() if "ts_code" in frame.columns else 0
    dates = frame["trade_date"].dropna().astype(str) if "trade_date" in frame.columns else pd.Series(dtype=str)
    date_range = "NA" if dates.empty else f"{dates.min()}..{dates.max()}"
    return True, f"panel rows={len(frame)}; stocks={n_stocks}; date_range={date_range}"


def write_report(config_path: str | Path) -> tuple[Path, bool]:
    config = load_config(config_path)
    data_root = config.data_root
    statuses = pd.DataFrame([table_status(data_root, table, rels) for table, rels in REQUIRED_RAW.items()])
    panel_path = data_root / "processed/phase2_free/stock_panel.parquet"
    panel_ok, panel_message = validate_panel(panel_path, config)
    stock_codes = frozen_stock_source_codes(config)
    integrity = inspect_daily_pairs(data_root, sorted(stock_codes)) if stock_codes else {}
    valid_stock_codes = {code for code, result in integrity.items() if result.complete}
    invalid_pairs = [(code, result.error_summary) for code, result in integrity.items() if not result.complete]
    expected_symbols = int(config.raw.get("panel_build", {}).get("expected_symbols", 0)) or len(stock_codes)
    raw_ok = bool((statuses["present"]).all()) and len(valid_stock_codes) == expected_symbols
    free_allowed = raw_ok and panel_ok

    admissions = [
        strategy_allowed_in_tier("S2_real_stock_momentum", "free_real"),
        strategy_allowed_in_tier("S3_real_stock_breakout", "free_real"),
        strategy_allowed_in_tier("S4_real_smallcap_factor", "free_real"),
        strategy_allowed_in_tier("S11_real_short_term_reversal", "free_real"),
        strategy_allowed_in_tier("S12_real_low_volatility", "free_real"),
        strategy_allowed_in_tier("S13_real_residual_momentum", "free_real"),
        strategy_allowed_in_tier("S14_real_volume_price_shock", "free_real"),
        strategy_allowed_in_tier("S20_real_stateful_trend", "free_real"),
        strategy_allowed_in_tier("S21_real_volatility_contraction", "free_real"),
        strategy_allowed_in_tier("S22_real_concentrated_trend", "free_real"),
        strategy_allowed_in_tier("S23_real_concentrated_contraction", "free_real"),
        strategy_allowed_in_tier("S24_real_regime_contraction", "free_real"),
        strategy_allowed_in_tier("S26_real_gap_intraday", "free_real"),
        strategy_allowed_in_tier("S27_real_momentum_acceleration", "free_real"),
        strategy_allowed_in_tier("S5_real_limitup_board", "free_real"),
        strategy_allowed_in_tier("S2_real_stock_momentum", "proxy_research"),
    ]
    admission_frame = pd.DataFrame([item.__dict__ | {"data_tier": item.data_tier.value} for item in admissions])

    out_path = Path(config.raw["paths"]["validation_report"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Phase 2 Free Real Data Validation",
        "",
        "## Leaderboard Tiers",
        "",
        "- `strict_real_leaderboard`: remains blocked unless paid/official fields exist (`official stk_limit`, `suspend_d`, `daily_basic`, `adj_factor`, futures/options chains).",
        f"- `free_real_leaderboard`: {'admitted' if free_allowed else 'blocked'}; requires every frozen-universe raw/qfq pair plus a matching panel provenance manifest.",
        "- `proxy_research_leaderboard`: Qlib/index proxy only; cannot enter real leaderboards.",
        "",
        "## Raw Table Status",
        "",
        statuses.to_markdown(index=False),
        "",
        "Raw file counts may include older cache files. Admission uses content-level pair integrity, not file presence.",
        "",
        "## Panel Status",
        "",
        f"- `{panel_path}`: {panel_message}",
        f"- frozen universe symbols: `{len(stock_codes)}` (expected `{expected_symbols}`)",
        f"- content-valid raw/qfq pairs: `{len(valid_stock_codes)}`",
        f"- invalid or missing pairs: `{len(invalid_pairs)}`",
        f"- first integrity failures: `{invalid_pairs[:10]}`",
        "",
        "## Strategy Admission",
        "",
        admission_frame.to_markdown(index=False),
        "",
        "## Required Disclaimers",
        "",
        "- `up_limit` and `down_limit` are derived from `pre_close` and board rules in `free_real`.",
        "- `is_suspended` uses BaoStock `tradestatus`; this is proxy evidence, not strict official `suspend_d`.",
        "- `is_st` uses BaoStock `isST`; this is sufficient for free-real filters but not a full `namechange` table.",
        "- `circ_mv_approx` is derived from `amount / (turnover_rate / 100)` and must not be renamed to official `circ_mv` in free-real reports.",
    ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"validation_report={out_path}")
    print(statuses.to_string(index=False))
    return out_path, free_allowed


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Phase 2 free-real data.")
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    parser.add_argument("--require-ready", action="store_true", help="Exit nonzero unless the free-real leaderboard is admitted.")
    args = parser.parse_args()
    _, ready = write_report(args.config)
    if args.require_ready and not ready:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_sources.baostock_adapter import load_config
from quant_proof.free_sources.validators import strategy_allowed_in_tier
from quant_proof.realdata.free_panel_builder import FREE_PANEL_COLUMNS


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


def listed_stock_source_codes(data_root: Path) -> set[str]:
    path = data_root / "raw/baostock/stock_basic.parquet"
    if not path.exists():
        return set()
    frame = pd.read_parquet(path)
    if "type" in frame.columns:
        frame = frame.loc[frame["type"].astype(str) == "1"]
    if "list_status" in frame.columns:
        frame = frame.loc[frame["list_status"].astype(str) == "1"]
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


def validate_panel(path: Path, data_root: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"missing panel: {path}"
    frame = pd.read_parquet(path)
    missing = sorted(set(FREE_PANEL_COLUMNS) - set(frame.columns))
    if missing:
        return False, f"panel missing columns: {missing}"
    if frame.empty:
        return False, "panel is empty"
    if frame["up_limit_source"].ne("derived").any() or frame["down_limit_source"].ne("derived").any():
        return False, "free_real panel must mark limit prices as derived"
    stock_codes = listed_stock_source_codes(data_root)
    if stock_codes:
        non_stock = sorted(set(frame["source_code"].dropna().astype(str)) - stock_codes)
        if non_stock:
            return False, f"panel contains non-stock source_code rows, e.g. {non_stock[:5]}"
    n_stocks = frame["ts_code"].dropna().astype(str).nunique() if "ts_code" in frame.columns else 0
    dates = frame["trade_date"].dropna().astype(str) if "trade_date" in frame.columns else pd.Series(dtype=str)
    date_range = "NA" if dates.empty else f"{dates.min()}..{dates.max()}"
    return True, f"panel rows={len(frame)}; stocks={n_stocks}; date_range={date_range}"


def write_report(config_path: str | Path) -> Path:
    config = load_config(config_path)
    data_root = config.data_root
    statuses = pd.DataFrame([table_status(data_root, table, rels) for table, rels in REQUIRED_RAW.items()])
    panel_path = data_root / "processed/phase2_free/stock_panel.parquet"
    panel_ok, panel_message = validate_panel(panel_path, data_root)
    stock_codes = listed_stock_source_codes(data_root)
    raw_stock_codes = daily_file_source_codes(data_root, "daily_raw") & stock_codes
    qfq_stock_codes = daily_file_source_codes(data_root, "daily_qfq") & stock_codes
    matched_stock_codes = raw_stock_codes & qfq_stock_codes
    raw_ok = bool((statuses["present"]).all()) and bool(matched_stock_codes)
    free_allowed = raw_ok or panel_ok

    admissions = [
        strategy_allowed_in_tier("S2_real_stock_momentum", "free_real"),
        strategy_allowed_in_tier("S3_real_stock_breakout", "free_real"),
        strategy_allowed_in_tier("S4_real_smallcap_factor", "free_real"),
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
        f"- `free_real_leaderboard`: {'can run after panel build' if free_allowed else 'blocked until BaoStock raw/qfq data or free panel exists'}; uses BaoStock/AKShare derived fields.",
        "- `proxy_research_leaderboard`: Qlib/index proxy only; cannot enter real leaderboards.",
        "",
        "## Raw Table Status",
        "",
        statuses.to_markdown(index=False),
        "",
        "Raw file counts may include older cache files. Panel admission is based on matched listed A-share stock raw/qfq files.",
        "",
        "## Panel Status",
        "",
        f"- `{panel_path}`: {panel_message}",
        f"- matched listed stock daily files: `{len(matched_stock_codes)}`",
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
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Phase 2 free-real data.")
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    args = parser.parse_args()
    write_report(args.config)


if __name__ == "__main__":
    main()

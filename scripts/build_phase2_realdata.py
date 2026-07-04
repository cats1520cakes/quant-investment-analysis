from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.realdata.calendar import build_trade_calendar
from quant_proof.realdata.corporate_actions import build_stock_adj_factor
from quant_proof.realdata.limits import build_stock_limit
from quant_proof.realdata.loader import RawDataError, load_config, read_required_tables, write_processed_table
from quant_proof.realdata.schema import OUTPUT_TABLES, REQUIRED_RAW_TABLES
from quant_proof.realdata.st_status import build_st_flags, build_stock_namechange
from quant_proof.realdata.suspension import build_stock_suspend, build_suspension_flags
from quant_proof.realdata.universe import build_stock_basic
from quant_proof.realdata.validation import build_stock_daily, build_stock_daily_basic, build_stock_panel


def build_phase2_realdata(config_path: str | Path) -> dict[str, Path]:
    config = load_config(config_path)
    raw = read_required_tables(config, REQUIRED_RAW_TABLES)

    trade_calendar = build_trade_calendar(raw["trade_cal"], config)
    stock_basic = build_stock_basic(raw["stock_basic"])
    stock_daily = build_stock_daily(raw["daily"])
    stock_adj_factor = build_stock_adj_factor(raw["adj_factor"])
    stock_daily_basic = build_stock_daily_basic(raw["daily_basic"])
    stock_limit = build_stock_limit(raw["stk_limit"])
    stock_suspend = build_stock_suspend(raw["suspend_d"])
    stock_namechange = build_stock_namechange(raw["namechange"])
    suspension_flags = build_suspension_flags(stock_suspend, trade_calendar)
    st_flags = build_st_flags(stock_namechange, trade_calendar)
    stock_panel = build_stock_panel(
        trade_calendar=trade_calendar,
        stock_basic=stock_basic,
        stock_daily=stock_daily,
        stock_adj_factor=stock_adj_factor,
        stock_daily_basic=stock_daily_basic,
        stock_limit=stock_limit,
        suspension_flags=suspension_flags,
        st_flags=st_flags,
    )

    outputs = {
        "trade_calendar": trade_calendar,
        "stock_basic": stock_basic,
        "stock_daily": stock_daily,
        "stock_adj_factor": stock_adj_factor,
        "stock_daily_basic": stock_daily_basic,
        "stock_limit": stock_limit,
        "stock_suspend": stock_suspend,
        "stock_namechange": stock_namechange,
        "stock_panel": stock_panel,
    }
    written: dict[str, Path] = {}
    for name, frame in outputs.items():
        expected = OUTPUT_TABLES[name]
        path = write_processed_table(config, name, frame)
        if path.name != expected:
            raise RuntimeError(f"unexpected output filename for {name}: {path.name} != {expected}")
        written[name] = path
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Phase 2 real-data processed parquet tables.")
    parser.add_argument("--config", default="config/phase2_real_data.yaml")
    args = parser.parse_args()

    try:
        written = build_phase2_realdata(args.config)
    except (RawDataError, KeyError, ValueError) as exc:
        print(f"phase2 realdata build failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    for name, path in written.items():
        print(f"{name}={path}")


if __name__ == "__main__":
    main()

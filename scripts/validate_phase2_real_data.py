from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml


REQUIRED_FIELDS: dict[str, set[str]] = {
    "trade_cal": {"cal_date", "is_open"},
    "stock_basic": {"ts_code", "symbol", "name", "list_date", "list_status", "exchange"},
    "daily": {"ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"},
    "adj_factor": {"ts_code", "trade_date", "adj_factor"},
    "daily_basic": {"ts_code", "trade_date", "turnover_rate", "total_mv", "circ_mv"},
    "stk_limit": {"ts_code", "trade_date", "up_limit", "down_limit"},
    "suspend_d": {"ts_code", "suspend_date"},
    "namechange": {"ts_code", "name", "start_date"},
    "fund_basic": {"ts_code", "name", "market"},
    "fund_daily": {"ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"},
    "fut_basic": {"ts_code", "symbol", "name", "exchange", "list_date", "delist_date"},
    "fut_daily": {"ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"},
    "opt_basic": {"ts_code", "name", "exchange", "call_put", "exercise_price", "list_date", "delist_date"},
    "opt_daily": {"ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"},
}

STOCK_INTERSECTION_TABLES = ["daily", "adj_factor", "daily_basic", "stk_limit", "suspend_d"]

GATE_LABELS = {
    "stock_strategies": "S2/S3/S4 real A-share stock strategies",
    "etf_strategies": "real ETF strategies",
    "futures_strategies": "stock-index futures integer-lot overlay",
    "options_strategies": "real option convexity budget",
}


@dataclass(frozen=True)
class TableDiagnostics:
    table: str
    files: int
    rows: int
    present: bool
    required_fields: str
    missing_fields: str
    fields_ok: bool
    start_date: str
    end_date: str
    code_count: int
    checksum_ok: bool
    sampled_rows: int
    notes: str


def load_config(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def manifest_path(config: dict) -> Path:
    return Path(config["data_root"]).expanduser() / config["paths"]["manifest"]


def read_manifest(config: dict) -> pd.DataFrame:
    path = manifest_path(config)
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path)
    if "table" not in frame.columns:
        return pd.DataFrame()
    return frame


def table_dir(config: dict, table: str) -> Path:
    return Path(config["data_root"]).expanduser() / "raw" / "tushare" / table


def required_fields(config: dict, table: str) -> set[str]:
    configured = config.get("table_schemas", {}).get(table)
    if configured:
        return set(map(str, configured))
    return REQUIRED_FIELDS.get(table, set())


def discover_files(config: dict, manifest: pd.DataFrame, table: str) -> list[Path]:
    paths: list[Path] = []
    if not manifest.empty and "path" in manifest.columns:
        subset = manifest[manifest["table"] == table]
        for value in subset["path"].dropna().astype(str):
            path = Path(value).expanduser()
            if path.exists():
                paths.append(path)
    if not paths:
        root = table_dir(config, table)
        if root.exists():
            paths.extend(sorted(root.glob("*.parquet")))
            paths.extend(sorted(root.glob("*.csv")))
    return sorted(dict.fromkeys(paths))


def manifest_columns(manifest: pd.DataFrame, table: str) -> set[str]:
    if manifest.empty or "columns" not in manifest.columns:
        return set()
    columns: set[str] = set()
    for value in manifest.loc[manifest["table"] == table, "columns"].dropna().astype(str):
        columns.update(part.strip() for part in value.split(",") if part.strip())
    return columns


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_status(manifest: pd.DataFrame, table: str) -> bool:
    if manifest.empty or "sha256" not in manifest.columns or "path" not in manifest.columns:
        return False
    subset = manifest[manifest["table"] == table]
    if subset.empty:
        return False
    checked = 0
    for _, row in subset.iterrows():
        expected = str(row.get("sha256") or "")
        path = Path(str(row.get("path") or "")).expanduser()
        if not expected or not path.exists():
            continue
        checked += 1
        if sha256(path) != expected:
            return False
    return checked > 0


def read_frame(path: Path, columns: Iterable[str] | None = None) -> pd.DataFrame:
    if path.suffix == ".parquet":
        try:
            return pd.read_parquet(path, columns=list(columns) if columns else None)
        except Exception:
            return pd.read_parquet(path)
    return pd.read_csv(path, usecols=lambda col: columns is None or col in set(columns))


def read_table_sample(
    config: dict,
    manifest: pd.DataFrame,
    table: str,
    columns: Iterable[str] | None = None,
) -> pd.DataFrame:
    validation_cfg = config.get("validation", {})
    max_files = int(validation_cfg.get("max_files_per_table", 80))
    max_rows = int(validation_cfg.get("max_sample_rows_per_table", 250_000))
    frames: list[pd.DataFrame] = []
    rows = 0
    for path in discover_files(config, manifest, table)[:max_files]:
        try:
            frame = read_frame(path, columns=columns)
        except Exception:
            continue
        if frame.empty:
            continue
        remaining = max_rows - rows
        if remaining <= 0:
            break
        frame = frame.head(remaining)
        frames.append(frame)
        rows += len(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def table_rows(manifest: pd.DataFrame, table: str, sample: pd.DataFrame) -> int:
    if not manifest.empty and "rows" in manifest.columns:
        subset = manifest[manifest["table"] == table]
        if not subset.empty:
            return int(pd.to_numeric(subset["rows"], errors="coerce").fillna(0).sum())
    return int(len(sample))


def date_coverage(frame: pd.DataFrame, table: str) -> tuple[str, str]:
    if frame.empty:
        return "", ""
    candidates = ["trade_date", "cal_date", "suspend_date", "start_date", "list_date"]
    for column in candidates:
        if column in frame.columns:
            values = frame[column].dropna().astype(str)
            if not values.empty:
                return str(values.min()), str(values.max())
    return "", ""


def code_count(frame: pd.DataFrame) -> int:
    if frame.empty or "ts_code" not in frame.columns:
        return 0
    return int(frame["ts_code"].dropna().nunique())


def diagnose_table(config: dict, manifest: pd.DataFrame, table: str) -> tuple[TableDiagnostics, pd.DataFrame]:
    required = required_fields(config, table)
    columns_for_sample = sorted(required | {"ts_code", "trade_date", "cal_date", "suspend_date", "list_status", "name", "up_limit", "down_limit"})
    sample = read_table_sample(config, manifest, table, columns=columns_for_sample)
    fields = set(sample.columns) if not sample.empty else manifest_columns(manifest, table)
    missing = sorted(required - fields)
    rows = table_rows(manifest, table, sample)
    files = len(discover_files(config, manifest, table))
    present = files > 0 and rows > 0
    start, end = date_coverage(sample, table)
    notes: list[str] = []
    if not present:
        notes.append("missing_or_empty")
    if missing:
        notes.append("required_fields_missing")
    return (
        TableDiagnostics(
            table=table,
            files=files,
            rows=rows,
            present=present,
            required_fields=", ".join(sorted(required)),
            missing_fields=", ".join(missing),
            fields_ok=not missing,
            start_date=start,
            end_date=end,
            code_count=code_count(sample),
            checksum_ok=checksum_status(manifest, table),
            sampled_rows=int(len(sample)),
            notes=", ".join(notes),
        ),
        sample,
    )


def diagnostics_frame(items: list[TableDiagnostics]) -> pd.DataFrame:
    return pd.DataFrame([item.__dict__ for item in items])


def code_sets(samples: dict[str, pd.DataFrame]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for table in STOCK_INTERSECTION_TABLES:
        frame = samples.get(table, pd.DataFrame())
        if not frame.empty and "ts_code" in frame.columns:
            result[table] = set(frame["ts_code"].dropna().astype(str))
        else:
            result[table] = set()
    return result


def stock_data_quality(samples: dict[str, pd.DataFrame]) -> dict[str, str]:
    stock_basic = samples.get("stock_basic", pd.DataFrame())
    namechange = samples.get("namechange", pd.DataFrame())
    suspend_d = samples.get("suspend_d", pd.DataFrame())
    stk_limit = samples.get("stk_limit", pd.DataFrame())

    delisted = 0
    if not stock_basic.empty and "list_status" in stock_basic.columns:
        delisted = int((stock_basic["list_status"].astype(str) == "D").sum())

    st_rows = 0
    if not namechange.empty and "name" in namechange.columns:
        st_rows = int(namechange["name"].astype(str).str.contains("ST", case=False, na=False).sum())

    suspension_rows = int(len(suspend_d)) if not suspend_d.empty else 0
    limit_rows = 0
    if not stk_limit.empty and {"up_limit", "down_limit"}.issubset(stk_limit.columns):
        limit_rows = int(stk_limit[["up_limit", "down_limit"]].notna().all(axis=1).sum())

    intersections = code_sets(samples)
    common = set.intersection(*(values for values in intersections.values())) if intersections else set()
    return {
        "delisted_stock_rows": str(delisted),
        "st_namechange_rows": str(st_rows),
        "suspension_rows": str(suspension_rows),
        "limit_price_rows": str(limit_rows),
        "stock_code_intersection": str(len(common)),
        "stock_code_counts": ", ".join(f"{table}={len(values)}" for table, values in intersections.items()),
    }


def gate_reasons(gate_tables: list[str], status_by_table: dict[str, TableDiagnostics]) -> list[str]:
    reasons: list[str] = []
    for table in gate_tables:
        status = status_by_table.get(table)
        if status is None or not status.present:
            reasons.append(f"missing_or_empty:{table}")
        elif not status.fields_ok:
            reasons.append(f"missing_fields:{table}({status.missing_fields})")
        elif not status.checksum_ok:
            reasons.append(f"checksum_unverified:{table}")
    return reasons


def stock_strategy_reasons(quality: dict[str, str], base_reasons: list[str]) -> list[str]:
    reasons = list(base_reasons)
    if int(quality.get("stock_code_intersection", "0")) <= 0:
        reasons.append("no_positive_code_intersection_across_daily_adj_basic_limit_suspend")
    if int(quality.get("suspension_rows", "0")) <= 0:
        reasons.append("no_suspend_d_rows_to_enforce_no_trade_days")
    if int(quality.get("limit_price_rows", "0")) <= 0:
        reasons.append("no_stk_limit_rows_to_model_limit_up_down_fills")
    if int(quality.get("delisted_stock_rows", "0")) <= 0:
        reasons.append("no_delisted_samples_detected_for_survivorship_check")
    return reasons


def make_gate_lines(
    required: dict[str, list[str]],
    status_by_table: dict[str, TableDiagnostics],
    quality: dict[str, str],
) -> list[str]:
    lines: list[str] = []
    for gate_name, gate_tables in required.items():
        reasons = gate_reasons(gate_tables, status_by_table)
        if gate_name == "stock_strategies":
            reasons = stock_strategy_reasons(quality, reasons)
        label = GATE_LABELS.get(gate_name, gate_name)
        if reasons:
            lines.append(f"- `{gate_name}` ({label})：不可进榜；原因：`{'; '.join(reasons)}`。")
        else:
            lines.append(f"- `{gate_name}` ({label})：表、字段、checksum、核心样本约束通过，可进入下一步回测。")
    return lines


def strategy_gate_lines(stock_reasons: list[str], futures_reasons: list[str], options_reasons: list[str]) -> list[str]:
    mapping = {
        "S2_real_stock_momentum": stock_reasons,
        "S3_real_stock_breakout": stock_reasons,
        "S4_real_smallcap_factor": stock_reasons,
        "index_futures_integer_lot_overlay": futures_reasons,
        "option_convexity_budget": options_reasons,
    }
    lines: list[str] = []
    for strategy, reasons in mapping.items():
        if reasons:
            lines.append(f"- `{strategy}`：禁止进真实排行榜；原因：`{'; '.join(reasons)}`。")
        else:
            lines.append(f"- `{strategy}`：数据准入通过，可进入候选回测。")
    return lines


def write_report(
    config: dict,
    manifest: pd.DataFrame,
    status: pd.DataFrame,
    gates: list[str],
    strategy_gates: list[str],
    quality: dict[str, str],
) -> Path:
    out_path = Path(config["data_root"]).expanduser() / config["paths"]["validation_report"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_display = str(manifest_path(config))
    manifest_state = "present" if not manifest.empty else "missing"
    quality_table = pd.DataFrame([{"metric": key, "value": value} for key, value in quality.items()])
    blocked = [line for line in strategy_gates if "禁止进真实排行榜" in line]
    lines = [
        "# Phase 2 真实数据验证报告",
        "",
        f"- manifest: `{manifest_display}` ({manifest_state})",
        "- 结论：真实排行榜必须使用真实 ETF/个股/期货/期权表；不得静默降级为指数代理。",
        "",
        "## 数据表状态",
        "",
        status.to_markdown(index=False) if not status.empty else "未找到 manifest 或 raw 文件。",
        "",
        "## 股票样本完整性",
        "",
        quality_table.to_markdown(index=False) if not quality_table.empty else "未找到可校验的股票样本。",
        "",
        "## 排行榜准入判断",
        "",
        *gates,
        "",
        "## 当前重点策略准入",
        "",
        *strategy_gates,
        "",
        "## 为什么当前不能运行真实 leaderboard",
        "",
    ]
    if blocked:
        lines.extend(f"- {line[2:]}" for line in blocked)
    else:
        lines.append("- 数据门禁已通过；下一步才允许运行真实 leaderboard。")
    lines.extend(
        [
            "",
            "## 硬性边界",
            "",
            "- Phase 1 是指数代理筛查，不等价于真实 ETF / 个股撮合。",
            "- 没有 `daily` / `adj_factor` / `daily_basic`，不能计算真实个股信号和市值/流动性过滤。",
            "- 没有 `stk_limit`，不能模拟涨停买入失败和跌停卖出失败。",
            "- 没有 `suspend_d`，不能执行停牌不可成交约束。",
            "- 没有 `stock_basic` 的退市状态和 `namechange`，不能处理幸存者偏差与 ST 样本。",
            "- 没有真实期货/期权合约与日线，不允许做整手 overlay 或真实期权凸性预算。",
        ]
    )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/phase2_real_data.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    manifest = read_manifest(config)
    required = config.get("required_for_leaderboard", {})
    configured_schema_tables = set(config.get("table_schemas", {}))
    tables = sorted(set(REQUIRED_FIELDS) | configured_schema_tables | {table for values in required.values() for table in values})

    diagnostics: list[TableDiagnostics] = []
    samples: dict[str, pd.DataFrame] = {}
    for table in tables:
        item, sample = diagnose_table(config, manifest, table)
        diagnostics.append(item)
        samples[table] = sample

    status = diagnostics_frame(diagnostics)
    status_by_table = {item.table: item for item in diagnostics}
    quality = stock_data_quality(samples)
    gates = make_gate_lines(required, status_by_table, quality)
    stock_reasons = stock_strategy_reasons(quality, gate_reasons(required.get("stock_strategies", []), status_by_table))
    futures_reasons = gate_reasons(required.get("futures_strategies", []), status_by_table)
    options_reasons = gate_reasons(required.get("options_strategies", []), status_by_table)
    strategy_gates = strategy_gate_lines(stock_reasons, futures_reasons, options_reasons)
    out_path = write_report(config, manifest, status, gates, strategy_gates, quality)

    print(f"validation_report={out_path}")
    print(status[["table", "files", "rows", "present", "fields_ok", "checksum_ok"]].to_string(index=False))


if __name__ == "__main__":
    main()

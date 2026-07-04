from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import yaml


def load_config(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def read_manifest(config: dict) -> pd.DataFrame:
    path = Path(config["data_root"]) / config["paths"]["manifest"]
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def table_status(manifest: pd.DataFrame, table: str) -> dict:
    subset = manifest[manifest["table"] == table] if not manifest.empty else pd.DataFrame()
    return {
        "table": table,
        "files": int(len(subset)),
        "rows": int(subset["rows"].sum()) if not subset.empty and "rows" in subset else 0,
        "present": bool(len(subset) > 0 and subset["rows"].sum() > 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/phase2_real_data.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    manifest = read_manifest(config)
    required = config.get("required_for_leaderboard", {})
    tables = sorted({table for values in required.values() for table in values})
    rows = [table_status(manifest, table) for table in tables]
    status = pd.DataFrame(rows)

    lines = [
        "# Phase 2 真实数据验证报告",
        "",
        "## 数据表状态",
        "",
        status.to_markdown(index=False) if not status.empty else "未找到 manifest。",
        "",
        "## 排行榜准入判断",
        "",
    ]
    for gate_name, gate_tables in required.items():
        missing = [table for table in gate_tables if not table_status(manifest, table)["present"]]
        if missing:
            lines.append(f"- `{gate_name}`：不可进榜，缺少 `{', '.join(missing)}`。")
        else:
            lines.append(f"- `{gate_name}`：数据表存在，可进入下一步字段级校验。")

    lines.extend(
        [
            "",
            "## 硬性边界",
            "",
            "- 没有真实 ETF 数据，不允许 ETF 策略进榜。",
            "- 没有真实期货合约数据，不允许股指期货策略进榜。",
            "- 没有真实期权合约数据，不允许期权策略进榜。",
            "- 没有 `stk_limit` 和 `suspend_d`，不允许个股涨停/动量策略进榜。",
            "- 必须报告数据完整性缺口，不允许静默降级为指数代理。",
        ]
    )
    out_path = Path(config["data_root"]) / config["paths"]["validation_report"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"validation_report={out_path}")
    print(status.to_string(index=False) if not status.empty else "manifest missing")


if __name__ == "__main__":
    main()


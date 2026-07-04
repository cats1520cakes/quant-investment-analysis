from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict

import pandas as pd


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _fmt_money(value: float) -> str:
    return f"{value:,.0f}"


def write_phase1_report(
    path: Path,
    leaderboard: pd.DataFrame,
    bootstrap_summary: pd.DataFrame,
    data_paths: Dict[str, str],
    generated_at: datetime,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    top = leaderboard.head(12).copy()
    lines = [
        "# 附文第一阶段实验证明报告",
        "",
        f"生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 结论摘要",
        "",
    ]
    if leaderboard.empty:
        lines.append("本轮没有得到有效窗口，需先检查数据下载范围。")
    else:
        best = leaderboard.iloc[0]
        highest_success = leaderboard.sort_values(["p_success", "median_w24"], ascending=False).iloc[0]
        lines.extend(
            [
                f"- 历史滚动窗口最优策略：`{best['strategy']}`，入金口径 `{best['deposit_timing']}`。",
                f"- 达标概率：{_fmt_pct(best['p_success'])}；24 个月期末资产中位数：{_fmt_money(best['median_w24'])}。",
                f"- 历史滚动最高达标率策略：`{highest_success['strategy']}`，入金口径 `{highest_success['deposit_timing']}`，达标概率 {_fmt_pct(highest_success['p_success'])}，但 24 个月资产中位数只有 {_fmt_money(highest_success['median_w24'])}。",
                f"- 95 分位最大回撤：{_fmt_pct(best['p95_max_drawdown'])}；24 个月低于累计入金概率：{_fmt_pct(best['p_w24_below_deposit'])}。",
                f"- 数据边界：{data_paths['scope_note']}",
                "- 第一阶段结果不覆盖全 A 个股涨跌停排队、停牌、ST、退市、融资融券逐日担保比例、股指期货合约粒度和期权 IV 曲面。",
            ]
        )

    lines.extend(
        [
            "",
            "## 数据与落盘",
            "",
            f"- 原始数据目录：`{data_paths['raw']}`",
            f"- 处理后收盘价：`{data_paths['processed_close']}`",
            f"- 下载 manifest：`{data_paths['manifest']}`",
            f"- 外置盘结果目录：`{data_paths['external_reports']}`",
            "",
            "## 历史滚动窗口 Leaderboard",
            "",
        ]
    )
    if not top.empty:
        table = top[
            [
                "strategy",
                "deposit_timing",
                "n_windows",
                "p_success",
                "median_w24",
                "p95_max_drawdown",
                "p_w24_below_deposit",
                "score",
            ]
        ].copy()
        table["p_success"] = table["p_success"].map(_fmt_pct)
        table["median_w24"] = table["median_w24"].map(_fmt_money)
        table["p95_max_drawdown"] = table["p95_max_drawdown"].map(_fmt_pct)
        table["p_w24_below_deposit"] = table["p_w24_below_deposit"].map(_fmt_pct)
        table["score"] = table["score"].map(lambda x: f"{x:.2f}")
        lines.append(table.to_markdown(index=False))
    else:
        lines.append("无。")

    lines.extend(["", "", "## Bootstrap 摘要", ""])
    if not bootstrap_summary.empty:
        boot = bootstrap_summary.head(12).copy()
        boot["p_success"] = boot["p_success"].map(_fmt_pct)
        boot["median_w24"] = boot["median_w24"].map(_fmt_money)
        boot["p95_max_drawdown"] = boot["p95_max_drawdown"].map(_fmt_pct)
        lines.append(boot.to_markdown(index=False))
    else:
        lines.append("本轮未生成 bootstrap 结果。")

    lines.extend(
        [
            "",
            "",
            "## 解释边界",
            "",
            "- 如果所有策略达标概率接近 0，结论不是“目标绝对不可能”，而是在本轮指数代理、日线交易、费用滑点和 24 个月窗口下，没有找到能稳定满足硬目标的操作族。",
            "- 如果轻度融资版本提高达标概率但显著推高回撤或亏损概率，应进入 Phase 2 做逐日担保比例、强平和融资利率压力测试后再评价。",
            "- 个股强势股、涨停/连板、事件驱动、期货和期权 overlay 需要更完整的 A 股撮合层与数据表，不能用本轮 ETF 结果替代。",
            "",
            "## 下一步",
            "",
            "1. 加入 AKShare / Tushare / 付费源交叉验证 ETF 可交易价格、复权因子、停牌、ST 和涨跌停价格。",
            "2. 建全 A 股票池与动态上市/退市过滤，再运行 S2/S3/S4。",
            "3. 对候选策略做 2015-like crash、滑点放大、成交率坍塌和参数邻域稳定性测试。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

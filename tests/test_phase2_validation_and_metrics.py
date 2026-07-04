from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from quant_proof.metrics import max_drawdown
from quant_proof.simulator import (
    ExecutionCost,
    aggregate_windows,
    first_trading_day_mask,
    last_trading_day_mask,
    simulate_path,
)


def write_phase2_config(tmp_path: Path) -> Path:
    config = {
        "data_root": str(tmp_path),
        "start_date": "20200101",
        "end_date": "20200131",
        "token_env": "TUSHARE_TOKEN",
        "write_format": "parquet",
        "paths": {
            "manifest": "00_meta/manifests/phase2_real_data_manifest.csv",
            "validation_report": "reports/phase2_real_data_validation.md",
            "errors_dir": "00_meta/errors",
            "logs_dir": "00_meta/logs",
        },
        "tushare": {
            "stock_statuses": ["L", "D"],
            "exchanges": {"index": ["000300.SH"], "futures": "CFFEX", "options": ["SSE"]},
            "etf_markets": ["E"],
        },
        "required_for_leaderboard": {
            "stock_strategies": [
                "trade_cal",
                "stock_basic",
                "daily",
                "adj_factor",
                "daily_basic",
                "stk_limit",
                "suspend_d",
                "namechange",
            ],
            "etf_strategies": ["trade_cal", "fund_basic", "fund_daily"],
            "futures_strategies": ["trade_cal", "fut_basic", "fut_daily"],
            "options_strategies": ["trade_cal", "opt_basic", "opt_daily"],
        },
    }
    path = tmp_path / "phase2.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


def run_python(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_python_files_compile() -> None:
    result = run_python(["-m", "compileall", "src", "scripts"], cwd=Path.cwd())
    assert result.returncode == 0, result.stdout + result.stderr


def test_phase2_yaml_parses() -> None:
    config = yaml.safe_load(Path("config/phase2_real_data.yaml").read_text(encoding="utf-8"))
    assert config["required_for_leaderboard"]["stock_strategies"][:2] == ["trade_cal", "stock_basic"]
    assert config["phase2_strategy_gate"]["no_index_proxy_in_real_leaderboard"] is True


def test_download_script_fails_clearly_without_tushare_token(tmp_path: Path) -> None:
    config_path = write_phase2_config(tmp_path)
    env = os.environ.copy()
    env.pop("TUSHARE_TOKEN", None)

    result = run_python(
        ["scripts/download_phase2_real_data.py", "--config", str(config_path), "--tables", "stock_basic", "--allow-proxy"],
        cwd=Path.cwd(),
        env=env,
    )

    assert result.returncode == 2
    assert "Missing TUSHARE_TOKEN" in result.stderr
    assert 'export TUSHARE_TOKEN="..."' in result.stderr


def test_download_script_refuses_visible_proxy_by_default(tmp_path: Path) -> None:
    config_path = write_phase2_config(tmp_path)
    env = os.environ.copy()
    env["HTTP_PROXY"] = "http://127.0.0.1:1082"

    result = run_python(
        ["scripts/download_phase2_real_data.py", "--config", str(config_path), "--tables", "stock_basic"],
        cwd=Path.cwd(),
        env=env,
    )

    assert result.returncode == 2
    assert "refusing market-data download" in result.stderr


def test_validation_reports_missing_tables_and_blocks_stock_strategies(tmp_path: Path) -> None:
    config_path = write_phase2_config(tmp_path)

    result = run_python(["scripts/validate_phase2_real_data.py", "--config", str(config_path)], cwd=Path.cwd())

    assert result.returncode == 0, result.stdout + result.stderr
    report_path = tmp_path / "reports" / "phase2_real_data_validation.md"
    report = report_path.read_text(encoding="utf-8")
    assert "missing_or_empty:stk_limit" in report
    assert "missing_or_empty:suspend_d" in report
    assert "`S2_real_stock_momentum`：禁止进真实排行榜" in report
    assert "不得静默降级为指数代理" in report


def test_month_beginning_and_ending_deposits_are_counted() -> None:
    index = pd.bdate_range("2020-01-01", "2021-12-31")
    returns = pd.DataFrame({"cash": np.zeros(len(index))}, index=index)
    target_weights = pd.DataFrame({"cash": np.zeros(len(index))}, index=index)
    zero_cost = ExecutionCost(
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps_each_side=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    _, beginning = simulate_path(
        returns,
        target_weights,
        monthly_deposit=30_000.0,
        deposit_timing="beginning",
        execution=zero_cost,
    )
    _, ending = simulate_path(
        returns,
        target_weights,
        monthly_deposit=30_000.0,
        deposit_timing="ending",
        execution=zero_cost,
    )

    assert beginning["w12"] == 360_000.0
    assert ending["w12"] == 360_000.0
    assert beginning["w24"] == 720_000.0
    assert ending["w24"] == 720_000.0
    assert first_trading_day_mask(index).sum() == 24
    assert last_trading_day_mask(index).sum() == 24


def test_12_and_24_month_target_judgment() -> None:
    windows = pd.DataFrame(
        [
            {
                "strategy": "candidate",
                "family": "S2",
                "deposit_timing": "beginning",
                "w12": 500_000.0,
                "w24": 1_200_000.0,
                "total_deposit": 720_000.0,
                "max_drawdown": 0.2,
                "avg_turnover": 0.1,
                "fee_drag": 100.0,
                "financing_drag": 0.0,
            },
            {
                "strategy": "candidate",
                "family": "S2",
                "deposit_timing": "beginning",
                "w12": 499_999.0,
                "w24": 1_300_000.0,
                "total_deposit": 720_000.0,
                "max_drawdown": 0.4,
                "avg_turnover": 0.1,
                "fee_drag": 100.0,
                "financing_drag": 0.0,
            },
        ]
    )

    leaderboard = aggregate_windows(windows, target_12=500_000.0, target_24=1_200_000.0)
    row = leaderboard.iloc[0]

    assert row["p_success"] == 0.5
    assert row["p_w12"] == 0.5
    assert row["p_w24"] == 1.0
    assert row["p_drawdown_gt_35"] == 0.5


def test_max_drawdown_calculation() -> None:
    equity = pd.Series([100.0, 120.0, 90.0, 150.0])
    assert max_drawdown(equity) == 0.25

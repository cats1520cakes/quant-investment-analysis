from __future__ import annotations

import subprocess
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_proof.free_sources.download_planner import refresh_download_plan, write_download_plan
from scripts.run_phase2_free_real_download_plan import (
    ISOLATION_MANIFEST_NAME,
    run_download_plan,
)


BAD_CODE = "sz.000022"
BAD_ERROR = (
    "DailyIntegrityError: invalid daily pair for sz.000022: "
    "daily_qfq: adj_close_for_signal must be positive and complete"
)


def _daily_frames(source_code: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    exchange, symbol = source_code.split(".", 1)
    ts_code = f"{symbol}.{exchange.upper()}"
    raw = pd.DataFrame(
        {
            "trade_date": ["20200102"],
            "ts_code": [ts_code],
            "source_code": [source_code],
            "open": [10.0],
            "high": [10.5],
            "low": [9.5],
            "close": [10.2],
            "pre_close": [10.0],
            "volume": [1000.0],
            "amount": [10000.0],
            "turnover_rate": [1.0],
            "pct_chg": [2.0],
            "pe_ttm": [10.0],
            "pb": [1.0],
            "ps_ttm": [2.0],
            "pcf_ttm": [3.0],
            "trade_status": [1],
            "is_st_raw": [0],
        }
    )
    qfq = pd.DataFrame(
        {
            "trade_date": ["20200102"],
            "ts_code": [ts_code],
            "source_code": [source_code],
            "adj_open_for_signal": [10.0],
            "adj_high_for_signal": [10.5],
            "adj_low_for_signal": [9.5],
            "adj_close_for_signal": [10.2],
        }
    )
    return raw, qfq


def _write_daily_pair(data_root: Path, source_code: str) -> None:
    filename = f"{source_code.replace('.', '_')}.parquet"
    raw_path = data_root / "raw" / "baostock" / "daily_raw" / filename
    qfq_path = data_root / "raw" / "baostock" / "daily_qfq" / filename
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    qfq_path.parent.mkdir(parents=True, exist_ok=True)
    raw, qfq = _daily_frames(source_code)
    raw.to_parquet(raw_path, index=False)
    qfq.to_parquet(qfq_path, index=False)


def _codes_from_command(command: list[str]) -> list[str]:
    codes_path = Path(command[command.index("--codes-file") + 1])
    return [line for line in codes_path.read_text(encoding="utf-8").splitlines() if line]


def _setup_plan(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_root = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"data_root: {data_root}\npaths:\n  manifest: 00_meta/manifests/free_real.csv\n",
        encoding="utf-8",
    )
    universe_path = tmp_path / "universe.csv"
    pd.DataFrame(
        {"source_code": ["sz.000001", BAD_CODE, "sz.000032"]}
    ).to_csv(universe_path, index=False)
    plan_path, _, _ = write_download_plan(data_root, universe_path, shard_size=2)
    return data_root, config_path, plan_path


class PairFailureRunner:
    def __init__(self, data_root: Path, *, fail_bad: bool = True) -> None:
        self.data_root = data_root
        self.fail_bad = fail_bad
        self.calls: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess:
        del cwd, check
        codes = _codes_from_command(command)
        self.calls.append(codes)
        failures: list[str] = []
        for source_code in codes:
            if source_code == BAD_CODE and self.fail_bad:
                failures.append(source_code)
                continue
            _write_daily_pair(self.data_root, source_code)
        if failures:
            error_log = self.data_root / "00_meta" / "errors" / "phase2_free_daily.log"
            error_log.parent.mkdir(parents=True, exist_ok=True)
            with error_log.open("a", encoding="utf-8") as handle:
                for source_code in failures:
                    handle.write(f"2026-07-11T09:32:03 {source_code}: {BAD_ERROR}\n")
        return subprocess.CompletedProcess(command, 1 if failures else 0)


def test_permanent_bad_pair_is_isolated_without_blocking_later_shards(
    tmp_path: Path,
) -> None:
    data_root, config_path, plan_path = _setup_plan(tmp_path)
    runner = PairFailureRunner(data_root)

    first_exit = run_download_plan(
        config_path=config_path,
        plan_path=plan_path,
        max_shards=1,
        run_command=runner,
    )

    assert first_exit == 0
    assert runner.calls == [["sz.000001", BAD_CODE]]
    first_plan = refresh_download_plan(data_root, plan_path)
    assert first_plan["status"].tolist() == ["partial", "pending"]
    assert first_plan["remaining_codes"].astype(int).tolist() == [1, 1]
    first_shard_file = Path(first_plan.loc[first_plan["shard_id"] == 1, "codes_file"].iloc[0])
    assert first_shard_file.read_text(encoding="utf-8").splitlines() == [
        "sz.000001",
        BAD_CODE,
    ]
    isolation_path = plan_path.parent / ISOLATION_MANIFEST_NAME
    isolated = pd.read_csv(isolation_path, dtype={"source_code": str})
    assert isolated.loc[0, "source_code"] == BAD_CODE
    assert isolated.loc[0, "status"] == "isolated"
    assert int(isolated.loc[0, "attempt_count"]) == 1
    assert isolated.loc[0, "last_pair_error"] == BAD_ERROR
    assert "file is missing" in isolated.loc[0, "last_integrity_error"]

    second_exit = run_download_plan(
        config_path=config_path,
        plan_path=plan_path,
        max_shards=1,
        run_command=runner,
    )

    assert second_exit == 1
    assert runner.calls[-2:] == [["sz.000032"], [BAD_CODE]]
    second_plan = refresh_download_plan(data_root, plan_path)
    assert second_plan["status"].tolist() == ["partial", "complete"]
    isolated = pd.read_csv(isolation_path, dtype={"source_code": str})
    assert isolated.loc[0, "status"] == "isolated"
    assert int(isolated.loc[0, "attempt_count"]) == 2
    assert int(second_plan["remaining_codes"].sum()) == 1


def test_isolated_pair_is_retried_and_resolved_only_after_integrity_passes(
    tmp_path: Path,
) -> None:
    data_root, config_path, plan_path = _setup_plan(tmp_path)
    runner = PairFailureRunner(data_root)
    assert (
        run_download_plan(
            config_path=config_path,
            plan_path=plan_path,
            max_shards=1,
            run_command=runner,
        )
        == 0
    )

    runner.fail_bad = False
    exit_code = run_download_plan(
        config_path=config_path,
        plan_path=plan_path,
        max_shards=1,
        run_command=runner,
    )

    assert exit_code == 0
    assert runner.calls[-2:] == [["sz.000032"], [BAD_CODE]]
    final = refresh_download_plan(data_root, plan_path)
    assert final["status"].tolist() == ["complete", "complete"]
    assert int(final["remaining_codes"].sum()) == 0
    isolated = pd.read_csv(
        plan_path.parent / ISOLATION_MANIFEST_NAME,
        dtype={"source_code": str},
    )
    assert isolated.loc[0, "status"] == "resolved"
    assert int(isolated.loc[0, "attempt_count"]) == 2
    assert isinstance(isolated.loc[0, "resolved_at"], str)
    assert isolated.loc[0, "resolved_at"]


def test_process_failure_without_pair_error_is_not_quarantined(
    tmp_path: Path,
) -> None:
    data_root, config_path, plan_path = _setup_plan(tmp_path)
    calls: list[list[str]] = []

    def fail_preflight(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess:
        del cwd, check
        calls.append(_codes_from_command(command))
        return subprocess.CompletedProcess(command, 2)

    exit_code = run_download_plan(
        config_path=config_path,
        plan_path=plan_path,
        max_shards=1,
        run_command=fail_preflight,
    )

    assert exit_code == 1
    assert calls == [["sz.000001", BAD_CODE]]
    isolation = pd.read_csv(plan_path.parent / ISOLATION_MANIFEST_NAME)
    assert isolation.empty
    plan = refresh_download_plan(data_root, plan_path)
    assert plan["status"].tolist() == ["pending", "pending"]


def test_malformed_isolation_manifest_fails_closed_before_download(
    tmp_path: Path,
) -> None:
    _, config_path, plan_path = _setup_plan(tmp_path)
    (plan_path.parent / ISOLATION_MANIFEST_NAME).write_text(
        "plan_id,source_code,status\nwrong,sz.000022,isolated\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="isolation manifest is missing fields"):
        run_download_plan(
            config_path=config_path,
            plan_path=plan_path,
            max_shards=1,
            run_command=lambda *args, **kwargs: pytest.fail("download must not start"),
        )

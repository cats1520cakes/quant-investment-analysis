from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant_proof.free_sources.download_planner import (
    missing_daily_codes,
    read_download_plan_provenance,
    refresh_download_plan,
    write_download_plan,
)


def _daily_frames(source_code: str, trade_date: str = "20200102") -> tuple[pd.DataFrame, pd.DataFrame]:
    exchange, symbol = source_code.split(".", 1)
    ts_code = f"{symbol}.{exchange.upper()}"
    raw = pd.DataFrame(
        {
            "trade_date": [trade_date],
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
            "trade_date": [trade_date],
            "ts_code": [ts_code],
            "source_code": [source_code],
            "adj_open_for_signal": [10.0],
            "adj_high_for_signal": [10.5],
            "adj_low_for_signal": [9.5],
            "adj_close_for_signal": [10.2],
        }
    )
    return raw, qfq


def _write_daily_pair(root: Path, source_code: str, include_qfq: bool = True) -> None:
    raw_root = root / "raw" / "baostock" / "daily_raw"
    qfq_root = root / "raw" / "baostock" / "daily_qfq"
    raw_root.mkdir(parents=True, exist_ok=True)
    qfq_root.mkdir(parents=True, exist_ok=True)
    filename = f"{source_code.replace('.', '_')}.parquet"
    raw, qfq = _daily_frames(source_code)
    raw.to_parquet(raw_root / filename, index=False)
    if include_qfq:
        qfq.to_parquet(qfq_root / filename, index=False)


def test_download_plan_requires_both_raw_and_qfq(tmp_path: Path) -> None:
    _write_daily_pair(tmp_path, "sh.600001")
    _write_daily_pair(tmp_path, "sh.600002", include_qfq=False)
    universe = pd.DataFrame({"source_code": ["sh.600001", "sh.600002", "sz.000001"]})

    missing = missing_daily_codes(tmp_path, universe)

    assert missing["source_code"].tolist() == ["sh.600002", "sz.000001"]


def test_write_download_plan_is_content_addressed_and_sharded(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.csv"
    pd.DataFrame({"source_code": ["sh.600001", "sh.600002", "sz.000001"]}).to_csv(universe_path, index=False)

    manifest_path, missing_path, manifest = write_download_plan(tmp_path, universe_path, shard_size=2)

    assert manifest_path.exists()
    assert missing_path.exists()
    assert manifest["codes"].tolist() == [2, 1]
    assert Path(manifest.iloc[0]["codes_file"]).read_text(encoding="utf-8").splitlines() == ["sh.600001", "sh.600002"]


def test_refresh_download_plan_marks_partial_and_complete(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.csv"
    pd.DataFrame({"source_code": ["sh.600001", "sh.600002"]}).to_csv(universe_path, index=False)
    manifest_path, _, _ = write_download_plan(tmp_path, universe_path, shard_size=2)
    _write_daily_pair(tmp_path, "sh.600001")

    partial = refresh_download_plan(tmp_path, manifest_path)
    assert partial.iloc[0]["status"] == "partial"
    assert int(partial.iloc[0]["remaining_codes"]) == 1

    _write_daily_pair(tmp_path, "sh.600002")
    complete = refresh_download_plan(tmp_path, manifest_path)
    assert complete.iloc[0]["status"] == "complete"


def test_zero_shard_plan_is_a_complete_provenance_definition(tmp_path: Path) -> None:
    _write_daily_pair(tmp_path, "sh.600001")
    universe_path = tmp_path / "universe.csv"
    pd.DataFrame({"source_code": ["sh.600001"]}).to_csv(universe_path, index=False)
    manifest_path, _, manifest = write_download_plan(
        tmp_path,
        universe_path,
        shard_size=2,
    )

    assert manifest.empty
    refresh_download_plan(tmp_path, manifest_path)
    provenance = read_download_plan_provenance(
        manifest_path,
        expected_codes={"sh.600001"},
    )

    assert provenance["status"] == "complete"
    assert provenance["shards"] == 0
    assert provenance["planned_codes"] == 0
    assert provenance["remaining_codes"] == 0

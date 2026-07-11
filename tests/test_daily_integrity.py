from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from quant_proof.free_sources import daily_integrity
from quant_proof.free_sources.daily_integrity import inspect_daily_pairs, validate_daily_pair_frames
from test_download_planner import _daily_frames


def test_daily_pair_requires_schema_unique_keys_source_code_and_matching_keys() -> None:
    raw, qfq = _daily_frames("sh.600001")
    assert validate_daily_pair_frames(raw, qfq, "sh.600001").complete

    assert not validate_daily_pair_frames(raw.iloc[0:0], qfq, "sh.600001").complete
    assert not validate_daily_pair_frames(raw.drop(columns="close"), qfq, "sh.600001").complete
    assert not validate_daily_pair_frames(pd.concat([raw, raw], ignore_index=True), qfq, "sh.600001").complete
    assert not validate_daily_pair_frames(raw.assign(source_code="sh.600002"), qfq, "sh.600001").complete
    assert not validate_daily_pair_frames(raw.assign(ts_code="600002.SH"), qfq, "sh.600001").complete
    assert not validate_daily_pair_frames(raw.assign(trade_date="2020-01-02"), qfq, "sh.600001").complete
    assert not validate_daily_pair_frames(raw, qfq.assign(adj_close_for_signal=0.0), "sh.600001").complete
    assert not validate_daily_pair_frames(raw, qfq.assign(trade_date="20200103"), "sh.600001").complete


def test_qfq_may_be_missing_only_on_an_explicit_non_trading_terminal_row() -> None:
    raw, qfq = _daily_frames("sz.000022")
    terminal_raw = raw.assign(
        trade_date="20200103",
        open=float("nan"),
        high=float("nan"),
        low=float("nan"),
        close=float("nan"),
        trade_status=0,
    )
    terminal_qfq = qfq.assign(
        trade_date="20200103",
        adj_open_for_signal=float("nan"),
        adj_high_for_signal=float("nan"),
        adj_low_for_signal=float("nan"),
        adj_close_for_signal=float("nan"),
        trade_status=0,
    )
    qfq = qfq.assign(trade_status=1)
    raw_pair = pd.concat([raw, terminal_raw], ignore_index=True)
    qfq_pair = pd.concat([qfq, terminal_qfq], ignore_index=True)

    assert validate_daily_pair_frames(raw_pair, qfq_pair, "sz.000022").complete
    assert not validate_daily_pair_frames(
        raw_pair,
        qfq_pair.assign(trade_status=[1, 1]),
        "sz.000022",
    ).complete
    assert not validate_daily_pair_frames(
        raw_pair,
        qfq_pair.assign(adj_close_for_signal=[10.2, 0.0]),
        "sz.000022",
    ).complete


def test_integrity_cache_reuses_unchanged_files_and_rechecks_changed_side(tmp_path: Path, monkeypatch) -> None:
    source_code = "sh.600001"
    raw, qfq = _daily_frames(source_code)
    raw_path = daily_integrity.daily_file_path(tmp_path, "daily_raw", source_code)
    qfq_path = daily_integrity.daily_file_path(tmp_path, "daily_qfq", source_code)
    raw_path.parent.mkdir(parents=True)
    qfq_path.parent.mkdir(parents=True)
    raw.to_parquet(raw_path, index=False)
    qfq.to_parquet(qfq_path, index=False)

    original_read_parquet = pd.read_parquet
    reads: list[Path] = []

    def counted_read_parquet(path, *args, **kwargs):
        reads.append(Path(path))
        return original_read_parquet(path, *args, **kwargs)

    monkeypatch.setattr(daily_integrity.pd, "read_parquet", counted_read_parquet)
    first = inspect_daily_pairs(tmp_path, [source_code])[source_code]
    second = inspect_daily_pairs(tmp_path, [source_code])[source_code]

    assert first.complete
    assert second.complete and second.cache_hit
    assert reads == [raw_path, qfq_path]

    stat = raw_path.stat()
    os.utime(raw_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    third = inspect_daily_pairs(tmp_path, [source_code])[source_code]

    assert third.complete
    assert reads == [raw_path, qfq_path, raw_path]

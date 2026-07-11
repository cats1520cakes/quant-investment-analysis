from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.download_phase3_cffex_trade_parameters_cloud import (
    _atomic_json,
    _calendar_dates,
)


def test_calendar_crawl_dates_are_explicit_inclusive_natural_days() -> None:
    assert _calendar_dates("2024-02-28", "2024-03-01") == [
        "20240228",
        "20240229",
        "20240301",
    ]


def test_calendar_crawl_rejects_reverse_bounds() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        _calendar_dates("2025-01-02", "2025-01-01")


def test_attempt_ledger_atomic_write_replaces_stale_content(tmp_path: Path) -> None:
    output = tmp_path / "attempts.json"
    output.write_text("stale", encoding="utf-8")
    _atomic_json(output, {"20240102": {"status": "downloaded"}})
    assert json.loads(output.read_text(encoding="utf-8"))["20240102"]["status"] == "downloaded"
    assert not list(tmp_path.glob(".*.tmp"))

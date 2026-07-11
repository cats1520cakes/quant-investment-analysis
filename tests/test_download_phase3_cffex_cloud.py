from pathlib import Path

from scripts.download_phase3_cffex_cloud import resolve_cloud_output_paths


def test_scoped_cloud_download_cannot_overwrite_canonical_panel() -> None:
    panel, master = resolve_cloud_output_paths(Path("data"), "20260101", "20260630", 6, False)
    assert panel.name == "cffex_contract_daily_20260101_20260630_6m.parquet"
    assert master.name == "cffex_contract_master_20260101_20260630_6m.parquet"


def test_only_full_default_scope_uses_canonical_names() -> None:
    panel, master = resolve_cloud_output_paths(Path("data"), "20100416", "20260630", 195, True)
    assert panel.name == "cffex_contract_daily.parquet"
    assert master.name == "cffex_contract_master.parquet"

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from quant_proof.free_sources.cffex_adapter import (
    CffexDataError,
    build_cffex_contract_master,
    build_cffex_contract_panel,
    cffex_panel_manifest_path,
    download_cffex_month,
    parse_cffex_daily_csv,
    validate_cffex_panel_manifest,
    validate_cffex_contract_master_manifest,
)
from quant_proof.network_guard import DirectHttpResponse, DirectSocketRoute


def _daily_csv() -> bytes:
    text = "\n".join(
        [
            "合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,今收盘,今结算,前结算,涨跌1,涨跌2,Delta",
            "IF2401,3439.4,3442,3388,53653,5484659.652,100995,-4709,3388.2,3394.8,3439.8,-51.6,-45,--",
            "IO2401-C-3400,80,85,75,100,80.5,1000,20,79,79.2,81,-2,-1.8,0.52",
            "小计,,,,53753,5484740.152,101995,-4689,,,,,,",
        ]
    )
    return text.encode("gb18030")


def _month_zip_bytes() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("20240102_1.csv", _daily_csv())
        archive.writestr("20240103_1.csv", _daily_csv())
    return buffer.getvalue()


def test_parse_cffex_daily_csv_keeps_real_futures_and_options() -> None:
    frame = parse_cffex_daily_csv(_daily_csv(), "20240102", "202401.zip:20240102_1.csv")

    assert frame["contract"].tolist() == ["IF2401", "IO2401-C-3400"]
    future = frame.loc[frame["contract"] == "IF2401"].iloc[0]
    option = frame.loc[frame["contract"] == "IO2401-C-3400"].iloc[0]
    assert future["instrument_type"] == "future"
    assert future["underlying_index"] == "CSI300"
    assert future["multiplier"] == 300.0
    assert option["instrument_type"] == "option"
    assert option["option_type"] == "call"
    assert option["strike"] == 3400.0
    assert option["delta"] == 0.52
    assert bool(option["open_executable"])
    assert bool(option["settlement_mark_valid"])
    assert set(frame["source_tier"]) == {"official_exchange_daily"}
    assert set(frame["execution_tier"]) == {"daily_settlement_no_quotes"}


def test_download_cffex_month_uses_atomic_validated_archive_and_cache(tmp_path: Path) -> None:
    route = DirectSocketRoute(
        host="www.cffex.com.cn",
        resolved_ip="58.32.205.2",
        port=80,
        interface="en0",
        interface_index=12,
        interface_ipv4="192.168.3.36",
        dns_server="192.168.3.1",
        route_interface="utun5",
        connected_local=("192.168.3.36", 55001),
        connected_peer=("58.32.205.2", 80),
    )

    def fake_fetcher(url, **kwargs):
        return DirectHttpResponse(
            url=url,
            status=200,
            reason="OK",
            headers={"content-type": "application/zip"},
            body=_month_zip_bytes(),
            route=route,
        )

    first = download_cffex_month(tmp_path, "202401", fetcher=fake_fetcher)
    assert first.status == "downloaded"
    assert first.entries == 2
    assert first.rows == 4
    assert first.local_ip == "192.168.3.36"

    def no_network(*args, **kwargs):
        raise AssertionError("valid archive should be reused")

    second = download_cffex_month(tmp_path, "202401", fetcher=no_network)
    assert second.status == "cached_valid"
    assert second.sha256 == first.sha256


def test_build_cffex_panel_is_manifest_bound(tmp_path: Path) -> None:
    archive_path = tmp_path / "202401.zip"
    archive_path.write_bytes(_month_zip_bytes())
    panel_path = tmp_path / "cffex_contract_daily.parquet"

    output = build_cffex_contract_panel([archive_path], panel_path)
    frame = pd.read_parquet(output)
    manifest = validate_cffex_panel_manifest(output)

    assert len(frame) == 4
    assert int(manifest["rows"]) == 4
    assert manifest["products"] == ["IF", "IO"]
    assert manifest["source_tier"] == "official_exchange_daily"

    manifest_path = cffex_panel_manifest_path(output)
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["panel_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(CffexDataError, match="hash"):
        validate_cffex_panel_manifest(output)


def test_contract_master_is_bound_to_official_daily_panel(tmp_path: Path) -> None:
    archive_path = tmp_path / "202401.zip"
    archive_path.write_bytes(_month_zip_bytes())
    panel_path = build_cffex_contract_panel(
        [archive_path],
        tmp_path / "cffex_contract_daily.parquet",
    )

    master_path = build_cffex_contract_master(
        panel_path,
        tmp_path / "cffex_contract_master.parquet",
    )
    master = pd.read_parquet(master_path)
    manifest = validate_cffex_contract_master_manifest(master_path, panel_path)

    assert set(master["contract"]) == {"IF2401", "IO2401-C-3400"}
    assert set(master["last_trade_date"]) == {"20240103"}
    option = master.loc[master["instrument_type"].eq("option")].iloc[0]
    assert option["exercise_style"] == "european"
    assert option["settlement_style"] == "premium_cash_settled"
    assert int(manifest["rows"]) == 2

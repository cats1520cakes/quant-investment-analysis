from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from quant_proof.free_sources.cffex_settlement_params import (
    CffexArtifactError,
    CffexContentError,
    CffexDiscoveryError,
    CffexLookupError,
    DiscoveredSettlementCsv,
    FeeUnit,
    SettlementDownloadRecord,
    ShortOptionsDisabledError,
    build_cffex_settlement_artifact,
    decode_cffex_settlement_csv,
    discover_cffex_settlement_csvs,
    download_cffex_settlement_csv,
    latest_exact_settlement_record,
    load_settlement_download_manifest,
    parse_cffex_settlement_csv,
    parse_fee_standard,
    resolve_settlement_output_path,
    settlement_artifact_manifest_path,
    settlement_local_path,
    sha256_bytes,
    validate_cffex_settlement_artifact,
    validate_official_csv_path,
    validate_page_numbers,
)
from quant_proof.network_guard import DirectHttpResponse, DirectSocketRoute


def _payload(
    snapshot_date: str = "20260623",
    *,
    future_contract: str = "IF2607",
    future_margin: str = "12%",
    future_fee: str = "万分之0.23",
    future_close_today: str = "1000%",
    padded_contract: bool = False,
    treasury_fee: str = "3元/手",
    include_options: bool = True,
    option_title_date: str | None = None,
) -> bytes:
    displayed_contract = future_contract + (" " * 20 if padded_contract else "")
    lines = [
        f"期货合约结算业务参数表（{snapshot_date}）",
        "期货合约,合约多头保证金标准,合约空头保证金标准,交易手续费标准,交割手续费标准,平今仓收取率",
        f"{displayed_contract},{future_margin},{future_margin},{future_fee},万分之1,{future_close_today}",
        f"T2609,2%,2%,{treasury_fee},2.5元/手,0%",
    ]
    if include_options:
        lines.extend(
            [
                f"期权合约结算业务参数表（{option_title_date or snapshot_date}）",
                "合约系列,保证金调整系数,最低保障系数,交易手续费标准,行权（履约）手续费标准,平今仓收取率",
                "IO2607,12%,0.5,15元/手,1元/手,100%",
            ]
        )
    return ("\n".join(lines) + "\n").encode("gb18030")


def _official_path(snapshot_date: str) -> str:
    return f"/sj/jscs/{snapshot_date[:6]}/{snapshot_date[6:]}/{snapshot_date}_1.csv"


def _source(snapshot_date: str = "20260623", pages: tuple[int, ...] = (1,)) -> DiscoveredSettlementCsv:
    path = _official_path(snapshot_date)
    return DiscoveredSettlementCsv(
        official_path=path,
        url="http://www.cffex.com.cn" + path,
        snapshot_date=snapshot_date,
        discovery_pages=pages,
    )


def _route() -> DirectSocketRoute:
    return DirectSocketRoute(
        host="www.cffex.com.cn",
        resolved_ip="58.33.201.201",
        port=80,
        interface="en0",
        interface_index=12,
        interface_ipv4="192.168.3.36",
        dns_server="192.168.3.1",
        route_interface="utun5",
        connected_local=("192.168.3.36", 55001),
        connected_peer=("58.33.201.201", 80),
    )


def _response(url: str, body: bytes, *, status: int = 200) -> DirectHttpResponse:
    return DirectHttpResponse(
        url=url,
        status=status,
        reason="OK" if status == 200 else "Not Found",
        headers={},
        body=body,
        route=_route(),
    )


def _page(page: int, total: int, paths: list[str]) -> bytes:
    links = "".join(f'<a href="{path}">csv</a>' for path in paths)
    return (
        "<!doctype html><html><body>"
        f'<a href="#" onclick="lastPage({page})">prev</a>'
        f'<a href="#" onclick="nextPage({page}, {total})">next</a>'
        f'<a href="#" onkeyup="toPage(\'{total}\')">jump</a>'
        f"{links}</body></html>"
    ).encode("utf-8")


def _download_record(tmp_path: Path, snapshot_date: str = "20260623", pages: tuple[int, ...] = (1,)) -> SettlementDownloadRecord:
    source = _source(snapshot_date, pages)
    payload = _payload(
        snapshot_date,
        future_contract="IF1005" if snapshot_date.startswith("2010") else "IF2607",
        include_options=not snapshot_date.startswith("2010"),
    )
    local_path = settlement_local_path(tmp_path / "raw", source)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(payload)
    return SettlementDownloadRecord(
        official_path=source.official_path,
        url=source.url,
        snapshot_date=snapshot_date,
        discovery_pages=pages,
        local_path=str(local_path),
        bytes=len(payload),
        sha256=sha256_bytes(payload),
        local_ip="192.168.3.36",
        remote_ip="58.33.201.201",
        interface="en0",
        status="downloaded",
        checked_at_utc="2026-07-11T00:00:00+00:00",
        fetched_at_utc="2026-07-11T00:00:00+00:00",
    )


def _build_one_source_artifact(tmp_path: Path) -> tuple[Path, SettlementDownloadRecord]:
    record = _download_record(tmp_path)
    canonical = tmp_path / "processed" / "cffex_settlement_params.parquet"
    artifact = build_cffex_settlement_artifact(
        [record],
        canonical,
        selected_pages=[1],
        expected_pages=[1],
        expected_unique_csvs=1,
        expected_first_snapshot="20260623",
        expected_last_snapshot="20260623",
    )
    assert artifact.canonical
    return artifact.parquet_path, record


def test_discovery_enumerates_deduplicates_and_passes_physical_route_arguments() -> None:
    paths = [_official_path("20240103"), _official_path("20240102"), _official_path("20240101")]
    pages = {
        1: _page(1, 2, [paths[0], paths[1], paths[1]]),
        2: _page(2, 2, [paths[1], paths[2]]),
    }
    calls: list[tuple[str, dict]] = []

    def fake_fetcher(url: str, **kwargs) -> DirectHttpResponse:
        calls.append((url, kwargs))
        page = 1 if url.endswith("jscs.html") else 2
        return _response(url, pages[page])

    discovered = discover_cffex_settlement_csvs(
        page_numbers=[1, 2],
        expected_total_pages=2,
        expected_unique_csvs=3,
        expected_first_snapshot="20240101",
        expected_last_snapshot="20240103",
        interface="en0",
        dns_server="192.168.3.1",
        timeout_seconds=17,
        max_page_bytes=12345,
        fetcher=fake_fetcher,
    )

    assert [item.snapshot_date for item in discovered] == ["20240101", "20240102", "20240103"]
    assert discovered[1].discovery_pages == (1, 2)
    assert [url for url, _ in calls] == [
        "http://www.cffex.com.cn/cn/jscs.html",
        "http://www.cffex.com.cn/cn/jscs_2.html",
    ]
    assert all(call["interface"] == "en0" for _, call in calls)
    assert all(call["dns_server"] == "192.168.3.1" for _, call in calls)
    assert all(call["timeout_seconds"] == 17 for _, call in calls)
    assert all(call["max_bytes"] == 12345 for _, call in calls)


def test_discovery_rejects_missing_continuity_malformed_dates_and_future_growth() -> None:
    with pytest.raises(CffexDiscoveryError, match="not continuous"):
        validate_page_numbers([1, 3], expected_total_pages=3)
    with pytest.raises(CffexDiscoveryError, match="date disagreement"):
        validate_official_csv_path("/sj/jscs/202401/02/20240103_1.csv")

    malformed = _page(1, 1, ["/sj/jscs/202401/02/20240103_1.csv"])
    with pytest.raises(CffexDiscoveryError, match="date disagreement"):
        discover_cffex_settlement_csvs(
            page_numbers=[1],
            expected_total_pages=1,
            expected_unique_csvs=1,
            expected_first_snapshot=None,
            expected_last_snapshot=None,
            fetcher=lambda url, **kwargs: _response(url, malformed),
        )

    grown = _page(1, 2, [_official_path("20240101")])
    with pytest.raises(CffexDiscoveryError, match="grew beyond"):
        discover_cffex_settlement_csvs(
            page_numbers=[1],
            expected_total_pages=1,
            expected_unique_csvs=1,
            expected_first_snapshot=None,
            expected_last_snapshot=None,
            fetcher=lambda url, **kwargs: _response(url, grown),
        )


def test_gb18030_historical_padding_and_large_close_today_multiplier_are_preserved() -> None:
    old = _payload(
        "20100414",
        future_contract="IF1005",
        future_margin="15%",
        future_fee="万分之0.5",
        future_close_today="100%",
        padded_contract=True,
        include_options=False,
    )
    assert "期货合约" in decode_cffex_settlement_csv(old)
    old_frame = parse_cffex_settlement_csv(old, "20100414")
    assert old_frame.iloc[0]["contract_or_series"] == "IF1005"
    assert old_frame.iloc[0]["raw_contract_or_series"].startswith("IF1005 ")
    assert old_frame.iloc[0]["long_margin_rate"] == pytest.approx(0.15)
    assert old_frame.iloc[0]["close_today_fee_multiplier"] == pytest.approx(1.0)

    stress = _payload(
        "20150907",
        future_contract="IF1510",
        future_margin="40%",
        future_close_today="10000%",
        include_options=False,
    )
    stress_frame = parse_cffex_settlement_csv(stress, "20150907")
    assert stress_frame.iloc[0]["long_margin_rate"] == pytest.approx(0.40)
    assert stress_frame.iloc[0]["close_today_fee_multiplier"] == pytest.approx(100.0)


def test_section_parser_keeps_mixed_fee_units_and_option_short_margin_data() -> None:
    frame = parse_cffex_settlement_csv(_payload(), "20260623")

    assert frame["contract_or_series"].tolist() == ["IF2607", "IO2607"]
    future = frame.loc[frame["instrument_type"].eq("future")].iloc[0]
    option = frame.loc[frame["instrument_type"].eq("option")].iloc[0]
    assert future["trading_fee_unit"] == FeeUnit.NOTIONAL_RATE.value
    assert future["trading_fee_value"] == pytest.approx(0.23 / 10_000)
    assert option["trading_fee_unit"] == FeeUnit.CURRENCY_PER_CONTRACT.value
    assert option["trading_fee_value"] == pytest.approx(15.0)
    assert option["option_margin_adjustment_rate"] == pytest.approx(0.12)
    assert option["option_minimum_guarantee_coefficient"] == pytest.approx(0.5)
    assert not bool(option["option_shorting_enabled"])
    assert option["settlement_fee_kind"] == "exercise"
    assert "行权（履约）手续费标准" in option["raw_source_fields_json"]


def test_stale_historical_option_title_is_preserved_without_overclaiming_effective_date() -> None:
    frame = parse_cffex_settlement_csv(
        _payload("20200120", future_contract="IF2002", option_title_date="20191223"),
        "20200120",
    )
    future = frame.loc[frame["instrument_type"].eq("future")].iloc[0]
    option = frame.loc[frame["instrument_type"].eq("option")].iloc[0]

    assert future["source_section_title_date"] == "20200120"
    assert bool(future["source_section_title_matches_snapshot"])
    assert option["snapshot_date"] == "20200120"
    assert option["source_section_title_date"] == "20191223"
    assert not bool(option["source_section_title_matches_snapshot"])


def test_out_of_scope_treasury_rows_are_validated_before_exclusion() -> None:
    valid = parse_cffex_settlement_csv(_payload(treasury_fee=""), "20260623")
    assert "T" not in set(valid["product"])

    malformed = _payload().decode("gb18030").replace(
        "T2609,2%,2%,3元/手,2.5元/手,0%",
        "T2609,2%,2%,3元/手,0%",
    ).encode("gb18030")
    with pytest.raises(CffexContentError, match="exactly six fields"):
        parse_cffex_settlement_csv(malformed, "20260623")


def test_unsupported_fee_unit_is_explicit_and_fails_closed_in_a_section() -> None:
    parsed = parse_fee_standard("每笔3元")
    assert parsed.unit is FeeUnit.UNSUPPORTED
    assert parsed.value is None
    with pytest.raises(CffexContentError, match="unsupported fee unit"):
        parse_cffex_settlement_csv(_payload(future_fee="每笔3元"), "20260623")


@pytest.mark.parametrize(
    "body,error",
    [
        (b"", "empty"),
        (b"<!doctype html><html><body>not found</body></html>", "HTML"),
    ],
)
def test_empty_and_html_csv_payloads_fail_closed(body: bytes, error: str) -> None:
    with pytest.raises(CffexContentError, match=error):
        parse_cffex_settlement_csv(body, "20260623")


def test_download_quarantines_invalid_cache_records_route_and_reuses_only_manifest_bound_cache(
    tmp_path: Path,
) -> None:
    source = _source()
    raw_root = tmp_path / "raw"
    manifest_path = tmp_path / "manifest.json"
    target = settlement_local_path(raw_root, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"<html>stale cache</html>")
    calls: list[dict] = []

    def fake_fetcher(url: str, **kwargs) -> DirectHttpResponse:
        calls.append(kwargs)
        return _response(url, _payload())

    first = download_cffex_settlement_csv(
        source,
        raw_root=raw_root,
        manifest_path=manifest_path,
        interface="en0",
        dns_server="192.168.3.1",
        timeout_seconds=31,
        max_csv_bytes=7777,
        expected_pages=[1],
        expected_unique_csvs=1,
        fetcher=fake_fetcher,
    )

    assert first.status == "replaced_invalid_cache"
    assert Path(first.quarantine_path).read_bytes() == b"<html>stale cache</html>"
    assert first.local_ip == "192.168.3.36"
    assert first.remote_ip == "58.33.201.201"
    assert calls == [
        {
            "interface": "en0",
            "dns_server": "192.168.3.1",
            "timeout_seconds": 31,
            "max_bytes": 7777,
        }
    ]

    second = download_cffex_settlement_csv(
        source,
        raw_root=raw_root,
        manifest_path=manifest_path,
        expected_pages=[1],
        expected_unique_csvs=1,
        fetcher=lambda *args, **kwargs: pytest.fail("manifest-bound valid cache should avoid the network"),
    )
    assert second.status == "cached_valid"
    assert second.sha256 == first.sha256


def test_downloaded_html_is_quarantined_and_manifested_as_failed(tmp_path: Path) -> None:
    source = _source()
    manifest_path = tmp_path / "manifest.json"
    html = b"<!doctype html><html><body>maintenance</body></html>"

    with pytest.raises(CffexContentError, match="invalid CFFEX settlement response"):
        download_cffex_settlement_csv(
            source,
            raw_root=tmp_path / "raw",
            manifest_path=manifest_path,
            expected_pages=[1],
            expected_unique_csvs=1,
            fetcher=lambda url, **kwargs: _response(url, html),
        )

    manifest = load_settlement_download_manifest(manifest_path)
    record = manifest["records"][0]
    assert record["status"] == "failed_validation"
    assert Path(record["quarantine_path"]).read_bytes() == html
    assert not Path(record["local_path"]).exists()


def test_physical_route_failure_is_manifested_without_fallback(tmp_path: Path) -> None:
    source = _source()
    manifest_path = tmp_path / "manifest.json"

    def failed_route(url: str, **kwargs) -> DirectHttpResponse:
        raise RuntimeError("physical DNS timeout")

    with pytest.raises(Exception, match="physical-route.*physical DNS timeout"):
        download_cffex_settlement_csv(
            source,
            raw_root=tmp_path / "raw",
            manifest_path=manifest_path,
            expected_pages=[1],
            expected_unique_csvs=1,
            fetcher=failed_route,
        )

    record = load_settlement_download_manifest(manifest_path)["records"][0]
    assert record["status"] == "failed_network"
    assert record["bytes"] == 0
    assert record["local_ip"] == ""


def test_artifact_manifest_binds_parquet_sources_and_parser(tmp_path: Path) -> None:
    parquet_path, record = _build_one_source_artifact(tmp_path)
    manifest = validate_cffex_settlement_artifact(parquet_path)

    assert manifest["source_count"] == 1
    assert manifest["sources"][0]["sha256"] == record.sha256
    assert manifest["snapshot_semantics"].startswith("official publication snapshot")
    assert manifest["option_shorting_enabled"] is False
    assert pd.read_parquet(parquet_path)["source_sha256"].unique().tolist() == [record.sha256]


def test_artifact_validation_rejects_stale_parser_and_parquet_hash(tmp_path: Path) -> None:
    parquet_path, _ = _build_one_source_artifact(tmp_path)
    manifest_path = settlement_artifact_manifest_path(parquet_path)
    original = json.loads(manifest_path.read_text(encoding="utf-8"))

    stale = dict(original)
    stale["parser_source_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(CffexArtifactError, match="stale parser"):
        validate_cffex_settlement_artifact(parquet_path)

    manifest_path.write_text(json.dumps(original), encoding="utf-8")
    parquet_path.write_bytes(parquet_path.read_bytes() + b"tamper")
    with pytest.raises(CffexArtifactError, match="size drifted|hash"):
        validate_cffex_settlement_artifact(parquet_path)


def test_artifact_validation_rejects_source_hash_drift(tmp_path: Path) -> None:
    parquet_path, record = _build_one_source_artifact(tmp_path)
    source_path = Path(record.local_path)
    source_path.write_bytes(source_path.read_bytes() + b"\n")

    with pytest.raises(CffexArtifactError, match="source hash/size drifted"):
        validate_cffex_settlement_artifact(parquet_path)


def test_exact_asof_lookup_has_no_future_backfill_or_product_substitution() -> None:
    first = parse_cffex_settlement_csv(
        _payload("20240101", future_contract="IF2401", future_margin="12%", include_options=False),
        "20240101",
    )
    later = parse_cffex_settlement_csv(
        _payload("20240103", future_contract="IF2401", future_margin="15%", include_options=False),
        "20240103",
    )
    frame = pd.concat([first, later], ignore_index=True)

    record = latest_exact_settlement_record(frame, "IF2401", "20240102")
    assert record["snapshot_date"] == "20240101"
    assert record["long_margin_rate"] == pytest.approx(0.12)
    assert latest_exact_settlement_record(frame, "IF2401", "20240103")["long_margin_rate"] == pytest.approx(0.15)
    with pytest.raises(CffexLookupError, match="future backfill"):
        latest_exact_settlement_record(frame, "IF2401", "20231231")
    with pytest.raises(CffexLookupError, match="product substitution"):
        latest_exact_settlement_record(frame, "IF", "20240103")
    with pytest.raises(CffexLookupError, match="product substitution"):
        latest_exact_settlement_record(frame, "IF2402", "20240103")


def test_exact_lookup_retains_option_margin_but_blocks_short_option_use() -> None:
    frame = parse_cffex_settlement_csv(_payload(), "20260623")
    option = latest_exact_settlement_record(frame, "IO2607", "20260623")
    assert option["option_minimum_guarantee_coefficient"] == pytest.approx(0.5)
    with pytest.raises(ShortOptionsDisabledError, match="short options are disabled"):
        latest_exact_settlement_record(frame, "IO2607", "20260623", position_side="short")


def test_partial_page_or_date_output_cannot_overwrite_canonical(tmp_path: Path) -> None:
    canonical = tmp_path / "processed" / "cffex_settlement_params.parquet"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"canonical-do-not-touch")
    record = _download_record(tmp_path)

    partial = build_cffex_settlement_artifact(
        [record],
        canonical,
        selected_pages=[1],
        expected_pages=[1, 2],
        expected_unique_csvs=2,
    )
    assert not partial.canonical
    assert partial.parquet_path != canonical
    assert ".scope-p01-p01" in partial.parquet_path.name
    assert canonical.read_bytes() == b"canonical-do-not-touch"

    date_scoped = resolve_settlement_output_path(
        canonical,
        selected_pages=[1, 2],
        expected_pages=[1, 2],
        start_date="20260623",
        end_date="20260623",
    )
    assert date_scoped != canonical
    assert ".scope-d20260623-20260623" in date_scoped.name


def test_canonical_completeness_gate_rejects_missing_source_count(tmp_path: Path) -> None:
    record = _download_record(tmp_path, pages=(1, 2))
    with pytest.raises(CffexArtifactError, match="requires 2 source CSVs"):
        build_cffex_settlement_artifact(
            [record],
            tmp_path / "canonical.parquet",
            selected_pages=[1, 2],
            expected_pages=[1, 2],
            expected_unique_csvs=2,
            expected_first_snapshot="20260623",
            expected_last_snapshot="20260623",
        )


def test_config_freezes_36_pages_360_sources_and_external_drive_paths() -> None:
    config_path = Path(__file__).parents[1] / "config" / "phase3_cffex_settlement_params.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["source"]["page_numbers"] == list(range(1, 37))
    assert config["source"]["expected_unique_csvs"] == 360
    assert config["source"]["fail_on_unconfigured_page_growth"] is True
    assert config["data_root"] == "/Volumes/PSSD1TB/量化数据"
    assert config["network"]["physical_dns_server"] == "auto"
    assert config["network"]["physical_dns_mode"] == "dhcp_from_physical_interface"
    assert config["paths"]["raw_settlement_params"] == "raw/cffex/settlement_params"
    assert config["paths"]["canonical_parquet"].startswith("processed/phase3_derivatives/")
    assert config["policy"]["short_options_enabled"] is False

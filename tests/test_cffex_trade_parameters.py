from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
import yaml

from quant_proof.free_sources.cffex_adapter import (
    build_cffex_contract_master,
    build_cffex_contract_panel,
)
from quant_proof.free_sources.cffex_trade_parameters import (
    CFFEX_TRADE_PARAMETERS_URL,
    CffexTradeParameterError,
    build_cffex_trade_parameter_metadata,
    cffex_trade_parameter_metadata_manifest_path,
    cffex_trade_parameter_path,
    cffex_trade_parameters_url,
    derive_required_snapshot_dates,
    derive_required_snapshot_dates_from_frames,
    download_cffex_trade_parameters,
    parse_cffex_trade_parameters_csv,
    parse_position_limit,
    query_exact_contract,
    query_exact_contract_asof,
    reconcile_cffex_last_trade_dates,
    scoped_artifact_path,
    validate_cffex_trade_parameter_download_manifest,
    validate_cffex_trade_parameter_metadata_manifest,
    validate_cffex_trade_parameter_rows,
    write_cffex_trade_parameter_download_manifest,
)
from quant_proof.network_guard import DirectHttpResponse, DirectSocketRoute


def _parameter_row(
    contract: str,
    contract_month: str,
    open_date: str,
    last_trade_date: str,
    *,
    basis_price: str = "3500.2",
    position_limit: str = "5000",
    upper_percentage: str = "10%",
    lower_percentage: str = "10%",
) -> list[str]:
    return [
        contract,
        contract_month,
        basis_price,
        open_date,
        last_trade_date,
        upper_percentage,
        lower_percentage,
        "3850.2",
        "3150.2",
        position_limit,
    ]


def _parameter_csv(
    snapshot_date: str,
    rows: list[list[str]],
    *,
    title_date: str | None = None,
) -> bytes:
    text = "\n".join(
        [
            f"合约交易业务参数表（{title_date or snapshot_date}）",
            "合约代码,合约月份,挂盘基准价,上市日,最后交易日,"
            "涨停板幅度（%）,跌停板幅度（%）,涨停板价位,跌停板价位,持仓限额",
            *[",".join(row) for row in rows],
        ]
    )
    return text.encode("gb18030")


def _daily_csv(*contracts: str) -> bytes:
    text = "\n".join(
        [
            "合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,"
            "今收盘,今结算,前结算,涨跌1,涨跌2,Delta",
            *[
                f"{contract},3439.4,3442,3388,100,1000,500,5,3388.2,3394.8,3439.8,-51.6,-45,--"
                for contract in contracts
            ],
        ]
    )
    return text.encode("gb18030")


def _month_zip() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("20240102_1.csv", _daily_csv("IF2401"))
        archive.writestr("20240103_1.csv", _daily_csv("IF2402"))
    return output.getvalue()


def _route() -> DirectSocketRoute:
    return DirectSocketRoute(
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


def _response(url: str, body: bytes) -> DirectHttpResponse:
    return DirectHttpResponse(
        url=url,
        status=200,
        reason="OK",
        headers={"content-type": "text/csv"},
        body=body,
        route=_route(),
    )


def _canonical_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    archive = tmp_path / "202401.zip"
    archive.write_bytes(_month_zip())
    panel = build_cffex_contract_panel(
        [archive], tmp_path / "cffex_contract_daily.parquet"
    )
    master = build_cffex_contract_master(
        panel, tmp_path / "cffex_contract_master.parquet"
    )
    manifest = tmp_path / "cffex_trade_parameters_manifest.csv"
    payloads = {
        "20240102": _parameter_csv(
            "20240102",
            [_parameter_row("IF2401", "2401", "20240102", "20240102")],
        ),
        "20240103": _parameter_csv(
            "20240103",
            [_parameter_row("IF2402", "2402", "20240103", "20240119")],
        ),
    }
    for snapshot_date, payload in payloads.items():
        record = download_cffex_trade_parameters(
            tmp_path,
            snapshot_date,
            dns_server="192.168.3.1",
            fetcher=lambda url, body=payload, **kwargs: _response(url, body),
        )
        write_cffex_trade_parameter_download_manifest(manifest, [record])
    return panel, master, manifest


def _revision_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    december = tmp_path / "202312.zip"
    january = tmp_path / "202401.zip"
    with zipfile.ZipFile(december, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("20231227_1.csv", _daily_csv("HO2402-C-2000"))
        archive.writestr(
            "20231229_1.csv", _daily_csv("HO2402-C-2000", "IF2312")
        )
    with zipfile.ZipFile(january, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("20240103_1.csv", _daily_csv("IF2402"))
    panel = build_cffex_contract_panel(
        [december, january], tmp_path / "revision_daily.parquet"
    )
    master = build_cffex_contract_master(
        panel, tmp_path / "revision_master.parquet"
    )
    manifest = tmp_path / "revision_sources.csv"
    payloads = {
        "20231227": _parameter_csv(
            "20231227",
            [
                _parameter_row(
                    "HO2402-C-2000", "2402", "20231227", "20240216"
                )
            ],
        ),
        "20231229": _parameter_csv(
            "20231229",
            [
                _parameter_row(
                    "HO2402-C-2000", "2402", "20231227", "20240219"
                ),
                _parameter_row("IF2312", "2312", "20231229", "20231229"),
            ],
        ),
        "20240103": _parameter_csv(
            "20240103",
            [
                _parameter_row(
                    "HO2402-C-2000", "2402", "20231227", "20240220"
                ),
                _parameter_row("IF2402", "2402", "20240103", "20240119"),
            ],
        ),
    }
    for snapshot_date, payload in payloads.items():
        record = download_cffex_trade_parameters(
            tmp_path,
            snapshot_date,
            dns_server="192.168.3.1",
            fetcher=lambda url, body=payload, **kwargs: _response(url, body),
        )
        write_cffex_trade_parameter_download_manifest(manifest, [record])
    return panel, master, manifest


def test_required_date_derivation_deduplicates_and_adds_panel_end() -> None:
    master = pd.DataFrame(
        {
            "contract": ["IF2401", "IF2402", "IF2403"],
            "first_observation_date": ["20240102", "20240102", "20240103"],
        }
    )
    calendar = ["20240102", "20240103", "20240104"]

    assert derive_required_snapshot_dates_from_frames(
        master, calendar, "20240104"
    ) == ["20240102", "20240103", "20240104"]
    assert derive_required_snapshot_dates_from_frames(
        master, calendar, "20240104", max_date="20240103"
    ) == ["20240102", "20240103"]
    with pytest.raises(CffexTradeParameterError, match="canonical requirement"):
        derive_required_snapshot_dates_from_frames(
            master,
            calendar,
            "20240104",
            scoped_dates=["20240104", "20240105"],
        )


def test_gb18030_parser_preserves_raw_fields_and_normalizes_exact_contracts() -> None:
    payload = _parameter_csv(
        "20260630",
        [
            _parameter_row("IF2607", "2607", "20260623", "20260717"),
            _parameter_row(
                "HO2607-C-2500",
                "2607",
                "20260609",
                "20260717",
                position_limit="同月份限仓1200",
                upper_percentage="--",
                lower_percentage="--",
            ),
        ],
    )

    frame = parse_cffex_trade_parameters_csv(
        payload,
        "20260630",
        source_file="20260630_1.csv",
    )

    assert frame["contract"].tolist() == ["IF2607", "HO2607-C-2500"]
    assert frame["product"].tolist() == ["IF", "HO"]
    assert frame["contract_month"].tolist() == ["2607", "2607"]
    assert frame["position_limit"].tolist() == [5000, 1200]
    assert pd.isna(frame.iloc[1]["upper_limit_percentage"])
    assert frame.iloc[1]["position_limit_raw"] == "同月份限仓1200"
    assert frame.iloc[0]["raw_basis_price"] == "3500.2"
    assert "涨停板价位" in json.loads(frame.iloc[0]["raw_record_json"])
    assert frame["source_sha256"].str.fullmatch(r"[0-9a-f]{64}").all()


def test_parser_filters_only_known_non_target_treasury_contracts() -> None:
    payload = _parameter_csv(
        "20260630",
        [
            _parameter_row("IF2607", "2607", "20260623", "20260717"),
            _parameter_row("T2609", "2609", "20260316", "20260911"),
            _parameter_row("TF2609", "2609", "20260316", "20260911"),
            _parameter_row("TS2609", "2609", "20260316", "20260911"),
            _parameter_row("TL2609", "2609", "20260316", "20260911"),
        ],
    )

    frame = parse_cffex_trade_parameters_csv(payload, "20260630")

    assert frame["contract"].tolist() == ["IF2607"]

    malformed = _parameter_csv(
        "20260630",
        [
            _parameter_row("IF2607", "2607", "20260623", "20260717"),
            _parameter_row("ZZ2609", "2609", "20260316", "20260911"),
        ],
    )
    with pytest.raises(CffexTradeParameterError, match="invalid exact contract codes"):
        parse_cffex_trade_parameters_csv(malformed, "20260630")


def test_title_date_mismatch_is_rejected() -> None:
    payload = _parameter_csv(
        "20240102",
        [_parameter_row("IF2401", "2401", "20240102", "20240119")],
        title_date="20240103",
    )
    with pytest.raises(CffexTradeParameterError, match="title date mismatch"):
        parse_cffex_trade_parameters_csv(payload, "20240102")


def test_source_path_date_mismatch_is_rejected() -> None:
    payload = _parameter_csv(
        "20240102",
        [_parameter_row("IF2401", "2401", "20240102", "20240119")],
    )
    with pytest.raises(CffexTradeParameterError, match="source path date mismatch"):
        parse_cffex_trade_parameters_csv(
            payload, "20240102", source_file="20240103_1.csv"
        )


def test_invalid_html_cache_is_quarantined_and_replaced(tmp_path: Path) -> None:
    snapshot_date = "20240102"
    cache = cffex_trade_parameter_path(tmp_path, snapshot_date)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"<html><body>temporary error</body></html>")
    payload = _parameter_csv(
        snapshot_date,
        [_parameter_row("IF2401", "2401", snapshot_date, "20240119")],
    )

    record = download_cffex_trade_parameters(
        tmp_path,
        snapshot_date,
        dns_server="192.168.3.1",
        fetcher=lambda url, **kwargs: _response(url, payload),
    )

    assert record.status == "downloaded"
    assert cache.read_bytes() == payload
    assert len(list(cache.parent.glob(f"{cache.name}.invalid.*"))) == 1


def test_expiry_revision_is_legal_but_true_static_changes_are_rejected() -> None:
    first = parse_cffex_trade_parameters_csv(
        _parameter_csv(
            "20240102",
            [_parameter_row("IF2401", "2401", "20240102", "20240119")],
        ),
        "20240102",
    )
    second = parse_cffex_trade_parameters_csv(
        _parameter_csv(
            "20240103",
            [_parameter_row("IF2401", "2401", "20240102", "20240120")],
        ),
        "20240103",
    )

    revised = pd.concat([first, second], ignore_index=True)
    validate_cffex_trade_parameter_rows(revised)
    assert revised["official_last_trade_date"].tolist() == [
        "20240119",
        "20240120",
    ]

    changed_basis = second.copy()
    changed_basis["basis_price"] = 3500.3
    with pytest.raises(CffexTradeParameterError, match="static contract metadata"):
        validate_cffex_trade_parameter_rows(
            pd.concat([first, changed_basis], ignore_index=True)
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1200", 1200),
        ("同月份限仓1200", 1200),
        ("同月份限仓 1,200 手", 1200),
        ("--", None),
        ("1200或600", None),
    ],
)
def test_position_limit_formats(raw: str, expected: int | None) -> None:
    assert parse_position_limit(raw) == expected


def test_download_uses_only_physical_fetcher_arguments(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    payload = _parameter_csv(
        "20240102",
        [_parameter_row("IF2401", "2401", "20240102", "20240119")],
    )

    def fake_fetcher(url: str, **kwargs: object) -> DirectHttpResponse:
        calls.append((url, kwargs))
        return _response(url, payload)

    download_cffex_trade_parameters(
        tmp_path,
        "20240102",
        interface="en0",
        dns_server="192.168.3.1",
        timeout_seconds=17.5,
        max_bytes=123456,
        fetcher=fake_fetcher,
    )

    assert calls == [
        (
            "http://www.cffex.com.cn/sj/jycs/202401/02/20240102_1.csv",
            {
                "interface": "en0",
                "dns_server": "192.168.3.1",
                "timeout_seconds": 17.5,
                "max_bytes": 123456,
            },
        )
    ]


def test_source_and_metadata_manifests_detect_staleness(tmp_path: Path) -> None:
    panel, master, source_manifest = _canonical_inputs(tmp_path)
    output = build_cffex_trade_parameter_metadata(
        panel,
        master,
        source_manifest,
        tmp_path / "cffex_contract_trade_parameters.parquet",
    )
    validate_cffex_trade_parameter_metadata_manifest(
        output, panel, master, source_manifest
    )

    source = cffex_trade_parameter_path(tmp_path, "20240102")
    source.write_bytes(source.read_bytes().replace(b"3500.2", b"3500.3", 1))
    with pytest.raises(CffexTradeParameterError, match="source is stale"):
        validate_cffex_trade_parameter_metadata_manifest(
            output, panel, master, source_manifest
        )
    with pytest.raises(CffexTradeParameterError, match="source hash is stale"):
        validate_cffex_trade_parameter_download_manifest(
            source_manifest,
            required_snapshot_dates=["20240102", "20240103"],
        )


def test_metadata_manifest_detects_source_manifest_staleness(tmp_path: Path) -> None:
    panel, master, source_manifest = _canonical_inputs(tmp_path)
    output = build_cffex_trade_parameter_metadata(
        panel,
        master,
        source_manifest,
        tmp_path / "cffex_contract_trade_parameters.parquet",
    )
    manifest = pd.read_csv(source_manifest)
    manifest.loc[0, "status"] = "changed-after-build"
    manifest.to_csv(source_manifest, index=False)

    with pytest.raises(CffexTradeParameterError, match="source manifest is stale"):
        validate_cffex_trade_parameter_metadata_manifest(
            output, panel, master, source_manifest
        )


def test_partial_build_cannot_overwrite_canonical(tmp_path: Path) -> None:
    panel, master, source_manifest = _canonical_inputs(tmp_path)
    canonical = tmp_path / "canonical.parquet"
    canonical.write_bytes(b"existing canonical data")

    with pytest.raises(CffexTradeParameterError, match="partial.*canonical"):
        build_cffex_trade_parameter_metadata(
            panel,
            master,
            source_manifest,
            canonical,
            required_snapshot_dates=["20240102"],
            canonical_output_path=canonical,
        )
    assert canonical.read_bytes() == b"existing canonical data"
    scoped = scoped_artifact_path(canonical, ["20240102"])
    assert scoped != canonical
    assert "scoped_20240102_20240102_1d" in scoped.name


def test_canonical_build_reconciles_complete_master_and_censored_expiry(
    tmp_path: Path,
) -> None:
    panel, master, source_manifest = _canonical_inputs(tmp_path)
    required = derive_required_snapshot_dates(panel, master)
    output = build_cffex_trade_parameter_metadata(
        panel,
        master,
        source_manifest,
        tmp_path / "cffex_contract_trade_parameters.parquet",
        required_snapshot_dates=required,
    )
    metadata = pd.read_parquet(output)
    manifest = json.loads(
        cffex_trade_parameter_metadata_manifest_path(output).read_text(
            encoding="utf-8"
        )
    )
    reconciliation = reconcile_cffex_last_trade_dates(
        master, metadata, panel_last_date="20240103"
    )

    assert len(metadata) == 2
    assert set(metadata["contract"]) == {"IF2401", "IF2402"}
    assert manifest["complete_master_coverage"] is True
    assert manifest["source_panel_sha256"]
    assert manifest["source_master_sha256"]
    assert len(manifest["sources"]) == 2
    assert reconciliation.is_complete
    assert reconciliation.summary["exact_matches"] == 1
    assert reconciliation.summary["right_censored_corrections"] == 1
    correction = reconciliation.right_censored_corrections.iloc[0]
    assert correction["contract"] == "IF2402"
    assert correction["derived_last_trade_date"] == "20240103"
    assert correction["official_last_trade_date"] == "20240119"


def test_revision_history_asof_selection_and_tamper_detection(tmp_path: Path) -> None:
    panel, master, source_manifest = _revision_inputs(tmp_path)
    metadata_path = tmp_path / "revision_metadata.parquet"
    history_path = tmp_path / "revision_history.parquet"
    output = build_cffex_trade_parameter_metadata(
        panel,
        master,
        source_manifest,
        metadata_path,
        history_path=history_path,
    )
    manifest = validate_cffex_trade_parameter_metadata_manifest(
        output, panel, master, source_manifest
    )
    metadata = pd.read_parquet(output)
    history = pd.read_parquet(history_path)
    option = query_exact_contract(metadata, "HO2402-C-2000")

    assert manifest["history_path"] == str(history_path)
    assert int(manifest["history_rows"]) == len(history)
    assert manifest["expiry_revised_contracts"] == 1
    assert manifest["expiry_revision_events"] == 1
    assert manifest["history_expiry_revision_events"] == 2
    assert option["snapshot_date"] == "20231229"
    assert option["official_last_trade_date"] == "20240219"
    assert option["expiry_first"] == "20240216"
    assert option["expiry_latest"] == "20240219"
    assert int(option["expiry_revision_count"]) == 1
    assert bool(option["expiry_changed"])

    old = query_exact_contract_asof(history, "HO2402-C-2000", "20231228")
    revised = query_exact_contract_asof(history, "HO2402-C-2000", "20231229")
    future_history = query_exact_contract_asof(
        history, "HO2402-C-2000", "20240103"
    )
    assert old["official_last_trade_date"] == "20240216"
    assert revised["official_last_trade_date"] == "20240219"
    assert future_history["official_last_trade_date"] == "20240220"
    assert option["official_last_trade_date"] != future_history[
        "official_last_trade_date"
    ]
    with pytest.raises(CffexTradeParameterError, match="on or before"):
        query_exact_contract_asof(history, "HO2402-C-2000", "20231226")
    with pytest.raises(ValueError, match="exact supported"):
        query_exact_contract_asof(history, "HO", "20231229")
    with pytest.raises(ValueError, match="valid YYYYMMDD"):
        query_exact_contract_asof(history, "HO2402-C-2000", "20231301")

    tampered = history.copy()
    tampered.loc[
        tampered["contract"].eq("HO2402-C-2000"), "official_last_trade_date"
    ] = "20240221"
    tampered.to_parquet(history_path, index=False)
    with pytest.raises(CffexTradeParameterError, match="history hash"):
        validate_cffex_trade_parameter_metadata_manifest(
            output, panel, master, source_manifest
        )


def test_reconciliation_reports_missing_and_extra_contracts() -> None:
    master = pd.DataFrame(
        {
            "contract": ["IF2401", "IF2402"],
            "last_trade_date": ["20240102", "20240103"],
        }
    )
    official = pd.DataFrame(
        {
            "contract": ["IF2402", "IF2403"],
            "official_last_trade_date": ["20240119", "20240315"],
        }
    )

    report = reconcile_cffex_last_trade_dates(
        master, official, panel_last_date="20240103"
    )

    assert report.summary["right_censored_corrections"] == 1
    assert report.missing_contracts["contract"].tolist() == ["IF2401"]
    assert report.extra_contracts["contract"].tolist() == ["IF2403"]
    assert not report.is_complete


def test_exact_contract_query_has_no_product_fallback(tmp_path: Path) -> None:
    panel, master, source_manifest = _canonical_inputs(tmp_path)
    output = build_cffex_trade_parameter_metadata(
        panel,
        master,
        source_manifest,
        tmp_path / "cffex_contract_trade_parameters.parquet",
    )

    row = query_exact_contract(output, "IF2402")
    assert row["official_last_trade_date"] == "20240119"
    with pytest.raises(ValueError, match="exact supported"):
        query_exact_contract(output, "IF")
    with pytest.raises(CffexTradeParameterError, match="unavailable"):
        query_exact_contract(output, "IF2499")


def test_config_uses_external_paths_and_vetted_endpoint_pattern() -> None:
    config_path = (
        Path(__file__).parents[1] / "config" / "phase3_cffex_trade_parameters.yaml"
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert Path(config["data_root"]).is_absolute()
    assert config["data_root"].startswith("/Volumes/")
    assert config["source"]["url_pattern"] == CFFEX_TRADE_PARAMETERS_URL
    assert config["source"]["encoding"] == "gb18030"
    assert config["paths"]["raw_trade_parameters"] == "raw/cffex/trade_parameters"
    assert config["paths"]["contract_metadata_history"].endswith(
        "cffex_contract_trade_parameters_history.parquet"
    )
    assert cffex_trade_parameters_url("20260630").endswith(
        "/202606/30/20260630_1.csv"
    )

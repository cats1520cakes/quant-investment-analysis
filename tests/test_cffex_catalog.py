from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

import quant_proof.cffex_catalog as cffex_catalog_module
from quant_proof.cffex_catalog import (
    CffexCatalog,
    CffexCatalogError,
    RightCensoredExpiryError,
)
from quant_proof.free_sources.cffex_adapter import (
    CffexDataError,
    build_cffex_contract_master,
    build_cffex_contract_panel,
    cffex_contract_master_manifest_path,
)


SIGNAL_DATE = "20260105"
NEXT_DATE = "20260106"


def _row(
    trade_date: str,
    contract: str,
    product: str,
    *,
    option_type: str = "",
    strike: float = 4000.0,
    multiplier: float | None = None,
    open_price: float | None = 100.0,
    settle: float | None = 100.0,
    volume: float = 10.0,
    open_interest: float = 10.0,
    delta: float | None = None,
    open_executable: bool = True,
    settlement_mark_valid: bool = True,
) -> dict[str, object]:
    is_future = product in {"IF", "IH", "IC", "IM"}
    product_multipliers = {
        "IF": 300.0,
        "IH": 300.0,
        "IC": 200.0,
        "IM": 200.0,
        "IO": 100.0,
        "HO": 100.0,
        "MO": 100.0,
    }
    return {
        "trade_date": trade_date,
        "contract": contract,
        "product": product,
        "instrument_type": "future" if is_future else "option",
        "option_type": option_type,
        "strike": None if is_future else strike,
        "multiplier": product_multipliers[product] if multiplier is None else multiplier,
        "open": open_price,
        "settle": settle,
        "volume": volume,
        "open_interest": open_interest,
        "delta": delta,
        "open_executable": open_executable,
        "settlement_mark_valid": settlement_mark_valid,
    }


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = [
        _row(SIGNAL_DATE, "IF2601", "IF", settle=100.0, volume=900.0, open_interest=900.0),
        _row(SIGNAL_DATE, "IF2602", "IF", settle=102.0, volume=100.0, open_interest=500.0),
        _row(SIGNAL_DATE, "IF2603", "IF", settle=103.0, volume=200.0, open_interest=400.0),
        _row(SIGNAL_DATE, "IH2602", "IH", settle=200.0, volume=100.0, open_interest=500.0),
        _row(SIGNAL_DATE, "IH2603", "IH", settle=202.0, volume=200.0, open_interest=500.0),
        _row(SIGNAL_DATE, "IH2604", "IH", settle=204.0, volume=200.0, open_interest=500.0),
        _row(
            SIGNAL_DATE,
            "IO2601-C-3900",
            "IO",
            option_type="call",
            settle=20.0,
            volume=1000.0,
            open_interest=1000.0,
            delta=0.50,
        ),
        _row(
            SIGNAL_DATE,
            "IO2602-C-3900",
            "IO",
            option_type="call",
            strike=3900.0,
            settle=0.0,
            volume=20.0,
            open_interest=200.0,
            delta=0.49,
        ),
        _row(
            SIGNAL_DATE,
            "IO2602-C-4000",
            "IO",
            option_type="call",
            strike=4000.0,
            settle=10.0,
            volume=20.0,
            open_interest=200.0,
            delta=0.51,
        ),
        _row(NEXT_DATE, "IF2601", "IF", open_price=None, volume=25.0, open_executable=False),
        _row(NEXT_DATE, "IF2602", "IF", open_price=102.5, volume=25.0, open_interest=100.0),
        _row(
            NEXT_DATE,
            "IF2603",
            "IF",
            open_price=103.5,
            volume=0.0,
            open_interest=1000.0,
            open_executable=True,
            settlement_mark_valid=False,
        ),
        _row(
            NEXT_DATE,
            "IO2602-C-3900",
            "IO",
            option_type="call",
            delta=0.10,
            volume=100.0,
            open_interest=100.0,
        ),
        _row(
            NEXT_DATE,
            "IO2602-C-4000",
            "IO",
            option_type="call",
            delta=0.50,
            volume=100.0,
            open_interest=1000.0,
        ),
    ]
    last_trade_dates = {
        "IF2601": "20260116",
        "IF2602": "20260220",
        "IF2603": "20260320",
        "IH2602": "20260220",
        "IH2603": "20260320",
        "IH2604": "20260417",
        "IO2601-C-3900": "20260116",
        "IO2602-C-3900": "20260220",
        "IO2602-C-4000": "20260220",
    }
    master = pd.DataFrame(
        [{"contract": contract, "last_trade_date": expiry} for contract, expiry in last_trade_dates.items()]
    )
    return pd.DataFrame(rows), master


def _catalog() -> CffexCatalog:
    daily, master = _frames()
    return CffexCatalog.from_frames(daily, master)


def _manifest_csv() -> bytes:
    return "\n".join(
        [
            "合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,"
            "今收盘,今结算,前结算,Delta",
            "IF2601,4000,4010,3990,100,40000,500,10,4005,4004,3995,--",
            "IO2601-C-4000,100,110,90,50,500,200,5,105,104,99,0.51",
        ]
    ).encode("gb18030")


def test_path_constructor_loads_manifest_bound_panel_and_master(tmp_path: Path) -> None:
    archive = tmp_path / "202601.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("20260105_1.csv", _manifest_csv())
        output.writestr("20260106_1.csv", _manifest_csv())
    panel_path = build_cffex_contract_panel([archive], tmp_path / "daily.parquet")
    master_path = build_cffex_contract_master(panel_path, tmp_path / "master.parquet")

    catalog = CffexCatalog(panel_path, master_path)

    assert catalog.available_dates == (SIGNAL_DATE, NEXT_DATE)
    assert catalog.product_date_ranges == {
        "IF": (SIGNAL_DATE, NEXT_DATE),
        "IO": (SIGNAL_DATE, NEXT_DATE),
    }
    assert catalog.next_trading_date(SIGNAL_DATE) == NEXT_DATE
    assert catalog.panel_horizon == NEXT_DATE
    assert catalog.right_censored_contracts == ("IF2601", "IO2601-C-4000")
    assert catalog.panel_manifest is not None
    assert catalog.master_manifest is not None
    with pytest.raises(RightCensoredExpiryError, match="right-censored expiry.*DTE is unknown"):
        catalog.select_future("IF", SIGNAL_DATE)

    manifest_path = cffex_contract_master_manifest_path(master_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["master_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CffexDataError, match="master hash"):
        CffexCatalog(panel_path, master_path)


def test_exact_contract_last_trade_date_uses_the_requested_date_row() -> None:
    catalog = _catalog()

    assert catalog.last_trade_date("IF2602", SIGNAL_DATE) == "20260220"
    with pytest.raises(CffexCatalogError, match="missing exact CFFEX row"):
        catalog.last_trade_date("IF2602", "20260107")


def test_path_constructor_official_mode_validates_canonical_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "202601.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("20260105_1.csv", _manifest_csv())
        output.writestr("20260106_1.csv", _manifest_csv())
    panel_path = build_cffex_contract_panel([archive], tmp_path / "daily.parquet")
    master_path = build_cffex_contract_master(panel_path, tmp_path / "master.parquet")
    metadata_path = tmp_path / "trade_parameters.parquet"
    history_path = tmp_path / "trade_parameters_history.parquet"
    download_manifest_path = tmp_path / "trade_parameter_sources.csv"
    pd.DataFrame(
        [
            {
                "snapshot_date": SIGNAL_DATE,
                "contract": "IF2601",
                "official_last_trade_date": "20260116",
            },
            {
                "snapshot_date": SIGNAL_DATE,
                "contract": "IO2601-C-4000",
                "official_last_trade_date": "20260116",
            },
        ]
    ).to_parquet(history_path, index=False)
    metadata_path.with_suffix(metadata_path.suffix + ".manifest.json").write_text(
        json.dumps(
            {"source_download_manifest_path": str(download_manifest_path)}
        ),
        encoding="utf-8",
    )
    validated_manifest = {
        "canonical": True,
        "complete_master_coverage": True,
        "history_path": str(history_path),
    }
    calls: list[tuple[Path, Path, Path, Path]] = []

    def validate(
        metadata: str | Path,
        panel: str | Path,
        master: str | Path,
        download_manifest: str | Path,
    ) -> dict[str, object]:
        calls.append(
            (
                Path(metadata),
                Path(panel),
                Path(master),
                Path(download_manifest),
            )
        )
        return validated_manifest

    monkeypatch.setattr(
        cffex_catalog_module,
        "validate_cffex_trade_parameter_metadata_manifest",
        validate,
    )

    catalog = CffexCatalog(
        panel_path,
        master_path,
        trade_parameter_metadata_path=metadata_path,
    )

    assert calls == [
        (metadata_path, panel_path, master_path, download_manifest_path)
    ]
    assert catalog.expiry_mode == "official_asof_history"
    assert catalog.official_expiry_manifest == validated_manifest
    assert catalog.right_censored_contracts == ()
    selected = catalog.select_future("IF", SIGNAL_DATE)
    assert selected.last_trade_date == "20260116"
    assert selected.expiry_snapshot_date == SIGNAL_DATE


def test_future_selection_is_signal_date_only_and_enforces_dte() -> None:
    catalog = _catalog()

    selected = catalog.select_future("IF", SIGNAL_DATE, min_dte=20)

    assert selected.contract == "IF2602"
    assert selected.dte == 46
    assert selected.multiplier == 300.0
    assert selected.open_interest == 500.0
    assert catalog.next_open(selected.contract, SIGNAL_DATE).open_price == 102.5
    assert catalog.select_future("IF", SIGNAL_DATE).contract == "IF2601"


def test_future_and_option_ties_are_deterministic() -> None:
    catalog = _catalog()

    future = catalog.select_future("IH", SIGNAL_DATE, min_dte=20)
    option = catalog.select_option(
        "IO",
        SIGNAL_DATE,
        option_type="C",
        target_abs_delta=0.50,
        min_dte=20,
        max_dte=60,
    )

    assert future.contract == "IH2603"
    assert future.multiplier == 300.0
    assert option.contract == "IO2602-C-3900"
    assert option.multiplier == 100.0
    assert option.strike == 3900.0
    assert option.delta_distance == pytest.approx(0.01)


def test_option_dte_range_excludes_near_expiry_and_future_delta_changes() -> None:
    catalog = _catalog()

    selected = catalog.select_option(
        "IO",
        SIGNAL_DATE,
        option_type="call",
        target_abs_delta=0.50,
        min_dte=20,
        max_dte=60,
    )

    assert selected.contract == "IO2602-C-3900"
    assert selected.dte == 46
    with pytest.raises(CffexCatalogError, match="no valid IO call"):
        catalog.select_option(
            "IO",
            SIGNAL_DATE,
            option_type="call",
            target_abs_delta=0.50,
            min_dte=47,
            max_dte=60,
        )


def test_expiry_revisions_are_applied_asof_without_future_backfill() -> None:
    before_revision = "20260105"
    after_revision = "20260113"
    future_contract = "IF2602"
    option_contract = "IO2602-C-4000"
    daily = pd.DataFrame(
        [
            _row(before_revision, future_contract, "IF"),
            _row(after_revision, future_contract, "IF"),
            _row(
                before_revision,
                option_contract,
                "IO",
                option_type="call",
                delta=0.50,
            ),
            _row(
                after_revision,
                option_contract,
                "IO",
                option_type="call",
                delta=0.50,
            ),
        ]
    )
    master = pd.DataFrame(
        [
            {"contract": future_contract, "last_trade_date": "20260227"},
            {"contract": option_contract, "last_trade_date": "20260227"},
        ]
    )
    history = pd.DataFrame(
        [
            {
                "snapshot_date": "20260101",
                "contract": future_contract,
                "official_last_trade_date": "20260220",
            },
            {
                "snapshot_date": "20260110",
                "contract": future_contract,
                "official_last_trade_date": "20260227",
            },
            {
                "snapshot_date": "20260101",
                "contract": option_contract,
                "official_last_trade_date": "20260220",
            },
            {
                "snapshot_date": "20260110",
                "contract": option_contract,
                "official_last_trade_date": "20260227",
            },
        ]
    )
    catalog = CffexCatalog.from_frames(
        daily,
        master,
        expiry_history=history,
    )

    future_before = catalog.select_future("IF", before_revision)
    future_after = catalog.select_future("IF", after_revision)
    option_before = catalog.select_option(
        "IO",
        before_revision,
        option_type="call",
        target_abs_delta=0.50,
    )
    option_after = catalog.select_option(
        "IO",
        after_revision,
        option_type="call",
        target_abs_delta=0.50,
    )

    assert catalog.expiry_mode == "provided_asof_history"
    assert catalog.unresolved_expiry_contracts == ()
    assert (
        future_before.last_trade_date,
        future_before.expiry_snapshot_date,
        future_before.dte,
    ) == ("20260220", "20260101", 46)
    assert (
        future_after.last_trade_date,
        future_after.expiry_snapshot_date,
        future_after.dte,
    ) == ("20260227", "20260110", 45)
    assert option_before.last_trade_date == "20260220"
    assert option_before.expiry_snapshot_date == "20260101"
    assert option_after.last_trade_date == "20260227"
    assert option_after.expiry_snapshot_date == "20260110"


def test_asof_expiry_history_fails_closed_when_no_history_exists() -> None:
    daily = pd.DataFrame([_row(SIGNAL_DATE, "IF2602", "IF")])
    master = pd.DataFrame(
        [{"contract": "IF2602", "last_trade_date": "20260220"}]
    )
    empty_history = pd.DataFrame(columns=[
        "snapshot_date",
        "contract",
        "official_last_trade_date",
    ])
    catalog = CffexCatalog.from_frames(
        daily,
        master,
        expiry_history=empty_history,
    )

    assert catalog.unresolved_expiry_contracts == ("IF2602",)
    with pytest.raises(
        CffexCatalogError,
        match="exact-contract as-of expiry history.*IF2602",
    ):
        catalog.select_future("IF", SIGNAL_DATE)


def test_asof_expiry_history_does_not_see_a_future_snapshot() -> None:
    daily = pd.DataFrame([_row(SIGNAL_DATE, "IF2602", "IF")])
    master = pd.DataFrame(
        [{"contract": "IF2602", "last_trade_date": "20260227"}]
    )
    history = pd.DataFrame(
        [
            {
                "snapshot_date": NEXT_DATE,
                "contract": "IF2602",
                "official_last_trade_date": "20260220",
            }
        ]
    )
    catalog = CffexCatalog.from_frames(
        daily,
        master,
        expiry_history=history,
    )

    with pytest.raises(
        CffexCatalogError,
        match="only snapshot_date <= 20260105 is visible",
    ):
        catalog.select_future("IF", SIGNAL_DATE)


def test_asof_expiry_history_never_substitutes_another_exact_contract() -> None:
    daily = pd.DataFrame([_row(SIGNAL_DATE, "IF2602", "IF")])
    master = pd.DataFrame(
        [{"contract": "IF2602", "last_trade_date": "20260220"}]
    )
    other_contract_history = pd.DataFrame(
        [
            {
                "snapshot_date": "20260101",
                "contract": "IF2603",
                "official_last_trade_date": "20260320",
            }
        ]
    )
    catalog = CffexCatalog.from_frames(
        daily,
        master,
        expiry_history=other_contract_history,
    )

    with pytest.raises(CffexCatalogError, match="history for IF2602"):
        catalog.select_future("IF", SIGNAL_DATE)


def test_official_history_corrects_master_right_censoring_for_roll_dte() -> None:
    panel_horizon = "20260630"
    signal_date = "20260629"
    daily = pd.DataFrame(
        [_row(signal_date, "IF2607", "IF", volume=100.0, open_interest=500.0)]
    )
    master = pd.DataFrame(
        [{"contract": "IF2607", "last_trade_date": panel_horizon}]
    )
    history = pd.DataFrame(
        [
            {
                "snapshot_date": "20260601",
                "contract": "IF2607",
                "official_last_trade_date": "20260717",
            }
        ]
    )
    catalog = CffexCatalog.from_frames(
        daily,
        master,
        panel_horizon=panel_horizon,
        expiry_history=history,
    )

    selected = catalog.select_future("IF", signal_date, min_dte=1)
    curve = catalog.curve_snapshot("IF", signal_date, min_dte=0)

    assert catalog.right_censored_contracts == ()
    assert selected.last_trade_date == "20260717"
    assert selected.dte == 18
    assert selected.expiry_snapshot_date == "20260601"
    assert [(point.contract, point.dte) for point in curve] == [("IF2607", 18)]


def test_expiry_at_explicit_panel_horizon_is_unknown_for_all_dte_paths() -> None:
    panel_horizon = "20260630"
    daily = pd.DataFrame(
        [
            _row("20260629", "IF2607", "IF", volume=100.0, open_interest=500.0),
            _row(
                "20260629",
                "IO2607-C-4000",
                "IO",
                option_type="call",
                volume=100.0,
                open_interest=500.0,
                delta=0.50,
            ),
        ]
    )
    master = pd.DataFrame(
        [
            {"contract": "IF2607", "last_trade_date": panel_horizon},
            {"contract": "IO2607-C-4000", "last_trade_date": panel_horizon},
        ]
    )
    catalog = CffexCatalog.from_frames(daily, master, panel_horizon=panel_horizon)

    assert catalog.panel_horizon == panel_horizon
    assert catalog.right_censored_contracts == ("IF2607", "IO2607-C-4000")
    with pytest.raises(RightCensoredExpiryError, match="panel horizon 20260630.*DTE is unknown"):
        catalog.select_future("IF", "20260629", min_dte=1)
    with pytest.raises(RightCensoredExpiryError, match="panel horizon 20260630.*DTE is unknown"):
        catalog.select_option(
            "IO",
            "20260629",
            option_type="call",
            target_abs_delta=0.50,
            min_dte=0,
            max_dte=30,
        )
    with pytest.raises(RightCensoredExpiryError, match="right-censored expiry"):
        catalog.curve_snapshot("IF", "20260629")


def test_censoring_error_requires_an_otherwise_eligible_contract() -> None:
    panel_horizon = "20260630"
    daily = pd.DataFrame(
        [_row("20260629", "IF2607", "IF", volume=0.0, open_interest=500.0)]
    )
    master = pd.DataFrame(
        [{"contract": "IF2607", "last_trade_date": panel_horizon}]
    )
    catalog = CffexCatalog.from_frames(daily, master, panel_horizon=panel_horizon)

    with pytest.raises(CffexCatalogError, match="no valid IF future") as raised:
        catalog.select_future("IF", "20260629")

    assert not isinstance(raised.value, RightCensoredExpiryError)


def test_future_selection_rejects_zero_volume_but_allows_zero_oi() -> None:
    daily = pd.DataFrame(
        [
            _row(SIGNAL_DATE, "IC2602", "IC", volume=0.0, open_interest=10_000.0),
            _row(SIGNAL_DATE, "IC2603", "IC", volume=5.0, open_interest=0.0),
        ]
    )
    master = pd.DataFrame(
        [
            {"contract": "IC2602", "last_trade_date": "20260220"},
            {"contract": "IC2603", "last_trade_date": "20260320"},
        ]
    )

    selected = CffexCatalog.from_frames(daily, master).select_future("IC", SIGNAL_DATE)

    assert selected.contract == "IC2603"
    assert selected.open_interest == 0.0
    assert selected.volume == 5.0


def test_option_selection_rejects_zero_volume_but_allows_zero_oi() -> None:
    daily = pd.DataFrame(
        [
            _row(
                SIGNAL_DATE,
                "MO2602-C-6000",
                "MO",
                option_type="call",
                volume=0.0,
                open_interest=10_000.0,
                delta=0.50,
            ),
            _row(
                SIGNAL_DATE,
                "MO2603-C-6000",
                "MO",
                option_type="call",
                strike=6100.0,
                volume=5.0,
                open_interest=0.0,
                delta=0.55,
            ),
        ]
    )
    master = pd.DataFrame(
        [
            {"contract": "MO2602-C-6000", "last_trade_date": "20260220"},
            {"contract": "MO2603-C-6000", "last_trade_date": "20260320"},
        ]
    )

    selected = CffexCatalog.from_frames(daily, master).select_option(
        "MO",
        SIGNAL_DATE,
        option_type="call",
        target_abs_delta=0.50,
    )

    assert selected.contract == "MO2603-C-6000"
    assert selected.multiplier == 100.0
    assert selected.strike == 6100.0
    assert selected.open_interest == 0.0
    assert selected.volume == 5.0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("multiplier", 0.0, "multipliers must be finite and positive"),
        ("multiplier", float("nan"), "multipliers must be finite and positive"),
        ("strike", -1.0, "strikes must be finite and non-negative"),
        ("strike", float("nan"), "strikes must be finite and non-negative"),
    ],
)
def test_catalog_rejects_invalid_official_contract_economics(
    field: str,
    value: float,
    message: str,
) -> None:
    row = _row(
        SIGNAL_DATE,
        "IO2602-C-4000",
        "IO",
        option_type="call",
        strike=4000.0,
    )
    row[field] = value
    daily = pd.DataFrame([row])
    master = pd.DataFrame(
        [{"contract": "IO2602-C-4000", "last_trade_date": "20260220"}]
    )

    with pytest.raises(CffexCatalogError, match=message):
        CffexCatalog.from_frames(daily, master)


@pytest.mark.parametrize("contract", ["IF2601", "IF2603"])
def test_next_open_rejects_missing_or_zero_volume_open(contract: str) -> None:
    catalog = _catalog()

    with pytest.raises(CffexCatalogError, match="not executable"):
        catalog.next_open(contract, SIGNAL_DATE)


def test_settlement_requires_official_valid_mark_without_price_fallback() -> None:
    catalog = _catalog()

    assert catalog.settlement("IF2602", SIGNAL_DATE) == 102.0
    assert catalog.settlement("IO2602-C-3900", SIGNAL_DATE) == 0.0
    with pytest.raises(CffexCatalogError, match="official settlement is invalid"):
        catalog.settlement("IF2603", NEXT_DATE)
    with pytest.raises(CffexCatalogError, match="missing exact CFFEX row"):
        catalog.settlement("IH2603", NEXT_DATE)


def test_curve_snapshot_and_front_next_carry_use_signal_settlements() -> None:
    catalog = _catalog()

    curve = catalog.curve_snapshot("IF", SIGNAL_DATE, min_dte=20)
    carry = catalog.front_next_carry("IF", SIGNAL_DATE, min_dte=20)

    assert [(point.contract, point.settle) for point in curve] == [("IF2602", 102.0), ("IF2603", 103.0)]
    assert carry.front.contract == "IF2602"
    assert carry.next.contract == "IF2603"
    assert carry.tenor_days == 28
    assert carry.relative_spread == pytest.approx(103.0 / 102.0 - 1.0)
    assert carry.annualized_carry == pytest.approx((102.0 / 103.0 - 1.0) * 365.0 / 28.0)

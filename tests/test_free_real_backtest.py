from __future__ import annotations

import pandas as pd

from quant_proof.free_real_backtest import (
    FreeRealBacktestConfig,
    aggregate_free_real_windows,
    evaluate_free_real_strategy,
    load_backtest_config,
    simulate_free_real_window,
)
from quant_proof.real_strategies import RealStockStrategySpec


def synthetic_panel() -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-01", "2020-04-30")
    rows = []
    for symbol, source, base, slope in [
        ("600001.SH", "sh.600001", 10.0, 0.03),
        ("600002.SH", "sh.600002", 9.0, 0.01),
    ]:
        for i, dt in enumerate(dates):
            close = base + i * slope
            rows.append(
                {
                    "trade_date": dt.strftime("%Y%m%d"),
                    "ts_code": symbol,
                    "source_code": source,
                    "open": close * 0.995,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "pre_close": close / 1.001,
                    "volume": 1_000_000,
                    "amount": 30_000_000,
                    "turnover_rate": 2.0,
                    "pct_chg": 0.1,
                    "pe_ttm": 10.0,
                    "pb": 1.0,
                    "ps_ttm": 2.0,
                    "pcf_ttm": 3.0,
                    "adj_close_for_signal": close,
                    "trade_status": 1,
                    "is_suspended": False,
                    "is_st": False,
                    "list_date": "20190101",
                    "delist_date": "",
                    "list_status": "1",
                    "listing_days": 300 + i,
                    "board": "main",
                    "limit_pct": 0.10,
                    "up_limit": close * 1.10,
                    "down_limit": close * 0.90,
                    "circ_mv_approx": 1_500_000_000,
                    "data_tier": "free_real",
                    "up_limit_source": "derived",
                    "down_limit_source": "derived",
                    "market_cap_source": "derived_from_amount_turnover",
                }
            )
    return pd.DataFrame(rows)


def test_load_backtest_config_defaults_and_overrides() -> None:
    cfg = load_backtest_config(
        {
            "target_backtest": {
                "monthly_deposit": 1000,
                "target_month_12": 12_000,
                "target_month_24": 24_000,
                "execution": {"lot_size": 100, "max_order_notional": 20_000},
            }
        }
    )

    assert cfg.monthly_deposit == 1000
    assert cfg.target_month_12 == 12_000
    assert cfg.target_month_24 == 24_000
    assert cfg.lot_size == 100
    assert cfg.max_order_notional == 20_000


def test_free_real_target_backtest_generates_goal_windows() -> None:
    spec = RealStockStrategySpec(
        name="S2_real_stock_momentum_k1_daily",
        family="S2_real_stock_momentum",
        params={
            "kind": "real_stock_momentum",
            "holding_k": 1,
            "rebalance": "daily",
            "min_listing_days": 0,
            "min_price": 0.0,
            "min_avg_amount_20d": 1.0,
        },
    )
    cfg = FreeRealBacktestConfig(
        monthly_deposit=10_000,
        target_month_12=120_000,
        target_month_24=240_000,
        window_months=1,
        min_trading_days=10,
        max_order_notional=50_000,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    windows = evaluate_free_real_strategy(synthetic_panel(), spec, cfg, deposit_timings=("beginning",), max_windows=2)

    assert not windows.empty
    assert set(["w12", "w24", "max_drawdown", "filled_orders", "rejected_orders"]).issubset(windows.columns)
    assert windows["filled_orders"].sum() > 0


def test_free_real_window_aggregation_uses_hard_targets() -> None:
    windows = pd.DataFrame(
        [
            {
                "strategy": "s",
                "family": "S2",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "w12": 500_000.0,
                "w24": 1_200_000.0,
                "total_deposit": 720_000.0,
                "max_drawdown": 0.1,
                "avg_turnover": 0.01,
                "filled_orders": 10,
                "rejected_orders": 1,
                "rejected_limit_up": 0,
                "rejected_limit_down": 0,
                "rejected_suspended": 1,
                "fees": 100,
            },
            {
                "strategy": "s",
                "family": "S2",
                "data_tier": "free_real",
                "deposit_timing": "beginning",
                "w12": 499_999.0,
                "w24": 1_300_000.0,
                "total_deposit": 720_000.0,
                "max_drawdown": 0.4,
                "avg_turnover": 0.02,
                "filled_orders": 12,
                "rejected_orders": 2,
                "rejected_limit_up": 1,
                "rejected_limit_down": 0,
                "rejected_suspended": 0,
                "fees": 120,
            },
        ]
    )

    leaderboard = aggregate_free_real_windows(windows, FreeRealBacktestConfig())
    row = leaderboard.iloc[0]

    assert row["p_success"] == 0.5
    assert row["p_w12"] == 0.5
    assert row["p_w24"] == 1.0
    assert row["avg_rejected_orders"] == 1.5


def test_target_window_counts_execution_constraints() -> None:
    def row(
        symbol: str,
        open_price: float,
        *,
        close_price: float | None = None,
        suspended: bool = False,
        up_limit: float | None = None,
        down_limit: float | None = None,
    ) -> dict[str, object]:
        close = open_price if close_price is None else close_price
        return {
            "ts_code": symbol,
            "open": open_price,
            "close": close,
            "is_suspended": suspended,
            "up_limit": open_price * 1.1 if up_limit is None else up_limit,
            "down_limit": open_price * 0.9 if down_limit is None else down_limit,
        }

    panel_by_date = {
        "20200101": {
            "600001.SH": row("600001.SH", 50.0),
            "600002.SH": row("600002.SH", 10.0),
            "600003.SH": row("600003.SH", 20.0),
        },
        "20200102": {
            "600001.SH": row("600001.SH", 50.0),
            "600002.SH": row("600002.SH", 10.0),
            "600003.SH": row("600003.SH", 20.0),
        },
        "20200103": {
            "600001.SH": row("600001.SH", 45.0, down_limit=45.0),
            "600002.SH": row("600002.SH", 10.0, suspended=True),
            "600003.SH": row("600003.SH", 20.0),
        },
        "20200106": {
            "600001.SH": row("600001.SH", 46.0),
            "600002.SH": row("600002.SH", 10.0),
            "600003.SH": row("600003.SH", 20.0, up_limit=20.0),
        },
    }
    cfg = FreeRealBacktestConfig(
        monthly_deposit=20_000,
        window_months=1,
        min_trading_days=2,
        lot_size=100,
        max_order_notional=6_000,
        commission_bps=0.0,
        min_commission=0.0,
        transfer_fee_bps=0.0,
        stamp_tax_sell_bps=0.0,
        slippage_bps=0.0,
    )

    _, metrics = simulate_free_real_window(
        panel_by_date=panel_by_date,
        trading_dates=list(panel_by_date),
        top_by_signal_date={
            "20200101": ["600001.SH"],
            "20200102": ["600002.SH"],
            "20200103": ["600003.SH"],
        },
        rebalance_dates=set(panel_by_date),
        start=pd.Timestamp("2020-01-01"),
        end=pd.Timestamp("2020-01-06"),
        deposit_timing="beginning",
        cfg=cfg,
    )

    assert metrics["filled_orders"] == 2.0
    assert metrics["clipped_orders"] == 1.0
    assert metrics["rejected_limit_down"] == 1.0
    assert metrics["rejected_suspended"] == 1.0
    assert metrics["rejected_limit_up"] == 1.0

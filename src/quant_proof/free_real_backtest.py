from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import floor
from typing import Iterable

import numpy as np
import pandas as pd

from .engine.account import Account
from .engine.cost import CostModel
from .engine.exchange_rules import ExchangeRules, MarketSnapshot
from .engine.execution import ExecutionEngine, ExecutionReport
from .engine.orders import Order, OrderSide, RejectReason
from .engine.risk import RiskLimits
from .real_strategies import RealStockStrategySpec, compute_real_stock_scores, strategy_rebalance_dates
from .simulator import first_trading_day_mask, last_trading_day_mask, rolling_windows, summarize_equity


@dataclass(frozen=True)
class FreeRealBacktestConfig:
    monthly_deposit: float = 30_000.0
    target_month_12: float = 500_000.0
    target_month_24: float = 1_200_000.0
    window_months: int = 24
    min_trading_days: int = 420
    lot_size: int = 100
    max_order_notional: float | None = 100_000.0
    commission_bps: float = 2.0
    min_commission: float = 5.0
    transfer_fee_bps: float = 0.1
    stamp_tax_sell_bps: float = 10.0
    slippage_bps: float = 5.0
    target_weight_buffer: float = 0.05
    min_symbols: int = 500
    max_daily_amount_participation: float | None = None


def load_backtest_config(raw: dict) -> FreeRealBacktestConfig:
    cfg = raw.get("target_backtest", {}) if isinstance(raw.get("target_backtest", {}), dict) else {}
    execution = cfg.get("execution", {}) if isinstance(cfg.get("execution", {}), dict) else {}
    participation = execution.get("max_daily_amount_participation")
    return FreeRealBacktestConfig(
        monthly_deposit=float(cfg.get("monthly_deposit", 30_000.0)),
        target_month_12=float(cfg.get("target_month_12", 500_000.0)),
        target_month_24=float(cfg.get("target_month_24", 1_200_000.0)),
        window_months=int(cfg.get("window_months", 24)),
        min_trading_days=int(cfg.get("min_trading_days", 420)),
        lot_size=int(execution.get("lot_size", 100)),
        max_order_notional=(
            None
            if execution.get("max_order_notional") in {None, "", 0}
            else float(execution.get("max_order_notional", 100_000.0))
        ),
        commission_bps=float(execution.get("commission_bps", 2.0)),
        min_commission=float(execution.get("min_commission", 5.0)),
        transfer_fee_bps=float(execution.get("transfer_fee_bps", 0.1)),
        stamp_tax_sell_bps=float(execution.get("stamp_tax_sell_bps", 10.0)),
        slippage_bps=float(execution.get("slippage_bps", 5.0)),
        target_weight_buffer=float(execution.get("target_weight_buffer", 0.05)),
        min_symbols=int(cfg.get("min_symbols", 500)),
        max_daily_amount_participation=(
            None if participation in {None, "", 0} else float(participation)
        ),
    )


def _date_index(panel: pd.DataFrame) -> pd.DatetimeIndex:
    dates = pd.to_datetime(pd.Series(sorted(panel["trade_date"].astype(str).unique())), format="%Y%m%d")
    return pd.DatetimeIndex(dates)


def _date_key(value: pd.Timestamp | date | str) -> str:
    if isinstance(value, str):
        return value.replace("-", "")
    return pd.Timestamp(value).strftime("%Y%m%d")


def _to_date(value: pd.Timestamp | date | str) -> date:
    return pd.Timestamp(value).date()


def _snapshot(row: dict[str, object], trade_date: date, price_column: str) -> MarketSnapshot:
    price = _execution_price(row, price_column)
    return MarketSnapshot(
        symbol=str(row["ts_code"]),
        trade_date=trade_date,
        price=price,
        suspended=bool(row.get("is_suspended", False)),
        limit_up=None if pd.isna(row.get("up_limit", np.nan)) else float(row["up_limit"]),
        limit_down=None if pd.isna(row.get("down_limit", np.nan)) else float(row["down_limit"]),
    )


DailyRows = dict[str, dict[str, object]]


def _prepare_daily_panel(panel: pd.DataFrame) -> dict[str, DailyRows]:
    required = {"trade_date", "ts_code", "open", "close", "is_suspended", "up_limit", "down_limit"}
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(f"free-real panel missing backtest columns: {missing}")
    frame = panel.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str)
    for column in ["open", "close", "up_limit", "down_limit"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "amount" in frame.columns:
        frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    frame["is_suspended"] = frame["is_suspended"].fillna(False).astype(bool)
    return {
        date_key: day.set_index("ts_code", drop=False).to_dict(orient="index")
        for date_key, day in frame.groupby("trade_date", sort=True)
    }


def _top_symbols_by_signal_date(scores: pd.DataFrame, holding_k: int) -> dict[str, list[str]]:
    ranked = scores.loc[scores["rank_score"].notna()].copy()
    if ranked.empty:
        return {}
    ranked["trade_date"] = ranked["trade_date"].astype(str)
    ranked["ts_code"] = ranked["ts_code"].astype(str)
    ranked = ranked.sort_values(["trade_date", "rank_score", "ts_code"], ascending=[True, False, True])
    return {
        str(trade_date): group["ts_code"].head(holding_k).tolist()
        for trade_date, group in ranked.groupby("trade_date", sort=True)
    }


def _current_quantities(account: Account) -> dict[str, int]:
    return {
        symbol: account.portfolio.quantity(symbol)
        for symbol in list(account.portfolio.lots)
        if account.portfolio.quantity(symbol) > 0
    }


def _available_quantity(account: Account, symbol: str, trade_date: date) -> int:
    return account.portfolio.available_quantity(symbol, trade_date)


def _round_lot(quantity: float, lot_size: int) -> int:
    if lot_size <= 1:
        return max(int(floor(quantity)), 0)
    return max(int(floor(quantity / lot_size) * lot_size), 0)


def _market_prices(day: DailyRows, price_column: str) -> dict[str, float]:
    prices: dict[str, float] = {}
    for symbol, row in day.items():
        price = row.get(price_column, np.nan)
        if pd.isna(price) or float(price) <= 0.0:
            price = row.get("close", np.nan)
        if pd.notna(price) and float(price) > 0.0:
            prices[str(symbol)] = float(price)
    return prices


def _execution_price(row: dict[str, object], price_column: str = "open") -> float:
    price = float(row.get(price_column, np.nan))
    if not np.isfinite(price) or price <= 0.0:
        price = float(row.get("close", np.nan))
    return price


def _submit_order(
    engine: ExecutionEngine,
    account: Account,
    symbol: str,
    side: OrderSide,
    quantity: int,
    snapshot: MarketSnapshot,
) -> ExecutionReport:
    order = Order(symbol=symbol, side=side, quantity=int(quantity), submitted_at=snapshot.trade_date)
    return engine.execute(account=account, order=order, snapshot=snapshot)


def _amount_cap_quantity(
    row: dict[str, object],
    quantity: int,
    price: float,
    cfg: FreeRealBacktestConfig,
) -> tuple[int, float, bool, bool]:
    if cfg.max_daily_amount_participation is None:
        return quantity, 0.0, False, False
    if cfg.max_daily_amount_participation <= 0.0 or quantity <= 0 or not np.isfinite(price) or price <= 0.0:
        notional_price = price if np.isfinite(price) and price > 0.0 else 0.0
        return 0, float(max(quantity, 0) * notional_price), False, True

    amount = row.get("amount", np.nan)
    if pd.isna(amount) or float(amount) <= 0.0:
        return 0, float(quantity * price), False, True

    max_notional = float(amount) * cfg.max_daily_amount_participation
    max_quantity = _round_lot(max_notional / price, cfg.lot_size)
    if max_quantity <= 0:
        return 0, float(quantity * price), False, True
    if quantity <= max_quantity:
        return quantity, 0.0, False, False
    clipped_quantity = min(quantity, max_quantity)
    return clipped_quantity, float((quantity - clipped_quantity) * price), True, False


def _count_report(report: ExecutionReport, counters: dict[str, float]) -> None:
    if report.filled:
        if report.filled_quantity < report.requested_quantity:
            counters["clipped_orders"] = counters.get("clipped_orders", 0.0) + 1.0
            counters["clipped_notional"] = counters.get("clipped_notional", 0.0) + float(
                (report.requested_quantity - report.filled_quantity) * report.price
            )
        counters["filled_orders"] += 1
        counters["traded_notional"] += float(report.gross_notional)
        counters["fees"] += float(report.fees)
        counters["stamp_tax"] += float(report.stamp_tax)
        return
    counters["rejected_orders"] += 1
    reason = report.reason.value if report.reason else "unknown"
    counters[f"reject_{reason}"] = counters.get(f"reject_{reason}", 0.0) + 1.0


def _execute_rebalance(
    engine: ExecutionEngine,
    account: Account,
    day: DailyRows,
    trade_date: date,
    desired_symbols: list[str],
    cfg: FreeRealBacktestConfig,
    valuation_prices: dict[str, float] | None = None,
) -> dict[str, float]:
    counters: dict[str, float] = {
        "filled_orders": 0.0,
        "rejected_orders": 0.0,
        "traded_notional": 0.0,
        "fees": 0.0,
        "stamp_tax": 0.0,
        "clipped_orders": 0.0,
        "clipped_notional": 0.0,
        "participation_clipped_orders": 0.0,
        "participation_clipped_notional": 0.0,
        "participation_blocked_orders": 0.0,
        "participation_blocked_notional": 0.0,
        "blocked_missing_rows": 0.0,
    }
    open_prices = dict(valuation_prices or {})
    open_prices.update(_market_prices(day, "open"))
    equity_at_open = account.total_equity(open_prices)
    desired = [symbol for symbol in desired_symbols if symbol in day]
    target_value = equity_at_open / len(desired) if desired else 0.0

    current = _current_quantities(account)
    sell_plan: list[tuple[str, int]] = []
    for symbol, quantity in sorted(current.items()):
        row = day.get(symbol)
        if row is None:
            if symbol not in desired:
                counters["blocked_missing_rows"] += 1.0
            continue
        price = _execution_price(row, "open")
        if not np.isfinite(price) or price <= 0.0:
            continue
        target_qty = 0
        if symbol in desired and target_value > 0.0:
            target_qty = _round_lot(target_value / price, cfg.lot_size)
            current_value = quantity * price
            if current_value <= target_value * (1.0 + cfg.target_weight_buffer):
                continue
        available = _available_quantity(account, symbol, trade_date)
        sell_qty = _round_lot(min(quantity - target_qty, available), cfg.lot_size)
        if sell_qty > 0:
            sell_plan.append((symbol, sell_qty))

    for symbol, quantity in sell_plan:
        if symbol not in day:
            counters["rejected_orders"] += 1
            counters[f"reject_{RejectReason.SUSPENDED.value}"] = counters.get(f"reject_{RejectReason.SUSPENDED.value}", 0.0) + 1.0
            continue
        capped_quantity, clipped_notional, clipped, blocked = _amount_cap_quantity(
            day[symbol],
            quantity,
            _execution_price(day[symbol], "open"),
            cfg,
        )
        if blocked:
            counters["participation_blocked_orders"] += 1.0
            counters["participation_blocked_notional"] += clipped_notional
            continue
        if clipped:
            counters["participation_clipped_orders"] += 1.0
            counters["participation_clipped_notional"] += clipped_notional
        report = _submit_order(
            engine=engine,
            account=account,
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=capped_quantity,
            snapshot=_snapshot(day[symbol], trade_date, "open"),
        )
        _count_report(report, counters)

    if not desired:
        return counters

    open_prices = _market_prices(day, "open")
    if valuation_prices:
        open_prices = {**valuation_prices, **open_prices}
    equity_after_sells = account.total_equity(open_prices)
    target_value = equity_after_sells / len(desired)
    for symbol in desired:
        if symbol not in day:
            continue
        row = day[symbol]
        price = _execution_price(row, "open")
        if not np.isfinite(price) or price <= 0.0:
            continue
        current_qty = account.portfolio.quantity(symbol)
        current_value = current_qty * price
        if current_value >= target_value * (1.0 - cfg.target_weight_buffer):
            continue
        cash_budget = min(account.cash, max(target_value - current_value, 0.0))
        estimated_fee_rate = (cfg.commission_bps + cfg.transfer_fee_bps + cfg.slippage_bps) / 10000.0
        buy_qty = _round_lot(cash_budget / (price * (1.0 + estimated_fee_rate)), cfg.lot_size)
        if buy_qty <= 0:
            continue
        capped_quantity, clipped_notional, clipped, blocked = _amount_cap_quantity(row, buy_qty, price, cfg)
        if blocked:
            counters["participation_blocked_orders"] += 1.0
            counters["participation_blocked_notional"] += clipped_notional
            continue
        if clipped:
            counters["participation_clipped_orders"] += 1.0
            counters["participation_clipped_notional"] += clipped_notional
        report = _submit_order(
            engine=engine,
            account=account,
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=capped_quantity,
            snapshot=_snapshot(row, trade_date, "open"),
        )
        _count_report(report, counters)
    return counters


def simulate_free_real_window(
    panel_by_date: dict[str, DailyRows],
    trading_dates: list[str],
    top_by_signal_date: dict[str, list[str]],
    rebalance_dates: set[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
    deposit_timing: str,
    cfg: FreeRealBacktestConfig,
) -> tuple[pd.DataFrame, dict[str, float]]:
    window_dates = [d for d in trading_dates if _date_key(start) <= d <= _date_key(end)]
    if not window_dates:
        return pd.DataFrame(), {}
    date_index = pd.to_datetime(pd.Series(window_dates), format="%Y%m%d")
    month_start = first_trading_day_mask(pd.DatetimeIndex(date_index)).to_numpy(dtype=bool)
    month_end = last_trading_day_mask(pd.DatetimeIndex(date_index)).to_numpy(dtype=bool)
    calendar = [_to_date(d) for d in trading_dates]
    engine = ExecutionEngine(
        exchange_rules=ExchangeRules(t_plus_one=True, trading_calendar=calendar),
        cost_model=CostModel(
            commission_bps=cfg.commission_bps,
            min_commission=cfg.min_commission,
            transfer_fee_bps=cfg.transfer_fee_bps,
            stamp_tax_sell_bps=cfg.stamp_tax_sell_bps,
            slippage_bps=cfg.slippage_bps,
        ),
        risk_limits=RiskLimits(max_order_notional=cfg.max_order_notional, min_quantity=cfg.lot_size, lot_size=cfg.lot_size),
    )
    account = Account(cash=0.0)
    last_desired: list[str] = []
    records = []
    counters = {
        "filled_orders": 0.0,
        "rejected_orders": 0.0,
        "traded_notional": 0.0,
        "fees": 0.0,
        "stamp_tax": 0.0,
        "clipped_orders": 0.0,
        "clipped_notional": 0.0,
        "participation_clipped_orders": 0.0,
        "participation_clipped_notional": 0.0,
        "participation_blocked_orders": 0.0,
        "participation_blocked_notional": 0.0,
        "blocked_missing_rows": 0.0,
    }
    prev_equity = 0.0
    last_prices: dict[str, float] = {}
    for i, trade_date_key in enumerate(window_dates):
        trade_date = _to_date(trade_date_key)
        day = panel_by_date[trade_date_key]
        deposit_today = 0.0
        if deposit_timing == "beginning" and month_start[i]:
            account.cash += cfg.monthly_deposit
            deposit_today = cfg.monthly_deposit

        if trade_date_key in rebalance_dates:
            previous_signal_date = window_dates[i - 1] if i > 0 else ""
            last_desired = top_by_signal_date.get(previous_signal_date, [])
            open_valuation_prices = dict(last_prices)
            open_valuation_prices.update(_market_prices(day, "open"))
            rebalance_counts = _execute_rebalance(
                engine=engine,
                account=account,
                day=day,
                trade_date=trade_date,
                desired_symbols=last_desired,
                cfg=cfg,
                valuation_prices=open_valuation_prices,
            )
            for key, value in rebalance_counts.items():
                counters[key] = counters.get(key, 0.0) + value

        close_prices = _market_prices(day, "close")
        last_prices.update(close_prices)
        equity = account.total_equity(last_prices)
        if deposit_timing == "ending" and month_end[i]:
            account.cash += cfg.monthly_deposit
            deposit_today = cfg.monthly_deposit
            equity += cfg.monthly_deposit
        base = prev_equity + deposit_today
        daily_return = 0.0 if base <= 0.0 else equity / base - 1.0
        records.append(
            {
                "trade_date": trade_date_key,
                "wealth": float(equity),
                "daily_return": float(daily_return),
                "cash": float(account.cash),
                "positions": len(_current_quantities(account)),
                "desired_positions": len(last_desired),
            }
        )
        prev_equity = equity
    equity_frame = pd.DataFrame(records)
    equity_frame.index = pd.DatetimeIndex(pd.to_datetime(equity_frame["trade_date"], format="%Y%m%d"))
    metrics = summarize_equity(equity_frame, monthly_deposit=cfg.monthly_deposit)
    metrics.update(counters)
    metrics["avg_turnover"] = float(counters["traded_notional"] / max(float(equity_frame["wealth"].sum()), 1.0))
    metrics["rejected_limit_up"] = float(counters.get(f"reject_{RejectReason.LIMIT_UP.value}", 0.0))
    metrics["rejected_limit_down"] = float(counters.get(f"reject_{RejectReason.LIMIT_DOWN.value}", 0.0))
    metrics["rejected_suspended"] = float(counters.get(f"reject_{RejectReason.SUSPENDED.value}", 0.0))
    metrics["rejected_t_plus_one"] = float(counters.get(f"reject_{RejectReason.T_PLUS_ONE.value}", 0.0))
    metrics["rejected_cash"] = float(counters.get(f"reject_{RejectReason.INSUFFICIENT_CASH.value}", 0.0))
    metrics["clipped_orders"] = float(counters.get("clipped_orders", 0.0))
    metrics["clipped_notional"] = float(counters.get("clipped_notional", 0.0))
    metrics["participation_clipped_orders"] = float(counters.get("participation_clipped_orders", 0.0))
    metrics["participation_clipped_notional"] = float(counters.get("participation_clipped_notional", 0.0))
    metrics["participation_blocked_orders"] = float(counters.get("participation_blocked_orders", 0.0))
    metrics["participation_blocked_notional"] = float(counters.get("participation_blocked_notional", 0.0))
    metrics["blocked_missing_rows"] = float(counters.get("blocked_missing_rows", 0.0))
    return equity_frame, metrics


def evaluate_free_real_strategy(
    panel: pd.DataFrame,
    spec: RealStockStrategySpec,
    cfg: FreeRealBacktestConfig,
    deposit_timings: Iterable[str] = ("beginning", "ending"),
    max_windows: int = 0,
    panel_by_date: dict[str, DailyRows] | None = None,
) -> pd.DataFrame:
    date_index = _date_index(panel)
    windows = rolling_windows(date_index, window_months=cfg.window_months, min_trading_days=cfg.min_trading_days)
    if max_windows > 0:
        windows = windows[:max_windows]
    if not windows:
        return pd.DataFrame()
    panel_by_date = panel_by_date or _prepare_daily_panel(panel)
    trading_dates = sorted(panel_by_date)
    scores = compute_real_stock_scores(panel, spec)
    holding_k = int(spec.params.get("holding_k", 10))
    top_by_signal_date = _top_symbols_by_signal_date(scores, holding_k=holding_k)
    rebalance = str(spec.params.get("rebalance", "daily"))
    rebalance_dates = set(strategy_rebalance_dates(panel, rebalance))
    rows = []
    for deposit_timing in deposit_timings:
        for start, end in windows:
            _, metrics = simulate_free_real_window(
                panel_by_date=panel_by_date,
                trading_dates=trading_dates,
                top_by_signal_date=top_by_signal_date,
                rebalance_dates=rebalance_dates,
                start=start,
                end=end,
                deposit_timing=deposit_timing,
                cfg=cfg,
            )
            if not metrics:
                continue
            rows.append(
                {
                    "strategy": spec.name,
                    "family": spec.family,
                    "data_tier": "free_real",
                    "deposit_timing": deposit_timing,
                    "start": start.date().isoformat(),
                    "end": end.date().isoformat(),
                    "holding_k": holding_k,
                    "rebalance": rebalance,
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def aggregate_free_real_windows(windows: pd.DataFrame, cfg: FreeRealBacktestConfig) -> pd.DataFrame:
    if windows.empty:
        return pd.DataFrame()
    rows = []
    group_cols = ["strategy", "family", "data_tier", "deposit_timing"]
    def mean_col(group: pd.DataFrame, column: str) -> float:
        return float(group[column].mean()) if column in group.columns else 0.0

    for key, group in windows.groupby(group_cols, sort=True):
        success = (group["w12"] >= cfg.target_month_12) & (group["w24"] >= cfg.target_month_24)
        rows.append(
            {
                "strategy": key[0],
                "family": key[1],
                "data_tier": key[2],
                "deposit_timing": key[3],
                "n_windows": int(len(group)),
                "p_success": float(success.mean()),
                "p_w12": float((group["w12"] >= cfg.target_month_12).mean()),
                "p_w24": float((group["w24"] >= cfg.target_month_24).mean()),
                "median_w24": float(group["w24"].median()),
                "p10_w24": float(group["w24"].quantile(0.10)),
                "p90_w24": float(group["w24"].quantile(0.90)),
                "median_max_drawdown": float(group["max_drawdown"].median()),
                "p95_max_drawdown": float(group["max_drawdown"].quantile(0.95)),
                "p_w24_below_deposit": float((group["w24"] < group["total_deposit"]).mean()),
                "p_w24_below_720k": float((group["w24"] < 720_000).mean()),
                "p_drawdown_gt_35": float((group["max_drawdown"] > 0.35).mean()),
                "p_drawdown_gt_50": float((group["max_drawdown"] > 0.50).mean()),
                "avg_turnover": mean_col(group, "avg_turnover"),
                "avg_filled_orders": mean_col(group, "filled_orders"),
                "avg_rejected_orders": mean_col(group, "rejected_orders"),
                "avg_rejected_limit_up": mean_col(group, "rejected_limit_up"),
                "avg_rejected_limit_down": mean_col(group, "rejected_limit_down"),
                "avg_rejected_suspended": mean_col(group, "rejected_suspended"),
                "avg_clipped_orders": mean_col(group, "clipped_orders"),
                "avg_clipped_notional": mean_col(group, "clipped_notional"),
                "avg_participation_clipped_orders": mean_col(group, "participation_clipped_orders"),
                "avg_participation_clipped_notional": mean_col(group, "participation_clipped_notional"),
                "avg_participation_blocked_orders": mean_col(group, "participation_blocked_orders"),
                "avg_participation_blocked_notional": mean_col(group, "participation_blocked_notional"),
                "avg_blocked_missing_rows": mean_col(group, "blocked_missing_rows"),
                "avg_fees": mean_col(group, "fees"),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["score"] = (
        100.0 * out["p_success"]
        + 20.0 * np.log((out["median_w24"] / cfg.target_month_24).clip(lower=1e-9))
        - 35.0 * out["p_w24_below_720k"]
        - 25.0 * out["p_drawdown_gt_35"]
        - 15.0 * out["p_drawdown_gt_50"]
        - 10.0 * out["avg_turnover"].clip(lower=0.0, upper=1.0)
    )
    return out.sort_values(["score", "p_success", "median_w24"], ascending=False).reset_index(drop=True)

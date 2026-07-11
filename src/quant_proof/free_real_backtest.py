from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from math import comb, floor, sqrt
from typing import Callable, Iterable, Protocol

import numpy as np
import pandas as pd

from .engine.account import Account
from .engine.cost import CostModel
from .engine.exchange_rules import ExchangeRules, MarketSnapshot, ShareQuantityRules
from .engine.execution import ExecutionEngine, ExecutionReport
from .engine.orders import Order, OrderSide, RejectReason
from .engine.risk import RiskLimits
from .real_strategies import (
    RealStockStrategySpec,
    compute_real_stock_scores,
    stock_target_weights_by_signal_date,
    strategy_rebalance_dates,
    target_symbols_by_signal_date,
)
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
    delisting_terminal_value_fraction: float = 0.0
    candidate_min_nonoverlap_hit_share_lower95: float = 0.50
    candidate_min_p05_w24: float = 720_000.0
    candidate_max_p95_drawdown: float = 0.35
    candidate_joint_success_alpha: float = 0.05
    candidate_min_nonoverlap_blocks: int = 5
    candidate_min_nonoverlap_w24: float = 720_000.0
    candidate_max_nonoverlap_drawdown: float = 0.35
    candidate_max_unfilled_sell_share: float = 0.10
    candidate_max_margin_call_window_share: float = 0.0
    candidate_max_default_window_share: float = 0.0

    def __post_init__(self) -> None:
        if self.max_daily_amount_participation is not None and not (
            0.0 < self.max_daily_amount_participation <= 1.0
        ):
            raise ValueError("max_daily_amount_participation must be in (0, 1]")
        if not 0.0 <= self.delisting_terminal_value_fraction <= 1.0:
            raise ValueError("delisting_terminal_value_fraction must be between 0 and 1")
        if not 0.0 <= self.candidate_min_nonoverlap_hit_share_lower95 <= 1.0:
            raise ValueError("candidate min_nonoverlap_hit_share_lower95 must be between 0 and 1")
        if not np.isfinite(self.candidate_min_p05_w24) or self.candidate_min_p05_w24 < 0.0:
            raise ValueError("candidate min_p05_w24 must be non-negative")
        if not 0.0 <= self.candidate_max_p95_drawdown <= 1.0:
            raise ValueError("candidate max_p95_drawdown must be between 0 and 1")
        if not 0.0 < self.candidate_joint_success_alpha < 1.0:
            raise ValueError("candidate joint_success_alpha must be between 0 and 1")
        try:
            min_blocks = float(self.candidate_min_nonoverlap_blocks)
        except (TypeError, ValueError):
            min_blocks = float("nan")
        if isinstance(self.candidate_min_nonoverlap_blocks, bool) or not (
            np.isfinite(min_blocks) and min_blocks.is_integer() and min_blocks >= 1.0
        ):
            raise ValueError("candidate min_nonoverlap_blocks must be a positive integer")
        if (
            not np.isfinite(self.candidate_min_nonoverlap_w24)
            or self.candidate_min_nonoverlap_w24 < 0.0
        ):
            raise ValueError("candidate min_nonoverlap_w24 must be non-negative")
        if not 0.0 <= self.candidate_max_nonoverlap_drawdown <= 1.0:
            raise ValueError("candidate max_nonoverlap_drawdown must be between 0 and 1")
        if not 0.0 <= self.candidate_max_unfilled_sell_share <= 1.0:
            raise ValueError("candidate max_unfilled_sell_share must be between 0 and 1")
        if not 0.0 <= self.candidate_max_margin_call_window_share <= 1.0:
            raise ValueError(
                "candidate max_margin_call_window_share must be between 0 and 1"
            )
        if not 0.0 <= self.candidate_max_default_window_share <= 1.0:
            raise ValueError(
                "candidate max_default_window_share must be between 0 and 1"
            )


@dataclass(frozen=True)
class DerivativeEndOfDaySnapshot:
    nav: float
    margin_status: str
    margin_shortfall: float
    settlement_fees: float = 0.0
    futures_expiry_settlements: int = 0
    option_expiry_settlements: int = 0


class FreeRealDerivativeCoordinator(Protocol):
    """Hook contract for a derivative overlay sharing the stock account cash pool."""

    account: Account

    def execute_reductions(self, trade_date: str) -> Mapping[str, float]: ...

    def execute_increases(self, trade_date: str) -> Mapping[str, float]: ...

    def settle_end_of_day(
        self,
        trade_date: str,
        stock_prices: Mapping[str, float],
    ) -> DerivativeEndOfDaySnapshot: ...

    def latch_close_signal(
        self,
        trade_date: str,
        nav: float,
        *,
        force_flat: bool,
    ) -> Mapping[str, float]: ...


def load_backtest_config(raw: dict) -> FreeRealBacktestConfig:
    cfg = raw.get("target_backtest", {}) if isinstance(raw.get("target_backtest", {}), dict) else {}
    execution = cfg.get("execution", {}) if isinstance(cfg.get("execution", {}), dict) else {}
    panel_build = raw.get("panel_build", {}) if isinstance(raw.get("panel_build", {}), dict) else {}
    participation = execution.get("max_daily_amount_participation")
    terminal_fraction = float(panel_build.get("delisting_terminal_value_fraction", 0.0))
    if not 0.0 <= terminal_fraction <= 1.0:
        raise ValueError("delisting_terminal_value_fraction must be between 0 and 1")
    candidate_gates = cfg.get("candidate_gates", {}) if isinstance(cfg.get("candidate_gates", {}), dict) else {}
    min_nonoverlap_hit_share_lower95 = float(
        candidate_gates.get("min_nonoverlap_hit_share_lower95", 0.50)
    )
    min_p05_w24 = float(candidate_gates.get("min_p05_w24", 720_000.0))
    max_p95_drawdown = float(candidate_gates.get("max_p95_drawdown", 0.35))
    joint_success_alpha = float(candidate_gates.get("joint_success_alpha", 0.05))
    min_nonoverlap_blocks_value = candidate_gates.get("min_nonoverlap_blocks", 5)
    if isinstance(min_nonoverlap_blocks_value, bool):
        raise ValueError("candidate min_nonoverlap_blocks must be a positive integer")
    min_nonoverlap_blocks_raw = float(min_nonoverlap_blocks_value)
    if not np.isfinite(min_nonoverlap_blocks_raw) or not min_nonoverlap_blocks_raw.is_integer():
        raise ValueError("candidate min_nonoverlap_blocks must be a positive integer")
    min_nonoverlap_blocks = int(min_nonoverlap_blocks_raw)
    min_nonoverlap_w24 = float(candidate_gates.get("min_nonoverlap_w24", min_p05_w24))
    max_nonoverlap_drawdown = float(
        candidate_gates.get("max_nonoverlap_drawdown", max_p95_drawdown)
    )
    max_unfilled_sell_share = float(candidate_gates.get("max_unfilled_sell_share", 0.10))
    max_margin_call_window_share = float(
        candidate_gates.get("max_margin_call_window_share", 0.0)
    )
    max_default_window_share = float(
        candidate_gates.get("max_default_window_share", 0.0)
    )
    if not 0.0 <= min_nonoverlap_hit_share_lower95 <= 1.0:
        raise ValueError("candidate min_nonoverlap_hit_share_lower95 must be between 0 and 1")
    if min_p05_w24 < 0.0:
        raise ValueError("candidate min_p05_w24 must be non-negative")
    if not 0.0 <= max_p95_drawdown <= 1.0:
        raise ValueError("candidate max_p95_drawdown must be between 0 and 1")
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
        delisting_terminal_value_fraction=terminal_fraction,
        candidate_min_nonoverlap_hit_share_lower95=min_nonoverlap_hit_share_lower95,
        candidate_min_p05_w24=min_p05_w24,
        candidate_max_p95_drawdown=max_p95_drawdown,
        candidate_joint_success_alpha=joint_success_alpha,
        candidate_min_nonoverlap_blocks=min_nonoverlap_blocks,
        candidate_min_nonoverlap_w24=min_nonoverlap_w24,
        candidate_max_nonoverlap_drawdown=max_nonoverlap_drawdown,
        candidate_max_unfilled_sell_share=max_unfilled_sell_share,
        candidate_max_margin_call_window_share=max_margin_call_window_share,
        candidate_max_default_window_share=max_default_window_share,
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
    raw_board = row.get("board")
    board = None if raw_board is None or pd.isna(raw_board) else str(raw_board)
    return MarketSnapshot(
        symbol=str(row["ts_code"]),
        trade_date=trade_date,
        price=price,
        suspended=bool(row.get("is_suspended", False)),
        limit_up=None if pd.isna(row.get("up_limit", np.nan)) else float(row["up_limit"]),
        limit_down=None if pd.isna(row.get("down_limit", np.nan)) else float(row["down_limit"]),
        board=board,
    )


DailyRow = dict[str, object]
DailyRows = Mapping[str, DailyRow]


class _IndexedDayRows(Mapping[str, DailyRow]):
    def __init__(self, frame: pd.DataFrame):
        self._frame = frame.set_index("ts_code", drop=False)

    def __getitem__(self, symbol: str) -> DailyRow:
        try:
            row = self._frame.loc[str(symbol)]
        except KeyError:
            raise KeyError(symbol) from None
        if isinstance(row, pd.DataFrame):
            raise ValueError(f"duplicate daily execution row for {symbol}")
        return row.to_dict()

    def __iter__(self) -> Iterator[str]:
        return iter(self._frame.index.astype(str))

    def __len__(self) -> int:
        return int(len(self._frame))

    def __contains__(self, symbol: object) -> bool:
        return str(symbol) in self._frame.index


class IndexedDailyPanel(Mapping[str, DailyRows]):
    def __init__(self, frame: pd.DataFrame, cache_days: int = 4):
        if cache_days <= 0:
            raise ValueError("cache_days must be positive")
        self._frame = frame
        self._bounds: dict[str, tuple[int, int]] = {}
        self._indices: dict[str, np.ndarray] = {}
        if frame["trade_date"].is_monotonic_increasing:
            counts = frame.groupby("trade_date", sort=True, observed=True).size()
            self._dates = counts.index.astype(str).tolist()
            offsets = counts.cumsum().shift(fill_value=0).astype(int)
            self._bounds = {
                str(date_key): (int(offsets.loc[date_key]), int(offsets.loc[date_key] + counts.loc[date_key]))
                for date_key in counts.index
            }
        else:
            grouped_indices = frame.groupby("trade_date", sort=True, observed=True).indices
            self._dates = [str(date_key) for date_key in grouped_indices]
            index_dtype = np.int32 if len(frame) <= np.iinfo(np.int32).max else np.int64
            self._indices = {
                str(date_key): np.asarray(indices, dtype=index_dtype)
                for date_key, indices in grouped_indices.items()
            }
        self._cache_days = int(cache_days)
        self._cache: OrderedDict[str, _IndexedDayRows] = OrderedDict()

    def __getitem__(self, date_key: str) -> DailyRows:
        key = str(date_key)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if self._bounds:
            try:
                start, end = self._bounds[key]
            except KeyError:
                raise KeyError(date_key) from None
            day_frame = self._frame.iloc[start:end]
        else:
            try:
                indices = self._indices[key]
            except KeyError:
                raise KeyError(date_key) from None
            day_frame = self._frame.iloc[indices]
        day = _IndexedDayRows(day_frame)
        self._cache[key] = day
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_days:
            self._cache.popitem(last=False)
        return day

    def __iter__(self) -> Iterator[str]:
        return iter(self._dates)

    def __len__(self) -> int:
        return len(self._dates)


def _prepare_daily_panel(panel: pd.DataFrame) -> IndexedDailyPanel:
    required = {
        "trade_date",
        "ts_code",
        "open",
        "close",
        "pre_close",
        "corporate_action_share_factor",
        "is_suspended",
        "up_limit",
        "down_limit",
    }
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(f"free-real panel missing backtest columns: {missing}")
    optional = {"amount", "board", "delisting_exit_required"}
    columns = [column for column in panel.columns if column in required | optional]
    frame = panel.loc[:, columns].copy(deep=False)
    if not isinstance(frame["trade_date"].dtype, pd.StringDtype):
        frame["trade_date"] = frame["trade_date"].astype("string[pyarrow]")
    if not isinstance(frame["ts_code"].dtype, pd.StringDtype):
        frame["ts_code"] = frame["ts_code"].astype("string[pyarrow]")
    for column in ["open", "close", "pre_close", "corporate_action_share_factor", "up_limit", "down_limit"]:
        if not pd.api.types.is_numeric_dtype(frame[column]):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "amount" in frame.columns and not pd.api.types.is_numeric_dtype(frame["amount"]):
        frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    if not pd.api.types.is_bool_dtype(frame["is_suspended"]) or frame["is_suspended"].isna().any():
        frame["is_suspended"] = frame["is_suspended"].fillna(False).astype(bool)
    return IndexedDailyPanel(frame)


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


def _effective_quantity_rules(
    snapshot: MarketSnapshot,
    cfg: FreeRealBacktestConfig,
) -> ShareQuantityRules:
    if snapshot.quantity_rules is not None:
        return snapshot.quantity_rules
    lot_size = max(int(cfg.lot_size), 1)
    return ShareQuantityRules(
        buy_minimum=lot_size,
        quantity_step=lot_size,
        odd_lot_threshold=lot_size,
    )


def _market_prices(
    day: DailyRows,
    price_column: str,
    symbols: Iterable[str] | None = None,
) -> dict[str, float]:
    prices: dict[str, float] = {}
    wanted = list(day) if symbols is None else list(dict.fromkeys(map(str, symbols)))
    for symbol in wanted:
        row = day.get(symbol)
        if row is None:
            continue
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
    *,
    side: OrderSide,
    quantity_rules: ShareQuantityRules,
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
    raw_max_quantity = max(int(floor(max_notional / price)), 0)
    if quantity <= raw_max_quantity:
        return quantity, 0.0, False, False
    if side == OrderSide.BUY:
        max_quantity = quantity_rules.normalize_buy(raw_max_quantity)
    else:
        max_quantity = quantity_rules.normalize_regular_sell(raw_max_quantity)
    if max_quantity <= 0:
        return 0, float(quantity * price), False, True
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


def _empty_rebalance_counters() -> dict[str, float]:
    return {
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
        "requested_sell_notional": 0.0,
        "participation_unfilled_sell_notional": 0.0,
        "blocked_missing_rows": 0.0,
        "terminal_exit_positions": 0.0,
        "terminal_exit_shares": 0.0,
        "terminal_recovery": 0.0,
        "terminal_writeoff_notional": 0.0,
    }


def _merge_rebalance_counters(
    target: dict[str, float],
    source: Mapping[str, float],
) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0.0) + float(value)


def _target_weights(
    desired_symbols: Iterable[str],
    desired_weights: Mapping[str, float] | None,
    *,
    tradable_symbols: set[str],
) -> dict[str, float]:
    desired = list(dict.fromkeys(map(str, desired_symbols)))
    if desired_weights is None:
        tradable = [symbol for symbol in desired if symbol in tradable_symbols]
        if not tradable:
            return {}
        equal_weight = 1.0 / len(tradable)
        return {symbol: equal_weight for symbol in tradable}

    unknown = set(map(str, desired_weights)).difference(desired)
    if unknown:
        raise ValueError(
            "target weights contain symbols outside the desired book: "
            + ",".join(sorted(unknown))
        )
    weights: dict[str, float] = {}
    for symbol in desired:
        raw_weight = desired_weights.get(symbol, 0.0)
        if isinstance(raw_weight, bool):
            raise ValueError(f"target weight for {symbol} must be numeric")
        weight = float(raw_weight)
        if not np.isfinite(weight) or weight < 0.0:
            raise ValueError(f"target weight for {symbol} must be finite and non-negative")
        if symbol in tradable_symbols and weight > 0.0:
            weights[symbol] = weight
    total_weight = sum(float(value) for value in desired_weights.values())
    if not np.isfinite(total_weight) or total_weight > 1.0 + 1e-12:
        raise ValueError("unlevered stock target weights must sum to at most 1")
    return weights


def _execute_stock_sells(
    engine: ExecutionEngine,
    account: Account,
    day: DailyRows,
    trade_date: date,
    desired_symbols: list[str],
    desired_weights: Mapping[str, float] | None,
    cfg: FreeRealBacktestConfig,
    valuation_prices: dict[str, float] | None = None,
    liquidity_day: DailyRows | None = None,
) -> dict[str, float]:
    counters = _empty_rebalance_counters()
    current = _current_quantities(account)
    desired_set = set(map(str, desired_symbols))
    relevant_symbols = set(current) | desired_set
    open_prices = dict(valuation_prices or {})
    open_prices.update(_market_prices(day, "open", relevant_symbols))
    equity_at_open = account.total_equity(open_prices)
    weights = _target_weights(
        desired_symbols,
        desired_weights,
        tradable_symbols=set(day),
    )

    sell_plan: list[tuple[str, int, MarketSnapshot]] = []
    for symbol, quantity in sorted(current.items()):
        row = day.get(symbol)
        if row is None:
            if symbol not in desired_set:
                counters["blocked_missing_rows"] += 1.0
            continue
        price = _execution_price(row, "open")
        if not np.isfinite(price) or price <= 0.0:
            continue
        snapshot = _snapshot(row, trade_date, "open")
        quantity_rules = _effective_quantity_rules(snapshot, cfg)
        target_value = equity_at_open * weights.get(symbol, 0.0)
        target_qty = 0
        if target_value > 0.0:
            target_qty = quantity_rules.normalize_buy(int(floor(target_value / price)))
            current_value = quantity * price
            if current_value <= target_value * (1.0 + cfg.target_weight_buffer):
                continue
        available = _available_quantity(account, symbol, trade_date)
        full_liquidation = target_qty == 0 and available >= quantity
        sell_qty = (
            quantity
            if full_liquidation
            else quantity_rules.normalize_regular_sell(
                min(quantity - target_qty, available)
            )
        )
        if sell_qty > 0:
            sell_plan.append((symbol, sell_qty, snapshot))

    for symbol, quantity, snapshot in sell_plan:
        if symbol not in day:
            counters["rejected_orders"] += 1
            counters[f"reject_{RejectReason.SUSPENDED.value}"] = counters.get(f"reject_{RejectReason.SUSPENDED.value}", 0.0) + 1.0
            continue
        sell_price = _execution_price(day[symbol], "open")
        counters["requested_sell_notional"] += float(quantity * sell_price)
        capped_quantity, clipped_notional, clipped, blocked = _amount_cap_quantity(
            (liquidity_day or {}).get(symbol, {}),
            quantity,
            sell_price,
            cfg,
            side=OrderSide.SELL,
            quantity_rules=_effective_quantity_rules(snapshot, cfg),
        )
        if blocked:
            counters["participation_blocked_orders"] += 1.0
            counters["participation_blocked_notional"] += clipped_notional
            counters["participation_unfilled_sell_notional"] += clipped_notional
            continue
        if clipped:
            counters["participation_clipped_orders"] += 1.0
            counters["participation_clipped_notional"] += clipped_notional
            counters["participation_unfilled_sell_notional"] += clipped_notional
        report = _submit_order(
            engine=engine,
            account=account,
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=capped_quantity,
            snapshot=snapshot,
        )
        _count_report(report, counters)
    return counters


def _execute_stock_buys(
    engine: ExecutionEngine,
    account: Account,
    day: DailyRows,
    trade_date: date,
    desired_symbols: list[str],
    desired_weights: Mapping[str, float] | None,
    cfg: FreeRealBacktestConfig,
    valuation_prices: dict[str, float] | None = None,
    liquidity_day: DailyRows | None = None,
) -> dict[str, float]:
    counters = _empty_rebalance_counters()
    weights = _target_weights(
        desired_symbols,
        desired_weights,
        tradable_symbols=set(day),
    )
    if not weights:
        return counters

    open_prices = _market_prices(day, "open", set(_current_quantities(account)) | set(weights))
    if valuation_prices:
        open_prices = {**valuation_prices, **open_prices}
    equity_after_sells = account.total_equity(open_prices)
    for symbol, target_weight in weights.items():
        if symbol not in day:
            continue
        row = day[symbol]
        price = _execution_price(row, "open")
        if not np.isfinite(price) or price <= 0.0:
            continue
        snapshot = _snapshot(row, trade_date, "open")
        quantity_rules = _effective_quantity_rules(snapshot, cfg)
        current_qty = account.portfolio.quantity(symbol)
        current_value = current_qty * price
        target_value = equity_after_sells * target_weight
        if current_value >= target_value * (1.0 - cfg.target_weight_buffer):
            continue
        cash_budget = min(account.cash, max(target_value - current_value, 0.0))
        estimated_fee_rate = (cfg.commission_bps + cfg.transfer_fee_bps + cfg.slippage_bps) / 10000.0
        buy_qty = quantity_rules.normalize_buy(
            int(floor(cash_budget / (price * (1.0 + estimated_fee_rate))))
        )
        if buy_qty <= 0:
            continue
        capped_quantity, clipped_notional, clipped, blocked = _amount_cap_quantity(
            (liquidity_day or {}).get(symbol, {}),
            buy_qty,
            price,
            cfg,
            side=OrderSide.BUY,
            quantity_rules=quantity_rules,
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
            side=OrderSide.BUY,
            quantity=capped_quantity,
            snapshot=snapshot,
        )
        _count_report(report, counters)
    return counters


def _execute_rebalance(
    engine: ExecutionEngine,
    account: Account,
    day: DailyRows,
    trade_date: date,
    desired_symbols: list[str],
    desired_weights: Mapping[str, float] | None,
    cfg: FreeRealBacktestConfig,
    valuation_prices: dict[str, float] | None = None,
    liquidity_day: DailyRows | None = None,
) -> dict[str, float]:
    counters = _empty_rebalance_counters()
    sell_counts = _execute_stock_sells(
        engine=engine,
        account=account,
        day=day,
        trade_date=trade_date,
        desired_symbols=desired_symbols,
        desired_weights=desired_weights,
        cfg=cfg,
        valuation_prices=valuation_prices,
        liquidity_day=liquidity_day,
    )
    _merge_rebalance_counters(counters, sell_counts)
    buy_counts = _execute_stock_buys(
        engine=engine,
        account=account,
        day=day,
        trade_date=trade_date,
        desired_symbols=desired_symbols,
        desired_weights=desired_weights,
        cfg=cfg,
        valuation_prices=valuation_prices,
        liquidity_day=liquidity_day,
    )
    _merge_rebalance_counters(counters, buy_counts)
    return counters


def _rebalance_requires_retry(counters: dict[str, float]) -> bool:
    return any(
        float(counters.get(key, 0.0)) > 0.0
        for key in (
            "rejected_orders",
            "clipped_orders",
            "participation_clipped_orders",
            "participation_blocked_orders",
            "blocked_missing_rows",
        )
    )


def _apply_terminal_settlements(
    account: Account,
    day: DailyRows,
    cfg: FreeRealBacktestConfig,
    last_prices: dict[str, float],
) -> dict[str, float]:
    counters = {
        "terminal_exit_positions": 0.0,
        "terminal_exit_shares": 0.0,
        "terminal_recovery": 0.0,
        "terminal_writeoff_notional": 0.0,
    }
    for symbol, quantity in sorted(_current_quantities(account).items()):
        row = day.get(symbol)
        if row is None or not bool(row.get("delisting_exit_required", False)):
            continue
        close = _execution_price(row, "close")
        reference_notional = float(quantity * close) if np.isfinite(close) and close > 0.0 else 0.0
        terminal_price = close * cfg.delisting_terminal_value_fraction if np.isfinite(close) and close > 0.0 else 0.0
        settled_quantity, recovery = account.apply_terminal_value(symbol, terminal_price)
        last_prices.pop(symbol, None)
        counters["terminal_exit_positions"] += 1.0
        counters["terminal_exit_shares"] += float(settled_quantity)
        counters["terminal_recovery"] += float(recovery)
        counters["terminal_writeoff_notional"] += max(reference_notional - recovery, 0.0)
    return counters


def _apply_corporate_actions(account: Account, day: DailyRows) -> dict[str, float]:
    counters = {
        "corporate_action_events": 0.0,
        "corporate_action_shares_before": 0.0,
        "corporate_action_shares_after": 0.0,
        "corporate_action_cash_in_lieu": 0.0,
    }
    for symbol in sorted(_current_quantities(account)):
        row = day.get(symbol)
        if row is None:
            continue
        factor_value = row.get("corporate_action_share_factor", 1.0)
        factor = 1.0 if pd.isna(factor_value) else float(factor_value)
        if abs(factor - 1.0) <= 1e-9:
            continue
        settlement_price = _execution_price(row, "pre_close")
        if not np.isfinite(settlement_price) or settlement_price < 0.0:
            raise ValueError(f"invalid corporate-action settlement price for {symbol}: {settlement_price}")
        before, after, cash_in_lieu = account.apply_share_factor(
            symbol=symbol,
            share_factor=factor,
            settlement_price=settlement_price,
        )
        counters["corporate_action_events"] += 1.0
        counters["corporate_action_shares_before"] += float(before)
        counters["corporate_action_shares_after"] += float(after)
        counters["corporate_action_cash_in_lieu"] += float(cash_in_lieu)
    return counters


def simulate_free_real_window(
    panel_by_date: Mapping[str, DailyRows],
    trading_dates: list[str],
    top_by_signal_date: dict[str, list[str]],
    rebalance_dates: set[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
    deposit_timing: str,
    cfg: FreeRealBacktestConfig,
    rebalance_only_on_target_change: bool = False,
    account_factory: Callable[..., Account] = Account,
    derivative_coordinator_factory: (
        Callable[[Account], FreeRealDerivativeCoordinator] | None
    ) = None,
    target_weights_by_signal_date: (
        Mapping[str, Mapping[str, float]] | None
    ) = None,
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
    account = account_factory(cash=0.0)
    if not isinstance(account, Account):
        raise TypeError("account_factory must return an Account")
    derivative_coordinator = (
        None
        if derivative_coordinator_factory is None
        else derivative_coordinator_factory(account)
    )
    if (
        derivative_coordinator is not None
        and getattr(derivative_coordinator, "account", None) is not account
    ):
        raise ValueError("derivative coordinator must share the simulated account")
    last_desired: list[str] = []
    last_desired_weights: Mapping[str, float] | None = None
    pending_rebalance = False
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
        "requested_sell_notional": 0.0,
        "participation_unfilled_sell_notional": 0.0,
        "blocked_missing_rows": 0.0,
        "terminal_exit_positions": 0.0,
        "terminal_exit_shares": 0.0,
        "terminal_recovery": 0.0,
        "terminal_writeoff_notional": 0.0,
        "rebalance_checks": 0.0,
        "rebalance_executions": 0.0,
        "rebalance_retry_checks": 0.0,
        "rebalance_retry_executions": 0.0,
        "corporate_action_events": 0.0,
        "corporate_action_shares_before": 0.0,
        "corporate_action_shares_after": 0.0,
        "corporate_action_cash_in_lieu": 0.0,
    }
    if derivative_coordinator is not None:
        counters.update(
            {
                "derivative_coordinator_active": 1.0,
                "margin_call_days": 0.0,
                "default_events": 0.0,
                "max_margin_shortfall": 0.0,
                "raw_default_nav": 0.0,
            }
        )
    prev_equity = 0.0
    last_prices: dict[str, float] = {}
    defaulted = False
    raw_default_nav = 0.0
    for i, trade_date_key in enumerate(window_dates):
        trade_date = _to_date(trade_date_key)
        day = panel_by_date[trade_date_key]
        if defaulted:
            records.append(
                {
                    "trade_date": trade_date_key,
                    "wealth": 0.0,
                    "daily_return": 0.0,
                    "cash": 0.0,
                    "positions": len(_current_quantities(account)),
                    "desired_positions": len(last_desired),
                    "raw_wealth": float(raw_default_nav),
                    "raw_cash": float(account.cash),
                    "margin_status": "default",
                    "defaulted": True,
                }
            )
            prev_equity = 0.0
            continue
        corporate_action_counts = _apply_corporate_actions(account, day)
        for key, value in corporate_action_counts.items():
            counters[key] = counters.get(key, 0.0) + value
        deposit_today = 0.0
        if deposit_timing == "beginning" and month_start[i]:
            account.cash += cfg.monthly_deposit
            deposit_today = cfg.monthly_deposit
        if derivative_coordinator is not None:
            _merge_rebalance_counters(
                counters,
                derivative_coordinator.execute_reductions(trade_date_key),
            )

        is_signal_rebalance = trade_date_key in rebalance_dates
        target_changed = False
        if is_signal_rebalance:
            counters["rebalance_checks"] += 1.0
            previous_signal_date = window_dates[i - 1] if i > 0 else ""
            desired_today = top_by_signal_date.get(previous_signal_date, [])
            desired_weights_today = (
                None
                if target_weights_by_signal_date is None
                else dict(target_weights_by_signal_date.get(previous_signal_date, {}))
            )
            target_changed = (
                desired_today != last_desired
                or desired_weights_today != last_desired_weights
            )
            last_desired = desired_today
            last_desired_weights = desired_weights_today
        if pending_rebalance and not is_signal_rebalance:
            counters["rebalance_retry_checks"] += 1.0
        should_execute = (
            pending_rebalance
            or (
                is_signal_rebalance
                and (
                    not rebalance_only_on_target_change
                    or target_changed
                    or bool(month_start[i])
                )
            )
            or (bool(month_start[i]) and bool(last_desired))
        )
        if should_execute:
            counters["rebalance_executions"] += 1.0
            if pending_rebalance:
                counters["rebalance_retry_executions"] += 1.0
            previous_market_date = window_dates[i - 1] if i > 0 else ""
            open_valuation_prices = dict(last_prices)
            relevant_symbols = set(_current_quantities(account)) | set(last_desired)
            open_valuation_prices.update(_market_prices(day, "open", relevant_symbols))
            if derivative_coordinator is None:
                rebalance_counts = _execute_rebalance(
                    engine=engine,
                    account=account,
                    day=day,
                    trade_date=trade_date,
                    desired_symbols=last_desired,
                    desired_weights=last_desired_weights,
                    cfg=cfg,
                    valuation_prices=open_valuation_prices,
                    liquidity_day=panel_by_date.get(previous_market_date, {}),
                )
            else:
                rebalance_counts = _execute_stock_sells(
                    engine=engine,
                    account=account,
                    day=day,
                    trade_date=trade_date,
                    desired_symbols=last_desired,
                    desired_weights=last_desired_weights,
                    cfg=cfg,
                    valuation_prices=open_valuation_prices,
                    liquidity_day=panel_by_date.get(previous_market_date, {}),
                )
                _merge_rebalance_counters(
                    counters,
                    derivative_coordinator.execute_increases(trade_date_key),
                )
                _merge_rebalance_counters(
                    rebalance_counts,
                    _execute_stock_buys(
                        engine=engine,
                        account=account,
                        day=day,
                        trade_date=trade_date,
                        desired_symbols=last_desired,
                        desired_weights=last_desired_weights,
                        cfg=cfg,
                        valuation_prices=open_valuation_prices,
                        liquidity_day=panel_by_date.get(previous_market_date, {}),
                    ),
                )
            pending_rebalance = _rebalance_requires_retry(rebalance_counts)
            for key, value in rebalance_counts.items():
                counters[key] = counters.get(key, 0.0) + value
        elif derivative_coordinator is not None:
            _merge_rebalance_counters(
                counters,
                derivative_coordinator.execute_increases(trade_date_key),
            )

        close_prices = _market_prices(day, "close", _current_quantities(account))
        last_prices.update(close_prices)
        terminal_counts = _apply_terminal_settlements(account, day, cfg, last_prices)
        for key, value in terminal_counts.items():
            counters[key] = counters.get(key, 0.0) + value
        margin_status = "ok"
        margin_shortfall = 0.0
        if derivative_coordinator is None:
            equity = account.total_equity(last_prices)
        else:
            stock_marks = {
                symbol: last_prices[symbol]
                for symbol in _current_quantities(account)
                if symbol in last_prices
            }
            derivative_eod = derivative_coordinator.settle_end_of_day(
                trade_date_key,
                stock_marks,
            )
            equity = float(derivative_eod.nav)
            margin_status = str(derivative_eod.margin_status).strip().lower()
            margin_shortfall = float(derivative_eod.margin_shortfall)
            settlement_fees = float(derivative_eod.settlement_fees)
            if not np.isfinite(settlement_fees) or settlement_fees < 0.0:
                raise ValueError(
                    "derivative settlement fees must be finite and non-negative"
                )
            counters["derivative_fees"] = counters.get(
                "derivative_fees", 0.0
            ) + settlement_fees
            counters["futures_expiry_settlements"] = counters.get(
                "futures_expiry_settlements", 0.0
            ) + float(derivative_eod.futures_expiry_settlements)
            counters["option_expiry_settlements"] = counters.get(
                "option_expiry_settlements", 0.0
            ) + float(derivative_eod.option_expiry_settlements)
            if not np.isfinite(equity):
                raise ValueError("derivative end-of-day NAV must be finite")
            if margin_status not in {"ok", "margin_call", "default"}:
                raise ValueError(f"unsupported derivative margin status: {margin_status}")
            if not np.isfinite(margin_shortfall) or margin_shortfall < 0.0:
                raise ValueError("derivative margin shortfall must be finite and non-negative")
            counters["max_margin_shortfall"] = max(
                counters["max_margin_shortfall"],
                margin_shortfall,
            )
            if margin_status == "margin_call":
                counters["margin_call_days"] += 1.0

        raw_equity = float(equity)
        if derivative_coordinator is not None and margin_status == "default":
            defaulted = True
            raw_default_nav = raw_equity
            counters["default_events"] += 1.0
            counters["raw_default_nav"] = raw_default_nav
            equity = 0.0
        elif deposit_timing == "ending" and month_end[i]:
            account.cash += cfg.monthly_deposit
            deposit_today = cfg.monthly_deposit
            equity += cfg.monthly_deposit
            raw_equity += cfg.monthly_deposit

        if (
            derivative_coordinator is not None
            and not defaulted
            and i + 1 < len(window_dates)
        ):
            _merge_rebalance_counters(
                counters,
                derivative_coordinator.latch_close_signal(
                    trade_date_key,
                    float(equity),
                    force_flat=margin_status != "ok",
                ),
            )
        base = prev_equity + deposit_today
        daily_return = 0.0 if base <= 0.0 else equity / base - 1.0
        record = {
            "trade_date": trade_date_key,
            "wealth": float(equity),
            "daily_return": float(daily_return),
            "cash": 0.0 if defaulted else float(account.cash),
            "positions": len(_current_quantities(account)),
            "desired_positions": len(last_desired),
        }
        if derivative_coordinator is not None:
            record.update(
                {
                    "raw_wealth": raw_equity,
                    "raw_cash": float(account.cash),
                    "margin_status": margin_status,
                    "margin_shortfall": margin_shortfall,
                    "defaulted": defaulted,
                }
            )
        records.append(record)
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
    requested_sell_notional = float(counters.get("requested_sell_notional", 0.0))
    unfilled_sell_notional = float(counters.get("participation_unfilled_sell_notional", 0.0))
    metrics["requested_sell_notional"] = requested_sell_notional
    metrics["participation_unfilled_sell_notional"] = unfilled_sell_notional
    metrics["participation_unfilled_sell_share"] = (
        unfilled_sell_notional / requested_sell_notional if requested_sell_notional > 0.0 else 0.0
    )
    metrics["blocked_missing_rows"] = float(counters.get("blocked_missing_rows", 0.0))
    metrics["terminal_exit_positions"] = float(counters.get("terminal_exit_positions", 0.0))
    metrics["terminal_exit_shares"] = float(counters.get("terminal_exit_shares", 0.0))
    metrics["terminal_recovery"] = float(counters.get("terminal_recovery", 0.0))
    metrics["terminal_writeoff_notional"] = float(counters.get("terminal_writeoff_notional", 0.0))
    metrics["rebalance_checks"] = float(counters.get("rebalance_checks", 0.0))
    metrics["rebalance_executions"] = float(counters.get("rebalance_executions", 0.0))
    metrics["rebalance_retry_checks"] = float(counters.get("rebalance_retry_checks", 0.0))
    metrics["rebalance_retry_executions"] = float(counters.get("rebalance_retry_executions", 0.0))
    metrics["corporate_action_events"] = float(counters.get("corporate_action_events", 0.0))
    metrics["corporate_action_shares_before"] = float(counters.get("corporate_action_shares_before", 0.0))
    metrics["corporate_action_shares_after"] = float(counters.get("corporate_action_shares_after", 0.0))
    metrics["corporate_action_cash_in_lieu"] = float(counters.get("corporate_action_cash_in_lieu", 0.0))
    return equity_frame, metrics


def select_rolling_windows(
    windows: list[tuple[pd.Timestamp, pd.Timestamp]],
    max_windows: int = 0,
    sampling: str = "head",
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    if max_windows <= 0 or len(windows) <= max_windows:
        return list(windows)
    if sampling == "head":
        return list(windows[:max_windows])
    if sampling != "even":
        raise ValueError(f"unsupported rolling-window sampling: {sampling}")
    indices = np.linspace(0, len(windows) - 1, num=max_windows)
    selected_indices = sorted({int(round(value)) for value in indices})
    if len(selected_indices) < max_windows:
        for index in range(len(windows)):
            if index not in selected_indices:
                selected_indices.append(index)
            if len(selected_indices) >= max_windows:
                break
        selected_indices.sort()
    return [windows[index] for index in selected_indices[:max_windows]]


def filter_rolling_windows_by_start(
    windows: list[tuple[pd.Timestamp, pd.Timestamp]],
    start_min: str = "",
    start_max: str = "",
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    lower = pd.Timestamp(start_min) if start_min else None
    upper = pd.Timestamp(start_max) if start_max else None
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("window_start_min must not exceed window_start_max")
    return [
        (start, end)
        for start, end in windows
        if (lower is None or start >= lower) and (upper is None or start <= upper)
    ]


def evaluate_free_real_strategy(
    panel: pd.DataFrame,
    spec: RealStockStrategySpec,
    cfg: FreeRealBacktestConfig,
    deposit_timings: Iterable[str] = ("beginning", "ending"),
    max_windows: int = 0,
    window_sampling: str = "head",
    window_start_min: str = "",
    window_start_max: str = "",
    panel_by_date: Mapping[str, DailyRows] | None = None,
    account_factory: Callable[..., Account] = Account,
    derivative_coordinator_factory: (
        Callable[[Account], FreeRealDerivativeCoordinator] | None
    ) = None,
) -> pd.DataFrame:
    date_index = _date_index(panel)
    windows = rolling_windows(date_index, window_months=cfg.window_months, min_trading_days=cfg.min_trading_days)
    windows = filter_rolling_windows_by_start(windows, start_min=window_start_min, start_max=window_start_max)
    windows = select_rolling_windows(windows, max_windows=max_windows, sampling=window_sampling)
    if not windows:
        return pd.DataFrame()
    panel_by_date = panel_by_date or _prepare_daily_panel(panel)
    trading_dates = sorted(panel_by_date)
    scores = compute_real_stock_scores(panel, spec)
    holding_k = int(spec.params.get("holding_k", 10))
    rebalance = str(spec.params.get("rebalance", "daily"))
    rebalance_dates = set(strategy_rebalance_dates(panel, rebalance))
    stateful_kind = str(spec.params.get("kind", "")) in {
        "real_stock_breakout",
        "real_stateful_trend",
        "real_volatility_contraction",
        "real_regime_contraction",
        "real_gap_intraday",
        "real_momentum_acceleration",
        "real_post_limit_release",
    }
    shared_target_book = (
        {}
        if stateful_kind
        else target_symbols_by_signal_date(scores, spec=spec, holding_k=holding_k)
    )
    uses_explicit_stock_weights = any(
        key in spec.params
        for key in ("weighting", "gross_exposure", "max_weight", "risk_window", "risk_floor")
    )
    shared_target_weights = (
        stock_target_weights_by_signal_date(scores, shared_target_book, spec)
        if uses_explicit_stock_weights and not stateful_kind
        else None
    )
    window_target_books: dict[tuple[str, str], dict[str, list[str]]] = {}
    window_target_weights: dict[
        tuple[str, str], dict[str, dict[str, float]]
    ] = {}
    rows = []
    for deposit_timing in deposit_timings:
        for start, end in windows:
            window_key = (_date_key(start), _date_key(end))
            if stateful_kind and window_key not in window_target_books:
                window_target_books[window_key] = target_symbols_by_signal_date(
                    scores,
                    spec=spec,
                    holding_k=holding_k,
                    start_date=window_key[0],
                    end_date=window_key[1],
                )
                if uses_explicit_stock_weights:
                    window_target_weights[window_key] = (
                        stock_target_weights_by_signal_date(
                            scores,
                            window_target_books[window_key],
                            spec,
                        )
                    )
            top_by_signal_date = (
                window_target_books[window_key] if stateful_kind else shared_target_book
            )
            target_weights_by_signal_date = (
                window_target_weights.get(window_key)
                if stateful_kind
                else shared_target_weights
            )
            _, metrics = simulate_free_real_window(
                panel_by_date=panel_by_date,
                trading_dates=trading_dates,
                top_by_signal_date=top_by_signal_date,
                rebalance_dates=rebalance_dates,
                start=start,
                end=end,
                deposit_timing=deposit_timing,
                cfg=cfg,
                rebalance_only_on_target_change=stateful_kind,
                target_weights_by_signal_date=target_weights_by_signal_date,
                account_factory=account_factory,
                derivative_coordinator_factory=derivative_coordinator_factory,
            )
            if not metrics:
                continue
            rows.append(
                {
                    "strategy": spec.name,
                    "family": spec.family,
                    "data_tier": str(spec.params.get("data_tier", "free_real")),
                    "deposit_timing": deposit_timing,
                    "start": start.date().isoformat(),
                    "end": end.date().isoformat(),
                    "holding_k": holding_k,
                    "rebalance": rebalance,
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def select_nonoverlapping_windows(group: pd.DataFrame) -> pd.DataFrame:
    if not {"start", "end"}.issubset(group.columns):
        return group.copy()
    ordered = group.copy()
    ordered["_start_ts"] = pd.to_datetime(ordered["start"], errors="raise")
    ordered["_end_ts"] = pd.to_datetime(ordered["end"], errors="raise")
    ordered = ordered.sort_values(["_start_ts", "_end_ts"], kind="mergesort")
    selected: list[object] = []
    previous_end: pd.Timestamp | None = None
    for index, row in ordered.iterrows():
        if previous_end is None or row["_start_ts"] > previous_end:
            selected.append(index)
            previous_end = pd.Timestamp(row["_end_ts"])
    return group.loc[selected].copy()


def _wilson_lower_bound(successes: int, trials: int, z: float = 1.959963984540054) -> float:
    if trials <= 0:
        return 0.0
    share = successes / float(trials)
    denominator = 1.0 + z * z / trials
    center = share + z * z / (2.0 * trials)
    radius = z * sqrt((share * (1.0 - share) + z * z / (4.0 * trials)) / trials)
    return max(0.0, (center - radius) / denominator)


def _binomial_upper_tail_pvalue(successes: int, trials: int) -> float:
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("binomial successes and trials must satisfy 0 <= successes <= trials")
    if trials == 0 or successes == 0:
        return 1.0
    favorable_outcomes = sum(comb(trials, hits) for hits in range(successes, trials + 1))
    return float(favorable_outcomes / (1 << trials))


def aggregate_free_real_windows(windows: pd.DataFrame, cfg: FreeRealBacktestConfig) -> pd.DataFrame:
    if windows.empty:
        return pd.DataFrame()
    unique_columns = ["strategy", "data_tier", "deposit_timing", "start", "end"]
    if set(unique_columns).issubset(windows.columns):
        duplicated = windows.duplicated(unique_columns, keep=False)
        if duplicated.any():
            sample = windows.loc[duplicated, unique_columns].iloc[0].to_dict()
            raise ValueError(f"duplicate rolling window result: {sample}")
    rows = []
    group_cols = ["strategy", "family", "data_tier", "deposit_timing"]

    def mean_col(group: pd.DataFrame, column: str) -> float:
        return float(group[column].mean()) if column in group.columns else 0.0

    liquidity_columns = {
        "requested_sell_notional",
        "participation_unfilled_sell_notional",
        "participation_unfilled_sell_share",
        "blocked_missing_rows",
    }
    margin_columns = {
        "margin_call_days",
        "default_events",
        "max_margin_shortfall",
        "raw_default_nav",
    }
    for key, group in windows.groupby(group_cols, sort=True):
        success = (group["w12"] >= cfg.target_month_12) & (group["w24"] >= cfg.target_month_24)
        nonoverlap = select_nonoverlapping_windows(group)
        nonoverlap_success = (
            (nonoverlap["w12"] >= cfg.target_month_12)
            & (nonoverlap["w24"] >= cfg.target_month_24)
        )
        nonoverlap_successes = int(nonoverlap_success.sum())
        n_nonoverlap = int(len(nonoverlap))
        has_sell_liquidity_accounting = liquidity_columns.issubset(group.columns)
        sell_liquidity_accounting_valid = False
        mean_unfilled_sell_share = float("nan")
        worst_unfilled_sell_share = float("nan")
        max_blocked_missing_rows = float("nan")
        if has_sell_liquidity_accounting:
            requested_sell = pd.to_numeric(group["requested_sell_notional"], errors="coerce")
            unfilled_sell = pd.to_numeric(
                group["participation_unfilled_sell_notional"], errors="coerce"
            )
            unfilled_share = pd.to_numeric(
                group["participation_unfilled_sell_share"], errors="coerce"
            )
            missing_rows = pd.to_numeric(group["blocked_missing_rows"], errors="coerce")
            expected_share = pd.Series(0.0, index=group.index, dtype=float)
            positive_requested = requested_sell > 0.0
            expected_share.loc[positive_requested] = (
                unfilled_sell.loc[positive_requested] / requested_sell.loc[positive_requested]
            )
            notional_tolerance = requested_sell.abs() * 1e-12 + 1e-9
            valid_values = (
                np.isfinite(requested_sell)
                & np.isfinite(unfilled_sell)
                & np.isfinite(unfilled_share)
                & np.isfinite(missing_rows)
                & (requested_sell >= 0.0)
                & (unfilled_sell >= 0.0)
                & (unfilled_sell <= requested_sell + notional_tolerance)
                & unfilled_share.between(0.0, 1.0)
                & (missing_rows >= 0.0)
            )
            sell_liquidity_accounting_valid = bool(
                valid_values.all()
                and np.allclose(
                    unfilled_share.to_numpy(dtype=float),
                    expected_share.to_numpy(dtype=float),
                    rtol=1e-9,
                    atol=1e-12,
                )
            )
            mean_unfilled_sell_share = float(unfilled_share.mean(skipna=False))
            worst_unfilled_sell_share = float(unfilled_share.max(skipna=False))
            max_blocked_missing_rows = float(missing_rows.max(skipna=False))
        derivative_active = bool(
            "derivative_coordinator_active" in group.columns
            and pd.to_numeric(
                group["derivative_coordinator_active"], errors="coerce"
            ).fillna(0.0).gt(0.0).any()
        )
        has_margin_accounting = margin_columns.issubset(group.columns)
        margin_accounting_valid = not derivative_active
        margin_call_window_share = 0.0
        default_window_share = 0.0
        max_margin_shortfall = 0.0
        min_raw_default_nav = 0.0
        if has_margin_accounting:
            margin_calls = pd.to_numeric(group["margin_call_days"], errors="coerce")
            defaults = pd.to_numeric(group["default_events"], errors="coerce")
            shortfalls = pd.to_numeric(group["max_margin_shortfall"], errors="coerce")
            raw_default_navs = pd.to_numeric(group["raw_default_nav"], errors="coerce")
            margin_accounting_valid = bool(
                np.isfinite(margin_calls).all()
                and np.isfinite(defaults).all()
                and np.isfinite(shortfalls).all()
                and np.isfinite(raw_default_navs).all()
                and margin_calls.ge(0.0).all()
                and defaults.ge(0.0).all()
                and shortfalls.ge(0.0).all()
            )
            margin_call_window_share = float(margin_calls.gt(0.0).mean())
            default_window_share = float(defaults.gt(0.0).mean())
            max_margin_shortfall = float(shortfalls.max(skipna=False))
            min_raw_default_nav = float(raw_default_navs.min(skipna=False))
        rows.append(
            {
                "strategy": key[0],
                "family": key[1],
                "data_tier": key[2],
                "deposit_timing": key[3],
                "n_windows": int(len(group)),
                "p_success": float(success.mean()),
                "rolling_start_hit_share": float(success.mean()),
                "n_nonoverlap_windows": n_nonoverlap,
                "n_nonoverlap_successes": nonoverlap_successes,
                "nonoverlap_hit_share": float(nonoverlap_success.mean()),
                "nonoverlap_hit_share_lower95": _wilson_lower_bound(nonoverlap_successes, n_nonoverlap),
                "nonoverlap_binomial_pvalue": _binomial_upper_tail_pvalue(
                    nonoverlap_successes, n_nonoverlap
                ),
                "p_w12": float((group["w12"] >= cfg.target_month_12).mean()),
                "p_w24": float((group["w24"] >= cfg.target_month_24).mean()),
                "median_w12": float(group["w12"].median()),
                "median_w24": float(group["w24"].median()),
                "nonoverlap_median_w12": float(nonoverlap["w12"].median()),
                "nonoverlap_median_w24": float(nonoverlap["w24"].median()),
                "p05_w24": float(group["w24"].quantile(0.05)),
                "nonoverlap_p05_w24": float(nonoverlap["w24"].quantile(0.05)),
                "nonoverlap_min_w24": float(nonoverlap["w24"].min(skipna=False)),
                "p10_w24": float(group["w24"].quantile(0.10)),
                "p90_w24": float(group["w24"].quantile(0.90)),
                "median_max_drawdown": float(group["max_drawdown"].median()),
                "p95_max_drawdown": float(group["max_drawdown"].quantile(0.95)),
                "nonoverlap_p95_max_drawdown": float(nonoverlap["max_drawdown"].quantile(0.95)),
                "nonoverlap_max_drawdown": float(
                    nonoverlap["max_drawdown"].max(skipna=False)
                ),
                "p_w24_below_deposit": float((group["w24"] < group["total_deposit"]).mean()),
                "p_w24_below_720k": float((group["w24"] < 720_000).mean()),
                "nonoverlap_p_w24_below_deposit": float(
                    (nonoverlap["w24"] < nonoverlap["total_deposit"]).mean()
                ),
                "p_drawdown_gt_35": float((group["max_drawdown"] > 0.35).mean()),
                "p_drawdown_gt_50": float((group["max_drawdown"] > 0.50).mean()),
                "nonoverlap_p_drawdown_gt_35": float((nonoverlap["max_drawdown"] > 0.35).mean()),
                "nonoverlap_p_drawdown_gt_50": float((nonoverlap["max_drawdown"] > 0.50).mean()),
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
                "avg_requested_sell_notional": mean_col(group, "requested_sell_notional"),
                "avg_participation_unfilled_sell_notional": mean_col(
                    group, "participation_unfilled_sell_notional"
                ),
                "mean_participation_unfilled_sell_share": mean_unfilled_sell_share,
                "worst_participation_unfilled_sell_share": worst_unfilled_sell_share,
                "avg_blocked_missing_rows": mean_col(group, "blocked_missing_rows"),
                "max_blocked_missing_rows": max_blocked_missing_rows,
                "has_sell_liquidity_accounting": has_sell_liquidity_accounting,
                "sell_liquidity_accounting_valid": sell_liquidity_accounting_valid,
                "has_participation_cap": cfg.max_daily_amount_participation is not None,
                "avg_terminal_exit_positions": mean_col(group, "terminal_exit_positions"),
                "avg_terminal_writeoff_notional": mean_col(group, "terminal_writeoff_notional"),
                "avg_corporate_action_events": mean_col(group, "corporate_action_events"),
                "avg_corporate_action_cash_in_lieu": mean_col(group, "corporate_action_cash_in_lieu"),
                "avg_rebalance_checks": mean_col(group, "rebalance_checks"),
                "avg_rebalance_executions": mean_col(group, "rebalance_executions"),
                "avg_fees": mean_col(group, "fees"),
                "derivative_coordinator_active": derivative_active,
                "has_margin_accounting": has_margin_accounting,
                "margin_accounting_valid": margin_accounting_valid,
                "margin_call_window_share": margin_call_window_share,
                "default_window_share": default_window_share,
                "max_margin_shortfall": max_margin_shortfall,
                "min_raw_default_nav": min_raw_default_nav,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["score"] = (
        100.0 * out["nonoverlap_hit_share_lower95"]
        + 20.0 * np.log((out["nonoverlap_median_w24"] / cfg.target_month_24).clip(lower=1e-9))
        + 10.0 * np.log(
            (out["nonoverlap_p05_w24"] / max(cfg.candidate_min_p05_w24, 1e-9)).clip(lower=1e-9)
        )
        - 20.0 * out["nonoverlap_p_w24_below_deposit"]
        - 25.0 * out["nonoverlap_p_drawdown_gt_35"]
        - 15.0 * out["nonoverlap_p_drawdown_gt_50"]
        - 10.0 * out["avg_turnover"].clip(lower=0.0, upper=1.0)
        - 30.0 * out["margin_call_window_share"]
        - 100.0 * out["default_window_share"]
    )
    out["passes_median_target"] = (
        (out["nonoverlap_median_w12"] >= cfg.target_month_12)
        & (out["nonoverlap_median_w24"] >= cfg.target_month_24)
    )
    out["passes_min_nonoverlap_blocks"] = (
        out["n_nonoverlap_windows"] >= cfg.candidate_min_nonoverlap_blocks
    )
    out["passes_wilson_descriptive_threshold"] = (
        out["nonoverlap_hit_share_lower95"]
        >= cfg.candidate_min_nonoverlap_hit_share_lower95
    )
    out["passes_joint_success_gate"] = (
        out["passes_min_nonoverlap_blocks"]
        & (out["nonoverlap_binomial_pvalue"] <= cfg.candidate_joint_success_alpha)
    )
    out["passes_p05_deposit_floor"] = out["nonoverlap_p05_w24"] >= cfg.candidate_min_p05_w24
    out["passes_tail_gate"] = (
        out["nonoverlap_min_w24"] >= cfg.candidate_min_nonoverlap_w24
    )
    out["passes_p95_drawdown_gate"] = (
        out["nonoverlap_p95_max_drawdown"] <= cfg.candidate_max_p95_drawdown
    )
    out["passes_drawdown_gate"] = (
        out["nonoverlap_max_drawdown"] <= cfg.candidate_max_nonoverlap_drawdown
    )
    out["passes_liquidity_gate"] = (
        out["has_participation_cap"]
        & out["has_sell_liquidity_accounting"]
        & out["sell_liquidity_accounting_valid"]
        & (out["max_blocked_missing_rows"] == 0.0)
        & (
            out["worst_participation_unfilled_sell_share"]
            <= cfg.candidate_max_unfilled_sell_share
        )
    )
    out["passes_margin_gate"] = (
        ~out["derivative_coordinator_active"]
        | (
            out["has_margin_accounting"]
            & out["margin_accounting_valid"]
            & (
                out["margin_call_window_share"]
                <= cfg.candidate_max_margin_call_window_share
            )
            & (
                out["default_window_share"]
                <= cfg.candidate_max_default_window_share
            )
        )
    )
    out["passes_core_candidate_gates"] = (
        out["passes_median_target"]
        & out["passes_joint_success_gate"]
        & out["passes_tail_gate"]
        & out["passes_drawdown_gate"]
        & out["passes_liquidity_gate"]
        & out["passes_margin_gate"]
    )
    return out.sort_values(["score", "p_success", "median_w24"], ascending=False).reset_index(drop=True)

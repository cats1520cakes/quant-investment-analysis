from __future__ import annotations

import json
import hashlib
from pathlib import Path
import time
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Tuple

import pandas as pd

from .config import ProjectConfig, ensure_data_dirs


REQUIRED_PRICE_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount")


def instrument_map(config: ProjectConfig) -> Dict[str, Dict[str, str]]:
    if "instruments" in config.raw:
        return {
            symbol: {
                "name": str(meta.get("name", symbol)),
                "baostock_code": str(meta.get("baostock_code", "")),
            }
            for symbol, meta in config.raw["instruments"].items()
        }
    return {symbol: {"name": str(name), "baostock_code": ""} for symbol, name in config.raw.get("etfs", {}).items()}


def _normalize_akshare_etf_frame(frame: pd.DataFrame, symbol: str, name: str) -> pd.DataFrame:
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
        "涨跌幅": "pct_change",
        "涨跌额": "change",
        "换手率": "turnover",
    }
    normalized = frame.rename(columns=rename_map).copy()
    missing = [column for column in REQUIRED_PRICE_COLUMNS if column not in normalized.columns]
    if missing:
        raise ValueError(f"{symbol} missing required columns: {missing}; got {list(frame.columns)}")

    normalized = normalized.loc[:, [column for column in rename_map.values() if column in normalized.columns]]
    normalized["date"] = pd.to_datetime(normalized["date"])
    for column in normalized.columns:
        if column != "date":
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized["symbol"] = symbol
    normalized["name"] = name
    normalized = normalized.sort_values("date").drop_duplicates(["date", "symbol"])
    return normalized


def _normalize_baostock_frame(frame: pd.DataFrame, symbol: str, name: str) -> pd.DataFrame:
    rename_map = {
        "preclose": "pre_close",
        "pctChg": "pct_change",
        "turn": "turnover",
    }
    normalized = frame.rename(columns=rename_map).copy()
    missing = [column for column in REQUIRED_PRICE_COLUMNS if column not in normalized.columns]
    if missing:
        raise ValueError(f"{symbol} missing required columns: {missing}; got {list(frame.columns)}")
    normalized = normalized.loc[
        :,
        [
            "date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "volume",
            "amount",
            "pct_change",
            "turnover",
        ],
    ]
    normalized["date"] = pd.to_datetime(normalized["date"])
    for column in normalized.columns:
        if column != "date":
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized["symbol"] = symbol
    normalized["name"] = name
    return normalized.sort_values("date").drop_duplicates(["date", "symbol"])


def _download_baostock_index_frame(config: ProjectConfig, symbol: str, name: str, baostock_code: str) -> pd.DataFrame:
    import baostock as bs

    fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg,turn"
    start = f"{config.start_date[:4]}-{config.start_date[4:6]}-{config.start_date[6:8]}"
    end = f"{config.end_date[:4]}-{config.end_date[4:6]}-{config.end_date[6:8]}"
    result = bs.query_history_k_data_plus(
        baostock_code,
        fields,
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag="2",
    )
    rows = []
    while result.error_code == "0" and result.next():
        rows.append(result.get_row_data())
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock failed for {baostock_code}: {result.error_msg}")
    frame = pd.DataFrame(rows, columns=result.fields)
    if frame.empty:
        raise RuntimeError(f"BaoStock returned no rows for {baostock_code}")
    return _normalize_baostock_frame(frame, symbol=symbol, name=name)


def _eastmoney_secid(symbol: str) -> str:
    market = "1" if symbol.startswith(("5", "6", "9")) else "0"
    return f"{market}.{symbol}"


def _download_eastmoney_etf_frame(config: ProjectConfig, symbol: str, name: str) -> pd.DataFrame:
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "101",
        "fqt": "1",
        "beg": config.start_date,
        "end": config.end_date,
        "secid": _eastmoney_secid(symbol),
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(params)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    with opener.open(request, timeout=8) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if data.get("rc") != 0 or not data.get("data") or not data["data"].get("klines"):
        raise RuntimeError(f"Eastmoney returned no kline data for {symbol}: {payload[:200]}")
    rows = []
    for line in data["data"]["klines"]:
        parts = line.split(",")
        rows.append(
            {
                "date": parts[0],
                "open": parts[1],
                "close": parts[2],
                "high": parts[3],
                "low": parts[4],
                "volume": parts[5],
                "amount": parts[6],
                "amplitude": parts[7],
                "pct_change": parts[8],
                "change": parts[9],
                "turnover": parts[10] if len(parts) > 10 else None,
            }
        )
    return _normalize_akshare_etf_frame(pd.DataFrame(rows), symbol=symbol, name=name)


def download_etf_daily(config: ProjectConfig, force: bool = False, retries: int = 3) -> Dict[str, Path]:
    ensure_data_dirs(config.data_root)
    source = str(config.raw.get("data_source", "eastmoney_etf"))
    if source == "baostock_index":
        return download_baostock_index_daily(config, force=force, retries=retries)
    raw_dir = config.data_root / "raw" / "akshare" / "etf_daily"
    outputs: Dict[str, Path] = {}
    failures: List[str] = []
    for symbol, meta in instrument_map(config).items():
        name = meta["name"]
        out_path = raw_dir / f"{symbol}.csv"
        if out_path.exists() and not force:
            print(f"[cache] {symbol} {name} -> {out_path}", flush=True)
            outputs[symbol] = out_path
            continue
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                print(f"[download] {symbol} {name} attempt={attempt}", flush=True)
                normalized = _download_eastmoney_etf_frame(config, symbol=symbol, name=name)
                normalized.to_csv(out_path, index=False, encoding="utf-8")
                print(f"[ok] {symbol} rows={len(normalized)} -> {out_path}", flush=True)
                outputs[symbol] = out_path
                break
            except Exception as exc:  # noqa: BLE001 - keep batch download alive and report exact failures.
                last_error = exc
                print(f"[fail] {symbol} attempt={attempt}: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(min(2 * attempt, 8))
        if symbol not in outputs:
            failures.append(f"{symbol} {name}: {type(last_error).__name__}: {last_error}")
    if failures:
        failure_path = config.data_root / "logs" / "phase1_download_failures.txt"
        failure_path.write_text("\n".join(failures) + "\n", encoding="utf-8")
        if not outputs:
            raise RuntimeError(f"all ETF downloads failed; see {failure_path}")
    return outputs


def download_baostock_index_daily(config: ProjectConfig, force: bool = False, retries: int = 3) -> Dict[str, Path]:
    import baostock as bs

    ensure_data_dirs(config.data_root)
    raw_dir = config.data_root / "raw" / "baostock" / "index_daily"
    outputs: Dict[str, Path] = {}
    failures: List[str] = []
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    try:
        for symbol, meta in instrument_map(config).items():
            name = meta["name"]
            baostock_code = meta["baostock_code"]
            out_path = raw_dir / f"{symbol}.csv"
            if out_path.exists() and not force:
                print(f"[cache] {symbol} {name} -> {out_path}", flush=True)
                outputs[symbol] = out_path
                continue
            last_error = None
            for attempt in range(1, retries + 1):
                try:
                    print(f"[download] {symbol} {name} {baostock_code} attempt={attempt}", flush=True)
                    normalized = _download_baostock_index_frame(config, symbol=symbol, name=name, baostock_code=baostock_code)
                    normalized.to_csv(out_path, index=False, encoding="utf-8")
                    print(f"[ok] {symbol} rows={len(normalized)} -> {out_path}", flush=True)
                    outputs[symbol] = out_path
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    print(f"[fail] {symbol} attempt={attempt}: {type(exc).__name__}: {exc}", flush=True)
                    time.sleep(min(2 * attempt, 8))
            if symbol not in outputs:
                failures.append(f"{symbol} {name}: {type(last_error).__name__}: {last_error}")
    finally:
        bs.logout()
    if failures:
        failure_path = config.data_root / "logs" / "phase1_download_failures.txt"
        failure_path.write_text("\n".join(failures) + "\n", encoding="utf-8")
        if not outputs:
            raise RuntimeError(f"all index downloads failed; see {failure_path}")
    return outputs


def load_raw_etf_prices(config: ProjectConfig) -> Dict[str, pd.DataFrame]:
    if str(config.raw.get("data_source", "eastmoney_etf")) == "baostock_index":
        raw_dir = config.data_root / "raw" / "baostock" / "index_daily"
    else:
        raw_dir = config.data_root / "raw" / "akshare" / "etf_daily"
    prices: Dict[str, pd.DataFrame] = {}
    for symbol, meta in instrument_map(config).items():
        name = meta["name"]
        path = raw_dir / f"{symbol}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        frame["date"] = pd.to_datetime(frame["date"])
        frame["symbol"] = symbol
        frame["name"] = name
        prices[symbol] = frame.sort_values("date")
    return prices


def build_close_matrix(frames: Dict[str, pd.DataFrame]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    close_parts = []
    amount_parts = []
    for symbol, frame in frames.items():
        indexed = frame.set_index("date").sort_index()
        close_parts.append(indexed["close"].rename(symbol))
        if "amount" in indexed:
            amount_parts.append(indexed["amount"].rename(symbol))
    close = pd.concat(close_parts, axis=1).sort_index()
    amount = pd.concat(amount_parts, axis=1).sort_index() if amount_parts else pd.DataFrame(index=close.index)
    return close, amount


def write_processed_prices(config: ProjectConfig, close: pd.DataFrame, amount: pd.DataFrame) -> Dict[str, Path]:
    ensure_data_dirs(config.data_root)
    processed_dir = config.data_root / "processed"
    close_path = processed_dir / "phase1_daily_close.csv"
    amount_path = processed_dir / "phase1_daily_amount.csv"
    close.to_csv(close_path, encoding="utf-8")
    amount.to_csv(amount_path, encoding="utf-8")
    return {"close": close_path, "amount": amount_path}


def load_processed_close(config: ProjectConfig) -> pd.DataFrame:
    path = config.data_root / "processed" / "phase1_daily_close.csv"
    if not path.exists():
        path = config.data_root / "processed" / "phase1_etf_close.csv"
    close = pd.read_csv(path, index_col=0, parse_dates=True)
    close = close.sort_index()
    return close


def available_symbol_names(config: ProjectConfig, symbols: Iterable[str]) -> Dict[str, str]:
    names = {symbol: meta["name"] for symbol, meta in instrument_map(config).items()}
    return {symbol: names.get(symbol, symbol) for symbol in symbols}


def write_download_manifest(config: ProjectConfig, outputs: Dict[str, Path], close: pd.DataFrame) -> Path:
    manifest_dir = config.data_root / "00_meta" / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    instruments = instrument_map(config)
    for symbol, path in outputs.items():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(
            {
                "symbol": symbol,
                "name": instruments[symbol]["name"],
                "source": config.raw.get("data_source", "eastmoney_etf"),
                "raw_path": str(path),
                "rows": int(close[symbol].notna().sum()) if symbol in close.columns else 0,
                "first_date": str(close[symbol].dropna().index.min().date()) if symbol in close.columns and close[symbol].notna().any() else "",
                "last_date": str(close[symbol].dropna().index.max().date()) if symbol in close.columns and close[symbol].notna().any() else "",
                "sha256": digest,
            }
        )
    manifest = pd.DataFrame(rows).sort_values("symbol")
    out_path = manifest_dir / "phase1_daily_manifest.csv"
    manifest.to_csv(out_path, index=False, encoding="utf-8")
    return out_path

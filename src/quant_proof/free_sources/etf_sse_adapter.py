from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd


SSE_DAYK_URL = "https://yunhq.sse.com.cn:32042/v1/sh1/dayk/{code}?begin=0&end={total}&period=day"
SSE_ETF_COLUMNS = ("trade_date", "code", "open", "high", "low", "close", "volume", "amount")


class SseEtfDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class SuspensionException:
    code: str
    trade_date: str
    status: str
    source_url: str
    reason: str


OFFICIAL_SUSPENSION_EXCEPTIONS: tuple[SuspensionException, ...] = (
    SuspensionException(
        "510500", "20150413", "confirmed_suspension",
        "https://www.sse.com.cn/disclosure/fund/announcement/c/2015-04-07/510500_20150408_1.pdf",
        "fund_share_conversion",
    ),
    SuspensionException(
        "510500", "20150414", "confirmed_suspension",
        "https://www.sse.com.cn/disclosure/fund/announcement/c/2015-04-07/510500_20150408_1.pdf",
        "fund_share_conversion",
    ),
    SuspensionException(
        "512100", "20220902", "confirmed_suspension",
        "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2022-08-24/512100_20220824_1_x5QccCZk.pdf",
        "fund_share_consolidation",
    ),
)


def parse_sse_dayk(payload: Mapping[str, object], code: str) -> pd.DataFrame:
    rows = payload.get("kline") or payload.get("data")
    if isinstance(rows, Mapping):
        rows = rows.get("kline")
    if not isinstance(rows, list):
        raise SseEtfDataError("SSE day-k payload has no kline list")
    normalized: list[list[object]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            raise SseEtfDataError("SSE day-k row has fewer than seven fields")
        normalized.append(list(row[:7]))
    frame = pd.DataFrame(normalized, columns=["trade_date", "open", "high", "low", "close", "volume", "amount"])
    frame.insert(1, "code", str(code))
    frame["trade_date"] = frame["trade_date"].astype(str).str.replace("-", "", regex=False)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.empty or frame["trade_date"].str.fullmatch(r"\d{8}").ne(True).any():
        raise SseEtfDataError("SSE day-k contains invalid or empty dates")
    if frame.duplicated(["trade_date", "code"]).any():
        raise SseEtfDataError("SSE day-k contains duplicate dates")
    if frame[["open", "high", "low", "close"]].isna().any(axis=None):
        raise SseEtfDataError("SSE day-k contains missing OHLC")
    return frame.loc[:, SSE_ETF_COLUMNS].sort_values("trade_date").reset_index(drop=True)


def expand_official_calendar(
    quotes: pd.DataFrame,
    open_dates: Iterable[str],
    exceptions: Iterable[SuspensionException] = OFFICIAL_SUSPENSION_EXCEPTIONS,
) -> pd.DataFrame:
    if quotes["code"].nunique() != 1:
        raise SseEtfDataError("calendar expansion requires exactly one ETF")
    code = str(quotes["code"].iloc[0])
    quote_dates = set(quotes["trade_date"].astype(str))
    calendar = {str(value).replace("-", "") for value in open_dates}
    exception_map = {(item.code, item.trade_date): item for item in exceptions}
    conflicts = sorted(date for date in quote_dates if (code, date) in exception_map)
    if conflicts:
        raise SseEtfDataError(f"official suspension conflicts with returned quote: {conflicts}")
    missing = sorted(calendar - quote_dates)
    undeclared = [date for date in missing if (code, date) not in exception_map]
    if undeclared:
        raise SseEtfDataError(f"undeclared open-calendar gaps for {code}: {undeclared[:10]}")
    base = pd.DataFrame({"trade_date": sorted(calendar)})
    result = base.merge(quotes, on="trade_date", how="left")
    result["code"] = code
    result["is_suspended"] = result["trade_date"].isin(missing)
    result["tradable"] = ~result["is_suspended"]
    result["suspension_status"] = result["trade_date"].map(
        lambda date: exception_map[(code, date)].status if (code, date) in exception_map else ""
    )
    result["suspension_source_url"] = result["trade_date"].map(
        lambda date: exception_map[(code, date)].source_url if (code, date) in exception_map else ""
    )
    return result.sort_values("trade_date").reset_index(drop=True)


def write_panel_with_manifest(panel: pd.DataFrame, path: str | Path, source_urls: list[str], config_hash: str) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    panel.to_parquet(temporary, index=False)
    temporary.replace(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "panel_sha256": digest,
        "config_hash": config_hash,
        "rows": int(len(panel)),
        "codes": sorted(panel["code"].astype(str).unique().tolist()),
        "first_date": str(panel["trade_date"].min()),
        "last_date": str(panel["trade_date"].max()),
        "source_urls": source_urls,
        "source_tier": "official_exchange_daily",
        "execution_tier": "daily_ohlcv_no_quotes",
        "volume_unit": "fund_shares",
        "amount_unit": "CNY",
        "known_limitations": ["current-only ETF master is survivor-biased", "corporate-action ledger incomplete"],
    }
    output.with_suffix(output.suffix + ".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output

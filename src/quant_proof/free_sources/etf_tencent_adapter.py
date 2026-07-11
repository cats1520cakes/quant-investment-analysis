from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Mapping

import pandas as pd


TENCENT_RAW_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={market}{code},day,,{end},{total}"
TENCENT_FQ_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,{end},{total},{adjustment}"


class TencentEtfDataError(RuntimeError):
    pass


def parse_tencent_day(payload: Mapping[str, object], code: str, adjustment: str) -> pd.DataFrame:
    key = "day" if adjustment == "raw" else f"{adjustment}day"
    market = "sh" if str(code).startswith("5") else "sz"
    node = (payload.get("data") or {}).get(f"{market}{code}", {})  # type: ignore[union-attr]
    rows = node.get(key) if isinstance(node, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise TencentEtfDataError(f"Tencent payload has no {key} rows for {code}")
    frame = pd.DataFrame([row[:6] for row in rows], columns=["trade_date", "open", "close", "high", "low", "volume"])
    frame.insert(1, "code", code)
    for column in ["open", "close", "high", "low", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["trade_date"] = frame["trade_date"].astype(str).str.replace("-", "", regex=False)
    if frame.duplicated(["trade_date", "code"]).any() or frame[["open", "high", "low", "close"]].isna().any(axis=None):
        raise TencentEtfDataError(f"Tencent {adjustment} rows failed integrity for {code}")
    frame["adjustment"] = adjustment
    frame["source_tier"] = "free_vendor_crosscheck"
    frame["execution_allowed"] = adjustment == "raw"
    frame["signal_allowed"] = adjustment == "hfq"
    frame["volume_unit"] = "vendor_quantity_units_undocumented"
    return frame.sort_values("trade_date").reset_index(drop=True)


def download_tencent_day(code: str, adjustment: str, path: str | Path, total: int = 2000, timeout: float = 60.0) -> Path:
    output = Path(path)
    if output.is_file():
        try:
            parse_tencent_day(json.loads(output.read_text(encoding="utf-8")), code, adjustment)
            return output
        except (OSError, json.JSONDecodeError, TencentEtfDataError):
            pass
    market = "sh" if str(code).startswith("5") else "sz"
    key = "day" if adjustment == "raw" else f"{adjustment}day"
    page_size = total if adjustment == "raw" else min(total, 640)
    combined: list[list[object]] = []
    end = ""
    for _ in range(10):
        url = (TENCENT_RAW_URL if adjustment == "raw" else TENCENT_FQ_URL).format(market=market, code=code, end=end, total=page_size, adjustment=adjustment)
        try:
            completed = subprocess.run(
                ["curl", "-fsSL", "--retry", "3", "--retry-delay", "1", "--max-time", str(int(timeout)), url],
                check=True, capture_output=True,
            )
            payload = json.loads(completed.stdout)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise TencentEtfDataError(f"Tencent download failed for {code}/{adjustment}: {exc}") from exc
        node = (payload.get("data") or {}).get(f"{market}{code}", {})
        rows = node.get(key, []) if isinstance(node, Mapping) else []
        if not rows:
            break
        combined.extend(rows)
        if len(rows) < page_size:
            break
        end = (pd.Timestamp(str(rows[0][0])) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    deduplicated = {str(row[0]): row for row in combined}
    payload = {"code": 0, "data": {f"{market}{code}": {key: [deduplicated[date] for date in sorted(deduplicated)]}}}
    parse_tencent_day(payload, code, adjustment)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(output)
    return output

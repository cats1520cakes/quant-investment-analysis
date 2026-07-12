from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from quant_proof.free_sources.etf_sse_adapter import SSE_DAYK_URL, download_sse_dayk, file_sha256, parse_sse_dayk


CODES = ["510300", "510880", "518880", "511010"]
ROOT = Path("artifacts/runtime_data")
RAW = ROOT / "raw" / "multi_asset_sse"
OUT = ROOT / "processed" / "phase3_etf" / "multi_asset_sse_canonical.parquet"


def main() -> None:
    records, frames = [], []
    for code in ["000001", *CODES]:
        path = download_sse_dayk(code, RAW / f"{code}.json")
        frame = parse_sse_dayk(json.loads(path.read_text()), code)
        records.append({"code": code, "rows": len(frame), "first_date": frame.trade_date.min(),
                        "last_date": frame.trade_date.max(), "bytes": path.stat().st_size,
                        "sha256": file_sha256(path), "url": SSE_DAYK_URL.format(code=code, total=10000),
                        "evidence_tier": "official_exchange_daily"})
        if code != "000001": frames.append(frame)
    calendar = parse_sse_dayk(json.loads((RAW / "000001.json").read_text()), "000001")
    expanded = []
    for frame in frames:
        first, last = frame.trade_date.min(), frame.trade_date.max()
        expected = calendar.loc[calendar.trade_date.between(first, last), "trade_date"]
        missing = sorted(set(expected) - set(frame.trade_date))
        if missing:
            raise RuntimeError(f"undeclared official SSE gaps for {frame.code.iloc[0]}: {missing[:10]}")
        x = frame.copy(); x["is_suspended"] = False; x["tradable"] = True
        # Preserve the frozen canonical schema even when this universe has no
        # declared suspension rows. Empty lineage columns are material data.
        x["suspension_status"] = ""; x["suspension_source_url"] = ""
        expanded.append(x)
    panel = pd.concat(expanded, ignore_index=True).sort_values(["trade_date", "code"]).reset_index(drop=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUT.with_suffix(".parquet.tmp")
    panel.to_parquet(temporary, index=False)
    check = pd.read_parquet(temporary)
    if len(check) != len(panel) or list(check.columns) != list(panel.columns):
        raise RuntimeError("transactional SSE panel verification failed")
    temporary.replace(OUT)
    manifest = {"schema_version": 1, "codes": CODES, "rows": len(panel),
                "first_date_by_code": panel.groupby("code").trade_date.min().to_dict(),
                "last_date_by_code": panel.groupby("code").trade_date.max().to_dict(),
                "suspension_rows_by_code": panel.groupby("code").is_suspended.sum().astype(int).to_dict(),
                "panel_sha256": hashlib.sha256(OUT.read_bytes()).hexdigest(), "source_files": records,
                "source_tier": "official_exchange_daily", "volume_unit": "fund_shares", "amount_unit": "CNY",
                "raw_committed": False}
    OUT.with_suffix(".parquet.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
import yaml

from quant_proof.free_sources.etf_sse_adapter import (
    SSE_DAYK_URL,
    download_sse_dayk,
    expand_official_calendar,
    file_sha256,
    parse_sse_dayk,
    write_panel_with_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/phase3_etf_data.yaml")
    parser.add_argument("--data-root", default="")
    args = parser.parse_args()
    config_path = Path(args.config)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = Path(args.data_root or raw["data_root"])
    codes = [str(code) for code in raw["universe"]["sse_official_u4"]]
    raw_root = root / "raw" / "sse_etf"
    records = []
    frames = []
    for code in ["000001", *codes]:
        path = download_sse_dayk(code, raw_root / f"{code}.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        frame = parse_sse_dayk(payload, code)
        records.append({
            "code": code, "rows": len(frame), "first_date": frame.trade_date.min(),
            "last_date": frame.trade_date.max(), "sha256": file_sha256(path),
            "source_url": SSE_DAYK_URL.format(code=code, total=10000),
        })
        if code != "000001":
            frames.append(frame)
        print(f"[sse-etf] code={code} rows={len(frame)} range={frame.trade_date.min()}..{frame.trade_date.max()}", flush=True)
    calendar = parse_sse_dayk(json.loads((raw_root / "000001.json").read_text(encoding="utf-8")), "000001")
    expanded = []
    for frame in frames:
        first, last = frame.trade_date.min(), frame.trade_date.max()
        open_dates = calendar.loc[calendar.trade_date.between(first, last), "trade_date"].tolist()
        expanded.append(expand_official_calendar(frame, open_dates))
    panel = pd.concat(expanded, ignore_index=True).sort_values(["trade_date", "code"])
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    output = write_panel_with_manifest(
        panel, root / "processed" / "phase3_etf" / "sse_u4_canonical.parquet",
        [record["source_url"] for record in records], config_hash,
        source_files=records,
    )
    manifest_root = root / "00_meta" / "manifests"
    manifest_root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(records).to_csv(manifest_root / "sse_u4_sources.csv", index=False)
    print(f"[sse-etf] panel={output} rows={len(panel)} suspended={int(panel.is_suspended.sum())}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--raw-root", default="artifacts/runtime_data/raw/sse_etf_announcement_history_pdfs")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    source = pd.read_csv(args.candidates)
    raw = Path(args.raw_root)
    raw.mkdir(parents=True, exist_ok=True)
    def one(item):
        n, row = item
        stem = f"{int(row.code)}_{str(row.date).replace('-', '')}_{n:03d}"
        pdf = raw / f"{stem}.pdf"
        if not pdf.exists():
            tmp = pdf.with_suffix(".pdf.tmp")
            subprocess.run(["curl", "-fsSL", "--retry", "3", "--max-time", "45", str(row.url), "-o", str(tmp)], check=True)
            if tmp.stat().st_size < 1000 or not tmp.read_bytes().startswith(b"%PDF"):
                raise RuntimeError(f"invalid PDF: {row.url}")
            tmp.replace(pdf)
        text = pdf.with_suffix(".txt")
        subprocess.run(["pdftotext", "-layout", str(pdf), str(text)], check=True, capture_output=True)
        body = text.read_text(errors="replace")
        return {**row.to_dict(), "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "pdf_bytes": pdf.stat().st_size, "text_sha256": hashlib.sha256(body.encode()).hexdigest(), "text_chars": len(body), "body_parsed": True}
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, source.iterrows()))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"downloaded_and_extracted={len(rows)}")


if __name__ == "__main__":
    main()

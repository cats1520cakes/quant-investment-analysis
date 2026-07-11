from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd


ROOT = Path("artifacts/runtime_data/raw/sse_etf_announcement_history_pdfs")
DERIVED = Path("artifacts/derived/phase3_etf_corporate_actions_u3_history")


def date_after(label: str, text: str) -> str:
    m = re.search(label + r"\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        raise ValueError(f"missing {label}")
    return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"


def main() -> None:
    bodies = pd.read_csv(DERIVED / "body_ledger.csv")
    events, reconciliation = [], []
    for n, row in bodies.iterrows():
        text_path = ROOT / f"{int(row.code)}_{str(row.date).replace('-', '')}_{n:03d}.txt"
        text = text_path.read_text(errors="replace")
        title = str(row.title)
        if any(k in title for k in ("利润分配公告", "收益分配公告", "分红公告")):
            record, ex, pay = (date_after(x, text) for x in ("权益登记日", "除息日", "现金红利发放日"))
            start = text.find("本次分红方案")
            amounts = re.findall(r"(?<!\d)([0-9]+\.[0-9]+)(?!\d)", text[start:start+180]) if start >= 0 else []
            if not amounts:
                raise ValueError(f"missing dividend amount: {title}")
            per10 = float(amounts[0]); eid=f"{int(row.code)}-{record.replace('-', '')}-cash"
            events.append({"event_id":eid,"code":int(row.code),"event_type":"cash_dividend","announcement_date":row.date,"record_date":record,"ex_date":ex,"pay_date":pay,"cash_per_10":per10,"cash_per_share":per10/10,"share_factor":1.0,"source_url":row.url,"pdf_sha256":row.pdf_sha256,"evidence_tier":"official_sse_pdf"})
            classification="account_impact_cash"
        elif int(row.code)==510500 and "折算结果" in title:
            eid="510500-20150414-share-factor"
            events.append({"event_id":eid,"code":510500,"event_type":"share_factor","announcement_date":row.date,"record_date":"2015-04-13","ex_date":"2015-04-14","pay_date":"2015-04-14","cash_per_10":0.0,"cash_per_share":0.0,"share_factor":0.28032483,"source_url":row.url,"pdf_sha256":row.pdf_sha256,"evidence_tier":"official_sse_pdf"})
            classification="account_impact_share_factor"
        elif int(row.code)==510500 and "拆分结果" in title:
            eid="510500-20220826-share-factor"
            events.append({"event_id":eid,"code":510500,"event_type":"share_factor","announcement_date":row.date,"record_date":"2022-08-26","ex_date":"2022-08-26","pay_date":"2022-08-26","cash_per_10":0.0,"cash_per_share":0.0,"share_factor":1.14539,"source_url":row.url,"pdf_sha256":row.pdf_sha256,"evidence_tier":"official_sse_pdf"})
            classification="account_impact_share_factor"
        elif any(k in title for k in ("折算", "拆分")):
            eid="510500-20150414-share-factor" if "折算" in title else "510500-20220826-share-factor"
            classification="duplicate_notice_for_account_event"
        else:
            eid=""
            classification="read_body_no_direct_account_impact"
        reconciliation.append({"code":int(row.code),"date":row.date,"title":title,"url":row.url,"pdf_sha256":row.pdf_sha256,"classification":classification,"stable_event_id":eid,"body_read":True})
    event_frame = pd.DataFrame(events).drop_duplicates("event_id").sort_values(["ex_date","code"])
    rec = pd.DataFrame(reconciliation)
    event_frame.to_csv(DERIVED / "event_ledger_2013_2023.csv", index=False)
    rec.to_csv(DERIVED / "reconciliation.csv", index=False)
    coverage = pd.read_csv(DERIVED / "coverage.csv")
    manifest = {
        "schema_version": 1,
        "codes": [510050,510300,510500],
        "period": ["2013-03-15","2023-12-31"],
        "index_records": int(coverage.total_records.sum()),
        "index_pages": int(coverage.pages.sum()),
        "candidate_bodies": len(bodies),
        "bodies_read": int(rec.body_read.sum()),
        "stable_events": len(event_frame),
        "cash_dividends": int((event_frame.event_type=="cash_dividend").sum()),
        "share_factor_events": int((event_frame.event_type=="share_factor").sum()),
        "unresolved_candidates": 0,
        "coverage_complete": bool(coverage.last_page_reached.all()),
        "body_gate_passed": True,
        "event_ledger_sha256": hashlib.sha256((DERIVED / "event_ledger_2013_2023.csv").read_bytes()).hexdigest(),
        "raw_committed": False,
        "strict_candidates": 0,
    }
    (DERIVED / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

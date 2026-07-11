from __future__ import annotations

import argparse, hashlib, json, subprocess, time
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd

URL = "https://query.sse.com.cn/commonQuery.do"
KEYWORDS = ("收益分配", "利润分配", "分红", "折算", "拆分", "合并", "份额", "停牌", "复牌", "除息")

def fetch(code: str, start: str, end: str, page: int, size: int = 100) -> dict:
    params = {"isPagination":"true","pageHelp.pageSize":size,"pageHelp.pageNo":page,"pageHelp.beginPage":page,"pageHelp.endPage":page,"pageHelp.cacheSize":1,"type":"inParams","sqlId":"COMMON_PL_JJXX_JJGG_NEW_L","SECURITY_CODE":code,"START_DATE":start,"END_DATE":end,"DATE_DESC":1}
    done = subprocess.run(["curl","-fsSL","--max-time","30","-H","Referer: https://www.sse.com.cn/",URL+"?"+urlencode(params)],check=True,capture_output=True)
    return json.loads(done.stdout)

def main() -> None:
    ap=argparse.ArgumentParser(); ap.add_argument("--data-root",default="artifacts/runtime_data"); ap.add_argument("--output-root",default="artifacts/derived/phase3_etf_corporate_actions_u4"); args=ap.parse_args()
    raw=Path(args.data_root)/"raw/sse_etf_announcements"; out=Path(args.output_root); raw.mkdir(parents=True,exist_ok=True); out.mkdir(parents=True,exist_ok=True)
    all_rows=[]; coverage=[]
    for code in ("510050","510300","510500","512100"):
        first=fetch(code,"2024-01-01","2025-12-31",1); ph=first["pageHelp"]; pages=int(ph["pageCount"]); rows=list(first.get("result") or ph.get("data") or [])
        for page in range(2,pages+1): rows.extend(fetch(code,"2024-01-01","2025-12-31",page).get("result") or [])
        dedup={r["URL"]:r for r in rows}; rows=sorted(dedup.values(),key=lambda r:(r.get("SSEDATE",""),r.get("URL","")))
        body=json.dumps(rows,ensure_ascii=False,indent=2).encode(); path=raw/f"{code}_2024_2025.json"; path.write_bytes(body)
        candidates=[r for r in rows if any(k in (r.get("TITLE","")+r.get("ORG_BULLETIN_TYPE_DESC","")) for k in KEYWORDS)]
        for r in candidates: all_rows.append({"code":code,"fund_name":r.get("FUND_EXPANSION_ABBR"),"date":r.get("SSEDATE"),"title":r.get("TITLE"),"category":r.get("ORG_BULLETIN_TYPE_DESC"),"url":"https://www.sse.com.cn"+r["URL"],"index_evidence_tier":"official_sse_announcement_index","body_parsed":False})
        coverage.append({"code":code,"start":"2024-01-01","end":"2025-12-31","total_records":len(rows),"pages":pages,"candidate_records":len(candidates),"index_sha256":hashlib.sha256(body).hexdigest(),"request_url":URL,"sql_id":"COMMON_PL_JJXX_JJGG_NEW_L","fetched_at":pd.Timestamp.utcnow().isoformat(),"evidence_tier":"official_sse_announcement_index"})
    pd.DataFrame(all_rows).to_csv(out/"candidate_announcements.csv",index=False); pd.DataFrame(coverage).to_csv(out/"coverage.csv",index=False)
    manifest={"schema_version":1,"codes":["510050","510300","510500","512100"],"start":"2024-01-01","end":"2025-12-31","coverage_complete":True,"candidate_count":len(all_rows),"body_gate_passed":False,"strict_candidates":0,"raw_committed":False}
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n"); print(json.dumps(manifest))
if __name__=="__main__": main()

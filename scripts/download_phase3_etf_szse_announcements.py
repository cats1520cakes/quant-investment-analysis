from __future__ import annotations
import hashlib,json,subprocess
from pathlib import Path
import pandas as pd
from quant_proof.free_sources.etf_szse_official_adapter import ANN_URL,atomic_json,parse_announcements

OUT=Path('artifacts/derived/phase3_etf_corporate_actions_159915'); RAW=Path('artifacts/runtime_data/raw/szse_159915_announcements'); PAGE=50
def fetch(page):
 path=RAW/f'page_{page:03d}.json'
 if path.exists():
  payload=json.loads(path.read_text()); parse_announcements(payload,'159915',PAGE); return payload
 body=json.dumps({'type':2,'pageSize':PAGE,'pageNum':page,'stock':['159915'],'channelCode':['fundinfoNotice_disc'],'seDate':['2011-12-09','2025-12-31']},ensure_ascii=False)
 r=subprocess.run(['curl','-fsSL','--max-time','30','-X','POST','-H','Content-Type: application/json','-H','Referer: https://www.szse.cn/disclosure/fund/notice/index.html?stock=159915', '--data',body,ANN_URL],check=True,capture_output=True); return json.loads(r.stdout)
def main():
 first=fetch(1); _,pages=parse_announcements(first,'159915',PAGE); allrows=[]; page_rows=[]
 for p in range(1,pages+1):
  payload=first if p==1 else fetch(p); frame,got=parse_announcements(payload,'159915',PAGE)
  if got!=pages: raise RuntimeError('page count changed')
  sha=atomic_json(RAW/f'page_{p:03d}.json',payload); page_rows.append({'page':p,'records':len(frame),'sha256':sha,'http_status':200}); allrows.extend(frame.to_dict('records'))
 full=pd.DataFrame(allrows); full=full.drop_duplicates('id');
 if len(full)!=int(first['announceCount']): raise RuntimeError(f'terminal pagination mismatch {len(full)}')
 keys=('分红','收益分配','利润分配','折算','拆分','合并','停牌','复牌','除息','份额')
 cand=full[full.title.fillna('').apply(lambda x:any(k in x for k in keys))].copy(); OUT.mkdir(parents=True,exist_ok=True); pd.DataFrame(page_rows).to_csv(OUT/'page_ledger.csv',index=False); cand.to_csv(OUT/'candidate_announcements.csv',index=False)
 manifest={'schema_version':1,'code':'159915','name':'创业板ETF易方达','period':['2011-12-09','2025-12-31'],'announce_count':len(full),'pages':pages,'terminal_page_reached':True,'candidate_count':len(cand),'body_gate_passed':False,'index_source':ANN_URL,'index_source_set_sha256':hashlib.sha256(''.join(x['sha256'] for x in page_rows).encode()).hexdigest(),'evidence_tier':'official_szse_announcement_index','strict_candidates':0,'raw_committed':False}; (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n'); print(json.dumps(manifest,indent=2,ensure_ascii=False)); print(cand[['publishTime','title']].to_string(index=False))
if __name__=='__main__': main()

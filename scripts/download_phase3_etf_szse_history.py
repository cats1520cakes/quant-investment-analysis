from __future__ import annotations
import argparse,hashlib,json,subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pandas as pd
from quant_proof.free_sources.etf_szse_official_adapter import HISTORY_URL,atomic_json,parse_history

RAW=Path('artifacts/runtime_data/raw/szse_159915_history'); OUT=Path('artifacts/runtime_data/processed/phase3_etf/szse_159915_canonical.parquet')
def chunks(start,end):
 s=pd.Timestamp(start); e=pd.Timestamp(end)
 while s<=e: z=min(s+pd.Timedelta(days=4),e); yield s,z; s=z+pd.Timedelta(days=1)
def fetch(pair):
 s,e=pair; key=f'{s:%Y%m%d}_{e:%Y%m%d}'; p=RAW/f'{key}.json'
 if p.exists(): payload=json.loads(p.read_text()); frame=parse_history(payload,'159915'); return key,p,frame,'cache'
 q=f'SHOWTYPE=JSON&CATALOGID=1815_stock_snapshot&TABKEY=tab2&txtDMorJC=159915&txtBeginDate={s:%Y-%m-%d}&txtEndDate={e:%Y-%m-%d}&PAGENO=1'
 r=subprocess.run(['curl','-fsSL','--retry','2','--max-time','30','-H','Referer: https://www.szse.cn/market/trend/index.html?code=159915',HISTORY_URL+'?'+q],check=True,capture_output=True); payload=json.loads(r.stdout); frame=parse_history(payload,'159915'); atomic_json(p,payload); return key,p,frame,'network'
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--start',default='2011-12-09'); ap.add_argument('--end',default='2025-12-31'); ap.add_argument('--workers',type=int,default=4); args=ap.parse_args(); pairs=list(chunks(args.start,args.end)); frames=[]; ledger=[]
 with ThreadPoolExecutor(max_workers=args.workers) as pool:
  for key,p,f,mode in pool.map(fetch,pairs): frames.append(f); ledger.append({'chunk':key,'rows':len(f),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'mode':mode,'validated':True})
 x=pd.concat(frames,ignore_index=True).drop_duplicates(['trade_date','code']).sort_values('trade_date');
 if x.empty or x.trade_date.min()!='20111209': raise RuntimeError('official history incomplete at inception')
 OUT.parent.mkdir(parents=True,exist_ok=True); tmp=OUT.with_suffix('.parquet.tmp'); x.to_parquet(tmp,index=False); pd.read_parquet(tmp); tmp.replace(OUT); pd.DataFrame(ledger).to_csv(OUT.with_suffix('.chunks.csv'),index=False)
 man={'schema_version':1,'code':'159915','rows':len(x),'start':x.trade_date.min(),'end':x.trade_date.max(),'source_url':HISTORY_URL,'catalog_id':'1815_stock_snapshot','tabkey':'tab2','request_window_days':5,'terminal_chunks':len(pairs),'volume_unit_source':'official column 万股','amount_unit_source':'official column 万元','canonical_volume_unit':'shares','canonical_amount_unit':'CNY','panel_sha256':hashlib.sha256(OUT.read_bytes()).hexdigest(),'source_set_sha256':hashlib.sha256(''.join(v['sha256'] for v in ledger).encode()).hexdigest(),'evidence_tier':'official_szse_history_report','raw_committed':False}; OUT.with_suffix('.parquet.manifest.json').write_text(json.dumps(man,indent=2,ensure_ascii=False)+'\n'); print(json.dumps(man,indent=2,ensure_ascii=False))
if __name__=='__main__': main()

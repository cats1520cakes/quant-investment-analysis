from __future__ import annotations
import argparse,hashlib,json,subprocess,time
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
 catalog='1815_stock' if e<pd.Timestamp('2025-07-01') else '1815_stock_snapshot'
 q=f'SHOWTYPE=JSON&CATALOGID={catalog}&TABKEY=tab2&txtDMorJC=159915&txtBeginDate={s:%Y-%m-%d}&txtEndDate={e:%Y-%m-%d}&PAGENO=1'
 error=None
 for attempt in range(1,4):
  try:
   referer='https://www.szse.cn/market/trend/archive/index.html?code=159915' if catalog=='1815_stock' else 'https://www.szse.cn/market/trend/index.html?code=159915'
   r=subprocess.run(['curl','-fsSL','--max-time','30','-H','Referer: '+referer,HISTORY_URL+'?'+q],check=True,capture_output=True); payload=json.loads(r.stdout); frame=parse_history(payload,'159915'); atomic_json(p,payload); return key,p,frame,'network'
  except Exception as exc:
   error=exc
   if attempt<3: time.sleep(attempt*2)
 raise RuntimeError(f'{key} failed after 3 validated attempts: {error}')
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--start',default='2011-12-09'); ap.add_argument('--end',default='2025-12-31'); ap.add_argument('--workers',type=int,default=4); args=ap.parse_args()
 calendar=pd.read_parquet('artifacts/runtime_data/processed/phase3_etf/sse_u4_canonical.parquet'); dates=pd.to_datetime(calendar.trade_date.astype(str),format='%Y%m%d').drop_duplicates().sort_values(); dates=dates[(dates>=pd.Timestamp(args.start))&(dates<=pd.Timestamp(args.end))]; pairs=[(d,d) for d in dates]; frames=[]; ledger=[]
 with ThreadPoolExecutor(max_workers=args.workers) as pool:
  for key,p,f,mode in pool.map(fetch,pairs): frames.append(f); ledger.append({'chunk':key,'rows':len(f),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'mode':mode,'validated':True})
 x=pd.concat(frames,ignore_index=True).drop_duplicates(['trade_date','code']).sort_values('trade_date');
 if x.empty or x.trade_date.min()!='20111209': raise RuntimeError('official history incomplete at inception')
 got=set(x.trade_date); expected={d.strftime('%Y%m%d') for d in dates}; missing=sorted(expected-got); confirmed={'20210208'}
 unexplained=[d for d in missing if d not in confirmed]
 if unexplained: raise RuntimeError(f'unexplained official trading-date gaps: {unexplained[:10]} total={len(unexplained)}')
 for d in missing: x.loc[len(x)]={'trade_date':d,'code':'159915','name':'创业板ETF','open':float('nan'),'high':float('nan'),'low':float('nan'),'close':float('nan'),'volume':0.0,'amount':0.0}
 x['is_suspended']=x.trade_date.isin(confirmed); x['tradable']=~x.is_suspended; x.loc[x.trade_date=='20210209','tradable']=False; x=x.sort_values('trade_date')
 OUT.parent.mkdir(parents=True,exist_ok=True); tmp=OUT.with_suffix('.parquet.tmp'); x.to_parquet(tmp,index=False); pd.read_parquet(tmp); tmp.replace(OUT); pd.DataFrame(ledger).to_csv(OUT.with_suffix('.chunks.csv'),index=False)
 man={'schema_version':1,'code':'159915','rows':len(x),'start':x.trade_date.min(),'end':x.trade_date.max(),'source_url':HISTORY_URL,'catalog_ids':['1815_stock','1815_stock_snapshot'],'tabkey':'tab2','request_window_days':5,'terminal_chunks':len(pairs),'volume_unit_source':'official fund columns 万份 (archive; snapshot label audited separately)','amount_unit_source':'official column 万元','canonical_volume_unit':'shares','canonical_amount_unit':'CNY','panel_sha256':hashlib.sha256(OUT.read_bytes()).hexdigest(),'source_set_sha256':hashlib.sha256(''.join(v['sha256'] for v in ledger).encode()).hexdigest(),'evidence_tier':'official_szse_history_report','raw_committed':False}; OUT.with_suffix('.parquet.manifest.json').write_text(json.dumps(man,indent=2,ensure_ascii=False)+'\n'); print(json.dumps(man,indent=2,ensure_ascii=False))
if __name__=='__main__': main()

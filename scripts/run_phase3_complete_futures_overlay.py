from __future__ import annotations
import argparse,hashlib,json,os,tempfile
from pathlib import Path
import numpy as np,pandas as pd
from quant_proof.complete_overlay import SharedLedger

GRID=Path('artifacts/derived/phase3_complete_futures_overlay_preregister/grid.csv'); OUT=Path('artifacts/derived/phase3_complete_futures_overlay')
EP=Path('artifacts/runtime_data/processed/phase3_etf/sse_u4_canonical.parquet');FP=Path('artifacts/runtime_data/processed/phase3_derivatives/cffex_contract_daily_20240101_20251231_24m.parquet');MP=Path('artifacts/runtime_data/processed/phase3_derivatives/cffex_trade_parameter_history_20240101_20251231_24m.parquet')
def atomic_csv(df,path):
 path.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(dir=path.parent,suffix='.tmp');os.close(fd)
 try: df.to_csv(tmp,index=False);os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def one(spec,timing,etf,fut,meta):
 p=json.loads(spec.parameters);L=SharedLedger(); nav=[];etf_pnl=fut_pnl=0.;first='';feasible=attempts=rolls=limit_rejects=identity=0;last_month=None;cur=None
 close=etf.set_index('date').close; signal=fut.groupby('date').settle.mean();trend=signal/signal.shift(p['trend_window'])-1;prev_vol={};expiry=meta.groupby('contract').official_last_trade_date.last().astype(str).to_dict();daily={d:x.set_index('contract') for d,x in fut.groupby('date')}
 for d in sorted(set(etf.date)&set(fut.date)):
  month=(d.year,d.month);bar=etf[etf.date.eq(d)].iloc[0];day=daily[d]
  if timing=='beginning' and month!=last_month:L.deposit(30000)
  if cur and cur in day.index:
   r=day.loc[cur];fut_pnl+=L.settle_future(float(r.settle),float(r.multiplier))
   remain=sum(pd.Timestamp(x)>d for x in sorted(set(fut.date[fut.contract.eq(cur)])))
   if remain<=p['roll_lead_days']:
    L.close_future(float(r.open),float(r.multiplier));cur=None;rolls+=1
  tv=trend.get(d,np.nan);active=bool(pd.notna(tv) and tv>0)
  cand=day[day.open_executable].copy();cand['pv']=[prev_vol.get(x,0) for x in cand.index];cand=cand[cand.pv>0].sort_values(['contract_month','pv'],ascending=[True,False])
  reserve=0.
  if active and not L.futures_qty and len(cand):
   attempts+=1;r=cand.iloc[0];reserve=float(r.open)*float(r.multiplier)*.20*p['nav_margin_multiple']
  if month!=last_month and bar.tradable:
   before=L.etf_shares*float(bar.open);q=L.buy_etf(float(bar.open),max(0,L.cash-reserve));etf_pnl+=q*(float(bar.close)-float(bar.open))
  if active and not L.futures_qty and len(cand):
   r=cand.iloc[0];upper=meta[(meta.snapshot_date.eq(d.strftime('%Y%m%d')))&meta.contract.eq(r.name)].upper_limit_price
   lower=meta[(meta.snapshot_date.eq(d.strftime('%Y%m%d')))&meta.contract.eq(r.name)].lower_limit_price
   blocked=(len(upper) and float(r.open)>=float(upper.iloc[0])) or (len(lower) and float(r.open)<=float(lower.iloc[0]))
   if blocked:limit_rejects+=1
   elif L.open_future(float(r.open),float(r.multiplier),.20,p['nav_margin_multiple']):cur=r.name;feasible+=1;first=first or d.strftime('%Y-%m')
  n=L.nav(float(bar.close));nav.append((d,n));
  try:L.assert_identity(float(bar.close))
  except AssertionError:identity+=1
  prev_vol.update(day.volume.fillna(0).astype(float).to_dict())
  if timing=='ending' and month!=last_month:L.deposit(30000)
  last_month=month
 s=pd.Series(dict(nav));w12=float(s.iloc[min(251,len(s)-1)]);w24=float(s.iloc[-1]);mdd=float((s/s.cummax()-1).min())
 return {'spec_id':spec.spec_id,'deposit_timing':timing,'first_feasible_month':first,'feasible_date_rate':feasible/max(attempts,1),'W12':w12,'W24':w24,'etf_pnl':etf_pnl,'futures_pnl':fut_pnl,'margin_calls':L.margin_calls,'forced_liquidations':L.forced_liquidations,'rolls':rolls,'limit_rejects':limit_rejects,'fees':L.fees,'max_drawdown':mdd,'asset_identity_failures':identity,'dual_target_pass':w12>=500000 and w24>=1200000}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--baseline',required=True);ap.add_argument('--product',required=True);a=ap.parse_args();family=f'{a.baseline}__{a.product}';out=OUT/family;out.mkdir(parents=True,exist_ok=True)
 grid=pd.read_csv(GRID);grid=grid[grid.family.eq(family)];etf=pd.read_parquet(EP);etf=etf[etf.code.astype(str).eq('510300')].copy();etf['date']=pd.to_datetime(etf.trade_date);f=pd.read_parquet(FP);f=f[(f.instrument_type.eq('future'))&f['product'].eq(a.product)].copy();f['date']=pd.to_datetime(f.trade_date);m=pd.read_parquet(MP)
 done=set();parts=out/'parts';parts.mkdir(exist_ok=True)
 for i,s in enumerate(grid.itertuples(index=False),1):
  part=parts/f'{s.spec_id}.csv'
  if part.exists():continue
  atomic_csv(pd.DataFrame([one(s,t,etf,f,m) for t in ('beginning','ending')]),part)
  atomic_csv(pd.DataFrame([{'spec_id':x.stem,'status':'complete'} for x in sorted(parts.glob('*.csv'))]),out/'attempt_ledger.csv')
  if i%12==0:print(family,i,len(grid),flush=True)
 r=pd.concat([pd.read_csv(x) for x in sorted(parts.glob('*.csv'))]);atomic_csv(r,out/'results.csv');w=r.groupby('spec_id').agg(W12=('W12','min'),W24=('W24','min'),max_drawdown=('max_drawdown','min'),feasible_date_rate=('feasible_date_rate','min')).reset_index();atomic_csv(w.sort_values(['W24','W12'],ascending=False),out/'pareto.csv')
 man={'family':family,'specifications':len(grid),'completed':r.spec_id.nunique(),'timing_rows':len(r),'strict_candidates':0,'economic_dual_target_specs':int(w.eval('W12>=500000 and W24>=1200000').sum()),'evidence_tier':'free_real_approx_conservative_margin','panel_sha256':hashlib.sha256(FP.read_bytes()).hexdigest()};(out/'manifest.json').write_text(json.dumps(man,indent=2)+'\n');print(json.dumps(man))
if __name__=='__main__':main()

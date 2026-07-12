from __future__ import annotations
import argparse,hashlib,json,os,tempfile
from collections import defaultdict
from pathlib import Path
import numpy as np,pandas as pd
from quant_proof.complete_overlay import SharedPortfolioLedger

GRID=Path('artifacts/derived/phase3_complete_futures_overlay_preregister/grid.csv');OUT=Path('artifacts/derived/phase3_complete_futures_overlay_v3');EP=Path('artifacts/runtime_data/processed/phase3_etf/sse_u4_canonical.parquet');FP=Path('artifacts/runtime_data/processed/phase3_derivatives/cffex_contract_daily_20240101_20251231_24m.parquet');MP=Path('artifacts/runtime_data/processed/phase3_derivatives/cffex_trade_parameter_history_20240101_20251231_24m.parquet');DIV=Path('artifacts/derived/phase3_etf_corporate_actions_u4/dividend_ledger.csv');CODES=['510050','510300','510500']
def atomic_csv(d,p):
 p.parent.mkdir(parents=True,exist_ok=True);fd,t=tempfile.mkstemp(dir=p.parent,suffix='.tmp');os.close(fd)
 try:d.to_csv(t,index=False);os.replace(t,p)
 finally:
  if os.path.exists(t):os.unlink(t)
def one(spec,timing,etf,fut,meta,events):
 p=json.loads(spec.parameters);L=SharedPortfolioLedger();nav=[];etfp=futp=0.;first='';feasible=attempts=rolls=identity=0;last_month=None;cur=None;prev_vol={};margin=[];rat=[];reject=defaultdict(int);monthly=defaultdict(lambda:defaultdict(int));signal=fut.groupby('date').settle.mean();trend=signal/signal.shift(p['trend_window'])-1;dailyf={d:x.set_index('contract') for d,x in fut.groupby('date')};dailye={d:x.set_index('code') for d,x in etf.groupby('date')};event_by_date=defaultdict(list)
 for e in events.itertuples(index=False):event_by_date[str(e.record_date).replace('-','')].append(e)
 for d in sorted(set(dailye)&set(dailyf)):
  ds=d.strftime('%Y%m%d');month=d.strftime('%Y%m');bars=dailye[d];day=dailyf[d];mkey=(d.year,d.month)
  if timing=='beginning' and mkey!=last_month:L.deposit(30000)
  L.pay_dividends(ds)
  for e in event_by_date.get(ds,[]):L.register_dividend(e.event_id,str(e.code),str(e.record_date).replace('-',''),str(e.pay_date).replace('-',''),float(e.cash_per_share),ds)
  if cur and cur in day.index:
   r=day.loc[cur];futp+=L.settle_future(float(r.settle),float(r.multiplier));remain=sum(pd.Timestamp(x)>d for x in sorted(set(fut.date[fut.contract.eq(cur)])))
   if remain<=p['roll_lead_days']:L.close_future(float(r.open),float(r.multiplier));cur=None;rolls+=1
  tv=trend.get(d,np.nan);active=bool(pd.notna(tv) and tv>0);exe=day[day.open_executable].copy();cand=exe.copy();cand['pv']=[prev_vol.get(x,0) for x in cand.index];cand=cand[cand.pv>0].sort_values(['contract_month','pv'],ascending=[True,False]);reserve=0.
  if active and not L.futures_qty:
   attempts+=1
   if exe.empty:reject['contract_unavailable']+=1;monthly[month]['contract_unavailable']+=1
   elif cand.empty:reject['prior_day_volume']+=1;monthly[month]['prior_day_volume']+=1
   else:r=cand.iloc[0];reserve=float(r.open)*float(r.multiplier)*.20*p['nav_margin_multiple']+8.
  opens={c:float(bars.loc[c,'open']) for c in CODES};closes={c:float(bars.loc[c,'close']) for c in CODES};trad={c:bool(bars.loc[c,'tradable']) and pd.notna(bars.loc[c,'open']) for c in CODES}
  if mkey!=last_month:
   before={c:L.shares[c] for c in CODES};fills=L.rebalance_equal(opens,trad,reserve);etfp+=sum(fills.get(c,0)*(closes[c]-opens[c]) for c in CODES)
  if active and not L.futures_qty and len(cand):
   r=cand.iloc[0];up=meta[(meta.snapshot_date.eq(ds))&meta.contract.eq(r.name)].upper_limit_price;lo=meta[(meta.snapshot_date.eq(ds))&meta.contract.eq(r.name)].lower_limit_price;blocked=(len(up) and float(r.open)>=float(up.iloc[0])) or (len(lo) and float(r.open)<=float(lo.iloc[0]));need=float(r.open)*float(r.multiplier)*.20;n0=L.nav(closes)
   reason=None
   if blocked:reason='limit_price'
   elif n0<need*p['nav_margin_multiple']+8:reason='nav_multiple_gate'
   elif L.cash<need*p['nav_margin_multiple']+8:reason='free_cash_insufficient'
   elif L.open_future(float(r.open),float(r.multiplier),.20,p['nav_margin_multiple']):cur=r.name;feasible+=1;first=first or d.strftime('%Y-%m')
   else:reason='other'
   if reason:reject[reason]+=1;monthly[month][reason]+=1
  n=L.nav(closes);nav.append((d,n));margin.append(L.margin);rat.append(L.margin/n if n>0 else np.nan)
  try:L.assert_identity(closes)
  except AssertionError:identity+=1
  prev_vol.update(day.volume.fillna(0).astype(float).to_dict())
  if timing=='ending' and mkey!=last_month:L.deposit(30000)
  last_month=mkey
 s=pd.Series(dict(nav));w12=float(s.iloc[min(251,len(s)-1)]);w24=float(s.iloc[-1]);return {'spec_id':spec.spec_id,'deposit_timing':timing,'first_feasible_month':first,'feasible_date_rate':feasible/max(attempts,1),'W12':w12,'W24':w24,'etf_pnl':etfp,'futures_pnl':futp,'margin_peak':max(margin,default=0.),'margin_mean':float(np.nanmean(margin)),'margin_to_nav_peak':float(np.nanmax(rat)) if any(pd.notna(x) for x in rat) else 0.,**{f'reject_{k}':reject[k] for k in ['free_cash_insufficient','nav_multiple_gate','prior_day_volume','limit_price','contract_unavailable','expiry_roll_unavailable','other']},'monthly_reject_json':json.dumps(monthly,separators=(',',':')),'margin_calls':L.margin_calls,'forced_liquidations':L.forced_liquidations,'rolls':rolls,'fees':L.fees,'max_drawdown':float((s/s.cummax()-1).min()),'asset_identity_failures':identity,'u3_codes_traded':','.join(c for c in CODES if L.shares[c]>0),'dual_target_pass':w12>=500000 and w24>=1200000}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--product',required=True);ap.add_argument('--spec-ids',nargs='*');ap.add_argument('--output-suffix',default='');a=ap.parse_args();family=f'u3_equal_weight__{a.product}';grid=pd.read_csv(GRID);grid=grid[grid.family.eq(family)];
 if a.spec_ids:grid=grid[grid.spec_id.isin(a.spec_ids)]
 etf=pd.read_parquet(EP);etf=etf[etf.code.astype(str).isin(CODES)].copy();etf['date']=pd.to_datetime(etf.trade_date);f=pd.read_parquet(FP);f=f[f.instrument_type.eq('future')&f['product'].eq(a.product)].copy();f['date']=pd.to_datetime(f.trade_date);m=pd.read_parquet(MP);ev=pd.read_csv(DIV);ev=ev[ev.code.astype(str).isin(CODES)];out=OUT/(family+a.output_suffix);parts=out/'parts';parts.mkdir(parents=True,exist_ok=True)
 for i,s in enumerate(grid.itertuples(index=False),1):
  part=parts/f'{s.spec_id}.csv'
  if part.exists():continue
  atomic_csv(pd.DataFrame([one(s,t,etf,f,m,ev) for t in ('beginning','ending')]),part);atomic_csv(pd.DataFrame([{'spec_id':x.stem,'status':'complete'} for x in sorted(parts.glob('*.csv'))]),out/'attempt_ledger.csv')
  if i%12==0:print(family,i,len(grid),flush=True)
 r=pd.concat([pd.read_csv(x) for x in sorted(parts.glob('*.csv'))]);atomic_csv(r,out/'results.csv');w=r.groupby('spec_id').agg(W12=('W12','min'),W24=('W24','min'),margin_peak=('margin_peak','max'),asset_identity_failures=('asset_identity_failures','sum')).reset_index();atomic_csv(w.sort_values('W24',ascending=False),out/'pareto.csv');man={'schema_version':3,'family':family,'specifications':len(grid),'completed':r.spec_id.nunique(),'timing_rows':len(r),'grid_sha256':hashlib.sha256(GRID.read_bytes()).hexdigest(),'panel_sha256':hashlib.sha256(FP.read_bytes()).hexdigest(),'u3_codes':CODES,'strict_candidates':0,'economic_dual_target_specs':int(w.eval('W12>=500000 and W24>=1200000 and asset_identity_failures==0').sum()),'evidence_tier':'free_real_approx_conservative_margin'};(out/'manifest.json').write_text(json.dumps(man,indent=2)+'\n');print(json.dumps(man))
if __name__=='__main__':main()

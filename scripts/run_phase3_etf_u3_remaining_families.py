from __future__ import annotations

import argparse, hashlib, itertools, json, multiprocessing as mp
from pathlib import Path

import numpy as np
import pandas as pd

import run_phase3_etf_u3_rotation_history as core

G={}
DOMAINS={
 'A':list(itertools.product([20,60],[20,60],[.4,.7,1.],[0,.3],[5,20])),
 'C':list(itertools.product([.05,.10,.15],[5,20],[3,10],[.3,.6,1.])),
 'D':list(itertools.product([.10,.15,.20],[.3,.6,1.],['inverse_volatility','minimum_variance'])),
}
NAMES={
 'A':['trend_window','volatility_window','risk_fraction','cash_fraction','rebalance_days'],
 'C':['drawdown_threshold','cooldown_days','reentry_confirmation_days','risk_budget'],
 'D':['volatility_target','cash_cap','weighting'],
}

def weights_for(family,spec):
 signal=G['signal']; ret=signal.pct_change(); w=pd.DataFrame(0.,index=signal.index,columns=core.CODES)
 if family=='A':
  tw,vw,risk,cash,_=spec; active=(signal>signal.rolling(tw).mean())&(ret.rolling(vw).std()*np.sqrt(252)<=ret.rolling(vw).std().rolling(60).median()*np.sqrt(252)); risk=max(0,min(risk,1-cash))
  for d in signal.index:
   a=active.loc[d].fillna(False); w.loc[d]=a/a.sum()*risk if a.sum() else 0
 elif family=='C':
  threshold,cooldown,confirm,budget=spec; base=signal.mean(axis=1); dd=base/base.cummax()-1; ma=base.rolling(20).mean(); off=good=0
  for i,d in enumerate(signal.index):
   if dd.iloc[i]<=-threshold: off=cooldown; good=0
   elif off>0: off-=1
   else: good=good+1 if base.iloc[i]>=ma.iloc[i] else 0
   risk=budget if off==0 and good>=confirm else budget*.3; w.loc[d]=risk/len(core.CODES)
 else:
  target,cap,method=spec
  for i in range(60,len(signal)):
   cov=ret.iloc[i-60:i].dropna().cov().to_numpy()+np.eye(3)*1e-8
   if method=='inverse_volatility': raw=1/np.sqrt(np.diag(cov))
   else:
    try: raw=np.maximum(np.linalg.solve(cov,np.ones(3)),0)
    except np.linalg.LinAlgError: raw=np.ones(3)
   raw=raw/raw.sum(); pv=float(np.sqrt(raw@cov@raw)*np.sqrt(252)); risk=max(1-cap,min(1.,target/max(pv,1e-9))); w.iloc[i]=raw*risk
 return w

def worker(item):
 family,si,spec=item; weights=weights_for(family,spec); hold=spec[4] if family=='A' else (5 if family=='C' else 20); rows=[]
 params=dict(zip(NAMES[family],spec))
 for timing in ('beginning','ending'):
  for start in G['starts']:
   dates=G['close'].loc[start:start+pd.DateOffset(months=24)-pd.Timedelta(days=1)].index
   m=core.simulate(dates,G['close'],G['opn'],G['trad'],G['amount'],weights,G['ev'],timing,hold)
   rows.append({'spec_id':f'U3-{family}{si:03d}','family':family,'deposit_timing':timing,'cohort_start':start.date().isoformat(),**params,**m})
 return rows

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--family',choices=['A','C','D'],required=True); args=ap.parse_args(); family=args.family
 panel_hash=hashlib.sha256(core.PANEL.read_bytes()).hexdigest()
 if panel_hash!='f21ff743900607819436fc3897d1af6ac152e8993649241c79139ed26b6cb3b2': raise RuntimeError('panel hash mismatch')
 event_path=Path('artifacts/derived/phase3_etf_corporate_actions_u3_history/event_ledger_2013_2023.csv'); event_hash=hashlib.sha256(event_path.read_bytes()).hexdigest()
 if event_hash!='0878ad61abddc3c221580d08331069d73aa26a9cbdcb89a93bed3b7bd623bede': raise RuntimeError('event hash mismatch')
 x=pd.read_parquet(core.PANEL); x=x[x.code.isin(core.CODES)&x.trade_date.between('20130315','20251231')].copy(); x.trade_date=pd.to_datetime(x.trade_date,format='%Y%m%d')
 close=x.pivot(index='trade_date',columns='code',values='close'); opn=x.pivot(index='trade_date',columns='code',values='open'); trad=x.pivot(index='trade_date',columns='code',values='tradable').fillna(False); amount=x.pivot(index='trade_date',columns='code',values='amount'); ev=core.events(); signal=core.adjusted_signal(close,ev)
 starts=pd.Series(close.index[120:]).groupby(close.index[120:].to_period('M')).first(); starts=[d for d in starts if d+pd.DateOffset(months=24)<=pd.Timestamp('2026-01-01')]
 G.update(close=close,opn=opn,trad=trad,amount=amount,ev=ev,signal=signal,starts=starts)
 items=[(family,i,s) for i,s in enumerate(DOMAINS[family])]
 with mp.get_context('fork').Pool(8) as pool: chunks=pool.map(worker,items)
 d=pd.DataFrame([r for chunk in chunks for r in chunk]); agg=[]
 reason_cols=['funds_insufficient','lot_rounding','suspension_or_missing_price','target_change_unfilled']
 for sid,g in d.groupby('spec_id'):
  per=g.groupby('cohort_start').agg(w12=('w12','min'),w24=('w24','min'),max_drawdown=('max_drawdown','max'),unexecutable_rate=('unexecutable_rate','max'),fees=('fees','max'),turnover=('turnover','max'),**{c:(c,'max') for c in reason_cols}).reset_index(); blocks=per.iloc[::24].head(6)
  row={'spec_id':sid,'cohorts':len(per),'worst_w12':per.w12.min(),'p5_w12':per.w12.quantile(.05),'worst_w24':per.w24.min(),'p5_w24':per.w24.quantile(.05),'median_w24':per.w24.median(),'dual_target_rate':((per.w12>=500000)&(per.w24>=1200000)).mean(),'nonoverlap_blocks':len(blocks),'nonoverlap_dual_passes':int(((blocks.w12>=500000)&(blocks.w24>=1200000)).sum()),'max_drawdown':per.max_drawdown.max(),'unexecutable_rate':per.unexecutable_rate.max(),'fees':per.fees.max(),'turnover':per.turnover.max(),'asset_identity_passed':bool(g.asset_identity_passed.all()),'negative_cash_events':int(g.negative_cash_events.sum())}
  row.update({c:int(g[c].sum()) for c in reason_cols}); row['passes_targets']=row['worst_w12']>=500000 and row['worst_w24']>=1200000; agg.append(row)
 a=pd.DataFrame(agg); a['passes_strict_gate']=False; a['blocking_reason']=np.where(a.passes_targets,'stress_and_outer_holdout_pending','dual_target_failure')
 out=Path(f'artifacts/derived/phase3_etf_u3_family_{family.lower()}_history'); out.mkdir(parents=True,exist_ok=True); d.to_csv(out/'results_by_cohort_timing.csv',index=False); a.to_csv(out/'candidate_registry.csv',index=False); a.to_csv(out/'elimination_ledger.csv',index=False); a.sort_values(['worst_w24','worst_w12'],ascending=False).head(10).to_csv(out/'pareto.csv',index=False)
 best=a.sort_values('worst_w24',ascending=False).iloc[0]; manifest={'schema_version':1,'universe':'U3','family':family,'specifications':len(items),'monthly_cohorts':len(starts),'cohort_timing_rows':len(d),'nonoverlap_blocks':6,'panel_sha256':panel_hash,'event_source_sha256':event_hash,'cash_dividends':20,'share_factor_events':2,'strict_candidates':0,'base_target_passes':int(a.passes_targets.sum()),'best_worst_w12':float(a.worst_w12.max()),'best_worst_w24':float(a.worst_w24.max()),'best_p5_w24':float(a.p5_w24.max()),'asset_identity_passed':bool(a.asset_identity_passed.all()),'negative_cash_events':int(a.negative_cash_events.sum()),'stress_status':'trigger only if base target passes','evidence_tier':'official_sse_raw_plus_official_sse_event_pdfs'}; (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n'); print(json.dumps(manifest,indent=2)); print(best.to_string())

if __name__=='__main__': main()

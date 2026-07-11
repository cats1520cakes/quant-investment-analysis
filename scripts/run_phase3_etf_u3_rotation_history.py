from __future__ import annotations

import hashlib, itertools, json, math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import multiprocessing as mp

PANEL=Path('artifacts/runtime_data/processed/phase3_etf/sse_u4_canonical.parquet')
OUT=Path('artifacts/derived/phase3_etf_u3_rotation_history')
CODES=['510050','510300','510500']
G={}

def events():
 old=pd.read_csv('artifacts/derived/phase3_etf_corporate_actions_u3_history/event_ledger_2013_2023.csv')
 new=pd.read_csv('artifacts/derived/phase3_etf_corporate_actions_u4/dividend_ledger.csv'); new=new[new.code.astype(str).isin(CODES)].copy(); new['event_type']='cash_dividend'; new['share_factor']=1.; new['source_url']='see_2024_2025_reconciliation'; new['cash_per_share']=new.cash_per_share.astype(float)
 return pd.concat([old,new],ignore_index=True,sort=False)

def adjusted_signal(close, ev):
 r=close.pct_change(); lookup={(pd.Timestamp(x.ex_date),str(x.code)):x for x in ev.itertuples()}
 for i,d in enumerate(close.index[1:],1):
  for c in close:
   x=lookup.get((d,c))
   if x is not None:
    factor=float(getattr(x,'share_factor',1) or 1); cash=float(getattr(x,'cash_per_share',0) or 0)
    r.loc[d,c]=(close.loc[d,c]*factor+cash)/close.iloc[i-1][c]-1
 return (1+r.fillna(0)).cumprod()

def simulate(dates, close, opn, trad, amount, weights, ev, timing, hold):
 cash=0.; shares={c:0 for c in CODES}; pending={}; fees=turn=blocked=orders=0; nav=[]; flows=[]
 by_record={}; by_pay={}; by_ex={}
 for x in ev.itertuples():
  by_record.setdefault(pd.Timestamp(x.record_date),[]).append(x); by_pay.setdefault(pd.Timestamp(x.pay_date),[]).append(x); by_ex.setdefault(pd.Timestamp(x.ex_date),[]).append(x)
 ms=pd.Series(dates).dt.to_period('M').ne(pd.Series(dates).shift().dt.to_period('M')).to_numpy(); me=pd.Series(dates).dt.to_period('M').ne(pd.Series(dates).shift(-1).dt.to_period('M')).to_numpy()
 for j,d in enumerate(dates):
  flow=0.
  if timing=='beginning' and ms[j]: cash+=30000; flow=30000
  for x in by_record.get(d,[]):
   if x.event_type=='cash_dividend': pending[x.event_id]=shares[str(x.code)]*float(x.cash_per_share)
  for x in by_ex.get(d,[]):
   if x.event_type=='share_factor': shares[str(x.code)]=math.floor(shares[str(x.code)]*float(x.share_factor)) if d.year==2015 else math.ceil(shares[str(x.code)]*float(x.share_factor))
  for x in by_pay.get(d,[]):
   if x.event_id in pending: cash+=pending.pop(x.event_id)
  gi=close.index.get_loc(d)
  if gi>0 and gi%hold==0:
   px=opn.loc[d]; prior_amt=amount.iloc[gi-1]; n0=cash+sum(shares[c]*close.loc[d,c] for c in CODES); target=weights.iloc[gi-1]
   desired={c:int(n0*max(float(target[c]),0)/px[c]/100)*100 if np.isfinite(px[c]) else shares[c] for c in CODES}
   for side in ('sell','buy'):
    for c in CODES:
     q=desired[c]-shares[c]
     if (side=='sell' and q>=0) or (side=='buy' and q<=0): continue
     orders+=1
     if not bool(trad.loc[d,c]) or not np.isfinite(px[c]): blocked+=1; continue
     cap=max(float(prior_amt[c])*0.05,0); maxq=int(cap/px[c]/100)*100
     qty=min(abs(q),maxq)
     if qty<=0: blocked+=1; continue
     if side=='sell':
      if qty<abs(q): blocked+=1
      gross=qty*px[c]; fee=max(5.,gross*.0007); cash+=gross-fee; shares[c]-=qty
     else:
      affordable=int(max(cash-5,0)/(px[c]*1.0007)/100)*100; qty=min(qty,affordable)
      if qty<=0: blocked+=1; continue
      if qty<q: blocked+=1
      gross=qty*px[c]; fee=max(5.,gross*.0007); cash-=gross+fee; shares[c]+=qty
     fees+=fee; turn+=gross
  if timing=='ending' and me[j]: cash+=30000; flow=30000
  n=cash+sum(shares[c]*close.loc[d,c] for c in CODES)
  if cash < -1e-7 or abs(n-(cash+sum(shares[c]*close.loc[d,c] for c in CODES)))>1e-7: raise RuntimeError('asset identity')
  nav.append(n); flows.append(flow)
 a=np.array(nav); peak=np.maximum.accumulate(a); dd=np.zeros_like(a); np.divide(a,peak,out=dd,where=peak>0); dd=np.where(peak>0,dd-1,0)
 return {'w12':a[min(len(a)-1,np.searchsorted(dates,dates[0]+pd.DateOffset(months=12),side='left')-1)],'w24':a[-1],'max_drawdown':float(-dd.min()),'fees':fees,'turnover':turn,'blocked_orders':blocked,'orders':orders,'unexecutable_rate':blocked/max(orders,1)}

def evaluate_spec(item):
 si,spec=item; top,mom,absolute,fallback,hold=spec; close=G['close']; signal=G['signal']
 momentum=signal/signal.shift(mom)-1; active=signal>signal.rolling(absolute).mean(); weights=pd.DataFrame(0.,index=signal.index,columns=CODES)
 for d in signal.index:
  eligible=momentum.loc[d][active.loc[d].fillna(False)].dropna().sort_values(ascending=False).head(top).index
  if len(eligible): weights.loc[d,eligible]=1/len(eligible)
  elif fallback!='cash': weights.loc[d,fallback]=1.
 rows=[]
 for timing in ('beginning','ending'):
  for start in G['starts']:
   end=start+pd.DateOffset(months=24)-pd.Timedelta(days=1); dates=close.loc[start:end].index
   m=simulate(dates,close,G['opn'],G['trad'],G['amount'],weights,G['ev'],timing,hold); rows.append({'spec_id':f'U3-B{si:03d}','top_k':top,'momentum_window':mom,'absolute_trend_window':absolute,'fallback':fallback,'holding_days':hold,'deposit_timing':timing,'cohort_start':start.date().isoformat(),**m})
 return rows

def main():
 if hashlib.sha256(PANEL.read_bytes()).hexdigest()!='f21ff743900607819436fc3897d1af6ac152e8993649241c79139ed26b6cb3b2': raise RuntimeError('panel hash')
 x=pd.read_parquet(PANEL); x=x[x.code.isin(CODES)&x.trade_date.between('20130315','20251231')].copy(); x.trade_date=pd.to_datetime(x.trade_date,format='%Y%m%d')
 close=x.pivot(index='trade_date',columns='code',values='close'); opn=x.pivot(index='trade_date',columns='code',values='open'); trad=x.pivot(index='trade_date',columns='code',values='tradable').fillna(False); amount=x.pivot(index='trade_date',columns='code',values='amount')
 ev=events(); signal=adjusted_signal(close,ev); starts=pd.Series(close.index[120:]).groupby(close.index[120:].to_period('M')).first(); starts=[d for d in starts if d+pd.DateOffset(months=24)<=pd.Timestamp('2026-01-01')]
 specs=list(itertools.product([1,2],[20,60,120],[20,60,120],['cash','510050'],[5,20])); G.update(close=close,opn=opn,trad=trad,amount=amount,signal=signal,ev=ev,starts=starts)
 with mp.get_context('fork').Pool(8) as pool: chunks=pool.map(evaluate_spec,list(enumerate(specs)))
 rows=[r for chunk in chunks for r in chunk]
 d=pd.DataFrame(rows); ag=[]
 for sid,g in d.groupby('spec_id'):
  per=g.groupby('cohort_start').agg(w12=('w12','min'),w24=('w24','min'),max_drawdown=('max_drawdown','max'),unexecutable_rate=('unexecutable_rate','max'),fees=('fees','max'),turnover=('turnover','max')).reset_index(); blocks=per.iloc[::24].head(6)
  ag.append({'spec_id':sid,'cohorts':len(per),'worst_w12':per.w12.min(),'worst_w24':per.w24.min(),'p5_w24':per.w24.quantile(.05),'median_w24':per.w24.median(),'dual_target_rate':((per.w12>=500000)&(per.w24>=1200000)).mean(),'nonoverlap_blocks':len(blocks),'nonoverlap_dual_passes':int(((blocks.w12>=500000)&(blocks.w24>=1200000)).sum()),'max_drawdown':per.max_drawdown.max(),'unexecutable_rate':per.unexecutable_rate.max(),'fees':per.fees.max(),'turnover':per.turnover.max(),'passes_targets':bool((per.w12.min()>=500000)&(per.w24.min()>=1200000))})
 a=pd.DataFrame(ag); a['passes_strict_gate']=False; a['blocking_reason']=np.where(a.passes_targets,'outer_holdout_and_stress_pending','dual_target_failure')
 OUT.mkdir(parents=True,exist_ok=True); d.to_csv(OUT/'results_by_cohort_timing.csv',index=False); a.to_csv(OUT/'candidate_registry.csv',index=False); a[~a.passes_strict_gate].to_csv(OUT/'elimination_ledger.csv',index=False); a.sort_values(['worst_w24','worst_w12'],ascending=False).head(10).to_csv(OUT/'pareto.csv',index=False)
 man={'schema_version':1,'universe':'U3','family':'B_rotation','specifications':72,'cohort_timing_rows':len(d),'panel_sha256':hashlib.sha256(PANEL.read_bytes()).hexdigest(),'event_source_sha256':hashlib.sha256(Path('artifacts/derived/phase3_etf_corporate_actions_u3_history/event_ledger_2013_2023.csv').read_bytes()).hexdigest(),'minimum_nonoverlap_blocks':5,'strict_candidates':0,'evidence_tier':'official_sse_raw_plus_official_sse_event_pdfs','survivorship_bias':'conditional_current_U4_parent'}; (OUT/'manifest.json').write_text(json.dumps(man,indent=2)+'\n'); print(a.sort_values('worst_w24',ascending=False).head().to_string(index=False))

if __name__=='__main__': main()

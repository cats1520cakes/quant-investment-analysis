from __future__ import annotations
import hashlib,json,itertools
from pathlib import Path
import numpy as np,pandas as pd,yaml

ROOT=Path('artifacts/runtime_data'); OUT=Path('artifacts/derived/phase3_etf_strict_family_a')
def run(close,opn,trad,events,spec,timing,precomputed_weights=None):
 idx=close.index; cash=0.; sh={c:0 for c in close}; recv={}; fees=turn=blocked=0; nav=[]; ext=[]
 ms=pd.Series(idx,index=idx).dt.to_period('M').ne(pd.Series(idx,index=idx).shift().dt.to_period('M')).values
 me=pd.Series(idx,index=idx).dt.to_period('M').ne(pd.Series(idx,index=idx).shift(-1).dt.to_period('M')).values
 ret=close.pct_change(); vol=ret.rolling(spec[1]).std()*np.sqrt(252); ma=close.rolling(spec[0]).mean(); sig=(close>ma)&(vol.le(vol.rolling(60).median()))
 for i,d in enumerate(idx):
  flow=0
  if timing=='beginning' and ms[i]: cash+=30000; flow=30000
  for e in events.get(d,[]): recv[e.event_id]=(pd.Timestamp(e.pay_date),sh[str(e.code)]*e.cash_per_share)
  for k,(pd_,amt) in list(recv.items()):
   if pd_==d: cash+=amt; del recv[k]
  if i>0 and i%spec[4]==0:
   if precomputed_weights is None:
    risk=max(0.,min(spec[2],1-spec[3])); active=sig.iloc[i-1].fillna(False); w=active.astype(float); w=w/w.sum()*risk if w.sum() else w
   else: w=precomputed_weights.iloc[i-1].fillna(0.0)
   prices=opn.iloc[i]
   for c in close:
    if sh[c] and (not trad.loc[d,c] or not np.isfinite(prices[c])): blocked+=1; continue
    target=int((cash+sum(sh[x]*close.loc[d,x] for x in close))*w[c]/prices[c]/100)*100 if w[c]>0 else 0
    q=target-sh[c]
    if q<0:
     gross=-q*prices[c]; fee=max(5.,gross*.0007); cash+=gross-fee; fees+=fee; turn+=gross; sh[c]=target
   for c in close:
    target=int((cash+sum(sh[x]*close.loc[d,x] for x in close))*w[c]/prices[c]/100)*100 if w[c]>0 else 0; q=max(0,target-sh[c])
    if q and trad.loc[d,c] and np.isfinite(prices[c]):
     gross=q*prices[c]; fee=max(5.,gross*.0007)
     if gross+fee<=cash: cash-=gross+fee; fees+=fee; turn+=gross; sh[c]+=q
     else: blocked+=1
  if timing=='ending' and me[i]: cash+=30000; flow=30000
  n=cash+sum(sh[c]*close.loc[d,c] for c in close); assert cash>=-1e-6 and abs(n-(cash+sum(sh[c]*close.loc[d,c] for c in close)))<1e-6
  nav.append(n); ext.append(flow)
 a=np.array(nav); months=((idx.year-idx[0].year)*12+idx.month-idx[0].month+1); w12=a[months<=12][-1]; peak=np.maximum.accumulate(a); dd=np.where(peak>0,a/peak-1,0)
 return dict(w12=w12,w24=a[-1],max_drawdown=-dd.min(),fees=fees,turnover=turn,blocked=blocked,unexecutable_rate=blocked/max(len(idx),1),sample_windows=1)
def main():
 cfg=yaml.safe_load(Path('config/phase3_etf_strict_new_families.yaml').read_text()); p=ROOT/'processed/phase3_etf/sse_u4_canonical.parquet'; man=json.load(open(str(p)+'.manifest.json')); assert hashlib.sha256(p.read_bytes()).hexdigest()==cfg['panel_sha256']==man['panel_sha256']
 x=pd.read_parquet(p); x=x[x.trade_date.between('20240101','20251231')]; x.trade_date=pd.to_datetime(x.trade_date); close=x.pivot(index='trade_date',columns='code',values='close'); opn=x.pivot(index='trade_date',columns='code',values='open'); trad=x.pivot(index='trade_date',columns='code',values='tradable').fillna(False)
 ev=pd.read_csv('artifacts/derived/phase3_etf_corporate_actions_u4/dividend_ledger.csv'); from types import SimpleNamespace
 events={}; [events.setdefault(pd.Timestamp(r.record_date),[]).append(SimpleNamespace(**r.to_dict())) for _,r in ev.iterrows()]
 specs=list(itertools.product([20,60],[20,60],[.4,.7,1.],[0,.3],[5,20])); rows=[]
 for j,s in enumerate(specs):
  for t in ['beginning','ending']: rows.append({'strategy':f'A{j:03d}','family':'cash_trend_volatility','trend_window':s[0],'volatility_window':s[1],'risk_fraction':s[2],'cash_fraction':s[3],'rebalance_days':s[4],'deposit_timing':t,**run(close,opn,trad,events,s,t)})
 d=pd.DataFrame(rows); agg=d.groupby('strategy').agg(worst_w12=('w12','min'),worst_w24=('w24','min'),max_drawdown=('max_drawdown','max'),unexecutable_rate=('unexecutable_rate','max'),fees=('fees','max'),turnover=('turnover','max'),sample_windows=('sample_windows','min')).reset_index(); agg['p5_w24']=np.nan; agg['passes_targets']=(agg.worst_w12>=500000)&(agg.worst_w24>=1200000); agg['sample_size_gate_passed']=False; agg['passes_strict_gate']=False; agg['blocking_reason']='sample_size_gate: one 24-month window; five nonoverlap blocks required'
 OUT.mkdir(parents=True,exist_ok=True); d.to_csv(OUT/'results_by_timing.csv',index=False); agg.to_csv(OUT/'candidate_registry.csv',index=False); agg[~agg.passes_strict_gate].to_csv(OUT/'elimination_ledger.csv',index=False); (OUT/'manifest.json').write_text(json.dumps({'family':'A','specifications':48,'timing_rows':96,'panel_sha256':man['panel_sha256'],'dividend_ledger_sha256':hashlib.sha256(Path('artifacts/derived/phase3_etf_corporate_actions_u4/dividend_ledger.csv').read_bytes()).hexdigest(),'strict_candidates':0,'sample_size_gate_passed':False},indent=2)+'\n'); print(agg.sort_values('worst_w24',ascending=False).head().to_string(index=False))
if __name__=='__main__': main()

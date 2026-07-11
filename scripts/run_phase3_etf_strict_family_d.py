from __future__ import annotations
import hashlib,itertools,json
from pathlib import Path
from types import SimpleNamespace
import numpy as np,pandas as pd,yaml
from run_phase3_etf_strict_family_a import run
ROOT=Path('artifacts/runtime_data'); OUT=Path('artifacts/derived/phase3_etf_strict_family_d')
def main():
 cfg=yaml.safe_load(Path('config/phase3_etf_strict_new_families.yaml').read_text()); p=ROOT/'processed/phase3_etf/sse_u4_canonical.parquet'; man=json.load(open(str(p)+'.manifest.json')); assert hashlib.sha256(p.read_bytes()).hexdigest()==cfg['panel_sha256']==man['panel_sha256']
 x=pd.read_parquet(p); x=x[x.trade_date.between('20240101','20251231')]; x.trade_date=pd.to_datetime(x.trade_date); close=x.pivot(index='trade_date',columns='code',values='close'); opn=x.pivot(index='trade_date',columns='code',values='open'); trad=x.pivot(index='trade_date',columns='code',values='tradable').fillna(False); ret=close.pct_change()
 ev=pd.read_csv('artifacts/derived/phase3_etf_corporate_actions_u4/dividend_ledger.csv'); events={}; [events.setdefault(pd.Timestamp(r.record_date),[]).append(SimpleNamespace(**r.to_dict())) for _,r in ev.iterrows()]
 specs=list(itertools.product([.10,.15,.20],[.3,.6,1.],['inverse_volatility','minimum_variance'])); rows=[]
 for j,s in enumerate(specs):
  target,cap,method=s; w=pd.DataFrame(0.,index=close.index,columns=close.columns)
  for i in range(60,len(close)):
   hist=ret.iloc[i-60:i].dropna(); cov=hist.cov().to_numpy()+np.eye(4)*1e-8
   if method=='inverse_volatility': raw=1/np.sqrt(np.diag(cov))
   else:
    try: raw=np.linalg.solve(cov,np.ones(4)); raw=np.maximum(raw,0)
    except np.linalg.LinAlgError: raw=np.ones(4)
   raw=raw/raw.sum(); pv=float(np.sqrt(raw@cov@raw)*np.sqrt(252)); risk=max(1-cap,min(1.,target/max(pv,1e-9))); w.iloc[i]=raw*risk
  for t in ['beginning','ending']: rows.append({'strategy':f'D{j:03d}','family':'downside_budget','volatility_target':target,'cash_cap':cap,'weighting':method,'deposit_timing':t,**run(close,opn,trad,events,(1,1,1,0,20),t,w)})
 d=pd.DataFrame(rows); a=d.groupby('strategy').agg(worst_w12=('w12','min'),worst_w24=('w24','min'),max_drawdown=('max_drawdown','max'),unexecutable_rate=('unexecutable_rate','max'),fees=('fees','max'),turnover=('turnover','max'),sample_windows=('sample_windows','min')).reset_index(); a['p5_w24']=np.nan; a['passes_targets']=(a.worst_w12>=500000)&(a.worst_w24>=1200000); a['sample_size_gate_passed']=False; a['asset_identity_passed']=True; a['passes_strict_gate']=False; a['blocking_reason']='sample_size_gate: one 24-month window; five nonoverlap blocks required'
 OUT.mkdir(parents=True,exist_ok=True); d.to_csv(OUT/'results_by_timing.csv',index=False); a.to_csv(OUT/'candidate_registry.csv',index=False); a.to_csv(OUT/'elimination_ledger.csv',index=False); a.sort_values(['worst_w24','max_drawdown'],ascending=[False,True]).head(10).to_csv(OUT/'pareto.csv',index=False); (OUT/'manifest.json').write_text(json.dumps({'family':'D','specifications':18,'timing_rows':36,'panel_sha256':man['panel_sha256'],'strict_candidates':0,'sample_size_gate_passed':False,'asset_identity_passed':True},indent=2)+'\n'); print(a.sort_values('worst_w24',ascending=False).head().to_string(index=False))
if __name__=='__main__': main()

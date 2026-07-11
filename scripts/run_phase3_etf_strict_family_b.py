from __future__ import annotations
import hashlib,itertools,json
from pathlib import Path
from types import SimpleNamespace
import numpy as np,pandas as pd,yaml
from run_phase3_etf_strict_family_a import run

ROOT=Path('artifacts/runtime_data'); OUT=Path('artifacts/derived/phase3_etf_strict_family_b')
def main():
 cfg=yaml.safe_load(Path('config/phase3_etf_strict_new_families.yaml').read_text()); p=ROOT/'processed/phase3_etf/sse_u4_canonical.parquet'; man=json.load(open(str(p)+'.manifest.json')); assert hashlib.sha256(p.read_bytes()).hexdigest()==cfg['panel_sha256']==man['panel_sha256']
 x=pd.read_parquet(p); x=x[x.trade_date.between('20240101','20251231')]; x.trade_date=pd.to_datetime(x.trade_date); close=x.pivot(index='trade_date',columns='code',values='close'); opn=x.pivot(index='trade_date',columns='code',values='open'); trad=x.pivot(index='trade_date',columns='code',values='tradable').fillna(False)
 ev=pd.read_csv('artifacts/derived/phase3_etf_corporate_actions_u4/dividend_ledger.csv'); events={}; [events.setdefault(pd.Timestamp(r.record_date),[]).append(SimpleNamespace(**r.to_dict())) for _,r in ev.iterrows()]
 specs=list(itertools.product([1,2],[20,60,120],[20,60,120],['cash','510050'],[5,20])); rows=[]
 for j,s in enumerate(specs):
  top,mom,absolute,fallback,hold=s; score=close/close.shift(mom)-1; ok=close>close.rolling(absolute).mean(); weights=pd.DataFrame(0.,index=close.index,columns=close.columns)
  for i in range(len(close)):
   eligible=score.iloc[i].where(ok.iloc[i]).dropna().nlargest(top).index
   if len(eligible): weights.loc[close.index[i],eligible]=1/len(eligible)
   elif fallback!='cash': weights.loc[close.index[i],fallback]=1.
  for t in ['beginning','ending']: rows.append({'strategy':f'B{j:03d}','family':'u4_rotation','top_k':top,'momentum_window':mom,'absolute_trend_window':absolute,'fallback':fallback,'holding_days':hold,'deposit_timing':t,**run(close,opn,trad,events,(1,1,1,0,hold),t,weights)})
 d=pd.DataFrame(rows); a=d.groupby('strategy').agg(worst_w12=('w12','min'),worst_w24=('w24','min'),max_drawdown=('max_drawdown','max'),unexecutable_rate=('unexecutable_rate','max'),fees=('fees','max'),turnover=('turnover','max'),sample_windows=('sample_windows','min')).reset_index(); a['p5_w24']=np.nan; a['passes_targets']=(a.worst_w12>=500000)&(a.worst_w24>=1200000); a['sample_size_gate_passed']=False; a['asset_identity_passed']=True; a['passes_strict_gate']=False; a['blocking_reason']='sample_size_gate: one 24-month window; five nonoverlap blocks required'
 OUT.mkdir(parents=True,exist_ok=True); d.to_csv(OUT/'results_by_timing.csv',index=False); a.to_csv(OUT/'candidate_registry.csv',index=False); a.to_csv(OUT/'elimination_ledger.csv',index=False); pareto=a.sort_values(['worst_w24','max_drawdown'],ascending=[False,True]).head(10); pareto.to_csv(OUT/'pareto.csv',index=False); (OUT/'manifest.json').write_text(json.dumps({'family':'B','specifications':72,'timing_rows':144,'panel_sha256':man['panel_sha256'],'dividend_ledger_sha256':hashlib.sha256(Path('artifacts/derived/phase3_etf_corporate_actions_u4/dividend_ledger.csv').read_bytes()).hexdigest(),'strict_candidates':0,'sample_size_gate_passed':False,'asset_identity_passed':True},indent=2)+'\n'); print(a.sort_values('worst_w24',ascending=False).head().to_string(index=False))
if __name__=='__main__': main()

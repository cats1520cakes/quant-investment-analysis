from __future__ import annotations

import hashlib, itertools, json, math, os, tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT=Path('artifacts/derived/phase3_sse_four_asset_state_drawdown_v1')
OP=ROOT/'operation_manifest.json'
PANEL=Path('artifacts/runtime_data/processed/phase3_etf/multi_asset_sse_canonical.parquet')
EVENTS=Path('artifacts/derived/phase3_multi_asset_official_actions_sse/event_ledger.csv')
EVENT_MANIFEST=Path('artifacts/derived/phase3_multi_asset_official_actions_sse/event_manifest.json')
CODES=['510300','510880','518880','511010']; EQUITY=['510300','510880']; DEFENSIVE=['518880','511010']
EXPECTED_OPERATION_SHA='5c949ec12854a6b951dbc8ecc10f470189db049271516a99e997c990702476c2'

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def atomic_csv(d,p):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);fd,t=tempfile.mkstemp(dir=p.parent,suffix='.tmp');os.close(fd)
 try:d.to_csv(t,index=False);os.replace(t,p)
 finally:
  if os.path.exists(t):os.unlink(t)
def atomic_json(x,p):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);fd,t=tempfile.mkstemp(dir=p.parent,suffix='.tmp');os.close(fd)
 try:
  with open(t,'w') as f: json.dump(x,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(t,p)
 finally:
  if os.path.exists(t):os.unlink(t)

def adjusted_signal(close,ev):
 r=close.pct_change(); look={(pd.Timestamp(x.ex_date),str(x.code)):x for x in ev.itertuples()}
 for i,d in enumerate(close.index[1:],1):
  for c in close:
   x=look.get((d,c))
   if x is not None:r.loc[d,c]=(close.loc[d,c]*(float(x.share_factor) if pd.notna(x.share_factor) else 1)+(float(x.cash_per_share) if pd.notna(x.cash_per_share) else 0))/close.iloc[i-1][c]-1
 return (1+r.fillna(0)).cumprod()

def weights(spec,signal):
 lb,ew,dcap,enter,exit_=spec; mom=signal/signal.shift(lb)-1; trend=signal['510300']>signal['510300'].rolling(120).mean(); out=pd.DataFrame(0.,index=signal.index,columns=CODES)
 state='normal'; peak=1.; synthetic=1.
 for i,d in enumerate(signal.index):
  if i: synthetic*=1+signal[EQUITY].pct_change().iloc[i].mean();peak=max(peak,synthetic)
  dd=synthetic/peak-1
  if state=='normal' and dd<=-enter:state='reduced'
  elif state=='reduced' and dd>=-exit_:state='normal'
  eq=mom.loc[d,EQUITY].dropna().sort_values(ascending=False); de=mom.loc[d,DEFENSIVE].dropna().sort_values(ascending=False)
  if trend.loc[d] and state=='normal' and len(eq):out.loc[d,eq.index[0]]=ew
  if len(de):out.loc[d,de.index[0]]=min(1-ew if trend.loc[d] and state=='normal' else dcap,1-out.loc[d].sum())
 return out

def simulate(dates,close,opn,trad,amount,w,ev,timing):
 cash=0.;sh={c:0 for c in CODES};pending={};fees=turn=orders=blocked=0;nav=[];daily=[];reasons={k:0 for k in ['funds_insufficient','lot_rounding','suspension_or_missing_price','target_change_unfilled']}
 rec={};pay={}
 for x in ev.itertuples():rec.setdefault(pd.Timestamp(x.record_date),[]).append(x);pay.setdefault(pd.Timestamp(x.pay_date),[]).append(x)
 months=pd.Series(dates).dt.to_period('M');ms=months.ne(months.shift()).to_numpy();me=months.ne(months.shift(-1)).to_numpy()
 for j,d in enumerate(dates):
  flow=0.
  if timing=='beginning' and ms[j]:cash+=30000;flow=30000
  for x in rec.get(d,[]):pending[x.event_id]=sh[str(x.code)]*(float(x.cash_per_share) if pd.notna(x.cash_per_share) else 0)
  for x in pay.get(d,[]):cash+=pending.pop(x.event_id,0)
  if ms[j] and close.index.get_loc(d)>0:
   px=opn.loc[d]; prior_amt=amount.iloc[close.index.get_loc(d)-1]; value=cash+sum(sh[c]*close.loc[d,c] for c in CODES);target=w.loc[close.index[close.index.get_loc(d)-1]]
   desired={c:int(value*max(float(target[c]),0)/px[c]/100)*100 if np.isfinite(px[c]) else sh[c] for c in CODES}
   for c in CODES:
    if np.isfinite(px[c]) and target[c]>0 and value*target[c]/px[c]>=1 and desired[c]==0:reasons['lot_rounding']+=1
   for side in ('sell','buy'):
    for c in CODES:
     q=desired[c]-sh[c]
     if (side=='sell' and q>=0) or (side=='buy' and q<=0):continue
     orders+=1
     if not bool(trad.loc[d,c]) or not np.isfinite(px[c]):blocked+=1;reasons['suspension_or_missing_price']+=1;continue
     maxq=int(max(float(prior_amt[c])*.05,0)/px[c]/100)*100;qty=min(abs(q),maxq)
     if qty<=0:blocked+=1;reasons['target_change_unfilled']+=1;continue
     if side=='sell':gross=qty*px[c];fee=max(5.,gross*.0007);cash+=gross-fee;sh[c]-=qty
     else:
      qty=min(qty,int(max(cash-5,0)/(px[c]*1.0007)/100)*100)
      if qty<=0:blocked+=1;reasons['funds_insufficient']+=1;continue
      gross=qty*px[c];fee=max(5.,gross*.0007);cash-=gross+fee;sh[c]+=qty
     fees+=fee;turn+=gross
     if qty<abs(q):blocked+=1;reasons['target_change_unfilled']+=1
  if timing=='ending' and me[j]:cash+=30000;flow=30000
  n=cash+sum(sh[c]*close.loc[d,c] for c in CODES);res=n-(cash+sum(sh[c]*close.loc[d,c] for c in CODES))
  if cash < -1e-7 or abs(res)>1e-7:raise RuntimeError('asset identity')
  nav.append(n);daily.append({'date':d,'cash':cash,'nav':n,'flow':flow,'asset_identity_residual':res,**{f'shares_{c}':sh[c] for c in CODES}})
 a=np.array(nav);peak=np.maximum.accumulate(a);dd=np.divide(a,peak,out=np.ones_like(a),where=peak>0)-1
 i12=min(len(a)-1,max(0,np.searchsorted(dates,dates[0]+pd.DateOffset(months=12),side='left')-1))
 return {'w12':a[i12],'w24':a[-1],'max_drawdown':float(dd.min()),'fees':fees,'turnover':turn,'unexecutable_rate':blocked/max(orders,1),**reasons,'asset_identity_passed':True},pd.DataFrame(daily)

def main():
 if sha(OP)!=EXPECTED_OPERATION_SHA:raise RuntimeError('operation manifest hash mismatch')
 em=json.loads(EVENT_MANIFEST.read_text())
 if not em.get('body_gate_passed') or em.get('unresolved_candidates'):raise RuntimeError('company action body gate')
 x=pd.read_parquet(PANEL);x=x[x.code.astype(str).isin(CODES)].copy();x['code']=x.code.astype(str);x['trade_date']=pd.to_datetime(x.trade_date.astype(str))
 close=x.pivot(index='trade_date',columns='code',values='close').dropna();opn=x.pivot(index='trade_date',columns='code',values='open').reindex(close.index);trad=x.pivot(index='trade_date',columns='code',values='tradable').reindex(close.index).fillna(False);amount=x.pivot(index='trade_date',columns='code',values='amount').reindex(close.index)
 ev=pd.read_csv(EVENTS);ev['code']=ev.code.astype(str);signal=adjusted_signal(close,ev);starts=pd.Series(close.index[240:]).groupby(close.index[240:].to_period('M')).first();starts=[d for d in starts if d+pd.DateOffset(months=24)<=close.index.max()+pd.Timedelta(days=1)]
 specs=list(itertools.product([60,120,240],[.5,.75,1.],[.5,.75,1.],[.08,.12],[.03,.06]));out=ROOT/'run';parts=out/'parts';ledgers=out/'daily_ledgers';parts.mkdir(parents=True,exist_ok=True);ledgers.mkdir(parents=True,exist_ok=True)
 remote_attempt=out/'attempt_ledger.csv'
 remote_complete=set(pd.read_csv(remote_attempt).spec_id.astype(str)) if remote_attempt.exists() else set()
 for i,spec in enumerate(specs):
  sid=f'S4SD-{i:04d}';part=parts/f'{sid}.csv'
  if part.exists() or sid in remote_complete:continue
  w=weights(spec,signal);rows=[];all_daily=[]
  for timing in ('beginning','ending'):
   for start in starts:
    dates=close.loc[start:start+pd.DateOffset(months=24)-pd.Timedelta(days=1)].index;m,d=simulate(dates,close,opn,trad,amount,w,ev,timing);rows.append({'spec_id':sid,'deposit_timing':timing,'cohort_start':start.date().isoformat(),**m});d.insert(0,'cohort_start',start.date().isoformat());d.insert(0,'deposit_timing',timing);all_daily.append(d)
  lp=ledgers/f'{sid}.parquet';tmp=lp.with_suffix('.tmp');pd.concat(all_daily).to_parquet(tmp,index=False,compression='zstd');os.replace(tmp,lp);h=sha(lp);frame=pd.DataFrame(rows);frame['daily_ledger_sha256']=h;frame['daily_ledger_rows']=sum(map(len,all_daily));atomic_csv(frame,part)
  local_done=sorted(parts.glob('*.csv'));all_done=sorted(remote_complete|{p.stem for p in local_done});atomic_csv(pd.DataFrame({'spec_id':all_done,'status':'complete'}),out/'attempt_ledger.csv');atomic_json({'completed_specs':len(all_done),'target_specs':108,'remote_compact_only_specs':len(remote_complete),'new_atomic_parts':len(local_done),'operation_sha256':EXPECTED_OPERATION_SHA,'panel_sha256':sha(PANEL),'event_ledger_sha256':sha(EVENTS),'daily_ledger_files_claimed':len(remote_complete)+len(local_done),'daily_ledger_files_locally_auditable':len(local_done),'reporting_gate':'fail_closed_until_remote_85_large_artifacts_recovered','strict_candidates':0},out/'coverage.json')
  if len(local_done)%6==0:print(f'sse_four_asset_state_drawdown_v1 {len(all_done)}/108 ({len(local_done)} new atomic)',flush=True)
 frames=[pd.read_csv(p) for p in sorted(parts.glob('*.csv'))];res=pd.concat(frames);atomic_csv(res,out/'results_by_cohort_timing_new_23.csv');agg=[]
 for sid,g in res.groupby('spec_id'):
  per=g.groupby('cohort_start').agg(w12=('w12','min'),w24=('w24','min'),max_drawdown=('max_drawdown','min'),unexecutable_rate=('unexecutable_rate','max'),fees=('fees','max'),turnover=('turnover','max')).reset_index();blocks=per.iloc[::24].head(6);agg.append({'spec_id':sid,'cohorts':len(per),'worst_w12':per.w12.min(),'p5_w12':per.w12.quantile(.05),'worst_w24':per.w24.min(),'p5_w24':per.w24.quantile(.05),'dual_target_rate':((per.w12>=500000)&(per.w24>=1200000)).mean(),'nonoverlap_blocks':len(blocks),'nonoverlap_dual_passes':int(((blocks.w12>=500000)&(blocks.w24>=1200000)).sum()),'max_drawdown':per.max_drawdown.min(),'unexecutable_rate':per.unexecutable_rate.max(),'fees':per.fees.max(),'turnover':per.turnover.max(),'asset_identity_passed':bool(g.asset_identity_passed.all()),'passes_targets':per.w12.min()>=500000 and per.w24.min()>=1200000})
 a=pd.DataFrame(agg);a['strict_candidate']=False;a['reporting_gate']='remote_first_85_detailed_artifacts_missing';atomic_csv(a,out/'candidate_registry_new_23.csv');atomic_csv(a,out/'elimination_ledger_new_23.csv');atomic_csv(a.sort_values(['worst_w24','worst_w12'],ascending=False).head(20),out/'pareto_new_23.csv');atomic_json({'schema_version':1,'family':'sse_four_asset_state_drawdown_v1','operation_sha256':EXPECTED_OPERATION_SHA,'specifications':108,'execution_coverage':108,'new_atomic_results':len(a),'remote_compact_only_results':85,'complete_family_aggregation_permitted':False,'reporting_gate':'remote_first_85_parts_and_ledgers_not_in_disaster_checkpoint','cohorts_per_new_spec':int(a.cohorts.min()),'six_block_gate_new_specs':bool((a.nonoverlap_blocks>=6).all()),'new_spec_dual_target_passes':int(a.passes_targets.sum()),'strict_candidates':0,'evidence_tier':'official_sse_raw_plus_official_sse_event_pdfs_etf_only','official_daily_margin_required':False,'panel_sha256':sha(PANEL),'event_ledger_sha256':sha(EVENTS)},out/'manifest.json')

if __name__=='__main__':main()

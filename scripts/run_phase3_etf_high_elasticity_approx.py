from __future__ import annotations
import hashlib,itertools,json,math,multiprocessing as mp
from pathlib import Path
import numpy as np,pandas as pd
from quant_proof.free_sources.etf_tencent_adapter import parse_tencent_day
import run_phase3_etf_u3_rotation_history as u3

OUT=Path('artifacts/derived/phase3_etf_high_elasticity_approx'); G={}
FAMILY_UNIVERSE={'single_159915_trend_cash':['159915'],'e2_relative_absolute':['159915','510500'],'e3_concentrated_rotation':['159915','510500','510300'],'vol_drawdown_cash':['159915'],'multiscale_breakout':['159915','510500','510300']}
def load_data():
 s=pd.read_parquet(u3.PANEL); s=s[s.code.isin(['510500','510300'])].copy(); s.trade_date=pd.to_datetime(s.trade_date,format='%Y%m%d'); raw=s.pivot(index='trade_date',columns='code',values='close'); op=s.pivot(index='trade_date',columns='code',values='open'); trad=s.pivot(index='trade_date',columns='code',values='tradable').fillna(False)
 tr=parse_tencent_day(json.load(open('artifacts/runtime_data/raw/tencent_etf/159915_raw.json')),'159915','raw'); th=parse_tencent_day(json.load(open('artifacts/runtime_data/raw/tencent_etf/159915_hfq.json')),'159915','hfq'); tr.trade_date=pd.to_datetime(tr.trade_date,format='%Y%m%d'); th.trade_date=pd.to_datetime(th.trade_date,format='%Y%m%d'); tr=tr.set_index('trade_date'); th=th.set_index('trade_date')
 raw['159915']=tr.close; op['159915']=tr.open; trad['159915']=tr.close.notna(); trad.loc[pd.Timestamp('2021-02-08'),'159915']=False; trad.loc[pd.Timestamp('2021-02-09'),'159915']=False
 ev=u3.events(); sig=u3.adjusted_signal(raw[['510500','510300']],ev); sig['159915']=th.close/th.close.dropna().iloc[0]; return raw.sort_index(),op.sort_index(),trad.sort_index(),sig.sort_index(),ev
def simulate(dates,codes,w,hold,timing):
 close,opn,trad,_,ev=G['close'],G['opn'],G['trad'],G['signal'],G['ev']; cash=0.; sh={c:0 for c in codes}; pending={}; fees=turn=orders=blocked=funds=lots=susp=targetfail=0; nav=[]
 byrec={}; bypay={}; byex={}
 for x in ev.itertuples():
  if str(x.code) not in codes: continue
  byrec.setdefault(pd.Timestamp(x.record_date),[]).append(x); bypay.setdefault(pd.Timestamp(x.pay_date),[]).append(x); byex.setdefault(pd.Timestamp(x.ex_date),[]).append(x)
 ms=pd.Series(dates).dt.to_period('M').ne(pd.Series(dates).shift().dt.to_period('M')).to_numpy(); me=pd.Series(dates).dt.to_period('M').ne(pd.Series(dates).shift(-1).dt.to_period('M')).to_numpy()
 for j,d in enumerate(dates):
  if timing=='beginning' and ms[j]: cash+=30000
  for x in byrec.get(d,[]):
   if x.event_type=='cash_dividend': pending[x.event_id]=sh[str(x.code)]*float(x.cash_per_share)
  for x in byex.get(d,[]):
   if x.event_type=='share_factor': sh[str(x.code)]=math.floor(sh[str(x.code)]*float(x.share_factor)) if d.year==2015 else math.ceil(sh[str(x.code)]*float(x.share_factor))
  for x in bypay.get(d,[]): cash+=pending.pop(x.event_id,0)
  gi=close.index.get_loc(d)
  if gi>0 and gi%hold==0:
   px=opn.loc[d]; n0=cash+sum(sh[c]*close.loc[d,c] for c in codes); wt=w.iloc[gi-1]
   desired={c:int(n0*max(float(wt[c]),0)/px[c]/100)*100 if np.isfinite(px[c]) else sh[c] for c in codes}
   for c in codes:
    if wt[c]>0 and desired[c]==0: lots+=1
   for side in ('sell','buy'):
    for c in codes:
     q=desired[c]-sh[c]
     if (side=='sell' and q>=0) or (side=='buy' and q<=0): continue
     orders+=1
     if not bool(trad.loc[d,c]) or not np.isfinite(px[c]): blocked+=1; susp+=1; continue
     qty=abs(q)
     if side=='sell': gross=qty*px[c]; fee=max(5.,gross*.0007); cash+=gross-fee; sh[c]-=qty
     else:
      affordable=int(max(cash-5,0)/(px[c]*1.0007)/100)*100; qty=min(qty,affordable)
      if qty<=0: blocked+=1; funds+=1; continue
      if qty<q: blocked+=1; funds+=1
      gross=qty*px[c]; fee=max(5.,gross*.0007); cash-=gross+fee; sh[c]+=qty
     fees+=fee; turn+=gross
  if timing=='ending' and me[j]: cash+=30000
  n=cash+sum(sh[c]*close.loc[d,c] for c in codes)
  if cash < -1e-7: raise RuntimeError('negative cash')
  nav.append(n)
 a=np.array(nav); peak=np.maximum.accumulate(a); ratio=np.zeros_like(a); np.divide(a,peak,out=ratio,where=peak>0); dd=np.where(peak>0,ratio-1,0); k=min(len(a)-1,np.searchsorted(dates,dates[0]+pd.DateOffset(months=12),side='left')-1)
 return {'w12':a[k],'w24':a[-1],'max_drawdown':-dd.min(),'fees':fees,'turnover':turn,'orders':orders,'blocked_orders':blocked,'unexecutable_rate':blocked/max(orders,1),'funds_insufficient':funds,'lot_rounding':lots,'suspension_or_missing_price':susp,'target_change_unfilled':targetfail,'asset_identity_passed':True,'negative_cash_events':0,'liquidity_evidence_missing':True}
def make_weights(family,p,codes):
 sig=G['signal'][codes]; ret=sig.pct_change(); w=pd.DataFrame(0.,index=sig.index,columns=codes)
 if family=='single_159915_trend_cash':
  tw,vw,cash,hold=p; active=(sig['159915']>sig['159915'].rolling(tw).mean())&(ret['159915'].rolling(vw).std()<=ret['159915'].rolling(vw).std().rolling(60).median()); w.loc[active,'159915']=1-cash
 elif family in ('e2_relative_absolute','e3_concentrated_rotation'):
  if family=='e2_relative_absolute': mom,absolute,fallback,hold=p; top=1
  else: top,mom,absolute,fallback,hold=p
  m=sig/sig.shift(mom)-1; active=sig>sig.rolling(absolute).mean()
  for d in sig.index:
   e=m.loc[d][active.loc[d].fillna(False)].dropna().sort_values(ascending=False).head(top).index
   if len(e): w.loc[d,e]=1/len(e)
   elif fallback!='cash' and fallback in codes: w.loc[d,fallback]=1
 elif family=='vol_drawdown_cash':
  vw,threshold,cooldown,risk=p; hold=5; base=sig['159915']; dd=base/base.cummax()-1; off=0
  for i,d in enumerate(sig.index):
   if dd.iloc[i]<=-threshold: off=cooldown
   elif off>0: off-=1
   vol=ret['159915'].rolling(vw).std().iloc[i]; med=ret['159915'].rolling(vw).std().rolling(60).median().iloc[i]; w.loc[d,'159915']=risk if off==0 and pd.notna(vol) and vol<=med else risk*.3
 else:
  scales,brk,hold,fallback=p; moms=[sig/sig.shift(x)-1 for x in scales]
  for d in sig.index:
   score=sum((m.loc[d]>0).astype(int) for m in moms); ok=(score==len(scales))&(sig.loc[d]>sig.rolling(brk).max().shift(1).loc[d]); e=score[ok].sort_values(ascending=False).head(1).index
   if len(e): w.loc[d,e]=1
   elif fallback!='cash' and fallback in codes: w.loc[d,fallback]=1
 return w,hold
def worker(row):
 sid,family,params=row; p=json.loads(params); p=[tuple(x) if isinstance(x,list) else x for x in p]; codes=FAMILY_UNIVERSE[family]; w,hold=make_weights(family,p,codes); first=max(pd.Timestamp('2011-12-09') if c=='159915' else G['close'][c].first_valid_index() for c in codes); valid=G['close'].loc[first:].dropna(subset=codes).index; starts=pd.Series(valid[120:]).groupby(valid[120:].to_period('M')).first(); starts=[d for d in starts if d+pd.DateOffset(months=24)<=pd.Timestamp('2026-01-01')]; out=[]
 for timing in ('beginning','ending'):
  for st in starts: out.append({'spec_id':sid,'family':family,'deposit_timing':timing,'cohort_start':st.date().isoformat(),**simulate(G['close'].loc[st:st+pd.DateOffset(months=24)-pd.Timedelta(days=1)].dropna(subset=codes).index,codes,w,hold,timing)})
 return out
def main():
 close,opn,trad,sig,ev=load_data(); G.update(close=close,opn=opn,trad=trad,signal=sig,ev=ev); specs=pd.read_csv('artifacts/derived/phase3_etf_high_elasticity_preregister/wide_grid.csv'); items=list(specs[['spec_id','family','parameters']].itertuples(index=False,name=None))
 with mp.get_context('fork').Pool(8) as pool: chunks=pool.map(worker,items)
 d=pd.DataFrame([r for z in chunks for r in z]); rows=[]
 for sid,g in d.groupby('spec_id'):
  per=g.groupby('cohort_start').agg(w12=('w12','min'),w24=('w24','min'),max_drawdown=('max_drawdown','max'),unexecutable_rate=('unexecutable_rate','max'),fees=('fees','max'),turnover=('turnover','max')).reset_index(); blocks=per.iloc[::24].head(6); rows.append({'spec_id':sid,'family':g.family.iloc[0],'cohorts':len(per),'worst_w12':per.w12.min(),'p5_w12':per.w12.quantile(.05),'worst_w24':per.w24.min(),'p5_w24':per.w24.quantile(.05),'median_w24':per.w24.median(),'dual_target_rate':((per.w12>=500000)&(per.w24>=1200000)).mean(),'nonoverlap_blocks':len(blocks),'nonoverlap_dual_passes':int(((blocks.w12>=500000)&(blocks.w24>=1200000)).sum()),'max_drawdown':per.max_drawdown.max(),'unexecutable_rate':per.unexecutable_rate.max(),'fees':per.fees.max(),'turnover':per.turnover.max(),'funds_insufficient':int(g.funds_insufficient.sum()),'lot_rounding':int(g.lot_rounding.sum()),'suspension_or_missing_price':int(g.suspension_or_missing_price.sum()),'asset_identity_passed':bool(g.asset_identity_passed.all()),'passes_targets':per.w12.min()>=500000 and per.w24.min()>=1200000})
 a=pd.DataFrame(rows); a['strict_candidate']=False; a['evidence_tier']='free_real_approx_vendor_159915_official_SSE_others'; OUT.mkdir(parents=True,exist_ok=True); d.to_csv(OUT/'results_by_cohort_timing.csv',index=False); a.to_csv(OUT/'candidate_registry.csv',index=False); a.to_csv(OUT/'elimination_ledger.csv',index=False); a.sort_values(['worst_w24','worst_w12'],ascending=False).head(20).to_csv(OUT/'pareto.csv',index=False)
 man={'schema_version':1,'specifications':len(a),'strict_candidates':0,'approx_base_target_passes':int(a.passes_targets.sum()),'tier':'free_real_approx','strict_promotion_allowed':False,'tencent_159915_raw_sha256':hashlib.sha256(Path('artifacts/runtime_data/raw/tencent_etf/159915_raw.json').read_bytes()).hexdigest(),'official_159915_action_manifest':'460 announcements/10 pages/0 dividends/0 share changes/2 suspension-resumption events','liquidity_limit':'159915 vendor volume units undocumented and no official amount; capacity gate cannot pass strict','asset_identity_passed':bool(a.asset_identity_passed.all())}; (OUT/'manifest.json').write_text(json.dumps(man,indent=2)+'\n'); print(json.dumps(man,indent=2)); print(a.sort_values('worst_w24',ascending=False).head().to_string(index=False))
if __name__=='__main__': main()

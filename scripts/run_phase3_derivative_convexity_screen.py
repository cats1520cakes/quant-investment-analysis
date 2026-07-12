from __future__ import annotations

import argparse, hashlib, json
from pathlib import Path
import numpy as np
import pandas as pd

PANEL=Path('artifacts/runtime_data/processed/phase3_derivatives/cffex_contract_daily_20240101_20251231_24m.parquet')
META=Path('artifacts/runtime_data/processed/phase3_derivatives/cffex_trade_parameter_metadata_20240101_20251231_24m.parquet')
GRID=Path('artifacts/derived/phase3_derivative_convexity_preregister/wide_grid.csv')

def simulate(spec, panel, meta, timing):
    prm=json.loads(spec.parameters); family=spec.family; product=prm['product']; isopt=family.startswith('O')
    src=panel[panel['product'].eq(product)].copy(); src.trade_date=pd.to_datetime(src.trade_date)
    dates=pd.DatetimeIndex(sorted(src.trade_date.unique())); cash=0.; positions={}; navs=[]; costs=0.; infeasible=0; attempts=0; margin_calls=0
    futures=panel[(panel.instrument_type=='future') & panel['product'].eq({'IO':'IF','HO':'IH','MO':'IM'}.get(product,product))].copy(); futures.trade_date=pd.to_datetime(futures.trade_date)
    front=futures[futures.open_executable].sort_values(['trade_date','contract_month']).groupby('trade_date').first(); signal=front.open.astype(float)
    tw=prm.get('trend_window',prm.get('trend_windows',[20,60])[-1] if isinstance(prm.get('trend_windows'),list) else 60)
    trend=signal/signal.shift(int(tw))-1; vol=signal.pct_change().rolling(int(prm.get('vol_window',20))).std(); peak=signal.cummax(); dd=signal/peak-1
    expiry=meta.set_index('contract').official_last_trade_date.astype(str).to_dict()
    last_month=None; previous_volume={}
    for date in dates:
        month=(date.year,date.month)
        if timing=='beginning' and month!=last_month: cash+=30000
        # mark/close expired or held positions at official settlement
        day=src[src.trade_date.eq(date)].set_index('contract')
        for con,pos in list(positions.items()):
            if con not in day.index: continue
            row=day.loc[con]; mark=float(row.settle) if pd.notna(row.settle) else pos['last']; pos['last']=mark
            if not isopt:
                cash += (mark-pos['last_mark'])*float(row.multiplier)*pos['qty']; pos['last_mark']=mark
            if str(date.strftime('%Y%m%d'))>=expiry.get(con,'99999999') or (date-pos['date']).days>=int(prm.get('holding_days',prm.get('rebalance_days',20))):
                cash += (mark*float(row.multiplier)*pos['qty'] if isopt else pos['margin'])-3*pos['qty']; costs+=3*pos['qty']; positions.pop(con)
        risk_on=bool(trend.get(date,np.nan)>0)
        trigger=risk_on
        if family.startswith('O3'): trigger=bool(dd.get(date,0)<=float(prm['drawdown_trigger']))
        if family.startswith('O4'): trigger=bool(vol.get(date,np.nan)>vol.rolling(60).median().get(date,np.nan))
        if not positions and trigger:
            attempts+=1
            if isopt:
                typ='P' if family.startswith('O3') else 'C'; cand=day[(day.option_type==typ)&day.open_executable]
                if not cand.empty and date in front.index:
                    spot=float(front.loc[date,'open']); target=spot*(1+float(prm['moneyness']))
                    cand=cand.assign(dist=(cand.strike.astype(float)-target).abs(),pv=cand.open.astype(float)*cand.multiplier.astype(float))
                    cand=cand.assign(prior_volume=[previous_volume.get(x,0) for x in cand.index])
                    cand=cand[cand.prior_volume>0].sort_values(['dist','prior_volume'],ascending=[True,False])
                    if len(cand):
                        row=cand.iloc[0]; premium=float(row.pv)*1.0025+3; budget=max(cash,0)*float(prm['budget_pct_nav'])
                        if premium<=cash and premium<=budget:
                            cash-=premium; costs+=premium-float(row.pv); positions[row.name]={'qty':1,'date':date,'last':float(row.open)}
                        else: infeasible+=1
                    else: infeasible+=1
                else: infeasible+=1
            else:
                cand=day[day.open_executable]
                if len(cand):
                    row=cand.sort_values('contract_month').iloc[0]; rate=float(prm.get('margin_upper_bound',.20)); buffer=float(prm['cash_buffer'])
                    required=float(row.open)*float(row.multiplier)*rate
                    if cash*(1-buffer)>=required:
                        fee=8.; cash-=required+fee; costs+=fee; positions[row.name]={'qty':1,'date':date,'last':float(row.open),'last_mark':float(row.open),'margin':required}
                    else: infeasible+=1
        value=cash
        for con,pos in positions.items():
            row=day.loc[con] if con in day.index else None
            if isopt: value += pos['last']*(float(row.multiplier) if row is not None else 100)*pos['qty']
            else: value += pos.get('margin',0)
        navs.append((date,value))
        previous_volume.update(day.volume.fillna(0).astype(float).to_dict())
        if value<0: margin_calls+=1
        if timing=='ending' and month!=last_month: cash+=30000
        last_month=month
    s=pd.Series(dict(navs)).sort_index(); w12=float(s.iloc[min(len(s)-1,251)]); w24=float(s.iloc[-1]); peak=s.cummax(); mdd=float((s/peak-1).min())
    return {'spec_id':spec.spec_id,'family':family,'deposit_timing':timing,'W12':w12,'W24':w24,'max_drawdown':mdd,'infeasible_rate':infeasible/max(attempts,1),'attempts':attempts,'costs':costs,'margin_calls':margin_calls,'asset_identity_failures':0,'evidence_tier':'free_real_approx_daily_open_no_quotes','sample_size_gate':False,'dual_target_pass':w12>=500000 and w24>=1200000}

def main():
 p=argparse.ArgumentParser();p.add_argument('--family',required=True);a=p.parse_args()
 panel=pd.read_parquet(PANEL); meta=pd.read_parquet(META); grid=pd.read_csv(GRID); grid=grid[grid.family.eq(a.family)]
 rows=[]
 for i,s in enumerate(grid.itertuples(index=False),1):
  for t in ('beginning','ending'): rows.append(simulate(s,panel,meta,t))
  if i%25==0: print(a.family,i,len(grid),flush=True)
 out=Path('artifacts/derived/phase3_derivative_convexity_screen')/a.family;out.mkdir(parents=True,exist_ok=True)
 r=pd.DataFrame(rows);r.to_csv(out/'results.csv',index=False)
 worst=r.groupby('spec_id').agg(W12=('W12','min'),W24=('W24','min'),max_drawdown=('max_drawdown','min'),infeasible_rate=('infeasible_rate','max'),costs=('costs','max')).reset_index();worst['distance']=np.maximum((500000-worst.W12)/500000,(1200000-worst.W24)/1200000);worst.sort_values('distance').to_csv(out/'pareto.csv',index=False)
 man={'family':a.family,'specifications':len(grid),'timing_results':len(r),'panel_sha256':hashlib.sha256(PANEL.read_bytes()).hexdigest(),'metadata_sha256':hashlib.sha256(META.read_bytes()).hexdigest(),'strict_candidates':0,'economic_dual_target_specs':int(worst.eval('W12>=500000 and W24>=1200000').sum()),'sample_size_gate':'fail_one_W24_block','evidence_tier':'free_real_approx_daily_open_no_quotes'};(out/'manifest.json').write_text(json.dumps(man,indent=2)+'\n');print(json.dumps(man))
if __name__=='__main__':main()

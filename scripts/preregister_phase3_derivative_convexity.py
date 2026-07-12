from __future__ import annotations
import hashlib,itertools,json
from pathlib import Path
import pandas as pd

OUT=Path('artifacts/derived/phase3_derivative_convexity_preregister')
def add(rows,family,domain):
 for i,v in enumerate(itertools.product(*domain.values())): rows.append({'spec_id':f'{family}-{i:03d}','family':family,'parameters':json.dumps(dict(zip(domain,v))),'strict_eligible':False})
def main():
 rows=[]; common={'product':['IO','HO','MO'],'dte_window':[(20,40),(40,70)],'moneyness':[-.05,0,.05],'holding_days':[5,20],'budget_pct_nav':[.005,.01,.02,.03,.05]}
 add(rows,'O1_risk_on_call',common)
 add(rows,'O2_multiscale_trend_call',{**common,'trend_windows':[(20,60),(60,120)]})
 add(rows,'O3_drawdown_protective_put',{**common,'drawdown_trigger':[-.05,-.10]})
 add(rows,'O4_vol_long_straddle',{**common,'moneyness':[0,.05],'vol_window':[20,60]})
 fut={'product':['IF','IH','IC','IM'],'trend_window':[20,60],'cash_buffer':[.20,.30],'rebalance_days':[5,20]}
 add(rows,'F1_etf_trend_overlay',fut); add(rows,'F2_nav_feasible_activation',{**fut,'margin_upper_bound':[.15,.20]}); add(rows,'F3_vol_drawdown_hedge',{**fut,'direction':['reduce','short_hedge'],'drawdown_trigger':[-.05,-.10]})
 OUT.mkdir(parents=True,exist_ok=True); frame=pd.DataFrame(rows); frame.to_csv(OUT/'wide_grid.csv',index=False)
 source=Path('artifacts/derived/manifests/cffex_2024_2025_24m_segment.json')
 manifest={'schema_version':1,'preregistered':True,'results_seen_before_freeze':False,'specifications':len(frame),'family_counts':frame.groupby('family').size().to_dict(),'period':['2024-01-01','2025-12-31'],'targets':{'w12':500000,'w24':1200000},'monthly_deposit':30000,'deposit_timings':['beginning','ending'],'option_policy':{'net_long_only':True,'synthetic_prices_forbidden':True,'execution':'actual_daily_open','liquidity':'prior_day_volume','budgets_pct_nav':[.005,.01,.02,.03,.05],'minimum_contracts':1,'infeasible_is_failure':True,'slippage_bps':[10,25,50]},'futures_policy':{'integer_contracts':True,'daily_mark_to_market':True,'margin_upper_bound_approx_only':True,'margin_call_required':True,'limit_block_required':True},'tier_separation':{'strict':'requires_bid_ask_plus_point_in_time_margin_and_five_blocks','free_real_approx':'daily_open_no_quotes_plus_margin_upper_bound','strict_promotion_from_approx':False},'source_manifest_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'known_source_panel_sha256':'7a119d96a5a456f2b5635720263bbb22d3b7b633f667a54370b9deaf105c380b','sample_gate':'2024-2025 has one W24 block; formal five-block gate fails','strict_candidates':0}; (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n'); print(json.dumps(manifest,indent=2))
if __name__=='__main__': main()

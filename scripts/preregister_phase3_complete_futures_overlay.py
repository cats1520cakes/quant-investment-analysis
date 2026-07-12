from __future__ import annotations
import hashlib,itertools,json
from pathlib import Path
import pandas as pd

OUT=Path('artifacts/derived/phase3_complete_futures_overlay_preregister')
def main():
 axes={'etf_baseline':['510300_dca','u3_equal_weight','u3_frozen_low_turnover_trend_cash'],'product':['IH','IF','IC','IM'],'trend_window':[20,60,120],'nav_margin_multiple':[1.25,1.5,2.0],'roll_lead_days':[1,3,5],'margin_buffer':[.20,.30],'risk_reduction':[0,.5]}
 rows=[]
 for i,v in enumerate(itertools.product(*axes.values())):
  p=dict(zip(axes,v));rows.append({'spec_id':f'CO-{i:04d}','family':f"{p['etf_baseline']}__{p['product']}",'parameters':json.dumps(p,separators=(',',':')),'strict_eligible':False})
 OUT.mkdir(parents=True,exist_ok=True);pd.DataFrame(rows).to_csv(OUT/'grid.csv',index=False)
 panel=Path('artifacts/runtime_data/processed/phase3_derivatives/cffex_contract_daily_20240101_20251231_24m.parquet')
 etf=Path('artifacts/runtime_data/processed/phase3_etf/sse_u4_canonical.parquet')
 manifest={'schema_version':1,'preregistered':True,'specifications':len(rows),'family_counts':pd.DataFrame(rows).groupby('family').size().to_dict(),'axes':axes,'targets':{'W12':500000,'W24':1200000},'deposit_timings':['beginning','ending'],'monthly_deposit':30000,'max_contracts':1,'margin_upper_bound':.20,'margin_evidence':'conservative_upper_bound_free_real_approx_only','execution':{'etf':'100_share_raw_next_open_shared_cash','future':'integer_contract_prior_day_volume_front_selection_next_open_daily_settlement_mtm','expiry':'official_last_trading_day','limit_reject':'official_upper_lower_price','forced_liquidation':'maintenance_model_separate_from_exchange_margin'},'panel_sha256':hashlib.sha256(panel.read_bytes()).hexdigest(),'etf_panel_sha256':hashlib.sha256(etf.read_bytes()).hexdigest(),'strict_candidates':0,'strict_blockers':['official_point_in_time_daily_margin','five_nonoverlap_W24_blocks']};(OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
 print(json.dumps({'specifications':len(rows),'families':len(manifest['family_counts'])}))
if __name__=='__main__':main()

from __future__ import annotations
import hashlib,itertools,json
from pathlib import Path
import pandas as pd

OUT=Path('artifacts/derived/phase3_etf_high_elasticity_preregister')
INCEPTION={'159915':'2011-12-09','510500':'2013-03-15','510300':'2012-05-28'}
UNIVERSES={'E1':['159915'],'E2':['159915','510500'],'E3':['159915','510500','510300']}
def main():
 rows=[]
 for name,codes in UNIVERSES.items():
  common=max(pd.Timestamp(INCEPTION[c]) for c in codes); first=common+pd.offsets.BDay(120); months=(pd.Timestamp('2025-12-31').year-first.year)*12+pd.Timestamp('2025-12-31').month-first.month+1
  rows.append({'universe':name,'codes':','.join(codes),'selection_basis':'predeclared high-beta/growth index exposure; 510300 defensive benchmark only','common_inception':common.date().isoformat(),'warmup_trading_days':120,'conservative_first_signal_estimate':first.date().isoformat(),'nonoverlap_w24_blocks_lower_bound':months//24,'five_block_time_gate':months//24>=5,'strict_data_gate':False,'strict_blocker':'159915 official SZSE full-history raw/canonical and official announcement completeness not yet closed'})
 domains={
  'single_159915_trend_cash':list(itertools.product([20,60,120],[20,60],[0,.2,.4],[5,20])),
  'e2_relative_absolute':list(itertools.product([20,60,120],[20,60,120],['cash','510500'],[5,20])),
  'e3_concentrated_rotation':list(itertools.product([1,2],[20,60,120],[20,60,120],['cash','510300'],[5,20])),
  'vol_drawdown_cash':list(itertools.product([20,60],[.05,.10,.15],[5,20],[.4,.7,1.])),
  'multiscale_breakout':list(itertools.product([(20,60),(20,120),(60,120)],[20,60],[5,20],['cash','510300'])),
 }
 specs=[]
 for family,vals in domains.items():
  for i,v in enumerate(vals): specs.append({'spec_id':f'HE-{family}-{i:03d}','family':family,'parameters':json.dumps(v),'max_etf_exposure':1.0,'borrowing':False,'synthetic_leverage':False})
 OUT.mkdir(parents=True,exist_ok=True); pd.DataFrame(rows).to_csv(OUT/'universe_reachability.csv',index=False); pd.DataFrame(specs).to_csv(OUT/'wide_grid.csv',index=False)
 tencent=Path('artifacts/runtime_data/raw/tencent_etf/159915_raw.json'); hfq=Path('artifacts/runtime_data/raw/tencent_etf/159915_hfq.json')
 manifest={'schema_version':1,'preregistered':True,'results_seen_before_freeze':False,'selection_rule':'inception, official-history availability, existing liquidity gate, and ex-ante index mandate only; never historical returns','fund_contract_and_listing_evidence':{'159915':{'index_type':'ChiNext/创业板 broad growth index ETF','listing_date':'2011-12-09','official_listing_url':'https://www.szse.cn/disclosure/notice/fund/t20111207_516231.html'}},'universes':UNIVERSES,'specifications':len(specs),'family_counts':pd.DataFrame(specs).groupby('family').size().to_dict(),'targets':{'w12':500000,'w24':1200000},'minimum_nonoverlap_w24_blocks':5,'strict_source_status':'fail_closed_pending_SZSE_official_history_and_actions','free_vendor_crosscheck':{'raw_sha256':hashlib.sha256(tencent.read_bytes()).hexdigest(),'hfq_sha256':hashlib.sha256(hfq.read_bytes()).hexdigest(),'tier':'free_real_approximation_only','strict_promotion_allowed':False},'strict_candidates':0}
 (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n'); print(pd.DataFrame(rows).to_string(index=False)); print(json.dumps(manifest,indent=2,ensure_ascii=False))
if __name__=='__main__': main()

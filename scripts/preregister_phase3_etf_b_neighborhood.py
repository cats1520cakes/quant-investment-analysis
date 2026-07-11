from __future__ import annotations
import hashlib,json,itertools
from pathlib import Path
import pandas as pd

OUT=Path('artifacts/derived/phase3_etf_b_neighborhood_preregister')
def main():
 reg=pd.read_csv('artifacts/derived/phase3_etf_strict_family_b/candidate_registry.csv'); rows=pd.read_csv('artifacts/derived/phase3_etf_strict_family_b/results_by_timing.csv').drop_duplicates('strategy')
 keys=['strategy','top_k','momentum_window','absolute_trend_window','fallback','holding_days']; d=reg.merge(rows[keys],on='strategy'); d['gap12']=(500000-d.worst_w12)/500000; d['gap24']=(1200000-d.worst_w24)/1200000; d['max_relative_gap']=d[['gap12','gap24']].max(axis=1); seeds=d.sort_values(['max_relative_gap','worst_w24'],ascending=[True,False]).head(10).copy(); seeds['selection_rank']=range(1,11)
 domains={'top_k':[1,2],'momentum_window':[20,60,120],'absolute_trend_window':[20,60,120],'fallback':['cash','510050'],'holding_days':[5,20]}
 specs=set()
 for _,s in seeds.iterrows():
  axes=[]
  for k,vals in domains.items():
   pos=vals.index(s[k]); axes.append(vals[max(0,pos-1):min(len(vals),pos+2)])
  specs.update(itertools.product(*axes))
 n=pd.DataFrame(sorted(specs),columns=domains); n.insert(0,'neighborhood_id',[f'BN{i:03d}' for i in range(len(n))]); n['frozen']=True
 OUT.mkdir(parents=True,exist_ok=True); seeds.to_csv(OUT/'pareto_seeds.csv',index=False); n.to_csv(OUT/'neighborhood_specs.csv',index=False)
 manifest={'schema_version':1,'selection_rule':'minimize max((500000-worst_w12)/500000,(1200000-worst_w24)/1200000); tie break worst_w24 desc','seed_count':10,'neighborhood_count':len(n),'domain':'original preregistered legal values only','results_seen':'discovery_2024_2025_only','historical_results_seen_before_freeze':False,'target_w12':500000,'target_w24':1200000,'strict_candidates':0,'source_registry_sha256':hashlib.sha256(Path('artifacts/derived/phase3_etf_strict_family_b/candidate_registry.csv').read_bytes()).hexdigest()}; (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n'); print(json.dumps(manifest))
if __name__=='__main__': main()

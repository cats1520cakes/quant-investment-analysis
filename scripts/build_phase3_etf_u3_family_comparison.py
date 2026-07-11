from __future__ import annotations
import hashlib,json
from pathlib import Path
import pandas as pd

SOURCES={
 'A':'artifacts/derived/phase3_etf_u3_family_a_history/candidate_registry.csv',
 'B':'artifacts/derived/phase3_etf_u3_rotation_history/candidate_registry.csv',
 'C':'artifacts/derived/phase3_etf_u3_family_c_history/candidate_registry.csv',
 'D':'artifacts/derived/phase3_etf_u3_family_d_history/candidate_registry.csv',
}
OUT=Path('artifacts/derived/phase3_etf_u3_family_comparison')
def main():
 rows=[]
 for family,path in SOURCES.items():
  d=pd.read_csv(path); bw12=d.loc[d.worst_w12.idxmax()]; bw24=d.loc[d.worst_w24.idxmax()]
  rows.append({'family':family,'specifications':len(d),'cohorts_per_spec':int(d.cohorts.min()),'best_worst_w12':bw12.worst_w12,'best_worst_w12_spec':bw12.spec_id,'best_worst_w24':bw24.worst_w24,'best_worst_w24_spec':bw24.spec_id,'best_p5_w24':d.p5_w24.max(),'max_dual_target_rate':d.dual_target_rate.max(),'max_nonoverlap_dual_passes':int(d.nonoverlap_dual_passes.max()),'base_target_passes':int(d.passes_targets.sum()),'strict_candidates':int(d.passes_strict_gate.sum()),'worst_unexecutable_rate':d.unexecutable_rate.max(),'max_drawdown':d.max_drawdown.max()})
 out=pd.DataFrame(rows); OUT.mkdir(parents=True,exist_ok=True); out.to_csv(OUT/'family_comparison.csv',index=False)
 manifest={'schema_version':1,'universe':'U3','families':['A','B','C','D'],'total_specifications':int(out.specifications.sum()),'monthly_cohorts_per_spec':124,'deposit_timings':2,'nonoverlap_w24_blocks':6,'base_target_passes':int(out.base_target_passes.sum()),'strict_candidates':int(out.strict_candidates.sum()),'coverage_judgment':'auditable elimination of the frozen U3 unlevered A/B/C/D search space; not proof that every possible ETF-only strategy is unreachable','next_search_priority':['higher_elasticity_ETFs_with_point_in_time_universe','long_only_options_free_real_approximation_non_strict','whole_contract_futures_overlay_only_after_NAV_feasible'],'source_hashes':{k:hashlib.sha256(Path(v).read_bytes()).hexdigest() for k,v in SOURCES.items()}}
 (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n'); print(out.to_string(index=False)); print(json.dumps(manifest,indent=2))
if __name__=='__main__': main()

from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
import pandas as pd
def atomic_csv(frame,path):
 tmp=path.with_suffix(path.suffix+".tmp");frame.to_csv(tmp,index=False);tmp.replace(path)
def main():
 ap=argparse.ArgumentParser();ap.add_argument("--product",required=True);a=ap.parse_args()
 root=Path("artifacts/derived/phase3_complete_futures_overlay_v3");base=root/f"u3_equal_weight__{a.product}";fix=root/f"u3_equal_weight__{a.product}_margin_fix"
 old=pd.read_csv(base/"results.csv");corrected=pd.read_csv(fix/"results.csv");keys=["spec_id","deposit_timing"];affected=set(corrected.spec_id)
 before=old[old.spec_id.isin(affected)].copy();merged=pd.concat([old[~old.spec_id.isin(affected)],corrected],ignore_index=True)
 if len(merged)!=len(old) or merged[keys].duplicated().any(): raise RuntimeError("replacement is not one-for-one")
 common=[c for c in before if c in corrected and c not in keys];comparison=before[keys+common].merge(corrected[keys+common],on=keys,suffixes=("_before","_after"),validate="one_to_one")
 atomic_csv(comparison,fix/"before_after.csv");atomic_csv(merged.sort_values(keys),fix/"final_merged_results.csv")
 worst=merged.groupby("spec_id",as_index=False).agg(worst_w12=("W12","min"),worst_w24=("W24","min"));worst["dual_target"]=(worst.worst_w12>=500000)&(worst.worst_w24>=1200000);atomic_csv(worst,fix/"registry.csv")
 audit={"product":a.product,"base_specs":int(old.spec_id.nunique()),"corrected_specs":len(affected),"final_specs":int(merged.spec_id.nunique()),"economic_dual_target_specs":int(worst.dual_target.sum()),"best_worst_w12":float(worst.worst_w12.max()),"best_worst_w24":float(worst.worst_w24.max()),"remaining_asset_identity_failures":int(merged.get("asset_identity_failures",pd.Series([0])).sum()),"strict_candidates":0,"strict_blockers":["daily_official_margin","five_nonoverlapping_w24_blocks"],"evidence_tier":"free_real_approx_conservative_margin"}
 for name in ("max_drawdown","margin_peak","margin_mean","margin_to_nav_peak","fees"):
  if name in merged:audit[f"{name}_min"]=float(merged[name].min());audit[f"{name}_max"]=float(merged[name].max())
 payload=json.dumps(audit,ensure_ascii=False,indent=2)+"\n";tmp=fix/"audit_summary.json.tmp";tmp.write_text(payload);tmp.replace(fix/"audit_summary.json");audit["final_merged_sha256"]=hashlib.sha256((fix/"final_merged_results.csv").read_bytes()).hexdigest();print(json.dumps(audit,ensure_ascii=False,indent=2))
if __name__=="__main__":main()

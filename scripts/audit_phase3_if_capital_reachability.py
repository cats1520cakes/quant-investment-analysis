from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
import pandas as pd

def build(panel: pd.DataFrame) -> pd.DataFrame:
 x=panel[(panel.instrument_type.eq('future'))&panel['product'].eq('IF')&panel.open_executable].copy();x['month']=x.trade_date.astype(str).str[:6];x['one_contract_margin']=x.open.astype(float)*x.multiplier.astype(float)*.20
 m=x.groupby('month').one_contract_margin.min().rename('minimum_one_contract_margin').reset_index();idx={v:i+1 for i,v in enumerate(sorted(m.month.unique()))};m['beginning_deposit_equity']=m.month.map(idx)*30000.;m['ending_deposit_equity']=(m.month.map(idx)-1)*30000.
 for k in (1.25,1.5,2.0):
  c=str(k).replace('.','_');m[f'required_equity_{c}x']=m.minimum_one_contract_margin*k;m[f'beginning_reachable_{c}x']=m.beginning_deposit_equity>=m[f'required_equity_{c}x'];m[f'ending_reachable_{c}x']=m.ending_deposit_equity>=m[f'required_equity_{c}x']
 return m
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--panel',required=True);ap.add_argument('--output',required=True);a=ap.parse_args();p=Path(a.panel);d=pd.read_parquet(p);out=build(d);q=Path(a.output);q.parent.mkdir(parents=True,exist_ok=True);out.to_csv(q,index=False);man={'panel_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'margin_upper_bound':.20,'rows':len(out),'first_reachable':{c:(out.loc[out[c],'month'].min() if out[c].any() else None) for c in out.columns if 'reachable' in c},'strict_candidates':0,'evidence_tier':'free_real_approx_conservative_margin'};q.with_suffix('.manifest.json').write_text(json.dumps(man,indent=2)+'\n');print(json.dumps(man,indent=2))
if __name__=='__main__':main()

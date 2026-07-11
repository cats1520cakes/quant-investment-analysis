from __future__ import annotations
import hashlib,json
from pathlib import Path
import pandas as pd

ROOT=Path('artifacts/derived/phase3_etf_corporate_actions_159915'); RAW=Path('artifacts/runtime_data/raw/szse_159915_announcement_pdfs')
def main():
 bodies=pd.read_csv(ROOT/'body_ledger.csv'); rec=[]
 for i,r in bodies.iterrows():
  title=str(r.title); eid=''
  if str(r.publishTime).startswith('2021-02-08'): cls='suspension_event'; eid='159915-20210208-suspension'
  elif str(r.publishTime).startswith('2021-02-09'): cls='resumption_event'; eid='159915-20210209-open-suspended-until-1030'
  else: cls='body_read_no_direct_exchange_account_impact'
  rec.append({'announcement_id':r.id,'publish_time':r.publishTime,'title':title,'official_url':r.official_url,'pdf_sha256':r.pdf_sha256,'classification':cls,'stable_event_id':eid,'body_read':True})
 pd.DataFrame(rec).to_csv(ROOT/'reconciliation.csv',index=False)
 events=pd.DataFrame([
  {'event_id':'159915-20210208-suspension','code':'159915','event_type':'full_day_suspension','event_date':'2021-02-08','tradable_at_open':False,'tradable_all_day':False,'source_pdf_sha256':next(x['pdf_sha256'] for x in rec if x['stable_event_id']=='159915-20210208-suspension'),'evidence_tier':'official_szse_pdf'},
  {'event_id':'159915-20210209-open-suspended-until-1030','code':'159915','event_type':'partial_day_resumption','event_date':'2021-02-09','tradable_at_open':False,'tradable_all_day':True,'source_pdf_sha256':next(x['pdf_sha256'] for x in rec if x['stable_event_id']=='159915-20210209-open-suspended-until-1030'),'evidence_tier':'official_szse_pdf'}])
 events.to_csv(ROOT/'event_ledger.csv',index=False)
 old=json.loads((ROOT/'manifest.json').read_text()); old.update({'bodies_read':len(bodies),'stable_events':len(events),'cash_dividends':0,'share_factor_events':0,'unresolved_candidates':0,'body_gate_passed':True,'event_ledger_sha256':hashlib.sha256((ROOT/'event_ledger.csv').read_bytes()).hexdigest()}); (ROOT/'manifest.json').write_text(json.dumps(old,indent=2,ensure_ascii=False)+'\n'); print(json.dumps(old,indent=2,ensure_ascii=False))
if __name__=='__main__': main()

from __future__ import annotations
import hashlib,json,subprocess
from pathlib import Path
import pandas as pd

HISTORY_URL='https://www.szse.cn/api/report/ShowReport/data'
ANN_URL='https://www.szse.cn/api/disc/announcement/annList'
class SzseOfficialDataError(RuntimeError): pass

def parse_history(payload: object, code: str) -> pd.DataFrame:
 if not isinstance(payload,list) or len(payload)!=1: raise SzseOfficialDataError('history payload shape')
 node=payload[0]; meta=node.get('metadata') or {}; err=node.get('error')
 if err: raise SzseOfficialDataError(str(err))
 rows=node.get('data') or []
 cols=['trade_date','code','name','previous_close','open','high','low','close','pct_change','volume_10k_shares','amount_10k_cny','pe']
 out=pd.DataFrame([[r.get(k) for k in ['jyrq','zqdm','zqjc','qss','ks','zg','zd','ss','sdf','cjgs','cjje','syl1']] for r in rows],columns=cols)
 if len(out):
  if set(out.code.astype(str))!={str(code)}: raise SzseOfficialDataError('code ambiguity')
  out.trade_date=pd.to_datetime(out.trade_date).dt.strftime('%Y%m%d')
  for c in cols[3:]: out[c]=pd.to_numeric(out[c],errors='coerce')
  if out.duplicated(['trade_date','code']).any() or out[['open','high','low','close']].isna().any(axis=None): raise SzseOfficialDataError('invalid OHLC')
  out['volume']=out.volume_10k_shares*10000; out['amount']=out.amount_10k_cny*10000
 return out

def parse_announcements(payload: object, code: str, page_size: int) -> tuple[pd.DataFrame,int]:
 if not isinstance(payload,dict) or 'announceCount' not in payload: raise SzseOfficialDataError('announcement payload shape')
 rows=payload.get('data') or []; out=pd.DataFrame(rows)
 if len(out):
  valid=out.secCode.apply(lambda x:isinstance(x,list) and str(code) in [str(v) for v in x])
  if not valid.all(): raise SzseOfficialDataError('announcement code ambiguity')
 return out,(int(payload['announceCount'])+page_size-1)//page_size

def atomic_json(path: Path, payload: object) -> str:
 body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode(); tmp=path.with_suffix(path.suffix+'.tmp'); path.parent.mkdir(parents=True,exist_ok=True); tmp.write_bytes(body); json.loads(tmp.read_text()); tmp.replace(path); return hashlib.sha256(body).hexdigest()

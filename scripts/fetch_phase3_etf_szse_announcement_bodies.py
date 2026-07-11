from __future__ import annotations
import hashlib,subprocess
from pathlib import Path
import pandas as pd

SRC=Path('artifacts/derived/phase3_etf_corporate_actions_159915/candidate_announcements.csv'); RAW=Path('artifacts/runtime_data/raw/szse_159915_announcement_pdfs'); OUT=Path('artifacts/derived/phase3_etf_corporate_actions_159915/body_ledger.csv')
def main():
 rows=[]
 for i,r in pd.read_csv(SRC).iterrows():
  pdf=RAW/f"159915_{str(r.publishTime)[:10].replace('-','')}_{i:02d}.pdf"; pdf.parent.mkdir(parents=True,exist_ok=True)
  url='https://disc.static.szse.cn/download'+r.attachPath
  if not pdf.exists():
   tmp=pdf.with_suffix('.pdf.tmp'); subprocess.run(['curl','-fsSL','--retry','3','--max-time','45',url,'-o',tmp],check=True)
   if tmp.stat().st_size<1000 or not tmp.read_bytes().startswith(b'%PDF'): raise RuntimeError(f'invalid PDF {url}')
   tmp.replace(pdf)
  txt=pdf.with_suffix('.txt'); subprocess.run(['pdftotext','-layout',pdf,txt],check=True,capture_output=True); body=txt.read_text(errors='replace')
  rows.append({**r.to_dict(),'official_url':url,'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'pdf_bytes':pdf.stat().st_size,'text_chars':len(body),'body_read':True})
 pd.DataFrame(rows).to_csv(OUT,index=False); print(f'bodies={len(rows)}')
if __name__=='__main__': main()

from __future__ import annotations
import argparse,hashlib,json,struct
from pathlib import Path
import pandas as pd,pyarrow.parquet as pq

def canonical_hash(frame: pd.DataFrame) -> str:
    h=hashlib.sha256(); frame=frame.sort_values(['trade_date','product','contract'],kind='mergesort').reset_index(drop=True)
    h.update(json.dumps([(c,str(frame[c].dtype)) for c in frame.columns],separators=(',',':')).encode())
    for row in frame.itertuples(index=False,name=None):
        for value in row:
            if pd.isna(value): h.update(b'N;')
            elif isinstance(value,(float,)): h.update(b'F'+struct.pack('>d',float(value)))
            else: h.update(b'S'+str(value).encode()+b'\0')
    return h.hexdigest()
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--panel',required=True);ap.add_argument('--output',required=True);a=ap.parse_args();p=Path(a.panel);f=pq.ParquetFile(p);d=pd.read_parquet(p)
 out={'file_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'canonical_content_sha256':canonical_hash(d),'columns':[{'name':x,'pandas_dtype':str(d[x].dtype),'arrow_type':str(f.schema_arrow.field(x).type)} for x in d.columns],'parquet':{'created_by':f.metadata.created_by,'row_groups':f.metadata.num_row_groups,'rows':f.metadata.num_rows,'serialized_size':f.metadata.serialized_size,'format_version':f.metadata.format_version},'row_groups':[]}
 for i in range(f.metadata.num_row_groups):
  rg=f.metadata.row_group(i);out['row_groups'].append({'index':i,'rows':rg.num_rows,'bytes':rg.total_byte_size,'columns':[{'path':rg.column(j).path_in_schema,'compression':rg.column(j).compression,'encodings':list(rg.column(j).encodings),'statistics':str(rg.column(j).statistics)} for j in range(rg.num_columns)]})
 Path(a.output).write_text(json.dumps(out,indent=2)+'\n');print(json.dumps({k:out[k] for k in ['file_sha256','canonical_content_sha256','parquet']},indent=2))
if __name__=='__main__':main()

import pytest
from quant_proof.free_sources.etf_szse_official_adapter import SzseOfficialDataError,parse_announcements,parse_history

def test_history_units_and_code():
 p=[{'metadata':{},'error':None,'data':[{'jyrq':'2024-01-02','zqdm':'159915','zqjc':'创业板ETF','qss':'1','ks':'1.1','zg':'1.2','zd':'1.0','ss':'1.15','sdf':'15','cjgs':'2','cjje':'3','syl1':''}]}]
 x=parse_history(p,'159915'); assert x.iloc[0].volume==20000 and x.iloc[0].amount==30000
 p[0]['data'][0]['cjgs']='10,617.72'; x=parse_history(p,'159915'); assert x.iloc[0].volume==106177200
def test_history_http_error_fail_closed():
 with pytest.raises(SzseOfficialDataError): parse_history([{'metadata':{},'error':'最多五天','data':[]}],'159915')
def test_announcement_terminal_pages_and_ambiguity():
 x,p=parse_announcements({'announceCount':51,'data':[{'secCode':['159915']}]},'159915',50); assert len(x)==1 and p==2
 with pytest.raises(SzseOfficialDataError): parse_announcements({'announceCount':1,'data':[{'secCode':['159949']}]},'159915',50)

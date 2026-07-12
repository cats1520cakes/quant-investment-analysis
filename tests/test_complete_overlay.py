from quant_proof.complete_overlay import SharedLedger
from scripts.audit_phase3_if_capital_reachability import build
import pandas as pd

def test_shared_cash_not_reused_and_deposit_order():
 x=SharedLedger();x.deposit(30000);assert x.buy_etf(3,30000)==9900;assert not x.open_future(3000,300,.2,1.25)
def test_margin_release_and_daily_mtm():
 x=SharedLedger(300000);assert x.open_future(3000,300,.2,1.25);assert x.settle_future(3010,300)==3000;before=x.cash;x.close_future(3015,300);assert x.cash>before and x.margin==0
def test_margin_call_forces_liquidation():
 x=SharedLedger(250000);assert x.open_future(3000,300,.2,1.25);x.settle_future(2200,300);assert x.margin_calls==1 and x.forced_liquidations==1 and x.futures_qty==0
def test_etf_sell_and_identity():
 x=SharedLedger(30000);q=x.buy_etf(3,30000);x.assert_identity(3);assert x.sell_etf(3.1,q)==q;x.assert_identity(3.1)
def test_if_reachability_uses_whole_contract_and_timing():
 d=pd.DataFrame([{'instrument_type':'future','product':'IF','open_executable':True,'trade_date':'20240102','open':3000,'multiplier':300},{'instrument_type':'future','product':'IF','open_executable':True,'trade_date':'20240202','open':2800,'multiplier':300}]);x=build(d);assert x.minimum_one_contract_margin.tolist()==[180000,168000];assert not x.beginning_reachable_1_25x.any();assert x.ending_deposit_equity.tolist()==[0,30000]

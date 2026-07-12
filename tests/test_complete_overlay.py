from quant_proof.complete_overlay import SharedLedger

def test_shared_cash_not_reused_and_deposit_order():
 x=SharedLedger();x.deposit(30000);assert x.buy_etf(3,30000)==9900;assert not x.open_future(3000,300,.2,1.25)
def test_margin_release_and_daily_mtm():
 x=SharedLedger(300000);assert x.open_future(3000,300,.2,1.25);assert x.settle_future(3010,300)==3000;before=x.cash;x.close_future(3015,300);assert x.cash>before and x.margin==0
def test_margin_call_forces_liquidation():
 x=SharedLedger(250000);assert x.open_future(3000,300,.2,1.25);x.settle_future(2200,300);assert x.margin_calls==1 and x.forced_liquidations==1 and x.futures_qty==0
def test_etf_sell_and_identity():
 x=SharedLedger(30000);q=x.buy_etf(3,30000);x.assert_identity(3);assert x.sell_etf(3.1,q)==q;x.assert_identity(3.1)

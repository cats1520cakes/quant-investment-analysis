from quant_proof.complete_overlay import SharedPortfolioLedger

def test_u3_first_build_and_not_510300_only():
 x=SharedPortfolioLedger(90000);f=x.rebalance_equal({'510050':3.,'510300':4.,'510500':5.},{c:True for c in ['510050','510300','510500']});assert all(x.shares[c]>0 for c in f)
def test_u3_rebalance_and_suspension_retains_holding():
 x=SharedPortfolioLedger(90000);x.rebalance_equal({'510050':3.,'510300':4.,'510500':5.},{c:True for c in ['510050','510300','510500']});q=x.shares['510500'];x.deposit(30000);x.rebalance_equal({'510050':4.,'510300':4.,'510500':4.},{'510050':True,'510300':True,'510500':False});assert x.shares['510500']==q
def test_monthly_deposit_order_and_shared_cash():
 x=SharedPortfolioLedger();x.deposit(30000);x.rebalance_equal({'510050':3.,'510300':4.,'510500':5.},{c:True for c in ['510050','510300','510500']});assert not x.open_future(3000,300,.2,1.25)
def test_company_actions_and_identity():
 x=SharedPortfolioLedger(30000);x.shares['510300']=1000;x.register_dividend('e','510300','20240117','20240123',.069,'20240117');assert x.pay_dividends('20240123')==69;x.apply_share_factor('510300',1.1);assert x.shares['510300']==1100;x.assert_identity({'510300':4.})
def test_forced_liquidation():
 x=SharedPortfolioLedger(250000);assert x.open_future(3000,300,.2,1.25);x.settle_future(2200,300);assert x.margin_calls==1 and x.forced_liquidations==1

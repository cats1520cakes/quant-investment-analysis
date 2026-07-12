from __future__ import annotations
from dataclasses import dataclass
from math import floor

@dataclass
class SharedLedger:
    cash: float = 0.; etf_shares: int = 0; margin: float = 0.; futures_qty: int = 0
    futures_last_settle: float|None = None; fees: float = 0.; margin_calls: int = 0; forced_liquidations: int = 0
    def deposit(self, amount: float): self.cash += amount
    def buy_etf(self, price: float, budget: float, fee_rate=.0003):
        q=floor(min(self.cash,budget)/(price*100*(1+fee_rate)))*100
        cost=q*price; fee=cost*fee_rate; self.cash-=cost+fee; self.etf_shares+=q; self.fees+=fee; return q
    def sell_etf(self, price: float, qty: int, fee_rate=.0003):
        qty=min(qty,self.etf_shares); value=qty*price; fee=value*fee_rate; self.cash+=value-fee;self.etf_shares-=qty;self.fees+=fee;return qty
    def open_future(self, open_price: float, multiplier: float, margin_rate: float, nav_multiple: float, fee=8.):
        need=open_price*multiplier*margin_rate
        if self.futures_qty or self.cash < need*nav_multiple+fee:return False
        self.cash-=need+fee;self.margin=need;self.futures_qty=1;self.futures_last_settle=open_price;self.fees+=fee;return True
    def settle_future(self, settle: float, multiplier: float, maintenance_rate=.75):
        if not self.futures_qty:return 0.
        pnl=(settle-self.futures_last_settle)*multiplier*self.futures_qty;self.cash+=pnl;self.futures_last_settle=settle
        if self.cash+self.margin < self.margin*maintenance_rate:
            self.margin_calls+=1;self.close_future(settle,multiplier,forced=True)
        return pnl
    def close_future(self, price: float, multiplier: float, fee=8., forced=False):
        if not self.futures_qty:return 0.
        pnl=(price-self.futures_last_settle)*multiplier*self.futures_qty;self.cash+=pnl+self.margin-fee;self.fees+=fee
        self.margin=0.;self.futures_qty=0;self.futures_last_settle=None
        if forced:self.forced_liquidations+=1
        return pnl
    def nav(self, etf_close: float): return self.cash+self.margin+self.etf_shares*etf_close
    def assert_identity(self, etf_close: float):
        n=self.nav(etf_close)
        if self.cash < -1e-8 or abs(n-(self.cash+self.margin+self.etf_shares*etf_close))>1e-7:raise AssertionError('shared-cash asset identity failed')

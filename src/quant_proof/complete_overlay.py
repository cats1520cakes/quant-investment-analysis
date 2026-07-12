from __future__ import annotations
from dataclasses import dataclass
from math import floor
from collections import defaultdict

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

@dataclass
class SharedPortfolioLedger:
    cash: float = 0.; margin: float = 0.; futures_qty: int = 0; futures_last_settle: float|None = None; fees: float = 0.; margin_calls: int = 0; forced_liquidations: int = 0
    def __post_init__(self): self.shares=defaultdict(int); self.receivables=[]
    def deposit(self,amount): self.cash+=amount
    def rebalance_equal(self,opens:dict[str,float],tradable:dict[str,bool],reserve:float=0.,fee_rate=.0003):
        codes=sorted(opens);nav=self.cash+sum(self.shares[c]*opens[c] for c in codes);target=max(0.,nav-reserve)/len(codes);fills={}
        for c in codes:
            if not tradable[c]:fills[c]=0;continue
            excess=self.shares[c]*opens[c]-target
            if excess>opens[c]*100:
                q=min(self.shares[c],floor(excess/(opens[c]*100))*100);v=q*opens[c];fee=v*fee_rate;self.cash+=v-fee;self.shares[c]-=q;self.fees+=fee;fills[c]=-q
        for c in codes:
            if not tradable[c]:continue
            deficit=max(0.,target-self.shares[c]*opens[c]);q=floor(min(deficit,max(0,self.cash-reserve))/(opens[c]*100*(1+fee_rate)))*100;v=q*opens[c];fee=v*fee_rate;self.cash-=v+fee;self.shares[c]+=q;self.fees+=fee;fills[c]=fills.get(c,0)+q
        return fills
    def register_dividend(self,event_id,code,record_date,pay_date,cash_per_share,date):
        if date==record_date:self.receivables.append((event_id,code,pay_date,self.shares[code]*cash_per_share))
    def pay_dividends(self,date):
        paid=sum(x[3] for x in self.receivables if x[2]==date);self.cash+=paid;self.receivables=[x for x in self.receivables if x[2]!=date];return paid
    def apply_share_factor(self,code,factor):self.shares[code]=int(round(self.shares[code]*factor))
    def open_future(self,price,multiplier,margin_rate,nav_multiple,fee=8.):
        need=price*multiplier*margin_rate
        if self.futures_qty or self.cash<need*nav_multiple+fee:return False
        self.cash-=need+fee;self.margin=need;self.futures_qty=1;self.futures_last_settle=price;self.fees+=fee;return True
    def settle_future(self,settle,multiplier,maintenance_rate=.75):
        if not self.futures_qty:return 0.
        pnl=(settle-self.futures_last_settle)*multiplier;self.cash+=pnl;self.futures_last_settle=settle
        if self.cash+self.margin<self.margin*maintenance_rate:self.margin_calls+=1;self.close_future(settle,multiplier,forced=True)
        return pnl
    def close_future(self,price,multiplier,fee=8.,forced=False):
        if not self.futures_qty:return 0.
        pnl=(price-self.futures_last_settle)*multiplier;self.cash+=pnl+self.margin-fee;self.fees+=fee;self.margin=0.;self.futures_qty=0;self.futures_last_settle=None
        if forced:self.forced_liquidations+=1
        return pnl
    def nav(self,closes):return self.cash+self.margin+sum(self.shares[c]*closes[c] for c in closes)
    def assert_identity(self,closes):
        n=self.nav(closes)
        if self.cash < -1e-8 or abs(n-(self.cash+self.margin+sum(self.shares[c]*closes[c] for c in closes)))>1e-7:raise AssertionError('portfolio identity failed')

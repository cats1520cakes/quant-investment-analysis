"""US short-put research. Real quotes and proxy prices never silently substitute.

One underlying per account, standard 100-share contracts, close-to-close research.
American early assignment and intraday liquidation are not reconstructed.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from math import erf, exp, log, sqrt, isfinite
from statistics import NormalDist
import hashlib
from pathlib import Path
import numpy as np
import pandas as pd
from .metrics import max_drawdown


def put_price(spot: float, strike: float, days: int, sigma: float, rate: float = .04, dividend_yield: float = 0.) -> float:
    if min(spot, strike) <= 0 or sigma <= 0:
        raise ValueError('positive spot, strike and volatility required')
    if days <= 0:
        return max(strike - spot, 0.)
    t = days / 365
    d1 = (log(spot / strike) + (rate - dividend_yield + sigma*sigma/2)*t) / (sigma*sqrt(t))
    d2 = d1 - sigma*sqrt(t)
    cdf = lambda x: .5*(1+erf(x/sqrt(2)))
    # European BSM with American intrinsic lower bound: explicitly a proxy.
    return max(strike - spot, strike*exp(-rate*t)*cdf(-d2) - spot*exp(-dividend_yield*t)*cdf(-d1), 0.)


def delta_strike(spot: float, days: int, sigma: float, delta: float, rate: float = .04) -> float:
    t = days / 365
    d1 = NormalDist().inv_cdf(1-delta)
    return spot*exp(-d1*sigma*sqrt(t) + (rate+sigma*sigma/2)*t)


def short_put_margin(spot: float, strike: float, mark: float, contracts: int = 1, house_factor: float = 1.) -> float:
    """IBKR published US stock-option baseline, NOT portfolio margin or live quote."""
    return contracts*100*(mark + house_factor*max(.20*spot-max(spot-strike, 0.), .10*strike))


@dataclass(frozen=True)
class PutConfig:
    initial_equity: float = 100000.
    mode: str = 'cash'
    target_delta: float = .20
    dte: int = 30
    notional_cap: float = 1.
    margin_budget: float = .25
    sgov_margin_rate: float = .25  # scenario input; no SGOV-specific broker confirmation
    stock_margin_rate: float = .50  # conservative research requirement after assignment
    house_factor: float = 1.
    borrow_apr: float = .065  # assumption, not historical broker rate series
    cash_apr: float = 0.
    iv_multiplier: float = 1.2
    iv_floor: float = .15
    pricing_rate: float = .04
    spread_fraction: float = .05
    fee_per_contract: float = 1.
    share_slippage_bps: float = 2.
    assignment_hold_days: int = 5
    withholding_rate: float = 0.
    quote_age_minutes: float = 30.

    def __post_init__(self):
        if not all(isfinite(v) for v in asdict(self).values() if isinstance(v,(int,float))):
            raise ValueError('nonfinite config')
        if self.mode not in {'cash', 'margin'} or self.initial_equity <= 0:
            raise ValueError('invalid account mode/equity')
        if not 0 < self.target_delta < .5 or self.dte < 1 or self.notional_cap <= 0:
            raise ValueError('invalid option sizing')
        if not 0 < self.margin_budget <= 1 or not 0 <= self.sgov_margin_rate <= 1 or not 0 <= self.stock_margin_rate <= 1:
            raise ValueError('invalid margin rates')
        if not 0 <= self.spread_fraction < 1 or not 0 <= self.withholding_rate <= 1 or self.assignment_hold_days < 1:
            raise ValueError('invalid costs/holding period')
        if min(self.borrow_apr, self.cash_apr, self.fee_per_contract, self.share_slippage_bps, self.house_factor) < 0:
            raise ValueError('negative cost/house factor')


def load_market(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
    if not {'close','dividend','split'} <= set(frame) or frame.empty or frame.index.has_duplicates:
        raise ValueError(f'invalid daily market file: {path}')
    if not np.isfinite(frame[['close','dividend','split']].to_numpy()).all() or (frame.close <= 0).any() or (frame.dividend < 0).any() or (frame.split <= 0).any():
        raise ValueError('invalid daily price/dividend/split')
    return frame


def validate_quotes(frame: pd.DataFrame, manifest: dict, file_path: Path) -> pd.DataFrame:
    """Normalized snapshot CSV gate: provenance, standard deliverable, timestamps.

    Required manifest attestations are declarations, not an independent quote audit.
    """
    if manifest.get('data_kind') != 'historical_nbbo' or not manifest.get('source_url') or not manifest.get('retrieved_utc'):
        raise ValueError('historical NBBO provenance required')
    if manifest.get('sha256') != hashlib.sha256(file_path.read_bytes()).hexdigest():
        raise ValueError('quote SHA256 mismatch')
    if manifest.get('standard_deliverables_only') is not True:
        raise ValueError('adjusted/nonstandard option deliverables unsupported')
    required = {'date','quote_ts','symbol','contract','expiry','strike','bid','ask','bid_size','ask_size','delta','multiplier','right'}
    if not required <= set(frame) or frame.empty:
        raise ValueError('incomplete normalized quotes')
    q = frame.copy()
    q['date'] = pd.to_datetime(q.date)
    q['expiry'] = pd.to_datetime(q.expiry)
    if not q.quote_ts.astype(str).str.contains(r'(?:Z|[+-]\d{2}:?\d{2})$',regex=True).all():
        raise ValueError('quote timestamp requires explicit timezone')
    q['quote_ts'] = pd.to_datetime(q.quote_ts, utc=True)
    ny = q.quote_ts.dt.tz_convert('America/New_York')
    numeric = ['strike','bid','ask','bid_size','ask_size','delta','multiplier']
    if not np.isfinite(q[numeric].to_numpy(dtype=float)).all():
        raise ValueError('nonfinite quote values')
    if (q.strike <= 0).any() or (q.bid < 0).any() or (q.ask < q.bid).any() or (q[['bid_size','ask_size']] < 0).any().any():
        raise ValueError('invalid quote market')
    if (q.right != 'put').any():
        raise ValueError('only put quotes supported')
    if (q.multiplier != 100).any() or (~q.delta.between(-1, 0)).any() or (q.expiry < q.date).any():
        raise ValueError('unsupported contract')
    if (ny.dt.date != q.date.dt.date).any() or (ny.dt.hour >= 16).any() or (ny.dt.hour < 9).any():
        raise ValueError('quote date mismatch or snapshot outside supported 09:30-16:00 ET window')
    if ((ny.dt.hour == 9) & (ny.dt.minute < 30)).any():
        raise ValueError('pre-open quote')
    if q.duplicated(['date','contract']).any():
        raise ValueError('one snapshot per contract/date required')
    if (q.groupby('contract')[['strike','expiry','symbol','multiplier']].nunique() > 1).any().any():
        raise ValueError('contract metadata changed')
    return q


class QuoteBook:
    def __init__(self, quotes: pd.DataFrame):
        self.quotes = quotes

    def row(self, day, contract, cfg):
        rows = self.quotes[(self.quotes.date == day) & (self.quotes.contract == contract)]
        if len(rows) != 1:
            raise ValueError(f'missing held-contract quote: {day} {contract}; no proxy fallback')
        row = rows.iloc[0]
        minutes = row.quote_ts.tz_convert('America/New_York').hour*60 + row.quote_ts.tz_convert('America/New_York').minute
        if 960-minutes > cfg.quote_age_minutes:
            raise ValueError('stale quote; no forward fill')
        return row

    def select(self, signal_day, cfg):
        rows = self.quotes[self.quotes.date == signal_day].copy()
        rows['dte'] = (rows.expiry-signal_day).dt.days
        rows = rows[(rows.dte >= cfg.dte-7) & (rows.dte <= cfg.dte+7) & (rows.bid > 0) & (rows.bid_size > 0)]
        if rows.empty:
            return None
        rows['delta_error'] = abs(-rows.delta-cfg.target_delta)
        rows['dte_error'] = abs(rows.dte-cfg.dte)
        return rows.sort_values(['delta_error','dte_error','contract']).iloc[0]


def simulate(market: pd.DataFrame, sgov: pd.DataFrame, cfg: PutConfig, book: QuoteBook | None = None) -> tuple[pd.DataFrame, dict]:
    """Signal at prior close; open on today's snapshot. Fail closed on quote gaps.

    SGOV raw price + distributions, credited on ex date (timing approximation).
    End-of-day margin breach liquidates all exposures. No implicit deposits.
    """
    if len(market) < 65 or not market.index.isin(sgov.index).all():
        raise ValueError('need >=65 days and complete SGOV alignment')
    if (market.split != 1).any() or (sgov.split != 1).any():
        raise ValueError('splits require deliverable-aware normalization; refused')
    rv = market.close.pct_change(fill_method=None).rolling(60).std()*sqrt(252)
    cash, sgshares, stock, pos = cfg.initial_equity, 0, 0, None
    sell_day = None
    stock_basis = 0.
    cumulative = dict(premium=0., option_pnl=0., sgov_income=0., stock_income=0., borrowing=0., cash_interest=0., costs=0., assignments=0, liquidations=0, sgov_price_pnl=0., stock_price_pnl=0.)
    rows = []; bankrupt = False
    def sell_sgov(amount, price):
        nonlocal cash, sgshares
        if amount <= 0: return
        n = min(sgshares, int(np.ceil(amount/price)))
        gross = n*price; cost = gross*cfg.share_slippage_bps/10000
        cash += gross-cost; sgshares -= n; cumulative['costs'] += cost

    for i in range(60, len(market)):
        day = market.index[i]; prior = market.index[i-1]
        spot = float(market.close.iloc[i]); sp = float(sgov.loc[day,'close'])
        days_elapsed = (day-prior).days
        cumulative['sgov_price_pnl'] += sgshares*(sp-float(sgov.loc[prior,'close']))
        income = sgshares*float(sgov.loc[day,'dividend'])*(1-cfg.withholding_rate)
        stock_income = stock*float(market.dividend.iloc[i])*(1-cfg.withholding_rate)
        cumulative['sgov_income'] += income; cumulative['stock_income'] += stock_income
        borrow = max(-cash, 0)*cfg.borrow_apr*days_elapsed/365
        ci = max(cash, 0)*cfg.cash_apr*days_elapsed/365
        cumulative['borrowing'] += borrow; cumulative['cash_interest'] += ci
        cash += income + stock_income - borrow + ci
        sigma = max(cfg.iv_floor, float(rv.iloc[i-1])*cfg.iv_multiplier)
        if stock and day >= sell_day:
            gross = stock*spot; cost = gross*cfg.share_slippage_bps/10000
            cash += gross-cost; cumulative['costs'] += cost
            cumulative['stock_price_pnl'] += gross-stock_basis; stock_basis=0.; stock = 0
        mark = 0.; margin = 0.; reserve = 0.
        if pos:
            if day == pos['expiry']:
                intrinsic = max(pos['strike']-spot, 0.)
                intrinsic = intrinsic if intrinsic >= .01 else 0.
                cumulative['option_pnl'] += pos['credit'] - intrinsic*pos['n']*100
                if intrinsic > 0:
                    need = pos['n']*100*pos['strike']
                    if cfg.mode == 'cash': sell_sgov(need-cash, sp)
                    cash -= need; stock = pos['n']*100; stock_basis = stock*spot; cumulative['assignments'] += pos['n']
                    sell_day = day + pd.Timedelta(days=cfg.assignment_hold_days)
                pos = None
            elif day > pos['expiry']:
                raise ValueError('expiry not in underlying calendar')
            else:
                if book:
                    quote = book.row(day, pos['contract'], cfg); mark = float(quote.ask)
                else: mark = put_price(spot, pos['strike'], (pos['expiry']-day).days, sigma, cfg.pricing_rate)*(1+cfg.spread_fraction)
        # Do not open on settlement/stock sale day; snapshot ordering kept simple.
        can_open = not pos and not stock and not bankrupt and (not rows or rows[-1]['contracts'] == 0 and rows[-1]['stock_shares'] == 0)
        if can_open and i < len(market)-cfg.dte:
            if book:
                selected = book.select(prior, cfg)
                if selected is not None:
                    book.row(prior, selected.contract, cfg)
                    quote = book.row(day, selected.contract, cfg)
                    strike = float(selected.strike); expiry = selected.expiry
                    bid = float(quote.bid); mark_new = float(quote.ask); contract = selected.contract
                    size_limit = int(quote.bid_size)
                else: strike = 0
            else:
                target = day+pd.Timedelta(days=cfg.dte)
                candidates = market.index[(market.index >= target-pd.Timedelta(days=3)) & (market.index <= target+pd.Timedelta(days=3))]
                fridays = candidates[candidates.weekday == 4]
                expiry = fridays[0] if len(fridays) else (candidates[0] if len(candidates) else day)
                strike = delta_strike(float(market.close.iloc[i-1]), (expiry-prior).days, sigma, cfg.target_delta, cfg.pricing_rate)
                mid = put_price(spot,strike,(expiry-day).days,sigma,cfg.pricing_rate)
                bid = max(0.,mid*(1-cfg.spread_fraction)); mark_new = mid*(1+cfg.spread_fraction)
                contract = f'proxy-{expiry.date()}-{strike:.8f}'; size_limit = 10**9
            nav = cash+sgshares*sp
            if strike > 0 and expiry > day and bid > 0:
                n = min(size_limit, int(max(nav,0)*cfg.notional_cap/(strike*100)))
                if cfg.mode == 'margin':
                    unit = short_put_margin(spot,strike,mark_new,house_factor=cfg.house_factor)
                    n = min(n,int(max(nav,0)*cfg.margin_budget/unit))
                    # SGOV collateral requirement must coexist with option margin.
                    while n > 0 and (sgshares*sp*cfg.sgov_margin_rate + n*unit > nav + n*(bid-mark_new)*100 - n*cfg.fee_per_contract): n -= 1
                if n > 0:
                    credit = n*bid*100
                    if cfg.mode == 'cash': sell_sgov(strike*100*n-cash,sp)
                    cash += credit-n*cfg.fee_per_contract
                    cumulative['premium'] += credit; cumulative['costs'] += n*cfg.fee_per_contract
                    pos = dict(strike=strike,expiry=expiry,n=n,credit=credit,contract=contract)
                    mark = mark_new
        if pos:
            reserve = pos['strike']*100*pos['n'] if cfg.mode == 'cash' else 0.
            margin = short_put_margin(spot,pos['strike'],mark,pos['n'],cfg.house_factor) if cfg.mode == 'margin' else reserve
        if cfg.mode == 'cash': sell_sgov(reserve-cash,sp)
        liability = mark*100*pos['n'] if pos else 0.
        nav = cash + sgshares*sp + stock*spot-liability
        requirement = margin + sgshares*sp*cfg.sgov_margin_rate + stock*spot*cfg.stock_margin_rate if cfg.mode == 'margin' else reserve
        excess_before = nav-requirement
        if cfg.mode == 'margin' and excess_before < 0:
            if pos:
                if book and int(quote.ask_size) < pos['n']:
                    raise ValueError('insufficient ask size for forced close; execution unresolved')
                fee = cfg.fee_per_contract*pos['n']; cash -= liability+fee
                cumulative['option_pnl'] += pos['credit']-liability; cumulative['costs'] += fee; pos = None
            if stock:
                gross = stock*spot; cost = gross*cfg.share_slippage_bps/10000
                cash += gross-cost; cumulative['costs'] += cost
                cumulative['stock_price_pnl'] += gross-stock_basis; stock_basis=0.; stock = 0
            sell_sgov(sgshares*sp,sp)
            cumulative['liquidations'] += 1; margin=reserve=liability=0.
            nav=cash; bankrupt = nav <= 0
        # Retain strike reserve in CSP; SGOV otherwise bought only from positive cash.
        investable = max(cash-reserve,0.)
        if not bankrupt:
            nsg = int(investable/(sp*(1+cfg.share_slippage_bps/10000)))
            if cfg.mode == 'margin' and cfg.sgov_margin_rate > 0:
                nsg = min(nsg, int(max(0.,nav-requirement)/(sp*(cfg.sgov_margin_rate+cfg.share_slippage_bps/10000))))
            gross = nsg*sp; cost = gross*cfg.share_slippage_bps/10000
            cash -= gross+cost; sgshares += nsg; cumulative['costs'] += cost
        nav = cash+sgshares*sp+stock*spot-liability
        requirement = (margin+sgshares*sp*cfg.sgov_margin_rate+stock*spot*cfg.stock_margin_rate) if cfg.mode == 'margin' else reserve
        rows.append(dict(date=day,nav=nav,cash=cash,sgov_value=sgshares*sp,stock_shares=stock,
                         contracts=pos['n'] if pos else 0,put_liability=liability,margin=requirement,
                         put_margin=margin,excess_liquidity=nav-requirement,pre_liquidation_excess=excess_before,
                         **cumulative))
    daily = pd.DataFrame(rows).set_index('date')
    # Include starting capital in drawdown and elapsed-time denominator.
    curve = pd.concat([pd.Series([cfg.initial_equity],index=[market.index[59]]),daily.nav])
    years = (daily.index[-1]-market.index[59]).days/365.25
    total = daily.nav.iloc[-1]/cfg.initial_equity-1
    summary = dict(data_kind='quote_based_research' if book else 'proxy_research',total_return=total,
                   cagr=(1+total)**(1/years)-1 if total > -1 else None,max_drawdown=max_drawdown(curve),
                   start=str(market.index[59].date()),end=str(daily.index[-1].date()),
                   mean_margin=float(daily.margin.mean()),peak_margin=float(daily.margin.max()),mean_put_margin=float(daily.put_margin.mean()),
                   gross_premium_per_mean_put_margin_per_year=float(cumulative['premium']/daily.put_margin.mean()/years) if daily.put_margin.mean()>0 else 0.,
                   peak_margin_fraction=float((daily.margin/daily.nav.replace(0,np.nan)).max()),
                   min_excess_liquidity=float(daily.pre_liquidation_excess.min()),
                   peak_cash_debit=float(max(0.,-daily.cash.min())),
                   ending_open_put_liability=float(daily.put_liability.iloc[-1]),
                   net_profit=float(daily.nav.iloc[-1]-cfg.initial_equity),**cumulative,config=asdict(cfg))
    summary['open_option_pnl'] = pos['credit']-daily.put_liability.iloc[-1] if pos else 0.
    summary['open_stock_pnl'] = stock*spot-stock_basis
    components = sum(cumulative[k] for k in ['option_pnl','sgov_income','stock_income','cash_interest','sgov_price_pnl','stock_price_pnl'])-cumulative['borrowing']-cumulative['costs']+summary['open_option_pnl']+summary['open_stock_pnl']
    summary['pnl_reconciliation_residual'] = summary['net_profit']-components
    return daily, summary


def stress_table(cfg: PutConfig, spot: float = 100., sigma: float = .25) -> pd.DataFrame:
    """Instantaneous price/IV/house shocks; no probability attached."""
    strike = delta_strike(spot,cfg.dte,sigma,cfg.target_delta,cfg.pricing_rate)
    mid = put_price(spot,strike,cfg.dte,sigma,cfg.pricing_rate)
    n = int(cfg.initial_equity*cfg.notional_cap/(strike*100))
    if cfg.mode == 'margin': n=min(n,int(cfg.initial_equity*cfg.margin_budget/short_put_margin(spot,strike,mid)))
    premium = n*mid*(1-cfg.spread_fraction)*100
    sgov_value = cfg.initial_equity if cfg.mode == 'margin' else max(0,cfg.initial_equity-strike*n*100)
    rows=[]
    for shock in [-.10,-.20,-.35,-.50,-.80]:
        for house in [1.,2.]:
            price=spot*(1+shock); mark=put_price(price,strike,max(cfg.dte-7,1),sigma*2,cfg.pricing_rate)*(1+cfg.spread_fraction)
            nav=cfg.initial_equity+premium-mark*n*100-n*cfg.fee_per_contract
            req=short_put_margin(price,strike,mark,n,house)+sgov_value*cfg.sgov_margin_rate if cfg.mode=='margin' else strike*n*100
            assignment_debit=max(strike*n*100-premium-(cfg.initial_equity-sgov_value),0)
            rows.append(dict(shock=shock,iv=sigma*2,house_factor=house,contracts=n,nav=nav,
                             margin=req,excess=nav-req,margin_breach=cfg.mode=='margin' and nav<req,
                             assignment_debit_if_keep_sgov=assignment_debit,
                             borrowing_30d=assignment_debit*cfg.borrow_apr*30/365,
                             sgov_30d_assumed_4pct=sgov_value*.04*30/365))
    return pd.DataFrame(rows)

"""Accounting and fail-closed evidence tests, independent of public downloads."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import pandas as pd
from quant_proof.us_short_put import PutConfig, QuoteBook, delta_strike, put_price, short_put_margin, simulate, validate_quotes


def market(prices):
    dates=pd.bdate_range('2023-01-02',periods=len(prices))
    return pd.DataFrame(dict(close=prices,dividend=0.,split=1.),index=dates)

class ShortPutTests(unittest.TestCase):
    def test_published_regt_formula_and_house_overlay(self):
        self.assertAlmostEqual(short_put_margin(100,95,2),1700)
        self.assertAlmostEqual(short_put_margin(70,95,25),3900)
        self.assertAlmostEqual(short_put_margin(70,95,25,2,2),10600)

    def test_expiry_intrinsic_and_delta(self):
        self.assertEqual(put_price(80,95,0,.3),15)
        k=delta_strike(100,30,.25,.2)
        self.assertTrue(90<k<100)
        self.assertGreater(put_price(100,k,30,.5),put_price(100,k,30,.25))

    def test_assignment_debit_financing_and_pnl_tieout(self):
        prices=np.full(145,100.); prices[70:]=60.
        m=market(prices); sg=market(np.full(145,100.))
        # Include an SGOV dividend and matching ex-date price drop.
        sg.loc[sg.index[100],'dividend']=.4
        sg.loc[sg.index[100]:,'close']=99.6
        for mode in ['cash','margin']:
            d,s=simulate(m,sg,PutConfig(mode=mode,notional_cap=.5,margin_budget=1.,assignment_hold_days=10))
            self.assertGreater(s['assignments'],0)
            self.assertAlmostEqual(s['pnl_reconciliation_residual'],0,places=7)
            if mode=='margin':
                self.assertLess(d.cash.min(),0)
                self.assertGreater(s['borrowing'],0)
            else:
                self.assertEqual(s['borrowing'],0)
            self.assertGreater(s['max_drawdown'],.1)

    def test_sgov_payout_not_double_counted(self):
        m=market(np.full(100,1000000.)); sg=market(np.full(100,100.))
        sg.loc[sg.index[70],'dividend']=1.
        sg.loc[sg.index[70]:,'close']=99.
        d,s=simulate(m,sg,PutConfig(share_slippage_bps=0))
        self.assertEqual(s['premium'],0)
        self.assertEqual(s['sgov_income'],1000)
        self.assertEqual(s['sgov_price_pnl'],-1000)
        self.assertAlmostEqual(s['net_profit'],0)

    def test_forced_exit_and_equity_not_clamped(self):
        prices=np.full(145,100.); prices[70:]=5.
        m=market(prices); sg=market(np.full(145,100.))
        d,s=simulate(m,sg,PutConfig(mode='margin',notional_cap=2.,margin_budget=.5))
        self.assertGreater(s['liquidations'],0)
        self.assertLess(d.pre_liquidation_excess.min(),0)
        self.assertLess(d.nav.min(),0)
        self.assertIsNone(s['cagr'])
        self.assertAlmostEqual(s['pnl_reconciliation_residual'],0,places=7)

    def test_splits_and_sgov_gap_rejected(self):
        m=market(np.full(100,100.)); sg=m.copy(); m.loc[m.index[70],'split']=2.
        with self.assertRaisesRegex(ValueError,'splits'): simulate(m,sg,PutConfig())
        m['split']=1.
        with self.assertRaisesRegex(ValueError,'alignment'): simulate(m,sg.iloc[:-1],PutConfig())

    def test_quote_gate_hash_timestamp_and_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'quotes.csv'
            q=pd.DataFrame([dict(date='2023-04-03',quote_ts='2023-04-03T19:55:00Z',symbol='SPY',right='put',contract='p1',expiry='2023-05-05',strike=390,bid=1.,ask=1.2,bid_size=5,ask_size=5,delta=-.2,multiplier=100)])
            q.to_csv(path,index=False)
            manifest=dict(data_kind='historical_nbbo',source_url='https://provider.example/export',retrieved_utc='2026-10-12T00:00:00Z',standard_deliverables_only=True,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            valid=validate_quotes(q,manifest,path)
            self.assertEqual(len(valid),1)
            with self.assertRaisesRegex(ValueError,'SHA256'): validate_quotes(q,{**manifest,'sha256':'bad'},path)
            q['quote_ts']='2023-04-04T19:55:00Z'
            with self.assertRaisesRegex(ValueError,'date mismatch'): validate_quotes(q,manifest,path)
            with self.assertRaisesRegex(ValueError,'missing held'): QuoteBook(valid).row(pd.Timestamp('2023-04-04'),'p1',PutConfig())
            with self.assertRaisesRegex(ValueError,'stale'): QuoteBook(valid.assign(quote_ts=pd.to_datetime(['2023-04-03T18:00:00Z'],utc=True))).row(pd.Timestamp('2023-04-03'),'p1',PutConfig())

    def test_initial_capital_in_drawdown(self):
        m=market(np.full(110,100.)); sg=market(np.full(110,100.))
        d,s=simulate(m,sg,PutConfig(spread_fraction=.2,share_slippage_bps=0))
        first_loss=1-d.nav.iloc[0]/100000
        self.assertGreater(first_loss,0)
        self.assertGreaterEqual(s['max_drawdown'],first_loss)

    def test_margin_includes_sgov_and_investment_respects_requirement(self):
        m=market(np.full(145,100.)); sg=market(np.full(145,100.))
        d,s=simulate(m,sg,PutConfig(mode='margin',sgov_margin_rate=.8,margin_budget=.7,notional_cap=2.))
        active=d[d.contracts>0]
        self.assertTrue((active.margin>=active.put_margin+active.sgov_value*.8-1e-7).all())
        self.assertTrue((active.excess_liquidity>=-1e-7).all())
        self.assertAlmostEqual(s['pnl_reconciliation_residual'],0,places=7)

    def test_real_quotes_execute_bid_mark_ask_and_reject_gap(self):
        m=market(np.full(120,100.)); sg=market(np.full(120,100.))
        expiry=m.index[84]; m.loc[expiry:,'close']=90.
        q=pd.DataFrame([dict(date=day,quote_ts=pd.Timestamp(str(day.date())+' 15:55',tz='America/New_York').tz_convert('UTC'),symbol='SPY',right='put',contract='fixture-put',expiry=expiry,strike=95.,bid=1.,ask=1.2,bid_size=100,ask_size=100,delta=-.2,multiplier=100) for day in m.index[59:85]])
        cfg=PutConfig(mode='margin',margin_budget=1.,share_slippage_bps=0)
        daily,summary=simulate(m,sg,cfg,QuoteBook(q))
        self.assertEqual(daily.premium.iloc[0],1000.)
        self.assertEqual(daily.put_liability.iloc[0],1200.)
        self.assertEqual(summary['assignments'],10)
        self.assertEqual(summary['option_pnl'],-4000.)
        self.assertAlmostEqual(summary['pnl_reconciliation_residual'],0,places=7)
        with self.assertRaisesRegex(ValueError,'missing held'):
            simulate(m,sg,cfg,QuoteBook(q[q.date!=m.index[70]]))

if __name__=='__main__': unittest.main()

from datetime import date
import pytest
from quant_proof.etf_dividends import DividendEvent,DividendLedgerError,DividendReceivables

def event(status="confirmed"):
 return DividendEvent("e1","510300",date(2024,1,17),date(2024,1,18),date(2024,1,23),.069,status)
def test_cash_is_receivable_on_record_date_but_paid_only_on_pay_date():
 x=DividendReceivables(); x.register(event(),150,date(2024,1,17)); assert x.pay(date(2024,1,18))==0; assert x.pay(date(2024,1,23))==pytest.approx(10.35)
def test_less_than_board_lot_is_still_dividend_eligible():
 x=DividendReceivables(); x.register(event(),50,date(2024,1,17)); assert x.pay(date(2024,1,23))==pytest.approx(3.45)
def test_duplicate_and_cancelled_events_fail_closed():
 x=DividendReceivables(); x.register(event(),100,date(2024,1,17))
 with pytest.raises(DividendLedgerError): x.register(event(),100,date(2024,1,17))
 with pytest.raises(DividendLedgerError): DividendReceivables().register(event("cancelled"),100,date(2024,1,17))

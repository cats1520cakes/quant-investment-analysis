from __future__ import annotations
from dataclasses import dataclass
from datetime import date

@dataclass(frozen=True)
class DividendEvent:
    event_id: str; code: str; record_date: date; ex_date: date; pay_date: date; cash_per_share: float; status: str="confirmed"

class DividendLedgerError(RuntimeError): pass

class DividendReceivables:
    def __init__(self): self.pending: dict[str, tuple[date,float]] = {}
    def register(self,event:DividendEvent,shares:int,on_date:date)->None:
        if event.status!="confirmed": raise DividendLedgerError("unconfirmed or cancelled dividend")
        if on_date!=event.record_date: raise DividendLedgerError("dividend eligibility must be recorded on record date")
        if event.event_id in self.pending: raise DividendLedgerError("duplicate dividend event")
        self.pending[event.event_id]=(event.pay_date,shares*event.cash_per_share)
    def pay(self,on_date:date)->float:
        due=[k for k,(d,_) in self.pending.items() if d==on_date]
        cash=sum(self.pending.pop(k)[1] for k in due)
        return cash

import pytest

from quant_proof.etf_execution import EtfExecutionBar, EtfExecutionError, execute_etf_buy


def test_etf_buy_uses_hfq_only_for_signal_and_raw_for_fill() -> None:
    fill = execute_etf_buy(cash_before=1000, monthly_deposit=30000, deposit_timing="beginning", requested_notional=31000, bar=EtfExecutionBar(raw_open=3.0, hfq_signal=9.0, tradable=True))
    assert fill.quantity == 10300 and fill.raw_price == 3.0 and fill.signal_value == 9.0


def test_deposit_timing_changes_current_day_affordability() -> None:
    bar = EtfExecutionBar(raw_open=10.0, hfq_signal=20.0, tradable=True)
    assert execute_etf_buy(cash_before=0, monthly_deposit=30000, deposit_timing="beginning", requested_notional=30000, bar=bar).quantity == 3000
    ending = execute_etf_buy(cash_before=0, monthly_deposit=30000, deposit_timing="ending", requested_notional=30000, bar=bar)
    assert ending.quantity == 0 and ending.cash_after == 30000


def test_insufficient_cash_returns_zero_whole_lots() -> None:
    fill = execute_etf_buy(cash_before=299, monthly_deposit=0, deposit_timing="beginning", requested_notional=299, bar=EtfExecutionBar(raw_open=3.0, hfq_signal=3.0, tradable=True))
    assert fill.quantity == 0 and fill.cash_after == 299


@pytest.mark.parametrize("bar", [EtfExecutionBar(None, 2.0, True), EtfExecutionBar(2.0, 2.0, False)])
def test_missing_raw_or_suspension_fails_closed(bar) -> None:
    with pytest.raises(EtfExecutionError, match="raw execution|suspended"):
        execute_etf_buy(cash_before=1000, monthly_deposit=0, deposit_timing="beginning", requested_notional=1000, bar=bar)


def test_incomplete_company_action_ledger_fails_closed() -> None:
    with pytest.raises(EtfExecutionError, match="company action"):
        execute_etf_buy(cash_before=1000, monthly_deposit=0, deposit_timing="beginning", requested_notional=1000, bar=EtfExecutionBar(2.0, 4.0, True, "missing_event"))

from quant_proof.complete_overlay import SharedPortfolioLedger


def test_negative_variation_cash_requires_immediate_margin_action():
    ledger = SharedPortfolioLedger(cash=559.61524)
    ledger.margin = 160476.0
    ledger.futures_qty = 1
    ledger.futures_last_settle = 2664.2
    ledger.settle_future(2659.0, 300.0)

    # This regression captures the defect in v3.  A corrected engine must not
    # leave an account with unfunded negative daily variation cash.
    assert ledger.cash >= 0, "negative variation cash escaped the margin-call path"

# US Short Put + SGOV: evidence and execution contract

## Scope and evidence (checked 2026-10-12)

The implementation lives in `src/quant_proof/us_short_put.py`. It reuses the existing `quant_proof.metrics.max_drawdown` and repository reporting/test conventions. It does not modify the CFFEX cash-settlement/long-option account, whose deliverable semantics differ from US physically settled short puts. No unrelated A-share experiment was rerun.

| Input | Verified source / availability | Implemented use | Boundary |
|---|---|---|---|
| Historical NBBO | [Cboe Option Quotes](https://datashop.cboe.com/option-quote-intervals), [ThetaData history quote](https://www.thetadata.net/docs/operations/option_history_quote.html) | Normalized CSV ingestion with provenance/hash gate; bid sells, ask marks/closes | No historical chain downloaded; Cboe ordering/licensing or Theta Terminal/access required. A current chain or free volume series does not suffice. |
| IBKR historical options | [Official API unavailable data](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-historical/historical-data-limitations/unavailable-historical-data) | Not used as expired-chain source | Expired options and option EOD data unavailable. |
| Underlying and SGOV daily prices | Yahoo chart endpoint, raw snapshots and hashes in local `data/us_short_put` | Unadjusted close, split and distribution event fields | Vendor data, not exchange-certified. Hash identifies an input snapshot, not source truth. No API keys used. |
| SGOV total returns/distributions | [iShares issuer page](https://www.ishares.com/us/products/314116/ishares-0-3-month-treasury-bond-etf-sgov) | Annual cross-check for 2021–2025; raw-close plus distribution accounting | Official NAV vs market-price difference is expected. Yahoo dividend timestamp is an ex-date; cash is modeled immediately at ex-date. Historical payable dates are absent. |
| Put baseline margin | [IBKR US option margin](https://www.interactivebrokers.com/en/trading/margin-options.php) | 100 × contracts × [option price + max(20% × S − max(S−K,0), 10% × K)] | ETFs use stock-option baseline, not 15% index-option formula. Broker house factor is a stress input. No portfolio-margin model or account-specific approval assumed. |
| Debit financing | [IBKR interest charged examples](https://www.interactivebrokers.com/en/accounts/fees/interestCharged-Examples.php), [margin rates](https://www.interactivebrokers.com/en/trading/margin-rates.php) | Daily interest on negative cash, elapsed calendar days | 6.5% flat APR is a scenario input. A historical tiered broker-rate series is not available. Short Put margin reservation alone generates no modeled borrowing charge. |
| SGOV and assigned-stock margin | Configuration inputs | SGOV 25%, assigned shares 50%; collateral and Put requirements additive | SGOV-specific account haircut unverified. SGOV purchases use positive cash; this is not a reproduction of broker initial/maintenance/EWL algorithms. |

## Experiment

The frozen evaluation end is **2025-12-31**. Common input start is 2021-04-15; the first 60 sessions are volatility warmup. The actual strategy start/end are reported for each run. SPY and QQQ cover broad US equity/growth, KWEB covers China internet, ARKK and COIN cover volatile thematic/individual equity paths. Their current or historical IV ranking has **not** been established.

Initial capital is $100,000, without external contributions. The experiment has three accounts per symbol and IV setting:

- `cash_secured`: target strike notional <= 1× current NAV; retain full strike cash; invest remaining positive cash in SGOV.
- `sgov_same_notional`: the same 1× strike-notional **cap**, but retain SGOV and reserve margin. Reinvesting returns changes subsequent contract counts; this is not an exact trade-matched pair.
- `sgov_margin_budget`: cap notional at 2× NAV, with Put entry margin capped at 25% NAV. SGOV collateral usage is additional and tested against equity.

Whole option contracts (100 shares), whole SGOV shares, $1/contract fees, 2 bps share slippage, zero interest on idle cash, no tax withholding by default. Reported net returns are after modeled transaction/financing costs **before investor-specific taxes**. Premium is a gross flow, not profit. `gross_premium_per_mean_put_margin_per_year` is a descriptive capital-efficiency ratio; it is not an account investment return.

One Put position at a time. Proxy strike comes from a theoretical 20-delta European formula using prior-close spot and prior 60-session realized volatility. Assumed IV multipliers 1.0/1.2/1.5 are sensitivity scenarios, not observed IV. Strike is continuous, with no inferred historical strike grid. Risk-free pricing rate is assumed 4%, and no dividend yield is used in option pricing. Dividend cash is credited if assigned stock is held. BSM with an intrinsic floor does not reproduce American exercise premiums, skew, earnings jumps, historical spread/liquidity or a volatility risk premium. SPY/QQQ distributions also affect this limitation.

Proxy fills at 95% of model price and marks/closes at 105%; real-quote research sells at bid and marks/buys at ask. The signal uses the previous session; execution uses today's snapshot. Proxy valuation uses today's close with prior-session volatility. This is a close-snapshot execution convention, not an assertion that the close and NBBO were synchronous or fully executable. Historical quote size caps entry; forced closes reject insufficient ask size. Existing positions are not forward-filled through missing quotes.

At exact expiry, exercise-by-exception is approximated using intrinsic >= $0.01; an in-the-money Put purchases shares at strike. Assigned shares remain for five calendar days, then sell on the next session. Margin accounts retain SGOV and can develop a debit cash balance; CSP spends strike reserve. A maintenance breach triggers modeled end-of-day closure of Put/shares and sale of SGOV. There is no assumed future deposit or hidden reset to positive NAV. Intraday broker liquidations, early assignment, contrary exercise instructions, holiday early closes, payment/settlement timing and discretionary house hikes are not reconstructed. Hence even supplied NBBO runs are labeled **quote_based_research**, not strict broker-realized performance.

## Real quote input gate

Supply a CSV containing one regular-session snapshot per date/contract, with:

```
date,quote_ts,symbol,contract,expiry,right,strike,bid,ask,bid_size,ask_size,delta,multiplier
2023-04-03,2023-04-03T19:55:00Z,SPY,EXAMPLE_PUT,2023-05-05,put,390,1.00,1.20,5,5,-0.20,100
```

The row above is a **format example, not market evidence**. `quote_ts` must explicitly include UTC/offset, and NY-local date must match `date`. Only snapshots within 30 minutes before a normal 16:00 ET close are supported by default. Filter to one consistent timestamp across the chain; early-close sessions require a session-aware extension. Delta must come from the documented provider/Greek export. Theta's quote endpoint alone lacks delta; join provider historical Greeks with the same contract and timestamp before ingestion. Cboe Calcs exports can supply historical delta. Do not invent a current Greek for past selection.

Companion JSON:

```json
{
  "data_kind": "historical_nbbo",
  "source_url": "https://your-provider/export-reference",
  "retrieved_utc": "2026-10-12T00:00:00Z",
  "sha256": "SHA256_OF_EXACT_NORMALIZED_CSV_BYTES",
  "standard_deliverables_only": true
}
```

This manifest declares provenance; the user/provider must substantiate authenticity and standard contract eligibility. A checksum does not authenticate a quote. Keep original provider export and normalizer mapping alongside it. Only puts, 100-share standard deliverables, finite noncrossed markets, valid dates and stable contract metadata pass. Stock splits anywhere in the input window are refused until deliverable-aware corporate-action support exists. Missing historical symbols, held-contract quotes, stale snapshots or insufficient exit size abort the run; none triggers a model-price fallback.

```bash
python scripts/run_us_short_put_research.py \
  --quotes /path/to/normalized_nbbo.csv \
  --quote-manifest /path/to/quote_manifest.json \
  --output reports/us_short_put_quotes
```

Configure symbols and dates to the available quote window. Preserve at least 59 daily observations before the first snapshot for warmup and previous-session selection. The first execution occurs after the first snapshot. Do not substitute illustrative fixture data for a historical run.

## Reproduce and inspect

```bash
python scripts/download_us_short_put_data.py
python scripts/run_us_short_put_research.py
PYTHONPATH=src python -m unittest discover -s tests -p test_us_short_put.py -v
python -m compileall -q src/quant_proof/us_short_put.py scripts/download_us_short_put_data.py scripts/run_us_short_put_research.py tests/test_us_short_put.py
```

Use the existing project environment (`uv sync --dev`) or installed numpy/pandas. `results.json` records configuration checksum, input checksums, decomposition, annual results and static shocks (-10/-20/-35/-50/-80%, IV ×2, house factor 1/2). Shocks carry no forecast probabilities. Individual daily CSVs include marked NAV, cash, SGOV value, shares, contracts, Put liability, requirements, before-liquidation excess and cumulative income/costs. Original raw data and CSVs stay outside git under the existing repository exclusions; committed JSON/report preserve the evidence manifest and compact outputs. Refreshing download endpoints creates a new snapshot, so preserve original local data for exact byte-level reruns.

The current real-chain gap is specific: historical NBBO and provider delta for selected contracts on every held session, standard-deliverable/corporate-action mapping, and licensing/access. Historical broker rates and SGOV payable dates would separately improve financing realism. No claims about real option CAGR, statistical superiority or investment suitability are supported until those gaps are addressed.

# Phase 2 Free Real Target Backtest

Generated at: 2026-07-05 23:08:36

## Scope

- Data tier: `free_real`.
- Strict real leaderboard remains separate and blocked until official/paid-grade fields exist.
- This target backtest uses BaoStock raw OHLC for execution, qfq prices for signals, derived limit prices, and BaoStock `tradestatus` as suspension proxy.
- It models monthly deposits, 12/24-month hard targets, T+1, suspended/no-trade checks, limit-up buy rejection, limit-down sell rejection, A-share board-lot buying, fixed order-notional caps, commission, transfer fee, stamp tax, and slippage.
- Daily amount participation cap: disabled; this report is the pre-cap baseline.
- Known gaps: no participation-rate cap from daily amount in this pre-cap baseline, no dividend/corporate-action cash adjustment, and free-real suspension/limit evidence is proxy/derived rather than official.

## Inputs

- Config: `config/phase2_free_real_data.yaml`
- Panel: `/Volumes/PSSD1TB/量化数据/processed/phase2_free/stock_panel.parquet`
- Panel snapshot: rows=`2016868`, symbols=`505`, date_range=`20100104..20260703`, data_tier=`free_real`.
- Minimum symbol gate: `500`.
- Run scope: `max_strategies=all, max_windows=all`.
- Window rows: `14700`

## Execution Semantics

- Strategy rankings and filters come from S2/S3/S4 specs, then the target layer converts selected names into equal-weight rebalances under execution constraints.
- S2/S3 stop-loss, trailing-stop, ATR stop, `risk_per_trade`, and `max_holding_days` parameters are not implemented as separate intra-window exits in this free-real target layer; they remain candidate-spec metadata until a stricter order-policy layer is added.
- `avg_turnover` is reported as traded notional divided by summed daily wealth across the window, not annualized portfolio turnover.

## Leaderboard

| strategy                                                   | family                  | deposit_timing   |   n_windows | p_success   | p_w12   | p_w24   |   median_w24 | p95_max_drawdown   | p_w24_below_deposit   |   avg_rejected_orders |   avg_clipped_orders |     score |
|:-----------------------------------------------------------|:------------------------|:-----------------|------------:|:------------|:--------|:--------|-------------:|:-------------------|:----------------------|----------------------:|---------------------:|----------:|
| S4_real_smallcap_factor_low_turnover_k10_weekly            | S4_real_smallcap_factor | beginning        |         175 | 6.29%       | 15.43%  | 23.43%  |      940,474 | 40.70%             | 13.71%                |               36.4686 |          11.3029     |  -8.64175 |
| S4_real_smallcap_factor_low_turnover_k10_weekly            | S4_real_smallcap_factor | ending           |         175 | 5.14%       | 6.86%   | 19.43%  |      923,821 | 40.63%             | 14.29%                |               34.8    |           8.01714    | -10.0562  |
| S4_real_smallcap_factor_low_turnover_k20_weekly            | S4_real_smallcap_factor | beginning        |         175 | 3.43%       | 6.29%   | 13.71%  |      900,705 | 38.17%             | 20.57%                |               83.8457 |           0.04       | -13.0619  |
| S4_real_smallcap_factor_low_turnover_k20_weekly            | S4_real_smallcap_factor | ending           |         175 | 2.86%       | 4.57%   | 10.86%  |      885,017 | 38.14%             | 21.14%                |               80.16   |           0.0228571  | -13.8982  |
| S4_real_smallcap_factor_low_turnover_k50_weekly            | S4_real_smallcap_factor | beginning        |         175 | 1.71%       | 4.57%   | 6.86%   |      853,322 | 45.81%             | 19.43%                |              195.829  |           0          | -15.0006  |
| S4_real_smallcap_factor_low_turnover_k30_weekly            | S4_real_smallcap_factor | ending           |         175 | 1.71%       | 4.00%   | 6.29%   |      862,366 | 43.73%             | 20.57%                |              126.806  |           0          | -15.7118  |
| S4_real_smallcap_factor_low_turnover_k30_weekly            | S4_real_smallcap_factor | beginning        |         175 | 1.71%       | 4.57%   | 6.86%   |      876,240 | 43.89%             | 20.57%                |              132.697  |           0          | -15.8217  |
| S4_real_smallcap_factor_low_turnover_k50_weekly            | S4_real_smallcap_factor | ending           |         175 | 0.57%       | 4.57%   | 4.57%   |      845,232 | 45.76%             | 21.14%                |              186.251  |           0          | -16.6478  |
| S4_real_smallcap_factor_low_turnover_k10_monthly           | S4_real_smallcap_factor | beginning        |         175 | 3.43%       | 5.71%   | 11.43%  |      834,124 | 44.41%             | 21.14%                |               11.5771 |           2.74286    | -16.8696  |
| S4_real_smallcap_factor_low_turnover_k30_monthly           | S4_real_smallcap_factor | beginning        |         175 | 1.71%       | 4.00%   | 7.43%   |      836,805 | 43.91%             | 22.86%                |               30.6743 |           0          | -17.7643  |
| S4_real_smallcap_factor_low_turnover_k30_monthly           | S4_real_smallcap_factor | ending           |         175 | 1.14%       | 4.00%   | 6.29%   |      830,425 | 43.76%             | 21.71%                |               30.1771 |           0          | -17.8033  |
| S4_real_smallcap_factor_low_turnover_k50_monthly           | S4_real_smallcap_factor | beginning        |         175 | 1.71%       | 4.57%   | 6.86%   |      830,901 | 45.41%             | 23.43%                |               49.4971 |           0          | -17.9197  |
| S4_real_smallcap_factor_low_turnover_k20_monthly           | S4_real_smallcap_factor | beginning        |         175 | 2.29%       | 5.14%   | 9.14%   |      858,265 | 43.51%             | 24.57%                |               21.0343 |           0.00571429 | -18.3207  |
| S4_real_smallcap_factor_low_turnover_k50_monthly           | S4_real_smallcap_factor | ending           |         175 | 1.14%       | 4.00%   | 5.71%   |      824,424 | 45.36%             | 23.43%                |               47.6571 |           0          | -18.3619  |
| S4_real_smallcap_factor_low_turnover_k20_monthly           | S4_real_smallcap_factor | ending           |         175 | 1.71%       | 4.00%   | 8.00%   |      853,408 | 43.26%             | 25.14%                |               20.8343 |           0          | -18.7773  |
| S4_real_smallcap_factor_low_turnover_k10_monthly           | S4_real_smallcap_factor | ending           |         175 | 1.71%       | 4.00%   | 7.43%   |      828,623 | 44.34%             | 22.29%                |               11.4286 |           1.76571    | -18.8311  |
| S4_real_smallcap_factor_high_turnover_breakout_k50_monthly | S4_real_smallcap_factor | beginning        |         175 | 1.14%       | 4.00%   | 4.57%   |      782,808 | 47.68%             | 34.86%                |               41.7829 |           0          | -23.6549  |
| S4_real_smallcap_factor_high_turnover_breakout_k50_monthly | S4_real_smallcap_factor | ending           |         175 | 0.00%       | 2.86%   | 4.57%   |      781,934 | 47.49%             | 34.86%                |               40.1086 |           0          | -24.5345  |
| S4_real_smallcap_factor_high_turnover_breakout_k30_monthly | S4_real_smallcap_factor | beginning        |         175 | 0.00%       | 2.86%   | 4.00%   |      774,067 | 41.82%             | 37.14%                |               28.2629 |           0          | -26.2876  |
| S4_real_smallcap_factor_high_turnover_breakout_k30_monthly | S4_real_smallcap_factor | ending           |         175 | 0.00%       | 2.29%   | 4.00%   |      770,188 | 41.80%             | 37.71%                |               27.4857 |           0          | -26.3025  |

## Readout

- Best score: `S4_real_smallcap_factor_low_turnover_k10_weekly` / `beginning`, success=6.29%, median_w24=940,474.
- Highest success: `S4_real_smallcap_factor_low_turnover_k10_weekly` / `beginning`, success=6.29%, median_w24=940,474.
- A positive signal leaderboard is not enough; target proof requires these rolling-window success and drawdown fields.

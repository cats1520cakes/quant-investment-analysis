# Phase 2 Free Real Target Backtest

Generated at: 2026-07-07 01:13:41

## Scope

- Data tier: `free_real`.
- Strict real leaderboard remains separate and blocked until official/paid-grade fields exist.
- This target backtest uses BaoStock raw OHLC for execution, qfq prices for signals, derived limit prices, and BaoStock `tradestatus` as suspension proxy.
- It models monthly deposits, 12/24-month hard targets, T+1, suspended/no-trade checks, limit-up buy rejection, limit-down sell rejection, A-share board-lot buying, fixed order-notional caps, commission, transfer fee, stamp tax, and slippage.
- Daily amount participation cap: `0.0500` of BaoStock full-day `amount` per stock per rebalance order.
- Known gaps: the participation cap uses BaoStock full-day `amount` as an approximate liquidity stress, not official intraday order-book depth or guaranteed fill capacity; dividend/corporate-action cash adjustment and official suspension/limit evidence are still absent.

## Inputs

- Config: `config/phase2_free_real_data.yaml`
- Panel: `/Volumes/PSSD1TB/量化数据/processed/phase2_free/stock_panel.parquet`
- Panel snapshot: rows=`2016868`, symbols=`505`, date_range=`20100104..20260703`, data_tier=`free_real`.
- Minimum symbol gate: `500`.
- Positive `amount` ratio: `0.9692414178815867`.
- Run scope: `max_strategies=all, max_windows=all, output_label=participation_5pct, max_daily_amount_participation=0.05`.
- Window rows: `14700`

## Execution Semantics

- Strategy rankings and filters come from S2/S3/S4 specs, then the target layer converts selected names into equal-weight rebalances under execution constraints.
- S2/S3 stop-loss, trailing-stop, ATR stop, `risk_per_trade`, and `max_holding_days` parameters are not implemented as separate intra-window exits in this free-real target layer; they remain candidate-spec metadata until a stricter order-policy layer is added.
- Participation-cap clipping is applied before submitting orders to the execution engine, so these rows are a more conservative free-real daily-amount stress rather than strict-real fill proof.
- `avg_turnover` is reported as traded notional divided by summed daily wealth across the window, not annualized portfolio turnover.

## Leaderboard

| strategy                                                   | family                  | deposit_timing   |   n_windows | p_success   | p_w12   | p_w24   |   median_w24 | p95_max_drawdown   | p_w24_below_deposit   |   avg_rejected_orders |   avg_clipped_orders |   avg_participation_clipped_orders |   avg_participation_blocked_orders |    score |
|:-----------------------------------------------------------|:------------------------|:-----------------|------------:|:------------|:--------|:--------|-------------:|:-------------------|:----------------------|----------------------:|---------------------:|-----------------------------------:|-----------------------------------:|---------:|
| S4_real_smallcap_factor_low_turnover_k10_weekly            | S4_real_smallcap_factor | beginning        |         175 | 6.29%       | 15.43%  | 23.43%  |      940,474 | 40.70%             | 13.71%                |               2.50857 |          11.2743     |                         0.28       |                           33.9657  |  -8.6416 |
| S4_real_smallcap_factor_low_turnover_k10_weekly            | S4_real_smallcap_factor | ending           |         175 | 5.14%       | 6.86%   | 20.00%  |      923,821 | 40.63%             | 14.29%                |               2.45143 |           8          |                         0.257143   |                           32.3486  | -10.056  |
| S4_real_smallcap_factor_low_turnover_k20_weekly            | S4_real_smallcap_factor | beginning        |         175 | 3.43%       | 6.29%   | 13.71%  |      900,705 | 38.17%             | 20.57%                |               6.02857 |           0.04       |                         0.171429   |                           77.8229  | -13.0619 |
| S4_real_smallcap_factor_low_turnover_k20_weekly            | S4_real_smallcap_factor | ending           |         175 | 2.86%       | 4.57%   | 10.86%  |      885,017 | 38.14%             | 21.14%                |               5.86286 |           0.0228571  |                         0.154286   |                           74.2914  | -13.8982 |
| S4_real_smallcap_factor_low_turnover_k50_weekly            | S4_real_smallcap_factor | beginning        |         175 | 1.71%       | 4.57%   | 6.86%   |      853,322 | 45.81%             | 19.43%                |              10.7029  |           0          |                         0.0685714  |                          185.126   | -15.0006 |
| S4_real_smallcap_factor_low_turnover_k30_weekly            | S4_real_smallcap_factor | ending           |         175 | 1.71%       | 4.00%   | 6.29%   |      862,366 | 43.73%             | 20.57%                |               8.33714 |           0          |                         0.177143   |                          118.469   | -15.7118 |
| S4_real_smallcap_factor_low_turnover_k30_weekly            | S4_real_smallcap_factor | beginning        |         175 | 1.71%       | 4.57%   | 6.86%   |      876,240 | 43.89%             | 20.57%                |               8.66286 |           0          |                         0.194286   |                          124.034   | -15.8217 |
| S4_real_smallcap_factor_low_turnover_k50_weekly            | S4_real_smallcap_factor | ending           |         175 | 0.57%       | 4.57%   | 4.57%   |      845,232 | 45.76%             | 21.14%                |              10.3314  |           0          |                         0.0571429  |                          175.92    | -16.6478 |
| S4_real_smallcap_factor_low_turnover_k10_monthly           | S4_real_smallcap_factor | beginning        |         175 | 3.43%       | 5.71%   | 11.43%  |      834,124 | 44.41%             | 21.14%                |               1.49714 |           2.74286    |                         0          |                           10.08    | -16.8696 |
| S4_real_smallcap_factor_low_turnover_k30_monthly           | S4_real_smallcap_factor | beginning        |         175 | 1.71%       | 4.00%   | 7.43%   |      836,805 | 43.91%             | 22.86%                |               3.02286 |           0          |                         0.0114286  |                           27.6514  | -17.7643 |
| S4_real_smallcap_factor_low_turnover_k30_monthly           | S4_real_smallcap_factor | ending           |         175 | 1.14%       | 4.00%   | 6.29%   |      830,425 | 43.76%             | 21.71%                |               3.04571 |           0          |                         0.00571429 |                           27.1314  | -17.8033 |
| S4_real_smallcap_factor_low_turnover_k50_monthly           | S4_real_smallcap_factor | beginning        |         175 | 1.71%       | 4.57%   | 6.86%   |      830,901 | 45.41%             | 23.43%                |               5.06286 |           0          |                         0          |                           44.4343  | -17.9197 |
| S4_real_smallcap_factor_low_turnover_k20_monthly           | S4_real_smallcap_factor | beginning        |         175 | 2.29%       | 5.14%   | 9.14%   |      858,265 | 43.51%             | 24.57%                |               2.05714 |           0.00571429 |                         0          |                           18.9771  | -18.3207 |
| S4_real_smallcap_factor_low_turnover_k50_monthly           | S4_real_smallcap_factor | ending           |         175 | 1.14%       | 4.00%   | 5.71%   |      824,424 | 45.36%             | 23.43%                |               5.01714 |           0          |                         0          |                           42.64    | -18.3619 |
| S4_real_smallcap_factor_low_turnover_k20_monthly           | S4_real_smallcap_factor | ending           |         175 | 1.71%       | 4.00%   | 8.00%   |      853,408 | 43.26%             | 25.14%                |               2.06286 |           0          |                         0          |                           18.7714  | -18.7773 |
| S4_real_smallcap_factor_low_turnover_k10_monthly           | S4_real_smallcap_factor | ending           |         175 | 1.71%       | 4.00%   | 7.43%   |      828,623 | 44.34%             | 22.29%                |               1.49714 |           1.76571    |                         0          |                            9.93143 | -18.8311 |
| S4_real_smallcap_factor_high_turnover_breakout_k50_monthly | S4_real_smallcap_factor | beginning        |         175 | 1.14%       | 4.00%   | 4.57%   |      782,808 | 47.68%             | 34.86%                |               4.65143 |           0          |                         0          |                           37.1314  | -23.6549 |
| S4_real_smallcap_factor_high_turnover_breakout_k50_monthly | S4_real_smallcap_factor | ending           |         175 | 0.00%       | 2.86%   | 4.57%   |      781,934 | 47.49%             | 34.86%                |               4.56    |           0          |                         0          |                           35.5486  | -24.5345 |
| S4_real_smallcap_factor_high_turnover_breakout_k30_monthly | S4_real_smallcap_factor | beginning        |         175 | 0.00%       | 2.86%   | 4.00%   |      774,067 | 41.82%             | 37.14%                |               3.72571 |           0          |                         0          |                           24.5371  | -26.2876 |
| S4_real_smallcap_factor_high_turnover_breakout_k30_monthly | S4_real_smallcap_factor | ending           |         175 | 0.00%       | 2.29%   | 4.00%   |      770,188 | 41.80%             | 37.71%                |               3.72    |           0          |                         0          |                           23.7657  | -26.3025 |

## Readout

- Best score: `S4_real_smallcap_factor_low_turnover_k10_weekly` / `beginning`, success=6.29%, median_w24=940,474.
- Highest success: `S4_real_smallcap_factor_low_turnover_k10_weekly` / `beginning`, success=6.29%, median_w24=940,474.
- A positive signal leaderboard is not enough; target proof requires these rolling-window success and drawdown fields.

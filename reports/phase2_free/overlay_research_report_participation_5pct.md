# Phase 2 Overlay Research

Generated at: 2026-07-07 01:48:25

## Scope

- Data tier: `proxy_overlay_research`.
- This report is not a strict-real futures/options leaderboard.
- Base stock paths come from the 505-stock `free_real` target backtest layer with a `0.0500` BaoStock daily-amount participation cap.
- Futures overlays use index proxies, integer lots, daily mark-to-market, effective margin rates, and cash-buffer gates.
- Option overlays are parametric call-budget tests: premiums are Black-Scholes approximations from realized-vol multiples, contracts are integer, and payoff is held to tenor expiry.
- Real futures/options chains remain blocked until contract-level daily data, bid/ask, volume/open-interest, margin and adjustment fields exist.

## Inputs

- Base target leaderboard: `reports/phase2_free/free_real_target_leaderboard_participation_5pct.csv`
- Base run label: `participation_5pct`
- Index close proxy: `/Volumes/PSSD1TB/量化数据/processed/phase1_daily_close.csv`
- Selected base rows: `6`
- Window rows: `109200`

## Selected Base Strategies

| strategy                                        | family                  | deposit_timing   | p_success   |   median_w24 | p95_max_drawdown   |    score |
|:------------------------------------------------|:------------------------|:-----------------|:------------|-------------:|:-------------------|---------:|
| S4_real_smallcap_factor_low_turnover_k10_weekly | S4_real_smallcap_factor | beginning        | 6.29%       |      940,474 | 40.70%             |  -8.6416 |
| S4_real_smallcap_factor_low_turnover_k10_weekly | S4_real_smallcap_factor | ending           | 5.14%       |      923,821 | 40.63%             | -10.056  |
| S4_real_smallcap_factor_low_turnover_k20_weekly | S4_real_smallcap_factor | beginning        | 3.43%       |      900,705 | 38.17%             | -13.0619 |
| S4_real_smallcap_factor_low_turnover_k20_weekly | S4_real_smallcap_factor | ending           | 2.86%       |      885,017 | 38.14%             | -13.8982 |
| S2_real_stock_momentum_k10_weekly               | S2_real_stock_momentum  | ending           | 0.00%       |      426,613 | 59.94%             | -66.3233 |
| S3_real_stock_breakout_d55_r0.005               | S3_real_stock_breakout  | ending           | 0.00%       |      398,934 | 56.74%             | -81.6542 |

## Overlay Leaderboard

| base_strategy                                   | overlay_type                  | overlay_name                              |   n_windows | p_success   |   median_w24 | p95_max_drawdown   | p_w24_below_deposit   |   avg_futures_cannot_afford |   avg_futures_forced_liquidations |   avg_option_premium_spent |   avg_option_payoff |     score |
|:------------------------------------------------|:------------------------------|:------------------------------------------|------------:|:------------|-------------:|:-------------------|:----------------------|----------------------------:|----------------------------------:|---------------------------:|--------------------:|----------:|
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_IO_call_budget0.05_t30_d0.5_iv1.3  |         175 | 8.57%       |      919,545 | 38.43%             | 12.57%                |                      0      |                                 0 |                    131,840 |             191,752 | -0.523961 |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_IO_call_budget0.02_t30_d0.35_iv1.3 |         175 | 9.14%       |      928,490 | 38.36%             | 15.43%                |                      0      |                                 0 |                     59,760 |             130,684 | -0.616069 |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_HO_call_budget0.02_t30_d0.35_iv1.3 |         175 | 8.57%       |      934,040 | 39.74%             | 16.00%                |                      0      |                                 0 |                     67,896 |              96,023 | -1.23972  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_IO_call_budget0.05_t30_d0.35_iv1.3 |         175 | 9.14%       |      909,949 | 38.44%             | 18.29%                |                      0      |                                 0 |                    108,920 |             157,272 | -1.87662  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_IO_call_budget0.05_t30_d0.5_iv1.6  |         175 | 8.00%       |      911,304 | 39.97%             | 14.86%                |                      0      |                                 0 |                    104,128 |             107,826 | -1.96116  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_IO_call_budget0.01_t30_d0.35_iv1.3 |         175 | 7.43%       |      939,736 | 39.64%             | 15.43%                |                      0      |                                 0 |                     27,333 |              60,900 | -2.08956  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_IO_call_budget0.05_t30_d0.5_iv1.3  |         175 | 7.43%       |      907,942 | 38.25%             | 13.14%                |                      0      |                                 0 |                    121,894 |             180,564 | -2.09222  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_MO_call_budget0.05_t30_d0.35_iv1.3 |         175 | 8.57%       |      911,899 | 37.90%             | 17.71%                |                      0      |                                 0 |                     97,567 |             187,558 | -2.23381  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_IO_call_budget0.02_t30_d0.5_iv1.3  |         175 | 6.86%       |      933,777 | 39.76%             | 13.71%                |                      0      |                                 0 |                     45,619 |              69,681 | -2.27392  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_HO_call_budget0.02_t30_d0.5_iv1.3  |         175 | 6.86%       |      933,433 | 40.59%             | 13.71%                |                      0      |                                 0 |                     51,373 |              48,304 | -2.2813   |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_IO_call_budget0.05_t60_d0.5_iv1.3  |         175 | 7.43%       |      914,406 | 38.37%             | 14.86%                |                      0      |                                 0 |                     88,417 |             132,764 | -2.46461  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_HO_call_budget0.05_t30_d0.5_iv1.3  |         175 | 7.43%       |      925,277 | 42.29%             | 16.00%                |                      0      |                                 0 |                    108,431 |              95,782 | -2.5711   |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_HO_call_budget0.05_t30_d0.35_iv1.3 |         175 | 8.57%       |      909,831 | 41.79%             | 18.86%                |                      0      |                                 0 |                    108,237 |             117,675 | -2.62207  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_IO_call_budget0.05_t60_d0.5_iv1.6  |         175 | 7.43%       |      914,613 | 39.92%             | 15.43%                |                      0      |                                 0 |                     72,513 |              77,983 | -2.63152  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | parametric_option_call_budget | option_HO_call_budget0.01_t30_d0.35_iv1.3 |         175 | 6.86%       |      938,931 | 40.44%             | 15.43%                |                      0      |                                 0 |                     33,218 |              42,223 | -2.67813  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | futures_integer_lot_proxy     | futures_IC_beta0.3_margin0.15_buffer0.33  |         175 | 6.29%       |      940,474 | 40.70%             | 13.71%                |                     10.2114 |                                 0 |                          0 |                   0 | -2.70242  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | futures_integer_lot_proxy     | futures_IC_beta0.3_margin0.15_buffer0.5   |         175 | 6.29%       |      940,474 | 40.70%             | 13.71%                |                     10.2114 |                                 0 |                          0 |                   0 | -2.70242  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | futures_integer_lot_proxy     | futures_IC_beta0.3_margin0.2_buffer0.33   |         175 | 6.29%       |      940,474 | 40.70%             | 13.71%                |                     10.2114 |                                 0 |                          0 |                   0 | -2.70242  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | futures_integer_lot_proxy     | futures_IC_beta0.3_margin0.2_buffer0.5    |         175 | 6.29%       |      940,474 | 40.70%             | 13.71%                |                     10.2114 |                                 0 |                          0 |                   0 | -2.70242  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | futures_integer_lot_proxy     | futures_IC_beta0.5_margin0.15_buffer0.33  |         175 | 6.29%       |      940,474 | 40.70%             | 13.71%                |                     10.2114 |                                 0 |                          0 |                   0 | -2.70242  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | futures_integer_lot_proxy     | futures_IC_beta0.5_margin0.15_buffer0.5   |         175 | 6.29%       |      940,474 | 40.70%             | 13.71%                |                     10.2114 |                                 0 |                          0 |                   0 | -2.70242  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | futures_integer_lot_proxy     | futures_IC_beta0.5_margin0.2_buffer0.33   |         175 | 6.29%       |      940,474 | 40.70%             | 13.71%                |                     10.2114 |                                 0 |                          0 |                   0 | -2.70242  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | futures_integer_lot_proxy     | futures_IC_beta0.5_margin0.2_buffer0.5    |         175 | 6.29%       |      940,474 | 40.70%             | 13.71%                |                     10.2114 |                                 0 |                          0 |                   0 | -2.70242  |
| S4_real_smallcap_factor_low_turnover_k10_weekly | futures_integer_lot_proxy     | futures_IF_beta0.3_margin0.15_buffer0.33  |         175 | 6.29%       |      940,474 | 40.70%             | 13.71%                |                     10.5257 |                                 0 |                          0 |                   0 | -2.70242  |

## Readout

- Best overlay score: `option_IO_call_budget0.05_t30_d0.5_iv1.3` on `S4_real_smallcap_factor_low_turnover_k10_weekly`, success=8.57%, median_w24=919,545, p95 drawdown=38.43%.
- Highest success: `option_IO_call_budget0.02_t30_d0.35_iv1.3` on `S4_real_smallcap_factor_low_turnover_k10_weekly`, success=9.14%, median_w24=928,490.
- If this table does not materially beat the selected base free-real rows under the same stock execution assumptions, derivatives should not be treated as a shortcut; the next honest step is either real contract data or a different base strategy family.
- Best futures proxy keeps success at 6.29%; average cannot-afford events per window are 10.21, showing integer-lot and cash-buffer constraints dominate.

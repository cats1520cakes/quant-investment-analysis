# Phase 2 Free Real Data Validation

## Leaderboard Tiers

- `strict_real_leaderboard`: remains blocked unless paid/official fields exist (`official stk_limit`, `suspend_d`, `daily_basic`, `adj_factor`, futures/options chains).
- `free_real_leaderboard`: blocked until BaoStock raw/qfq data or free panel exists; uses BaoStock/AKShare derived fields.
- `proxy_research_leaderboard`: Qlib/index proxy only; cannot enter real leaderboards.

## Raw Table Status

| table          |   files |   rows | present   | columns                                                                                                                                                           |
|:---------------|--------:|-------:|:----------|:------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| stock_basic    |       1 |   8819 | True      | delist_date, list_date, list_status, name, source_code, ts_code, type                                                                                             |
| trade_calendar |       1 |   6029 | True      | is_open, trade_date                                                                                                                                               |
| daily_raw      |       6 |  24030 | True      | amount, close, high, is_st_raw, low, open, pb, pcf_ttm, pct_chg, pe_ttm, pre_close, ps_ttm, source_code, trade_date, trade_status, ts_code, turnover_rate, volume |
| daily_qfq      |       6 |  24030 | True      | adj_close_for_signal, adj_high_for_signal, adj_low_for_signal, adj_open_for_signal, source_code, trade_date, ts_code                                              |

## Panel Status

- `/Volumes/PSSD1TB/量化数据/processed/phase2_free/stock_panel.parquet`: missing panel: /Volumes/PSSD1TB/量化数据/processed/phase2_free/stock_panel.parquet
- matched listed stock daily files: `0`

## Strategy Admission

| strategy                | data_tier      | allowed   | reason                                                                                   |
|:------------------------|:---------------|:----------|:-----------------------------------------------------------------------------------------|
| S2_real_stock_momentum  | free_real      | True      |                                                                                          |
| S3_real_stock_breakout  | free_real      | True      |                                                                                          |
| S4_real_smallcap_factor | free_real      | True      |                                                                                          |
| S5_real_limitup_board   | free_real      | False     | free_real lacks official queue/limit-order evidence for strict limit-up board strategies |
| S2_real_stock_momentum  | proxy_research | False     | proxy_research cannot enter real leaderboards                                            |

## Required Disclaimers

- `up_limit` and `down_limit` are derived from `pre_close` and board rules in `free_real`.
- `is_suspended` uses BaoStock `tradestatus`; this is proxy evidence, not strict official `suspend_d`.
- `is_st` uses BaoStock `isST`; this is sufficient for free-real filters but not a full `namechange` table.
- `circ_mv_approx` is derived from `amount / (turnover_rate / 100)` and must not be renamed to official `circ_mv` in free-real reports.

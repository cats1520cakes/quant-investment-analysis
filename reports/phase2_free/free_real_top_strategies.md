# Phase 2 Free Real Top Strategies

- Data tier: `free_real`.
- Strict real leaderboard remains separate and blocked until official/paid-grade fields exist.
- `up_limit/down_limit` are derived; `is_suspended` is BaoStock `tradestatus` proxy.

| strategy                         | family                 | data_tier   | leaderboard_tier   | allowed   |   n_signal_days |   eligible_rows |   avg_top_score |   median_top_score | uses_derived_limits   | uses_suspension_proxy   | market_cap_source            | blocked_reason   |
|:---------------------------------|:-----------------------|:------------|:-------------------|:----------|----------------:|----------------:|----------------:|-------------------:|:----------------------|:------------------------|:-----------------------------|:-----------------|
| S2_real_stock_momentum_k1_daily  | S2_real_stock_momentum | free_real   | free_real          | True      |            3954 |            3954 |       0.0510374 |          0.0312519 | True                  | True                    | derived_from_amount_turnover |                  |
| S2_real_stock_momentum_k1_2d     | S2_real_stock_momentum | free_real   | free_real          | True      |            3954 |            3954 |       0.0510374 |          0.0312519 | True                  | True                    | derived_from_amount_turnover |                  |
| S2_real_stock_momentum_k1_weekly | S2_real_stock_momentum | free_real   | free_real          | True      |            3954 |            3954 |       0.0510374 |          0.0312519 | True                  | True                    | derived_from_amount_turnover |                  |

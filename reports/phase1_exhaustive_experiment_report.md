# 附文第一阶段实验证明报告

生成时间：2026-07-04 16:10:39

## 结论摘要

- 历史滚动窗口最优策略：`S1_mom_lb20_top1_2d_ma200`，入金口径 `beginning`。
- 达标概率：2.86%；24 个月期末资产中位数：790,027。
- 历史滚动最高达标率策略：`S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.07`，入金口径 `beginning`，达标概率 4.57%，但 24 个月资产中位数只有 714,361。
- 95 分位最大回撤：20.59%；24 个月低于累计入金概率：16.00%。
- 数据边界：BaoStock 指数日频代理，覆盖沪深300、中证500、中证1000、创业板、创业板50、上证50、上证、深证、中小100；用于第一阶段市场状态和操作族筛查，不等价于真实 ETF / 个股撮合。
- 第一阶段结果不覆盖全 A 个股涨跌停排队、停牌、ST、退市、融资融券逐日担保比例、股指期货合约粒度和期权 IV 曲面。

## 数据与落盘

- 原始数据目录：`/Volumes/PSSD1TB/量化数据/raw/baostock/index_daily`
- 处理后收盘价：`/Volumes/PSSD1TB/量化数据/processed/phase1_daily_close.csv`
- 下载 manifest：`/Volumes/PSSD1TB/量化数据/00_meta/manifests/phase1_daily_manifest.csv`
- 外置盘结果目录：`/Volumes/PSSD1TB/量化数据/reports`

## 历史滚动窗口 Leaderboard

| strategy                             | deposit_timing   |   n_windows | p_success   |   median_w24 | p95_max_drawdown   | p_w24_below_deposit   |   score |
|:-------------------------------------|:-----------------|------------:|:------------|-------------:|:-------------------|:----------------------|--------:|
| S1_mom_lb20_top1_2d_ma200            | beginning        |         175 | 2.86%       |      790,027 | 20.59%             | 16.00%                |  -12.28 |
| S1_mom_lb20_top1_2d_dual_ma60_200    | beginning        |         175 | 3.43%       |      764,109 | 13.80%             | 21.14%                |  -13.79 |
| S1_ramom_lb20_top1_2d_ma200          | beginning        |         175 | 2.29%       |      772,379 | 19.22%             | 17.14%                |  -13.93 |
| S1_mom_lb20_top1_2d_ma200            | ending           |         175 | 2.29%       |      771,504 | 20.59%             | 19.43%                |  -14.47 |
| S1_mom_lb20_top1_daily_ma200         | beginning        |         175 | 2.86%       |      775,803 | 20.59%             | 20.57%                |  -14.74 |
| S1_mom_lb20_top1_2d_none             | beginning        |         175 | 3.43%       |      814,127 | 20.59%             | 24.00%                |  -14.8  |
| S1_mom_lb20_top2_2d_ma200            | beginning        |         175 | 2.29%       |      775,715 | 20.43%             | 22.29%                |  -15.4  |
| S1_mom_lb20_top1_daily_dual_ma60_200 | beginning        |         175 | 3.43%       |      752,522 | 15.16%             | 24.00%                |  -15.51 |
| S1_mom_lb20_top1_2d_ma120            | beginning        |         175 | 2.86%       |      776,336 | 20.59%             | 24.00%                |  -15.57 |
| S1_mom_lb20_top1_daily_none          | beginning        |         175 | 3.43%       |      817,009 | 20.59%             | 25.14%                |  -15.95 |
| S1_mom_lb20_top3_2d_ma200            | beginning        |         175 | 1.71%       |      769,281 | 18.31%             | 22.86%                |  -16.23 |
| S1_mom_lb10_top2_2d_ma200            | beginning        |         175 | 1.14%       |      772,496 | 27.42%             | 21.14%                |  -16.61 |


## Bootstrap 摘要

| strategy                                  | family   | deposit_timing   |   block_size |   paths | p_success   |   median_w24 | p95_max_drawdown   |
|:------------------------------------------|:---------|:-----------------|-------------:|--------:|:------------|-------------:|:-------------------|
| S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.07 | S7       | beginning        |           20 |    2000 | 6.10%       |      819,068 | 48.62%             |
| S7_mom_lb20_top1_weekly_ma120_x2.0_fr0.07 | S7       | beginning        |           60 |    2000 | 5.50%       |      745,808 | 48.65%             |
| S7_mom_lb20_top1_weekly_ma200_x1.8_fr0.07 | S7       | beginning        |           60 |    2000 | 5.35%       |      773,613 | 44.85%             |
| S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.07 | S7       | beginning        |           60 |    2000 | 5.30%       |      775,732 | 48.65%             |
| S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.07 | S7       | beginning        |            5 |    2000 | 5.25%       |      824,923 | 41.03%             |
| S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.09 | S7       | beginning        |           60 |    2000 | 5.25%       |      764,278 | 48.74%             |
| S7_mom_lb20_top1_weekly_ma120_x1.8_fr0.09 | S7       | beginning        |           60 |    2000 | 5.25%       |      739,617 | 44.90%             |
| S7_mom_lb20_top1_weekly_ma120_x2.0_fr0.07 | S7       | ending           |           60 |    2000 | 5.20%       |      745,901 | 48.62%             |
| S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.12 | S7       | beginning        |           20 |    2000 | 5.10%       |      769,190 | 48.67%             |
| S7_mom_lb20_top1_weekly_ma120_x1.8_fr0.07 | S7       | beginning        |           60 |    2000 | 5.10%       |      748,216 | 44.82%             |
| S7_mom_lb20_top1_weekly_ma120_x2.0_fr0.07 | S7       | beginning        |           20 |    2000 | 5.00%       |      781,711 | 48.60%             |
| S1_mom_lb20_top1_daily_none               | S1       | beginning        |           60 |    2000 | 4.95%       |      904,170 | 20.59%             |


## 解释边界

- 如果所有策略达标概率接近 0，结论不是“目标绝对不可能”，而是在本轮指数代理、日线交易、费用滑点和 24 个月窗口下，没有找到能稳定满足硬目标的操作族。
- 如果轻度融资版本提高达标概率但显著推高回撤或亏损概率，应进入 Phase 2 做逐日担保比例、强平和融资利率压力测试后再评价。
- 个股强势股、涨停/连板、事件驱动、期货和期权 overlay 需要更完整的 A 股撮合层与数据表，不能用本轮 ETF 结果替代。

## 下一步

1. 加入 AKShare / Tushare / 付费源交叉验证 ETF 可交易价格、复权因子、停牌、ST 和涨跌停价格。
2. 建全 A 股票池与动态上市/退市过滤，再运行 S2/S3/S4。
3. 对候选策略做 2015-like crash、滑点放大、成交率坍塌和参数邻域稳定性测试。

## 压力测试摘要

| stress_case         | strategy                                          | deposit_timing   | p_success   |   median_w24 | p95_max_drawdown   |    score |
|:--------------------|:--------------------------------------------------|:-----------------|:------------|-------------:|:-------------------|---------:|
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_dual_ma60_200_x1.5_fr0.07 | beginning        | 3.43%       |      709,428 | 38.76%             | -31.0732 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_dual_ma60_200_x1.5_fr0.09 | beginning        | 3.43%       |      709,428 | 38.76%             | -31.0732 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_dual_ma60_200_x1.5_fr0.12 | beginning        | 3.43%       |      709,428 | 38.76%             | -31.0732 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x1.8_fr0.07         | beginning        | 4.00%       |      721,105 | 51.32%             | -32.6409 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x1.8_fr0.09         | beginning        | 4.00%       |      721,105 | 51.32%             | -32.6409 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x1.8_fr0.12         | beginning        | 4.00%       |      721,105 | 51.32%             | -32.6409 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.07         | beginning        | 4.57%       |      714,361 | 56.07%             | -32.9461 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.09         | beginning        | 4.57%       |      714,361 | 56.07%             | -32.9461 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.12         | beginning        | 4.57%       |      714,361 | 56.07%             | -32.9461 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x1.8_fr0.07         | ending           | 3.43%       |      713,224 | 51.00%             | -33.9463 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x1.8_fr0.09         | ending           | 3.43%       |      713,224 | 51.00%             | -33.9463 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x1.8_fr0.12         | ending           | 3.43%       |      713,224 | 51.00%             | -33.9463 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_dual_ma60_200_x1.8_fr0.07 | beginning        | 4.00%       |      693,430 | 44.88%             | -34.1102 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_dual_ma60_200_x1.8_fr0.09 | beginning        | 4.00%       |      693,430 | 44.88%             | -34.1102 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_dual_ma60_200_x1.5_fr0.07 | ending           | 2.86%       |      700,758 | 38.68%             | -34.1166 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_dual_ma60_200_x1.5_fr0.09 | ending           | 2.86%       |      700,758 | 38.68%             | -34.1166 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_dual_ma60_200_x1.5_fr0.12 | ending           | 2.86%       |      700,758 | 38.68%             | -34.1166 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma120_x1.8_fr0.07         | beginning        | 4.00%       |      691,355 | 44.88%             | -34.5652 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma120_x1.8_fr0.09         | beginning        | 4.00%       |      691,355 | 44.88%             | -34.5652 |
| financing_rate_0.07 | S7_mom_lb20_top1_weekly_ma200_x2.0_fr0.07         | ending           | 4.00%       |      703,194 | 55.80%             | -34.7404 |

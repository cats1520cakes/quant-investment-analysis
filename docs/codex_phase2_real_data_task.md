# Codex Phase 2 真实数据任务

Phase 1 的指数代理穷尽模拟只能作为目标难度下限。Phase 2 必须切换到真实数据层，不能把指数代理、当前指数成分回填、连续期货合约或参数化期权近似结果放进真实排行榜。

## 目标

使用 Tushare Pro 为主源，AKShare / BaoStock 为备源，把真实全 A、ETF、股指期货、股指/ETF 期权数据落盘到：

```text
/Volumes/PSSD1TB/量化数据
```

必须输出：

```text
/Volumes/PSSD1TB/量化数据/00_meta/manifests/phase2_real_data_manifest.csv
/Volumes/PSSD1TB/量化数据/reports/phase2_real_data_validation.md
```

## 先小样本

```bash
export TUSHARE_TOKEN="..."

uv run python scripts/download_phase2_real_data.py \
  --config config/phase2_real_data.yaml \
  --tables stock_basic,trade_cal,daily,adj_factor,daily_basic,stk_limit \
  --max-codes 10

uv run python scripts/validate_phase2_real_data.py \
  --config config/phase2_real_data.yaml
```

## 全量数据表

关键表：

- `trade_cal`
- `stock_basic`
- `daily`
- `adj_factor`
- `daily_basic`
- `stk_limit`
- `suspend_d`
- `namechange`
- `index_daily`
- `index_weight`
- `fund_basic`
- `fund_daily`
- `fut_basic`
- `fut_daily`
- `opt_basic`
- `opt_daily`

## 不允许进入 Phase 2 真实排行榜的情况

- 用指数代理 ETF。
- 用当前指数成分回填历史。
- 删除退市股票。
- 没有停牌数据却做个股回测。
- 没有涨跌停价却做涨停/打板/个股动量策略。
- 用复权价撮合成交。
- 用连续期货合约代替真实合约逐日盯市。
- 用指数收益粗略替代期权收益。

## 真实个股回测硬约束

- 信号可用复权价。
- 成交必须用未复权真实价格。
- 停牌不能成交。
- 涨停买入不能默认成交。
- 跌停卖出不能默认成交。
- T+1 必须生效。
- 退市样本必须保留。
- ST / 名称变更状态必须按历史日期识别。

## 重跑策略族

- `S2_real_stock_momentum`
- `S3_real_stock_breakout`
- `S4_real_smallcap_factor`
- `S5_real_limitup_model`
- `S7_real_margin_stock_overlay`
- `S8_real_index_futures_overlay`
- `S9_real_etf_options_overlay`
- `S10_real_mixed_allocator`

先看这些列：

- `P_success`
- `P_12_success`
- `P_24_success`
- `median_W24`
- `p5_W24`
- `max_drawdown_p95`
- `margin_call_prob`
- `liquidity_trap_prob`
- `fee_drag`
- `slippage_drag`
- `parameter_stability`


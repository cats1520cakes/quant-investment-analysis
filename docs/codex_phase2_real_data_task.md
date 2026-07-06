# Codex Phase 2 真实数据任务

Phase 1 的指数代理穷尽模拟只能作为目标难度下限。Phase 2 必须切换到分层数据门禁，不能把指数代理、当前指数成分回填、连续期货合约或参数化期权近似结果混进严格真实排行榜。

## 目标

严格真实榜使用 Tushare Pro / 官方级字段。免费真实近似榜使用 BaoStock 为主源、AKShare 为校验/补充源、Qlib 只作 proxy research。数据落盘到：

```text
/Volumes/PSSD1TB/量化数据
```

必须输出：

```text
/Volumes/PSSD1TB/量化数据/00_meta/manifests/phase2_real_data_manifest.csv
/Volumes/PSSD1TB/量化数据/reports/phase2_real_data_validation.md
```

## 三层门禁

- `strict_real`：需要官方/付费级 `stk_limit`, `suspend_d`, `namechange`, `daily_basic`, `adj_factor`, 真实期货/期权链；缺失则阻断。
- `free_real`：BaoStock/AKShare 免费字段，允许 S2/S3/S4，但必须标注停牌、ST、涨跌停和市值为代理或派生字段。
- `proxy_research`：Qlib / 指数代理 / 缺成交限制数据，只做预筛，不能进入 real leaderboard。

所有下载脚本默认启用 direct mode：清理代理环境变量、设置 `NO_PROXY=*`、禁用 Python proxy discovery，并设置 socket timeout；如果显式传 `--allow-proxy`，才允许代理路径。

## 先小样本

```bash
export TUSHARE_TOKEN="..."

uv run python scripts/download_phase2_real_data.py \
  --config config/phase2_real_data.yaml \
  --tables stock_basic,trade_cal,daily,adj_factor,daily_basic,stk_limit,suspend_d,namechange \
  --max-codes 10 \
  --max-dates 30

uv run python scripts/validate_phase2_real_data.py \
  --config config/phase2_real_data.yaml
```

## Free Real smoke / 500+ 样本

```bash
uv run python scripts/download_phase2_free_real_data.py \
  --config config/phase2_free_real_data.yaml \
  --max-codes 100

uv run python scripts/validate_phase2_free_real_data.py \
  --config config/phase2_free_real_data.yaml

uv run python scripts/build_phase2_free_stock_panel.py \
  --config config/phase2_free_real_data.yaml

uv run python scripts/run_phase2_free_real_experiment.py \
  --config config/phase2_free_real_data.yaml

uv run python scripts/run_phase2_free_real_target_backtest.py \
  --config config/phase2_free_real_data.yaml

uv run python scripts/run_phase2_overlay_research.py \
  --config config/phase2_free_real_data.yaml
```

`--max-codes 100` 只作为 smoke 示例。当前 canonical free-real panel 是 505 只 raw+qfq+上市普通股匹配股票、2,016,868 行、日期覆盖 `20100104` 到 `20260703`。更大 BaoStock 样本应使用低并发分片，例如 `--start-index 100 --end-index 200`，或用 `--codes-file` 重试缺失代码；高并发突发请求可能使 BaoStock 登录态失效。

`run_phase2_overlay_research.py` 只生成 `proxy_overlay_research` 压力层：股指期货使用指数代理和整手/保证金/现金 buffer 约束，期权使用参数化 call-budget 近似；它不进入 strict-real 或 free-real leaderboard。

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
- `S8_real_index_futures_overlay`
- `S9_real_etf_options_overlay`

当前算力优先级是 S2/S3/S4。`S5_real_limitup_model`、融资融券增强和混合 allocator 必须等真实个股撮合层稳定后再进入。

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

## 当前代码入口

- `scripts/validate_phase2_real_data.py`：真实数据门禁，不允许缺表或字段不全时进榜。
- `scripts/build_phase2_realdata.py`：生成 `processed/phase2` 的真实个股面板。
- `src/quant_proof/realdata/`：交易日历、股票池、复权因子、涨跌停、停牌、ST 和 `stock_panel` 构造。
- `src/quant_proof/engine/`：真实撮合规则骨架。
- `src/quant_proof/real_strategies.py`：S2/S3/S4 真实个股候选策略规格与信号打分。

真实 leaderboard 输出只能在 validation 通过后生成：

```text
reports/phase2/real_stock_windows.csv
reports/phase2/real_stock_leaderboard.csv
reports/phase2/real_stock_top_strategies.md
reports/phase2/real_stock_regime_breakdown.csv
reports/phase2/equity_curves/*.csv
reports/phase2/drawdown_curves/*.csv
```

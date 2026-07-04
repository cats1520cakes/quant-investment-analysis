# 量化投资分析：Phase 1 指数代理与 Phase 2 真实个股线

这个工程把附文中的收益目标、入金约束、A 股交易成本和策略族，落成可运行的仿真实验。

大体量行情数据不放在仓库里，默认放到外置硬盘：

```bash
/Volumes/PSSD1TB/量化数据
```

## Phase 1：指数代理筛查

第一阶段下载 BaoStock 指数日线代理数据，验证：

- 月初 / 月末每月入金 30,000 的资金流口径；
- `W_12 >= 500000` 且 `W_24 >= 1200000` 的硬目标；
- 基准定投、指数等权、指数动量轮动、带现金过滤和轻度融资的原型策略；
- 真实历史 24 个月滚动窗口和区块 bootstrap；
- 费用、滑点、换手、融资利息对目标达成概率的影响。

运行：

```bash
uv sync
uv run python scripts/download_phase1_data.py
uv run python scripts/run_phase1_experiment.py
```

输出：

- 外置盘数据：`/Volumes/PSSD1TB/量化数据/raw`、`processed`、`reports`、`00_meta/manifests`
- 工作区报告：`reports/phase1_experiment_report.md`

Phase 1 结果已经说明：指数代理、简单轮动、轻度融资这条线的达标率只有低个位数，不能作为主方案。它只用于市场状态和操作族筛查，不等价于真实 ETF、真实个股撮合、股指期货或期权回测。

## Phase 2：真实数据准入

Phase 2 的重点算力方向是：

- `S2_real_stock_momentum`
- `S3_real_stock_breakout`
- `S4_real_smallcap_factor`
- 通过真实股指期货合约做整手 overlay
- 通过真实期权链或明确标注的参数化压力层做凸性预算

真实排行榜必须先通过数据门禁；不得静默降级为 Phase 1 指数代理，不得用当前成分回填历史，不得删除退市样本。

```bash
uv run python scripts/validate_phase2_real_data.py --config config/phase2_real_data.yaml
```

当前本机还没有 `TUSHARE_TOKEN` 和 Phase 2 真实表，验证报告会阻断真实排行榜：

```text
/Volumes/PSSD1TB/量化数据/reports/phase2_real_data_validation.md
```

## Phase 2 小样本下载

```bash
export TUSHARE_TOKEN="..."

uv run python scripts/download_phase2_real_data.py \
  --config config/phase2_real_data.yaml \
  --tables stock_basic,trade_cal,daily,adj_factor,daily_basic,stk_limit,suspend_d,namechange \
  --max-codes 10 \
  --max-dates 30

uv run python scripts/validate_phase2_real_data.py --config config/phase2_real_data.yaml
```

## Phase 2 处理与撮合入口

真实个股处理层：

```bash
uv run python scripts/build_phase2_realdata.py --config config/phase2_real_data.yaml
```

该脚本从 `/Volumes/PSSD1TB/量化数据/raw/phase2` 或 `/Volumes/PSSD1TB/量化数据/raw/tushare` 读取真实表，生成：

```text
/Volumes/PSSD1TB/量化数据/processed/phase2/stock_panel.parquet
```

`stock_panel` 保留 raw `open/high/low/close` 作为执行价，只把 `adj_close_for_signal` 用作信号复权价。撮合层在 `src/quant_proof/engine`，覆盖 T+1、停牌不可成交、涨停买入失败、跌停卖出失败、卖出印花税和单笔成交金额上限。真实个股策略入口在 `src/quant_proof/real_strategies.py`，默认生成 S2/S3/S4 的候选参数族。

## 测试

```bash
uv sync --dev
uv run python -m compileall src scripts tests
uv run pytest -q
```

这不是投资建议，也不证明某个策略可以实盘稳定达到目标。工程目标是把目标约束转成可复验的筛选实验，系统寻找哪些操作族在真实数据、真实约束和压力场景下更接近目标。

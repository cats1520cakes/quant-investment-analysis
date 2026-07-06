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

三层数据门禁：

| Tier | 用途 | 可进榜策略 | 禁止项 |
| --- | --- | --- | --- |
| `strict_real` | 付费级 / 官方级字段 | 严格真实榜 | 缺 `stk_limit/suspend_d/daily_basic/adj_factor` 时阻断 |
| `free_real` | BaoStock 主源 + AKShare 校验/补充 | S2/S3/S4 免费真实近似榜 | S5 打板、期货 overlay、期权 overlay |
| `proxy_research` | Qlib / 指数代理 / 缺成交限制数据 | 预筛研究 | 不允许进入 real leaderboard |

```bash
uv run python scripts/validate_phase2_real_data.py --config config/phase2_real_data.yaml
```

当前本机还没有 `TUSHARE_TOKEN` 和 Phase 2 真实表，验证报告会阻断真实排行榜：

```text
/Volumes/PSSD1TB/量化数据/reports/phase2_real_data_validation.md
```

## Phase 2 小样本下载

所有行情下载脚本默认启用 direct mode：清理代理环境变量、设置 `NO_PROXY=*`、禁用 Python proxy discovery，并设置 socket timeout。即使 macOS 系统代理仍指向 `127.0.0.1:1082`，脚本也会尝试让本进程行情请求直连；只有显式传 `--allow-proxy` 才允许代理路径。

```bash
export TUSHARE_TOKEN="..."

uv run python scripts/download_phase2_real_data.py \
  --config config/phase2_real_data.yaml \
  --tables stock_basic,trade_cal,daily,adj_factor,daily_basic,stk_limit,suspend_d,namechange \
  --max-codes 10 \
  --max-dates 30

uv run python scripts/validate_phase2_real_data.py --config config/phase2_real_data.yaml
```

## Phase 2 Free Real 路线

不走 Tushare 付费 API 时，使用 BaoStock 免费源建立 `free_real` 面板：

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
```

上面的 `--max-codes 100` 是 smoke 示例。当前已完成 BaoStock direct-mode 505 只 raw+qfq+上市普通股匹配样本：`processed/phase2_free/stock_panel.parquet` 为 2,016,868 行、505 只股票，覆盖 `20100104` 到 `20260703`。完整 free-real 预榜覆盖 42 个 S2/S3/S4 规格；这仍是免费真实近似榜，不是 strict-real 真实排行榜或统计结论。BaoStock 高并发可能触发登录态失效，建议用 `--start-index/--end-index` 或 `--codes-file` 做低并发分片续跑。

Target backtest 已在 505 只样本上覆盖 42 个 S2/S3/S4 规格、14,700 个 24 月滚动窗口、月入金 30,000、硬目标 `W_12 >= 500000` 且 `W_24 >= 1200000`。当前最优为 `S4_real_smallcap_factor_low_turnover_k10_weekly` / beginning，达标率 6.29%、24 月中位资产 940,474、p95 最大回撤 40.70%；S2/S3 family best 的达标率仍为 0%。这说明信号预榜不能替代目标约束回测，当前 S2/S3/S4 free-real 线仍未形成主方案证据。

衍生品 overlay 已作为 `proxy_overlay_research` 压力层单独测试：6 个 base 策略/入金组合、32 个整手期货规格、72 个参数化期权 call-budget 规格、109,200 个 overlay-window。最高成功率为 9.14%（`option_IO_call_budget0.02_t30_d0.35_iv1.3` on `S4_real_smallcap_factor_low_turnover_k10_weekly`），仍未形成主方案证据；最佳期货整手 proxy 仍停在 6.29%，平均每窗 10.21 次买不起/不能开够一手，说明早期资金约束主导。

`free_real` 字段边界：

- raw `open/high/low/close/pre_close` 来自 BaoStock `adjustflag=3`，只用于撮合。
- `adj_close_for_signal` 来自 BaoStock `adjustflag=2` 或 AKShare qfq，只用于信号。
- `is_suspended` 来自 BaoStock `tradestatus != 1`，是停牌代理证据。
- `is_st` 来自 BaoStock `isST`，不是完整 `namechange`。
- `up_limit/down_limit` 由 `pre_close` 和板块规则派生，标记为 `derived`。
- `circ_mv_approx` 由 `amount / (turnover_rate / 100)` 近似，不能写成官方 `circ_mv`。

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

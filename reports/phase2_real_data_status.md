# Phase 2 真实数据状态

## 已接入

- 配置：`config/phase2_real_data.yaml`
- 下载脚本：`scripts/download_phase2_real_data.py`
- 验证脚本：`scripts/validate_phase2_real_data.py`
- 任务说明：`docs/codex_phase2_real_data_task.md`
- 个股/期货/期权扩展方案：`docs/phase2_stock_derivatives_plan.md`
- 缺口报告：`/Volumes/PSSD1TB/量化数据/reports/phase2_real_data_validation.md`

## 当前状态

本机当前没有 `TUSHARE_TOKEN` 环境变量，因此还没有真实 Phase 2 数据落盘。验证报告已经升级为字段级和策略门禁级检查；当前显示以下表均缺失：

- 个股：`trade_cal`, `stock_basic`, `daily`, `adj_factor`, `daily_basic`, `stk_limit`, `suspend_d`, `namechange`
- ETF：`fund_basic`, `fund_daily`
- 期货：`fut_basic`, `fut_daily`
- 期权：`opt_basic`, `opt_daily`

因此当前不允许生成 Phase 2 真实排行榜；指数代理结果只能作为 Phase 1 基线。当前明确禁止进榜：

- `S2_real_stock_momentum`
- `S3_real_stock_breakout`
- `S4_real_smallcap_factor`
- `index_futures_integer_lot_overlay`
- `option_convexity_budget`

原因是缺少真实行情、复权因子、日频基本面、涨跌停、停牌、ST/退市和真实衍生品合约数据。

## Free Real 当前状态

已新增免费路线：

- 配置：`config/phase2_free_real_data.yaml`
- 下载：`scripts/download_phase2_free_real_data.py`
- 验证：`scripts/validate_phase2_free_real_data.py`
- 构建：`scripts/build_phase2_free_stock_panel.py`
- 预榜：`scripts/run_phase2_free_real_experiment.py`
- 报告：`reports/phase2_free/free_real_data_validation.md`

`free_real` 使用 BaoStock 不复权 OHLCV 做撮合、前复权收盘价做信号、`tradestatus` 做停牌代理、`isST` 做 ST 过滤，并派生涨跌停价和 `circ_mv_approx`。

当前机器 macOS 系统 HTTP/HTTPS 代理为 `127.0.0.1:1082`。下载脚本现在默认启用 direct mode：清理代理环境变量、设置 `NO_PROXY=*`、禁用 Python proxy discovery，并设置 socket timeout；只有显式传 `--allow-proxy` 才允许代理路径。

已用 direct mode 完成 BaoStock 免费路线 505 只 raw+qfq+上市普通股匹配样本：raw 与 qfq 日线已落盘，`processed/phase2_free/stock_panel.parquet` 已生成 2,016,868 行、505 只股票，日期覆盖 `20100104` 到 `20260703`。完整 free-real 预榜已覆盖 42 个 S2/S3/S4 规格。当前仍是免费近似榜，不具备 strict-real 统计意义；后续若继续放大到全量股票，应使用低并发分片续跑。

已完成目标约束回测：505 股样本上覆盖 42 个 S2/S3/S4 规格、14,700 个 24 月滚动窗口，月入金 30,000，硬目标为 `W_12 >= 500000` 且 `W_24 >= 1200000`。Pre-cap baseline 与 5% BaoStock 日成交额 participation-cap stress 的当前最优都为 `S4_real_smallcap_factor_low_turnover_k10_weekly` / beginning，达标率 6.29%、24 月中位资产 940,474、p95 最大回撤 40.70%；5% stress 平均每窗约 33.97 次 participation blocked，S2/S3 family best 达标率仍为 0%。信号预榜不能替代目标约束回测，且 5% cap 只是日成交额近似压力，不是 strict-real 盘口成交证明。

已完成 `proxy_overlay_research` 衍生品压力层，并在 5% participation-cap base 上重跑：选取 6 个 base 策略/入金组合，覆盖 32 个股指期货整手 proxy 规格、72 个参数化期权 call-budget 规格、109,200 个 overlay-window。最高成功率为 9.14%（沪深300参数化 call budget），但仍低且中位资产未达 120 万；最佳期货整手 proxy 仍为 6.29%，平均每窗 10.21 次买不起/不能开够一手，说明整手期货不是小资金阶段的捷径。

`free_real` 准入：

- 允许：`S2_real_stock_momentum`, `S3_real_stock_breakout`, `S4_real_smallcap_factor`
- 禁止：`S5_real_limitup_board`, futures overlay, options overlay
- `proxy_research` 不允许进入 real leaderboard

## 先跑小样本

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

## 真实排行榜准入

- 没有真实 ETF 数据，不允许 ETF 策略进榜。
- 没有真实期货合约数据，不允许股指期货策略进榜。
- 没有真实期权合约数据，不允许期权策略进榜。
- 没有 `stk_limit` 和 `suspend_d`，不允许个股涨停/动量策略进榜。
- 没有 `stock_basic` 的退市样本和 `namechange` 的 ST 证据，不允许把结果称为真实全 A 回测。
- 必须报告数据完整性缺口，不允许静默降级为指数代理。

## 已完成的 Phase 2 代码入口

- `scripts/build_phase2_realdata.py`：从真实 raw 表生成 `processed/phase2/*.parquet`；缺表时退出并拒绝 fallback。
- `src/quant_proof/realdata/`：构造 `stock_panel`，保留 raw OHLC 执行价，`adj_close_for_signal` 仅用于信号。
- `src/quant_proof/engine/`：T+1、停牌、涨跌停、费用、印花税、成交金额上限等撮合规则骨架。
- `src/quant_proof/free_real_backtest.py`：free-real target layer 支持可选 BaoStock 日成交额 participation cap，并记录 participation clipped/blocked 指标；该约束不能升格为 strict-real 流动性证据。
- `src/quant_proof/real_strategies.py`：S2/S3/S4 真实个股策略规格与信号打分入口。
- `tests/`：覆盖 validation 缺表门禁、stock_panel 字段、月初/月末入金、12/24 月目标、回撤、T+1、停牌、涨跌停、participation cap 和费用规则。

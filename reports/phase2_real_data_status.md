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

已用 direct mode 完成 BaoStock 免费路线 100 只上市 A 股样本：raw 与 qfq 日线已落盘，`processed/phase2_free/stock_panel.parquet` 已生成 394,843 行、100 只股票，日期覆盖 `20100104` 到 `20260703`。完整 free-real 预榜已覆盖 42 个 S2/S3/S4 规格。当前仍是小样本近似榜，不具备全量统计意义；正式 free-real leaderboard 仍需扩大到 500、全量股票后再评估。BaoStock 高并发可能触发登录态失效，后续放大应使用低并发分片续跑。

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
- `src/quant_proof/real_strategies.py`：S2/S3/S4 真实个股策略规格与信号打分入口。
- `tests/`：覆盖 validation 缺表门禁、stock_panel 字段、月初/月末入金、12/24 月目标、回撤、T+1、停牌、涨跌停和费用规则。

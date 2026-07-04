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

# Phase 2 真实数据状态

## 已接入

- 配置：`config/phase2_real_data.yaml`
- 下载脚本：`scripts/download_phase2_real_data.py`
- 验证脚本：`scripts/validate_phase2_real_data.py`
- 任务说明：`docs/codex_phase2_real_data_task.md`
- 个股/期货/期权扩展方案：`docs/phase2_stock_derivatives_plan.md`
- 缺口报告：`/Volumes/PSSD1TB/量化数据/reports/phase2_real_data_validation.md`

## 当前状态

本机当前没有 `TUSHARE_TOKEN` 环境变量，因此还没有真实 Phase 2 数据落盘。验证报告显示以下表均缺失：

- 个股：`stock_basic`, `daily`, `adj_factor`, `daily_basic`, `stk_limit`, `suspend_d`, `namechange`
- ETF：`fund_basic`, `fund_daily`
- 期货：`fut_basic`, `fut_daily`
- 期权：`opt_basic`, `opt_daily`

因此当前不允许生成 Phase 2 真实排行榜；指数代理结果只能作为 Phase 1 基线。

## 先跑小样本

```bash
export TUSHARE_TOKEN="..."

uv run python scripts/download_phase2_real_data.py \
  --config config/phase2_real_data.yaml \
  --tables stock_basic,trade_cal,daily,adj_factor,daily_basic,stk_limit \
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
- 必须报告数据完整性缺口，不允许静默降级为指数代理。


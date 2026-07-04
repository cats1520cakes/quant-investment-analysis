# GPT Pro Handoff：量化投资分析实验工程

这个包用于给 GPT Pro 阅读当前工程状态。不要依赖 GitHub 私有仓库链接；模型通常无法访问你的私有 GitHub 会话。

## 已完成

- Phase 1 指数代理证明版。
- Phase 1 exhaustive 穷尽版。
- Phase 2 真实数据采集与验证脚本骨架。
- 个股、股指期货、股指/ETF 期权的 Phase 2 边界文档。

## 关键文件

- `README.md`
- `reports/phase1_exhaustive_experiment_report.md`
- `reports/phase2_real_data_status.md`
- `docs/codex_phase2_real_data_task.md`
- `docs/phase2_stock_derivatives_plan.md`
- `config/phase2_real_data.yaml`
- `scripts/download_phase2_real_data.py`
- `scripts/validate_phase2_real_data.py`

## 当前限制

- 当前包不含外置盘大结果 CSV。
- 当前机器没有 `TUSHARE_TOKEN`，所以 Phase 2 真实数据尚未下载。
- Phase 1 结果是 BaoStock 指数代理，不等价于真实 ETF、全 A 个股、真实期货或真实期权回测。

## 给 GPT Pro 的建议问题

请审查这个量化实验工程：

1. Phase 1 exhaustive 的结论是否足以说明指数/轮动/简单融资代理不足以稳定达成 `W_12 >= 500000` 和 `W_24 >= 1200000`？
2. Phase 2 真实数据采集表是否完整，是否遗漏了 A 股真实撮合必需表？
3. 个股、股指期货、ETF/股指期权进入真实排行榜的准入规则是否足够严格？
4. 对 `scripts/download_phase2_real_data.py` 和 `scripts/validate_phase2_real_data.py`，请找出接口风险、数据缺口和需要补充的字段校验。
5. 不要把指数代理结果解释成真实可交易策略；请重点检查 claim boundary。


# GPT Pro Handoff：量化投资分析实验工程

这个包用于给 GPT Pro 阅读当前工程状态。不要依赖 GitHub 私有仓库链接；模型通常无法访问你的私有 GitHub 会话。

## 已完成

- Phase 1 指数代理证明版。
- Phase 1 exhaustive 穷尽版。
- Phase 2 真实数据采集与字段级验证门禁。
- Phase 2 realdata 处理层：生成 `processed/phase2/stock_panel.parquet`。
- Phase 2 engine 撮合规则骨架：T+1、停牌、涨跌停、卖出印花税和成交金额上限。
- S2/S3/S4 真实个股策略规格与信号打分入口。
- Phase 2 Free Real 路线：BaoStock 主源、AKShare 补充、Qlib 作为 proxy research，严格区分 `strict_real/free_real/proxy_research`。
- 下载脚本默认启用 direct mode：清理代理环境变量、设置 `NO_PROXY=*`、禁用 Python proxy discovery，并设置 socket timeout；只有 `--allow-proxy` 才允许代理路径。
- 个股、股指期货、股指/ETF 期权的 Phase 2 边界文档。
- `reports/current_repo_healthcheck.md` 记录当前仓库可运行状态。

## 关键文件

- `README.md`
- `reports/phase1_exhaustive_experiment_report.md`
- `reports/phase2_real_data_status.md`
- `docs/codex_phase2_real_data_task.md`
- `docs/phase2_stock_derivatives_plan.md`
- `config/phase2_real_data.yaml`
- `config/phase2_free_real_data.yaml`
- `scripts/download_phase2_real_data.py`
- `scripts/validate_phase2_real_data.py`
- `scripts/build_phase2_realdata.py`
- `scripts/download_phase2_free_real_data.py`
- `scripts/validate_phase2_free_real_data.py`
- `scripts/build_phase2_free_stock_panel.py`
- `scripts/run_phase2_free_real_experiment.py`
- `src/quant_proof/realdata/`
- `src/quant_proof/free_sources/`
- `src/quant_proof/engine/`
- `src/quant_proof/real_strategies.py`
- `tests/`

## 当前限制

- 当前包不含外置盘大结果 CSV。
- 当前机器没有 `TUSHARE_TOKEN`，所以 Phase 2 真实数据尚未下载。
- Phase 1 结果是 BaoStock 指数代理，不等价于真实 ETF、全 A 个股、真实期货或真实期权回测。
- 当前真实 leaderboard 被 validation 阻断：缺 `trade_cal`, `stock_basic`, `daily`, `adj_factor`, `daily_basic`, `stk_limit`, `suspend_d`, `namechange`, `fut_*`, `opt_*` 等真实表。
- 参数化期权只能作为压力层，不能作为真实期权链 leaderboard。
- 当前机器可见 macOS 系统代理 `127.0.0.1:1082`，但下载脚本默认 direct mode 会绕过 Python 可见代理。
- 已完成 `600000.SH` 单票 BaoStock direct-mode smoke，生成 `processed/phase2_free/stock_panel.parquet` 4,005 行和 free-real smoke leaderboard；这只是管线验证，不是统计结论。

## 给 GPT Pro 的建议问题

请审查这个量化实验工程：

1. Phase 1 exhaustive 的结论是否足以说明指数/轮动/简单融资代理不足以稳定达成 `W_12 >= 500000` 和 `W_24 >= 1200000`？
2. Phase 2 真实数据采集表是否完整，是否遗漏了 A 股真实撮合必需表？
3. 个股、股指期货、ETF/股指期权进入真实排行榜的准入规则是否足够严格？
4. 对 `scripts/download_phase2_real_data.py` 和 `scripts/validate_phase2_real_data.py`，请找出接口风险、数据缺口和需要补充的字段校验。
5. 不要把指数代理结果解释成真实可交易策略；请重点检查 claim boundary。
6. 请重点审 `src/quant_proof/realdata/` 的 `stock_panel` 字段是否足以支撑 S2/S3/S4，并确认 `adj_close_for_signal` 没有被用于执行价。
7. 请审 `src/quant_proof/engine/` 是否还缺 A 股整百股、涨跌停部分成交概率、流动性成交额上限和退市处理。
8. 请审 `free_real` 中 `tradestatus/isST/derived limit/circ_mv_approx` 的声明是否足够清楚，是否还能进一步惩罚 derived/proxy 字段不确定性。

## 当前验证命令

```bash
uv sync --dev
uv run python -m compileall src scripts tests
uv run pytest -q
uv run python scripts/validate_phase2_real_data.py --config config/phase2_real_data.yaml
uv run python scripts/validate_phase2_free_real_data.py --config config/phase2_free_real_data.yaml
uv run python scripts/build_phase2_realdata.py --config config/phase2_real_data.yaml
```

最后一条在当前机器上应当失败并返回 exit 2，因为真实 Phase 2 raw 表还没有下载；这是预期的门禁行为。

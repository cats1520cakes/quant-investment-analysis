# 量化投资分析：附文第一阶段实验证明

这个工程把附文中的收益目标、入金约束、A 股交易成本和策略族，先落成一个可运行的第一阶段实验。

大体量行情数据不放在仓库里，默认放到外置硬盘：

```bash
/Volumes/PSSD1TB/量化数据
```

第一阶段优先下载 BaoStock 指数日线代理数据，验证：

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

这不是投资建议，也不证明某个策略可以实盘稳定达到目标。本轮指数代理结果只用于市场状态和操作族筛查，不等价于真实 ETF 或个股撮合。它的作用是把目标约束转成可复验的筛选实验，先找出哪些操作族在历史与重采样场景下更接近目标，以及哪些风险约束会让结果失效。

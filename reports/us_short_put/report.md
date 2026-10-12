# 美股 Sell Put + SGOV 比较研究

结果分类：**proxy_research**。真实历史期权回测尚未完成。

Historical spot and SGOV prices. Proxy options use prior 60-session RV times assumed IV multiplier; no actual IV history. SGOV ex-date cash timing, flat financing, SGOV haircut, stock margin and EOD liquidation are scenario assumptions. No early American assignment, intraday path, external deposits, tax advice or strategy significance claim.

初始资金均为 $100,000；CSP 保留全额执行价现金，其他正现金买 SGOV。相同名义上限组上限为 1× NAV；margin budget 组上限为 2× NAV 且 Put 初始保证金预算为 25% NAV。SGOV 保证金另计 25%，该参数未核实为账户实际值。

| 标的 | 策略 | 总收益 | CAGR | 最大回撤 | 权利金累计/$ | SGOV派息/$ | 借款利息/$ | 强制退出次数 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| SPY | cash_secured | 22.20% | 4.58% | 6.85% | 28766 | 5279 | 0 | 0 |
| SPY | sgov_same_notional | 37.46% | 7.36% | 6.85% | 30533 | 18426 | 590 | 0 |
| SPY | sgov_margin_budget | 40.99% | 7.97% | 9.47% | 48191 | 18150 | 748 | 1 |
| QQQ | cash_secured | 23.86% | 4.89% | 7.54% | 37795 | 4486 | 0 | 0 |
| QQQ | sgov_same_notional | 41.63% | 8.08% | 7.50% | 39125 | 18848 | 557 | 0 |
| QQQ | sgov_margin_budget | 68.27% | 12.32% | 14.33% | 73911 | 21168 | 701 | 1 |
| KWEB | cash_secured | 29.66% | 5.97% | 49.57% | 101480 | 2166 | 0 | 0 |
| KWEB | sgov_same_notional | 48.15% | 9.17% | 50.60% | 104551 | 18733 | 2587 | 0 |
| KWEB | sgov_margin_budget | 36.78% | 7.24% | 46.61% | 150309 | 14986 | 373 | 7 |
| ARKK | cash_secured | 16.92% | 3.55% | 40.39% | 82937 | 1829 | 0 | 0 |
| ARKK | sgov_same_notional | 16.13% | 3.39% | 40.97% | 83080 | 16137 | 1517 | 1 |
| ARKK | sgov_margin_budget | 1.85% | 0.41% | 53.40% | 133368 | 14604 | 160 | 11 |
| COIN | cash_secured | 78.12% | 13.76% | 53.44% | 226437 | 2938 | 0 | 0 |
| COIN | sgov_same_notional | 69.68% | 12.53% | 41.71% | 219861 | 20823 | 2267 | 1 |
| COIN | sgov_margin_budget | 122.00% | 19.49% | 64.79% | 380499 | 22059 | 363 | 8 |

## 保证金和资金效率

| 标的 | 策略 | 平均Put占用/$ | 平均总占用/$ | 峰值总占用/$ | 最大现金借款/$ | 毛权利金/平均Put占用/年 |
|---|---|---:|---:|---:|---:|---:|
| SPY | cash_secured | 77879 | 77879 | 119435 | 0 | 8.25% |
| SPY | sgov_same_notional | 14352 | 43776 | 88134 | 115837 | 47.50% |
| SPY | sgov_margin_budget | 22398 | 51942 | 116383 | 173743 | 48.04% |
| QQQ | cash_secured | 82671 | 82671 | 118571 | 0 | 10.21% |
| QQQ | sgov_same_notional | 13363 | 43069 | 80661 | 100027 | 65.37% |
| QQQ | sgov_margin_budget | 25312 | 58042 | 133476 | 200096 | 65.19% |
| KWEB | cash_secured | 92062 | 92062 | 127200 | 0 | 24.61% |
| KWEB | sgov_same_notional | 14564 | 44566 | 91792 | 125431 | 160.27% |
| KWEB | sgov_margin_budget | 21125 | 44780 | 120727 | 184422 | 158.85% |
| ARKK | cash_secured | 83479 | 83479 | 113360 | 0 | 22.18% |
| ARKK | sgov_same_notional | 12848 | 39080 | 88327 | 118504 | 144.36% |
| ARKK | sgov_margin_budget | 22012 | 44541 | 96375 | 133555 | 135.27% |
| COIN | cash_secured | 103282 | 103282 | 179828 | 0 | 48.95% |
| COIN | sgov_same_notional | 16268 | 49261 | 134610 | 183018 | 301.73% |
| COIN | sgov_margin_budget | 27656 | 61018 | 215049 | 132909 | 307.16% |

CSP占用表示执行价现金储备；margin总占用包含SGOV与已指派股票的情景保证金。效率列为毛收入比率，不是净收益率；不能据此比较投资回报。

## 极端下跌情景

归一化现价 $100，按期末60日RV × 1.2设基础波动，瞬时下跌后IV翻倍、剩余期限减少7天。SGOV价格不变，不赋予情景概率。

| 标的 | 策略 | 跌幅 | House倍数 | 压力NAV/$ | 要求/$ | 超额流动性/$ | 缺口 |
|---|---|---:|---:|---:|---:|---:|---|
| SPY | sgov_margin_budget | -35% | 2 | 53994 | 108066 | -54072 | True |
| SPY | sgov_margin_budget | -50% | 2 | 31944 | 121716 | -89772 | True |
| QQQ | sgov_margin_budget | -35% | 2 | 53262 | 111759 | -58497 | True |
| QQQ | sgov_margin_budget | -50% | 2 | 29637 | 126384 | -96747 | True |
| KWEB | sgov_margin_budget | -35% | 2 | 51306 | 119613 | -68307 | True |
| KWEB | sgov_margin_budget | -50% | 2 | 24531 | 136188 | -111657 | True |
| ARKK | sgov_margin_budget | -35% | 2 | 48582 | 131408 | -82826 | True |
| ARKK | sgov_margin_budget | -50% | 2 | 17943 | 150048 | -132105 | True |
| COIN | sgov_margin_budget | -35% | 2 | 49029 | 138398 | -89370 | True |
| COIN | sgov_margin_budget | -50% | 2 | 20382 | 153845 | -133463 | True |

这些数值描述历史价格路径与假设组合；不能据此认定某标的卖 Put 的真实长期收益或排序。累计权利金为毛收入，税前净收益已扣期权亏损、交易成本和借款利息，包含 SGOV 价格变动和最终未平仓市值。

总收益以账户净资产为分母，保证金效率另列；保证金较低并不减少每张合约的到期损失。现货被指派后按 5 个日历日持有、首个满足条件的交易日卖出；margin 组保留 SGOV，用现金借款支付执行价，按 6.5% APR 计息；CSP 使用预留现金支付。

## SGOV 数据交叉核验

| 年度 | 官方 NAV 总回报 | 下载数据价格+派息重投 | 差值/百分点 |
|---|---:|---:|---:|
| 2021 | 0.02% | 0.04% | 0.0230 |
| 2022 | 1.58% | 1.58% | -0.0002 |
| 2023 | 5.13% | 5.12% | -0.0088 |
| 2024 | 5.28% | 5.27% | -0.0052 |
| 2025 | 4.24% | 4.24% | 0.0002 |

完整参数、年度净资产、压力表、来源 URL 和 SHA256 见 `results.json`。逐日日志为本地 CSV，不提交带许可约束的原始行情。

## 复验

```bash
python scripts/download_us_short_put_data.py
python scripts/run_us_short_put_research.py
PYTHONPATH=src python -m unittest discover -s tests -p test_us_short_put.py
```

真实 NBBO 输入格式与限制见 `docs/us_short_put_research.md`。原始快照下载时间变化可能导致 SHA 变化；已有原始文件运行不重新下载可复验固定快照。

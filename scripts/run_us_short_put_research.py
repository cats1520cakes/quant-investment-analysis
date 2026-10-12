"""Run historical-spot proxy comparison or fail-closed normalized NBBO research."""
from __future__ import annotations
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from quant_proof.us_short_put import PutConfig, QuoteBook, load_market, simulate, stress_table, validate_quotes


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=ROOT/'config/us_short_put.json')
    p.add_argument('--data',type=Path,default=ROOT/'data/us_short_put')
    p.add_argument('--output',type=Path,default=ROOT/'reports/us_short_put')
    p.add_argument('--quotes',type=Path)
    p.add_argument('--quote-manifest',type=Path)
    args=p.parse_args(); c=json.loads(args.config.read_text()); args.output.mkdir(parents=True,exist_ok=True)
    if bool(args.quotes) != bool(args.quote_manifest): p.error('--quotes and --quote-manifest must be supplied together')
    manifest=json.loads((args.data/'manifest.json').read_text())
    provenance={r['symbol']:r for r in manifest}
    def verified_market(s):
        path=args.data/f'{s}.csv'
        if s not in provenance or hashlib.sha256(path.read_bytes()).hexdigest()!=provenance[s]['csv_sha256']:
            raise ValueError(f'market checksum mismatch: {s}')
        return load_market(path)
    sg=verified_market('SGOV'); summaries=[]; annual=[]; stress=[]
    q=None
    if args.quotes:
        q=validate_quotes(pd.read_csv(args.quotes),json.loads(args.quote_manifest.read_text()),args.quotes)
    for symbol in c['symbols']:
        if q is not None and symbol not in set(q.symbol):
            raise ValueError(f'no NBBO rows for {symbol}; do not silently drop requested symbols')
        market=verified_market(symbol).loc[c['start']:c['end']]
        if q is not None:
            # Keep 60-day warmup prior to the first quote; no invented quote history.
            first=q[q.symbol==symbol].date.min(); last=q[q.symbol==symbol].date.max()
            start_pos=market.index.searchsorted(first)
            if start_pos<59: raise ValueError('need 59 prior daily observations before quote window')
            market=market.iloc[start_pos-59:]; market=market.loc[:last]
        for iv in ([None] if q is not None else c['iv_multipliers']):
            for label,mode,cap in [('cash_secured','cash',1.),('sgov_same_notional','margin',1.),('sgov_margin_budget','margin',c['margin_notional_cap'])]:
                cfg=PutConfig(initial_equity=c['initial_equity'],mode=mode,notional_cap=cap,iv_multiplier=iv or 1.,
                    target_delta=c['target_delta'],dte=c['dte'],assignment_hold_days=c['assignment_hold_days'],
                    borrow_apr=c['borrow_apr'],sgov_margin_rate=c['sgov_margin_rate'],house_factor=c['house_factor'],
                    spread_fraction=c['spread_fraction'],withholding_rate=c['withholding_rate'],cash_apr=c['cash_apr'],
                    quote_age_minutes=c['quote_age_minutes'],margin_budget=c['margin_budget'] if label=='sgov_margin_budget' else 1.)
                book=QuoteBook(q[q.symbol==symbol]) if q is not None else None
                daily,s=simulate(market,sg,cfg,book)
                run=f'{symbol}_{label}_iv{iv if iv else "quoted"}'
                daily.to_csv(args.output/f'{run}_daily.csv')
                s.update(symbol=symbol,strategy=label,run=run); summaries.append(s)
                base=cfg.initial_equity
                for year, group in daily.groupby(daily.index.year):
                    annual.append(dict(run=run,year=int(year),return_=float(group.nav.iloc[-1]/base-1),end_nav=float(group.nav.iloc[-1]),partial_year=year==daily.index[0].year))
                    base=group.nav.iloc[-1]
                if iv == 1.2 or q is not None:
                    sigma=max(cfg.iv_floor, float(market.close.pct_change(fill_method=None).rolling(60).std().iloc[-1])*(252**.5)*cfg.iv_multiplier)
                    for row in stress_table(cfg,sigma=sigma).to_dict('records'):
                        row.update(symbol=symbol,strategy=label,base_sigma=sigma); stress.append(row)
    check=[]
    # Reinvestment-on-ex-date SGOV check: raw-price total return, never add dividends to adjusted close.
    growth=(sg.close+sg.dividend)/sg.close.shift(1)
    for year, expected in c['official_sgov_calendar_total_returns'].items():
        actual=float(growth[growth.index.year==int(year)].prod()-1)
        check.append(dict(year=int(year),official_nav_total_return=expected,yahoo_close_exdate_reinvest_return=actual,difference=actual-expected))
    result=dict(data_kind='quote_based_research' if q is not None else 'proxy_research',config=c,
                config_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest(),
                market_manifest=manifest,quote_manifest=json.loads(args.quote_manifest.read_text()) if q is not None else None,
                summaries=summaries,annual=annual,stress=stress,sgov_crosscheck=check,
                boundary='Historical spot and SGOV prices. Proxy options use prior 60-session RV times assumed IV multiplier; no actual IV history. SGOV ex-date cash timing, flat financing, SGOV haircut, stock margin and EOD liquidation are scenario assumptions. No early American assignment, intraday path, external deposits, tax advice or strategy significance claim.')
    (args.output/'results.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    baseline=[s for s in summaries if s['config']['iv_multiplier']==1.2 or q is not None]
    lines=['# 美股 Sell Put + SGOV 比较研究','',f'结果分类：**{result["data_kind"]}**。真实历史期权回测尚未完成。' if q is None else '结果分类：**quote_based_research**；受下列执行假设限制。','',
           result['boundary'],'','初始资金均为 $100,000；CSP 保留全额执行价现金，其他正现金买 SGOV。相同名义上限组上限为 1× NAV；margin budget 组上限为 2× NAV 且 Put 初始保证金预算为 25% NAV。SGOV 保证金另计 25%，该参数未核实为账户实际值。','',
           '| 标的 | 策略 | 总收益 | CAGR | 最大回撤 | 权利金累计/$ | SGOV派息/$ | 借款利息/$ | 强制退出次数 |',
           '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for s in baseline:
        cagr_text = f'{s["cagr"]:.2%}' if s['cagr'] is not None else '破产/未定义'
        lines.append(f'| {s["symbol"]} | {s["strategy"]} | {s["total_return"]:.2%} | {cagr_text} | {s["max_drawdown"]:.2%} | {s["premium"]:.0f} | {s["sgov_income"]:.0f} | {s["borrowing"]:.0f} | {s["liquidations"]} |')
    lines += ['', '## 保证金和资金效率', '', '| 标的 | 策略 | 平均Put占用/$ | 平均总占用/$ | 峰值总占用/$ | 最大现金借款/$ | 毛权利金/平均Put占用/年 |', '|---|---|---:|---:|---:|---:|---:|']
    for s in baseline:
        lines.append(f'| {s["symbol"]} | {s["strategy"]} | {s["mean_put_margin"]:.0f} | {s["mean_margin"]:.0f} | {s["peak_margin"]:.0f} | {s["peak_cash_debit"]:.0f} | {s["gross_premium_per_mean_put_margin_per_year"]:.2%} |')
    lines += ['', 'CSP占用表示执行价现金储备；margin总占用包含SGOV与已指派股票的情景保证金。效率列为毛收入比率，不是净收益率；不能据此比较投资回报。', '', '## 极端下跌情景', '', '归一化现价 $100，按期末60日RV × 1.2设基础波动，瞬时下跌后IV翻倍、剩余期限减少7天。SGOV价格不变，不赋予情景概率。', '', '| 标的 | 策略 | 跌幅 | House倍数 | 压力NAV/$ | 要求/$ | 超额流动性/$ | 缺口 |', '|---|---|---:|---:|---:|---:|---:|---|']
    for row in stress:
        if row['strategy']=='sgov_margin_budget' and row['shock'] in [-.35,-.50] and row['house_factor']==2.:
            lines.append(f'| {row["symbol"]} | {row["strategy"]} | {row["shock"]:.0%} | 2 | {row["nav"]:.0f} | {row["margin"]:.0f} | {row["excess"]:.0f} | {row["margin_breach"]} |')
    lines += ['','这些数值描述历史价格路径与假设组合；不能据此认定某标的卖 Put 的真实长期收益或排序。累计权利金为毛收入，税前净收益已扣期权亏损、交易成本和借款利息，包含 SGOV 价格变动和最终未平仓市值。','',
              '总收益以账户净资产为分母，保证金效率另列；保证金较低并不减少每张合约的到期损失。现货被指派后按 5 个日历日持有、首个满足条件的交易日卖出；margin 组保留 SGOV，用现金借款支付执行价，按 6.5% APR 计息；CSP 使用预留现金支付。','',
              '## SGOV 数据交叉核验','', '| 年度 | 官方 NAV 总回报 | 下载数据价格+派息重投 | 差值/百分点 |','|---|---:|---:|---:|']
    for x in check: lines.append(f'| {x["year"]} | {x["official_nav_total_return"]:.2%} | {x["yahoo_close_exdate_reinvest_return"]:.2%} | {100*x["difference"]:.4f} |')
    lines+=['','完整参数、年度净资产、压力表、来源 URL 和 SHA256 见 `results.json`。逐日日志为本地 CSV，不提交带许可约束的原始行情。','',
            '## 复验','', '```bash','python scripts/download_us_short_put_data.py','python scripts/run_us_short_put_research.py','PYTHONPATH=src python -m unittest discover -s tests -p test_us_short_put.py','```','',
            '真实 NBBO 输入格式与限制见 `docs/us_short_put_research.md`。原始快照下载时间变化可能导致 SHA 变化；已有原始文件运行不重新下载可复验固定快照。']
    (args.output/'report.md').write_text('\n'.join(lines)+'\n')
    print(f'{len(summaries)} runs; max accounting residual: {max(abs(s["pnl_reconciliation_residual"]) for s in summaries):.8f}')

if __name__=='__main__': main()

# Current Repository Healthcheck

Date: 2026-07-07 Asia/Shanghai

## Command Results

| Check | Command | Status | Notes |
| --- | --- | --- | --- |
| Dependency sync | `uv sync` | PASS | Resolved 59 packages; checked 41 packages. |
| Compile | `uv run python -m compileall src scripts tests` | PASS | Source, script, and test files compile. |
| Phase 1 smoke | `uv run python scripts/run_phase1_experiment.py --config config/phase1.yaml --max-strategies 3 --bootstrap-paths 0` | PASS | Ran 3 strategy specs and 1,050 rolling windows. Output leaderboard: `/Volumes/PSSD1TB/量化数据/reports/phase1_leaderboard.csv`. |
| Phase 2 validation | `uv run python scripts/validate_phase2_real_data.py --config config/phase2_real_data.yaml` | PASS, BLOCKING | Validator ran and wrote `/Volumes/PSSD1TB/量化数据/reports/phase2_real_data_validation.md`; all required Phase 2 real-data tables are currently missing or empty. |
| Tests | `uv run pytest -q` | PASS | 36 tests passed, covering strict/free validation, proxy guard, resumable free-real downloads, stock panels, target backtests, participation caps, overlay research, drawdown, and engine rules. |
| Phase 2 realdata build | `uv run python scripts/build_phase2_realdata.py --config config/phase2_real_data.yaml` | EXPECTED BLOCK | Exit 2 because real raw tables are missing; script refuses to build `stock_panel` from index/proxy data. |
| Phase 2 free validation | `uv run python scripts/validate_phase2_free_real_data.py --config config/phase2_free_real_data.yaml` | PASS, 505-STOCK PANEL READY | Free-real report is generated. Current matched listed-stock raw/qfq files: 505. |
| Download direct mode | `uv run python scripts/download_phase2_free_real_data.py --config config/phase2_free_real_data.yaml --max-codes 100` | PASS | Download ran in direct mode while macOS proxy was visible; script bypassed Python proxy discovery and wrote the free-real manifest. |
| Phase 2 free strategy pre-leaderboard | `uv run python scripts/run_phase2_free_real_experiment.py --config config/phase2_free_real_data.yaml` | PASS, FREE-REAL APPROX | Evaluated all 42 S2/S3/S4 free-real specs and wrote `reports/phase2_free/free_real_top_strategies.md`. |
| Phase 2 target backtest | `uv run python scripts/run_phase2_free_real_target_backtest.py --config config/phase2_free_real_data.yaml` | PASS, NON-PRIMARY | 42 S2/S3/S4 specs, 14,700 windows; best target success is 6.29%. |
| Phase 2 overlay research | `uv run python scripts/run_phase2_overlay_research.py --config config/phase2_free_real_data.yaml` | PASS, PROXY ONLY | 109,200 futures/options overlay-window rows; best parametric option success is 9.14%, and integer-lot futures did not improve success. |
| Phase 2 participation-cap stress | `uv run python scripts/run_phase2_free_real_target_backtest.py --config config/phase2_free_real_data.yaml --max-daily-amount-participation 0.05 --output-label participation_5pct` | PASS, NON-PRIMARY | 42 S2/S3/S4 specs, 14,700 windows; best target success remains 6.29%, with about 33.97 participation-blocked orders/window for the top row. |
| Phase 2 post-cap overlay research | `uv run python scripts/run_phase2_overlay_research.py --config config/phase2_free_real_data.yaml --base-output-label participation_5pct --max-daily-amount-participation 0.05 --output-label participation_5pct` | PASS, PROXY ONLY | 109,200 futures/options overlay-window rows on the 5% participation-cap base; best parametric option success remains 9.14%, and integer-lot futures still do not improve success. |

## Phase 1 Status

The current Phase 1 smoke run is runnable. It is only a small sample healthcheck, not the exhaustive Phase 1 result. Phase 1 remains an index-proxy experiment: it is useful for rejecting weak operation families, but it is not a real ETF, real stock, real futures, or real option execution proof.

## Phase 2 Validation Status

The Phase 2 validation script now checks more than table presence:

- manifest or raw-file discovery;
- table row counts and file counts;
- required fields by table;
- checksum verification when manifest rows exist;
- date coverage and sampled code counts;
- stock-code intersection across `daily`, `adj_factor`, `daily_basic`, `stk_limit`, and `suspend_d`;
- delisted stock, ST/namechange, suspension, and limit-price evidence;
- hard strategy gates for `S2_real_stock_momentum`, `S3_real_stock_breakout`, `S4_real_smallcap_factor`, futures integer-lot overlay, and option convexity budget.

## Phase 2 Free Real Status

The repository now has three data tiers:

- `strict_real`: paid/official-grade fields; remains blocked until official `stk_limit`, `suspend_d`, `daily_basic`, `adj_factor`, futures, and options data exist.
- `free_real`: BaoStock/AKShare-derived fields; admits S2/S3/S4 only after listed-stock raw and qfq files match.
- `proxy_research`: Qlib/index proxy only; cannot enter real leaderboards.

Current local machine has visible macOS system proxies at `127.0.0.1:1082`. Market-data download scripts now default to direct mode: they clear proxy environment variables, set `NO_PROXY=*`, disable Python proxy discovery, and use a socket timeout. Passing `--allow-proxy` is required to intentionally use visible proxy settings.

The existing external-drive BaoStock daily cache under `raw/baostock/daily_raw` still includes older index data (`sh.000001`, etc.), but the free-real builder filters `stock_basic.type == 1` and only accepts matched listed A-share stock raw/qfq rows. A direct-mode 505-stock free-real panel now exists at `/Volumes/PSSD1TB/量化数据/processed/phase2_free/stock_panel.parquet` with 2,016,868 rows, 505 stocks, and `20100104` to `20260703` coverage. The 505-stock target backtest covered 42 S2/S3/S4 specs and 14,700 rolling 24-month windows; the pre-cap and 5% BaoStock daily-amount participation-cap stress runs both top out at only 6.29% target success, so they remain insufficient as primary strategy proof. A separate post-cap `proxy_overlay_research` pass covered 109,200 futures/options overlay-window rows; the best parametric option success rate was 9.14%, while integer-lot futures did not improve success because early windows usually cannot open enough whole contracts. BaoStock multi-session bursts can invalidate login state, so larger runs should use low-concurrency `--start-index/--end-index` shards or `--codes-file` retries with resumable manifests.

Current missing table set:

`trade_cal`, `stock_basic`, `daily`, `adj_factor`, `daily_basic`, `stk_limit`, `suspend_d`, `namechange`, `fund_basic`, `fund_daily`, `fut_basic`, `fut_daily`, `opt_basic`, `opt_daily`.

## Why Real Leaderboard Cannot Run Yet

The real leaderboard is blocked because there is no validated Phase 2 real-data manifest and no raw Tushare tables under `/Volumes/PSSD1TB/量化数据/raw/tushare` for the required stock, futures, and option families.

Without those tables, the project cannot truthfully model:

- adjusted signal prices while preserving raw OHLC execution prices;
- suspended-stock no-trade days;
- limit-up buy failures and limit-down sell failures;
- ST filters, listing-age filters, and delisted samples;
- futures integer-lot margin and mark-to-market overlay;
- real option-chain contract selection and premium budget losses.

Therefore no Phase 2 real leaderboard should be produced yet. The validator must remain blocking rather than falling back to Phase 1 index proxies.

## Next Data Checklist

Small sample:

```bash
export TUSHARE_TOKEN="..."
uv run python scripts/download_phase2_real_data.py \
  --config config/phase2_real_data.yaml \
  --tables stock_basic,trade_cal,daily,adj_factor,daily_basic,stk_limit,suspend_d,namechange \
  --max-codes 10 \
  --max-dates 30
uv run python scripts/validate_phase2_real_data.py --config config/phase2_real_data.yaml
```

Full stock-line data before S2/S3/S4 leaderboard:

- `trade_cal`
- `stock_basic` including listed, delisted, and paused statuses
- `daily`
- `adj_factor`
- `daily_basic`
- `stk_limit`
- `suspend_d`
- `namechange`

Derivatives data before overlays:

- futures: `fut_basic`, `fut_daily`
- options: `opt_basic`, `opt_daily`

## Next Script Checklist

- Build `/Volumes/PSSD1TB/量化数据/processed/phase2/stock_panel.parquet` from validated raw tables with `scripts/build_phase2_realdata.py`.
- Run S2/S3/S4 only on real stock panel data through `src/quant_proof/real_strategies.py`.
- Keep execution prices on raw OHLC; use adjusted close only for signals.
- Enforce T+1, suspended no-trade, limit-up/limit-down fill rules, turnover caps, commission, stamp tax, transfer fee, and slippage.
- Emit `reports/phase2/real_stock_windows.csv`, `reports/phase2/real_stock_leaderboard.csv`, trade logs, equity curves, drawdown curves, and stress summaries only after the real-data gate passes.

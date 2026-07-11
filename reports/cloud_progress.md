# Cloud progress

- Base commit: `53eb97461f88cebff345009d6f25ab7d99d28004`.
- Branch: `cloud/quant-exhaustive-work`.
- Resources: 9 CPU threads, 55 GB initially free, no visible GPU.
- Baseline: 439 passed, 4 environment-link failures, 1 skipped; the four failures share a malformed runtime-created virtualenv symlink.
- SSE U4 complete: 14,214 canonical rows through 2026-07-10, 3 confirmed suspension rows, 0 undeclared gaps. Panel SHA-256: `2c5c31d886219807e10cf2c7126a77d699996ee8a999036970b11d2aeda4a9c8`.
- ETF development screen complete: 77 strategies, 36,036 rolling-window evaluations, two deposit timings. Best worst-timing dual-target rate is 2.564%; strict candidates: 0.
- Tencent U6 pagination implemented. Source throttling interrupted completion after three cached layers; retry is resumable and no partial panel was promoted.
- Derivative audit: prior-day volume is enforced for capacity and OI is available for contract selection, but OI/volume crowding is not yet a direction-rule family. Long-option execution uses real daily open plus prior-day volume; no quotes means the evidence tier remains daily execution without bid/ask.
- Cloud CFFEX reconstruction checkpoint: 29/195 monthly archives are atomically cached and validated (2010-04 through 2012-08). Four-way download triggered exchange-route timeouts, so the resumable default is deliberately one worker.
- Exact resume: `PYTHONPATH=src uv run python scripts/download_phase3_cffex_cloud.py --data-root artifacts/runtime_data --workers 1`.
- Tencent resume: `PYTHONPATH=src uv run python scripts/download_phase3_etf_tencent_data.py --data-root artifacts/runtime_data --timeout 20` (3/12 layers currently cached; no partial panel promoted).
- 2026-07-11 continuation: CFFEX remains 29/195 after a bounded retry of 2012-09 returned zero bytes. The attempt is recorded in the runtime attempt ledger; no incomplete panel is promoted.
- Causal crowding implementation now covers lagged total-OI change, volume/OI and cross-contract OI concentration with expanding thresholds shifted by one observation. A partial 29-month IF smoke run resolved 12 gates over 580 dates (6,960 gate-date rows); IH/IC/IM are correctly marked absent in this early horizon. This is implementation evidence only; strict candidates remain 0.
- Tencent now records per-layer attempts and stops after two consecutive source failures. Latest bounded run: 3/12 valid, 2 timed-out attempts, no layered panel promoted.
- Latest bounded CFFEX run: 29/54 valid in requested scope; 2012-09 through 2012-11 timed out and triggered the three-failure circuit breaker. Full target remains 29/195.
- Crowding-to-overlay integration is wired but fail-closed on the partial panel: 24 IF base/gate maps were rejected because expiries at the truncated 2012-08 horizon are right-censored. This prevents partial-history contract boundaries from becoming false roll/expiry evidence.
- Sparse official recovery checkpoint: 25 new valid months were added without touching raw Git history: 2020-01..12, 2022-01..07 and 2026-01..06. Total valid official archives are now 54/195; source bytes in the runtime ledger are 6,362,526. Segment panels contain 66,992, 39,216 and 83,508 rows respectively.
- The cloud downloader now resumes partial HTTP transfers with Range requests and records URL, HTTP/partial bytes, SHA-256 and attempts. Scoped runs are forced into scoped panel/master names; regression tests prevent a segment from overwriting canonical artifacts.
- Official expiry evidence for the 2026H1 segment is complete: 45/45 required snapshots, 1,694 exact contracts, 31,803 history rows, complete master coverage. The segment metadata is canonical relative to its manifest-bound six-month panel; it is not a claim of full-history completeness.
- With exact last-trading-day history, all 96 combinations of 2 base direction rules × 48 causal crowding gates resolved with 0 integration errors. The six-month horizon cannot produce W12/W24 evidence, so strict candidates remain 0.
- Small-capital executable screen on 116 dates: with 12% margin and 15% cash buffer, IF/IC/IM are infeasible on 100% of 2026H1 dates under both deposit timings; IH is infeasible on 60.34% (beginning) and 81.90% (ending). This uses the official 2026-06-23 margin snapshot, not a complete effective-date schedule.
- Fixed-premium long-option lower bound is also mostly infeasible: at 0.5% NAV, IO/HO/MO infeasible shares are 96.55%/93.10%/98.28% for beginning deposits and 98.28%/97.41%/98.28% for ending deposits. DTE and exact target-delta constraints can only make these rates worse.
- Sparse recovery continued into 2023: 10/12 months validated; 2023-07 and 2023-12 remain in the gap ledger. Total official monthly archives are 64/195, 10,377,039 bytes. No incomplete 2023 segment panel was promoted.
- 2023-07 and 2023-12 were subsequently recovered, completing the 2023 12/12 segment. 2024-01..05 raw archives also validated, taking raw coverage to 71/195.
- The 2024 five-month panel failed post-write Parquet footer validation. It was not promoted; a new immediate post-write Parquet/row-count gate now fails before manifest publication. Strict candidates remain 0.
# 2026-07-11 — 2024 Parquet publication gate repaired

- Root cause reproduced: the former multi-row-group `ParquetWriter.close()` path returned normally but left the 2024 temporary artifact without the terminal `PAR1` footer. Publication previously renamed before durable validation.
- Replacement path writes one schema-unified Arrow table through an explicitly closed `OSFile`, fsyncs the same-directory temporary file, validates header/footer/schema/73,846 rows/2024-01-02..2024-05-31/hash, atomically renames, fsyncs the directory, then reopens and validates again before manifest publication.
- Fault injection: truncated footer, unclosed partial writer, cross-device rename, and stale output all fail closed; focused adapter tests 8/8 passed.
- Cached official 2024-01..05 archives were reused without download. Valid panel hash: `4e39e2d17ba443d2b7d9911de324e0379941ffb60a31ee9504c7d6124cfa298a`.
- Strategy promotion: none. Strict candidates: **0**.

## 2024 continuation after repair

- Newly acquired and revalidated: 2024-06, 08, 09, 10, 11 (5 months). 2024-07 initially reported downloaded but failed the subsequent full-year content revalidation and is now a declared gap; 2024-12 remained incomplete after three bounded resume attempts.
- Current publishable 2024 coverage: 10/12 months. Missing/rejected: 2024-07 and 2024-12. No annual segment manifest was published.
- This checkpoint deliberately counts only revalidated archives, not download-success messages. Continuous 24-month evidence remains unavailable; strict candidates: **0**.

## 2024 full-year segment completed

- 2024-07 and 2024-12 were recovered with bounded 60-second single-worker resumes; all 12/12 archives then passed a fresh cache-content validation pass.
- Durable official panel: 189,422 rows, 3,032-contract master, 2024-01-02..2024-12-31, all seven IF/IH/IC/IM/IO/HO/MO products. Panel hash `b0eb01683556edcf193560d505e03d26fb8a4dfcfd690263e369ad6dbe4bd45e`.
- This is one complete year, not a 24-month evaluation interval. W12/W24 promotion remains blocked pending adjacent continuous official coverage and historical effective-date trade parameters. Strict candidates: **0**.

## 2025 first pass and H2 checkpoint

- First pass recovered 10/12 official archives; gaps are 2025-01 and 2025-03. Failures did not stop later months.
- 2025H2 passed a second complete cache validation and durable publication: 6/6 months, 89,822 rows, 1,636 master rows, 2025-07-01..2025-12-31, seven products.
- Panel hash: `bd68f5e8107a541a66bbf831eb50fc33b94b7f652f1e2a6719e85ae03507f746`; master hash: `6f4d7a818914adafb9c0577658a993d1ce290d9739658b3caa56d2e375322f39`.
- The generated master labels expiry as `last_official_daily_record`; this is explicitly **not accepted** as official expiry evidence. Derivative strategy evaluation remains blocked until official as-of history is bound.
- Strict candidates: **0**.

## 2025 full year and continuous 24-month panel

- Recovered 2025-01 and 2025-03 separately, then revalidated all 12/12 archives before publication.
- 2025 panel: 176,230 rows, 2,630 master rows, 2025-01-02..2025-12-31; panel hash `1e6bb9f74d0302e75ec99988753ad7f8accffaad2f2676224f94fd583394db0f`.
- Combined 2024+2025 panel: 24/24 months, 365,652 rows, 4,910 master rows, 2024-01-02..2025-12-31; panel hash `7a119d96a5a456f2b5635720263bbb22d3b7b633f667a54370b9deaf105c380b`.
- Raw archive lineage is bound by source-set hash `9c00d2abf8112e73cee866653b30481f5617b584d97f717a3c34eac2155b1796`.
- Data continuity gate passed. Official effective-date expiry/margin/limit history gate remains blocked; no derivative backtest is released using panel-last-record expiry. Strict candidates: **0**.

## Official trade-parameter calendar recovery

- Added an unbound calendar-crawl mode for recovery after runtime-cache loss. It never promotes metadata: valid official snapshots must later be reconciled to the frozen panel calendar and contract master.
- Every attempt is atomically persisted with URL, date, HTTP status where available, bytes, SHA-256, row/contract counts, failure text, and evidence tier.
- Smoke scope 2024-01-01..10: 7 official valid snapshots and 3 unavailable holiday/weekend dates; valid snapshots contain 672–684 contracts.
- Tests: 24/24 passed across the trade-parameter adapter and new calendar/atomic-ledger regressions. Metadata and strategy gates remain blocked. Strict candidates: **0**.

- Full-year crawl was started and durably checkpointed through 2024-01-18: 18 natural dates, 13 valid official snapshots (7 cache-validated, 6 newly downloaded), 5 unavailable dates. Raw snapshots remain runtime-only.

## 2024Q1 official parameter coverage

- Calendar crawl completed through 2024-03-31: 91 natural dates, 58 valid official snapshots, 33 unavailable weekend/holiday dates. Q1 coverage matrix has 406 date-product rows across all seven products.
- Official last-trading-day, listing date, contract month, limit prices and position-limit raw fields have 100% within-snapshot coverage. Futures limit percentages are complete; IO/HO/MO percentage cells are absent although official limit prices are present, so percentage-based execution remains blocked for options.
- This endpoint does not supply historical initial/maintenance margin, multiplier or minimum tick. Those four official rule fields remain 0% and require versioned rule sources with effective dates.
- Panel/calendar binding is unavailable after runtime pruning, so Q1 remains evidence collection rather than an execution master. Strict candidates: **0**.

## 2024Q2 batch 1 parameter recovery

- 2024-04-01..05-15 completed: 45 natural dates, 28 valid official snapshots, 17 unavailable weekend/holiday dates. Raw snapshots remain runtime-only.
- This is a resumable acquisition checkpoint, not a complete-quarter coverage claim. Strict candidates: **0**.

## 2024Q2 official parameter coverage

- Q2 completed: 91 natural dates, 59 valid official snapshots and 32 unavailable weekend/holiday dates. Coverage matrix has 413 date-product rows across all seven products; source-set SHA256 is `d82f40768bb182cd6773ed9dfd8b785403e378dc64e46a4898c9769f3b5b9c34`.
- Snapshot fields retain the Q1 pattern. Historical exchange margin, multiplier and minimum-tick rule versions remain the minimum补证 set; no synthetic maintenance margin is introduced. Strict candidates: **0**.

## 2024Q3 batch 1

- 2024-07-01..08-15 completed: 46 natural dates, 33 valid official snapshots and 13 unavailable dates. Raw remains runtime-only; quarter coverage is not yet promoted. Strict candidates: **0**.

## 2024Q3 complete and ETF execution gate

- Q3 completed: 92 natural dates, 63 valid official snapshots, 29 unavailable dates; 441 date-product coverage rows; source-set SHA256 `b330a320a3176c26537ead8432432ef0a025addf74a86d9f00e9fb6aee47c8a2`.
- Added strict ETF execution module: 100-share board lots, explicit beginning/ending deposit ordering, raw-open fills with hfq signal-only values, suspension/missing-price rejection, and incomplete company-action-ledger rejection.
- Focused parameter plus ETF tests: 30/30 passed. No old ETF screen rerun. Strict candidates: **0**.

## 2024Q4 batch 1

- 2024-10-01..11-15 completed: 46 natural dates, 29 valid official snapshots, 17 unavailable dates. Strict candidates: **0**.

## 2024Q4 complete

- Q4 completed: 92 natural dates, 59 valid official snapshots and 33 unavailable dates; 413 date-product rows; source-set SHA256 `cae3872061c788f1861fe5253e99e4cb5adddeefa4056b050aaac65efd617ee4`.
- Runtime audit found 0/24 CFFEX monthly archives and no 24-month panel after workspace pruning. Frozen lineage remains valid but local execution rebinding is blocked until exact restoration reproduces panel SHA256 `7a119d96a5a456f2b5635720263bbb22d3b7b633f667a54370b9deaf105c380b`. Strict candidates: **0**.

## 2025Q1 parameter coverage and CFFEX pause

- Q1 completed: 90 natural dates, 55 valid official snapshots, 35 unavailable dates; 385 date-product rows; source-set SHA256 `5c53941e422e419aa7331d6a7240bea6707e08221f19cc4a6089436da2c7ca80`.
- Further CFFEX crawl is paused. Futures remain fail-closed; work shifts to the independent ETF-only strict path. Strict candidates: **0**.

## ETF U4 official recovery

- Rebuilt SSE U4 canonical data from official endpoints: 14,214 rows, four codes, three confirmed suspension rows, panel SHA256 `f21ff743900607819436fc3897d1af6ac152e8993649241c79139ed26b6cb3b2`.
- Source units remain fund shares and CNY. Tencent raw/hfq cross-check is complete for U4 only; U2 was not promoted after one hfq failure.
- Corporate-action announcement coverage for 2024-2025 is not yet proven complete, so strict ETF evaluation remains fail-closed. Strict candidates: **0**.

## 2026-07-12 — long-history ETF sample gate and U3 B evaluation

- U4 remains time-evidence-pending: 4 complete non-overlapping W24 blocks through 2025 versus 5 required.
- U3 was selected without return information by removing only later-inception 512100. Common inception is 2013-03-15; first post-warmup signal is 2013-09-11; 6 W24 blocks are available.
- Historical official event evidence closed for the SSE index chain: 710 announcements, 42 candidate bodies, 22 stable account events, unresolved candidates 0.
- Frozen B rotation completed 72 specifications over 124 monthly cohorts and both deposit timings. Dual-target passes 0; strict candidates **0**. Best worst W12/W24 were 321,724/646,617 (different specifications).

## 2026-07-12 — U3 A/C/D and unified comparison

- A: 48 specs, best worst W12/W24 346,209/685,525; base passes 0.
- C: 36 specs, best worst W12/W24 354,414/709,790; base passes 0.
- D: 18 specs, best worst W12/W24 318,332/647,455; base passes 0.
- Together with B, 174/174 frozen U3 specifications are complete. Every specification has 124 cohorts and 6 non-overlap W24 blocks; strict candidates **0**.
- No stress promotion was triggered because no base specification passed the dual target. Next work should expand executable economic convexity rather than retune this failed grid.

## 2026-07-12 — high-elasticity ETF universe preregistration

- E1={159915}, E2={159915,510500}, and E3={159915,510500,510300} were selected by inception, index mandate and liquidity rules without return screening. Each has a conservative lower bound of 6 non-overlapping W24 blocks through 2025.
- Five new, non-U3-equivalent families were frozen before results: 204 total specifications. Maximum ETF exposure is 100%; borrowing and synthetic leverage are prohibited.
- SZSE's official listing notice is accessible and proves identity/listing, but it does not prove full-history OHLCV or announcement completeness. An attempted announcement API query returned HTTP 500; no official structured historical chain has yet been verified.
- Tencent raw/hfq each contain 3,539 rows from 2011-12-09 to 2026-07-10, but remain vendor cross-check only. Strict E1/E2/E3 evaluation is fail-closed. Strict candidates: **0**.

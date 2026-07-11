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

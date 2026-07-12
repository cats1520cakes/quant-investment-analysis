# Cloud elimination ledger

## Complete futures overlay lineage blocker — 2026-07-12

- Exact official raw source-set recovered, but the frozen Parquet byte SHA could not be reproduced and neither a frozen file copy nor frozen canonical content hash is accessible.
- A newly computed canonical hash cannot be compared retrospectively and is not accepted as a replacement gate. Complete IF/IC/IM and later U3 futures-overlay families are blocked before strategy execution.
- This is a data-lineage blocker, not economic failure evidence. Strict candidates: **0**.

Existing development-exposed negative evidence is retained: Phase 1 simple index/rotation/leverage, Phase 2 free-real stock families, parameterized option overlays, and single-family dynamic whole-contract futures do not pass the strict dual-target stability gates.

## ETF U4 development screen — 2026-07-11

- 77 specifications and 36,036 rolling-window evaluations with beginning/ending deposits.
- Best worst-deposit-timing dual-target rate: 2.564% (`S1_mom_lb60_top2_monthly_none`).
- Median W24: 727,124; p5 W24: 591,622; worst W24: 551,229; worst drawdown: 39.92%.
- Eliminated from strict promotion: numerical rate is far below 50%, and the company-action ledger plus whole-100-share accounting are incomplete.
- Strict candidates: **0**.

## ETF U3 official-history B rotation — 2026-07-12

- Ex-ante U3 reaches 6 non-overlapping W24 blocks after a 120-trading-day warmup; the unchanged five-block sample gate is therefore evaluable.
- Official SSE coverage: 710 index records across 9 terminally paged pages; all 42 candidates were read and reconciled into 20 cash dividends, 2 share-factor events, and 20 no-direct-account-impact/duplicate notices.
- Frozen B grid: 72/72 specifications, 124 monthly cohorts, two deposit timings. Target passes: 0; strict candidates: **0**.
- Best worst-timing W12: 321,724 (`U3-B064`). Best worst-timing W24: 646,617 (`U3-B005`). Best p5 W24: 666,976 (`U3-B040`). Six-block dual-target passes: 0 for every specification.
- Maximum observed cohort dual-target rate was 0.806%; worst-case unexecutable-order rates ranged from 27.08% to 65.00%. The family is eliminated economically under the frozen rules; no stress test can rescue the failed base target gate.

## ETF U3 frozen A/B/C/D completion — 2026-07-12

- All 174 preregistered specifications completed with 124 monthly cohorts, both deposit timings, and 6 non-overlapping W24 blocks per specification.
- Base dual-target passes: 0/174. Non-overlap dual-target block passes: 0 for every specification. Strict candidates: **0**.
- Best family worst W12/W24: A 346,209/685,525; B 321,724/646,617; C 354,414/709,790; D 318,332/647,455. These are far below 500,000/1,200,000.
- A/C/D asset identities passed and negative-cash events were zero. Cause-decomposed execution evidence is retained in their registries; high unexecutability is never treated as a pass.
- This is auditable elimination of the frozen, survivor-biased U3 unlevered A/B/C/D space, not a proof covering every possible ETF-only operation family.

## U3 elimination boundary frozen — 2026-07-12

- A best worst/p5 W24: 685,525/694,449; B: 646,617/666,976; C: 709,790/713,216; D: 647,455/669,338.
- Every family has zero specifications passing all 6 non-overlapping dual-target blocks. This eliminates only the frozen `U3 broad-index, unlevered A/B/C/D` space; it is not extrapolated to high-elasticity ETFs or all ETF strategies.

## High-elasticity ETF free-real approximation — 2026-07-12

- All 204 frozen E1/E2/E3 specifications completed in a separately labelled `free_real_approx` ranking. No result is eligible for strict promotion.
- Best worst W12/W24 by family: single-159915 332,837/652,281; E2 relative/absolute 330,346/631,291; E3 rotation 332,852/659,483; volatility/drawdown 346,865/694,031; multiscale breakout 326,387/644,571.
- Base dual-target passes: 0/204. All six-block dual-target pass counts are zero. This is negative economic evidence for the frozen high-elasticity, unlevered ETF approximation—not strict-real proof.
- The family is far below 500,000/1,200,000 and therefore does not trigger stress promotion. Strict candidates: **0**.

## ETF U4 formal sample-size blocker — 2026-07-12

- U4 common inception is 2016-11-04; after the preregistered 120-trading-day warmup, the first signal date is 2017-05-04.
- Through 2025-12-31 this provides only 4 complete non-overlapping W24 blocks versus the unchanged formal requirement of 5.
- The fifth block cannot mature before 2027-04. U4 is retained as `time_evidence_pending`, not classified as an economic strategy failure and not promoted.
- Strict candidates: **0**.

## CFFEX long-option approximation O1/O2 — 2026-07-12

- O1 completed 180/180 specifications (360 deposit-timing paths); O2 completed 360/360 (720 paths).
- Both families have zero dual-target passes. Worst-timing W12/W24 are 390,000/720,000, reflecting deposits only.
- Every attempted purchase was infeasible: the cheapest eligible real contract premium exceeded the frozen 0.5%–5% NAV hard budget. No infeasible date was skipped or converted to a fractional contract.
- Evidence tier is `free_real_approx_daily_open_no_quotes`; the one-W24-block sample gate and missing bid/ask independently prohibit strict promotion. Strict candidates: **0**.

## U3 equal-weight × IM, schema v4 — 2026-07-12

- All 108 frozen specifications completed under two deposit timings; 216/216 compressed daily shared-cash ledgers were written with SHA-256, 485 rows each.
- Asset-identity failures: 0. Economic dual-target passes: 0. Best worst-timing W12/W24: 459,980 / 972,748; worst max drawdown: -37.11%.
- Margin calls/forced liquidations total 200/200. Rejections: NAV-multiple gate 19,044; free-cash gate 5,116; limit-price 216. Infeasible dates remain failures, not cash-strategy successes.
- Evidence tier remains `free_real_approx_conservative_margin`; the five non-overlapping W24-block gate and point-in-time official margin gate remain closed. Strict candidates: **0**.

## Frozen low-turnover trend/cash overlay baseline — definition gate — 2026-07-12

- The preregistration contains the family label and 108×4 parameter maps, but no frozen operational definition for its ETF signal, cash state, rebalance rule or holdings.
- The existing generic overlay runner hard-codes 510300 and the v3 runner implements U3 equal-weight only. Neither may be relabelled as this baseline.
- The four low-turnover families are blocked pending an explicit frozen baseline definition; no result is imputed and strict candidates remain **0**.

## U3 low-turnover trend/cash v1 × futures overlays — 2026-07-12

- The replacement family was frozen before results with operation/grid SHA `6413e808243ca42ce26bf460beffb4c83d002356119b5dc929e26803772933df`; the obsolete label remains `definition_missing`.
- All IH/IF/IC/IM families completed 108/108 specifications and 216/216 SHA-bound daily ledgers each. Asset-identity failures and base dual-target passes are both zero for every product.
- Best worst-timing W12/W24: IH 429,024/898,933; IF 555,163/1,007,194; IC 594,413/1,054,314; IM 654,280/1,128,087. The cash state improves both horizons versus U3 equal weight for every product, increasingly from IH to IM, but no product reaches W24 1,200,000.
- The improvement is economic approximation evidence only. Five non-overlapping W24 blocks and point-in-time official margin remain closed; strict candidates: **0**.

## Multi-asset monthly trend/risk-budget v1 — preregistered data blocker — 2026-07-12

- A non-repeating ETF-led family spanning broad equity, growth, dividend, gold and government bonds is preregistered without viewing results.
- Execution is blocked until official canonical history, company actions and common five-block coverage close for 159915/510880/518880/511010. No proxy or Tencent data may promote it.
- Strategy runs permitted: 0. Strict candidates: **0**.

## SSE four-asset trend/risk-budget v1 × IH — 2026-07-13

- The independent data-availability family was frozen before results by mechanically removing only 159915 from five-asset v2. Grid SHA is `c80e5de617357f86f1915827a84f41b4505afa01b9f8634ac13dec3497e322cc`; 108/108 IH specifications and 216/216 timing ledgers completed.
- Best worst-timing W12/W24 is 385,612/810,311 (S4ARB-0024), so dual-target passes are 0. All 108 specifications traded futures; first feasible month was 2024-06. Asset-identity failures are 0.
- For the best specification: maximum drawdown -13.72%, futures PnL 20,607, fees 1,220, peak/mean margin 180,864/57,256, peak margin/NAV 50.28%, 17 rolls, one margin call and one forced liquidation. Across paths, rejection counts are free cash 6,432, NAV threshold 10,588, and limit price 216.
- Official ETF common history supports six non-overlapping W24 blocks after the 120-day warmup, but the bound derivative execution panel covers only 2024-2025. Consequently strategy-level six-block results are unavailable and the sample gate fails; this is not imputed from ETF-only history.
- ETF-versus-futures PnL decomposition is incomplete in the v1 IH output (futures PnL is explicit; ETF contribution is not transaction-attributed), so the reporting gate also fails. Point-in-time official daily margin remains missing. Strict candidates: **0**.

## SSE four-asset trend/risk-budget v1 × IF — 2026-07-13

- After workspace-prune recovery, all frozen inputs were restored before rerun: CFFEX panel SHA `7a119d96...c380b`, SSE panel SHA `c3e58154...370ac`, 192/192 bound official parameter snapshots, and six execution-period official SSE dividend events. The unrecoverable prior attempt rows were discarded and 108/108 specifications were recomputed from zero.
- Best worst-timing W12/W24 is 394,877/859,394 (S4ARB-0136); dual-target passes are 0. All 108 specifications traded futures, first feasible month was 2024-10, and asset-identity failures are 0.
- For the best specification, beginning-timing W24 is 859,394 with futures PnL 34,092 and ETF/shared-fee residual 105,302; ending-timing W24 is 889,303 with futures PnL 135,282 and residual 64,021. Reported fees are 1,374/1,263. Worst maximum drawdown is -7.43%; peak margin is 279,300, peak margin/NAV 43.03%, with one margin call/forced liquidation in the beginning path.
- Aggregate rejection counts are free cash 5,128, NAV threshold 19,264, and limit price 216. Risk-budget weights follow the frozen causal 60-day inverse-volatility water-fill with 40% cap and cash residual; no result-dependent weight changes occurred.
- Official ETF history has six W24 blocks, but the bound derivative execution panel has one. Strategy-level sample gate and point-in-time official daily margin gate fail independently. Strict candidates: **0**.

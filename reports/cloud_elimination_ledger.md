# Cloud elimination ledger

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

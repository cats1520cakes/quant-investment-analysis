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

## ETF U4 formal sample-size blocker — 2026-07-12

- U4 common inception is 2016-11-04; after the preregistered 120-trading-day warmup, the first signal date is 2017-05-04.
- Through 2025-12-31 this provides only 4 complete non-overlapping W24 blocks versus the unchanged formal requirement of 5.
- The fifth block cannot mature before 2027-04. U4 is retained as `time_evidence_pending`, not classified as an economic strategy failure and not promoted.
- Strict candidates: **0**.

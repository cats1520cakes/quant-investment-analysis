# Remote checkpoint audit: 2c9dd25a84e1416b07e14c438b083261fcbb89a0

- Audit source is the detached remote commit only; later local or partially uploaded work is excluded.
- Frozen grid SHA is `c80e5de617357f86f1915827a84f41b4505afa01b9f8634ac13dec3497e322cc`; schema is v4.
- IH: aggregate/coverage/attempt metadata says 108/108 and dual-target passes 0, but atomic parts and daily ledgers are absent at this commit. Existing economic elimination is retained; atomic artifact coverage is incomplete and IH is not rerun.
- IF: 108/108 atomic parts, 216/216 daily ledgers, and 108 attempt entries exist. Every part has both deposit timings; every stored ledger SHA and row count matches its part; asset-identity residual failures are zero. The synchronized `results.csv` initially had a race-induced SHA mismatch versus coverage; deterministic reconstruction from the 108 parts restored the declared results SHA `92760669265cd1bddd4544145aa630a312c2ae1132fb6325ae5f0cda8db7af15`. Economic dual-target passes remain 0.
- IC: 0/108 at the audited commit. IM: 0/108 at the audited commit. Both must run from their first specification.
- 159915: remote nonraw attempt ledger contains 350 validated dates. No raw is committed by design. A recovered runtime cache contains 336 matching official responses with zero SHA mismatches against those 350 entries; remaining registered and new dates must be fetched/validated.
- Frozen CFFEX panel SHA `7a119d96...c380b` and four-asset SSE panel SHA `c3e58154...370ac` were revalidated before resuming.
- Strategy execution has only one W24 block and point-in-time official daily margin is not closed. Evidence remains `free_real_approx_conservative_margin`; strict candidates are 0.

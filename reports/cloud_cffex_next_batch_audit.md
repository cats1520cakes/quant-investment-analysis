# CFFEX next executable batch audit

Current implementation passes 42 focused ETF/derivative tests.

- Futures and options use official CFFEX daily rows; an absent/zero open or zero volume blocks execution.
- Capacity is causal: execution-date orders are capped by exact signal-date volume.
- OI is present in the catalog and helps contract selection, but no causal OI/volume crowding direction family currently exists. This is the next additive family.
- Whole-contract futures, daily mark-to-market, margin buffers and forced-liquidation paths already exist.
- Long IO/HO/MO structures buy actual listed contracts at executable daily opens and respect premium cash budgets, expiry and volume caps. Because bid/ask is absent, they remain `daily_settlement_no_quotes`, not quote-level strict execution.
- Short options remain forbidden. No synthetic Black–Scholes fill is admitted.

Next batch: add lagged OI change, volume/OI ratio and cross-contract OI concentration as signal-date-only gates; then rerun futures-only and fixed-premium long-convexity grids. Formal candidates remain **0**.

# ETF strict lineage audit

Audit date: 2026-07-12. Strict candidates: **0**.

## U3 frozen evidence

The frozen U3 run is already complete and must not be repeated: 174/174 specifications, 124 monthly cohorts per specification, two deposit timings and six non-overlapping W24 blocks. All four frozen families had zero dual-target passes. The results bind to official SSE panel SHA256 `f21ff743900607819436fc3897d1af6ac152e8993649241c79139ed26b6cb3b2` and the official event-ledger hash recorded by their manifests.

Official SSE history contains 5,196 rows for 510050, 3,431 for 510300 and 3,234 for 510500, ending 2026-07-10. Units are fund shares and CNY. The evaluation-period corporate-action chain contains 710 index records, 22 stable events for 2013-03-15 through 2023-12-31, plus 12 candidates reconciled to seven economic events for 2024-2025. The current-only parent universe remains survivor-biased.

The recovered runtime Parquet has SHA256 `4ebf9cc24a774489f8d0ddd0d7d99decc5251c46a15100539bc1ce057622a1dc`, not the frozen `f21ff743...`. It therefore cannot be used to rerun or amend the frozen results. This does not invalidate the already-published frozen results; it blocks only a new run from this runtime file.

## E1/E2/E3 strict blocker

The 159915 announcement chain is closed: 460 records, 10 terminal pages, ten candidate bodies read, two stable events and zero unresolved candidates. Official SZSE OHLCV is not closed: only 134 validated date responses survive in the evidence manifest, versus the required complete series from 2011-12-09 through 2025-12-31. Tencent has 3,539 raw/hfq rows but is cross-check-only and cannot promote the series to strict.

Minimum remaining evidence is therefore precise: complete the official SZSE daily series; expand it against an official trading calendar with every missing trading date either represented or supported by an official suspension; validate archive/snapshot unit continuity; publish the canonical panel and source-set hashes; then bind that panel and the already-closed event ledger to the frozen E1/E2/E3 runner. Until then E1/E2/E3 remain fail-closed and are not rerun.

Generated audit artifacts: `artifacts/derived/phase3_etf_strict_lineage_audit/manifest.json` and `coverage_matrix.csv`. No public raw data is included.

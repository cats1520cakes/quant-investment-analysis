# CFFEX frozen-panel lineage forensics — 2026-07-12

- Frozen byte SHA: `7a119d96a5a456f2b5635720263bbb22d3b7b633f667a54370b9deaf105c380b`.
- Official raw source-set SHA was exactly recovered: `9c00d2abf8112e73cee866653b30481f5617b584d97f717a3c34eac2155b1796`.
- Frozen Parquet bytes were not committed and no Work/connector-accessible copy or previously frozen canonical content hash was found. Therefore raw identity and matching dimensions cannot prove cell identity.
- `uv.lock` identifies the original Python>=3.11 environment as pandas 3.0.3 / pyarrow 24.0.0. Two completed bounded rebuilds (pandas 2.2.3 with pyarrow 25 and 22) did not reproduce the byte SHA. The exact locked attempt was blocked by two package-proxy timeouts and is retained as an incomplete attempt, not evidence of equivalence.
- The recovered panel canonical content hash is `6f239feecb171c17415de46d19606d9ef59801071ec2387cfef71b8324a2bec3`; it cannot activate the proposed dual-hash gate because no frozen canonical counterpart exists.
- Final bounded attempt used the exact `uv.lock` environment (pandas 3.0.3 / pyarrow 24.0.0) from the recorded official wheel URLs and reproduced the frozen SHA byte-for-byte: `7a119d96a5a456f2b5635720263bbb22d3b7b633f667a54370b9deaf105c380b`.
- Result: the panel-lineage blocker is closed and approximate strategy execution is permitted. Strict promotion remains blocked by point-in-time daily margin evidence and the five-block sample gate. Strict candidates: **0**.

Next independent evidence line: CFFEX daily option files expose open/settlement/volume but no executable bid/ask. Long-option work therefore remains `free_real_approx`; strict promotion requires an official or otherwise strict point-in-time quote chain. ETF official strict evidence remains usable independently of this CFFEX Parquet blocker.

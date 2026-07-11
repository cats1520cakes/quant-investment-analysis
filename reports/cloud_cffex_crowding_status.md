# CFFEX causal crowding status

Implemented three signal-date-only state gates:

- lagged aggregate open-interest change;
- aggregate volume/open-interest;
- maximum-contract OI divided by total product OI.

Quantile thresholds use `value.shift(1).expanding(...)`, so the current observation and future observations cannot set their own threshold. The gate is formed at signal-date close and can only affect next-open execution. Tests explicitly mutate the final observation and verify its threshold is unchanged.

Partial official IF smoke evidence: 29 months, 580 signal dates, 12 resolved gates and 6,960 gate-date rows. The full grid has 48 specifications; 36 IH/IC/IM entries are blocked as product-absent in the partial horizon. No W12/W24 claim is made from this partial panel. Strict candidates: **0**.

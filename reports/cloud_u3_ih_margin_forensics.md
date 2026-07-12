# U3 equal-weight × IH margin failure forensics

The 20 frozen v3 failures are not accounting-equation residuals. They are all
beginning-of-month timing rows where daily variation margin made free cash
negative, while the margin-call predicate still passed because it tested
`cash + margin < 0.75 * margin`.

Representative frozen spec `CO-0432` first fails on 2025-04-02:

- cash before settlement: CNY 559.61524
- IH settlement: 2664.2 to 2659.0, multiplier 300
- daily MTM: CNY -1,560
- cash after MTM: CNY -1,000.38476
- frozen margin: CNY 160,476
- ETF market value: CNY 390,093.20
- NAV: CNY 549,568.81524
- accounting equation residual: exactly CNY 0

The engine therefore has a margin-call trigger defect/incomplete funding rule,
not a double-entry arithmetic defect and not proven economic insolvency. The
frozen v3 results remain unchanged. After adding an explicit negative-cash
variation-margin action (forced liquidation or a separately specified ETF
funding liquidation), exactly the 20 affected frozen specs must be rerun with
old and corrected lineage retained.

Strict candidates remain 0.

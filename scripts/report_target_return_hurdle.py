from __future__ import annotations

from pathlib import Path

from quant_proof.target_math import annualize_monthly_return, required_monthly_return


def main() -> None:
    rows = []
    for months, target in [(12, 500_000.0), (24, 1_200_000.0)]:
        for timing in ["beginning", "ending"]:
            monthly = required_monthly_return(30_000.0, months, target, timing)
            rows.append((months, target, timing, monthly, annualize_monthly_return(monthly)))
    lines = [
        "# Target Return Hurdle",
        "",
        "Constant-return equivalent for zero initial cash and monthly deposits of 30,000 CNY.",
        "",
        "| months | target wealth | deposit timing | required monthly return | annualized equivalent |",
        "| ---: | ---: | --- | ---: | ---: |",
    ]
    for months, target, timing, monthly, annualized in rows:
        lines.append(f"| {months} | {target:,.0f} | {timing} | {monthly:.4%} | {annualized:.2%} |")
    lines.extend(
        [
            "",
            "These are smooth-path arithmetic hurdles, not forecasts. Real strategies also face volatility drag, fees, slippage, unavailable fills, and drawdowns.",
            "",
        ]
    )
    path = Path("reports/target_return_hurdle.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"report={path.resolve()}")


if __name__ == "__main__":
    main()

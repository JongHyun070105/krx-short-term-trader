# Phase 2 Results

## Run and data

- Research run: `phase2-20260928T003848+0900`
- Research code commit: `84cd63f4e05a23a55977a8a4f080b3244ba08540` (clean tree)
- Machine-readable report: ignored local file `runtime/research/phase2-20260928T003848+0900.json`
- Dataset SHA-256: `8b5982e01bde9928d045195d8060e0402639568ceaf9cd20a9f70841c405ef27`
- Source: actual KIS read-only daily, minute, and index data; no synthetic data used in research.
- Stock cohort: 30 current KIS common-stock listings, price 1,000–50,000 KRW, selected by current-master market-cap field.
- Minute data: 17 common sessions, 2026-09-01 to 2026-09-23; 193,446 bars; 30/30 symbols succeeded; 0 duplicate rows; 0 data-quality errors.
- Index context: KOSPI and KOSDAQ each have 50 daily bars from 2026-07-14 to 2026-09-23.
- Current-listing selection has survivorship bias. The short recent sample is not sufficient to establish alpha.

## Holdout integrity

The frozen 55/20/25 split was development 2026-09-01–11 (9 sessions), validation 2026-09-14–16 (3 sessions), and final partition 2026-09-17–23 (5 sessions). The final partition is **contaminated**: an earlier invalid cohort run (`phase2-20260927T233839+0900`) evaluated 2026-09-23 using an unintended 31-symbol set that included the 005930 smoke-test partition. That failed artifact is preserved locally. The corrected run used only the 30-symbol research cohort, but no strategy may be promoted from this touched final partition.

## Baseline results

All values below use the fixed Phase 1 defaults, 100,000 KRW starting capital, 20,000 KRW order cap, at most two positions, one-share quantities, scanner Top 10, and Regime ON. Fees (0.015%), sell tax (0.20%), and slippage (15 bps) are assumptions. With zero completed trades, PF and trade-derived metrics are undefined; zero return/PnL describes an uninvested portfolio, not evidence of a profitable strategy.

| Strategy | Interval | OOS sessions | Trades | Net return | Net PnL | PF | Expectancy | MDD | Win rate | Avg hold | Cost drag | 1.5x / 2.0x costs | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| Breakout + volume | 15m | 5 | 0 | 0.00% | ₩0 | N/A | N/A | 0.00% | N/A | N/A | ₩0 | FAIL / FAIL (no trades) | INSUFFICIENT_SAMPLE |
| Breakout + volume | 30m | 5 | 0 | 0.00% | ₩0 | N/A | N/A | 0.00% | N/A | N/A | ₩0 | FAIL / FAIL (no trades) | INSUFFICIENT_SAMPLE |
| Pullback + rebreak | 15m | 5 | 0 | 0.00% | ₩0 | N/A | N/A | 0.00% | N/A | N/A | ₩0 | FAIL / FAIL (no trades) | INSUFFICIENT_SAMPLE |
| Pullback + rebreak | 30m | 5 | 0 | 0.00% | ₩0 | N/A | N/A | 0.00% | N/A | N/A | ₩0 | FAIL / FAIL (no trades) | INSUFFICIENT_SAMPLE |

Development baselines and validation parameter neighbors also produced 0 trades in all four combinations. The fixed gate requires at least 30 OOS trades; actual OOS trades were 0. No thresholds or strategy parameters were changed after viewing results. Trade-series Sharpe/Sortino, concentration, KOSPI/KOSDAQ trade splits, and price/liquidity bucket PnL are N/A because no portfolio trades closed.

## Scanner and regime comparisons

The A/B run completed, but no portfolio arm had a fill: all-eligible + Regime ON = 0 trades; Top 10 + Regime ON = 0; Top 10 + Regime OFF = 0. Therefore this sample does not establish that the scanner improves performance.

All five OOS sessions classified `HIGH_VOL`, and Regime ON blocked every baseline strategy decision. With Regime OFF, the Top 10 produced 43 Breakout 15m entry signals, 26 Breakout 30m signals, and one Pullback signal at each interval, but none could be sized into a portfolio fill. Independent one-share sizing diagnostics classified the Breakout 15m signals as 37 `INSUFFICIENT_CASH_OR_CAP`, 4 `RISK_BUDGET_BELOW_ONE_SHARE`, and 2 `PRICE_OUT_OF_RANGE`; Breakout 30m as 20, 2, and 4 respectively. Both Pullback signals exceeded available cash/order cap for one share. The 20,000 KRW order cap and 0.25% per-trade risk budget were left unchanged.

## Decision

- 100,000 KRW portfolio: ending equity ₩100,000; realized PnL ₩0; no open positions.
- Cost stress: executed at 1.5x and 2.0x, with no trades and no positive stress evidence.
- Shadow candidate exists: **NO**. `SHADOW=NOT_PROMOTED`; no realtime collector was started.
- Alpha: `UNPROVEN`. OOS: `INSUFFICIENT_SAMPLE`. Live: `DISABLED`.
- Main blockers: Regime ON was `HIGH_VOL` for every OOS session; the 100,000 KRW share-level portfolio and order cap rejected every Regime-OFF signal; the final partition was previously touched and the sample had 0 of the required 30 OOS trades.

The KIS scan smoke occurred at 2026-09-28 00:35 KST, outside the regular session. Its returned candidates confirm response parsing and ranking only; they are not evidence of a live-session signal or strategy edge.

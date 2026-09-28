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

## Phase 2.5 Overnight results (2026-09-28)

### Frozen research sample and integrity

- Split manifest `runtime/research/phase25-split-plan-20260928.json` was frozen before strategy results: Development 49 sessions (2026-04-17–06-30), Validation 18 (2026-07-01–07-27), and a 23-session Fresh Holdout (2026-07-28–08-28). The Fresh Holdout remains `LOCKED_NOT_EVALUATED`.
- The same 30 current KOSPI symbols were backfilled for 90 common sessions: 1,018,940 actual KIS minute rows, dataset SHA-256 `bb33c49aa0ce533a7a3f8a15360665f25dbba2d0f738ccabaceab560165e5304`. It is `FRESH_RESEARCH_DIAGNOSTIC_WITH_GAPS`, quality `PARTIAL`, promotion false.
- DQ: 2,700 symbol/session partitions; all sessions have all 30 partition files; 2,288 partitions meet continuous regular-minute completeness and 412 are partial, with 956 expected minute slots absent. No synthesized bars were used. Duplicate, out-of-order, null/invalid OHLC, and negative-volume errors were all 0. Session/auction semantics and limitations are recorded in `runtime/research/phase25-diagnostic-dataset-manifest-v1.json` and `phase25-dataset-dq-20260928.json`.
- The September 1–23 Phase 2 sample and its contaminated final remain `DIAGNOSTIC_ONLY`; they were not merged with the frozen clean-split research.
- Fees 0.015%, sell tax 0.20%, and slippage 15 bps remain `ASSUMED`, not verified for the user's account. The 30-symbol current-listing cohort has survivorship bias and is not a historical full-market scanner replay.

### Signal-level evidence and portfolio evidence

Signal-level one-share diagnostics use next executable bars, strategy stops, max holding periods, and modeled fees/tax/slippage. They are not portfolio results.

| Clean split / strategy | Top-10 proxy signals | Closed diagnostic trades | Net PnL | Net expectancy | PF | Median MFE / MAE | 10-bar forward return |
|---|---:|---:|---:|---:|---:|---:|---:|
| Development Breakout 15m | 353 | 293 | −₩112,137 | −₩382.72 | 0.406 | +1.989% / −2.432% | −0.227% |
| Development Breakout 30m | 177 | 152 | −₩37,948 | −₩249.66 | 0.757 | +2.917% / −3.089% | −0.210% |
| Validation Breakout 15m | 124 | 103 | −₩51,007 | −₩495.22 | 0.151 | +0.966% / −2.031% | −0.566% |
| Validation Breakout 30m | 58 | 47 closed, 2 open | −₩30,448 | −₩647.82 | 0.101 | +1.782% / −3.593% | −0.796% |

Unfilled next-bar signals are excluded from the closed diagnostic-trade counts. All four Breakout cells were negative after assumed costs; 1.5x and 2.0x cost stress stayed negative. Session-cluster bootstrap intervals are descriptive only because this is a partial current-universe cohort. No parameter or cost tuning was used to choose a winner.

Pullback was infrequent: raw entries were Dev 19/10 and Validation 5/5 at 15m/30m respectively across 49/18 sessions. Top-10 one-share diagnostics were tiny and negative (Dev 15m: 9 closed, −₩3,821, PF 0.199; Dev 30m: 4 closed, −₩5,682, PF 0.501; Validation 15m: 1 closed loss; Validation 30m: 2 closed plus 1 open, negative). Verdict: `TOO_RARE` / deprioritize for the fast-trading objective; the small trade counts do not establish a precise edge estimate.

Phase 2's 69 raw Regime-OFF Breakout entries were also replayed as independent one-share diagnostics, not portfolio trades:

| Interval | Closed / unfilled | Wins / losses | Net PnL | Expectancy | PF | Median risk per share |
|---|---:|---:|---:|---:|---:|---:|
| 15m | 39 / 4 | 16 / 23 | −₩1,409.20 | −₩36.13 | 0.864 | ₩555.33 |
| 30m | 20 / 5 (plus 1 open) | 7 / 13 | −₩4,926.72 | −₩246.34 | 0.522 | ₩867.19 |

For the 69 signals, only 2/43 15m and 1/26 30m expected one-share risks were within the current ₩250 budget. Risk includes price stop distance plus modeled fee, tax, and slippage.

### Zero trades and 100K feasibility

- Phase 2 zero portfolio trades were not caused by absent signals: Regime-OFF raw Breakout entries existed, but Regime ON blocked all strategies in the five OOS sessions. In the expanded Dev+Validation regime comparison, the current binary gate was HIGH_VOL on all 67/67 dates (threshold 2.5% aggregate volatility over 20 sessions; the threshold was not changed).
- In the contaminated Phase 2 funnel, scanner eligibility excluded 54.7% of 15m and 54.1% of 30m bars; one-share order cap and price eligibility left only 4/43 and 2/26 Top-10 entries respectively within the cap, and only 2/43 and 1/26 met the current ₩250 risk budget. Risk/cash/position/next-bar constraints also apply. The dominant root cause classification is **C — BOTH**: no robust net signal edge and material small-account/execution constraints; current Regime ON additionally makes fills zero on the observed high-volatility dates.
- The frozen-split 16-case order-cap/risk matrix was diagnostic, not a live-setting optimizer. With the fixed-30-symbol Top-10 turnover proxy, 100K at 30K/0.50% produced 34 Dev and 14 Validation 15m portfolio fills, both net negative (−₩2,001 and −₩3,473). At 50K/0.50%, it produced 41 and 19 fills, both net negative (−₩4,986 and −₩4,968). No tested Top-10/20/30 15m/30m combination had positive Validation PnL. Larger caps/risk increase mechanical fills but do not repair the observed edge.
- Regime formula audit: sample standard deviation of daily log returns × sqrt(N), N=20; this is N-session aggregate volatility, not annualized volatility. The 0.025 threshold is 2.5%. Among the expanded Development dates, KOSPI median was 14.73% (minimum 8.24%) and KOSDAQ median 12.20% (minimum 5.59%); Validation medians were 21.32% and 18.74%. All 67 dates were HIGH_VOL. Hand-calculation regression coverage passed. No threshold was changed.
- Current-strategy feasibility at 100,000 KRW: **NO** (diagnostic portfolio feasibility is `CONSTRAINED`). Current ₩20K/0.25% settings produced 11 Dev and 2 Validation 15m portfolio fills only in the Regime-OFF comparison, both losing. The 30K/0.50% research scenario was frozen solely for the prospective second simulated sizing ledger; it is not a live recommendation. Do not infer that the user should add capital.
- Breakout decision: current baseline **NO**; redesign from failure anatomy before considering a new untouched validation. Pullback: **DEPRIORITIZE / TOO_RARE**. Alpha remains `UNPROVEN`; Fresh Holdout remains locked. Next phase: Breakout redesign (Path C), then freeze a new untouched validation; do not open this holdout to rescue results.

### Prospective Shadow infrastructure (separate from strategy evidence)

- Run `shadow-20260928`, scheduled 09:00–13:00 KST, actual duration 14,399.99 seconds; source SHA `53cfc0662d9cf9537325578e35f50d55b325dc1b`, clean tree at start, config SHA-256 `5e4a76154e1d7e687becd804caff85ac3d5542ed45d16740a9263019d11e0113`.
- KIS confirmed 2026-09-28 regular-session open from 005930's 09:00 minute bar, observed 09:01:18. Eight live scanner cycles ran; the last shortlisted 047040 as one eligible candidate. The session captured 512 minute rows across 005930, 004710, and 047040, from 09:00 through 12:58. Polling stopped at 13:00, so this is a partial-session sample, not a full-day close.
- 50 completed-bar decisions (Breakout/Pullback, 15m/30m) were captured; all 50 were `HOLD / REGIME_BLOCK` with HIGH_VOL. There were 0 ENTERs, 0 simulated intents, 0 fills, 0 open positions, and 0 observational marks. This is evidence of live pipeline execution and the regime gate, not strategy profitability.
- 468 data-health events reported 0 missing regular minutes at poll time, 0 duplicate/quality errors, 0 stale events, 0 API errors, 0 rate limits, and 0 process restarts. At the stop: no account data, `order_api_calls=0`, and no broker order endpoint calls.
- Replay verification `runtime/shadow/shadow-20260928/parity.json`: PASS, 50/50 decisions replayed, **0 parity mismatches**; event, bar, warmup, and index capture checks passed. Shadow infrastructure status `PASS`; strategy Shadow promotion remains `NO`; `LIVE=DISABLED`.
- Post-session read-only smoke: token-only auth PASS; quote 005930 PASS at 13:02 KST; daily data and market scan commands exited successfully, scan status PASS. Two pre-market quote GETs (005930 and 047040) failed with redacted `KisApiError`; the after-session 005930 retry succeeded. No account/balance/open-order or order API was called.

Machine-readable Phase 2.5 artifacts are ignored under `runtime/research/`, `runtime/shadow/shadow-20260928/`, `runtime/premarket/`, and `runtime/final-smoke/`. They retain the split, source hashes, diagnostics, health stream, parity report, and read-only smoke outputs.

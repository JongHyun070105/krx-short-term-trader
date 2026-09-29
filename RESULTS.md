# Phase 6: Cross-Sectional Laggard Rebound and Mean-Reversion Strategy

## Scope, freeze, and evidence boundaries

- Research implementation SHA: `b66aae1a890894e4f68335a957f5d1bc1fa7ff75` (clean tree at Phase 6 research run).
- Safe-period dataset: 46 complete symbols (30 KOSPI + 16 KOSDAQ), 67 safe sessions (2026-04-17 to 2026-07-27). One partial symbol (`125490`, 63/67 sessions) and 13 not-acquired symbols due to KIS API rate-limit timeouts.
- Development window: 2026-04-17 to 2026-06-30 (50 sessions).
- Secondary Diagnostic window: 2026-07-01 to 2026-07-27 (17 sessions).
- Fresh Holdout 2026-07-28 to 2026-08-28 remained `LOCKED_NOT_EVALUATED`; exactly 0 partitions, 0 features, and 0 signals from the holdout window were accessed.
- External untouched block (2026-01-05 to 2026-04-16) was not opened because no Secondary-surviving candidate exists.
- Transaction cost assumptions: 0.015% broker fee, 0.20% sell tax, 15 bps slippage. Stress scenarios at 1.5x and 2.0x evaluated.
- Primary interval: 15m. Secondary diagnostic interval: 30m.

## Data acquisition

- Target cohort: 60 symbols (30 KOSPI + 30 KOSDAQ) from Phase 4 frozen cohort manifest.
- Acquired: 46 complete symbols (67/67 safe-period partitions each), 1 partial symbol (`125490`, 63/67), 13 not acquired.
- KOSPI complete: 30 symbols. KOSDAQ complete: 16 symbols.
- Safe-period dataset: 46 complete symbols loaded for research. Dataset hash and cohort hash recorded in all artifacts.
- Holdout integrity: `holdout_partitions_opened = 0`.

## Primary hypothesis

> "Cross-sectional laggard stocks that show decelerating weakness, relative rank recovery, and absolute price reversal produce a cost-adjusted rebound edge."

The hypothesis was tested through Development anatomy analysis. Neither MR-A nor MR-B was created because the anatomy gate failed.

## Q1: Do bottom-ranked intraday stocks rebound?

**WEAK.** Bottom-ranked stocks (0-10 percentile, n=6,377) showed a small positive 1-bar forward return (+0.032%, win rate 45.2%) but negative 4-bar (-0.020%) and 8-bar (-0.104%) returns. The rebound was small, short-lived, and did not persist. Bottom-ranked stocks also experienced worse MAE (-1.20%) than MFE (+1.27%), with time-to-MAE (3.83 bars) slightly longer than time-to-MFE (3.60 bars).

## Q2: Does stabilization + rank recovery outperform blind laggard selection?

**NO.** The stabilized relative+absolute reversal group (n=1,114) had:
- 1-bar forward return: -0.023% (worse than blind laggard baseline -0.007%)
- 2-bar forward return: -0.0004% (similar to baseline -0.017%)
- 4-bar forward return: -0.019% (better than baseline -0.063% but still negative)
- Win rate: 38.1% (1-bar), 42.3% (2-bar), 42.6% (4-bar) - consistently below 50%
- Future rank change: +8.4 points (1-bar), +20.0 points (2-bar) - rank improved but price did not follow

The stabilization filter reduced sample size without improving forward returns sufficiently to overcome transaction costs.

## Q3: Does the effect persist on cleaner/fresher observations?

**NO.** Clean-data sensitivity (high freshness + high completeness) showed negative direction, indicating the small observed rebound was not robust to data quality filtering.

## Anatomy gate failure

Both MR-A and MR-B were not created because the Development anatomy gate failed on three of four checks:
1. Development sample at least 30: **PASS** (1,114 stabilized observations)
2. Development forward 4-bar positive: **FAIL** (-0.019%)
3. Development beats blind laggard: **FAIL** (worse 1-bar return than baseline)
4. Clean data same positive direction: **FAIL**

## Cross-sectional rank dynamics

- Rank recovery was observed: bottom-ranked stocks improved their cross-sectional rank by +8.4 points (1-bar) to +40.9 points (4-bar).
- However, rank improvement did not translate into price outperformance. The market's cross-sectional structure exhibited mean-reversion in rank but not in absolute returns for the laggard group.

## Time-of-day analysis

Forward returns were analyzed across all time buckets (09:00-09:30, 09:30-10:00, 10:00-11:00, 11:00-12:00, 12:00-13:00, 13:00-14:00, 14:00-15:00, 15:00+). No time bucket showed a persistent positive rebound effect for laggard stocks.

## Market split

- KOSPI complete: 30 symbols
- KOSDAQ complete: 16 symbols
- Both markets showed similar negative forward return patterns for bottom-ranked stocks.

## Liquidity analysis

Traded-value bucket analysis (0-20%, 20-40%, 40-60%, 60-80%, 80-100% percentile) was performed. No liquidity bucket showed a persistent positive rebound effect.

## Extreme session losers

Session return bins (≤-8%, -8 to -5%, -5 to -3%, above -3%) were analyzed. Extreme laggards showed falling-knife behavior rather than rebound.

## Final verdicts

| Gate / Metric | Phase 6 Verdict | Operational Meaning |
|---|---|---|
| `PHASE6_DATA_ACQUISITION` | `PARTIAL` | 46/60 symbols acquired; KIS rate limits prevented full coverage |
| `MEAN_REVERSION_ANATOMY` | `COMPLETE` | Full anatomy analysis performed on available data |
| `MR_A` | `INSUFFICIENT` | Anatomy gate failed; no positive rebound beating blind laggard |
| `MR_B` | `INSUFFICIENT` | Same anatomy gate failure as MR-A |
| `MEAN_REVERSION_FAMILY` | `INSUFFICIENT` | No viable candidate for Secondary Diagnostic |
| `EXTERNAL_VALIDATION` | `NOT_AVAILABLE` | No Secondary-surviving candidate; untouched block preserved |
| `100K` | `NO` | No candidate for portfolio replay |
| `SHADOW_NEXT_SESSION` | `NO` | No candidate for prospective Shadow |
| `ALPHA` | `UNPROVEN` | No demonstrable market edge |
| `LIVE` | `DISABLED` | Live trading prohibited |

## Answers to final questions

- **Q1: Do bottom-ranked intraday stocks rebound?** WEAK (small positive 1-bar, negative longer-term)
- **Q2: Does stabilization + rank recovery outperform blind laggard selection?** NO
- **Q3: Does the effect persist on cleaner/fresher observations?** NO
- **Q4: Does MR-A or MR-B have positive gross expectancy?** NO (neither variant was created)
- **Q5: Does either have positive net expectancy?** NO
- **Q6: Does Secondary preserve the same direction?** NO (Secondary was not run)
- **Q7: Does untouched external evidence pass?** NOT_AVAILABLE
- **Q8: Is the 100K whole-share implementation feasible?** NO
- **Q9: Is there a candidate for a future prospective Shadow session?** NO

## Phase 6 artifacts

All 24 artifacts are recorded in `runtime/research/phase6/phase6-artifact-index.json`. Key artifacts:
- `phase6-acquisition-manifest.json` - Data acquisition state
- `phase6-dq.json` - Data quality report
- `mean-reversion-anatomy-15m.json` - 15m anatomy analysis
- `mean-reversion-anatomy-30m.json` - 30m anatomy analysis
- `mr-hypotheses.json` - Hypothesis support analysis
- `mr-a-development.json` / `mr-b-development.json` - Variant development results (both INSUFFICIENT)
- `phase6-summary.json` - Complete research summary with verdicts and question answers

Machine-readable Phase 6 artifacts are intentionally ignored under `runtime/research/phase6/`. Runtime data was not committed.

---

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

## Phase 3 — Breakout failure anatomy and v2 rejection

### Scope, provenance, and integrity

- Research implementation SHA: `623edb0abb22f9d3610cc1f05408882ab78774d1` (the code SHA recorded in every Phase 3 run artifact).
- Source dataset SHA-256: `bb33c49aa0ce533a7a3f8a15360665f25dbba2d0f738ccabaceab560165e5304`; selected Development + Validation partition hash: `9c9e41ccb650b3b10b2a1818b2742f21dd63bf8ea241ecd7a593ba945b3f06c`.
- 30 current KOSPI listings, fixed Top-10 turnover scanner proxy; 49 Development sessions (2026-04-17–06-30) and 18 Validation sessions (2026-07-01–07-27). This is a current-listing historical proxy with survivorship bias, not a full historical KRX universe replay.
- Selected input: 2,010 symbol/session minute partitions, 758,919 minute rows, of which 272 selected partitions are incomplete; the broader 90-session source remains `PARTIAL` (412 partial partitions, 956 missing minute slots). No synthetic bars were added. Cost inputs (0.015% fee, 0.20% sell tax, 15 bps slippage) are assumptions.
- `runtime/research/phase3/` holds ignored event Parquet and machine-readable JSON, including interval anatomy, comparison, hypothesis, and validation reports. Event files contain 477 15m and 235 30m ENTER events; the schema separates signal-time features from later trade outcomes. Features stop at the completed signal bar. Each run records code/data hashes, split, schema, source artifact hashes, and outputs.
- Fresh Holdout `2026-07-28–08-28` remained `LOCKED_NOT_EVALUATED`; zero post-validation minute partitions were opened. The index parquet row group overlaps the later period, so index context was not read; index direction, regime, and `MARKET_AGAINST` labels are explicitly unavailable. No post-validation feature, MFE/MAE, signal, or parameter comparison was run.
- Existing Phase 2/2.5 evidence and status are preserved. Phase 2.5 baseline numbers above are unchanged.

### Breakout v1 event-level results

The following are independent one-share signal diagnostics with next executable-bar entry, baseline stops/holding limit, and assumed costs. These are not portfolio equity curves; event-level MDD is therefore N/A. `MFE/MAE` below are signal-path median excursions. Closed counts exclude unfilled events; 30m Validation retains two open events.

| Interval / split | Signals | Closed | Unfilled / open | Wins / losses | Stop / time exits | PF | Expectancy | Net PnL | Gross PnL / cost drag | Median MFE / MAE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 15m Development | 353 | 293 | 60 / 0 | 86 / 207 | 122 / 171 | 0.406 | −₩383 | −₩112,137 | −₩50,091 / ₩62,046 | +1.989% / −2.432% |
| 15m Validation | 124 | 103 | 21 / 0 | 13 / 90 | 58 / 45 | 0.151 | −₩495 | −₩51,007 | −₩34,840 / ₩16,167 | +1.152% / −2.545% |
| 30m Development | 177 | 152 | 25 / 0 | 52 / 100 | 67 / 85 | 0.757 | −₩250 | −₩37,948 | −₩5,845 / ₩32,103 | +2.917% / −3.089% |
| 30m Validation | 58 | 47 | 9 / 2 | 6 / 41 | 25 / 22 | 0.101 | −₩648 | −₩30,448 | −₩23,145 / ₩7,303 | +1.782% / −3.593% |

All four gross-PnL results were already negative before modeled costs. Cost drag deepened each loss; the result is not explained by costs alone. 15m signal follow-through at +0.5% / +1% / +2% was 83.3% / 68.6% / 49.6% in Development and 74.2% / 54.0% / 28.2% in Validation. For 30m it was 84.7% / 73.4% / 59.9% and 82.8% / 70.7% / 44.8%. The gap between some favorable excursions and negative realized expectancy shows that favorable movement did not reliably become an exit win under the current entry/exit policy.

False-breakout definition was frozen from Development per interval: signal-path MFE below the Development P25 and MAE at or below its Development median. This yields 15m 17.56% Development / 25.81% Validation and 30m 17.51% / 22.41%. This is a descriptive label, not a universal definition.

### Failure anatomy and the largest loss sources

Deterministic categories overlap, so percentages below are shares of losing closed events and must not be summed. Categories use only point-in-time context and observed outcome paths.

| Interval / split | Largest categories among losses |
|---|---|
| 15m Development (207 losses) | Immediate rejection 83 (40.1%); late entry 63 (30.4%); no follow-through 53 (25.6%); volume-spike fade 53 (25.6%); repeated level 52 (25.1%) |
| 15m Validation (90 losses) | Low-liquidity flag 57 (63.3%); no follow-through 32 (35.6%); immediate rejection 29 (32.2%); volume-spike fade 21 (23.3%) |
| 30m Development (100 losses) | Volume-spike fade 42 (42.0%); late entry 31 (31.0%); immediate rejection 29 (29.0%); no follow-through 29 (29.0%) |
| 30m Validation (41 losses) | Low-liquidity flag 29 (70.7%); immediate rejection 19 (46.3%); volume-spike fade 15 (36.6%) |

The top three cross-split explanations are: (1) adverse/fast reversal or no continuation—15m stopped out 41.6% of Development and 56.3% of Validation closed events; 30m stopped out 44.1% and 53.2%; (2) chasing after prior extension—15m late-entry labels covered 30.4% of Dev and 17.8% of Validation losses, while winner median session extension was lower in both; (3) noisy volume/liquidity and execution drag—volume-spike fade marked 25.6%/23.3% of 15m losses and 42.0%/36.6% of 30m losses, while Validation low-turnover labels were frequent. These are overlapping diagnostics, not proven causal shares. The low-liquidity label uses a Development turnover quantile applied to the fixed cohort; it does not establish that raising the liquidity threshold would create alpha.

### Winner / loser separation and feature stability

The compact ranking below shows winner and loser medians; full P25/P50/P75 distributions and effect sizes are in the ignored anatomy JSON. 15m winners/losses numbered 86/207 in Dev and only 13/90 in Validation; 30m had 52/100 and 6/41. Validation winner medians are particularly noisy.

| Feature (winner / loser median) | 15m Dev | 15m Validation | 30m Dev | 30m Validation |
|---|---:|---:|---:|---:|
| Return from session open | 2.68% / 4.31% | 3.07% / 3.99% | 3.72% / 4.52% | 4.40% / 4.42% |
| Relative volume | 4.47 / 4.48 | 5.22 / 3.80 | 3.97 / 4.27 | 3.61 / 3.10 |
| Upper-wick / bar-range ratio | 0.125 / 0.192 | 0.250 / 0.188 | 0.237 / 0.209 | 0.211 / 0.315 |
| Body / total range | 0.608 / 0.588 | 0.556 / 0.616 | 0.530 / 0.519 | 0.609 / 0.547 |
| Distance from intraday high | −0.560% / −0.726% | −0.564% / −0.498% | −1.020% / −0.951% | −0.918% / −0.956% |
| Opening gap | 2.37% / 1.66% | 1.69% / 1.09% | 2.12% / 2.46% | 1.51% / 1.20% |
| Close-location value | 0.805 / 0.778 | 0.625 / 0.813 | 0.741 / 0.756 | 0.789 / 0.685 |

The only modest repeatable directional clue is lower session extension among 15m winners: median separation −1.63 percentage points in Dev and −0.92 points in Validation (Cliff's delta −0.256 and −0.171). It is not enough to distinguish profitable trades: the 30m difference shrinks from −0.80 points in Dev to −0.03 in Validation. Wick/body/close-location relationships reverse across splits, and RVOL does not separate 15m Dev winners from losers. Relative volume and relative turnover are nearly duplicate variables (Pearson r 0.998 in 15m Dev and 0.997 in 30m Dev); they should not be stacked as separate filters. No machine-learning classifier, magic score, or post-hoc p-value selection was used. Session-cluster bootstrap intervals were descriptive (2,000 resamples): 15m expectancy 95% intervals were −₩560 to −₩234 (Dev) and −₩693 to −₩254 (Validation); 30m were −₩847 to +₩371 and −₩1,017 to −₩420.

### Time, volume, candle, gap, liquidity, and price context

Time bucket entries are `signals / closed; PF; expectancy KRW`. The 30m interval has no 09:00–09:30 bucket; sparse buckets and Validation cells are not stable estimates.

| Time bucket | 15m Dev | 15m Val | 30m Dev | 30m Val |
|---|---:|---:|---:|---:|
| 09:00–09:30 | 123/112; .472; −330 | 51/41; .092; −595 | — | — |
| 09:30–10:00 | 74/65; .456; −282 | 16/14; .337; −370 | 79/69; .477; −393 | 26/21; .066; −614 |
| 10:00–11:00 | 45/38; .167; −706 | 22/20; .017; −463 | 38/33; .570; −533 | 15/13; .000; −929 |
| 11:00–12:00 | 30/21; .077; −538 | 5/5; .124; −252 | 22/19; 1.553; +746 | 3/3; .796; −66 |
| 12:00–13:00 | 15/11; .032; −730 | 2/2; .000; −733 | 10/10; .000; −900 | 2/2; 2.194; +363 |
| 13:00–14:00 | 14/11; 1.564; +372 | 8/7; .000; −538 | 10/7; .772; −237 | 0/0; N/A; N/A |
| 14:00–15:00 | 27/22; .575; −318 | 15/12; .440; −386 | 13/11; .986; −22 | 9/7; .000; −911 |
| 15:00+ | 25/13; .111; −596 | 5/2; .000; −521 | 5/3; 2.007; +1,164 | 3/1; N/A; +383 |

15m opening (09:00–10:00) remained negative: Dev 197/177, PF .467, −₩313; Validation 67/55, PF .147, −₩538. Midday (11:30–13:30) was Dev 29/22, PF .317, −₩407 and Validation 7/7, PF 0, −₩547. Late (>14:30) had overnight exits for 23/27 closed Dev trades and 9/10 Validation trades; their expectancy was −₩351 and −₩275. 30m midday and late had small positive Dev cells (21/20, PF 1.308; 10/7, PF 1.276) that did not generalize (3/3, PF 1.183; 10/6, PF .132). No time-of-day strategy was promoted.

| RVOL bucket | 15m Dev (signals; PF; exp) | 15m Val | 30m Dev | 30m Val |
|---|---:|---:|---:|---:|
| 1.5–2.0 | 49; .300; −422 | 21; .499; −226 | 23; .694; −411 | 5; .522; −212 |
| 2.0–3.0 | 74; .648; −167 | 26; .109; −421 | 39; 1.071; +62 | 22; .018; −458 |
| ≥3.0 | 230; .376; −435 | 77; .098; −593 | 115; .684; −323 | 31; .088; −848 |

The existing v1 requires RVOL ≥1.5, so lower buckets had no eligible events. High RVOL was not reliably better; the isolated 30m Dev PF above 1 disappeared in Validation. Relative-volume and relative-turnover comparisons therefore do not support adding another volume gate.

- **Breakout distance:** 15m's 0.5–1.0% bucket was the least negative common band (Dev 70 events, PF .437, −₩283; Validation 19, PF .468, −₩268); >1% stayed worse (188, .414, −₩411; 69, .097, −₩614). For 30m, 0.2–0.5% was positive in Dev (28, PF 1.427, +₩331) but reversed in Validation (11, .114, −₩550). No threshold generalized.
- **Candle structure:** 15m HIGH close-location bucket had PF .552/−₩283 Dev and .120/−₩582 Validation; the apparently better Validation LOW bucket had only six signals/four closed. Dev winners' upper wick median was 0.125 vs 0.192, but Validation reversed to 0.250 vs 0.188. A strong-close/wick filter was rejected as unstable.
- **Gap / extension:** 15m >5% gap had 55 Dev signals, PF .703, −₩163, but only four Validation signals/three closed (PF 0, −₩1,604). For 30m, 0–1% gap had Dev PF 2.007/+₩567 (21 closed) but Validation PF .051/−₩1,210 (12 closed). These cells do not justify gap filters. Lower pre-entry extension is the sole plausible but weak 15m separation noted above.
- **Liquidity / price:** 15m LOW turnover quantile looked less negative in Dev (117 signals, PF .839, −₩54) than HIGH (117, .251, −₩636), but LOW still lost and Validation LOW was PF .162, −₩446. 30m liquidity buckets were all net negative in Validation. 15m 1k–10k KRW shares had only 27 Dev events (PF .463, −₩40) and none in that price bucket in Validation; no low-price or whole-share advantage is established. The wider stock cohort is still KOSPI-only.

### MFE, MAE, data gaps, and holding behavior

- Median executable-trade MFE/MAE was 15m Dev +1.689%/−2.028%, Val +0.966%/−2.031%; 30m Dev +2.488%/−2.816%, Val +1.414%/−2.309%. Signal-path excursion medians are in the baseline table. Session-cluster bootstrap results and P25/P50/P75 are retained in the per-interval JSON.
- Median bars to MFE / to stop failure: 15m Dev 2 / 2, Validation 1 / 2.5; 30m Dev 2 / 4, Validation 3 / 1. Late 15m entries had 23/27 Dev and 9/10 Validation closed trades exit overnight because their holding window crossed session end; this is a timing diagnostic, not an exit optimization.
- Near-gap sensitivity remains negative. Complete-window-only 15m Dev: 210 signals, 169 closed, PF .257, expectancy −₩424, net −₩71,723 (vs all PF .406/−₩383/−₩112,137); Validation: 94/77, PF .111, −₩491, −₩37,800 (vs .151/−₩495/−₩51,007). 30m complete-only Dev: 81/70, PF .648, −₩250; Validation: 29/23, PF .065, −₩565. Removing near-gap events does not change the rejection.

### Hypotheses, v2 comparisons, and cost stress

| Variant | Frozen rule / evidence | Development | Validation / disposition |
|---|---|---|---|
| V1 15m | Existing breakout + volume baseline | 353 signals, 293 closed; PF .406; expectancy −₩383; net −₩112,137 | 124 / 103; PF .151; −₩495; −₩51,007 |
| V2-A 15m | Not overextended: session-open return ≤ Development P75, 7.0626% | 265 / 216; PF .483; −₩279; −₩60,235; 75.1% signal retention | 105 / 86; PF .193; −₩439; −₩37,760; 84.7% of Validation baseline signals retained; REJECTED |
| V2-B 15m | Strong close / limited upper wick | Dev winner median upper wick .125 vs loser .192 | Validation direction reversed (.250 vs .188); screening only, REJECTED unstable; not sent to Validation backtest |
| V2-C 15m | RVOL <3.0, predeclared bucket boundary | 123 / 89; PF .500; −₩262; −₩23,302; only 34.8% retention; false-breakout rate 19.51% vs V1 17.56% | Rejected on Development retention and false-break rate; Validation metrics deliberately not computed |
| 30m candidates | No stable winner/loser structure | No candidate selected | No Validation candidate; v1 remains negative |

V2-A removed 88/353 Development signals; the 77 removed closed trades had PF .282, expectancy −₩674 and net −₩51,902. This improves the retained subset's loss but does not make it positive. Validation removed 19 signals; the 17 closed removed trades had PF .003 and expectancy −₩779, while retained trades still had expectancy −₩439. Dev-derived nearby thresholds 6.0626%, 7.0626%, and 8.0626% yielded Validation expectancy −₩409, −₩439, and −₩452: direction vs baseline is nearby-stable, but no positive plateau exists. V2-A false-break rate moved 17.56%→16.60% in Dev and 25.81%→26.67% in Validation.

| 15m strategy / split | Net PnL at 1.0x | 1.5x | 2.0x |
|---|---:|---:|---:|
| V1 Development | −₩112,137 | −₩143,160 | −₩174,183 |
| V2-A Development | −₩60,235 | −₩82,418 | −₩104,602 |
| V1 Validation | −₩51,007 | −₩59,091 | −₩67,175 |
| V2-A Validation | −₩37,760 | −₩44,560 | −₩51,361 |

Validation gates fail for V2-A: expectancy ≤0, PF ≤1 (.193), negative at 1.5x costs, false-break rate not reduced, despite 86 closed trades and improved Dev expectancy. Therefore `FAILED_GENERALIZATION_NO_EDGE_AFTER_COST`, not `VALIDATION_CANDIDATE`. Break-even slippage was not estimated because no Validation candidate had positive expectancy.

### ₩100,000 whole-share feasibility

This separate portfolio replay used fixed scanner settings, ₩100,000 starting capital, actual whole-share fills, and unchanged live settings. `20K/.25%` and `30K/.50%` are research scenarios, not recommendations.

| 15m strategy / split | Scenario | Fills | Net PnL | PF | MDD | Avg invested / idle cash |
|---|---|---:|---:|---:|---:|---:|
| V1 Dev | 20K / .25% | 11 | −₩1,186 | .064 | 1.28% | ₩456 / 99.54% |
| V2-A Dev | 20K / .25% | 7 | −₩544 | .129 | 0.76% | ₩379 / 99.62% |
| V1 Validation | 20K / .25% | 2 | −₩415 | 0.000 | 0.63% | ₩313 / 99.69% |
| V2-A Validation | 20K / .25% | 2 | −₩415 | 0.000 | 0.63% | ₩313 / 99.69% |
| V1 Dev | 30K / .50% | 34 | −₩2,001 | .640 | 4.50% | ₩3,732 / 96.27% |
| V2-A Dev | 30K / .50% | 28 | −₩1,086 | .750 | 3.75% | ₩3,395 / 96.61% |
| V1 Validation | 30K / .50% | 14 | −₩3,473 | .134 | 3.47% | ₩3,663 / 96.34% |
| V2-A Validation | 30K / .50% | 13 | −₩3,152 | .146 | 3.30% | ₩3,557 / 96.44% |

For 30m V1, Dev had 3 / 9 fills and +₩210 / +₩616 in the two scenarios, but Validation had 1 / 4 fills and −₩153 / −₩1,583. That tiny Dev result does not qualify 30m; no 30m V2 candidate was selected. Typical idle cash was 96–99.95%, with only 1–14 Validation fills in the reported portfolios. Whole-share eligibility, risk sizing, and low utilization constrain execution; even the larger research scenario loses in Validation. Feasibility verdict: `CONSTRAINED`, with no capital-increase recommendation.

### Phase 3 decision and verification

- **Why did v1 lose?** Fast rejection / stop-outs and weak follow-through, late/extended entries, and volume/liquidity plus modeled execution drag are the three largest observed mechanisms. Every baseline interval/split was already gross-negative.
- **Is a repeatable winner-vs-loser feature present in both splits?** `WEAK`: 15m lower prior session extension repeats directionally with small effect, but no candidate turns positive; 30m feature differences largely vanish or flip.
- **Does a v2 candidate have positive cost-adjusted Validation expectancy?** `NO`. V2-A is rejected; V2-B and V2-C were screened out before Validation; no 30m candidate survived.
- **Is it executable in a ₩100,000 whole-share portfolio?** `CONSTRAINED`; portfolio fills are sparse and all V2-A Validation scenarios lose.
- Final: `BREAKOUT=REJECTED`; `PULLBACK=TOO_RARE / DEPRIORITIZED`; `ALPHA=UNPROVEN`; `LIVE=DISABLED`; `BREAKOUT_V2=NONE`; `SHADOW_FOR_V2=NOT_STARTED`.
- Next phase: **D — improve clean evidence / cohort breadth first**, preferably accumulate future KRX sessions or freeze a genuinely untouched historical block. Do not open the locked Phase 2.5 Holdout. After that, the one evidence-linked strategy hypothesis worth considering is a breakout retest/acceptance entry: require price to hold the broken level after the first push, targeting the observed immediate-rejection and no-follow-through failures. It is only a research proposal; no new strategy was implemented or validated here. Current-listing survivorship and the fixed-cohort scanner proxy remain material limitations.
- Research artifacts: ignored `runtime/research/phase3/breakout-signals-{15m,30m}.parquet`, `breakout-anatomy-{15m,30m}.json`, `breakout-v2-comparison-{15m,30m}.json`, `v2-hypotheses.json`, `v2-validation.json`, and `breakout-anatomy-summary.json`. `runtime/` remains untracked/ignored.
- Regression: `uv run ruff check src tests` PASS; `uv run pytest -q` PASS (105 tests); `uv run python -m compileall -q src tests` PASS; `git diff --check` PASS. New tests cover lookahead isolation, holdout guard, feature calculations, outcome labeling, data-gap flags, deterministic buckets, filter attribution, retention, and validation isolation.
- Phase 2/2.5 findings and the locked Holdout status were not changed. Source and research output are diagnostic only; no strategy promotion, V2 Shadow, account access, or live trading occurred.

## Phase 4 — Clean evidence expansion and breakout retest / acceptance

### Scope, freeze, and evidence boundaries

- Retest strategy freeze commit: `d0fad5bf18a3e1eae010dca21490ad15ba04dddd` (`feat: preregister breakout retest strategies`). Frozen config SHA-256: `4076bb140d1e9452e7788bc2730497a77af11a28ece1bd8685c37eaddb34980b`. Retest source SHA-256: `6fb31201b8f36b12d1b36b21913a167a762d303b7756f8a37bfece922b72fd28`.
- Historical dataset source SHA-256: `bb33c49aa0ce533a7a3f8a15360665f25dbba2d0f738ccabaceab560165e5304`. Retest used only 2,010 original-30 symbol/session partitions for Development (2026-04-17–06-30) and secondary Validation (2026-07-01–07-27); their selected partition SHA-256 is `9c9e41ccb650b3b10b2a1818b2742f21dd63bf8ea241ecd7a593ba945b3f06c`. V1 replay parity against the Phase 3 event files was exact for all four interval/split cells: 0 status mismatches, 0 exit-time mismatches, and 0 net-PnL differences above ₩0.02.
- The 23-session Fresh Holdout, 2026-07-28–08-28, stayed `LOCKED_NOT_EVALUATED`. No Holdout minute partition, feature, signal, PnL, or index context was opened. The Phase 3 index row group that overlaps the locked period was not read.
- Baseline costs remain assumptions: 0.015% broker fee, 0.20% sell tax, and 15 bps slippage. The study also records 1.5x and 2.0x stress. Regime v1 was OFF for primary evaluation; no V1/V2 threshold was retuned.

### Data quality and missing-minute semantics

The Phase 2.5 full 90-session summary remains as originally reported: 412 partial partitions and 956 expected minute slots absent. Phase 4 did not reopen all 90 sessions because that would cross the locked Holdout boundary. Its safe 67-session Development + Validation audit found 2,010/2,010 partition files, 759,420 expected slots, 758,910 observed slots, 510 absent slots, and 263 partial partitions (Development 178 partitions / 264 slots; Validation 85 / 246).

All 510 Phase 4 safe-window absences remain `UNKNOWN`: 0 were confirmed as retrieval gaps, no-trade minutes, provider-omitted no-trade minutes, session semantics, or halt/special-status minutes. No synthetic bars were added. Two already-existing bounded pre-Holdout KIS rereads showed persistent omissions (001440, 2026-06-08, 09:04–09:33; 034220, 2026-05-13, 10:22), but neither identifies whether a trade occurred. A fresh KIS reread was not performed because app credentials were absent from the process environment; `.env` was not read. Therefore `DATA_QUALITY=PARTIAL`, not evidence of 510 confirmed corrupt records.

### Expanded current-listing cohort

The deterministic cohort selection was frozen before the Retest study from the 2026-09-28 KIS common-stock master. It preserves all original 30 KOSPI symbols and expands to 60 (30 KOSPI / 30 KOSDAQ) and 100 (50 / 50). Price buckets are 19 / 22 / 19 for the 60 cohort and 34 / 38 / 28 for the 100 cohort across ₩1k–10k / ₩10k–30k / ₩30k–50k. At current reference prices, 34/60 and 60/100 are affordable as a single share under the ₩20K order cap. Affordability did not filter selection.

This remains a `CURRENT-LISTING COHORT`; historical point-in-time membership and survivorship are not reconstructed. Turnover was absent from the current master, so market-cap strata are only a labeled selection proxy. The 60/100 cohorts have metadata only—no expanded minute bars were acquired—so expanded-cohort strategy, KOSDAQ, liquidity-bucket, and breadth-generalization results are `NOT_AVAILABLE`. The Retest outcomes below use only the original 30 KOSPI names.

### Frozen Retest / Acceptance rules

Only two variants were registered. Both preserve the V1 breakout detector: a completed close above the previous 20 completed-bar highs and volume at least 1.5x the preceding 20-bar mean. They then wait up to three completed bars for the same breakout level to be retested. A retest bar's low must reach within 0.30% above the level, must not penetrate more than 0.30% below it, and must close above the level. A close at/below the level, excessive penetration, session boundary, duplicate level, or timeout invalidates/expires the setup.

- **RETEST-A:** acceptance is the retest bar's completed close above the breakout level.
- **RETEST-B:** after a valid retest, a later completed bar within the same window must close above the retest bar's high.
- Both signal only after a completed bar. Entry is the next available bar open; the acceptance bar's OHLC is never a fill. Initial stop is the lower of breakout level and retest low minus a 0.30% structural buffer. The existing 10-bar max-holding/stop-first exit is reused. Same resistance level cannot create a duplicate setup within a session.

The exact rules/config and 2026-01-05–2026-04-16, 100-symbol external block were frozen in ignored local artifact `runtime/research/phase4/retest-preregistration.json` before that block was inspected. External-block inspection afterward checked only 10,200 explicit parquet paths; 0 were present and 0 file contents were read.

### Used-data results: V1 vs Retest-A / Retest-B

Each expectancy is one-share event-level KRW expectancy after baseline modeled costs. Development is `DESIGN EVIDENCE`; the already-used Validation is only `SECONDARY DIAGNOSTIC`, never independent validation.

| Interval / split | V1 closed; net exp / PF | RETEST-A closed; gross exp → net exp / PF | RETEST-B closed; gross exp → net exp / PF |
|---|---:|---:|---:|
| 15m Development | 293; −₩382.72 / 0.406 | 162; −₩7.89 → −₩183.81 / 0.447 | 30; +₩18.67 → −₩147.04 / 0.404 |
| 15m Validation | 103; −₩495.22 / 0.151 | 64; −₩39.91 → −₩192.91 / 0.323 | 12; −₩3.07 → −₩173.56 / 0.341 |
| 30m Development | 152; −₩249.66 / 0.757 | 59; +₩149.51 → −₩22.43 / 0.935 | 10; −₩84.71 → −₩273.11 / 0.426 |
| 30m Validation | 47; −₩647.82 / 0.101 | 33; −₩18.13 → −₩165.27 / 0.513 | 5; +₩163.00 → +₩0.26 / 1.001 |

Retest-A is negative after cost in all four cells. Retest-B is negative in three cells; its only positive cell is +₩0.26 expectancy from five 30m Validation trades, with PF 1.001. No candidate passes the project evidence gate. The B sample is too small to qualify, while A has enough events to reject on the observed cost-adjusted results.

### Funnel, false-breakout failures, and frequency

Summed across the separate interval/split replays, the A funnel is 1,362 breakouts → 817 retest attempts → 395 held retests/acceptances → 319 next-bar executable → 73 one-share affordable → 68 risk-eligible → 49 portfolio fills → 318 closed event-level trades. The B funnel is 1,333 → 797 → 381 → 66 → 57 → 16 → 8 → 7, with 57 closed event-level trades. These counts cover overlapping 15m/30m evidence and separate portfolio replays; they are not independent samples or one combined portfolio.

| Interval / split | V1 immediate rejection / no-follow-through | A immediate rejection / no-follow-through | B immediate rejection / no-follow-through |
|---|---:|---:|---:|
| 15m Development | 28.3% / 18.4% | 39.5% / 46.3% | 16.7% / 23.3% |
| 15m Validation | 28.2% / 31.1% | 46.9% / 45.3% | 16.7% / 25.0% |
| 30m Development | 19.1% / 19.7% | 35.6% / 37.3% | 0.0% / 30.0% |
| 30m Validation | 40.4% / 19.1% | 42.4% / 51.5% | 0.0% / 40.0% |

Retest-B reduced the measured immediate-rejection rate in all four cells (11.5–40.4 percentage points), but no-follow-through improved only in 15m Validation and worsened in the other three cells. Counts range from 5 to 30 B closed trades per cell. Retest-A worsened both failures in all four cells. Removed V1 signals had worse expectancy than kept signals in 5/8 variant/split comparisons; the three exceptions included both 15m Retest-A cells. Kept V1 samples ranged from 2 to 52, and several very small B survivor groups remained net-negative. Filtering fewer trades is not sufficient evidence of an edge.

### Cost stress, gaps, buckets, and concentration

At 1.5x costs, all eight Retest interval/split/variant cells have negative expectancy; all are negative at 2.0x. The 30m Development A cell changes from +₩149.51 gross expectancy to −₩22.43 net. The 30m Validation B cell's +₩0.26 baseline expectancy falls to −₩561.97 at 1.5x. The small one-at-a-time Development neighborhood (tolerance 0.20% / 0.40%, expiry two bars) did not produce a stable cross-interval result: all 15m neighbors remained net-negative, while isolated 30m two-bar neighbors were positive on small samples and were not substituted for the frozen rules.

Clean-window-only results do not change the rejection: they are net-negative in six of eight cells. A 30m Validation A clean subset is only 21 closed trades at +₩1.04 expectancy; the B clean subset is four trades at +₩377.49, with 100% of positive PnL in the top five trades. These are sparse gap-filtered diagnostics, not alternate candidates. The event files retain gap flags, retest depth/timing, MFE/MAE, time-to-MFE, and time-to-stop for every setup.

Retest-A 15m Development lost across all price buckets; other cells contain sparse and inconsistent price/liquidity cells. A few 30m Dev or Validation buckets are positive but fail across the adjacent split or have only one to six events. All strategy samples are KOSPI; KOSDAQ performance is unavailable. In the only slightly positive all-data cell (RETEST-B 30m Validation), 81.8% of positive PnL came from its top trade and 100% from its top five; its positive contribution came from one KOSPI symbol/day. No result supports a price, liquidity, market, or concentration filter.

### ₩100,000 whole-share portfolio diagnostic

At the existing ₩100K capital / ₩20K order cap / 0.25% risk setting, Retest-A produced 27 / 9 / 9 / 4 fills in 15m Dev / Val and 30m Dev / Val, with portfolio net PnL −₩1,494 / −₩1,374 / −₩1,605 / −₩195, respectively. Retest-B produced 5 / 0 / 1 / 1 fills and −₩211 / ₩0 with no fills / −₩58 / −₩99. Baseline capital utilization ranges from 0% to 2.28%; idle cash remains high. 30K/.50% and 50K/.50% are retained as research-only scenarios and remained negative in nearly every cell; their higher fills did not create an edge. `100K=CONSTRAINED`, not a live recommendation.

### Untouched historical validation and decision

The frozen candidate block is 2026-01-05–2026-04-16 for the frozen 100-symbol cohort. Official KIS sample code documents up to 120 returned minute rows per call and a historical date input, with data bounded by what the service retains (up to one year); it does not guarantee that every requested old symbol/session is available ([KIS minute-chart example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py)). This block had no local parquet partitions, and KIS app credentials were absent from the process environment. No API call was made, `.env` was not read, and no substitute provider was used. Therefore `EXTERNAL_VALIDATION=NOT_AVAILABLE`; the frozen Holdout remains locked.

| Required verdict | Phase 4 result |
|---|---|
| `DATA_QUALITY` | `PARTIAL` — 510 safe-window gaps remain `UNKNOWN` |
| `COHORT_BREADTH` | `LIMITED` — 60/100 metadata cohort frozen; no broad minute evidence |
| `RETEST-A` | `REJECTED` — net-negative all four cells |
| `RETEST-B` | `INSUFFICIENT` — immediate rejection falls, but samples are sparse and net/cost-stress evidence fails |
| `BREAKOUT_FAMILY` | `REJECT` |
| `100K` | `CONSTRAINED` |
| `SHADOW_NEXT_SESSION` | `NO` — `NOT_PROMOTED` |

- **Did retest/acceptance reduce false breakouts?** `INSUFFICIENT`: B lowered the immediate-rejection metric in all four used-data cells, but had 5–30 closed trades per cell and did not improve no-follow-through consistently; A worsened the rejection rate.
- **Did reducing those failures improve cost-adjusted expectancy and PF?** `NO`. Every A cell was net-negative; B was negative in three cells and effectively flat on five trades in the fourth. Every 1.5x-cost expectancy was negative.
- **Does it hold on untouched evidence?** `NOT_AVAILABLE`.
- **Is the strategy executable with ₩100K whole-share limits?** `CONSTRAINED`; executions occurred, but fills were sparse and net results were negative.
- **Promotion:** `BREAKOUT_FAMILY_REJECTED`; `SHADOW=NOT_PROMOTED`; `ALPHA=UNPROVEN`; `LIVE=DISABLED`; `PAPER=OUT_OF_SCOPE`.
- Next step: do not run a Shadow for this family and do not open the locked Holdout. If a later research phase is authorized, consider only one newly preregistered family at a time; names for future consideration are relative-strength continuation, VWAP reclaim, or opening momentum with acceptance. None is implemented or evaluated in Phase 4.

Phase 4 machine artifacts are intentionally ignored under `runtime/research/phase4/`: `data-quality-audit.json`, `cohort-manifest.json`, `retest-preregistration.json`, `retest-anatomy-{15m,30m}.json`, `retest-events-{retesta,retestb}-{15m,30m}.json`, `retest-neighborhood.json`, `external-cache-audit.json`, `external-validation.json`, `100k-feasibility.json`, and `phase4-artifact-index.json`. The index records SHA-256 for all 13 artifacts plus source dataset, selected partitions, cohort symbols/master, strategy config, costs, periods, freeze commit, and Holdout counters. Index SHA-256: `807ac406e088414c6b5826be036d70b611d72034f77d8d9cdd56976645516779`. Runtime data was not committed.

---

## Phase 5: Relative-Strength Continuation and Post-Study Acquisition Reconciliation

### Research context and problem formulation

Following the definitive rejection of the Breakout family in Phase 4 (`BREAKOUT_FAMILY = REJECTED`), Phase 5 evaluated an entirely distinct, preregistered strategy family: **Relative-Strength Continuation**. The 60-symbol cohort was the acquisition target; it was not the breadth of the strategy evidence.

The core hypothesis examines:
> *"Does selecting intraday leaders that consistently outperform their peers across the market yield a statistically significant, cost-adjusted continuation edge after realistic KRX transaction costs and market frictions?"*

- **Strategy freeze & non-contamination:** The Breakout family remained completely untouched with zero threshold or filter adjustments.
- **Strict Holdout lock:** The 23-session Fresh Holdout period (`2026-07-28` to `2026-08-28`) remained strictly `LOCKED_NOT_EVALUATED`. Exactly 0 partitions, 0 features, and 0 signals from the holdout window were accessed.
- **Cost modeling:** Baseline transaction costs mirror previous phases: 0.015% broker fee, 0.200% securities transaction tax (sell-side), and 15 bps slippage (combined round-trip friction ≈ 0.527%). Stress scenarios at 1.5x (≈ 0.79%) and 2.0x (≈ 1.05%) were evaluated.
- **Primary & Secondary intervals:** 15m was designated as the primary decision interval; 30m was analyzed as secondary.

### Data acquisition and expanded cohort breadth

The frozen target cohort has 60 symbols (30 KOSPI and 30 KOSDAQ). The strategy study used the data available at its run time:
- **Study input:** 33 symbols (30 KOSPI + 3 KOSDAQ), 67 safe sessions (`2026-04-17` to `2026-07-27`), 2,209 loaded partitions, and 807,223 rows. It did not use the full 60-symbol target.
- **Reconstructed study symbols:** `001440, 001450, 003490, 004020, 005940, 006360, 006800, 009830, 010140, 011200, 015760, 018880, 022100, 024110, 028670, 029780, 032640, 034220, 035720, 036460, 047040, 082740, 088350, 088980, 138930, 175330, 199430, 208860, 316140, 323410, 336260, 377300, 457370`.
- **Study dataset hash:** `a58a63d77a3110b74927b64820c26bd8b969aba6e74d40400534f917e6f91b0f`. The original run did not persist an input partition manifest/hash; this value is reconstructed from the 2,209 safe-period partitions that existed by the `expanded-dq.json` load-completion timestamp, not a contemporaneously frozen input digest.
- **Post-study acquisition:** The acquisition target remained 60. Current safe-period files cover 36 complete symbols (67/67 partitions each), one partial symbol (`215000`, 30/67), and 23 symbols with no partitions. The three complete post-study additions are `222080`, `101680`, and `010240`; `215000` is partial. These later files were not blended into the original 33-symbol results.

| Post-study symbol | Market | Safe-period partitions | First partition mtime (KST) |
|---|---|---:|---|
| `222080` | KOSDAQ | 67 | 2026-09-28 23:09:18.470335 |
| `101680` | KOSDAQ | 67 | 2026-09-28 23:13:46.463411 |
| `010240` | KOSDAQ | 67 | 2026-09-28 23:15:54.026185 |
| `215000` | KOSDAQ | 30 | 2026-09-28 23:19:59.668464 |

- **Current safe-period DQ:** 2,442 readable and sidecar-verified partitions, 861,756 rows, 0 duplicate timestamps, 0 out-of-order rows, 0 invalid OHLC rows, 0 negative-volume rows, and 0 hash/row-count mismatches. There are 66,204 missing minute slots across 768 present partitions (380 start-labelled minutes per session); their cause remains unknown, and no bars were synthesized. The 23 not-started symbols are reported separately from within-partition gaps.
- **Acquisition state:** The historical manifest still says `RUNNING`, but no Phase 5 worker was present. Its last write predates the final 30 `215000` partitions. Reconciliation classifies the acquisition as `PARTIAL_INTERRUPTED`; the manifest was preserved unchanged.

### Cross-sectional feature anatomy and persistence decay

To test the foundational premise before strategy simulation, cross-sectional relative strength was evaluated across 50,271 bar observations (1,668 timestamps, 33 symbols) on the 15m timeframe:

#### 15m Forward return by relative strength decile/quintile
| RS Rank Bucket | Sample Size | Mean Fwd 1-bar Ret | Mean Fwd 2-bar Ret | Mean Fwd 4-bar Ret | 1-bar Win Rate | 10-bar MFE / MAE |
|---|---:|---:|---:|---:|---:|
| 0–20% (Lagging) | 9,073 | −0.0096% | −0.0084% | −0.0179% | 43.55% | +2.27% / −2.16% |
| 20–40% | 9,913 | −0.0060% | −0.0243% | −0.0515% | 41.82% | +1.77% / −1.72% |
| 40–60% (Median) | 9,989 | −0.0162% | −0.0228% | −0.0151% | 40.97% | +1.70% / −1.63% |
| 60–80% | 9,913 | −0.0248% | −0.0301% | −0.0375% | 39.95% | +1.77% / −1.67% |
| 80–90% | 4,957 | −0.0193% | −0.0282% | −0.0661% | 40.89% | +1.96% / −1.82% |
| **90–100% (Leading)** | 6,426 | **−0.0694%** | **−0.0884%** | **−0.1397%** | **40.66%** | +2.48% / −2.42% |

- **Monotonicity:** `FALSE`. Top-decile relative-strength leaders exhibit **worse** forward returns and lower win rates than bottom-quintile laggards.
- **Mean-reversion drag:** In the KRX intraday market, short-term leadership over 45–90 minutes is followed by rapid profit-taking and mean-reversion rather than continuation.

#### Persistence breakdown (3-bar consecutive leadership)
| Persistence Condition | Sample Size | Mean Fwd 1-bar Ret | 1-bar Win Rate | Mean Fwd 4-bar Ret |
|---|---:|---:|---:|---:|
| 0 of 3 bars in top decile | 29,508 | −0.0108% | 41.39% | −0.0327% |
| 1 of 3 bars in top decile | 11,135 | −0.0348% | 41.31% | −0.0467% |
| 2 of 3 bars in top decile | 6,405 | −0.0264% | 41.61% | −0.0624% |
| **3 of 3 bars in top decile** | 3,223 | **−0.0689%** | **40.71%** | **−0.1664%** |

- **Persistence advantage over spike:** `FALSE`. Sustained leaders (3/3 bars) suffer even deeper forward decay (−0.0689% 1-bar, −0.1664% 4-bar) than transient spikes.

### Preregistered strategy performance: RS-A vs RS-B

Two distinct candidate formulations were preregistered before backtesting:
1. **RS-A (Persistent Leader):** Cross-sectional relative strength rank ≥ 80th percentile, rank persistence ≥ 2 bars, 10-bar max holding or stop-first exit.
2. **RS-B (Persistent Leader + Reacceleration):** Same rank and persistence criteria plus positive rank acceleration (RS velocity > 0).

#### Development Period (2026-04-17 to 2026-06-30, 50 sessions)
| Strategy | Closed Trades | Win Rate | Gross Expectancy | Net Expectancy | Profit Factor |
|---|---:|---:|---:|---:|---:|
| **RS-A** | 691 | 24.75% | −0.296% | −0.823% | 0.436 |
| **RS-B** | 453 | 26.93% | −0.053% | −0.581% | 0.570 |

#### Secondary Diagnostic Period (2026-07-01 to 2026-07-27, 17 sessions)
| Strategy | Closed Trades | Win Rate | Gross Expectancy | Net Expectancy | Profit Factor |
|---|---:|---:|---:|---:|---:|
| **RS-A** | 219 | 26.03% | −0.058% | −0.587% | 0.534 |
| **RS-B** | 173 | 28.32% | −0.215% | −0.743% | 0.472 |

**Key observations:**
- **Gross expectancy is negative across all cells** before transaction costs.
- Adding reacceleration (RS-B) narrowed losses in Development but worsened performance in the Secondary diagnostic period.
- Neither candidate demonstrated positive gross expectancy; under Rule 71 and Rule 100, tuning exits or filters on a gross-negative foundation is strictly prohibited.

### Cost stress analysis

| Scenario | Multiplier | RS-A Net Expectancy (PF) | RS-B Net Expectancy (PF) |
|---|---:|---:|---:|
| Baseline | 1.0x | −0.823% (0.436) | −0.581% (0.570) |
| Moderate Friction | 1.5x | −1.086% (0.346) | −0.845% (0.453) |
| Severe Friction | 2.0x | −1.348% (0.279) | −1.107% (0.365) |

Under realistic friction escalation, the net drag compounds rapidly, driving Profit Factor below 0.35.

### Post-study fixed-rule breadth sensitivity

Because 36 complete symbols passed safe-period partition and bar-structure checks, one fixed-rule sensitivity replay used the original preregistered RS-A/RS-B configuration on the same Development and Secondary Diagnostic windows. It included the three fully acquired additions (`222080`, `101680`, `010240`) and excluded partial `215000`. This is labeled `POST_STUDY_BREADTH_SENSITIVITY`; it is not promotion evidence and no thresholds, stops, exits, scanner rules, or parameters were changed.

| Strategy | Window | Closed Trades | Gross Expectancy | Net Expectancy | Profit Factor |
|---|---|---:|---:|---:|---:|
| RS-A | Development | 698 | −0.168% | −0.696% | 0.506 |
| RS-B | Development | 453 | −0.104% | −0.632% | 0.536 |
| RS-A | Secondary Diagnostic | 212 | −0.066% | −0.595% | 0.534 |
| RS-B | Secondary Diagnostic | 181 | −0.273% | −0.800% | 0.443 |

Gross expectancy remained negative in all four fixed-rule cells. The original rejection conclusion does not change; no further sensitivity or optimization was run.

### ₩100,000 whole-share portfolio replay

A realistic whole-share replay was executed using the project's standard retail micro-capital constraints (₩100,000 initial capital, ₩20,000 order cap, 0.25% equity risk per trade, max 2 concurrent positions):
- **Total Fills / Trades:** 193
- **Win Rate:** 18.13%
- **Total Net PnL:** −₩14,443.00 (−14.44% total return)
- **Profit Factor:** 0.462
- **Maximum Drawdown:** 14.47%
- **Conclusion:** While the strategy is technically executable with whole-share lot sizes, it exhibits steady capital erosion. `100K = EXECUTABLE_NEGATIVE`.

### Untouched external validation and holdout integrity

- **External Block (2026-01-05 to 2026-04-16):** With both RS-A and RS-B failing primary viability gates on gross expectancy, the external historical block was intentionally kept untouched (`EXTERNAL_VALIDATION = NOT_AVAILABLE`) to prevent data snooping and conserve API quota (Rule 59, Rule 71).
- **Fresh Holdout (2026-07-28 to 2026-08-28, 23 sessions):** Maintained strictly as `LOCKED_NOT_EVALUATED` (0 partitions opened, 0 features computed).

### Final Status Matrix

| Gate / Metric | Phase 5 Verdict | Operational Meaning |
|---|---|---|
| `PHASE5_CODE` | `COMPLETE` | Existing Phase 5 implementation is preserved |
| `PHASE5_STRATEGY_RESEARCH` | `COMPLETE` | Preregistered study and one permitted fixed-rule sensitivity are recorded |
| `PHASE5_DATA_ACQUISITION` | `PARTIAL_INTERRUPTED` | 36 complete, 1 partial, 23 not started of 60 target symbols |
| `PHASE5_ARTIFACT_INTEGRITY` | `RECONCILED` | Original index preserved; final index records the later manifest and current safe-period DQ |
| `DATA_QUALITY` | `PARTIAL` | Current safe period: 2,442 partitions, 861,756 rows; 66,204 unresolved missing minute slots |
| `COHORT_BREADTH` | `33_STUDIED / 37_CURRENT_WITH_DATA` | Study: 33; current: 36 complete + 1 partial; target: 60 |
| `RS-A` | `REJECTED` | Gross-negative (−0.296%), Net-negative (−0.823%) |
| `RS-B` | `REJECTED` | Gross-negative (−0.053%), Net-negative (−0.581%) |
| `RELATIVE_STRENGTH_FAMILY`| `REJECTED` | Cross-sectional continuation lacks edge in KRX |
| `100K_PORTFOLIO` | `EXECUTABLE_NEGATIVE` | 193 fills, −14.44% PnL, steady capital erosion |
| `SHADOW_NEXT_SESSION` | `NO` (`NOT_PROMOTED`) | Promotion criteria failed |
| `ALPHA` | `UNPROVEN` | No demonstrable market edge |
| `LIVE` | `DISABLED` | Live trading prohibited |
| `PAPER` | `OUT_OF_SCOPE` | Paper trading withheld |

The original 13 Phase 5 artifacts remain under ignored `runtime/research/phase5/`. The original `phase5-artifact-index.json` is preserved: 12 entries still match, while its manifest entry records the earlier 7,776-byte / `21931d2909a31e6aae4321ec955dbae071ba1724fbf7742a025b6c6c1d5e2634` state. The current manifest is 8,435 bytes / `0d744dd815c9eb44611918c10518825d98fdfd5f5631ac3a50c6cd9b8f2daa9f` because acquisition continued after the index was generated. `phase5-current-dq.json`, `phase5-reconciliation-final.json`, `post-study-breadth-sensitivity.json`, and `phase5-artifact-index-final.json` record the reconciled local state; runtime data remains ignored and is not committed.

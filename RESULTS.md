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

## Phase 7 — VWAP reclaim / acceptance

### Final status and scope

Phase 7 tested whether a completed-bar reclaim of session VWAP, optionally followed by acceptance, has positive forward returns. This was a new session-VWAP hypothesis; it did not revive or retune the rejected Breakout family.

**Final verdict:** `VWAP_RECLAIM_ANATOMY=FAIL` on the primary 15m Development interval. The predeclared anatomy gate passed only 2 of 5 checks (enough events and a 10 percentage-point acceptance failure reduction). Gross four-bar opportunity, improvement over the matched baseline, and positive direction in the high-confidence subset all failed. The 30m secondary interval within Development was `WEAK` (3/5) and is descriptive support only. No VWAP-A or VWAP-B strategy variant was created.

| Research question | Phase 7 result | Evidence summary |
|---|---|---|
| Q1. Do reclaims beat comparable baseline bars? | **NO** | 15m four-bar mean −0.0471% vs. matched −0.0157%; event-minus-control −0.0314 pp. |
| Q2. Does first reclaim beat repeats? | **NO** | First −0.0634%; second +0.0268%; third+ −0.1202%. |
| Q3. Does time below VWAP matter? | **WEAK** | Non-monotone: 1 bar −0.0690%, 2 bars +0.0611%, 3–4 −0.1545%, 5+ −0.0120%. |
| Q4. Does acceptance reduce failure? | **YES** | Acceptance A four-bar recross failure 47.62% vs. unrestricted 62.23% (−14.61 pp). |
| Q5. Does acceptance improve gross expectancy, beyond reducing trades? | **YES, descriptively** | Acceptance A next-executable-bar four-bar mean +0.0365% vs. −0.0471% unrestricted (+0.0835 pp); only 1,470 accepted events had that full outcome. Within the same accepted cohort, waiting for confirmation reduced its immediate-entry mean by 0.2414 pp. This is not a viable strategy result. |
| Q6. Does the effect survive high-confidence data? | **NO** | High-confidence 15m events averaged −0.0674% over four bars. |
| Q7. Is VWAP-A positive before costs? | **NO** | No A variant passed the Development gate or was implemented/backtested. |
| Q8. Is either variant positive after costs? | **NO** | No candidate; descriptive event-hold mean was −0.5766% after base modeled costs. |
| Q9. Does Secondary preserve direction? | **NOT_RUN** | No frozen Development survivor qualified for strategy-level Secondary evaluation. |
| Q10. Does untouched external validation pass? | **NOT_AVAILABLE** | No preregistered Secondary survivor; external block remained unopened. |
| Q11. Is it feasible in the 100K whole-share portfolio? | **NOT_RUN** | No qualified strategy candidate to replay. |
| Q12. Is there a prospective Shadow candidate? | **NO** | `SHADOW_NEXT_SESSION=NO`; `LIVE=DISABLED`. |

### Repository, acquisition, and dataset snapshot

- Work started on `main` at the expected clean baseline `50314bd13d42caa168857f11f8c118843fa1bd6a`; fetched `origin/main`, which was at the same SHA. The existing 156-test suite passed before implementation.
- The 60-symbol cohort remains **46/60 complete**: 30 KOSPI and 16 KOSDAQ. A bounded read-only KIS historical OHLCV backfill reused 3,138 existing safe partitions and added 36 new ones without changing any existing partition. The cache now has two partial KOSDAQ symbols (`006910`, 61/67 sessions; `049120`, 31/67) and 12 KOSDAQ symbols with no partitions. Complete breadth did not increase.
- The current Phase 7 snapshot has 3,174 safe partitions (3,082 for the 46 complete symbols plus 61 and 31 for the partial symbols), 1,012,302 one-minute rows for complete symbols, 49 Development sessions (2026-04-17–2026-06-30), and 18 Secondary sessions (2026-07-01–2026-07-27). Outcome research uses only the 46 complete symbols; partial symbols contribute to acquisition/DQ accounting, not strategy outcomes.
- Acquisition started with a 1.0-second request interval. After KIS rate limiting, the shared persistent limiter had increased to 4.0 seconds; the resumable collector was interrupted while waiting for its next request slot. Its current Phase 6 manifest records 47 finished symbol results (46 complete skips and a partial `006910` result); the current cache also contains 31 new `049120` sessions that were written before the collector could finalize that symbol. The Phase 7 acquisition reconciliation compares the actual safe-cache partition hashes against the pre-acquisition snapshot.
- This collector writes its status to the Phase 6 runtime path. Its interrupted run replaced the ignored `runtime/research/phase6/phase6-acquisition-manifest.json` and left that new file at `RUNNING`. The unchanged Phase 6 artifact index expects the previous manifest (11,410 bytes, SHA-256 `0a29a04f9c362652f84370b2b327c0918d791143cea1d4b9d7e4bf7e41f51859`); the current file is 11,098 bytes, SHA-256 `2591c05a59f20a8b90d2adff09a581fd552de9fa8e44de265afb1a788c5d0440`. No local copy of the previous bytes was found, so the Phase 6 artifact-integrity state now needs reconciliation. The Phase 6 summary, historical research outcomes, and artifact index were not rewritten; `MEAN_REVERSION_FAMILY=INSUFFICIENT` is unchanged.
- Fresh Holdout (`2026-07-28`–`2026-08-28`) and external block (`2026-01-05`–`2026-04-16`) partitions opened: **0** each. Holdout features, signals, VWAP, outcomes, and DQ were not calculated. The external block was not evaluated because no frozen candidate qualified.

### VWAP formula, session, and point-in-time rules

The cached KIS minute Parquet schema is `timestamp/open/high/low/close/volume`. The retained minute parser also exposes OHLC and `cntg_vol`; neither has actual interval traded value or cumulative traded value. Exact traded-value VWAP could not be calculated. Phase 7 therefore labels every calculation `OHLCV_PROXY` / `PROXY`, never exact transaction VWAP.

For each symbol and KST session, at observed minute `t`:

```text
typical_price[t] = (high[t] + low[t] + close[t]) / 3
VWAP_PROXY[t] = sum(typical_price[i] * observed_volume[i], i <= t)
                / sum(observed_volume[i], i <= t)
```

The accumulator resets for every symbol/session at 09:00 KST. A completed 15m/30m bar uses only observed one-minute bars through that interval’s final minute. There is no forward fill, future volume, or future turnover. Missing one-minute observations are not synthesized; incomplete aggregate bars are omitted. Zero volume contributes zero; VWAP stays null until cumulative volume is positive, and a null VWAP cannot trigger a reclaim.

Data timestamps are `Asia/Seoul`, `bar_start`, for the continuous regular session 09:00–15:19. The 15:20 closing auction is excluded. The same semantics apply to 15m primary and 30m secondary anatomy; no auction records were added.

### Data quality and reliability

Across 3,174 cached symbol-sessions, the session DQ manifest records 1,041,514 observed minute slots of 1,206,120 expected and 164,606 missing slots. No missing cause is inferred: an absence could be provider omission, no trade, halt, session semantics, or retrieval loss. No synthetic minutes were added. Volume sum was 7,375,973,956 units; OHLCV turnover proxy sum was ₩163,047,101,917,067.34 (not provider turnover). Class counts are 1,771 HIGH_CONFIDENCE, 412 PARTIAL, and 991 UNRELIABLE symbol-sessions. These extra partitions are from partial symbols and do not enter the 46-complete-symbol event outcome sample.

Confidence rules were fixed before outcome aggregation: `HIGH_CONFIDENCE` requires all 380 continuous-session minutes observed and positive volume; `PARTIAL` requires at least 361/380 observed and positive volume; otherwise `UNRELIABLE`. Counts were 1,770, 408, and 960 symbol-sessions, respectively. For 15m reclaim outcomes, high-confidence events were n=1,970 with −0.0674% mean four-bar return and 62.08% four-bar failure; partial events n=258 were +0.3165% and unreliable events n=131 were −0.4573%. The partial subset’s positive mean does not replace the negative high-confidence result.

The all-events vs. high-confidence comparison is a required sensitivity, not evidence that the missing slots are no-trade minutes. Because both the source VWAP and liquidity turnover are OHLC-based proxies, no exact-versus-proxy crossing comparison is available.

### Development anatomy — 15m primary

The event dataset has 42,029 completed-bar observations and 2,761 up-reclaim events; 2,359 have complete four-bar outcomes. The event identity is deterministic by symbol/session/reclaim sequence. Down-crosses, first/second/third-plus reclaims, previous/current point-in-time VWAP distances, run length below VWAP, excursion, slope, age, time-of-day, session context, bar quality, liquidity proxy, confidence, acceptance definitions, and 1/2/4/8-bar outcomes are recorded. Forward return uses the next executable bar open after the signal/confirmation and the corresponding completed horizon; no signal or acceptance bar close is treated as a fill. MFE/MAE and their time-to fields are recorded per event.

Across the 2,359 reclaim events with four-bar outcomes:

- Mean forward gross return was −0.0556% at 1 bar, −0.0336% at 2 bars, −0.0471% at 4 bars, and −0.0141% at 8 bars (2,045 observations for the 8-bar horizon). Mean four-bar MFE was +0.9923% and MAE −0.9481%; median time-to-MFE was 2 bars and time-to-MAE was 2 bars. These excursion values overlap across event windows and are not independent trade results.
- Four-bar VWAP recross/failure rate was 62.23%; 1-bar 34.97%, 2-bar 48.66%.
- The same-market, same completed-bar time-bucket, same fixed 15m OHLCV turnover-proxy bucket control (excluding the event symbol-session) had a −0.0157% four-bar mean. Reclaims underperformed it by 0.0314 percentage points; the positive event-minus-control share was 46.08%.
- 30m Development anatomy had 19,694 observations, 1,524 reclaim events, and 1,090 four-bar outcomes. Mean four-bar gross return was −0.0411% vs. matched baseline −0.0599% (+0.0188 pp), but the event return and high-confidence subset (n=926, −0.0341%) remained negative. Its `WEAK` gate (3/5) did not satisfy the primary 15m gate.

#### First/repeated reclaim, time below, depth, and slope

| 15m Development group | Events with 4-bar outcome | Mean 4-bar gross | Failure within 4 bars |
|---|---:|---:|---:|
| First reclaim | 1,264 | −0.0634% | 62.97% |
| Second reclaim | 685 | +0.0268% | 60.44% |
| Third or later | 410 | −0.1202% | 62.93% |
| Below VWAP 1 bar | 945 | −0.0690% | 61.90% |
| Below VWAP 2 bars | 426 | +0.0611% | 62.68% |
| Below VWAP 3–4 bars | 421 | −0.1545% | 62.00% |
| Below VWAP 5+ bars | 567 | −0.0120% | 62.61% |
| Shallow excursion (>−0.25%) | 291 | +0.0270% | 59.45% |
| Moderate (−0.25% to −0.75%) | 711 | +0.0051% | 60.90% |
| Deep (≤−0.75%) | 1,357 | −0.0903% | 63.52% |

The first reclaim did not beat repeats: first-minus-mean(repeated) was −0.0167 pp. “Time below” results are non-monotone and do not support a robust duration gate. Shallow/moderate/deep excursion groups show some ordering in return/failure but all remain too small or negative to define an entry filter; no thresholds were optimized.

| 60m VWAP slope state at reclaim | n | Mean 4-bar gross | Failure within 4 bars |
|---|---:|---:|---:|
| Falling | 712 | −0.0855% | 64.47% |
| Flat | 717 | −0.0534% | 60.81% |
| Rising | 249 | +0.0221% | 53.41% |
| Unavailable | 681 | −0.0255% | 64.61% |

Rising slope was less negative/weakly positive, but n=249 and the rest of the anatomy does not validate it as a gate. It remains descriptive.

#### Acceptance and failure

Acceptance definitions were descriptive: A, next completed close remains above VWAP; B, next low touches VWAP and closes above; C, next close exceeds reclaim close; D, two consecutive closes above VWAP. For 15m, A applied to 1,732 events, with 1,470 full four-bar post-confirmation outcomes. Four-bar failure after A confirmation was 47.62%, versus 62.23% unrestricted (−14.61 pp). The confirmed next-executable-bar gross return was +0.0365%, compared with +0.2779% for those A events measured at immediate event entry; confirmation delayed that cohort’s mean by −0.2414 pp. Against the unrestricted −0.0471% mean, A’s post-confirmation mean is +0.0835 pp better, which is the descriptive basis for Q5=YES. It is still only 3.65 bp gross and does not establish an executable edge.

Other confirmations also reduced some failures, with weak/negative delayed-entry outcomes: B had 55.63% post-confirmation four-bar failure and +0.0525% delayed gross; C 40.00% and +0.0143%; D 39.98% and +0.0292%. The 30m A definition instead had 50.58% failure and −0.0933% delayed gross. Acceptance conditions are not strategy rules, and none received a parameter search.

#### Time, liquidity, market, and stability splits

15m four-bar means by time bucket were: 09:00–09:30 n=309, −0.1798%; 09:30–10:00 n=339, +0.1349%; 10:00–11:00 n=628, −0.0950%; 11:00–12:00 n=399, +0.0721%; 12:00–13:00 n=331, −0.1126%; 13:00–14:00 n=274, −0.1147%; and 14:00–15:00 n=79, −0.0203%. The 4-bar anatomy has no reportable 15:00+ horizon because continuous trading ends at 15:19; closing-auction data is excluded. The positive 09:30 and 11:00 buckets are not stable support for a narrow time window.

Completed 15m bar OHLCV-proxy turnover results were: <₩10M n=2, +0.0330%; ₩10M–₩50M n=10, +0.0752%; ₩50M–₩200M n=180, +0.0433%; ≥₩200M n=2,167, −0.0552%. The thin buckets are too small, and the dominant turnover group is negative.

| Market | n | Mean 4-bar gross | Failure within 4 bars |
|---|---:|---:|---:|
| KOSPI | 2,104 | −0.0193% | 61.55% |
| KOSDAQ | 255 | −0.2765% | 67.84% |

Monthly 15m means were April n=554, +0.0052%; May n=852, −0.0981%; June n=953, −0.0319%. They do not show stable positive direction. Session-direction four-bar means were down −0.0521%, near-flat +0.0552%, and up −0.1159%. Reclaim after early strength followed by VWAP loss (n=945) averaged −0.0257%, vs. first reclaim after morning weakness (n=710) at −0.0821%; neither was positive. Concentration shares are diagnostic sums over overlapping event outcomes, not trade PnL: top event 2.54%, top five 6.88%; top symbol 8.83%, top three symbols 25.59%; top day 6.76%, top five days 26.32% of positive gross contribution.

### Strategy gate, costs, freeze, and final boundaries

The 15m gate criteria were fixed before inspecting returns. Although event count and acceptance failure reduction passed, gross return, matched-baseline improvement, and high-confidence direction failed. Therefore neither proposed family concept was instantiated:

- **VWAP-A:** not created. The anatomy did not justify setting a time-below, VWAP-age, confidence/liquidity, holding, or structural-exit rule.
- **VWAP-B:** not created. A/B/C/D acceptance descriptions were measured but none met the family gate as an executable candidate.

No strategy-level Development trade list, Secondary strategy diagnostic, strategy preregistration, strategy freeze SHA, external evaluation, or 100K whole-share portfolio replay exists. The costs below are **descriptive event-hold calculations, not strategy backtest results**. Cost assumptions: 0.015% broker fee per side, 0.20% sell tax, 15 bps slippage per side; approximate round trip 53 bps. On 15m four-bar events, mean gross −0.0471% became net −0.5766% at 1.0× costs, −0.8411% at 1.5×, and −1.1055% at 2.0×. Costs were not adjusted to rescue a gross-negative hypothesis.

The external block remains unopened because there is no frozen Secondary survivor; the Fresh Holdout remains `LOCKED_NOT_EVALUATED`; both have zero opened partitions. `VWAP_RECLAIM_FAMILY=REJECT`, `VWAP_A=NOT_CREATED`, `VWAP_B=NOT_CREATED`, `SHADOW_NEXT_SESSION=NO`, `ALPHA=UNPROVEN`, `PAPER=OUT_OF_SCOPE`, and `LIVE=DISABLED`. Phase 6 remains `MEAN_REVERSION_FAMILY=INSUFFICIENT`; no Phase 2–6 verdict was rewritten.

### Artifacts, reproducibility, and validation

The reproducible entry point is `./.venv/bin/python -m krx_trader.research.phase7_vwap`. It writes ignored local artifacts under `runtime/research/phase7/`, with `phase7-artifact-index.json` hashing the generated outputs. The artifact set includes acquisition and dataset manifests, DQ, formula audit, complete 15m/30m observation and event JSONL streams, anatomy and reclaim-failure reports, time/liquidity/market splits, hypothesis summaries, cost stress, and explicit not-run records for Secondary, external validation, and 100K feasibility. `phase7-preregistration.json` was not created because there is no qualified candidate. The acquisition manifest records 36 newly available safe partitions, the 4-second shared limiter, the incomplete Phase 6 collector state, and the Phase 6 indexed-hash mismatch. Each artifact records its applicable source SHA, research-code SHA, dataset SHA, partition-index SHA, cohort SHA, formula/source class, periods, timestamp convention, and cost assumptions. Runtime artifacts are ignored and not part of the public Git commit.

Unit coverage checks hand-computable proxy cumulative math, explicit proxy labeling, session reset, zero volume, missing observations, future-price/future-volume/other-symbol isolation, cross events and sequence, acceptance and failure, next-executable-bar timing, confidence boundaries, 30m completeness, matched controls, gates, partition-delta reconciliation, Holdout/external guards, and deterministic artifacts. Phase 7 retained the original 156 tests and added targeted Phase 7 coverage; final regression evidence and commit/push identity are recorded in the delivery report.

## Phase 8 — Opening gap / first-hour information shock

### Status and Phase 6 reconciliation

Phase 8 completed a Development-only opening-gap anatomy study. Verdicts are `OPENING_GAP_ANATOMY=FAIL`, `OPENING_GAP_FAMILY=REJECT`, `GAP_A=NOT_CREATED`, `GAP_B=NOT_CREATED`, `SWING_SIGNAL=NONE`, and `SHADOW_NEXT_SESSION=NO`. Alpha remains `UNPROVEN`; Paper remains `OUT_OF_SCOPE`; Live and Private API remain disabled.

The Phase 6 artifact index expects the old `phase6-acquisition-manifest.json` at **11,410 bytes**, SHA-256 `0a29a04f9c362652f84370b2b327c0918d791143cea1d4b9d7e4bf7e41f51859`. The current manifest is **11,098 bytes**, SHA-256 `2591c05a59f20a8b90d2adff09a581fd552de9fa8e44de265afb1a788c5d0440`; it does not match the index. No copy matching the indexed hash was found, so the original bytes are unavailable and no restoration is claimed. Tracked source shows the Phase 7 acquisition path can call `run_phase6_acquisition()` → `run_phase5_backfill()` and write the Phase 6 path, but the exact top-level invocation that caused this mutation is not recorded. The Phase 6 summary digest remains `91339542969317a9226df145614d4250a4471d7d894fbb870ba44d40b32c3474`; the historical Phase 6 index and summary/verdict were not rewritten. Integrity is `DEGRADED_RECONCILED`, recorded in the Phase 8 reconciliation artifact.

### Acquisition isolation, coverage, and data quality

The Phase 5 backfill now accepts an explicit status output path and artifact name, request budget, and injected read-only client. Phase 8 passes its own `runtime/research/phase8/phase8-acquisition-manifest.json` path. The default Phase 8 acquisition is bounded to six new requests, honors a minimum four-second interval, and skips valid cached partitions. It does not read or write the shared daily cache; only safe-period minute partitions, the Phase 8 manifest, and shared persistent limiter state are in scope. A regression fixture snapshots Phase 5, Phase 6, and Phase 7 manifest hashes, runs Phase 8 acquisition, and verifies all three hashes are unchanged.

The safe 67-session acquisition now has **47 complete, 1 partial, and 12 not acquired** of 60 target symbols (47 complete = 30 KOSPI + 17 KOSDAQ). Six new read-only KIS requests were attempted; acquisition remains resumable and did not wait for complete cohort coverage. For the 49-session Development window, 2,334 of 2,940 symbol-session minute partitions were verified present, 606 were absent, and no bars were synthesized. The observed data contains 769,617 minute bars and 117,303 absent expected minute slots within present partitions; causes remain `UNKNOWN` rather than being labeled as no-trade or halt intervals. Eighteen symbols had safe Development daily rows.

There are 2,244 valid opening observations and 1,051 with absolute gap at least 1%. Across all 2,244 rows, 817 prior closes came from the KIS daily cache and 1,427 from last-continuous-minute proxies (15:19 where present); within the meaningful 1,051, the source split is 379 daily closes and 672 proxies. The proxy is the previous safe session's last observed continuous-minute close; it is kept separate because the daily close may include the closing auction while the minute cache stops at 15:19.

The current KIS adapter requests `FID_ORG_ADJ_PRC=1`, the unadjusted/raw-price setting shown in the [official KIS Open Trading API sample](https://github.com/koreainvestment/open-trading-api/blob/main/legacy/Sample01/kis_domstk.py). Older daily cache sidecars do not persist that parameter, so its semantics are inferred from the current adapter source, not proven independently for every historical row. Each event records its prior-close source. Candidate/high-confidence sensitivity requires complete minute observations, a raw daily-close reference, and a non-suspicious gap.

No corporate-action calendar was available. Absolute gaps of at least 20% are flagged `UNVERIFIED_SUSPICIOUS_PRICE_JUMP` and excluded from candidates, high-confidence sensitivity, and longer-horizon outcomes. There were **92** such meaningful gaps. They also fall inside the deliberately broad ±28.5% approximate price-limit band, but applicable historical limits could not be determined exactly; the artifact says `NEAR_APPROX_30_PERCENT_BAND`, not that a legal limit event was verified. Extreme raw-price changes, including apparent multi-fold jumps, are not treated as alpha. A source-convention mismatch or unobserved corporate action below the 20% screen remains possible.

The Phase 8 analysis opened zero Fresh Holdout price-data partitions (2026-07-28–08-28) and zero external-block partitions (2026-01-05–04-16). **A strict metadata-isolation incident occurred:** during an earlier broad `rg` search over local data/runtime paths while tracing the acquisition source, tool output surfaced date/timestamp metadata from one or more holdout `.metadata.json` sidecars dated 2026-08-28. The exact sidecar count was not recorded, and the complete sidecar contents were not retained. No holdout OHLCV/daily price rows, derived features, signals, PnL, DQ metrics, or index context were used, and no holdout outcomes were evaluated. The zero-partition count refers to price-data payloads and must not be read as proof that no holdout metadata was encountered. This limitation is recorded in `runtime/research/phase8/phase8-integrity-note.json`; complete holdout metadata isolation cannot be claimed. The external block remained unopened. Secondary (2026-07-01–07-27) was not opened because no Development candidate survived.

### Gap, fill, range, and first-hour anatomy

For each session the opening gap is `(current 09:00 minute-bar open / latest safe prior close − 1) × 100`. The prior close is the most recent safe daily close when its date is at least as recent as cached prior-minute data; otherwise it is a labeled previous-session continuous-minute proxy. Only sessions from the 2026-04-17–06-30 Development split are analyzed. Missing exact 09:00 opens (43) and missing safe prior closes (47) are excluded from event construction.

The fixed coarse buckets below include valid observations with less than 1% gaps as comparison context. `10:00 mean` is the raw open-to-10:00 return, not direction-aligned; a negative value means the price fell. Counts in the last column are all valid bucket observations, while checkpoint means use the stated number with an observed 10:00 mark.

| Opening-gap bucket | Events | Suspicious ≥20% | 10:00 observations | Mean open→10:00 |
|---|---:|---:|---:|---:|
| ≤ −5% | 32 | 0 | 30 | +1.980% |
| −5% to −3% | 56 | 0 | 54 | −0.811% |
| −3% to −1% | 279 | 0 | 270 | −0.822% |
| −1% to 0% | 418 | 0 | 388 | −0.780% |
| 0 to +1% | 775 | 0 | 684 | −0.740% |
| +1% to +3% | 416 | 0 | 391 | −0.782% |
| +3% to +5% | 113 | 0 | 106 | −0.437% |
| ≥ +5% | 155 | 92 | 99 | −0.624% |

The coarse magnitude curve is not monotone, and the most negative-gap bucket is small with zero high-confidence events. For meaningful gaps with observed marks, gap-up events averaged −0.695% open-to-10:00 (fade), while gap-down events averaged −0.582% (continued downside, descriptive only in this long-only project). The combined direction is therefore `MIXED`; it does not justify either long candidate.

| Gap direction | Full prior-close touch by 15m | 30m | 60m | Session |
|---|---:|---:|---:|---:|
| Gap up | 65.3% (693/1,062 known) | 71.2% (752/1,056) | 75.2% (794/1,056) | 83.8% (877/1,047) |
| Gap down | 56.5% (406/719) | 60.2% (432/718) | 63.0% (453/719) | 73.9% (512/693) |

The partial-fill fraction is not clipped: it can exceed 100% after price crosses through the prior close, and can be negative if price extends away from it. The 30-minute fill-vs-no-fill comparison did not predict subsequent gap reversal: its direction-aligned fade delta was −0.176 percentage points, so Q4 is `NO`.

Fixed opening ranges require every minute in the interval; they are descriptive and generated no breakout trades:

| Gap side / range | Complete ranges | Mean width | Median width | Mean close position |
|---|---:|---:|---:|---:|
| Up / 15m | 941 | 3.59% | 3.08% | 0.375 |
| Up / 30m | 898 | 3.99% | 3.57% | 0.415 |
| Down / 15m | 647 | 3.40% | 3.00% | 0.355 |
| Down / 30m | 630 | 3.82% | 3.40% | 0.392 |

The completed first-hour states are descriptive. Gap-up events that reversed by 10:00 numbered 368 and averaged −3.006% open-to-10:00; gap-up extensions numbered 187 and averaged +2.213%. For gap-down events, 243 first-hour extensions averaged −2.413%, while 107 reversals averaged +2.976%. These state labels use the completed first hour and therefore describe the path through 10:00; they are not evidence that a 10:00 signal predicts its own contemporaneous return. For Q3's forward test, the next executable bar after 10:00 to the continuous-session final mark was compared: 275 accepted events underperformed unaccepted events by 0.461 percentage points direction-aligned, so Q3 is `NO`.

### Market, price, liquidity, time stability, and costs

Among meaningful gap events, KOSPI had 672 observations and a −0.651% mean open-to-10:00 return (672 checkpoints); KOSDAQ had 379 observations and −0.657% (278 checkpoints). At continuous-session final, the means were −0.465% for KOSPI and −0.799% for KOSDAQ. These results do not show a repeatable positive direction on either market.

The coarse open-price means at 10:00 were: below ₩10K, +0.233% (129 observed marks; 180 events); ₩10K–₩30K, −0.722% (389/426); ₩30K–₩50K, −0.803% (313/322); and at least ₩50K, −0.992% (119/123). The below-₩10K bucket is positive but too small and not supported by the wider anatomy. Liquidity is a trailing close-times-volume proxy rather than verified traded value: <₩50M n=2 (+1.587%), ₩50M–₩250M n=102 (−0.538%), ₩250M–₩1B n=71 (−1.238%), ≥₩1B n=177 (−0.749%), and unknown n=699 (−0.608%); small and unknown buckets are not interpretable as an edge.

Mean open-to-10:00 returns were −0.387% in April (n=144), −1.042% in May (n=401), and −0.408% in June (n=506); continuous-final means were −0.029%, −0.959%, and −0.449%. Direction was not a stable positive result across months. Among positive event returns, overlapping event contribution was concentrated as follows: at 10:00 top event 2.41%, top five events 9.10%, top symbol 7.39%, top three symbols 19.76%, top day 7.06%, and top five days 29.58%; at continuous final, corresponding shares were 2.20%, 8.57%, 8.47%, 20.61%, 9.36%, and 33.33%. These are sums over descriptive overlapping events, not portfolio PnL.

For meaningful gaps, matched controls required same session, market, coarse opening-price bucket, and trailing-liquidity bucket, with absolute gap below 1%. There were 745 matches: gap events averaged −0.729% to 10:00 versus −0.610% for controls, a −0.119 percentage-point difference. This descriptive matching is not causal, but it provides no evidence that the gap event adds a positive return over the same-time baseline.

The assumed cost model remains unchanged: fee 0.015% per side, sell tax 0.20%, and slippage 15 bps per side, for 0.53% round-trip. Meaningful events averaged −0.653% open-to-10:00 (950 available marks), −0.652% to 11:00 (929), −0.641% to 14:00 (906), and −0.586% to continuous final (1,051). At 10:00 mean net was −1.183% at 1.0× cost, −1.448% at 1.5×, and −1.713% at 2.0×. The clean 61-event subset averaged −1.484% at 10:00 and −1.682% at continuous final. There is no positive candidate for which a positive break-even friction can be reported; the observed gross results are negative before costs.

### Candidate gates, longer horizons, and final answers

GAP-A diagnostics used gap-up ≥1%, a completed 30-minute close above both open and prior close, next 09:30 bar open entry, and continuous-session final exit. Only 14 events met the descriptive condition; gross mean was −0.858%, net at assumed costs −1.388%, and concentration/monthly/high-confidence gates failed. GAP-B diagnostics used gap-down ≤−1%, a completed first-hour close above open, next 10:00 bar open entry, and the same non-optimized final exit. Only 6 events qualified; gross mean was −0.192% and net −0.722%. Both remained `NOT_CREATED`; their samples are below 30 and fail the required positive gross, positive net, PF >1, high-confidence, stability, and concentration gates. No entry rule was promoted, and no same-bar entry was used.

Secondary is `NOT_RUN`; no preregistration or freeze commit was created. External validation is `NOT_AVAILABLE` because no candidate survived Secondary, and its 2026-01-05–04-16 data stayed unopened. The Fresh Holdout remains locked and unevaluated, with the incidental sidecar-metadata exposure documented above. 100K feasibility is `NOT_RUN`; no execution system, Paper, Private API, or Live behavior was built or enabled.

The separate swing description excluded all 92 suspicious opening gaps and additional raw daily jumps (8 next-session and 16 three-session outcomes). Of the remaining events, 274 next-session outcomes averaged −1.688% and 253 three-session outcomes averaged −4.114%; the stricter high-confidence subsets (60 and 57 outcomes) averaged −1.609% and −4.415%. These overlapping open-to-daily-close observations do not support a separate swing study; `SWING_SIGNAL=NONE`.

| Question | Phase 8 answer |
|---|---|
| Q1. Continuation or reversal? | **MIXED** — gap-up mean faded; gap-down mean continued lower. |
| Q2. Stable gap-magnitude relationship? | **NO** — coarse curve is not monotone and clean support is sparse. |
| Q3. Does first-hour acceptance improve forward return? | **NO** — accepted-vs-unaccepted direction-aligned delta −0.461 pp. |
| Q4. Does early gap fill predict reversal? | **NO** — 30m fill-vs-no-fill fade delta −0.176 pp. |
| Q5. Is gross move large enough versus modeled costs? | **NO** — overall gross outcomes are negative; assumed friction is 0.53%. |
| Q6. Does the result survive high-confidence data? | **NO** — 61-event clean daily-close subset is negative. |
| Q7. Does GAP-A or GAP-B qualify on Development? | **NO**. |
| Q8. Does Secondary preserve it? | **NOT_RUN**. |
| Q9. Does external validation pass? | **NOT_AVAILABLE**; block unopened. |
| Q10. Is 100K execution feasible? | **NOT_RUN**. |
| Q11. Evidence for separate 1–3 day swing research? | **NO** — descriptive outcomes are negative; `SWING_SIGNAL=NONE`. |
| Q12. Is a future Shadow candidate ready? | **NO**. |

### Artifacts, source identity, and verification

The reproducible command is `uv run --locked python -m krx_trader.research.phase8_opening_gap`; bounded safe acquisition is explicitly opt-in with `--acquire --max-new-requests 6`. The complete Phase 8 artifacts are local and ignored under `runtime/research/phase8/`, including the acquisition and dataset manifests, DQ, event stream, buckets, gap fills, opening ranges, first-hour states, splits, monthly/concentration anatomy, cost stress, swing description, not-run gates, reconciliation, holdout-integrity note, and artifact index. The dataset manifest records the source Git SHA, Phase 8 research-code SHA-256, dataset and partition-index SHA-256, cohort and strategy-config SHA-256, provider, periods, price semantics, action treatment, and assumed costs. Runtime evidence is not added to the public repository.

The Phase 8 tests cover prior-session close, exact session open, gap and bucket boundaries, corporate-action flags, gap fill, opening ranges and states, next-bar execution, future-bar isolation, longer-horizon suspicious-price guards, Holdout/external guards, daily-cache access isolation, token non-persistence, and acquisition-manifest isolation. The full regression suite passed **206 tests**; Ruff passed for `src` and `tests`. The original 178-test suite was retained. The final source commit and push are verified in the delivery response.

Next step: close Phase 8 with no candidate promotion. Keep the Fresh Holdout locked; its incidental sidecar metadata exposure is documented, and it must not be used to rescue or retune any hypothesis. Any future opening-gap or swing proposal must be a separately scoped and preregistered phase with new evidence.

---

## Phase 9 — Data Integrity Audit

### Scope and approach

Phase 9 audited KIS daily and minute price semantics, open/close alignment, suspicious gap root causes, timestamp conventions, cache integrity, and protected-data guards. No new strategy was implemented. Source Git SHA: `04ef91b2579e5ab085651d11469b399cdf5d7664`.

### Source inventory

| Source | Endpoint / path | Adjustment | Timestamp |
|---|---|---|---|
| KIS daily | `inquire-daily-itemchartprice` (`FHKST03010100`) | `FID_ORG_ADJ_PRC`: 0=adjusted, 1=raw | Session date (00:00 KST) |
| KIS minute | `inquire-time-dailychartprice` (`FHKST03010230`) | UNKNOWN (no parameter documented) | Bar-start, Asia/Seoul, 09:00–15:19 continuous |
| Daily cache | `data/daily/{symbol}-1d.parquet` | Mixed (historical sidecar metadata) | — |
| Safe daily reconciliation | `runtime/research/phase9/reconciliation/safe-daily-cache` | Separate adjusted and raw fetches | — |
| Minute cache | `data/minute/{symbol}/{session}.parquet` | UNKNOWN | — |

### Daily price adjustment semantics

`DAILY_PRICE_ADJUSTMENT = CONFIGURABLE`. The KIS adapter supports both raw (`FID_ORG_ADJ_PRC=1`) and adjusted (`0`). Phase 9 re-fetched both conventions into a separate reconciliation cache; raw and adjusted hashes are identical for most symbols (no recent corporate actions in Development), but the absolute price scales differ by a factor of ~10x for 2 KOSDAQ symbols (154040, 208860), indicating historical stock splits or par-value changes. Phase 10 uses adjusted only.

### Minute price adjustment semantics

`MINUTE_PRICE_ADJUSTMENT = UNKNOWN`. The KIS minute endpoint does not expose an adjustment parameter in the documented API fields. Phase 9 could not independently verify whether minute bars are raw or adjusted.

### Daily/minute open alignment

| Market | n | Exact match % | Max abs diff % |
|---|---|---|---|
| KOSPI | 1,457 | 98.90% | 0.003% |
| KOSDAQ | 834 | 100.00% | 0.000% |

Opens are effectively identical. The tiny KOSPI deviations (<0.003%) are rounding artifacts from the daily-bar decimal representation.

### Daily/minute close alignment

Daily close vs. 15:19 continuous-minute close:

| Market | n | Median abs diff % | P95 % | Max % | >0.5% | >1% | >3% | >5% |
|---|---|---|---|---|---|---|---|---|
| KOSPI | 1,470 | 0.183 | 0.890 | 8.44 | 246 | 26 | 3 | 2 |
| KOSDAQ | 763 | 0.241 | 1.460 | 4.99 | 231 | 74 | 11 | 0 |

Close differences are expected because the daily close includes the 15:20 closing auction while minute bars stop at 15:19. Median differences are small (<0.25%). The >3% tail is sparse (14 events total) and likely reflects auction-volume price moves or thin securities.

### Suspicious gap root cause analysis

94 Development sessions have adjusted-daily-based absolute gap >= 20%. All 94 are classified `ADJUSTMENT_MISMATCH`:

- Symbol 154040 (KOSDAQ): 49 events. Raw daily prices are ~1/10th of adjusted prices (stock split/par-value change). The `ratio_cluster` is 0.1 for all events.
- Symbol 208860 (KOSDAQ): 45 events. Same pattern.

When raw and adjusted price scales are mixed in a gap calculation, the apparent gap reaches 800–900%. The adjusted-only gap is typically 0–3%. No corporate action calendar was available to confirm the exact event, but the consistent 10:1 ratio cluster strongly indicates a historical stock split.

### Timestamp semantics

`TIMESTAMP_SEMANTICS = PASS`. Minute timestamps are Asia/Seoul bar-start labels. The 09:00 record is the first minute of the continuous session. 15m resampling uses bucket-end labels (09:15 = 09:00–09:14 inclusive). No future-bar contamination was detected.

### Unconditional intraday drift sanity

Open→checkpoint returns across all Development symbol/sessions, split by month and market:

| Checkpoint | KOSPI mean | KOSPI n | KOSDAQ mean | KOSDAQ n |
|---|---|---|---|---|
| 09:15 | −0.68% | 1,403 | −0.76% | 353 |
| 09:30 | −0.61% | 1,392 | −0.89% | 288 |
| 10:00 | −0.76% | 1,375 | −1.34% | 240 |
| 11:00 | −0.75% | 1,343 | −1.47% | 191 |
| 14:00 | −0.91% | 1,262 | −1.93% | 140 |
| 15:19 | −0.60% | 1,457 | −1.04% | 743 |

All checkpoint means are negative. May is the weakest month (KOSPI 10:00 mean −1.19%, KOSDAQ −1.77%). June is less negative. The persistent negative drift is consistent with the Phase 8 opening-gap finding that the first-hour mean is negative across gap directions.

### Cache integrity

`CACHE_INTEGRITY = PARTIAL`. Development scope: 60 symbols × 49 sessions = 2,940 expected partitions. Observed: 2,334 valid, 606 absent. Observed rows: 769,617 of 1,117,200 expected. Row-count distribution shows a dominant mode at 380 (full session: 1,356 partitions). No duplicate timestamps, out-of-order rows, invalid OHLC, or negative volume within present partitions. 0 synthetic rows added.

### Phase 8 evidence integrity

`PHASE8_EVIDENCE_INTEGRITY = DEGRADED_BUT_USABLE`. Phase 9 re-ran the Phase 8 gap analysis on adjusted prices and confirmed the same 92+ events. The Phase 8 corrected replay (`DATA_CORRECTED_REPLAY`) verified that the original Phase 8 descriptive definitions were preserved and the corrected data produces consistent results.

### Previous phase manifest immutability

Phase 5, 6, 7, and 8 runtime manifest hashes were snapshotted before Phase 9 work and verified unchanged after. `PREVIOUS_PHASE_MANIFESTS_IMMUTABLE = PASS`.

### Protected holdout

Protected period 2026-07-28 through 2026-08-28: payload reads 0, sidecar reads 0. The Phase 9 reconciliation cache was written to a separate path and does not overwrite existing daily cache files.

### Representative refresh comparison

3 existing minute partitions were re-fetched and compared. All 3 matched the original hash (`PERSISTENT_PROVIDER_SHAPE`), confirming that the KIS endpoint returns consistent data for the same request.

### Final verdicts

| Gate | Phase 9 result |
|---|---|
| `PRICE_SEMANTICS` | `PARTIAL` (daily CONFIGURABLE; minute UNKNOWN) |
| `DAILY_MINUTE_ALIGNMENT` | `PASS` |
| `TIMESTAMP_SEMANTICS` | `PASS` |
| `CORPORATE_ACTION_HANDLING` | `PARTIAL` (94 ADJUSTMENT_MISMATCH identified; no calendar) |
| `CACHE_INTEGRITY` | `PARTIAL` (606 absent Development partitions) |
| `PHASE8_EVIDENCE_INTEGRITY` | `DEGRADED_BUT_USABLE` |
| `RESEARCH_PLATFORM` | `READY_WITH_LIMITATIONS` |
| `PHASE9` | `COMPLETE` |

---

## Phase 10 — Low-Turnover Daily Opportunity Map

### Scope and approach

Phase 10 mapped daily-level trailing return states against 2/3/5-session forward outcomes on Development data only (2026-04-17–06-30). It used Phase 9 adjusted daily bars (`FID_ORG_ADJ_PRC=0`) from the separate reconciliation cache. Entry: next-session open after completed signal day. Exit: fixed horizon close. No parameter search, ML, or indicator soup.

### Data

- 60 frozen cohort symbols (30 KOSPI + 30 KOSDAQ)
- 49 Development sessions
- 2,940 feature observations, 11,100 outcome rows
- Source convention: KIS `inquire-daily-itemchartprice`, `FID_ORG_ADJ_PRC=0` (adjusted)
- Protected Holdout reads: 0. Secondary reads: 0. External reads: 0.

### Features

Per symbol and completed Development day: 1/2/3/5-session trailing adjusted-close return; 3/5-session range, realized volatility, and close location; distance from 5-session high/low; volume ratio vs. prior 5-day median; price bucket; adjusted-close-times-volume liquidity proxy; market; same-day cross-sectional return and volatility percentiles.

### Primary map: trailing 3-day return buckets × holding horizon

| 3-day return bucket | Horizon | n (non-overlapping) | Gross mean % | Net mean % (1×) | Win rate % | Payoff | Monthly (Apr/May/Jun) |
|---|---|---|---|---|---|---|---|
| <= -8% | 2d | 11 (8) | +1.57 | +1.04 | 54.5 | 1.38 | +/+/− |
| -8% to -4% | 2d | 36 (29) | +0.41 | −0.12 | 44.4 | 0.99 | +/−/+ |
| -4% to -2% | 2d | 86 (66) | −0.28 | −0.81 | 37.2 | 0.85 | +/−/− |
| -2% to 0% | 2d | 344 (275) | −0.32 | −0.85 | 36.6 | 0.85 | +/−/+ |
| 0 to +2% | 2d | 404 (319) | −0.51 | −1.04 | 34.4 | 0.80 | −/−/+ |
| +2% to +4% | 2d | 216 (171) | −0.46 | −0.99 | 35.2 | 0.82 | +/−/+ |
| +4% to +8% | 2d | 138 (111) | −0.49 | −1.02 | 34.8 | 0.79 | −/−/+ |
| >= +8% | 2d | 94 (74) | −0.10 | −0.63 | 39.4 | 0.89 | +/−/+ |

None of the buckets pass the promotion gate. The best gross mean is +1.57% (<= -8%, n=11) which is too small a sample. The best sample with n>=30 has gross mean +0.41% (-8% to -4%), below the 1.0% threshold. All payoff ratios except one small-sample cell are below 1.0.

### 5-day return buckets

Similar pattern. No bucket with adequate sample achieves 1.0% gross mean. The recent-loser buckets show slight positive means but fail the net-after-cost and payoff gates.

### Momentum vs. reversal

Neither direction produces a cost-sized edge. Recent losers (3-day return <= -8%) show a weak positive gross mean (+1.57%, n=11) that does not survive sample or cost gates. Recent winners show continuation drag (negative forward returns). The broad shape is mean-reverting but too small to be tradeable.

### Volatility, range, price, and liquidity splits

No volatility tercile, range position, price bucket, or liquidity bucket produced a qualifying state. LOW/MID/HIGH volatility states all had negative gross means at 2/3/5-day horizons.

### Monthly stability

May is consistently the weakest month across all states (mean gross typically -1.5% to -2.0%). April and June are slightly less negative or weakly positive. No state shows same-direction stability across all three months with adequate sample.

### KOSPI/KOSDAQ split

KOSPI is slightly less negative than KOSDAQ in most states (best observed: KOSPI -0.14% vs KOSDAQ -0.69% in the best broad state). Neither market produces a positive-cost edge.

### Promotion gate results

No candidate passed all required gates:
- Minimum 30 non-overlapping trades: Several cells pass.
- Gross mean >= 1.0%: Only the n=11 cell passes (insufficient sample).
- Net positive at 1× and 2× costs: No cell passes.
- Payoff ratio > 1.0: No cell with adequate sample passes.
- 2+ positive months with n>=5: Some cells pass.
- Max symbol/day share <= 20%: All cells pass.
- Market means not opposite: Mixed.

### Final verdicts

| Gate | Phase 10 result |
|---|---|
| `PHASE10_OPPORTUNITY_MAP` | `FAIL` |
| `DAILY_A` | `NOT_CREATED` |
| `PHASE10_SECONDARY` | `NOT_RUN` |
| `PHASE10_EXTERNAL` | `NOT_RUN` |
| `PHASE10_100K` | `NOT_RUN` |
| `SHADOW_NEXT_SESSION` | `NO` |
| `ALPHA` | `UNPROVEN` |
| `LIVE` | `DISABLED` |

Phase 10 was a **49-session Development study**. No cost-sized edge was found in that short window; this is not universal evidence against daily strategies. No daily-level family qualified for Secondary evaluation. The 0.53% round-trip cost exceeds every observed positive gross effect in the Development window. The overnight research scope (Phase 9 + Phase 10) is complete.

### Artifacts

Ignored `runtime/research/phase10/`: `daily-features.csv`, `daily-outcomes.csv`, `phase10-opportunity-map.json`, `phase10-summary.json`, `phase10-report.md`, `phase10-artifact-index.json`, `artifact-integrity.json`. Artifact integrity: PASS.

---

## Phase 11 — Multi-year Daily Evidence and Flow / Liquidity / Market-Context Map

### Scope and data

Phase 11 began from `7e867d9e7fb014e1c10d5bf83926817ffb3578da` on `main`; `origin/main` matched at start. The historical pool is 2023-01-02 through 2025-12-30, with Development 2023-01-02–2024-06-28, Validation 2024-07-01–2025-06-30, and Confirmation 2025-07-01–2025-12-30. 2026-01-05–2026-04-16 and 2026-07-28–2026-08-28 were not read. Warmup begins 2022-11-01 and contributes only trailing features.

The frozen Phase 4 cohort has 100 current listings (50 KOSPI, 50 KOSDAQ; order hash `5bcad330613b94bda148401cddcba8e62262a96e3e38acd4b70e4ed013e78f95`). This entails **CURRENT-LISTING SURVIVORSHIP BIAS**: point-in-time membership, delisted companies, and past index constituents were not reconstructed.

- 731 common index sessions; 68,236 daily stock rows and feature rows.
- 99/100 symbols returned KIS daily rows. KIS returned no daily rows for `282620` on the original acquisition and resumable retry. It remains missing; it was not replaced or filled.
- 90 symbols meet the >=500-row, multi-year-span, zero internal index-session-gap criterion. No synthetic bars were created. Dataset is `MULTIYEAR_DATA=COMPLETE` under the explicit 60-symbol minimum, while acquisition remains `PARTIAL` for the one no-row symbol.
- KOSPI and KOSDAQ index caches each contain 774 rows including warmup. All stock prices use only KIS adjusted daily bars, `FID_ORG_ADJ_PRC=0`; raw and adjusted conventions are not mixed. KIS adjusted history lacks a point-in-time revision vintage.
- KIS `acml_tr_pbmn` traded value and `acml_vol` volume were present in all 68,236 feature rows. Missing values, if returned, remain null rather than filled.
- Development outcomes contain 131,356 rows for 2/3/5/10-session horizons. Entry is next-session open after the completed signal day. Raw events, per-symbol non-overlap events, and unique session clusters are reported separately.
- Phase 5–10 protected manifest/index hashes: PASS, 20/20 unchanged. Phase 11 artifact index/integrity: PASS, 226 indexed files at the final analysis snapshot.

### KIS non-price source audit

The detailed source audit is retained at `runtime/research/phase11/phase11-source-audit.json`, including endpoints/adapters, available historical dates, timestamp semantics, revisions, point-in-time safety, rate limits, and reproducibility.

- Adjusted daily prices: `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`, TR `FHKST03010100`; date-ranged and reproducible with `FID_ORG_ADJ_PRC=0`.
- Daily turnover/volume: same response, fields `acml_tr_pbmn` and `acml_vol`; completed-session values, but no historical correction vintage or exact publication timestamp is documented.
- Latest per-stock investor endpoint `inquire-investor` (`FHKST01010900`) has no historical date parameter in the inspected sample and only a rolling 30-row example. It cannot reproduce this multi-year panel.
- Dated per-stock endpoint `investor-trade-by-stock-daily` (`FHPTJ04160001`) accepts one date per symbol. A full 100 × ~731 panel would require ~73,100 requests before retries, and exact publication/revision semantics remain undocumented. Excluded.
- Market investor flow `inquire-investor-daily-by-market` (`FHPTJ04040000`) can be queried by date, but is not per-stock flow; exact release time and revision policy are not established. Not used as a substitute.
- Historical KOSPI/KOSDAQ daily indexes use `/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice`, TR `FHKUP03500100`.
- Historical market cap, EPS, PER, PBR, financial statements, and sector membership were excluded because safe historical as-of/publication semantics were not established. Program-trading samples did not provide a suitable dated multi-year panel.

`FLOW_DATA=NOT_AVAILABLE`. Foreign, institutional, and individual flow factors are missing, not zero; price×flow interactions are unavailable. Price, liquidity, market-relative, and coarse price×liquidity maps are present, so `PHASE11_FACTOR_MAP=PARTIAL`.

### Development results

All states below are fixed coarse states from the Development map. Break-even friction equals gross mean.

| Family / state | Horizon | Gross mean | Net at 1× / 1.5× / 2× | Raw / non-overlap / unique signal sessions |
|---|---:|---:|---|---|
| Best price-only: stock 20d return DOWN (≤−4%) | 5d | +0.429% | −0.101% / −0.366% / −0.631% | 11,282 / 2,824 / 361 |
| Best liquidity: volume ratio 5d NORMAL (0.75–1.5) | 5d | +0.325% | −0.205% / −0.470% / −0.735% | 15,089 / 5,110 / 361 |
| Best price×liquidity: stock 5d return DOWN + no turnover expansion | 5d | +0.281% | −0.249% / −0.514% / −0.779% | 14,267 / 4,198 / 361 |
| Best standalone market context: matched index 20d return DOWN (≤−4%) | 3d | +1.046% | +0.516% / +0.251% / −0.014% | 4,624 / 1,927 / 76 raw dates, 35 non-overlap date clusters |
| Same market-down state | 5d | +1.601% | +1.071% / +0.806% / +0.541% | 4,624 / 1,300 / 76 raw dates, 28 non-overlap date clusters |
| Same market-down state | 10d | +2.935% | +2.405% / +2.140% / +1.875% | 4,624 / 903 / 76 raw dates, 20 non-overlap date clusters |

The market-down condition's 400-resample date-cluster lower 90% bounds were +0.213%, +0.338%, and +1.069% at 3/5/10d. The higher-horizon estimates have fewer independent non-overlap date clusters. At 3d, KOSPI gross was +1.423% (1,925 raw / 888 non-overlap / 39 sessions); KOSDAQ gross was +0.724% (2,699 / 1,039 / 65). At 5d the split was +2.342% / +0.982%, and at 10d +3.272% / +2.610%. These are shared market-state observations, not thousands of independent market events.

No flow state or price×flow interaction can be ranked. The 49-session Phase 10 study remains a short Development window and is not universal evidence against daily strategies.

### Frozen research candidate and Validation

The two initial 5-session rules—market-relative continuation and turnover expansion—failed their Development gates. After the maps were complete, one coarse market-context extension was frozen as the only `RESEARCH_CANDIDATE`:

> Matched KOSPI/KOSDAQ index 20-session return <=−4%; enter next-session open and exit at the close of the third session beginning with entry; per-symbol events do not overlap.

The 3-session horizon is the shortest primary horizon and retained 35 distinct signal-date clusters, compared with 28 at 5d and 20 at 10d; selection did not maximize gross return. The index state boundary was frozen in the factor map. The family/horizon were not in the initial candidate-eligible 5-session list, so this is explicitly a map-derived exploratory extension, not a claim of preregistered confirmation. See `candidate-map-extension.json` and `candidate-preregistration.json`.

- Development PASS: gross +1.046%, net +0.516% at 1× and +0.251% at 1.5×; payoff 1.328; 4,624 raw events, 1,927 non-overlap events, 76 raw signal dates and 35 non-overlap date clusters. All 6 Development quarters and all 3 half-years were positive. Cluster lower 90% bound: +0.213%. KOSPI and KOSDAQ gross means were both positive; KOSDAQ was below cost at 1.5×.
- Validation FAIL, one-shot and unchanged: gross +0.479% (<1%); net −0.051% at 1× and −0.316% at 1.5×; 5,239 raw, 2,114 non-overlap, 83 raw dates, and 41 non-overlap date clusters. Cluster lower 90% bound was −0.236%; the gross/cost gates failed. Validation split: KOSPI +0.997% gross, KOSDAQ +0.194% gross.
- Candidate `REJECTED`. Confirmation `NOT_RUN` because Validation failed. No post-Validation tuning occurred. Cost break-even was 1.046% in Development and 0.479% in Validation; at 2× cost the candidate was −0.014% and −0.581%, respectively.

### Other task item and final state

The cited overnight `execution-window` timestamps and report generator are absent from this repository and the supplied STATUS/RESULTS snapshots. The cause is `NOT_REPRODUCED_IN_REPOSITORY`; no report code was changed because its source path was unavailable.

| Status | Phase 11 result |
|---|---|
| `MULTIYEAR_DATA` | `COMPLETE` (90 dense multi-year symbols; acquisition 99/100 plus both indexes) |
| `FLOW_DATA` | `NOT_AVAILABLE` |
| `PHASE11_FACTOR_MAP` | `PARTIAL` (flow unavailable) |
| `PHASE11_CANDIDATE` | `REJECTED` |
| `VALIDATION` | `FAIL` |
| `CONFIRMATION` | `NOT_RUN` |
| `ALPHA` | `UNPROVEN` |
| `LIVE` | `DISABLED` |
| 2026 External / protected Holdout | `NOT_READ` / `NOT_READ` |

Next step: do not retune this rejected rule. Any new family should begin with a new frozen Development protocol; keep the untouched 2026 external and Holdout periods closed. The overnight report-generation bug needs the generator's source path for a separate fix.

Artifacts are under ignored `runtime/research/phase11/`: resumable adjusted cache, source/cohort/acquisition/data-quality manifests, features and Development outcomes, price/flow/liquidity/market-context maps, interactions, candidate extension/preregistration, Validation/Confirmation, cost stress, summary/report, artifact index, and integrity reports.

---

## Phase 12 — Market Stress Rebound Failure Anatomy and Regime Robustness

### Decision and boundary

Phase 12 explains, but does not retune, the Phase 11 candidate. Only Development (2023-01-02–2024-06-28) and touched Validation (2024-07-01–2025-06-30) were analyzed. These periods are mechanism-research evidence, not independent validation. Confirmation (2025-07-01–2025-12-30), external 2026, and protected Holdout were not read. The Phase 11 candidate remains `REJECTED`, its Validation remains `FAIL`, and all historical Phase 11 results were preserved.

**Verdict:** `PHASE12_FAILURE_ANATOMY=COMPLETE`; `MARKET_BETA_EXPLANATION=STRONG`; `STRESS_COMPOSITION_SHIFT=STRONG`; `MARKET_STRESS_MECHANISM=NOT_SUPPORTED`; `PHASE12_CANDIDATE=NOT_CREATED`; `CONFIRMATION=NOT_RUN`; `EXTERNAL_2026=NOT_READ`; `HOLDOUT_2026=NOT_READ`; `SHADOW_NEXT_SESSION=NO`; `ALPHA=UNPROVEN`; `LIVE=DISABLED`.

### Data, episode construction, and reproducibility

The analysis used the frozen 100-current-listing Phase 4 cohort (50 KOSPI / 50 KOSDAQ), KIS adjusted daily data (`FID_ORG_ADJ_PRC=0`), and the matched KOSPI/KOSDAQ indexes. The bounded loader produced touched-window bars for 96 symbols; 90 meet Phase 11's dense multi-year criterion. No delisted or point-in-time historical universe is reconstructed: **CURRENT-LISTING SURVIVORSHIP BIAS** applies. Investor flow remains `NOT_AVAILABLE`; no synthetic rows or forward-fill were used.

Market stress signal dates remain the Phase 11 matched-index 20-session return `<= -4%` rule. Entry is next-session open and exit is the third session close; per-symbol executions do not overlap. For episode anatomy, dates are grouped market-by-market when separated by at most two intervening non-signal market sessions; grouping resets at the Development/Validation boundary. Overlapping KOSPI/KOSDAQ episodes share joint macro clusters. The rule is independent of forward returns. Breadth requires at least 80% coverage of the eligible cohort on a market-date; all 104 Development and 114 Validation market-date observations passed.

The daily state dataset records index return horizons, realized volatility, drawdown/high-distance/age, ranges, cohort turnover/volume, breadth counts/coverage, and cross-sectional dispersion using information available through the completed signal date. Beta uses a fixed 60-session trailing window with at least 40 paired completed daily returns. Volatility, dispersion, and liquidity terciles are defined on Development and carried into Validation. Bootstrap resampling is by joint stress episode, never individual stock rows.

Phase 12 dataset SHA-256: `38734f06adb539ed5959b03cf4d22da139773429103bc3c2a588737c59f996cf`; cohort SHA-256: `5bcad330613b94bda148401cddcba8e62262a96e3e38acd4b70e4ed013e78f95`; config SHA-256: `e05dae8fe6bea8b10525a4c1f49274a86a3770bd2ad4cd404ccd5a4de0e3d2cf`. Bounded row hashes exclude later rows. Previous Phase 5–11 manifests/indexes: 23/23 unchanged, snapshot SHA-256 `acd09332adfb9603ebd4ea647389c8757958361519436c95ba3f16154e1201ea`.

Baseline verification passed 252 tests. Final regression passed 268 tests, including 16 new Phase 12 tests; full Ruff and `git diff --check` passed. A scoped credential-pattern scan over changed source, tests, and docs found 0 matches.

### Episode counts and chronological blocks

| Block | Episodes | Non-overlap events | Unique dates | Mean stress 20d | Stock 3d gross | Matched index | Stock excess |
|---|---:|---:|---:|---:|---:|---:|
| 2023 H1 | 4 | 350 | 14 | −7.123% | +1.900% | +1.543% | +0.357% |
| 2023 H2 | 6 | 936 | 35 | −8.282% | +0.359% | +0.330% | +0.029% |
| 2024 H1 | 5 | 641 | 27 | −5.961% | +1.583% | +1.361% | +0.222% |
| 2024 H2 | 10 | 1,544 | 57 | −7.366% | +0.410% | +0.404% | +0.007% |
| 2025 H1 | 2 | 570 | 26 | −6.902% | +0.665% | +0.265% | +0.400% |

Development had 15 market-specific episodes (KOSPI 7 / KOSDAQ 8) and 10 joint macro clusters. Validation had 12 (5 / 7) and 7 joint clusters. Performance alternated between stronger and weaker halves rather than fading gradually; the 2025 H1 excess recovery is based on only two market-specific episodes.

### Market-beta decomposition

| Period | Raw / non-overlap events | Raw dates / date clusters | Stock gross | Matched index | Stock excess | Beta residual | Net at 1× / 1.5× / 2× |
|---|---:|---:|---:|---:|---:|---:|---|
| Development | 4,624 / 1,927 | 76 / 35 | +1.046% | +0.893% | +0.153% | +0.319% | +0.516% / +0.251% / −0.014% |
| Validation | 5,239 / 2,114 | 83 / 41 | +0.479% | +0.366% | +0.113% | +0.162% | −0.051% / −0.316% / −0.581% |

The matched market mean fell 0.527 percentage points, 92.9% of the 0.567-point gross decay; it was 85.4% of Development gross. Arithmetic stock excess was small, had 90% cluster-bootstrap bands crossing zero in both periods, and was below the 0.53% cost. The beta residual is a descriptive diagnostic, not formal factor alpha.

KOSPI Development/Validation gross was +1.423%/+0.997%, versus matched indexes +1.228%/+0.953%. KOSDAQ gross was +0.724%/+0.194%, versus +0.607%/+0.044%. The Validation KOSPI result is market-led and remains a diagnostic split, not a retroactive market restriction.

### Stress regime, breadth, and beta findings

| Measure | Development | Validation |
|---|---:|---:|
| Mean signal-date index 20d return | −7.323% | −7.228% |
| Mean episode minimum index 20d return | −7.112% | −9.281% |
| Episode depth (MILD / MODERATE / DEEP / EXTREME) | 9 / 5 / 1 / 0 | 6 / 3 / 3 / 0 |
| FAST_SHOCK episodes | 3/15 (20.0%) | 11/12 (91.7%) |
| Mean signal-date 20d realized volatility | 1.327% | 1.859% |
| Mean 5d negative breadth | 0.636 | 0.582 |
| Mean 20d cross-sectional dispersion | 10.267% | 12.827% |

Signal-date 20d stress depth barely differed (SMD +0.035), although Validation had more deep episode minima. Volatility and dispersion rose substantially (SMD +0.909 and +0.662); breadth changed less (SMD −0.211). FAST_SHOCK outcome direction did not replicate: −0.040% gross / −0.220% excess in Development versus +1.529% / +0.224% in Validation. Neither speed nor another single state provides a stable mechanism.

Rolling-beta tercile diagnostics do not show a high-beta-only effect. LOW/MID/HIGH beta gross means were +0.725%/+1.471%/+1.300% in Development and +0.196%/+0.978%/+0.375% in Validation. HIGH_BETA residual was −0.267% in Development and +0.175% in Validation; MID_BETA residual was +0.573% / +0.255%. All Validation residual means are below the 0.53% cost scale.

The broad-selloff diagnostic (at least 70% of observed cohort members negative over 5 sessions) averaged +0.573% gross / +0.261% beta residual in Development and +1.994% / +0.672% in Validation. However Development had only 19 unique dates and residual below costs; Validation had only seven joint clusters. Narrow selloffs averaged +1.489% / −0.675% gross across Development / Validation. This failed the predeclared mechanism gate and was not promoted.

Recent-loser/winner, liquidity, and stock-volatility maps are descriptive. The lowest recent 20d stock-return tercile averaged +1.701% gross / +0.924% beta residual in Development and +0.657% / +0.140% in Validation; the highest-return tercile averaged +0.632% / −0.116% and +0.343% / +0.181%. Low/high stock-turnover terciles averaged +1.163%/+0.824% gross in Development and +0.497%/+0.469% in Validation. Low/high stock-volatility terciles averaged +0.897%/+1.031% in Development and +0.042%/+0.825% in Validation. None provides a stable after-cost separation. “Cheap” versus “expensive” was not assessed because point-in-time-safe valuation features are unavailable.

### Concentration, uncertainty, cost, and root cause

Top one / three / five market-episode shares of positive trade contribution were 9.64% / 31.86% / 44.64% in Development and 12.50% / 25.78% / 37.51% in Validation. Neither period depended on a single episode by this measure.

The 2,000-resample joint episode-cluster bootstrap 90% bands were: Development stock-minus-index excess [−0.041%, +0.488%], median +0.153%, and beta residual [+0.051%, +0.795%], median +0.333%; Validation excess [−0.194%, +0.280%], median +0.118%, and residual [−0.161%, +0.408%], median +0.163%. The Validation estimate has only seven joint clusters; no p-values were computed.

Cost assumptions remain fee 0.015% per side (`ASSUMED`), sell tax 0.20% (`VERIFIED_CURRENT` from the [KIS fee/tax schedule](https://securities.koreainvestment.com/main/customer/guide/_static/TF04ae010000.jsp?tab=3)), and slippage 0.15% per side (`CONSERVATIVE`, not empirically calibrated). Total round-trip remains 0.53%. No historical outcome or cost was changed.

Ranked conclusion: (1) **market-beta effect — STRONG**: index component explains 92.9% of gross decay; (2) **stress composition — STRONG descriptive shift**: volatility and dispersion increased, without a stable conditional rule; (3) **depth — WEAK**: signal-date depth almost unchanged; (4) **speed — weak causal support** despite a large composition shift; (5) **breadth — MODERATE shift**, not enough to pass the mechanism gate; (6) **dispersion — large shift but unsupported as a stable rule**; (7) **episode concentration — WEAK**; (8) **survivorship/data limitations — MODERATE limitation**, with no evidence attributing the decay specifically to it.

No hypothesis met the minimum of eight joint episodes in both periods, at least 20 market dates, at least three market-specific episodes per market, positive after-cost beta residual, both-period directional support, and the concentration gate. No candidate or candidate rule was created. Preregistration is `NOT_CREATED` with no freeze SHA; Confirmation is `NOT_RUN`. External 2026 and Holdout remain closed. Full definitions, distributions, integrity details, and explicit Q1–Q15 answers are in [the Phase 12 report](docs/research/phase12-market-stress-failure-anatomy.md). Machine-readable outputs are under ignored `runtime/research/phase12/`; artifact integrity checked 23 indexed artifacts and passed, with prior Phase 5–11 manifests/indexes unchanged.

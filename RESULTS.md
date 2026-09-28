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

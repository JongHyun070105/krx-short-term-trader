# Phase 13 — Point-in-Time Universe and Residual Factor Map

## Decision and evidence boundary

Phase 13 used the Phase 4 frozen 100-current-listing cohort (50 KOSPI, 50 KOSDAQ). KIS adjusted daily cache rows were available for 96 symbols; four symbols (`125490`, `282620`, `380550`, `456160`) had no usable rows through the touched period. The other 96 and both matched index series formed `CURRENT_COHORT`; no point-in-time cohort was substituted or mixed into it.

Only Discovery (2023-01-02–2024-06-28) and Touched Replication (2024-07-01–2025-06-30) outcomes were used for the factor map. Touched Replication is already historically touched and is not independent validation. The analysis clipped rows and outcomes at 2025-06-30. Confirmation (2025-07-01–2025-12-30) was not evaluated before the preregistered freeze; 2026 External and Holdout remained closed. No private KIS endpoint, account, order, paper trade, or live trade was used.

**Pre-confirmation finding:** a two-factor KOSPI state passed the fixed Discovery/Touched candidate gate and was preregistered as one `RESEARCH_CANDIDATE`. The broad single-factor winner failed after-cost economics. Date-cluster uncertainty for the candidate crosses zero in both periods, so `RESIDUAL_MECHANISM=WEAK` pending the one-shot Confirmation. `ALPHA=UNPROVEN`, `SHADOW_NEXT_SESSION=NO`, and `LIVE=DISABLED` remain in force.

## Point-in-time source audit

Official sources were inspected on 2026-10-01. No third-party finance source was merged into the primary dataset.

| Provider and source | Verified capability | Point-in-time / lifecycle limitation |
|---|---|---|
| [KRX KIND current company list](https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage) | Public current roster exposes listing date and current KOSPI/KOSDAQ/KONEX label, with an Excel export. | No dated membership snapshots, revision history, code lineage, or historical market-transfer semantics were established. Exact update cadence and bulk-use terms were not documented on the inspected page. |
| [KRX KIND delisted-company list](https://kind.krx.co.kr/investwarn/delcompany.do?currentPageSize=30&method=searchDelCompanySub) | Public, paginated current delisting reference; the inspected page showed 52 records and dates. | It is not a documented complete historical series. Complete retention, final trading date, code reuse, mergers, and lineage were not established. |
| [KRX Global delisting lookup](https://global.krx.co.kr/contents/GLB/03/0306/0306050000/GLB0306050000.jsp) | Interactive lookup exposes market/period search and code, name, delisting date, and reason fields. | The audit did not verify a reproducible bulk endpoint, complete date range, historical revisions, listing-date coverage, or security lineage. |
| [KRX OpenAPI service catalogue](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd) | Catalogue describes 2010+ KOSPI/KOSDAQ daily trading and basic security information. | API use requires an account, key approval, and per-service approval. The inspected description did not establish archived as-of membership, correction/vintage semantics, delisted-symbol retention, or ticker/code lineage. [Terms](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO002.jsp) limit API use to non-commercial purposes; data purchases can require separate review/fees. No key or API call was used. |
| [KIS official daily-chart sample](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_examples.py) and local Phase 11 cache | The local cache is KIS adjusted daily OHLCV (`FID_ORG_ADJ_PRC=0`) with matched KOSPI/KOSDAQ indexes. | KIS adjusted history has no historical-vintage selector; corporate-action restatements may revise earlier values. Delisted-price retention was not documented or tested. Phase 13 used local cache only. |

**Verdicts:** `POINT_IN_TIME_UNIVERSE=PARTIAL`; a defensible daily PIT cohort was **not built**. Listing dates, current market labels, and delisting references exist, but the inspected official sources did not establish complete effective-dated membership and security lineage for transfers, delistings, code changes, mergers, or spin-offs. `PIT_PRICE_HISTORY=NOT_AVAILABLE`: reproducible adjusted histories for delisted symbols were not verified. `SURVIVORSHIP_SENSITIVITY=NOT_TESTABLE`: the current-listing cohort necessarily omits firms that left the exchange, but no PIT cohort or delisted-price panel exists to quantify the effect. The limitation is structural and may be material; its numerical severity cannot be measured here.

## Dataset and point-in-time calculations

- **Prices:** local KIS adjusted daily OHLCV, `FID_ORG_ADJ_PRC=0`; market indexes are KOSPI and KOSDAQ. Source vintage is not point-in-time because KIS exposes no historical adjustment-vintage selector.
- **Signal features:** only completed session T and earlier. Cross-sectional ranks are scoped to market/date, ties share midrank, missing observations are omitted, and participant counts are retained. No missing value is set to zero.
- **Forward entry / exit:** entry at T+1 open; close exit at 3, 5, or 10 sessions counting the entry session. MFE/MAE use entry-to-exit daily highs/lows. No same-close fill is modeled.
- **Simple excess:** `stock return_h(T) − matched-index return_h(T)`, for h=1/3/5/10/20 close-to-close sessions.
- **Rolling beta:** OLS slope from at most 120 completed stock/index daily return pairs through T, minimum 60. The 120-session specification was fixed, not tuned. Beta, observation count and R² are saved. Estimates outside −2..5 are labeled; none are clipped. Missing beta stays missing.
- **Beta residual diagnostic:** `stock return_h − beta_T × matched-index return_h`. For forward outcomes, the same formula uses returns from T+1 open to the selected exit close. This is a simple residual diagnostic, not formal asset-pricing alpha.
- **Residual drawdown:** cumulative product of daily arithmetic stock-minus-index return differences; 20/60-session peak-to-current drawdown and distance from the recent high are computed from data through T.
- **Idiosyncratic volatility:** 20-session sample standard deviation of daily stock return minus matched-index return. LOW/MID/HIGH cut points are Discovery terciles, carried unchanged into Touched Replication.
- **Abnormal turnover:** five-session mean traded value divided by the prior 20-session median; states are `<0.75` CONTRACTED, `0.75–<1.5` NORMAL, and `≥1.5` EXPANDED.
- **Range position:** current close within the preceding 20-session high/low range, clipped to [0,1], then split into lower quarter, middle half, and upper quarter.
- **Costs:** unchanged 0.53% one-way-round-trip model: fee 0.015% each side (`ASSUMED`), 0.20% equivalent sell tax, 15 bps slippage each side. Stress costs are 0.795% (1.5×) and 1.06% (2×). Break-even friction equals mean absolute gross return.

The cache sidecar SHA-256 values (96 symbol entries plus two indexes) and the recomputed bounded dataset digest are recorded. The dataset digest covers the values passed after the date filter. Adjusted-price revision risk remains. The 56,045 feature rows generated 164,733 forward observations: 55,478 at 3 sessions, 55,100 at 5, and 54,155 at 10. Beta was missing for 2,274 feature rows; turnover was missing for 927. Missing values were excluded from only the affected map.

Dataset SHA-256: `a741a76f8b5519cd81125c289362b32da093b86b107ed7377eb89b0308623f10`

Cohort SHA-256: `5bcad330613b94bda148401cddcba8e62262a96e3e38acd4b70e4ed013e78f95`

Configuration SHA-256: `c2172600cfeca70e20da8effe1f23c93ca197452abb36cd424ace823dc0857e7`
Starting Git SHA: `0de301a1139ee84faa81a82ab6f6b7ead39ef6a5`

## Factor maps

All numbers below are percent means for 5-session forward outcomes using non-overlapping per-symbol executions. States are coarse, fixed, and missing rows are excluded. Map files also include raw observations, non-overlapping executions, signal dates, market/date clusters, symbol counts, MFE/MAE, gross/net, matched-market, excess, and beta-residual measures.

| Family | Discovery strongest broad cell (gross / excess / residual; net 1×) | Touched Replication strongest broad cell (gross / excess / residual; net 1×) | Read |
|---|---|---|---|
| Excess momentum | 10d excess 0–2%: +0.297 / +0.097 / +0.081; −0.233 | 20d excess 2–4%: +0.462 / +0.402 / +0.387; −0.068 | Positive continuation is descriptive; the leading horizon/state changes and neither is net-positive. |
| Excess reversal | 20d excess −2–0%: +0.377 / +0.145 / +0.145; −0.153 | 20d excess −8–−4%: +0.465 / +0.637 / +0.623; −0.065 | Recent underperformers often recovered relatively, but the strongest bucket changed and gross stayed below costs. |
| Beta-residual momentum | 20d residual +4–8%: +0.321 / +0.175 / +0.167; −0.209 | 20d residual +2–4%: +0.492 / +0.374 / +0.391; −0.038 | Positive continuation, too small in absolute economics; top state changed. |
| Beta-residual reversal | 20d residual −4–−2%: +0.224 / +0.282 / +0.231; −0.306 | Same 20d −4–−2% state: +0.347 / +0.492 / +0.444; −0.183 | Same-state residual/excess direction replicated, but both absolute net returns are negative. |
| Residual drawdown | 20d drawdown −4–0%: +0.338 / +0.056 / +0.162; −0.192. Deep ≤−8%: +0.208 / +0.018 / −0.053. | Deep ≤−8%: +0.342 / +0.342 / +0.352; −0.188. | The deep-drawdown residual changes sign across periods and remains below costs. A 60d touched cell with +2.901% gross has only 15 executions/dates and is not a qualifying state. |
| Idiosyncratic volatility | MID: +0.276 / −0.020 / −0.050; −0.254 | HIGH: +0.412 / +0.279 / +0.352; −0.118 | No stable volatility state; all broad 5d cells fail 1× costs. |
| Abnormal turnover | CONTRACTED: +0.317 / +0.117 / +0.107; −0.213 | EXPANDED: +0.748 / +0.400 / +0.443; +0.218 | The strongest state changes. EXPANDED turnover is promising only when combined with a specific KOSPI excess-reversal state below. |
| Residual rank | 20d MIDDLE: +0.235 / +0.092 / +0.110; −0.295 | 20d MIDDLE: +0.364 / +0.402 / +0.389; −0.166 | Same rank state has positive direction, insufficient absolute return after cost. |
| Range position | 20d LOWER_QUARTER: +0.266 / −0.005 / +0.011; −0.264 | Same state: +0.580 / +0.596 / +0.599; +0.050 | Same state is barely positive net in Touched and materially negative net in Discovery; not a standalone candidate. |

The full maps also record 1d/3d/5d/10d/20d feature availability, cross-sectional participant counts, market/date ranks, and diagnostic KOSPI/KOSDAQ index 5d/20d returns, 20d volatility, cohort breadth, and one-day excess dispersion. Market context is conditioning only; no market-direction rule was selected. Three pairwise interactions were evaluated, each with at most two factors: excess return × turnover, residual drawdown × idiosyncratic volatility, and residual rank × turnover. No third-order search or model fitting was performed.

## Strongest Discovery state and candidate selection

The strongest standalone Discovery residual cell was `BETA_RESIDUAL_REVERSAL`: trailing 20d beta residual from −4% to −2%, five-session horizon. Discovery had 3,246 raw observations, 1,557 non-overlapping executions, 340 unique signal dates, 611 market/date clusters, and 90 symbols. Gross was +0.224%, matched KOSPI/KOSDAQ return −0.059%, simple excess +0.282%, beta residual +0.231%, payoff 1.339, and break-even friction 0.224%. Net was −0.306% / −0.571% / −0.836% at 1×/1.5×/2×. Touched Replication had 2,175 raw, 1,087 non-overlap, 232 signal dates, 421 market/date clusters, 93 symbols; gross +0.347%, market −0.145%, excess +0.492%, residual +0.444%, payoff 1.340, break-even 0.347%, and net −0.183% / −0.448% / −0.713%. The market detracted from this cell (market/gross −26.2% and −41.8%); this cell fails absolute long-only economics.

The selected candidate came from a fixed, two-factor interaction map, then the already-required KOSPI/KOSDAQ split:

> **KOSPI excess-reversal with expanded turnover:** frozen KOSPI cohort classification; completed-session 5d stock-minus-KOSPI return ≤−4%; 5d mean traded value / prior 20d median ≥1.5; enter T+1 open, exit the fifth session close including entry; one active execution per symbol.

Two adjacent excess-return buckets crossed with EXPANDED turnover passed the fixed KOSPI sample, direction, absolute-net, payoff, date-cluster availability, market-component, concentration, and chronological gates in both Discovery and Touched Replication. The fixed priority selected the `≤−4%` bucket because its weaker-period residual was stronger, not because it had the highest gross/backtest return. The KOSPI/KOSDAQ split was pre-specified as a diagnostic and showed market-specific behavior in both periods; neither KOSDAQ cell passed the after-cost gate.

| Period / market | Raw / non-overlap | Signal dates / market-date clusters | Gross | Matched market | Excess | Beta residual | Net 1× / 1.5× / 2× | Market share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Discovery KOSPI | 580 / 249 | 159 / 159 | +1.160% | +0.479% | +0.681% | +0.590% | +0.630% / +0.365% / +0.100% | 41.3% |
| Touched KOSPI | 457 / 191 | 120 / 120 | +1.461% | +0.769% | +0.692% | +0.968% | +0.931% / +0.666% / +0.401% | 52.6% |
| Discovery KOSDAQ diagnostic | 315 non-overlap | 202 dates | +0.072% | +0.229% | −0.158% | −0.151% | −0.458% / −0.723% / −0.988% | undefined (small gross) |
| Touched KOSDAQ diagnostic | 244 non-overlap | 139 dates | +0.311% | +0.212% | +0.099% | +0.064% | −0.219% / −0.484% / −0.749% | 68.1% |

The KOSPI candidate's market contribution is below the 80% warning level; both residual and simple excess are positive in both periods, and absolute net remains positive at 2×. Gross exceeds 1% in both period aggregates. The period-level performance is uneven: KOSPI 2024 H2 was +0.301% gross and −0.229% net at 1×; all five chronological blocks had positive excess/residual, but only four had positive net at 1×. 2025 H1 was strongest. The state is plausible for a one-shot test, not established evidence.

| Chronological block | Executions | Gross | Excess | Residual | Net 1× |
|---|---:|---:|---:|---:|---:|
| 2023 H1 | 77 | +1.395% | +0.279% | +0.108% | +0.865% |
| 2023 H2 | 83 | +0.697% | +0.807% | +0.849% | +0.167% |
| 2024 H1 | 92 | +1.119% | +0.744% | +0.481% | +0.589% |
| 2024 H2 | 84 | +0.301% | +0.260% | +0.441% | −0.229% |
| 2025 H1 | 108 | +2.348% | +0.964% | +1.325% | +1.818% |

Concentration was within the fixed limits but approached them in Touched Replication. Top three symbols contributed 29.8% / 40.8% of positive gross contributions in Discovery / Touched; top five signal dates contributed 37.5% / 40.0%. The largest single trade was 7.8% / 16.4%; the top five trades were 26.3% / 36.2% of positive trade contribution. Bootstrap resampling used signal-date clusters, not stock rows: 2,000 draws, fixed seed 1313. Discovery 90% bands were gross [+0.029%, +1.791%], excess [−0.266%, +1.379%], residual [−0.451%, +1.179%]. Touched bands were gross [+0.401%, +3.494%], excess [−0.278%, +2.769%], residual [−0.099%, +3.036%]. Both residual intervals cross zero; hence `RESIDUAL_MECHANISM=WEAK` and the candidate remains unproven.

## Preregistration and Confirmation gate

The pre-confirmation preregistration records the exact candidate, CURRENT_COHORT/KOSPI membership rule, PARTIAL PIT status, feature states, beta and residual formulas, entry/exit, missing-data and non-overlap rules, costs, Discovery/Touched metrics, Confirmation dates, source Git SHA, dataset SHA, and config SHA. The one-shot runner will refuse access unless local `HEAD`, `origin/main`, and the expected freeze SHA match, the worktree is clean, the preregistration and dataset/config/source digests match, and the indexed pre-confirmation artifacts verify. It writes an `ACCESS_STARTED` marker before loading Confirmation data and rejects a second run.

At this pre-confirmation freeze, `CONFIRMATION=NOT_RUN`; `PRE_CONFIRMATION_FREEZE_SHA` is recorded by the one-shot runner after commit/push. Confirmation gates were fixed before access: at least 50 non-overlap executions and 40 signal dates; positive absolute gross, positive net at 1×, positive simple excess and beta residual, payoff >1, no excessive symbol/date concentration, and market component ≤80%. Positive gross ≥1% and positive net at 1.5× are preferred. A failure rejects the candidate and leaves 2026 External unopened. A pass produces `VALIDATION_CANDIDATE` only; 2026 External stays `PENDING_NEXT_PHASE` and Holdout stays `NOT_READ`.

## Artifact integrity and immutability

The reproducible JSON maps, dataset/source/cohort manifests, candidate analysis, preregistration, Confirmation guard, summaries, and gzip panel are generated in ignored `runtime/research/phase13/`. The panel gzip uses a fixed timestamp and sorted records. Its latest safe exit is 2025-06-30. The artifact index verifies 28 files. The Phase 5–12 manifest/index snapshot contains 21 entries; before/after hashes match with no changed path. Phase 11 and 12 historical artifacts/results were not overwritten.

The starting repository SHA was `0de301a1139ee84faa81a82ab6f6b7ead39ef6a5`. Phase13 implementation SHA-256, dataset SHA, cohort SHA, and config SHA are recorded in `phase13-dataset-manifest.json`; the selected rule/config and Discovery/Touched evidence are in `phase13-preregistration.json`. Source files are `src/krx_trader/research/phase13.py` and `tests/test_phase13.py`.

## Required questions

1. **Defensible historical PIT KRX universe?** `PARTIAL` evidence only; no complete defensible daily PIT universe was verified or built.
2. **Reproducible delisted-stock adjusted prices?** `NOT_AVAILABLE`; not verified from an official source.
3. **Current-listing survivorship sensitivity?** `NOT_TESTABLE`; structural bias is present, magnitude cannot be quantified.
4. **Does simple excess show momentum/reversal?** Both are visible descriptively; reversal among recent excess losers is more consistent, but state/horizon shifts and broad states do not clear costs.
5. **Does beta residual show momentum/reversal?** Both are visible; same-state 20d residual reversal is positive in D/T, but absolute net is negative.
6. **Does residual drawdown predict recovery?** No stable deep-drawdown result; the deepest 20d state changes residual direction across periods and stays below costs.
7. **Useful idiosyncratic-volatility information?** No stable state and no 1× net-positive broad cell.
8. **Does abnormal turnover improve residual prediction?** Expanded turnover is positive in Touched but not in Discovery alone; adjacent excess-reversal × expanded-turnover KOSPI interaction buckets pass the fixed gate, and one is selected by the predeclared residual-first priority.
9. **Strongest standalone Discovery factor state?** 20d beta residual reversal −4% to −2%, 5-session exit; gross +0.224%, excess +0.282%, residual +0.231%, net −0.306%.
10. **Same direction in Touched?** Yes for the same broad state, but gross +0.347% and net −0.183% fail costs.
11. **Market beta share for the standalone state?** Market detracted: −26.2% of gross in Discovery and −41.8% in Touched. For the selected KOSPI interaction the market shares are 41.3% and 52.6%, so the result is not explained primarily by beta.
12. **Positive absolute after-cost return?** Standalone winner: no. Selected KOSPI interaction: yes at 1×, 1.5× and 2× in both D/T aggregates.
13. **Economically meaningful residual/excess?** Selected KOSPI interaction has +0.590%/+0.968% beta residual and +0.681%/+0.692% simple excess, with gross +1.160%/+1.461%; uncertainty bands still cross zero.
14. **Did a mechanism justify one candidate?** Yes: exactly one `RESEARCH_CANDIDATE` was preregistered; residual mechanism is marked `WEAK` because clustered uncertainty crosses zero.
15. **Exact frozen candidate?** KOSPI only; 5d stock-minus-KOSPI excess ≤−4% and abnormal traded-value ratio ≥1.5; T+1 open to 5th-session close, non-overlapping per symbol.
16. **Did untouched 2025 H2 Confirmation pass?** At the pre-confirmation freeze, `NOT_RUN`; update this answer only after the guarded one-shot run.
17. **Should 2026 External be opened next?** Only if the one-shot Confirmation passes, and then in a separate next phase. It stays unopened in Phase 13.
18. **Ready for prospective Shadow?** No. `SHADOW_NEXT_SESSION=NO`, `ALPHA=UNPROVEN`, `LIVE=DISABLED`.

# Phase 11: Multi-year Daily Evidence and Factor Map

## Scope and evidence boundaries

Phase 11 uses a frozen current-listing cohort and the historical pool 2023-01-02 through 2025-12-30. Development is 2023-01-02 through 2024-06-28, Validation is 2024-07-01 through 2025-06-30, and Confirmation is 2025-07-01 through 2025-12-30. The 2026 external candidate window and 2026-07-28 through 2026-08-28 Holdout were not read. Warmup rows begin 2022-11-01 and are used only for completed-day trailing features.

The source cohort is the frozen Phase 4 100-symbol list (50 KOSPI, 50 KOSDAQ; SHA-256 `5bcad330613b94bda148401cddcba8e62262a96e3e38acd4b70e4ed013e78f95`). It contains current listings, not point-in-time historical index membership. **CURRENT-LISTING SURVIVORSHIP BIAS** therefore applies throughout; delisted and historical constituents are not reconstructed.

## Data and quality

- 731 common KRX index sessions span the full primary pool. The dataset has 68,236 stock-session rows and 68,236 completed-day feature rows.
- 99 of 100 cohort symbols returned KIS daily bars. KIS returned no daily rows for `282620` on both acquisition attempts; it remains missing, with no substitute symbol or synthetic bar.
- 90 symbols have at least 500 rows, a multi-year span, and no missing index session inside their observed span. The other symbols retain their actual listing/coverage gaps. Overall `MULTIYEAR_DATA=COMPLETE` because the frozen cohort exceeds the 60-symbol minimum and the multi-year/index coverage gates pass; acquisition itself remains `PARTIAL` because one symbol has no source rows.
- KOSPI and KOSDAQ index series each contain 774 rows including warmup. 2023–2025 feature maps use 731 common sessions.
- All 68,236 feature rows include KIS daily traded value (`acml_tr_pbmn`) and volume (`acml_vol`). Missing source values remain null; no forward-fill is used.
- Daily prices use only KIS adjusted daily OHLCV, `FID_ORG_ADJ_PRC=0`. Raw and adjusted bars are not mixed. KIS does not provide a historical adjusted-price vintage selector, so subsequent corporate-action revisions cannot be ruled out.
- The Development outcome file contains 131,356 rows across the defined 2/3/5/10-session horizons. Signal features use completed days; entries use the next session open.
- Twenty protected Phase 5–10 manifest/index hashes are unchanged. Phase 11's 226 indexed artifacts verify with SHA-256.

## Non-price source audit

The full endpoint, adapter, date depth, timestamp, revision, point-in-time, rate-limit and reproduction audit is `runtime/research/phase11/phase11-source-audit.json`.

| Source | Audit result |
|---|---|
| Adjusted daily OHLCV | KIS `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`, `FHKST03010100`; date-ranged history is reproducible using `FID_ORG_ADJ_PRC=0`. Session dates are represented at 00:00 KST. Later revisions have no point-in-time vintage selector. |
| Daily traded value and volume | Same daily response (`acml_tr_pbmn`, `acml_vol`); completed-session aggregates, reproducible for the pool. Exact publication time and historical correction policy are undocumented. |
| Per-symbol investor flow, latest endpoint | KIS `inquire-investor`, `FHKST01010900`; the inspected example has no historical date selector and the legacy helper exposes only a rolling 30-row response. Not a multi-year panel. |
| Per-symbol investor flow, dated endpoint | KIS `investor-trade-by-stock-daily`, `FHPTJ04160001`; accepts one date per symbol request, not a multi-year range. A 100-symbol, roughly 731-session panel would require about 73,100 requests before retries; exact release time and historical revision semantics are undocumented. Excluded. |
| Market investor flow | KIS `inquire-investor-daily-by-market`, `FHPTJ04040000`; official sample uses a single date in both date parameters. Per-date calls could build an aggregate series, but it is not per-symbol flow and its publication/revision timing is not established. Not used as a substitute. |
| KOSPI/KOSDAQ indexes | KIS `/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice`, `FHKUP03500100`; historical ranges are reproducible through bounded date requests. Index closes are used only after the signal session for next-session entry. No vintage selector is documented. |
| Program trading, market cap, valuation, fundamentals, sector | Inspected adapters/examples do not establish a reproducible point-in-time multi-year panel. Current market cap and sector membership lack historical as-of semantics; EPS, PER, PBR, and financial statements are excluded to prevent publication-date lookahead. |

Primary KIS references: [adjusted daily chart example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_examples.py), [dated per-stock investor flow](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/investor_trade_by_stock_daily/investor_trade_by_stock_daily.py), [latest investor query](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_investor/inquire_investor.py), and [daily market investor query](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_investor_daily_by_market/inquire_investor_daily_by_market.py).

`FLOW_DATA=NOT_AVAILABLE`; foreign, institutional, individual, and price×flow factors are missing, not zero. Consequently `PHASE11_FACTOR_MAP=PARTIAL`, even though price, turnover/liquidity, market-relative, and coarse price×liquidity maps are complete.

## Feature and outcome definitions

Features are completed-day only: 1/3/5/10/20-session adjusted returns; 5/20-session volatility and range; distance from 20-session high/low; overnight gap and intraday return; volume and traded-value ratios against prior 5/20-session medians; matching-index 1/5/20-session returns and 20-session volatility; symbol-minus-index returns; and cohort breadth where available. All state boundaries are coarse and fixed in the machine-readable hypothesis artifact. There are no weighted scores, machine learning, or broad parameter searches.

Primary outcome horizons are 3, 5, and 10 sessions; 2 sessions is context only. Entry is the first subsequent trading session's open, and exit is the close of the horizon-th session beginning with that entry session. The round-trip friction assumption is 0.53%; reports show 1×, 1.5×, and 2× cost and break-even friction.

Counts below distinguish raw symbol-events, non-overlapping per-symbol executions, and unique signal sessions. Co-movement across symbols is handled with deterministic 400-resample signal-session-cluster bootstrap for market-return states and candidate gates.

## Development factor map

All states below are Development-only. “Best” means the highest gross mean among broad states in that family at the given horizon, not a validated trading rule.

| Map family | Horizon | Best fixed state | Gross mean | Net after 1× / 1.5× / 2× cost | Raw / non-overlap / signal sessions |
|---|---:|---|---:|---|---|
| Price | 3d | Stock 5d return `DOWN` (≤−2%) | +0.206% | −0.324% / −0.589% / −0.854% | 10,753 / 4,733 / 363 |
| Price | 5d | Stock 20d return `DOWN` (≤−4%) | +0.429% | −0.101% / −0.366% / −0.631% | 11,282 / 2,824 / 361 |
| Price | 10d | Stock 5d return `DOWN` (≤−2%) | +0.758% | +0.228% / −0.037% / −0.302% | 10,531 / 2,128 / 356 |
| Liquidity | 3d | Volume ratio 5d `NORMAL` (0.75–1.5) | +0.130% | −0.400% / −0.665% / −0.930% | 15,175 / 7,631 / 363 |
| Liquidity | 5d | Volume ratio 5d `NORMAL` (0.75–1.5) | +0.325% | −0.205% / −0.470% / −0.735% | 15,089 / 5,110 / 361 |
| Liquidity | 10d | Turnover ratio 5d `NORMAL` (0.75–1.5) | +0.563% | +0.033% / −0.232% / −0.497% | 14,742 / 2,807 / 356 |

The strongest standalone market-context state was a matching index 20-session return `DOWN` (≤−4%): 3d gross +1.046%, 5d +1.601%, 10d +2.935%. Its raw count is 4,624 events across 76 signal sessions at each horizon. Non-overlap filtering leaves 1,927/1,300/903 executions and 35/28/20 unique execution signal sessions for 3d/5d/10d. Cluster-bootstrap lower 90% bounds are +0.213%/+0.338%/+1.069%, respectively. The larger-horizon means have fewer independent date clusters; 3d was selected for the candidate test because it was the shortest primary horizon meeting the 30-session-cluster gate, not because it maximized gross return.

At 3d, this market-down state averaged +1.423% gross on KOSPI (1,925 raw / 888 non-overlap / 39 signal sessions) and +0.724% on KOSDAQ (2,699 / 1,039 / 65). At 5d, the split was +2.342% KOSPI and +0.982% KOSDAQ; at 10d, +3.272% and +2.610%. The state is a common market condition: the 4,624 symbol-events are not 4,624 independent market observations.

Best 5d price×liquidity cell was stock 5d return `DOWN` plus no 20d turnover expansion: gross +0.281%, net −0.249% at 1×, 14,267 raw events, 4,198 non-overlap executions, 361 signal sessions. Price×flow is unavailable. The best price-only 5d state is also below assumed cost; neither initial 5d candidate rule (relative continuation or turnover expansion) passed its Development gate.

## Candidate and staged evaluation

One map-derived research candidate was frozen from Development:

> When the matched KOSPI or KOSDAQ index's completed-day 20-session return is at or below −4%, enter each eligible cohort symbol at the next session open and exit at the close of the third session beginning with entry. Per-symbol executions do not overlap.

The index state boundary already existed in the frozen factor map. The family was not one of the two initial 5-session candidate rules, so the extension and its exploratory status are explicit in `candidate-map-extension.json`. It is a research candidate only; no strategy, shadow, paper, or live trading was created.

| Stage | Result |
|---|---|
| Development | PASS: gross +1.046%; net +0.516% at 1× and +0.251% at 1.5×; payoff 1.328; 4,624 raw / 1,927 non-overlap / 76 raw signal sessions; 35 non-overlap signal-date clusters; all 6 quarters and 3 half-years positive; 400-cluster lower 90% bound +0.213%. |
| Validation (one-shot, unchanged rule) | FAIL: gross +0.479% (<1%); net −0.051% at 1× and −0.316% at 1.5×; 5,239 raw / 2,114 non-overlap / 83 raw signal sessions and 41 non-overlap signal-date clusters. The 400-cluster lower 90% bound was −0.236%, but cost and gross gates failed. |
| Confirmation | NOT_RUN because Validation failed. |

Validation's market split was +0.997% gross on KOSPI and +0.194% on KOSDAQ; neither provides a validated cost-sized family. The candidate is `REJECTED`, `ALPHA=UNPROVEN`, and `LIVE=DISABLED`. No retuning followed the Validation result. Cost break-even was 1.046% in Development and 0.479% in Validation. At 2× cost, Development net was −0.014%; Validation net was −0.581%.

## Other requested issue and next step

The cited overnight `execution-window` timestamps and a final-report generator do not occur in this repository or the supplied STATUS/RESULTS snapshots. The generator bug is `NOT_REPRODUCED_IN_REPOSITORY`; no report code was changed. A source path is needed to fix that separate report-generation issue.

Phase 11 is complete with a rejected candidate and no confirmation. The next research step is to write a new frozen Development protocol before considering any new family; do not retune this rejected rule or open the untouched 2026 external/Holdout periods.

## Artifacts

All Phase 11 runtime artifacts and the resumable daily cache are under ignored `runtime/research/phase11/`, including the source audit, acquisition manifest, dataset and availability manifests, feature/outcome CSVs, factor maps, interactions, candidate extension/preregistration, Validation/Confirmation, cost stress, summary, report, artifact index, and integrity reports. Artifact integrity is PASS; previous Phase 5–10 manifest integrity is PASS.

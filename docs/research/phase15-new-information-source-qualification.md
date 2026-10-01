# Phase 15 — New Information Source Qualification

## Decision

`PHASE15_SOURCE_AUDIT=PARTIAL`. No source meets the complete `QUALIFIED` gate, so `PRIMARY_SOURCE=NONE` and `SECONDARY_SOURCE=NONE`. Per-stock KIS investor flow has the strongest novelty and can be acquired at a manageable estimated cost, but seven same-request repeats returned different canonical payload hashes and the source exposes no verified publication timestamp or historical vintage. Market-level KIS flow and KIS program-trading samples repeated identically once each, but their time semantics and complete history remain unverified. Phase 15 did not run factor research, forward-return evaluation, strategy synthesis, or candidate creation.

```text
PHASE15_SOURCE_AUDIT=PARTIAL
PER_STOCK_FLOW=PARTIAL
MARKET_INVESTOR_FLOW=PARTIAL
PROGRAM_TRADING_DATA=PARTIAL
SECTOR_CONTEXT=PARTIAL
SECTOR_PIT_MEMBERSHIP=NOT_AVAILABLE
CORPORATE_EVENT_DATA=PARTIAL
HISTORICAL_MICROSTRUCTURE=NOT_AVAILABLE
PROSPECTIVE_MICROSTRUCTURE=AVAILABLE
POINT_IN_TIME_UNIVERSE=PARTIAL
DELISTED_PRICE_HISTORY=PARTIAL
SYMBOL_LINEAGE=PARTIAL
FRESH_HISTORICAL_EVIDENCE=PARTIAL
PRIMARY_SOURCE=NONE
SECONDARY_SOURCE=NONE
NEXT_RESEARCH_MODE=PROSPECTIVE_DATA_COLLECTION
EXTERNAL_2026=NOT_READ
HOLDOUT_2026=NOT_READ
ALPHA=UNPROVEN
SHADOW_NEXT_SESSION=NO
LIVE=DISABLED
```

Starting SHA: `a70fc8efcbebb9cf00757bb39deec34b649b16ba` (`main`, matched `origin/main` after fetch). The final delivery SHA is the resulting repository `HEAD` and is recorded in the execution report. The main branch was clean before Phase 15 began.

## Source audit and citations

The official KIS examples document date-anchor requests for per-stock investor flow (`FHPTJ04160001`), market-level investor flow (`FHPTJ04040000`), and per-stock program trading (`FHPPG04650201`): [per-stock flow](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/investor_trade_by_stock_daily/investor_trade_by_stock_daily.py), [market flow](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_investor_daily_by_market/inquire_investor_daily_by_market.py), [program trading](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/program_trade_by_stock_daily/program_trade_by_stock_daily.py), and the [KIS API catalog](https://apiportal.koreainvestment.com/apiservice-category). The examples establish request fields and continuation behavior, not a historical retention guarantee, publication time, or correction policy.

The [KIS usage page](https://apiportal.koreainvestment.com/about-open-api) limits quote-data use to the account holder's own investment purpose and restricts third-party provision. Raw provider values were therefore not persisted in Phase 15 artifacts; before broader research or sharing, confirm the applicable terms for internal research and any derived results.

The [KRX Data Marketplace](https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd?locale=ko) lists investor and industry/index services. Its [market investor page](https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT022) describes market-level investor categories and says finalized daily market data appears after 20:00 KST. Product-specific access and historical revision/vintage behavior still need a bounded series audit. Review the [KRX data-use policy](https://data.krx.co.kr/inc/datasale/Market%20Data%20Usage%20Polices_ko.pdf) before acquisition or redistribution.

The [OpenDART filing-list guide](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001) documents `rcept_dt` at day resolution, `corp_code` / `stock_code`, correction labels or remarks, paging, and a 20,000-request daily limit. The inspected guide does not establish an intraday publication timestamp or a complete original-to-correction parent chain. No API-key pilot was run; the earliest available filing date was not verified in this phase.

Official [KIND I-HQ delisting notice](https://kind.krx.co.kr/external/2024/12/05/000515/20241205001470/68051.htm) and [IzMedia delisting update](https://kind.krx.co.kr/external/2024/06/27/000865/20240627002013/70769.htm) were used to select two small delisted-symbol checks.

## Bounded KIS pilot

The authenticated pilot made 60 sequential data-endpoint query records. All 60 completed successfully: 50 per-stock flow queries (43 first-pass symbol/date requests plus 7 repeats), 3 market-flow queries, 3 program-trading queries, 2 delisted-symbol flow queries, and 2 delisted-symbol price queries. The initial live run also obtained one client-credentials token in memory; that token exchange is not included in the 60 data-endpoint requests and no token was persisted. No 2026 External or Holdout dates were requested. The shared local KIS limiter remained at 4 seconds per data request; the official per-endpoint rate limit was not located.

The deterministic current-symbol set was KOSPI `005930`, `000660`, `005380` and KOSDAQ `086520`, `028300`, `293490`. First-pass flow samples used representative dates in each year 2019–2025; a Samsung Electronics anchor dated 2018-06-01 tested older reach. Historical outcome values were not opened or evaluated. Two repeat calls were also made for one market-flow and one program-trading observation. Delisted samples were I-HQ `003560` at 2023-11-01 and IzMedia `181340` at 2023-06-01.

The date anchor is a rolling-window request in the observed responses:

| Endpoint | Observed rows per single-page response | Pilot date coverage | Repeat result |
|---|---:|---|---|
| KIS per-stock investor flow | 31 | 2018-04-18 through 2025-06-02 across sampled windows | 7/7 `REVISED` |
| KIS market investor flow | 300 | 2019-03-18 through 2024-06-03 across two sampled windows | 1/1 `IDENTICAL` |
| KIS per-stock program trading | 30 | 2021-04-19 through 2024-06-03 across two sampled windows | 1/1 `IDENTICAL` |

Each tested date anchor was the last returned session in its response window. The 2018-06-01 request returned rows beginning 2018-04-18; this is the earliest date actually observed, not a verified provider retention boundary. The dated stock-flow rows contained foreign registered/nonregistered buy, sell, net quantity and trading-value field names, plus individual (`prsn`) and organization (`orgn`) fields. Exact units and whether `orgn` maps one-to-one to the desired institutional definition must be verified against the provider field dictionary. Market-flow fields also included foreign, individual, organization, fund, insurance, bank, and other market-level categories. Program response fields included whole-market net quantity/value and buy/sell totals.

For the seven per-stock repeat pairs, both compact-response and canonical hashes differed. Row ordering and object-key ordering are normalized in the canonical comparison, so the verdict is `REVISED`, not a formatting-only mismatch. The pilot retained no raw values, so it cannot isolate which field changed or determine whether the change was a correction, provider refresh, or another source behavior. The market and program repeats were identical for their one selected sample each; this does not establish stability across their full histories.

The implementation follows continuation headers and has a bounded page limit. All 60 pilot requests completed in one page. The date anchor/window behavior was observed empirically; it was not found as a historical-retention promise in the reviewed documentation.

## Per-source qualification

### Per-stock investor flow — `PARTIAL`

Q1: Yes, reproducible historical foreign, individual, and organization/possible institutional fields were returned for sampled dates. However, same-request repeat hashes changed in all seven tested pairs, field-unit mapping remains incomplete, and the exact historical publication/vintage semantics are unknown. Therefore this is not yet safe to promote into retrospective predictive research.

Q2: The earliest source date returned in the pilot was **2018-04-18**, from the 2018-06-01 date anchor. The latest tested response reached 2025-06-02. These are sampled-window bounds, not verified continuous coverage or provider retention limits.

Q3: No. The API examples provide session-date parameters but no verified publication timestamp or historical as-published vintage. A future signal must be treated as `NEXT_SESSION_ONLY` at the earliest, after the collector records when it first observed the completed row. Unknown publication timing stays `UNKNOWN`.

### Market investor flow — `PARTIAL`

KIS returned 300 market-level dated rows for each tested date anchor, with the requested date as the last row. The repeated KOSPI query was `IDENTICAL`. KRX pages describe market-wide foreign, institution, and individual categories and state daily finalization after 20:00 KST. These are market aggregates, not stock-level observations. They cannot substitute for per-stock flow. KIS publication and revision semantics remain unknown.

### Program trading — `PARTIAL`

KIS returned 30 dated stock-level rows per tested anchor; the selected repeat was `IDENTICAL`. The sample included net and buy/sell fields. Broader category definitions, earliest retention, exact publication time, vintage behavior, and full-history continuity were not established. Program flow remains a separate input family from investor-class flow.

### Sector and industry context — `PARTIAL`; PIT membership — `NOT_AVAILABLE`

The official KIS catalog lists daily/period industry-index products, and KRX lists sector/index statistics. No historical sector-index series pilot was run, so an earliest verified date is not available. Current sector labels do not establish historical membership. Classification changes, market migration, mergers, ticker changes, and effective dates need an official point-in-time membership source. `SECTOR_PIT_MEMBERSHIP=NOT_AVAILABLE`.

### Corporate disclosures/events — `PARTIAL`

OpenDART provides date-filtered, paginated filing-list access and company/ticker identifiers. The inspected list guide exposes receipt date at day resolution, not a confirmed intraday publication timestamp. Correction report labels/remarks are available, but Phase 15 did not verify a complete parent-child correction chain or historical code mapping. No API-key acquisition pilot ran; earliest reproducible filing date is therefore unverified. KIND can provide dated listing/delisting notices, not by itself a complete corporate-event or code-lineage history.

### Order book and microstructure — historical `NOT_AVAILABLE`; prospective `AVAILABLE`

KIS catalog products expose real-time quotes/order-book capabilities, but the reviewed official materials did not establish retrospective order-book depth, historical tick trades, quote updates, or execution direction. Real-time collection could be prospective, with local receipt timestamps and frozen raw-data policy subject to the provider terms.

## Delisted symbols, PIT universe, and code lineage

Both officially documented sample companies returned 31 flow rows for their pre-delisting date anchors. The price endpoint returned one requested-date OHLCV row for each tested delisted symbol. This improves the project evidence from no verified delisted-price example to a **two-symbol partial sample**; it does not establish complete delisted coverage or survivorship-free universe membership.

`POINT_IN_TIME_UNIVERSE=PARTIAL`; `DELISTED_PRICE_HISTORY=PARTIAL`; `SYMBOL_LINEAGE=PARTIAL`. KIND listing/delisting notices and OpenDART company/ticker identifiers support individual identity checks. A complete effective-dated map for ticker changes, market migrations, mergers, spin-offs, and successor symbols remains unavailable.

## Acquisition cost and operability

The previous one-request-per-symbol-session estimate is too pessimistic for these KIS endpoints: the pilot returned up to 31 stock-flow rows per anchor. For a planning panel of 100 symbols and approximately 1,708 KRX sessions across 2019–2025, use a conservative 30 new sessions per 31-row response (one-session overlap buffer):

```text
ceil(1,708 / 30) = 57 date-anchor requests per symbol
100 × 57 = 5,700 base requests
1% retry allowance = 57 additional requests
total = 5,757 requests
5,757 × 4 seconds = 23,028 seconds = 6.40 continuous hours
```

At the same assumptions, a 100-symbol three-year/731-session panel is about 2,500 base requests, 2,525 including 1% retries, or 2.81 continuous hours. Program trading is estimated separately at 30 returned rows per anchor: 5,900 base and 5,959 total requests (about 6.62 hours). Market flow at 300 rows per anchor is about 12 base queries for two markets and 13 total with the same retry allowance (about 52 seconds).

These are `MANAGEABLE` planning estimates, not a completed panel. They assume responses can be stepped backwards without date gaps, the pilot row window is representative, each response remains one page, and the observed local 4-second limiter is sufficient. KIS endpoint-specific official rate limits and whole-history continuity remain unverified. Estimated size uses 2,000 bytes per request response and excludes runtime/cache overhead.

## Fresh historical evidence and proposed split

The Phase 14 `research-ledger.json` records outcome/factor usage beginning in 2023, and the Phase 11 report identifies 2022-11-01 through 2022-12-30 as price-only warmup. A search of `docs/research/`, `STATUS.md`, `RESULTS.md`, and the Phase 14 ledger found no documented 2019–2021 outcome window. The Phase 14 ledger does not fully enumerate Phase 3–9 data-use dates, so those manifests must be reconciled before any later return-based work declares a period independent.

The source pilot reaches 2018, which makes new historical source evidence plausible, but it is only sparse anchor sampling, not a complete panel. Therefore `FRESH_HISTORICAL_EVIDENCE=PARTIAL`, not `AVAILABLE`.

Conditional future plan only; Phase 15 evaluated no forward returns:

| Period | Proposed role | Status/caveat |
|---|---|---|
| 2018-04-18–2018-12-31 | Warmup | Source is observed from 2018-04-18, but continuous coverage and adequate warmup rows are unverified |
| 2019-01-02–2020-12-30 | New discovery | Conditional on Phase 3–9 manifest reconciliation and complete source coverage |
| 2021-01-04–2021-12-30 | New replication | Conditional on the same gates |
| 2022-01-03–2022-10-31 | New confirmation | Conditional; 2022-11 onward is already touched as Phase 11 warmup |

Resolve exact KRX sessions later from an official calendar. Keep 2023–2025 as historical sensitivity only. Keep 2026 External separate and unread; keep the 2026-07-28–2026-08-28 Holdout unread and excluded.

## Pipeline v2 integration and proposed registry

If a source later passes the timestamp, revision, coverage, and licensing gates, Phase 14 can consume it through a session/symbol adapter with explicit `availability_time`, source-vintage ID, missingness, and response hash. Required work includes session alignment, as-of joining, field/unit definitions, deterministic frozen source caches, and revision/vintage records. Until then, do not register it as an active factor.

The proposed extension is marked `PROPOSED`; its feature names are `POTENTIAL_FEATURE_ONLY`. Foreign/institution/individual net-flow ratios, five-session flow persistence, flow acceleration, program net-flow ratio, sector-relative return, and days-since-disclosure have no predictive result. The generated extension keeps all entries `DEFERRED`, records availability policy, and leaves the active registry unchanged.

Source ranking is by novelty, point-in-time safety, depth, fresh evidence, granularity, cost, coverage, survivorship usefulness, and pipeline compatibility—never profitability. Per-stock flow ranks highest for novelty; KRX market flow and OpenDART are cheaper but coarser or lack verified intraday timing. None is promoted as primary or secondary.

## Answers to Q1–Q20

1. **Can per-stock flow be obtained?** Yes for sampled historical date windows, including foreign, individual, and organization field families; repeat changes and unknown timing keep it `PARTIAL`.
2. **Earliest verified date?** 2018-04-18 returned in the 2018-06-01 anchor window.
3. **Safe publication semantics?** No. Exact publication time and vintage behavior are unknown; future use is at best next-session-only after timestamped capture.
4. **Full panel requests?** Approximately 5,700 for 100 symbols × 1,708 sessions if each response supplies 30 new sessions; 5,757 with the 1% retry allowance.
5. **Acquisition duration?** Approximately 6.40 hours at the observed 4-second local interval, continuous and before downtime/auth overhead.
6. **Program trading reproducible?** One selected repeated historical query was identical; two date anchors returned 30 rows. Broader stability/depth remains unverified.
7. **Historical sector context?** Official market/index products exist; KIS market-flow history was returned in 300-row windows. Sector-index history was not piloted.
8. **PIT sector membership?** Not established; `NOT_AVAILABLE`.
9. **Disclosures with original publication timestamps?** Not established. OpenDART filing-list receipt date is day-resolution in the inspected guide; no intraday timestamp pilot ran.
10. **Original vs corrected filings?** Some correction labels/remarks can be identified; a complete original-to-correction chain was not verified.
11. **Historical order book/tick?** Not verified/available from reviewed sources.
12. **Prospective microstructure?** KIS catalog exposes real-time quote/order-book products; prospective capture is possible, subject to terms.
13. **Represent delisted symbols?** Partially; two KIND-documented samples were queried.
14. **Delisted historical prices?** Yes for the two tested 2023 observations; general coverage remains partial.
15. **Code lineage?** Partial identity and listing/delisting notices; complete effective-dated successor lineage unavailable.
16. **Fresh 2019–2022 evidence?** Plausible but only partial. Historical dates exist, while contiguous panel availability and older phase-use reconciliation remain open.
17. **Most novel input?** Per-stock investor flow.
18. **Most practical?** KIS rolling-window flow is estimated manageable at about 6.4 hours for the 100-name 2019–2025 panel, but exact provider limits, continuous coverage, revisions, and terms remain gates. Market aggregate flow is cheaper but not stock-specific.
19. **Primary for next phase?** `NONE`; no source passes the full qualification definition.
20. **Next mode?** `PROSPECTIVE_DATA_COLLECTION`, freezing dated per-stock flow responses with local availability timestamps after provider terms are confirmed. Do not start factor discovery until the captured vintages and licensing are qualified.

## Artifact and integrity record

The live pilot and generated artifacts are under ignored `runtime/research/phase15/`. Source-specific append-only JSONL pilot caches contain request metadata, field names, dates, row counts, status, and hashes only; raw investor, market, program, and price values are not persisted. KIS token caching was disabled for the pilot, and authorization headers/credentials were not written to manifests.

`phase15-artifact-index.json` verifies all indexed Phase 15 payloads. `phase15-prior-artifact-integrity.json` reports all **48/48** snapshotted Phase 5–14 artifacts unchanged. See `phase15-summary.json`, `source-scorecards.json`, `pilot-request-manifest.json`, `acquisition-cost-estimates.json`, `fresh-evidence-feasibility.json`, and `source-ranking.json` for machine-readable detail.

The pre-implementation baseline was **323 passing tests**; the completed tree passes **366 tests**. Ruff, `git diff --check`, and the changed-source/docs credential-pattern scan pass; the scan found zero credential-pattern matches. The starting SHA was `a70fc8efcbebb9cf00757bb39deec34b649b16ba`; the final pushed `main` SHA is recorded in the Phase 15 delivery result.

No strategy, candidate, or profitability backtest was created. `EXTERNAL_2026=NOT_READ`, `HOLDOUT_2026=NOT_READ`, `ALPHA=UNPROVEN`, `SHADOW_NEXT_SESSION=NO`, and `LIVE=DISABLED` remain unchanged. The exact next research action is prospective, timestamped source capture after provider terms are confirmed, followed by a freeze and vintage/revision qualification before any factor discovery.

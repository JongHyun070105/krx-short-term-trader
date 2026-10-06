# Phase 16C — KIS Per-Stock Response Contract Requalification

## Verdict

`PHASE16C=COMPLETE`. The 2025-06-02 incident is reconstructable from safe stored date metadata. Its two recorded failures were `DATE_FIELD_INVALID_OR_MISSING` and `ROW_OUTSIDE_VERIFIED_RESPONSE_ENVELOPE`.

The first means at least one flattened KIS output object had no valid `stck_bsop_date`. The second was caused by the historical envelope calendar: it counted weekdays but applied no KRX closures. For anchor 2025-06-02, that calendar expected 2025-04-21; the 30 stored dated rows ran from 2025-04-17 through 2025-06-02. Those rows are consecutive sessions in the local KRX index calendar. The actual payload was not saved, so the incident's undated object's precise block cannot be recovered. All 24 new probes showed one consistently undated `output1` object and 30 dated `output2` rows, which corroborates the parser-shape explanation without proving that the incident's undated object was from `output1`.

The KIS trailing endpoint remains unsuitable for prospective evidence. Across the frozen 30-observation matrix, the best observed fit is 30 dated rows for the latest 30 KRX sessions plus one undated response object. This is not a provider guarantee. KIS documents no absolute response-row cap, and no maximum gap between provider observations has been proven. Therefore a finite protected-range envelope cannot be computed before transport. The persisted per-stock `ERROR` state and `REVIEW_REQUIRED` hold remain in place; the former first-safe date, 2026-10-16, is now `UNKNOWN` under this evidence policy.

No strategy, forward-return, factor IC, profitability, or backtest work was performed. `ALPHA=UNPROVEN`, `SHADOW_NEXT_SESSION=NO`, and `LIVE=DISABLED` remain unchanged.

## Incident reconstruction

| Field | Reconstructed evidence |
| --- | --- |
| Symbol / market | `005930`; KOSPI from the frozen Phase16B cohort (market absent in the violation event itself) |
| Requested / effective anchor | `2025-06-02` / `2025-06-02` |
| Old expected envelope | `2025-04-21` through `2025-06-02` |
| Actual stored dates | 30 descending dates, `2025-04-17` through `2025-06-02`; no duplicates or newer-than-anchor rows |
| Object counts | 31 response objects recorded; 30 parseable dated sessions and one undated object |
| Exact failures | `DATE_FIELD_INVALID_OR_MISSING`; `ROW_OUTSIDE_VERIFIED_RESPONSE_ENVELOPE` |
| Response hash | `347fbefb9abccdad62b59a9d0c06c13908677356e2307bee1f336c834651e3a4` |
| Request hash / incident contract version / collector SHA | Not retained in the incident; not inferred |
| Incident config SHA | Not retained. Active v4 config context is `85436bfa28e57367f417d5cf102f3a7112aff24309907b7d012d2a31dcf7c91e` |

The exact validator path is `phase16b.verify_response_contract` → `phase16b.extract_dated_rows` and `phase16b.possible_response_session_range`, using `phase16b._historical_probe_calendar`. The old calendar version was `FIXED-HISTORICAL-PROBE-WEEKDAY-ENVELOPE-v1`; its weekday list did not remove May 1, May 5, or May 6, 2025. The incident dates match 30 local KRX index sessions, so the incident itself does not demonstrate a Samsung-specific observation gap.

## Frozen sample and bounded probes

The sample was frozen and hashed before the new requests: `09556ab934cafc6fca365f483d7ce48b4c22e13b7ff0b4c90dc00ecbfd3e41b2`. It contains six preselected symbols, three KOSPI and three KOSDAQ, including the incident symbol, higher/lower activity comparators, and the 208860 inactivity stress proxy. Selection used no future returns. Five old anchors covered 2019, 2022, 2023, 2024, and 2025; six existing Phase16B observations were reused, so 24 new requests were planned and made.

All 24 requests used `CONTRACT_REQUALIFICATION_PROBE`, one page, the existing shared KIS limiter, and zero retries. The run lasted 92.49 seconds. Every requested anchor preceded the earliest protected period; no returned date intersected either protected range. No raw provider payload or credential was written. The separate Phase16C result records retain dated-session lists, ordering, counts, schema fingerprints, canonical response hashes, anchor relation, local calendar/price comparisons, request hashes, and safe timing metadata. Nothing was written to the prospective Phase16 store or its manifest chain.

The 24 new responses shared one Phase16C schema fingerprint and the same structure: 31 total objects, 30 dated `output2` rows, and one undated `output1` object. All were descending, anchor-inclusive, had no future rows or duplicates, and produced no Phase16C contract mismatch. The six reused Phase16B observations bring the comparison matrix to 30 responses across both markets and years 2019, 2022–2025. Phase16B used a different schema-fingerprint algorithm, so its hashes are compared internally and not directly with Phase16C's.

## Local calendar and symbol-gap comparison

The local Phase11 index calendar, sourced from KIS's separate daily index endpoint, covers 774 sessions from 2022-11-01 to 2025-12-30. Within that independent endpoint's coverage, 18 of 30 matrix responses were comparable and all 18 exactly matched the latest 30 local KRX sessions; none contradicted that model. The 12 earlier responses are not assessable against that local calendar. In the incident's 2025-06-02 span, all 30 dates match local KRX index sessions, with no missing index session inside the response range. This is a separate KIS index source, not an official KRX calendar feed.

Four symbols have local Phase11 daily price caches. All 12 responses in that coverage match the latest 30 local symbol price rows as well. This cannot distinguish a KRX-session model from a symbol-price-row model because those caches are dense. The 99 local symbol caches have no missing index sessions within their observed spans, but their date coverage begins in 2022 and cannot establish KIS flow coverage or a bound on provider gaps.

The low-activity proxies are not verified suspensions. Symbol `101680` has 101 zero-volume price rows of 774; `208860` has 654 of 774. In the three `208860` windows, the returned 30 dates coincide with 30 local zero-volume price rows. This supports the observation that the endpoint returned dated records on those no-volume price dates, but it does not establish the contents or future availability of KIS flow records. No verified long gap or suspension was found in the available local sample.

## Candidate response models

| Model | Explained | Contradicted | Not assessable | Worst observed span | Assessment |
| --- | ---: | ---: | ---: | ---: | --- |
| Last N KRX sessions | 18 | 0 | 12 | 46 calendar days | Best observed fit where local index coverage exists; not a vendor guarantee |
| Last N local symbol-price rows | 12 | 0 | 18 | 46 calendar days | Fits every covered local price sample; cannot be distinguished from KRX sessions here |
| 31-calendar-day trailing window | 0 | 30 | 0 | 46 calendar days | Contradicted by every matrix response's observed span |
| Provider-specific 31-object, one-page window | 30 | 0 | 0 | 46 calendar days | Describes this finite sample only; vendor cap is undocumented |
| No safe finite pre-network bound | N/A | N/A | N/A | 46 calendar days observed | Safety conclusion: no documented row cap and no proven maximum observation gap |

The matrix found no observed response-semantic difference by sampled symbol, market, or year. Twelve early responses lack independent local calendar coverage; this limits the stability claim. Weekend and holiday anchor behavior was not probed, so `NON_SESSION_ANCHOR=UNSUPPORTED`; scheduled collection must continue to require a verified completed KRX session.

## Bound and source decision

The current `maximum_rows=31` comes from a finite sample. Official KIS docs/examples do not state an absolute response cap. A fixed row count alone would not bound calendar time without a proven maximum missing-session gap. The local price cache cannot supply that guarantee because it is finite, begins in 2022, and is not proof of KIS flow-row availability. Filtering dates after a response arrives cannot prevent protected-data exposure.

Therefore:

- `KIS_PER_STOCK_CONTRACT=REJECTED_FOR_PROSPECTIVE_USE`.
- `KIS_PER_STOCK_CONTRACT_MODEL=NO_SAFE_FINITE_BOUND` for safety, while `LAST_N_KRX_SESSIONS` remains the best observed response fit.
- `PRE_NETWORK_BOUND=NOT_PROVEN`; `FIRST_SAFE_PER_STOCK_ANCHOR=UNKNOWN`.
- The original source hold is not cleared. Historical requests remain blocked before transport while the source has a persisted contract issue.
- KIS `/quotations/inquire-investor` is not an exact-day historical alternative: its official example takes only market and symbol inputs and describes current-day data after close.
- KRX Data Marketplace lists `투자자별 거래실적(개별종목)` and exposes a historical date-range UI. Its actual response payload, KOSPI/KOSDAQ coverage, field semantics, and publication timing could not be verified through the unauthenticated loader. This candidate is `PARTIAL`; no adapter was activated.
- Recommended automatic per-stock source: `NONE` until the official KRX per-stock response is verified with bounded old-date exact-session tests.

## Regression, integrity, and delivery

Twelve new Phase16C tests cover incident parsing, the original weekday-envelope failure, output-block parsing, ordering variation, future dates, non-session rows, duplicate dates, row-count drift, long/extreme observation gaps, finite-bound detection, protected-range preflight, evidence-gated hold clearing, first-safe-anchor handling, and contract versioning. Existing Phase16B tests still verify that historical probes remain blocked before transport after a contract violation, while program-flow and KRX market-flow behavior remain isolated. The required two-reviewer cross-provider attempt failed before worker output because the local orchestration gateway raised `NameError: _run is not defined`; no delegated claims were used.

The Phase16C probe artifacts are additive under ignored `runtime/research/phase16c/`. They do not modify Phase16 or Phase16B evidence and do not append to the Phase16 chain. Final byte-level evidence counts, chain prefix, test/Ruff results, credential scan, and Git delivery are recorded in `phase16c-summary.json` and the artifact index.

## Required answers

1. **Failed invariant:** `DATE_FIELD_INVALID_OR_MISSING` and `ROW_OUTSIDE_VERIFIED_RESPONSE_ENVELOPE`.
2. **Symbol:** `005930` (KOSPI context from the fixed cohort; market was not in the incident event).
3. **Expected dates:** Old envelope `2025-04-21`–`2025-06-02`; the reference 30-session KRX index span starts `2025-04-17`.
4. **Actual dates:** Recoverable from safe metadata: 30 descending dates, `2025-04-17`–`2025-06-02`; the full date list is in the incident artifact.
5. **Cause:** The envelope calendar omitted KRX holidays; one flattened incident object lacked a date. New probes identify `output1` as consistently undated, but the incident's exact output block is not recoverable. The incident does not show a symbol-specific gap.
6. **Observed response model:** Latest 30 KRX sessions is the best fit. Dense local symbol-price rows fit too; that does not prove KIS flow observation semantics.
7. **Support:** 30 matrix observations total (24 new, 6 reused); 18 are covered by the local index calendar and all 18 match. Twelve older responses are not assessable against that cache.
8. **Drift:** No sampled difference by symbol, market, or year; 24 new live responses shared one schema fingerprint. The sample is not a vendor guarantee.
9. **Finite pre-network range:** Not proven.
10. **Bound rule:** Not applicable until an official cap and maximum observation-gap rule exist.
11. **Safety reason:** Without those limits the trailing range is unbounded from pre-request evidence, so a response could cross a protected boundary before post-response checks run.
12. **Old 2026-10-16 anchor:** Not valid as a protected-range safety claim.
13. **Replacement:** `UNKNOWN`.
14. **Exact-day KIS alternative:** None found. The current investor endpoint has no historical date input.
15. **Exact-day KRX per-stock source:** Candidate found (`투자자별 거래실적(개별종목)`), but actual payload and contract remain unverified (`PARTIAL`).
16. **Safest prospective source:** `NONE` is qualified today; verify the KRX exact-day candidate next.
17. **Protected-period touch:** No request anchor or returned date touched a protected range.
18. **Existing evidence:** No Phase16/16B bytes changed; all 850 Phase5–15 snapshot artifacts and the Phase16 chain prefix are unchanged.
19. **Program flow:** Still `WAITING_FOR_SAFE_DATE`.
20. **KRX market flow:** Still `ACTIVE`.
21. **Per-stock state:** Contract `REJECTED_FOR_PROSPECTIVE_USE`; persisted source state `ERROR`; hold `REVIEW_REQUIRED`.
22. **Next action:** Keep KIS trailing transport denied; verify KRX's actual old-date individual-stock payload, KOSPI/KOSDAQ coverage, field semantics, publication timing, and access conditions before adapter work or activation.

## Official source references

- [KIS per-stock daily investor-flow sample](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/investor_trade_by_stock_daily/chk_investor_trade_by_stock_daily.py)
- [KIS current investor sample](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_investor/inquire_investor.py)
- [KRX Data Marketplace](https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd)
- [KRX individual-stock investor page](https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?kosdaqGlobalYn=1&locale=ko_KR&screenId=MDCSTAT023)

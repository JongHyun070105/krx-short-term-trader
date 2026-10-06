# Phase 16D — KRX Exact-Day Per-Stock Investor-Flow Qualification

## Verdict

`PHASE16D=PARTIAL` and `KRX_PER_STOCK_SOURCE=PARTIAL`. The official KRX screen is identifiable and exposes exact-date inputs, per-security selectors, a date-bearing daily-trend view, and explicit buy/sell/net and volume/value controls. The KRX page also documents its planned final-data availability after 20:00 KST. The response payload, exact-session behavior, market/security coverage, returned identifiers, raw-unit scale, repeatability, and correction behavior could not be qualified because the current KRX site terms prohibit automated collection without permission and no KRX automation authorization was available for this run.

The 30-request old-date plan was frozen and SHA-256 hashed before any data request. No data request was submitted, no raw provider response was received or stored, and no Phase16 prospective record or manifest node was appended. The adapter therefore remains request/validation scaffolding only. The Phase16 source registry, scheduler, KIS rejection, program-flow state, and aggregate KRX market-flow source were not changed.

No strategy, forward-return, alpha, factor-IC, candidate, or PnL analysis was performed. Protected periods were not queried. `ALPHA=UNPROVEN`, `SHADOW_NEXT_SESSION=NO`, and `LIVE=DISABLED` remain unchanged.

## Run identity and boundaries

| Item | Result |
| --- | --- |
| Starting branch / SHA | `main` / `ac1c83bac2ed2d43d982e855d9a0ed18a69cce7b` |
| Fetched `origin/main` | Same SHA as starting `HEAD` |
| Starting worktree | Clean |
| Baseline suite | 450 passed with `uv run pytest -q` |
| Phase16D tests | 24 deterministic tests; no network dependency |
| Protected ranges | `EXTERNAL_2026` 2026-01-05–2026-04-16 and `JUL_AUG_BLOCK` 2026-07-28–2026-08-28 |
| Phase16D request plan | 30 logical requests maximum, zero retries, fixed before any data requests |
| Plan SHA-256 | `0e04bcf37947928cbebf155de38e24f56bb1bff5650626d53def6a02c8f5982a` |
| Historical data requests submitted | 0 |
| Prospective data requests submitted | 0 |
| Protected-range breach | `NO` |

The frozen request list covers safe dates selected in the earlier Phase15 request matrix for 2019, 2021, 2022, 2023, 2024, and 2025. It uses frozen Phase4 cohort codes from KOSPI and KOSDAQ; includes repeated requests on 2024-06-03 and 2025-06-02; requests all six daily selector combinations for one sample in each market; and includes the previously documented 003560 / 2023-11-01 and 181340 / 2023-06-01 delisted examples. The full list is in ignored local artifact `runtime/research/phase16d/phase16d-probe-plan.json`.

## Official source and access path

The source is the KRX Data Marketplace screen **[12009] 투자자별 거래실적(개별종목)**, screen ID `MDCSTAT023`, under `통계 → 기본 통계 → 주식 → 거래실적`. The official screen URL is [MDCSTAT023](https://data.krx.co.kr/contents/MDC/STAT/standard/MDCSTAT023.jsp). Retrieval date: 2026-10-06.

The official screen HTML exposes a form with `inqTpCd`, a required security selector (`isuCd`, `isuCd2` and related search-component fields), and `strtDd` / `endDd`. Its own JavaScript returns before search when `isuCd` is blank. The screen exposes the grid service identifiers `MDCSTAT02301` for `기간합계`, `MDCSTAT02302` for daily summary views, and `MDCSTAT02303` for daily detailed views. The dynamic grid uses an AJAX request mechanism. The static page does not disclose the complete wire-level request or a response sample; those remain unverified without an authorized query session.

The UI is per-security: it requires a selected stock and provides no market-wide query control. The screen does not expose a KOSPI/KOSDAQ request field; market is implied by the selected instrument. The page is categorized under stock statistics, while ETF, ETN, ELW, and listed-fund investor pages are separate screens. The actual eligible instruments returned by this specific screen were not tested.

## Source contract and row shape

The exact-date request contract planned for qualification is `strtDd = endDd = D`, with `일별추이` selected so returned rows expose `TRD_DD`. The `기간합계` grid exposes buy/sell/net fields but its visible row template has no date field; it cannot independently prove its own returned session. The date-bearing daily grids expose `TRD_DD` and selected side/measure series. A date-stamped check plus the one-day period-total view was included in the frozen plan to compare their net values.

Because no data payload was received:

- `EXACT_SESSION_CONTRACT=PARTIAL`; no returned-date set, minimum/maximum date, row count, or unexpected date exists to report.
- `UNEXPECTED_DATES=NOT_OBSERVED` means no data response was observed; it does not mean a response passed.
- Pagination, page limits, duplicates, and empty-result meaning remain unknown.
- The returned data cannot be called a full-market table. The UI requires one selected security per search.
- KOSPI and KOSDAQ eligibility remains `PARTIAL`; the frozen plan includes codes from both markets, but no response verified either market.

The adapter in `src/krx_trader/research/phase16d.py` builds only exact-day per-security request descriptions, requires a verified local KRX session before request construction, rejects protected dates before transport, detects unexpected dates in a date-bearing payload, and requires a provider-authorization reference before any future automated transport. It has no network transport, scheduler, or Phase16 append call.

## Screen fields, categories, units, and signs

The screen’s visible period-total grid binds `INVST_TP_NM`, `ASK_TRDVOL`, `BID_TRDVOL`, `NETBID_TRDVOL`, `ASK_TRDVAL`, `BID_TRDVAL`, and `NETBID_TRDVAL`. The daily summary grid binds `TRD_DD`, `TRDVAL1`–`TRDVAL4`, and `TRDVAL_TOT`. The detailed daily grid binds `TRD_DD`, `TRDVAL1`–`TRDVAL11`, and `TRDVAL_TOT`; its labels are 금융투자, 보험, 투신, 사모, 은행, 기타금융, 연기금 등, 기타법인, 개인, 외국인, 기타외국인, 전체.

The summary labels are 기관 합계, 기타법인, 개인, 외국인 합계, 전체. The visible selectors map `askBid` values 1/2/3 to 매도/매수/순매수, `trdVolVal` values 1/2 to 거래량/거래대금, and `inqTpCd` values 1/2 to 기간합계/일별추이.

The screen’s unit controls offer 주/천주/백만주 and 원/천원/백만원/십억원. These are screen display choices; no response established the raw response magnitude or selected unit. No conversion was implemented. All amount and volume fields therefore remain quarantined with `UNKNOWN_SCALE`. The page labels the net fields and controls as 순매수, but no returned values or official sign equation were available; positive/negative numeric sign semantics remain unknown. The complete safe screen-field map is [krx-per-stock-field-map.json](krx-per-stock-field-map.json).

`FIELD_SEMANTICS=PARTIAL`, `UNIT_SEMANTICS=PARTIAL`. Missing fields, explicit nulls, and zero are kept distinct in the local validation helpers. Unknown units cannot be normalized into prospective factor values.

## Publication, corrections, and provider terms

The individual-stock screen itself says that regular-market trades are reflected after the regular close (planned 15:45 KST), and final data including after-hours trades is provided after the session close (planned 20:00 KST). The same screen warns that information may be delayed or erroneous. This is the per-stock screen’s own notice, not a timing rule transferred from aggregate market statistics. A conservative future collector would wait until after 20:30 KST, but no same-day observation was made, so actual availability is unverified. `PUBLICATION_TIMING=DOCUMENTED_SAME_DAY_TIME`; availability still does not qualify the source by itself.

No official correction, revision, finality, or historical-revision policy specific to this screen was found in the reviewed screen materials. `REVISION_POLICY=UNKNOWN`; historical values must not be treated as immutable point-in-time vintages.

The current [KRX homepage terms](https://data.krx.co.kr/contents/MDC/INFO/informationController/MDCINFO003.cmd), effective 2026-08-29, prohibit using automation to collect, copy, or distribute information without authorization and prohibit copying or redistribution without prior KRX permission. The reviewed [market-data usage policy](https://data.krx.co.kr/inc/datasale/Market%20Data%20Usage%20Polices_ko.pdf) is a separate feed-contract policy; it does not establish that automated storage of this screen’s statistical data is permitted. No KRX authorization or account session was provided. Automated exact-day probes and local raw-response retention were therefore not performed. Raw provider responses remain outside Git unless KRX terms clearly permit publication; no raw response exists for this phase.

## Coverage, cost, and integration

| Qualification item | Result |
| --- | --- |
| Earliest successfully reproduced date | `UNKNOWN`; none queried |
| Delisted support | `NOT_AVAILABLE`; the two planned examples were not queried |
| Frozen 100-symbol cohort | `NOT_EVALUATED`; 0 observed matches, 100 missing statuses unknown |
| Current-universe join | `NOT_STARTED`; no prospective flow snapshot |
| Repeatability | `INSUFFICIENT`; no repeated data request |
| Cross-source KRX market-flow comparison | `NOT_PERFORMED`; no per-stock response or qualified units |
| Full-market request count | Not available; the screen requires one selected security and provides no all-market request |
| Frozen cohort, one date-stamped side/measure | 100 security requests by screen granularity |
| Frozen cohort, all buy/sell/net × volume/value daily selectors | Up to 600 requests per session (6 selectors × 100 symbols), before any repeats |
| Pagination / latency / retries | Not measured; no data transport was run |
| Expected runtime | `NOT_MEASURED`; 600 sequential or rate-limited requests are operationally expensive |
| Storage projection | `NOT_COMPUTABLE`; no response rows or bytes-per-row were observed |

The code does not add KRX to the Phase16 prospective source registry and does not append to `runtime/research/phase16/`. It does not change the independent program-flow contract, active aggregate KRX market-flow contract, or rejected KIS per-stock source. No latest-universe snapshot is linked. No revision index or scheduler was changed. `SOURCE_FACTOR_STATUS=QUARANTINED_PROSPECTIVE`.

## Qualification gate matrix and decision

| Gate | Result | Evidence |
| --- | --- | --- |
| A — Official source | PASS | KRX screen [12009], screen ID `MDCSTAT023` |
| B — Exact session | PARTIAL | Exact date controls and date-bearing daily grid are visible; data rows were not received |
| C — KOSPI / KOSDAQ | PARTIAL | One-security selector has no market field; neither market’s payload was verified |
| D — Field semantics | PARTIAL | Official screen labels and bindings observed; response payload not observed |
| E — Unit semantics | PARTIAL | Display unit options observed; raw response scale not observed |
| F — Reproducibility | NOT_VERIFIED | Frozen repeats not run |
| G — Publication availability | DOCUMENTED | Per-stock screen gives planned final-data time after 20:00 KST; actual availability not sampled |
| H — Operational feasibility | FAIL_FOR_FULL_MARKET | Per-security UI; 600 date-bearing selector calls for full field set on the 100-symbol cohort |
| I — Protected-range safety | PASS | Frozen dates are outside protected windows; request builder rejects both windows; zero requests submitted |
| J — Pipeline integration | NOT_STARTED | Source did not qualify; no registry, store, or scheduler changes |
| Provider automation authorization | NOT_PROVIDED | Current KRX terms require authorization for automated collection |

Final: `KRX_PER_STOCK_SOURCE=PARTIAL`, `RECOMMENDED_PER_STOCK_SOURCE=NONE`, `KRX_PER_STOCK_COLLECTION=PARTIAL` / quarantined. KIS trailing per-stock remains `REJECTED_FOR_PROSPECTIVE_USE`.

## Current Phase16 state preserved

Read-only local Phase16 artifacts verified the existing 32-node manifest chain (`PASS`; tip `ee429cc1634b03e5fcf044c32a4c7ef70c77e93469a6f675dc6d1e3795bea025`). Phase16B’s current source table remains: program flow `WAITING_FOR_SAFE_DATE`; KRX aggregate market flow `ACTIVE`; KIS per-stock `ERROR` with its contract review hold. The current Phase16 summary reports 0 per-stock prospective sessions, 0 per-stock multi-vintage sessions, 0 revision comparisons, and Phase17 source stability `NO`. The KRX aggregate flow’s prospective sessions remain separate and are not counted as per-stock observations.

Phase16D added no prospective data or manifests, so the Phase16 chain prefix and all existing Phase16 / 16B / 16C files must remain byte-identical to the pre-work SHA-256 snapshot. The local integrity artifact records the before/after hash check. No Phase5–15 research outcomes were read or changed. Phase15 request-manifest metadata was used only to preselect safe probe dates.

## Questions and exact next action

1. **Official access path?** KRX Data Marketplace `[12009] 투자자별 거래실적(개별종목)`, screen `MDCSTAT023`; its HTML defines per-security form fields and `MDCSTAT02301/02/03` grid services.
2. **Does D–D return exactly D?** Not established; no authorized data response was obtained.
3. **Unexpected date?** None observed because no data response was received.
4. **Market or symbol request?** One selected security is required by the page; no market-wide UI request.
5. **Pagination?** Unknown.
6. **KOSPI and KOSDAQ?** Both are represented in the frozen plan; actual service support remains unverified.
7. **Instrument types?** Screen is in stock statistics; actual returned scope is unverified. Other product classes have separate screens.
8. **Stable identifiers?** The form exposes provider-code and short-code inputs; the returned identifier values and ISIN availability are unverified.
9. **Investor categories?** The page labels summary and detailed categories as listed above.
10. **Buy/sell/net?** Period grid visibly binds all three sides for volume and value; daily views select one side at a time.
11. **Exact units?** Display options are shares / thousand shares / million shares and won / thousand won / million won / billion won; raw scale remains unknown.
12. **Sign semantics?** Labels say sell, buy, and net buy; numeric sign convention is unverified.
13. **When knowable?** The individual screen says final after-hours-inclusive data is expected after 20:00 KST.
14. **Same-day defensible?** Only for post-publication observation, not a same-session trading decision; no live timing sample was obtained.
15. **T+1 defensible?** A next-session use after a post-20:30 observation is plausible but unverified and not authorized for collection.
16. **Repeat requests identical?** Not tested.
17. **Revision policy?** Unknown.
18. **Historical depth?** No KRX rows queried; earliest verified date unknown.
19. **Delisted examples?** Not queried; support not available.
20. **Frozen cohort coverage?** Not evaluated; 0 of 100 observed, missing counts unknown.
21. **Daily requests?** One security per request; 100 requests for one selected metric across the frozen cohort, or up to 600 for all six daily side/measure selectors.
22. **Runtime/storage?** Runtime and bytes are unmeasured; no response data or row-size estimate exists.
23. **Can it replace KIS?** Not yet. KIS remains rejected and recommended per-stock source remains none.
24. **Prospective observation persisted?** No.
25. **Per-stock prospective sessions?** 0.
26. **Stable enough for factor research?** No; source remains quarantined.
27. **Next action?** Provide confirmation of KRX authorization for automated queries and permitted local research retention. Then execute only the frozen 30-request plan, verify actual returned dates/schema/units for both markets, and keep the source quarantined until repeatability and coverage gates pass.

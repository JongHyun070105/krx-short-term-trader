# Phase 16B — Flow Activation, Response Contracts, and Revision Sampling

## Outcome

Phase 16B extends the Phase 16 append-only evidence store with guarded flow collection and revision monitoring. No strategy, factor profitability, forward-return, IC, or PnL analysis was performed.

As of 2026-10-02 14:29 KST, the KIS per-stock and program-flow contracts are empirically verified over bounded historical samples, but their prospective collectors remain `WAITING_FOR_SAFE_DATE`: the 31-session and 30-session response envelopes still touch the protected 2026-07-28–2026-08-28 range. The exact-day KRX market-investor route has a separate empirically verified contract and is `ACTIVE`. It has one prospective provider session (2026-10-01), observed on 2026-10-02 at 13:18:58 KST. No KIS prospective flow has been collected.

`PHASE17_SOURCE_STABILITY_READY=NO`. One KRX session is source evidence, not enough for the Phase 17 stability review. All collected fields remain quarantined from predictive use.

## Identity and integrity

| Item | Result |
|---|---|
| Starting `main` SHA | `c1243bc8cb311cedb18045e302d5960af00179db` |
| Test baseline before implementation | 401 passed |
| Final full-suite verification | 436 passed; Ruff, `py_compile`, and `git diff --check` passed |
| Pre-Phase16B snapshots | 2 unchanged; payload and manifest hashes match the captured baseline |
| Phase 5–15 artifacts | 850/850 unchanged; snapshot SHA-256 `061a12bd8108310f920cd0b3148c1e41b297c7522576d49baf49c31575ba14d2` |
| Phase16 chain after probes and first flow | valid, 22 snapshots; tip `cd39560d6abdb88283e199bed2da464b989d7d96d9a447c60d9f3fbbd58ae508` |
| Current config | `phase16b-v4`; SHA-256 `85436bfa28e57367f417d5cf102f3a7112aff24309907b7d012d2a31dcf7c91e` |
| Config parent | v3 SHA-256 `ec7a6ced0d9d04bbf90311e9ad2d19a38322c24b35f7c7a0d7987772cbe6bb2c` |
| First KRX flow snapshot config | v3 SHA-256 `ec7a6ced0d9d04bbf90311e9ad2d19a38322c24b35f7c7a0d7987772cbe6bb2c`; the manifest keeps its collection-time config |
| Original Phase16 config | remains present; Phase16B v1 records it as parent and later config revisions chain from it |
| Deterministic evidence export | two exports matched; SHA-256 `5c6e22929cf6843fd82b0b0d3f6bb7e90cdebdac3fac8420bd9e5802bd125438` |
| Changed-file credential scan | zero findings |

New Phase16B code persists actual `response_snapshot_id` values in newly normalized rows. The initial exact-day bootstrap response predates that final wiring and retains its deterministic response-reference hash; revision attribution resolves it through its containing immutable manifest. Its manifests record the starting Git HEAD and `collector_git_dirty=true`, because the observation was collected before the implementation was committed. Neither original Phase16 snapshot nor any existing response payload was rewritten.

## Source response contracts

### KIS per-stock investor flow

- Endpoint: `/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily`.
- Date field: `stck_bsop_date`.
- Requested date behaves as an upper-bound anchor in the sampled responses. The anchor was included; no returned row was later than the anchor.
- Responses were backward-looking, ordered newest to oldest, with up to 31 dated rows per sampled one-page response.
- 9 new fixed contract probes plus 50 Phase 15 observations were assessed. The 9 probe runs covered 3 fixed symbols and 3 anchors (`2019-06-03`, `2022-06-02`, `2025-06-02`). All probe responses were one page and at or below 31 rows.
- The 31-row value is an **empirical operational envelope**, not a vendor-documented permanent maximum. The collector makes one page only. A continuation beyond that page, an over-cap response, a future date, schema drift, or an out-of-envelope row is a contract violation and disables this source pending review.
- The shared KIS client understands its continuation header, but all bounded live probes returned one page. The flow collector caps the request at one page; a continuation response raises a contract violation rather than fetching another page.
- Returned windows span multiple months. The fixed samples did not cross a year boundary, so no year-boundary sample is claimed.
- Saturday, Sunday, holiday, and other unverified non-session anchors are denied before transport. Previous-session fallback has not been established.
- Contract confidence: `EMPIRICALLY_VERIFIED`, v2, 59 combined observations.
- First safe anchor: **2026-10-16**. The full 31-session envelope at that anchor starts after the protected range. As of Oct 2, it has not been reached; the source is `WAITING_FOR_SAFE_DATE`.

### KIS program trading

- Endpoint: `/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily`.
- Date field: `stck_bsop_date`.
- The requested date behaved as an inclusive upper-bound anchor. Sampled rows were backward-only and descending.
- 9 new probes plus 3 Phase 15 observations; maximum sampled response was 30 dated rows, one page per probe.
- As for per-stock flow, 30 is a sampled operational envelope, not an official permanent cap. Collection is one page and fails closed on pagination or response drift. Non-session anchors are denied.
- The program endpoint shares the client's continuation handling; probes observed one page only, and the collector does not request additional pages.
- Contract confidence: `EMPIRICALLY_VERIFIED`, v2, 12 combined observations.
- First safe anchor: **2026-10-15**. State on Oct 2: `WAITING_FOR_SAFE_DATE`. Program activation is independent of per-stock flow.

### KIS market flow

The existing 300-row sampled window has no conservative finite span contract. It remains `DEFERRED_RESPONSE_CONTRACT` and is not activated.

### Official KRX exact-day market-investor statistics

The KRX Data Marketplace route `MDCSTAT022` provides a date range and market selector. The adapter sends the same requested session as both range endpoints, separately selects KOSPI (`STK`) and KOSDAQ (`KSQ`), and verifies exactly one returned session per market. Two bounded historical requests (one per market, 2025-06-02) returned that same session. This source is distinct from KIS and is not substituted for it. KRX describes sell, buy, and net statistics for trading volume and value and says final same-day transaction details are available after 20:00; the collector observes at 20:30 or later. Raw field-category mapping and units remain unresolved, so fields stay quarantined. [KRX Data Marketplace](https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT022)

- Contract: exact requested session, one dated row, `TRD_DD`, separate market selection; `EMPIRICALLY_VERIFIED`, v1.
- First safe anchor: **2026-08-31**.
- State on Oct 2: `ACTIVE`.
- KIS market flow remains a separate deferred source.

## Calendar, guard, and activation

The current KRX calendar is configuration-versioned (`KRX-STOCK-2026-POST-HOLDOUT-v1`) and covers 2026-07-01 through 2026-12-30. It excludes weekends and configured official market closures, including the 2026 Chuseok closure, Oct 5, Oct 9, and the year-end closure. The config links the KRX calendar notice and the official KRX trading-day rules; KRX rules also state that exchange holidays and Saturdays are closed. [KRX trading-day rules](https://global.krx.co.kr/contents/GLB/06/0606/0606030101/GLB0606030101T3.jsp)

`possible_response_session_range` counts KRX sessions, not calendar days. `first_safe_anchor_session` scans configured sessions and returns the first whose entire envelope excludes all protected intervals. If the configured calendar does not cover the current day or a request anchor, preflight fails closed. Future calendar coverage requires a new config revision; no guessed session dates are introduced.

Before transport, each request checks the full envelope, the anchor, the contract state, and the protected ranges. Unknown non-session behavior is denied. Returned rows are checked immediately for row cap, date field, exact/upper-bound behavior, future dates, session membership, ordering, envelope membership, and protected dates. A breach is hashed and quarantined without persisting the response body, stops further requests for that source in the batch, and changes future source state to `ERROR` / contract review required.

Auto-activation is enabled in v4 for the eligible sources. At each scheduled invocation the collector runs preflight, reads the latest completed session, verifies store/collector health and credentials, then transitions to `ACTIVE` only when the contract and safe-anchor checks pass. Before then it records `WAITING_FOR_SAFE_DATE` and makes no flow transport request. KRX exact-day collection is already active. When calendar coverage is unknown, no stale prior session is reused.

## Evidence and revision model

- Provider session (event date), request anchor, and UTC/KST `observed_at` are separate fields. An observation of a 2026-09-15 row on 2026-10-20 means the system saw that provider value on Oct 20; it does not establish what was knowable on Sep 15.
- Every response is an immutable Phase16 snapshot; the snapshot manifest includes source, contract, config, request anchor, observed time, hashes, and latest universe snapshot link. Normalized rows retain provider-native keys, carry logical keys `(source_name, symbol, provider_session)`, and label semantic/unit status `UNKNOWN` where unresolved.
- Current and future flow fields use `QUARANTINED_PROSPECTIVE`; no Phase14 factor registry promotion occurs.
- Evidence classes stay distinct: `CONTRACT_VERIFICATION_PROBE`, prospective session rows, historical rows re-observed in a prospective response, and `HISTORICAL_REVISION_PROBE`.
- Revision output separates full-response hash changes from row changes. Supported classifications include identical, semantic-only, value, field/schema, missing/present, and uncomparable states. The revision curve records first/latest observation, vintages, revisions, revision age, and identical streak. Field profiles preserve unknown semantics and units.
- The frozen higher-frequency flow probe cohort is 6 KOSPI + 6 KOSDAQ symbols; its membership SHA is validated before scheduled collection. The full prospective cohort is the frozen Phase4 cohort of 100 symbols.
- The weekly old-session revision job is fixed to one KIS per-stock symbol and 2025-06-02 anchor. It is scheduled Fridays at 20:45 KST and had not run as of this report.

Current observation counts:

| Metric | Count |
|---|---:|
| KIS contract-verification responses | 18 (9 per endpoint) |
| KRX exact-day contract probes | 2 (KOSPI and KOSDAQ) |
| Prospective flow response snapshots | 2 |
| Prospective flow provider sessions | 1 (KRX 2026-10-01) |
| KIS per-stock prospective sessions | 0 |
| Sessions with multiple prospective vintages | 0 |
| Historical revision-probe snapshots | 0 |
| Revision comparisons / value revisions / schema changes | 0 / 0 / 0 |
| Per-stock field revision profile | No comparisons yet; no field has a measured revision rate |

## Scheduling, health, and storage

The five Phase16B LaunchAgents are installed and loaded. Each calls the guarded `collect-active-flow` command, which performs its own preflight. A Saturday, Sunday, configured closure, or out-of-calendar invocation returns `NO_COMPLETED_MARKET_SESSION` without provider transport.

| Agent | Local schedule |
|---|---:|
| Existing current-listing collector | Daily 20:15 |
| Flow probe close | 16:20 |
| Flow probe evening | 20:20 |
| Flow full cohort / exact-day KRX | 20:30 |
| Flow probe next morning | 08:20 |
| Historical revision probe | Friday 20:45 |

The KIS client uses the shared `runtime/kis_rate_limit.json` limiter at a 4-second minimum interval, memory-only token caching for the bounded contract runner, and no retries. A collector lock serializes scheduled collection, while the evidence store and revision-index locks protect manifests and revision output.

The operational storage projection assumes 60 KB per KIS response and 4 KB per exact-day KRX market response. Once all configured daily sources are eligible, the model is 238 requests and **14.168 MB per active day**, **425.297 MB per 30-day month**, and **5.174 GB per 365-day year**, including one weekly historical probe. These are planning assumptions to replace with measured sizes after safe KIS activation. Retention remains append-only; no automatic deletion or compression is configured.

Latest universe evidence: current-listing LaunchAgent is active at 20:15; the latest observed listing snapshot contains 3,544 listings and frozen cohort snapshot contains 100 members. Its observed state is maintained separately from any legal delisting inference. The latest exact-day KRX flow batch links the latest known universe snapshot. Current exact-day KRX coverage is 2/2 market requests; no full-cohort KIS coverage exists yet.

Health at 2026-10-02 14:17 KST was `PASS_WITH_WARNINGS`; the warnings were expected insufficient source-stability evidence, not-yet-measured full-cohort coverage, and no scheduled flow invocation yet (the first installed slot is later today). LaunchAgent checks passed, the chain was valid, storage status was OK, and no contract violation or provider error was present. Health also checks stale sources, repeated provider errors, coverage, schema drift, disk thresholds, current timezone, source eligibility, and agent load state. Actual clock drift was not measured because health does not query an independent time source.

## Export and Phase 17 gate

The Phase16 evidence export retains the existing immutable store as its single evidence truth. It includes the manifest chain, permissible normalized payloads, raw/canonical/normalized hashes, revision indexes, contract metadata, config versions, and chain witness; credential files, tokens, and limiter state are excluded. Archive contents use deterministic metadata for reproducibility.

Phase 17 source-stability readiness requires at least 20 completed **KIS per-stock prospective provider sessions**, at least 10 of those sessions with repeated vintages, a valid chain, acceptable full-cohort coverage (>=95%), and no unresolved schema drift. Current state is `NO`: KIS has not activated, full-cohort coverage is not yet measured, and no repeated prospective vintages exist. KRX exact-day data does not substitute for the per-stock source gate.

## Required status matrix

| Field | Value |
|---|---|
| `PHASE16B` | `COMPLETE` (infrastructure and bounded KRX observation); KIS collection awaits safe anchors |
| `PER_STOCK_FLOW_CONTRACT` | `VERIFIED` empirically; sampled 31-row one-page envelope, not an official permanent cap |
| `PER_STOCK_FLOW_STATE` | `WAITING_FOR_SAFE_DATE` |
| `FIRST_SAFE_PER_STOCK_ANCHOR` | `2026-10-16` |
| `PROGRAM_FLOW_CONTRACT` | `VERIFIED` empirically; sampled 30-row one-page envelope, not an official permanent cap |
| `PROGRAM_FLOW_STATE` | `WAITING_FOR_SAFE_DATE` |
| `FIRST_SAFE_PROGRAM_ANCHOR` | `2026-10-15` |
| `MARKET_FLOW_EXACT_DAY` | `AVAILABLE` via separate official KRX route |
| `MARKET_FLOW_STATE` | `ACTIVE` |
| `FIRST_SAFE_MARKET_ANCHOR` | `2026-08-31` |
| `FLOW_PROSPECTIVE_FIRST_OBSERVED_AT` | KRX exact-day: `2026-10-02T13:18:58+09:00`; KIS per-stock: `NOT_STARTED` |
| `FLOW_PROSPECTIVE_SESSIONS` | `1` |
| `FLOW_MULTI_VINTAGE_SESSIONS` | `0` |
| `FLOW_REVISION_COMPARISONS` / `FLOW_VALUE_REVISIONS` | `0` / `0` |
| `UNIVERSE_COLLECTION` | `ACTIVE` |
| `MANIFEST_CHAIN` | `PASS` |
| `PREEXISTING_PHASE16_EVIDENCE_INTEGRITY` | `PASS` |
| `PHASE17_SOURCE_STABILITY_READY` | `NO` |
| `EXTERNAL_2026` / `HOLDOUT_2026` | `NOT_READ` / `NOT_READ` |
| `ALPHA` / `SHADOW_NEXT_SESSION` / `LIVE` | `UNPROVEN` / `NO` / `DISABLED` |

## Answers to the Phase 16B questions

1. **Per-stock response contract:** empirical backward-looking upper-bound date anchor, inclusive in all sampled responses, `stck_bsop_date`, descending order, session-dated output, no future row observed, one page in the bounded sample.
2. **Maximum rows:** 31 observed in 59 combined historical observations; not a permanent provider guarantee.
3. **Upper-bound anchor:** yes, empirically in the sample.
4. **Newer than anchor:** none observed; future dates cause a contract violation.
5. **Non-session date:** not live-probed; request is denied unless previous-session fallback is independently verified.
6. **First safe per-stock date:** 2026-10-16; not reached as of Oct 2.
7. **Per-stock automatic activation:** configured and scheduled; preflight makes no flow request while unsafe.
8. **Program contract:** same backward inclusive anchor behavior in the sample, descending, at most 30 rows, one page observed; non-session requests denied.
9. **First safe program anchor:** 2026-10-15; not reached as of Oct 2.
10. **Exact-day KRX source:** found and empirically reproduced for one exact requested session per separate KOSPI/KOSDAQ request.
11. **Active market source:** the separate KRX exact-day route. KIS 300-row market flow remains deferred.
12. **Prospective sessions / repeated sessions / value revisions:** 1 / 0 / 0, from the KRX exact-day source; KIS per-stock prospective sessions are 0.
13. **Most revised fields:** none measured yet; there are no vintage comparisons.
14. **Unresolved semantics:** KIS `orgn` and units/publication timing; KRX response field-category mapping and units. All stay unknown/quarantined.
15. **Existing evidence integrity:** yes, both pre-Phase16B snapshots and the Phase5–15 artifacts are unchanged.
16. **Manifest chain:** valid, 22 nodes.
17. **Installed agents:** current-listing at 20:15 plus five flow agents at 16:20, 20:20, 20:30, 08:20, and Friday 20:45 KST.
18. **Unsafe source behavior:** preflight records the response envelope and waiting/deferred state, then stops before transport.
19. **Contract violation behavior:** quarantine/hash only, stop the source batch, enter contract review, and deny later requests until the contract is revised and reverified.
20. **Storage projection:** 14.168 MB/active day, 425.297 MB/30-day month, 5.174 GB/year under the stated planning assumptions.
21. **Phase17 review readiness:** no; prospective and repeated KIS per-stock sessions plus full-cohort coverage are absent.
22. **Next action:** let the guarded schedule continue preflighting. Program flow can first become eligible on Oct 15, and per-stock flow on Oct 16, subject to that day being a completed KRX session, credentials, healthy collector, and a still-valid contract.

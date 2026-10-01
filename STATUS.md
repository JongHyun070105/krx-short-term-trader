# Project status

```text
ALPHA: UNPROVEN
MEAN_REVERSION_FAMILY: INSUFFICIENT (anatomy gate did not support MR-A or MR-B creation on Development data)
MR_A: INSUFFICIENT (Development anatomy did not show positive rebound beating blind laggard selection)
MR_B: INSUFFICIENT (same anatomy gate failure; stabilization + rank recovery did not outperform blind laggards)
RELATIVE_STRENGTH_FAMILY: REJECTED (gross-negative across Development and Secondary diagnostic)
RS_A: REJECTED (gross exp -0.296%, net exp -0.823%, PF 0.44)
RS_B: REJECTED (gross exp -0.053%, net exp -0.581%, PF 0.57)
BREAKOUT_FAMILY: REJECTED (Phase 3 v1/v2 and Phase 4 Retest evidence)
BREAKOUT_V2: NONE (no Validation candidate survived)
BREAKOUT_RETEST_A: REJECTED
BREAKOUT_RETEST_B: INSUFFICIENT
PULLBACK: TOO_RARE / DEPRIORITIZED
OOS: FAILED_GENERALIZATION_NO_EDGE_AFTER_COST (V2-A; locked Fresh Holdout untouched)
DATA_QUALITY: PARTIAL (Phase 8 safe cache: 47 complete / 1 partial / 12 not acquired; 0 synthetic minutes; no holdout price payloads used; incidental sidecar metadata exposure documented)
COHORT_BREADTH: 47_COMPLETE / 60_TARGET (Phase 8; 30 KOSPI + 17 KOSDAQ complete; KIS limiter respected)
PHASE6_DATA_ACQUISITION: PARTIAL (46 complete, 1 partial,13 not acquired of 60 target; KIS rate-limit timeouts)
PHASE6_ARTIFACT_INTEGRITY: DEGRADED_RECONCILED (Phase 7 replaced ignored acquisition manifest; original unavailable; historical index and summary preserved)
PHASE5_DATA_ACQUISITION: PARTIAL_INTERRUPTED (36 complete, 215000 has 30/67 sessions, 23 not started; manifest RUNNING is stale)
PHASE5_ARTIFACT_INTEGRITY: RECONCILED
EXTERNAL_VALIDATION: NOT_AVAILABLE (no Secondary-surviving candidate; untouched block not opened)
LOCKED_FRESH_HOLDOUT: LOCKED_NOT_EVALUATED (2026-07-28..08-28; no price payloads evaluated; incidental sidecar metadata exposure documented)
SHADOW_NEXT_SESSION: NO (NOT_PROMOTED)
SHADOW_INFRA: PASS (2026-09-28 four-hour read-only simulated session)
PAPER: OUT_OF_SCOPE
LIVE: DISABLED
PHASE9_PRICE_SEMANTICS: PARTIAL (daily CONFIGURABLE adjusted/raw; minute UNKNOWN)
PHASE9_DAILY_MINUTE_ALIGNMENT: PASS (opens exact; closes expected closing-auction difference)
PHASE9_TIMESTAMP_SEMANTICS: PASS
PHASE9_CACHE_INTEGRITY: PARTIAL (606 absent Development partitions)
PHASE9_EVIDENCE_INTEGRITY: DEGRADED_BUT_USABLE
RESEARCH_PLATFORM: READY_WITH_LIMITATIONS
PHASE9: COMPLETE
PHASE10_OPPORTUNITY_MAP: FAIL (no broad state achieved 1% gross mean or positive net after costs)
DAILY_A: NOT_CREATED (no qualifying family; best state gross -0.32%, payoff 0.83)
PHASE10_SECONDARY: NOT_RUN
PHASE10_EXTERNAL: NOT_RUN
MULTIYEAR_DATA: COMPLETE (2023-01-02..2025-12-30; 731 common sessions; 90/100 dense multi-year symbols; CURRENT-LISTING SURVIVORSHIP BIAS)
PHASE11_ACQUISITION: PARTIAL (99/100 symbols returned rows; KIS returned no daily rows for 282620; both market indexes complete)
FLOW_DATA: NOT_AVAILABLE (no reproducible multi-year point-in-time per-symbol panel)
PHASE11_FACTOR_MAP: PARTIAL (price/liquidity/market context complete; flow unavailable)
PHASE11_CANDIDATE: REJECTED (one frozen 3-session market-context mean-reversion research candidate failed Validation)
PHASE11_VALIDATION: FAIL
PHASE11_CONFIRMATION: NOT_RUN
PHASE11_PREVIOUS_PHASE_MANIFEST_INTEGRITY: PASS (20 Phase 5–10 manifests/indexes unchanged)
PHASE11_ARTIFACT_INTEGRITY: PASS
PHASE11_EXTERNAL_2026: NOT_READ
PHASE11_HOLDOUT_2026: NOT_READ
PHASE11_REPORT_EXECUTION_WINDOW: NOT_REPRODUCED_IN_REPOSITORY (report generator path unavailable)
PHASE12_FAILURE_ANATOMY: COMPLETE (Phase 11 decay is predominantly matched-market rebound; no stable new mechanism passed)
MARKET_BETA_EXPLANATION: STRONG
STRESS_COMPOSITION_SHIFT: STRONG (20d volatility SMD +0.909; dispersion SMD +0.662)
MARKET_STRESS_MECHANISM: NOT_SUPPORTED
PHASE12_CANDIDATE: NOT_CREATED
PHASE12_CONFIRMATION: NOT_RUN
PHASE12_EXTERNAL_2026: NOT_READ
PHASE12_HOLDOUT_2026: NOT_READ
PHASE12_ARTIFACT_INTEGRITY: PASS (23 indexed artifacts; Phase 5–11 manifest/index snapshot 23/23 unchanged)
PHASE12_TESTS: PASS (268 total; baseline 252 plus 16 Phase 12 tests)
PHASE13_POINT_IN_TIME_UNIVERSE: PARTIAL (official lifecycle references found; complete dated membership/lineage not verified)
PHASE13_PIT_PRICE_HISTORY: NOT_AVAILABLE (delisted adjusted-price source not verified)
PHASE13_SURVIVORSHIP_SENSITIVITY: NOT_TESTABLE (current-listing cohort only)
PHASE13_FACTOR_MAP: PARTIAL (96/100 frozen cohort symbols have usable safe-window prices)
PHASE13_RESIDUAL_MECHANISM: WEAK (Discovery/Touched residual intervals cross zero; Confirmation reversed)
PHASE13_CANDIDATE: REJECTED (one preregistered KOSPI excess-reversal × expanded-turnover rule failed Confirmation)
PHASE13_CONFIRMATION: FAIL (105 non-overlap trades; gross, net, excess, residual, and date-concentration gates failed)
PHASE13_PRE_CONFIRMATION_FREEZE_SHA: 8a828629f0b81e9b1e4e46aeb2398c127afed263
PHASE13_EXTERNAL_2026: NOT_READ
PHASE13_HOLDOUT_2026: NOT_READ
PHASE13_ARTIFACT_INTEGRITY: PASS (28 indexed artifacts; Phase 5–12 snapshot unchanged)
PHASE13_TESTS: PASS (297 total; 268 baseline + 29 Phase 13)
PHASE14_RESEARCH_LEDGER: COMPLETE (9 strategy / 11 factor / 4 interaction family labels minimum)
PHASE14_PIPELINE_V2: PASS (63 factor definitions, 14 families; strategy-free)
PHASE14_FACTOR_VIABILITY: WEAK (no PROMISING_FOR_FUTURE_STUDY family; best NONE)
PHASE14_FALSE_POSITIVE_AUDIT: PASS (Phase 13 interaction family WEAK pre-Confirmation)
PHASE14_EXISTING_INFORMATION: WEAK
PHASE14_NEXT_RESEARCH_MODE: NEW_INFORMATION_SOURCE
PHASE14_EXTERNAL_2026: NOT_READ
PHASE14_HOLDOUT_2026: NOT_READ (outcomes unread; metadata previously exposed)
PHASE14_ARTIFACT_INTEGRITY: PASS (25 indexed payloads; Phase 5–13 snapshot 38/38 unchanged)
PHASE14_TESTS: PASS (323 total; baseline 297 plus 26 Phase 14)
PHASE15_SOURCE_AUDIT: PARTIAL (60 bounded KIS request records; all successful; no source passed full qualification)
PHASE15_PER_STOCK_FLOW: PARTIAL (31 rows/date anchor; earliest returned 2018-04-18; 7/7 repeat payloads revised)
PHASE15_MARKET_FLOW: PARTIAL (300 rows/date anchor; selected repeat identical; market aggregate only)
PHASE15_PROGRAM_TRADING: PARTIAL (30 rows/date anchor; selected repeat identical)
PHASE15_SECTOR_CONTEXT: PARTIAL
PHASE15_SECTOR_PIT_MEMBERSHIP: NOT_AVAILABLE
PHASE15_CORPORATE_EVENTS: PARTIAL (receipt date day-resolution; original/correction chain incomplete)
PHASE15_HISTORICAL_MICROSTRUCTURE: NOT_AVAILABLE
PHASE15_PROSPECTIVE_MICROSTRUCTURE: AVAILABLE
PHASE15_POINT_IN_TIME_UNIVERSE: PARTIAL
PHASE15_DELISTED_PRICE_HISTORY: PARTIAL (two documented symbols returned tested 2023 rows)
PHASE15_SYMBOL_LINEAGE: PARTIAL
PHASE15_FRESH_HISTORICAL_EVIDENCE: PARTIAL (2019–2022 plan conditional on prior-phase manifest reconciliation)
PHASE15_PRIMARY_SOURCE: NONE
PHASE15_SECONDARY_SOURCE: NONE
PHASE15_NEXT_RESEARCH_MODE: PROSPECTIVE_DATA_COLLECTION
PHASE15_EXTERNAL_2026: NOT_READ
PHASE15_HOLDOUT_2026: NOT_READ
PHASE15_ARTIFACT_INTEGRITY: PASS (28 indexed payloads; Phase 5–14 snapshot 48/48 unchanged)
PHASE15_TESTS: PASS (366 total; baseline 323 plus 43 Phase 15 tests)
PHASE15_RUFF: PASS
PHASE15_DIFF_CHECK: PASS
PHASE15_CREDENTIAL_SCAN: PASS (0 credential-pattern findings in changed source/docs)
PHASE15_ALPHA: UNPROVEN
PHASE15_SHADOW_NEXT_SESSION: NO
PHASE15_LIVE: DISABLED
VWAP_RECLAIM_ANATOMY: FAIL (15m Development gate passed 2/5; no variants created)
VWAP_A: NOT_CREATED
VWAP_B: NOT_CREATED
VWAP_RECLAIM_FAMILY: REJECT
PHASE7_SECONDARY_DIAGNOSTIC: NOT_RUN (no Development candidate)
PHASE7_EXTERNAL_VALIDATION: NOT_AVAILABLE (no frozen Secondary survivor; block unopened)
PHASE7_HOLDOUT: LOCKED_NOT_EVALUATED (2026-07-28..08-28; 0 partitions opened)
PHASE7_SHADOW_NEXT_SESSION: NO
PHASE7_SAFE_CACHE: 46_COMPLETE / 2_PARTIAL / 12_NOT_ACQUIRED (3,174 partitions; 36 added during bounded read-only KIS backfill)
PHASE7_ACQUISITION: PARTIAL_INTERRUPTED_RATE_LIMIT (shared interval increased to 4s; resumable)
PHASE8_OPENING_GAP_ANATOMY: FAIL (Development descriptive anatomy did not support a repeatable cost-sized edge)
PHASE8_OPENING_GAP_FAMILY: REJECT
PHASE8_GAP_A: NOT_CREATED (14 diagnostic events; Development gates failed)
PHASE8_GAP_B: NOT_CREATED (6 diagnostic events; Development gates failed)
PHASE8_SECONDARY: NOT_RUN (no Development survivor)
PHASE8_EXTERNAL: NOT_AVAILABLE (no frozen Secondary survivor; 0 partitions opened)
PHASE8_HOLDOUT: LOCKED_NOT_EVALUATED (2026-07-28..08-28; 0 price-data partitions opened; incidental sidecar metadata exposure recorded)
PHASE8_SHADOW_NEXT_SESSION: NO
```

Phase 2, Phase 2.5, Phase 3, Phase 4, and Phase 5 evidence is summarized in [RESULTS.md](RESULTS.md). Manifests, event rows, and Phase 5 diagnostics are retained in ignored `runtime/research/phase5/`. Phase 6 research artifacts are in ignored `runtime/research/phase6/`.

## Phase 15 current decision

Phase 15 source qualification is `PARTIAL`. The official KIS per-stock investor-flow endpoint returned historical rolling windows for the bounded 2018–2025 samples, but all seven same-request repeat pairs produced different canonical response hashes. KIS market-flow and program-trading repeats were identical in one sample each; both also returned rolling dated windows. Publication timestamps, historical vintages, complete coverage, and provider rate limits remain unresolved. No source qualifies for Phase 14 factor research, so the primary and secondary sources are `NONE`; the recommended next mode is prospective collection with locally frozen, timestamped vintages after terms are confirmed. No alpha study or strategy candidate was run.

`FRESH_HISTORICAL_EVIDENCE=PARTIAL`: sampled KIS flow reaches 2018-04-18. The proposed 2019–2020 discovery, 2021 replication, and 2022-01–10 confirmation periods are conditional on complete source acquisition and reconciliation of earlier Phase 3–9 usage. Phase 11's 2022-11–12 warmup stays disclosed. `EXTERNAL_2026=NOT_READ`; the 2026 Holdout remains outcome-unread and excluded; `ALPHA=UNPROVEN`, `SHADOW_NEXT_SESSION=NO`, and `LIVE=DISABLED`.

See the [Phase 15 report](docs/research/phase15-new-information-source-qualification.md) and ignored machine-readable artifacts under `runtime/research/phase15/`. All 48 snapshotted Phase 5–14 artifacts remain unchanged.

## Phase 14 current decision

Phase 14 Research Pipeline v2 is implemented and run using only the Phase 13 safe panel through 2025-06-30. The pipeline found descriptive and selection-aware IC structure, but no factor family passed the full pre-strategy viability gate. `BEST_FACTOR_FAMILY=NONE`; existing price/liquidity information is `WEAK`; the next research mode is `NEW_INFORMATION_SOURCE`. Its pre-Confirmation audit classified the Phase 13 interaction family `WEAK`, which would have blocked the candidate from Confirmation. No strategy or candidate was created. See the [Phase 14 report](docs/research/phase14-research-pipeline-v2.md) and machine-readable `runtime/research/phase14/` artifacts. External 2026 and Holdout outcomes remain unread; Holdout metadata exposure remains disclosed.

## Phase 9 current decision

- Phase 9 audited KIS daily and minute price semantics, alignment, cache integrity, and protected-data guards on Development data (2026-04-17–06-30). Daily prices use `FID_ORG_ADJ_PRC=0` (adjusted) for Phase 10; minute adjustment remains UNKNOWN. Open alignment is exact (KOSPI 98.9%, KOSDAQ 100%); close alignment reflects the expected closing-auction difference (KOSPI median 0.18%, KOSDAQ 0.24%). Timestamp semantics PASS. Cache integrity PARTIAL (606 absent Development partitions of 2,940 expected; 347,583 missing minute rows; cause UNKNOWN).
- 94 suspicious Development gaps (>=20%) are classified ADJUSTMENT_MISMATCH: raw and adjusted daily prices for the same stock have different absolute scales (ratio cluster ~0.1), producing spurious gap magnitudes when mixed. All 94 are from 2 KOSDAQ symbols (154040, 208860).
- Phase 8 evidence integrity: DEGRADED_BUT_USABLE. The 92 original Phase 8 suspicious gaps were from a raw-price search; Phase 9's adjusted-price search found 94 (the same set plus 2 additional sessions). The Phase 8 corrected replay was performed (DATA_CORRECTED_REPLAY).
- Previous-phase manifest hashes verified unchanged. Phase 6 artifact integrity remains DEGRADED_RECONCILED.
- Protected Holdout payload/sidecar reads: 0/0. Representative refresh comparison: 3 partitions showed PERSISTENT_PROVIDER_SHAPE (identical re-fetch).
- Research platform: READY_WITH_LIMITATIONS. Phase 10 is authorized to start.
- Verdicts: `PRICE_SEMANTICS=PARTIAL`; `DAILY_MINUTE_ALIGNMENT=PASS`; `TIMESTAMP_SEMANTICS=PASS`; `CORPORATE_ACTION_HANDLING=PARTIAL`; `CACHE_INTEGRITY=PARTIAL`; `PHASE8_EVIDENCE_INTEGRITY=DEGRADED_BUT_USABLE`; `RESEARCH_PLATFORM=READY_WITH_LIMITATIONS`; `PHASE9=COMPLETE`.

## Phase 10 current decision

- Phase 10 was a **49-session Development study**. It found no cost-sized edge in that short window; this is not universal evidence against daily strategies.
- Phase 10 mapped daily-level trailing return states, volatility, range position, price, and liquidity against 2/3/5-session forward outcomes on Development data only (2026-04-17–06-30, 60 symbols, 2,940 observations, 11,100 outcomes). Source: Phase 9 adjusted daily cache (`FID_ORG_ADJ_PRC=0`). Entry: next-session open after completed signal day. Exit: fixed horizon close. No parameter search or ML.
- The best broad state was 3-day trailing return bucket "-2% to 0%" at 2-day horizon (n=344/275 non-overlapping): gross mean −0.32%, win rate 36.4%, payoff ratio 0.83. No state achieved the 1.0% gross promotion threshold. Monthly: April +0.65%, May −1.66%, June +0.55%. Market: KOSPI −0.14%, KOSDAQ −0.69%. All cost multipliers (1×, 1.5×, 2×) were net-negative.
- No qualifying family was found. The 0.53% round-trip cost exceeds every observed positive gross effect. Momentum vs. reversal: neither produced a cost-sized edge. Daily-level opportunity mapping did not uncover a repeatable low-turnover directional edge in the KRX Development window.
- `PHASE10_OPPORTUNITY_MAP=FAIL`; `DAILY_A=NOT_CREATED`; Secondary, preregistration, external validation, 100K, and Shadow were not run.
- Artifacts: ignored `runtime/research/phase10/` (features, outcomes, map JSON, summary, report, index, integrity). Artifact integrity PASS.

## Phase 12 current decision

- Phase 12 analyzed only Development (2023-01-02–2024-06-28) and touched Validation (2024-07-01–2025-06-30) to explain the Phase 11 decline. The matched index averaged +0.893% vs. +1.046% stock gross in Development and +0.366% vs. +0.479% in Validation; the 0.527-point market-component decline explains 92.9% of the gross decay.
- Stock-minus-index means were +0.153% / +0.113%; simple rolling-beta residual means were +0.319% / +0.162%, below the unchanged 0.53% round-trip cost. No stable single-feature mechanism passed the minimum cluster, date, market-adjusted, and concentration gates.
- Volatility and dispersion shifted (SMD +0.909 and +0.662); breadth shifted modestly (SMD −0.211). Fast-shock composition rose from 20.0% to 91.7%, but its rebound direction did not replicate. The evidence supports market beta and changed stress composition as the anatomy, not an independently tradable mechanism.
- Verdicts: `PHASE12_FAILURE_ANATOMY=COMPLETE`; `MARKET_BETA_EXPLANATION=STRONG`; `STRESS_COMPOSITION_SHIFT=STRONG`; `MARKET_STRESS_MECHANISM=NOT_SUPPORTED`; `PHASE12_CANDIDATE=NOT_CREATED`; `PHASE12_CONFIRMATION=NOT_RUN`; `EXTERNAL_2026=NOT_READ`; `HOLDOUT_2026=NOT_READ`; `SHADOW_NEXT_SESSION=NO`; `ALPHA=UNPROVEN`; `LIVE=DISABLED`.
- The historical Phase 11 candidate remains `REJECTED` and its Validation remains `FAIL`. Current-listing survivorship bias applies. Phase 5–11 artifacts remain unchanged; Phase 12 machine-readable artifacts are under ignored `runtime/research/phase12/`. Details and explicit Q1–Q15 answers are in [Phase 12 research](docs/research/phase12-market-stress-failure-anatomy.md).

## Phase 13 current decision

- Official KRX/KIND sources expose useful current listings and delisting references, and KRX describes dated trading/security data, but complete as-of membership, code lineage, delisted retention, and revision semantics were not verified. `POINT_IN_TIME_UNIVERSE=PARTIAL`; `PIT_PRICE_HISTORY=NOT_AVAILABLE`; `SURVIVORSHIP_SENSITIVITY=NOT_TESTABLE`. The study kept the frozen 100-current-listing cohort separate, with usable safe prices for 96 symbols.
- KIS adjusted daily OHLCV (`FID_ORG_ADJ_PRC=0`) and matched KOSPI/KOSDAQ index returns covered only Discovery (2023-01-02–2024-06-28) and Touched Replication (2024-07-01–2025-06-30). No Confirmation or 2026 outcomes were evaluated before the preregistered freeze. Touched Replication is not independent.
- The strongest standalone state was 20d beta-residual reversal (−4% to −2%), 5-session horizon: gross +0.224%/+0.347%, excess +0.282%/+0.492%, residual +0.231%/+0.444%, but net −0.306%/−0.183% at 1× in Discovery/Touched. It fails long-only cost economics.
- Two adjacent 5d excess-return buckets × EXPANDED turnover passed the fixed KOSPI D/T gates; residual-first priority selected the ≤−4% state. It returned gross +1.160%/+1.461%, excess +0.681%/+0.692%, residual +0.590%/+0.968%, net +0.630%/+0.931% at 1× and positive net through 2×. One `RESEARCH_CANDIDATE` was preregistered, not proof of alpha.
- Candidate date-cluster 90% residual intervals include zero in both periods. Accordingly `RESIDUAL_MECHANISM=WEAK`. Current-listing survivorship bias remains; the mechanism is KOSPI-specific under the frozen current cohort. Confirmation is the only next statistical gate and must run once after the exact pushed freeze check. External 2026 and Holdout remain unread; `SHADOW_NEXT_SESSION=NO`, `ALPHA=UNPROVEN`, `LIVE=DISABLED`.
- Full methods, source audit, factor maps, market split, chronological stability, concentration, bootstrap, Q1–Q18, and artifact paths are in [Phase 13 research](docs/research/phase13-point-in-time-universe-and-residual-factor-map.md). Machine-readable outputs are generated under ignored `runtime/research/phase13/`.

## Phase 11 current decision

- Phase 11 acquired adjusted daily history (`FID_ORG_ADJ_PRC=0`) and KOSPI/KOSDAQ indexes for 2023-01-02–2025-12-30. The fixed 100-symbol cohort returned daily rows for 99 symbols; 90 meet the dense multi-year span gate. KIS returned no daily rows for `282620`; it remains missing. Current-listing survivorship bias applies.
- The investor-flow source audit did not find a reproducible multi-year point-in-time per-symbol panel. Price, volume, traded value, index context and coarse price×liquidity maps are available; flow and price×flow are `NOT_AVAILABLE`. Phase 11 factor map is `PARTIAL` for that reason.
- Best price-only 5d state: stock 20-session return <=−4%, gross +0.429%, net −0.101% at 1× cost. A broad matched-index 20-session decline state (<=−4%) produced +1.046% gross at 3d in Development and was frozen as one exploratory map-derived research candidate.
- The candidate used next-session open entry and third-session close exit. Development passed (+1.046% gross; +0.516% net at 1×; +0.251% at 1.5×; 4,624 raw events, 1,927 non-overlap, 35 date clusters). The untouched one-shot Validation failed (+0.479% gross; −0.051% net at 1×). It is `REJECTED`; Confirmation is `NOT_RUN`, Alpha remains `UNPROVEN`, and Live remains `DISABLED`.
- 2026 external and protected Holdout data were not read. Phase 5–10 manifest integrity and Phase 11 artifact integrity are PASS. Full report and machine-readable artifacts are documented in [Phase 11 research](docs/research/phase11-multi-year-factor-map.md) and ignored `runtime/research/phase11/`.
- The overnight report-generator execution-window issue was not reproducible because that generator is not in this repository; no report code was changed.

## Phase 8 current decision

- Phase 8 tested opening gaps against the latest safe prior close and measured first-hour price discovery on Development (2026-04-17–06-30). It found 2,244 valid sessions, including 1,051 absolute gaps of at least 1%; 92 of those were suspicious extreme gaps and are ineligible for candidates.
- Across meaningful gap events with an observed 10:00 checkpoint, the open-to-price mean was −0.653% gross; the stricter 61-event high-confidence, non-suspicious daily-close subset averaged −1.484%. Direction-aligned gap outcomes were weak or negative, first-hour acceptance and early gap fill did not improve subsequent outcomes, and matched controls were slightly less negative than gap events.
- The 0.53% assumed round-trip cost exceeded any positive descriptive edge. GAP-A and GAP-B were not created (14 and 6 diagnostic events, respectively); no Development candidate qualified. Secondary, preregistration, external validation, 100K replay, and Shadow were not run.
- The safe cache is now 47 complete symbols (30 KOSPI + 17 KOSDAQ), 1 partial, and 12 not acquired. Six bounded KIS requests were attempted at the persistent 4-second interval; safe acquisition remains resumable. No Fresh Holdout price-data partitions or external-block partitions were opened. An earlier broad source search incidentally surfaced date/timestamp metadata from one or more 2026-08-28 holdout sidecars; the exact count was not recorded, no holdout outcomes were evaluated, and complete metadata isolation cannot be claimed. See the integrity note in `runtime/research/phase8/` and [RESULTS.md](RESULTS.md).
- Phase 8 records the Phase 6 indexed-manifest mismatch as `DEGRADED_RECONCILED`: the original 11,410-byte copy is unavailable, the current 11,098-byte copy differs, and Phase 6 summary/verdicts and index were preserved. The Phase 8 isolation regression confirms its acquisition fixture leaves Phase 5/6/7 manifest hashes unchanged, and Phase 8 acquisition does not read or write the shared daily cache.
- Verdicts: `OPENING_GAP_ANATOMY=FAIL`; `OPENING_GAP_FAMILY=REJECT`; `GAP_A=NOT_CREATED`; `GAP_B=NOT_CREATED`; `SWING_SIGNAL=NONE`; `SHADOW_NEXT_SESSION=NO`; `ALPHA=UNPROVEN`; `PAPER=OUT_OF_SCOPE`; `LIVE=DISABLED`.
- Full Phase 8 evidence, source limitations, Q1–Q12 answers, and reproducibility manifests are appended to [RESULTS.md](RESULTS.md); ignored machine-readable artifacts are under `runtime/research/phase8/`.

## Phase 7 current decision

- Phase 7 tested a **session VWAP reclaim / acceptance** hypothesis on the existing safe-period cache. The 15m Development anatomy failed its predeclared five-part support gate (2/5); 30m was weak (3/5) and did not override the primary result.
- Cached KIS minute data has OHLCV only, with no transaction value/turnover field. The calculation is explicitly `VWAP_PROXY`: session cumulative `((high + low + close) / 3) * observed_volume / cumulative_observed_volume`; each symbol/session resets at 09:00 KST. Continuous bars are 09:00–15:19; the closing auction is excluded. No missing minutes were synthesized.
- The study used 46 complete symbols (30 KOSPI + 16 KOSDAQ), 2 partial KOSDAQ symbols, and 12 not acquired, over 67 safe sessions (49 Development, 18 Secondary). Complete breadth remained 46/60; a bounded authenticated read-only KIS historical OHLCV backfill added 36 safe partitions without changing any prior partition. It was stopped while waiting after the persistent request interval rose to 4 seconds. Holdout and external partitions opened: 0 each.
- Latest safe cache DQ covers 3,174 symbol-sessions: 1,771 HIGH_CONFIDENCE, 412 PARTIAL, 991 UNRELIABLE; 1,041,514 of 1,206,120 expected minutes observed and none synthesized. Missing-slot cause remains unknown.
- 15m reclaim events with four-bar outcomes averaged −0.0471% gross vs. −0.0157% for the matched baseline (−0.0314 percentage points); the high-confidence subset was −0.0674%. Reclaim failure within four bars was 62.23%. Acceptance A cut post-confirmation failure to 47.62%, but its delayed-entry four-bar mean was only +0.0365% gross and the one-bar confirmation delay reduced mean return by 0.2414 percentage points within that accepted subset.
- No VWAP-A/B strategy variant was justified or evaluated. Secondary strategy evaluation was not run, preregistration was not created, external validation is unavailable, 100K feasibility was not run, and Shadow remains NO. Historical Phase 2–6 decisions are preserved; in particular `MEAN_REVERSION_FAMILY=INSUFFICIENT` remains unchanged.
- The interrupted collector wrote through the existing Phase 6 runtime path. Its preserved artifact index still records the previous `phase6-acquisition-manifest.json` hash, so current Phase 6 artifact integrity needs reconciliation; the historical Phase 6 summary, research verdict, and index were not rewritten. Full details and Phase 7 machine-readable artifacts are in [RESULTS.md](RESULTS.md) and ignored `runtime/research/phase7/`.

## Phase 6 current decision

- Phase 6 studied the **Cross-Sectional Laggard Rebound** family (MR-A Laggard Reversal, MR-B Laggard Stabilization + Recovery Confirmation) on 46 complete symbols (30 KOSPI + 16 KOSDAQ). The 60-symbol cohort was the acquisition target; 46 passed safe-period partition and bar-structure checks.
- The core hypothesis tested whether bottom-ranked intraday stocks that show (1) decelerating weakness, (2) relative rank recovery, and (3) absolute price reversal produce a cost-adjusted rebound edge.
- Empirical anatomy across Development data (2026-04-17 to 2026-06-30) found:
  - Bottom-ranked stocks (0-10 percentile) showed a **WEAK** 1-bar rebound (+0.032% mean) but negative 4-bar (-0.020%) and 8-bar (-0.104%) forward returns. The rebound was small and short-lived.
  - **Stabilization + rank recovery did NOT outperform blind laggard selection.** The stabilized relative+absolute reversal group (n=1,114) had worse1-bar forward return (-0.023%) than the blind laggard baseline (-0.007%), and the 4-bar return was similar negative (-0.019% vs -0.063%).
  - **The effect did NOT persist on cleaner/fresher observations.** Clean-data sensitivity showed negative direction.
- Both MR-A and MR-B were **not created** because the Development anatomy gate failed:
  - Development sample at least 30: PASS (1,114 observations)
  - Development forward 4-bar positive: FAIL (stabilized group mean 4-bar return = -0.019%)
  - Development beats blind laggard: FAIL (stabilized group worse than baseline)
  - Clean data same positive direction: FAIL (clean data showed negative returns)
- Neither variant reached the Secondary Diagnostic. External validation was not performed. No preregistration was created.
- The untouched external block (2026-01-05 to 2026-04-16) remains `NOT_AVAILABLE` because no Secondary-surviving candidate exists.
- The 23-session Fresh Holdout (2026-07-28 to 2026-08-28) remains `LOCKED_NOT_EVALUATED`; zero holdout partitions were opened.
- Verdict: `MEAN_REVERSION_FAMILY=INSUFFICIENT`; `MR_A=INSUFFICIENT`; `MR_B=INSUFFICIENT`; `SHADOW_NEXT_SESSION=NO`; `ALPHA=UNPROVEN`.
- This completes the third strategy family rejection (Breakout, Relative-Strength, Mean-Reversion) using the fixed safe-period dataset.

## Phase 5 current decision

- Phase 5 studied the **Relative-Strength Continuation** family (`RS-A` Persistent Leader, `RS-B` Persistent Leader + Reacceleration) on 33 symbols (30 KOSPI + 3 KOSDAQ). The 60-symbol cohort was an acquisition target, not the study breadth.
- Empirical anatomy across 50,271 resampled observations (15m primary, 30m secondary) disproved the core continuation hypothesis:
  - Higher relative strength deciles exhibited **worse** forward 1-bar returns (Bucket 90–100: −0.069% fwd return, 40.7% win rate vs. Bucket 0–20: −0.0096% fwd return, 43.5% win rate). Monotonicity was `FALSE`.
  - Multi-bar rank persistence exhibited **no advantage** over single-bar impulse (Persistence 3/3: −0.069% fwd return vs. 0/3: −0.011% fwd return). KRX intraday momentum undergoes immediate mean-reversion rather than continuation.
- In Development (2026-04-17–06-30):
  - RS-A: 691 trades, win rate 24.7%, gross expectancy −0.296%, net expectancy −0.823%, PF 0.44.
  - RS-B: 453 trades, win rate 26.9%, gross expectancy −0.053%, net expectancy −0.581%, PF 0.57.
- In Secondary Diagnostic (2026-07-01–07-27):
  - RS-A: 219 trades, win rate 26.0%, gross expectancy −0.058%, net expectancy −0.587%, PF 0.53.
  - RS-B: 173 trades, win rate 28.3%, gross expectancy −0.215%, net expectancy −0.743%, PF 0.47.
- Both variants are **gross-negative before transaction costs** and fail the primary promotion gate (`gross expectancy > 0`, `net expectancy > 0`, `PF > 1.0`). Cost stress (1.5x, 2.0x) worsened negative expectancies to −0.84% ~ −1.35%.
- ₩100,000 whole-share portfolio replay executed 193 trades, yielding −₩14,443 net PnL (−14.44% return) with max drawdown 14.47% and PF 0.46.
- The untouched external block (2026-01-05–04-16) was recorded as `EXTERNAL_VALIDATION=NOT_AVAILABLE` because gross-negative candidates cannot be promoted or snooped under Rule 71 and Rule 100.
- The 23-session Fresh Holdout (2026-07-28–08-28) remains `LOCKED_NOT_EVALUATED`; zero holdout partitions were opened.
- Verdict: `RELATIVE_STRENGTH_FAMILY=REJECTED`; `SHADOW_NEXT_SESSION=NO`; `ALPHA=UNPROVEN`.
- Post-study reconciliation found 36 complete safe-period symbols, one partial symbol (`215000`, 30/67 sessions), and 23 not started. One fixed-rule `POST_STUDY_BREADTH_SENSITIVITY` on 36 complete symbols remained gross-negative in Development and Secondary Diagnostic; the rejection is unchanged.
- `data-acquisition-manifest.json` still says `RUNNING`, but no backfill worker remains. The historical manifest is preserved; the final reconciliation artifact classifies it as `PARTIAL_INTERRUPTED`. The original artifact index is retained and `phase5-artifact-index-final.json` records the reconciled state locally under ignored runtime data.
- Phase 5 is closed for strategy research and evidence reconciliation. The 60-symbol acquisition remains partial and is not being resumed here.

## Phase 4 historical summary

- Phase 4 expanded the metadata cohort to 60/100 current listings (balanced KOSPI/KOSDAQ), but no broad historical minute data was available. This remains a current-listing cohort with survivorship bias.
- In the safe 67-session Development + secondary Validation window, 510 missing minute slots were all classified `UNKNOWN`; 0 confirmed retrieval gaps or legitimate no-trade minutes. No synthetic bars were added. The earlier full-window count of 956/412 remains historical Phase 2.5 reporting; the locked 2026-07-28–08-28 partition was not opened.
- Retest-A was negative after costs in all four interval/split cells. Retest-B reduced measured immediate rejection in four cells but had only 5–30 closed trades per cell, did not improve follow-through consistently, and was negative at 1.5x costs throughout. `BREAKOUT_FAMILY=REJECT`; no strategy proceeds to Shadow.
- Strategy freeze: `d0fad5bf18a3e1eae010dca21490ad15ba04dddd`; config SHA-256 `4076bb140d1e9452e7788bc2730497a77af11a28ece1bd8685c37eaddb34980b`. The untouched 2026-01-05–04-16 block had 0 cached partitions across path-only checks; KIS process credentials were absent and `.env` was not read, so `EXTERNAL_VALIDATION=NOT_AVAILABLE`.
- Current verdicts: `DATA_QUALITY=PARTIAL`, `COHORT_BREADTH=LIMITED`, `RETEST-A=REJECTED`, `RETEST-B=INSUFFICIENT`, `100K=CONSTRAINED`, `SHADOW_NEXT_SESSION=NO`. `ALPHA=UNPROVEN`; `LIVE=DISABLED`; `PAPER=OUT_OF_SCOPE`.
- Full Phase 4 rules, metrics, artifacts, and next-step boundary are recorded in [RESULTS.md](RESULTS.md).

## Phase 3 historical summary

- Phase 3 analyzed only the 49-session Development window (2026-04-17–06-30) and 18-session Validation window (2026-07-01–07-27), using 30 current KOSPI listings and the fixed scanner proxy. The Phase 2.5 23-session Fresh Holdout (2026-07-28–08-28) remains `LOCKED_NOT_EVALUATED`; zero post-validation partitions were opened.
- Baseline Breakout was net-negative in all four interval/split cells. 15m PF/expectancy: Dev 0.406 / −₩383, Validation 0.151 / −₩495. 30m: Dev 0.757 / −₩250, Validation 0.101 / −₩648. All four cells were gross-negative before costs.
- V2-A not-overextended retained 75.1% of Dev signals and improved Dev PF to 0.483, but Validation remained PF 0.193 / −₩439 expectancy and false-breakout rate increased to 26.67% from 25.81%. V2 was rejected and not started in Shadow.

## Phase 2 & 2.5 historical summary

- KIS read-only auth, quote, daily OHLCV, minute OHLCV, KOSPI/KOSDAQ indices, official universe master, market scan, and both strategy rankers returned valid responses.
- The research cache contains 30 current common-stock symbols over 17 KRX sessions (193,446 actual minute rows, 0 duplicates, 0 DQ errors).
- Four 15m/30m Breakout/Pullback OOS portfolios completed with 100,000 KRW, 20,000 KRW order cap, and fixed Phase 1 parameters. Each had 0 executed trades.
- Phase 2.5 backfilled 30 current KOSPI symbols over 90 common sessions (2026-04-17–2026-08-28), 1,018,940 actual KIS minute rows. The dataset is diagnostic-only and partial: 956 expected minute slots are absent across 412 partial partitions; no bars were synthesized. Its fresh holdout remains locked and unevaluated.
- The 2026-09-28 prospective Shadow ran from 09:00:00 to 13:00:00 KST at frozen commit `53cfc0662d9cf9537325578e35f50d55b325dc1b`. KIS confirmed the open, eight scanner cycles and 50 completed-bar decisions were captured; all 50 were `HOLD / REGIME_BLOCK`. There were 0 simulated fills, 0 order API calls, 0 account reads, 0 restarts, and 0 replay mismatches.
- The KIS order adapter remains unavailable, account reconciliation was not run, and live preflight remains fail-closed.

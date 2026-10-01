# Phase 16 — Prospective Evidence Builder

## Decision and boundary

Phase 16 implements an append-only, timestamped evidence store and starts prospective observation with a current-only source. The safe bootstrap captured current KOSPI/KOSDAQ listings. Per-stock investor flow, KIS market flow, and program trading remain deferred because their observed date-anchored endpoints return trailing history whose protected-period overlap cannot be ruled out before the request.

No flow endpoint was called during Phase 16. No factor, forward-return relationship, candidate, strategy, threshold, backtest, or PnL result was created. `ALPHA=UNPROVEN`, `SHADOW_NEXT_SESSION=NO`, and `LIVE=DISABLED`. The 2026 External and 2026-07-28–2026-08-28 Holdout contents remain `NOT_READ`.

## Source safety and request guard

The collector evaluates an endpoint's declared response shape before transport. A request is allowed only for a verified current-only response or a proven exact-date response whose full possible response interval misses both protected ranges. Unknown and unproven trailing windows are denied before network access. The guard uses conservative calendar-day bounds derived from the observed row count; these are denial bounds, not claims about provider publication or exact response semantics.

| Source | Phase 15 observed shape | Current guard | Decision |
|---|---:|---|---|
| KIS current KOSPI/KOSDAQ master archives | Current listings; no historical date argument | `CURRENT_ONLY_CONFIRMED` | Active; current listings only |
| KIS per-stock investor flow | 31 dated rows ending at the requested date | Potential response starts 2026-06-29; overlaps Holdout | `DEFERRED_SAFETY_GUARD` |
| KIS market investor flow | Up to 300 dated rows | Potential response starts 2024-04-13; overlaps External and Holdout | `DEFERRED_SAFETY_GUARD` |
| KIS per-stock program trading | Up to 30 dated rows | Potential response starts 2026-07-02; overlaps Holdout | `DEFERRED_SAFETY_GUARD` |
| KRX market investor statistics | Exact-day response contract not verified | Unknown response range | `DEFERRED_SEMANTICS` |

Phase 15 verified a date input on the KIS per-stock example, but a date input did not constrain its response to one date: the local adapter observed a 31-row window. KIS's official example documents request fields and continuation, not exact-date-only response, publication time, or correction semantics ([per-stock example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/investor_trade_by_stock_daily/investor_trade_by_stock_daily.py)). The Phase 15 observations and field inventory are documented in the [Phase 15 report](phase15-new-information-source-qualification.md).

`collect-full` and `collect-probe` are no-network preflights for flow. On 2026-10-01 both returned `DEFERRED_SAFETY_GUARD`, `request_count=0`, and `network_accessed=false`. The historical-probe command records a separate `HISTORICAL_REVISION_PROBE` guard decision only; it does not issue historical requests.

## Current-only bootstrap

The collector fetched the two current KIS master archives sequentially using the repository's shared limiter. The bounded bootstrap completed with 2 requests, 0 retries, and about 4.30 seconds elapsed. It created two immutable prospective snapshots:

- `FULL_CURRENT_LISTINGS`: 3,544 listing rows.
- `FROZEN_RESEARCH_COHORT`: all 100 frozen Phase 4 symbols present, 100/100 coverage.

Both snapshots were observed at `2026-10-01T23:07:40.276373+09:00` (`2026-10-01T14:07:40.276373+00:00`). The label is `MANUAL_BOOTSTRAP`; it describes this observation, not provider publication. The observed listing fields are `symbol`, `market`, `company_name`, `listing_status`, `instrument_type`, `is_etp`, `is_preferred`, `is_spac`, `halted`, `management`, and `warning_status`; the cohort intersection also stores `current_master_presence` and `symbol_lineage`. Lineage remains `UNKNOWN` unless a source gives an explicit mapping. Prices, market capitalization, and raw ZIP contents are not stored. ZIP bytes are represented by per-market SHA-256 hashes only; raw retention remains disabled because provider retention terms were not established.

The first prospective observation timestamp is the one above. It is a listing observation, not a completed flow session. Thus `PROSPECTIVE_FIRST_SESSION_DATE` is unset, completed flow sessions are 0, and multi-vintage flow sessions are 0.

The bootstrap manifest records collector Git SHA `cfea01cfa8d8f531da905affd56a11f6a23a691e` with `collector_git_dirty=true`, because implementation files were still uncommitted at collection time. This records the actual source checkout state; it does not claim the final Phase 16 commit was already present.

## Flow fields and missing semantics

No Phase 16 per-stock flow values or rows have been collected. The normalizer is ready to preserve provider keys and values without assigning their meaning: it emits `session_date`, `symbol`, `market`, `provider_field_name`, `semantic_status=UNRESOLVED`, `raw_value`, `raw_numeric_value` when the original value is numeric text or a numeric scalar, `normalized_numeric_value=null`, `unit=null`, `unit_status=UNKNOWN`, UTC/KST observation timestamps, `source`, and `snapshot_id`. Missing values remain missing; the normalizer does not substitute zero.

The Phase 15 endpoint audit observed these row keys: `stck_bsop_date`; `frgn_nreg_askp_pbmn`, `frgn_nreg_askp_qty`, `frgn_nreg_bidp_pbmn`, `frgn_nreg_bidp_qty`, `frgn_nreg_ntby_pbmn`, `frgn_nreg_ntby_qty`; `frgn_ntby_qty`, `frgn_ntby_tr_pbmn`; `frgn_reg_askp_pbmn`, `frgn_reg_askp_qty`, `frgn_reg_bidp_pbmn`, `frgn_reg_bidp_qty`, `frgn_reg_ntby_pbmn`, `frgn_reg_ntby_qty`; `frgn_seln_tr_pbmn`, `frgn_seln_vol`, `frgn_shnu_tr_pbmn`, `frgn_shnu_vol`; `orgn_ntby_qty`, `orgn_ntby_tr_pbmn`, `orgn_seln_tr_pbmn`, `orgn_seln_vol`, `orgn_shnu_tr_pbmn`, `orgn_shnu_vol`; and `prsn_ntby_qty`, `prsn_ntby_tr_pbmn`, `prsn_seln_tr_pbmn`, `prsn_seln_vol`, `prsn_shnu_tr_pbmn`, `prsn_shnu_vol`. They are not present in Phase 16 snapshots. `orgn` is explicitly unresolved; quantity/value units and provider publication/correction times also remain unverified. Provider field names are retained as received.

## Vintage store and integrity

Prospective observations and historical revision probes use separate trees and explicit `evidence_class` values. Each successful collection creates a new snapshot with UTC and Seoul timestamps, endpoint/schema identity, logical key, config hash, collector Git SHA and dirty-state marker, sanitized raw hash, canonical hash, normalized hash, schema fingerprint, result classification, record count, availability label, quality flags, and previous vintage ID.

The current-only KIS listing ZIPs are not retained as raw payloads. Their `raw_payload_sha256` is over a documented concatenation of the two received archive byte strings with market separators. Normalized JSON snapshots are retained locally. Canonicalization sorts object keys and record collections, preserves scalar values and original field names, rejects non-finite numbers, and ignores only the configured transport metadata keys. Therefore byte/format changes can be distinguished from semantic value changes.

Payloads, manifests, and the chain are create-only and written atomically; the store takes a process lock, fsyncs, and rejects existing snapshot IDs. The ordered manifest chain records previous and current manifest hashes plus the exact manifest-file hash. `verify-store` returned `PASS` for 2 snapshots, with chain tip `2689ccfd549f098c233dbc5aa57e95c22defbb5b5b9e63d88ce9e873bc2b6e79`. Regression tests confirm a historical payload mutation breaks verification. No revisions are observed yet: 0 comparisons, 0 sessions with multiple vintages, and no schema drift observed. This is absence of comparison evidence, not evidence of stability.

Revision classes include `IDENTICAL`, `SEMANTICALLY_IDENTICAL`, value/missingness changes, field additions/removals, schema changes, and `UNCOMPARABLE`. Value changes retain field-level old/new values and decimal absolute/relative differences where meaningful. Business-session revision age is left unavailable because no verified session calendar is configured. Formatting-only differences change the raw hash while deterministic semantic canonicalization can leave the canonical hash unchanged.

## Schedule, missed observations, export, and health

The user-level LaunchAgent `com.krxtrader.phase16.universe` is `ACTIVE`, scheduled daily at 20:15 KST. It runs only the current-only listing collector. Flow probe/full-cohort, market-flow, program-flow, and microstructure jobs are not enabled. The configured flow observation slots (16:10, 18:00, 20:15, 22:15, and next business day 08:20) remain proposals and do not imply publication times.

Missed scheduled observations are recorded as `MISSED_OBSERVATION`; the system does not backdate or reuse an earlier timestamp. A later observation is a new snapshot with its actual time and a late-catch-up label when applicable. The daily current-listings schedule is not a market-session count.

`phase16 export-evidence` creates a deterministic local tar archive with normalized evidence, manifests, and reports. The bootstrap export is `phase16-evidence-422729d199b93cfec8dd9afa4fca40bdf1c5764dc00fd410346f8b317dad5239.tar`, SHA-256 `422729d199b93cfec8dd9afa4fca40bdf1c5764dc00fd410346f8b317dad5239`. Export excludes lock files, credentials, and other export packages. No remote backup or upload is configured.

At the latest recorded health/status check, the Phase 16 store used 2,494,635 bytes total: raw 0, normalized 981,708, manifests 12,924, and other 1,500,003. The total includes the first in-root export archive. The latest external local backup archive is 1,269,760 bytes; its SHA-256 is `9ec27590592e163708a5ca95b5a5a35ad06f643fbd1dfc335ad299fdacb82e12`. Thresholds are 5 GiB warning and 10 GiB critical. Health returned `PASS_WITH_WARNINGS`; the only warning was `SOURCE_STABILITY_INSUFFICIENT`. Previous-phase integrity passed for all 850 files in the Phase 5–15 snapshot (`061a12bd8108310f920cd0b3148c1e41b297c7522576d49baf49c31575ba14d2`).

## Stability gate and next step

The predeclared Phase 17 source-stability review gate is at least 20 completed flow sessions and at least 10 sessions with multiple observed vintages. Current counts are 0 and 0; therefore `SOURCE_STABILITY=INSUFFICIENT_OBSERVATIONS` and `PHASE17_SOURCE_STABILITY_READY=NO`. These are evidence-quality requirements, not alpha criteria. The next action is to continue the safe daily universe snapshots and establish a prospective flow endpoint whose complete response is proven exact-date/current-only before enabling flow collection. No historical-window fallback is allowed.

## Explicit answers

1. **Safe source:** KIS current KOSPI/KOSDAQ listing master archives, which contain current listings and do not request dated flow history.
2. **Exact-date KIS per-stock flow:** Not established. The date-anchored endpoint returned 31 rows; prospective requests are deferred.
3. **Rejected bulk endpoints:** KIS per-stock flow (31 rows), market flow (up to 300), and program flow (up to 30). Current 2026-10-01 guard windows overlap Holdout for per-stock/program and both protected ranges for market flow.
4. **First observation:** 2026-10-01 23:07:40.276373 KST, current listing master.
5. **Flow symbols:** 100 targeted, 0 flow-observed, 0 missing or failed because no flow request was sent. The separate frozen-cohort listing snapshot matched 100/100.
6. **Per-stock flow fields stored:** None yet. If a safe endpoint is approved by evidence, the generic normalizer preserves every provider key/value and adds the metadata fields listed above.
7. **Unresolved semantics:** `orgn` mapping, quantity/value units, publication time, and revision/vintage meaning.
8. **Prospective revisions:** None observed; 0 comparisons.
9. **Formatting vs values:** Raw SHA covers sanitized received representation; canonical SHA sorts deterministic objects/record collections and excludes only explicit transport metadata.
10. **Manifest chain:** Valid, 2 snapshots, chain tip above.
11. **Mutation detection:** Yes; payload and manifest hash checks make historical changes fail verification, covered by tests.
12. **Daily current universe:** Yes, through current-only listing archives.
13. **Universe scope:** One `FULL_CURRENT_LISTINGS` snapshot (3,544 rows) and one `FROZEN_RESEARCH_COHORT` intersection (100/100), not a historical point-in-time universe.
14. **Market aggregate flow:** Not proven safe; deferred.
15. **Program flow:** Not proven safe; deferred.
16. **Microstructure:** Not implemented for collection; deferred.
17. **Schedule:** Active user-level LaunchAgent, daily at 20:15 KST for current listings only.
18. **Missed windows:** Explicit `MISSED_OBSERVATION`; no timestamp backdating. Later catch-up is separately timestamped.
19. **Storage:** 2,494,635 bytes in the Phase 16 store at the latest status check, plus the 1,269,760-byte latest archive under ignored `runtime/research/phase16-backups/`.
20. **Backup:** Deterministic local tar export `9ec27590592e163708a5ca95b5a5a35ad06f643fbd1dfc335ad299fdacb82e12`, content-addressed by SHA-256; no remote upload.
21. **Completed flow sessions:** 0. A listing date is not counted as a completed flow session.
22. **Multi-vintage sessions:** 0.
23. **Stability review ready:** No; the minimum 20/10 gate is unmet.
24. **Next action:** Keep collecting permitted current-only observations and verify a safe exact-date flow contract before enabling any flow request.

## Status matrix

| Item | Status |
|---|---|
| Phase 16 builder / immutable store / manifest chain / protected-response guard | `COMPLETE` / `PASS` / `PASS` / `PASS` |
| Per-stock flow | `DEFERRED_SAFETY_GUARD` |
| Market flow / program flow | `DEFERRED_SAFETY_GUARD` / `DEFERRED_SAFETY_GUARD` |
| Universe snapshot | `ACTIVE` |
| Microstructure | `DEFERRED` |
| Revision tracker / live bootstrap | `PASS` infrastructure; 0 comparisons / `PARTIAL` (current listings passed; flow deferred) |
| Scheduler | `ACTIVE` |
| Prospective / multi-vintage sessions | `0` / `0` |
| Source stability / Phase 17 gate | `INSUFFICIENT_OBSERVATIONS` / `NO` |
| External 2026 / Holdout 2026 | `NOT_READ` / `NOT_READ` |
| Alpha / Shadow next session / Live | `UNPROVEN` / `NO` / `DISABLED` |

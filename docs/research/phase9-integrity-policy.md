# Phase 9 Market Data Integrity Policy

## Source inventory and conventions

| Source | Endpoint / TR | Stored fields | Adjustment interpretation |
| --- | --- | --- | --- |
| KIS daily equity bars | `inquire-daily-itemchartprice` / `FHKST03010100` | `stck_oprc`, `stck_hgpr`, `stck_lwpr`, `stck_clpr`, `acml_vol` | Configurable: `FID_ORG_ADJ_PRC=0` adjusted; `=1` raw/original. |
| KIS daily minute bars | `inquire-time-dailychartprice` / `FHKST03010230` | `stck_cntg_hour`, `stck_oprc`, `stck_hgpr`, `stck_lwpr`, `stck_prpr`, `cntg_vol` | No adjustment selector or explicit raw/adjusted guarantee; record as `UNKNOWN`. |
| Daily cache | `data/daily/{symbol}-1d.parquet` and sidecar | Multi-session KIS daily rows plus hash/provenance | Do not open during Phase 9 because a file could span a protected date. Use a separately fetched Development-only reconciliation copy. |
| Minute cache | `data/minute/{symbol}/{session}.parquet` and sidecar | One KRX session per partition | Resolve only explicit Development session paths; never enumerate Holdout sidecars. |
| Derived daily bars | None | No canonical minute-to-daily cache is currently implemented | Keep KIS daily and minute-derived inputs separate; do not mix conventions. |

KIS minute records are treated as observed bar-start labels: `09:00` aligns to the session open, and the continuous session uses rows from `09:00` through `15:19`. The `15:20` closing auction is outside the minute cache. Fifteen-minute resampling labels completed buckets by their end boundary: the `09:00`–`09:14` bars produce a `09:15` timestamp.

Official endpoint references:

- [KIS Open Trading API domestic stock endpoint catalog](https://github.com/koreainvestment/open-trading-api/blob/main/MCP/Kis%20Trading%20MCP/configs/domestic_stock.json)
- [KIS daily minute API example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py)

Phase 9 fetches both daily conventions to separate reconciliation partitions. The source comparison uses adjusted daily (`0`) against 09:00/15:19 minute observations; raw daily (`1`) is retained to reproduce frozen Phase 8 gap definitions. The regular KIS client keeps raw as its default for backward compatibility, and callers can request adjusted bars explicitly.

## Phase 9 access boundary

- Development analysis is limited to `2026-04-17` through `2026-06-30` and the frozen 60-symbol cohort.
- KIS reconciliation output belongs under `runtime/research/phase9/`; original Phase 5–8 manifests and cache partitions remain immutable.
- Missing bars are not synthesized. A bounded refresh writes into Phase 9 reconciliation storage and is compared with the existing safe partition before any replacement is considered.
- The existing July 28–August 28 Holdout is permanently excluded from Phase 9 and cannot be described as pristine. No price payload or sidecar content from that period may be read.
- `2026-01-05` through `2026-04-16` remains an external outcome block. Source semantics and integrity checks may use only the minimum data required for those checks; no candidate outcome is computed before a preregistration and freeze commit.
- A Phase 8 replay is allowed only after price semantics pass or a documented correction is made. Use its frozen definitions once and label the result `DATA_CORRECTED_REPLAY`.

## New future Holdout policy

The July–August period above remains contaminated for any future promotion because some sidecar metadata was exposed. Create the replacement Holdout prospectively after a Development candidate is frozen:

1. Freeze one candidate and commit its source, feature rules, cost model, and exact entry/exit rules. Record the commit SHA, config SHA, cohort SHA, and dataset SHA in preregistration.
2. Select and seal a future calendar interval before reading any of its payloads or sidecars. Record the interval, exact expected sessions, symbols, and partition paths in an append-only access manifest.
3. Keep the Holdout unavailable to feature generation, threshold selection, candidate choice, and repeated evaluation. The evaluation job accepts only the preregistered SHA and exact path allowlist.
4. On the single authorized evaluation, read only those listed partitions. Record payload/sidecar hashes and every path opened. Do not search, glob, or recursively enumerate the Holdout cache.
5. Any pre-freeze payload or sidecar exposure invalidates the interval for promotion. Preserve the evidence and move the replacement interval forward; do not repair the record or relabel the data pristine.

This policy does not authorize Phase 10 external evaluation, paper execution, shadow deployment, or live trading. `LIVE` remains disabled.

The one fixed Phase 8 correction replay uses the adjusted daily input and publishes the source as adjusted. It reuses the frozen Phase 8 descriptive calculations without changing Phase 8 manifests or outputs. It does not open Secondary, External, or Holdout data and does not run candidate promotion.

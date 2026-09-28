# Project status

```text
ALPHA: UNPROVEN
BREAKOUT_FAMILY: REJECTED (Phase 3 v1/v2 and Phase 4 Retest evidence)
BREAKOUT_V2: NONE (no Validation candidate survived)
BREAKOUT_RETEST_A: REJECTED
BREAKOUT_RETEST_B: INSUFFICIENT
PULLBACK: TOO_RARE / DEPRIORITIZED
OOS: FAILED_GENERALIZATION_NO_EDGE_AFTER_COST (V2-A; locked Fresh Holdout untouched)
DATA_QUALITY: PARTIAL (Phase 4 safe-window gaps unresolved)
COHORT_BREADTH: LIMITED (60/100 metadata only)
EXTERNAL_VALIDATION: NOT_AVAILABLE (no local partitions or KIS process credentials)
SHADOW_NEXT_SESSION: NO (NOT_PROMOTED)
SHADOW_INFRA: PASS (2026-09-28 four-hour read-only simulated session)
PAPER: OUT_OF_SCOPE
LIVE: DISABLED
```

Phase 2, Phase 2.5, and Phase 3 evidence is summarized in [RESULTS.md](RESULTS.md). Manifests, event rows, and Phase 3 diagnostics are retained in ignored `runtime/research/`.

- Phase 3 analyzed only the 49-session Development window (2026-04-17–06-30) and 18-session Validation window (2026-07-01–07-27), using 30 current KOSPI listings and the fixed scanner proxy. The Phase 2.5 23-session Fresh Holdout (2026-07-28–08-28) remains `LOCKED_NOT_EVALUATED`; zero post-validation partitions were opened.
- Baseline Breakout was net-negative in all four interval/split cells. 15m PF/expectancy: Dev 0.406 / −₩383, Validation 0.151 / −₩495. 30m: Dev 0.757 / −₩250, Validation 0.101 / −₩648. All four cells were gross-negative before costs.
- V2-A not-overextended retained 75.1% of Dev signals and improved Dev PF to 0.483, but Validation remained PF 0.193 / −₩439 expectancy and false-breakout rate increased to 26.67% from 25.81%. V2 was rejected and not started in Shadow.
- Research data quality remains partial (broader cohort: 412 incomplete partitions, 956 missing minute slots); cohort survivorship bias and fixed current-cohort scanner ranking remain. Phase 3 verdict is not full-market KRX alpha evidence.
- Next phase: broaden clean prospective or genuinely untouched evidence before considering another strategy hypothesis. Do not use the locked Fresh Holdout to rescue v1/v2 results.

## Phase 4 current decision

- Phase 4 expanded the metadata cohort to 60/100 current listings (balanced KOSPI/KOSDAQ), but no broad historical minute data was available. This remains a current-listing cohort with survivorship bias.
- In the safe 67-session Development + secondary Validation window, 510 missing minute slots were all classified `UNKNOWN`; 0 confirmed retrieval gaps or legitimate no-trade minutes. No synthetic bars were added. The earlier full-window count of 956/412 remains historical Phase 2.5 reporting; the locked 2026-07-28–08-28 partition was not opened.
- Retest-A was negative after costs in all four interval/split cells. Retest-B reduced measured immediate rejection in four cells but had only 5–30 closed trades per cell, did not improve follow-through consistently, and was negative at 1.5x costs throughout. `BREAKOUT_FAMILY=REJECT`; no strategy proceeds to Shadow.
- Strategy freeze: `d0fad5bf18a3e1eae010dca21490ad15ba04dddd`; config SHA-256 `4076bb140d1e9452e7788bc2730497a77af11a28ece1bd8685c37eaddb34980b`. The untouched 2026-01-05–04-16 block had 0 cached partitions across path-only checks; KIS process credentials were absent and `.env` was not read, so `EXTERNAL_VALIDATION=NOT_AVAILABLE`.
- Current verdicts: `DATA_QUALITY=PARTIAL`, `COHORT_BREADTH=LIMITED`, `RETEST-A=REJECTED`, `RETEST-B=INSUFFICIENT`, `100K=CONSTRAINED`, `SHADOW_NEXT_SESSION=NO`. `ALPHA=UNPROVEN`; `LIVE=DISABLED`; `PAPER=OUT_OF_SCOPE`.
- Full Phase 4 rules, metrics, artifacts, and next-step boundary are recorded in [RESULTS.md](RESULTS.md).

- KIS read-only auth, quote, daily OHLCV, minute OHLCV, KOSPI/KOSDAQ indices, official universe master, market scan, and both strategy rankers returned valid responses.
- The research cache contains 30 current common-stock symbols over 17 KRX sessions (193,446 actual minute rows, 0 duplicates, 0 DQ errors).
- Four 15m/30m Breakout/Pullback OOS portfolios completed with 100,000 KRW, 20,000 KRW order cap, and fixed Phase 1 parameters. Each had 0 executed trades.
- The five-session final partition was previously touched by an invalid 31-symbol cohort run; it is marked contaminated and cannot support promotion.
- Phase 2.5 backfilled 30 current KOSPI symbols over 90 common sessions (2026-04-17–2026-08-28), 1,018,940 actual KIS minute rows. The dataset is diagnostic-only and partial: 956 expected minute slots are absent across 412 partial partitions; no bars were synthesized. Its fresh holdout remains locked and unevaluated.
- Expanded Breakout signal-level diagnostics were net negative in development and validation at 15m and 30m after assumed costs. Pullback produced only 19/10 development and 5/5 validation raw entries at 15m/30m; classify it `TOO_RARE` for this objective. Alpha remains unproven.
- The 2026-09-28 prospective Shadow ran from 09:00:00 to 13:00:00 KST at frozen commit `53cfc0662d9cf9537325578e35f50d55b325dc1b`. KIS confirmed the open, eight scanner cycles and 50 completed-bar decisions were captured; all 50 were `HOLD / REGIME_BLOCK`. There were 0 simulated fills, 0 order API calls, 0 account reads, 0 restarts, and 0 replay mismatches. This validates the collector path, not strategy profitability or promotion.
- The KIS order adapter remains unavailable, account reconciliation was not run, and live preflight remains fail-closed. The post-session auth, quote, daily-data, and scanner GET smoke succeeded; an earlier pre-market quote request failed with redacted `KisApiError` and a later retry succeeded.

Useful local checks: `uv run krx-trader doctor`, `uv run krx-trader status`, and `uv run krx-trader validate`. The last command is offline but re-evaluates the cached final partition; preserve the holdout contamination warning and do not treat repeated runs as independent evidence.

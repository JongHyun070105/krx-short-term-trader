# Project status

```text
ALPHA: UNPROVEN
RELATIVE_STRENGTH_FAMILY: REJECTED (gross-negative across Development and Secondary diagnostic)
RS_A: REJECTED (gross exp -0.296%, net exp -0.823%, PF 0.44)
RS_B: REJECTED (gross exp -0.053%, net exp -0.581%, PF 0.57)
BREAKOUT_FAMILY: REJECTED (Phase 3 v1/v2 and Phase 4 Retest evidence)
BREAKOUT_V2: NONE (no Validation candidate survived)
BREAKOUT_RETEST_A: REJECTED
BREAKOUT_RETEST_B: INSUFFICIENT
PULLBACK: TOO_RARE / DEPRIORITIZED
OOS: FAILED_GENERALIZATION_NO_EDGE_AFTER_COST (V2-A; locked Fresh Holdout untouched)
DATA_QUALITY: PARTIAL (current safe data: 2,442 partitions / 861,756 rows; 66,204 unresolved missing minute slots; holdout unopened)
COHORT_BREADTH: 33_STUDIED / 37_CURRENT_WITH_DATA (36 complete, 1 partial; 60-symbol target)
PHASE5_DATA_ACQUISITION: PARTIAL_INTERRUPTED (36 complete, 215000 has 30/67 sessions, 23 not started; manifest RUNNING is stale)
PHASE5_ARTIFACT_INTEGRITY: RECONCILED
EXTERNAL_VALIDATION: NOT_AVAILABLE (RS candidates gross-negative; untouched block not opened to avoid data snooping)
LOCKED_FRESH_HOLDOUT: LOCKED_NOT_EVALUATED (2026-07-28..08-28 untouched, fail-closed)
SHADOW_NEXT_SESSION: NO (NOT_PROMOTED)
SHADOW_INFRA: PASS (2026-09-28 four-hour read-only simulated session)
PAPER: OUT_OF_SCOPE
LIVE: DISABLED
```

Phase 2, Phase 2.5, Phase 3, and Phase 4 evidence is summarized in [RESULTS.md](RESULTS.md). Manifests, event rows, and Phase 5 diagnostics are retained in ignored `runtime/research/phase5/`.

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

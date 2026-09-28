# Project status

```text
ALPHA: UNPROVEN
BACKTEST: PASS (real multi-symbol run completed; no trades)
OOS: INSUFFICIENT_SAMPLE (0 / 30 minimum trades)
SHADOW: NOT_PROMOTED
SHADOW_INFRA: PASS (2026-09-28 four-hour read-only simulated session)
PAPER: OUT_OF_SCOPE
LIVE: DISABLED
```

Phase 2 and Phase 2.5 evidence is summarized in [RESULTS.md](RESULTS.md). The Phase 2 manifest and Phase 2.5 diagnostics are retained in ignored `runtime/research/`.

- KIS read-only auth, quote, daily OHLCV, minute OHLCV, KOSPI/KOSDAQ indices, official universe master, market scan, and both strategy rankers returned valid responses.
- The research cache contains 30 current common-stock symbols over 17 KRX sessions (193,446 actual minute rows, 0 duplicates, 0 DQ errors).
- Four 15m/30m Breakout/Pullback OOS portfolios completed with 100,000 KRW, 20,000 KRW order cap, and fixed Phase 1 parameters. Each had 0 executed trades.
- The five-session final partition was previously touched by an invalid 31-symbol cohort run; it is marked contaminated and cannot support promotion.
- Phase 2.5 backfilled 30 current KOSPI symbols over 90 common sessions (2026-04-17–2026-08-28), 1,018,940 actual KIS minute rows. The dataset is diagnostic-only and partial: 956 expected minute slots are absent across 412 partial partitions; no bars were synthesized. Its fresh holdout remains locked and unevaluated.
- Expanded Breakout signal-level diagnostics were net negative in development and validation at 15m and 30m after assumed costs. Pullback produced only 19/10 development and 5/5 validation raw entries at 15m/30m; classify it `TOO_RARE` for this objective. Alpha remains unproven.
- The 2026-09-28 prospective Shadow ran from 09:00:00 to 13:00:00 KST at frozen commit `53cfc0662d9cf9537325578e35f50d55b325dc1b`. KIS confirmed the open, eight scanner cycles and 50 completed-bar decisions were captured; all 50 were `HOLD / REGIME_BLOCK`. There were 0 simulated fills, 0 order API calls, 0 account reads, 0 restarts, and 0 replay mismatches. This validates the collector path, not strategy profitability or promotion.
- The KIS order adapter remains unavailable, account reconciliation was not run, and live preflight remains fail-closed. The post-session auth, quote, daily-data, and scanner GET smoke succeeded; an earlier pre-market quote request failed with redacted `KisApiError` and a later retry succeeded.

Useful local checks: `uv run krx-trader doctor`, `uv run krx-trader status`, and `uv run krx-trader validate`. The last command is offline but re-evaluates the cached final partition; preserve the holdout contamination warning and do not treat repeated runs as independent evidence.

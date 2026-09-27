# Project status

```text
ALPHA: UNPROVEN
BACKTEST: PASS (real multi-symbol run completed; no trades)
OOS: INSUFFICIENT_SAMPLE (0 / 30 minimum trades)
SHADOW: NOT_PROMOTED
PAPER: OUT_OF_SCOPE
LIVE: DISABLED
```

Phase 2 evidence is summarized in [RESULTS.md](RESULTS.md); the full manifest is in ignored `runtime/research/phase2-20260928T003848+0900.json`.

- KIS read-only auth, quote, daily OHLCV, minute OHLCV, KOSPI/KOSDAQ indices, official universe master, market scan, and both strategy rankers returned valid responses.
- The research cache contains 30 current common-stock symbols over 17 KRX sessions (193,446 actual minute rows, 0 duplicates, 0 DQ errors).
- Four 15m/30m Breakout/Pullback OOS portfolios completed with 100,000 KRW, 20,000 KRW order cap, and fixed Phase 1 parameters. Each had 0 executed trades.
- The five-session final partition was previously touched by an invalid 31-symbol cohort run; it is marked contaminated and cannot support promotion.
- No realtime Shadow collector, account read, order API, or live session was run. The order adapter remains unavailable and live preflight is fail-closed.

Useful local checks: `uv run krx-trader doctor`, `uv run krx-trader status`, and `uv run krx-trader validate`. The last command is offline but re-evaluates the cached final partition; preserve the holdout contamination warning and do not treat repeated runs as independent evidence.

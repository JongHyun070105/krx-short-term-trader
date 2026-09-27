# Project status

```text
ALPHA: UNPROVEN
BACKTEST: NOT_RUN
OOS: NOT_RUN
SHADOW: NOT_STARTED
PAPER: OUT_OF_SCOPE
LIVE: DISABLED
```

Evidence for this checkout:

- Repository began with only a placeholder README; there was no historical dataset or prior validation report.
- Offline unit tests use synthetic fixtures only and do not count as market evidence.
- KIS auth, account-read, REST market-data, and WebSocket calls were not run.
- `shadow-replay` accepts a supplied OHLCV file; no realtime collector is wired into it.
- `live-preflight` is fail-closed; the live order adapter is unavailable.

Commands: `uv run pytest -q`, `uv run krx-trader doctor`, `uv run krx-trader status`.

The machine-readable initial status is written to ignored `runtime/project_status.json` by `krx-trader status`.

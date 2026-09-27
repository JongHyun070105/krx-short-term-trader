# Live readiness

Final: **MICRO_LIVE_NOT_READY**

| Gate | State | Evidence |
|---|---|---|
| Secret safe / `.env` ignored | PASS | `.env` is ignored and untracked; no local history contains it. |
| `main` branch / origin | PASS | Local `main` tracks `origin/main`; delivery audit recorded at commit. |
| KIS token/auth | NOT_RUN | No credentialed request was made. |
| REST market data | NOT_RUN | No KIS request was made. |
| WebSocket data | NOT_RUN | No stream was opened; raw message parsing is incomplete. |
| Historical dataset and DQ | NOT_RUN | No real dataset is present. Unit fixtures only. |
| Backtest / golden accounting | PARTIAL | Synthetic golden PnL and next-bar behavior covered by tests. |
| OOS / untouched final test | NOT_RUN | No actual data. Final partition remains untouched. |
| Cost stress | NOT_RUN | Cost model supports multipliers; no market results. |
| Realtime Shadow | NOT_STARTED | CSV replay exists; live market collector is not connected. |
| Risk sizing / gates | PARTIAL | Deterministic sizing and fail-closed entry gates tested. |
| Bot-owned cash / positions | PARTIAL | SQLite updates cash/positions from unique fills; account reconciliation is absent. |
| Order lifecycle / UNKNOWN | PARTIAL | State machine and fill idempotency tested; broker submission/reconciliation unavailable. |
| Kill switch / daily risk persistence | PARTIAL | Local daily state persists and corrupt state blocks; operational triggers are not wired end-to-end. |
| Read-only account smoke | NOT_RUN | Account/balance/open-order calls were not implemented or called. |
| Live order adapter | DISABLED | Adapter always blocks after local gates; no KIS order endpoint exists. |
| Live preflight | FAIL_CLOSED | Always `LIVE_READY=false` while required evidence and adapter are unavailable. |

No result in this table authorizes live trading. The initial bot cap is 100,000 KRW and order cap is 20,000 KRW, but configuration alone is not a readiness pass.

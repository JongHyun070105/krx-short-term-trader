# Live readiness

Final: **MICRO_LIVE_NOT_READY**

| Gate | State | Evidence |
|---|---|---|
| Secret safe / `.env` ignored | PASS | `.env` remains ignored and untracked; current credential-value scan found no matches in changes; no `.env` file exists in Git history. |
| KIS token/auth | PASS | `uv run krx-trader auth-test --read-only` returned token available; following real KIS GET requests were accepted. No token value was printed. |
| Quote | PASS | 005930 response parsed price/OHLC/volume/turnover at 2026-09-28 00:35 KST. This is local retrieval time outside regular trading, not a provider quote timestamp. |
| Daily market data | PASS | 005930 and 000660 each returned 17 ordered daily bars, 2026-09-01–23. Research set quality summary: 0 duplicate rows, 0 DQ errors. |
| Minute market data | PASS | 005930 2026-09-23 returned 380 regular-session bars, 09:00–15:19 KST; timestamp convention is `bar_start`. The 15:20–15:30 closing auction is excluded. |
| KOSPI / KOSDAQ index | PASS | Daily index code 0001 and 1001 each returned 50 bars for 2026-07-14–2026-09-23. |
| Official universe | PASS | KIS official KOSPI/KOSDAQ master refresh: 3,545 listings, 2,592 common stocks. Market fields include instrument type, halt, management and warning indicators. |
| Market scanner | PASS | Actual volume/turnover rank union returned 6 eligible candidates; 30 ranked rows were excluded by filters. Scanner ran outside regular hours; outputs verify parsing/filters only. |
| Strategy rankers | PASS | Actual Breakout and Pullback daily-bar scans each returned 6 candidates with fixed 0–1 priority scores and reason codes; no data failures. Scores do not imply entry. |
| Historical dataset and DQ | PASS | 30/30 symbols, 17 sessions each, 193,446 minute bars, 0 duplicate rows, 0 DQ errors. Parquet and provenance sidecars are ignored by Git. |
| Backtest / portfolio accounting | PASS | Four real-data 15m/30m Breakout/Pullback portfolios completed at 100,000 KRW; each recorded 0 trades and ₩0 PnL. These are execution results, not strategy passes. |
| OOS / final partition | INSUFFICIENT_SAMPLE | 0 OOS trades vs fixed minimum 30. Final 2026-09-17–23 partition is contaminated by an earlier invalid 31-symbol run; no promotion allowed. |
| Cost stress | FAIL / NO EVIDENCE | 1.5x and 2.0x runs completed but each had 0 trades and 0% return; positive cost robustness is unproven. |
| Scanner / regime A/B | INCONCLUSIVE | All eligible, Top 10 Regime ON, and Top 10 Regime OFF each had 0 portfolio fills. The sample cannot establish scanner uplift. |
| Realtime Shadow | NOT_PROMOTED | No strategy passed the OOS gate; no realtime collector or session was started. |
| Risk sizing / gates | PARTIAL | Phase 1 unit coverage remains. OOS Regime-OFF signals were blocked by one-share risk budget, order-cap, or price constraints. |
| Bot-owned cash / positions | PARTIAL | SQLite still tracks unique fills; broker account reconciliation was not added or used. Backtest portfolio stayed within 100,000 KRW and a 20,000 KRW order cap. |
| Order lifecycle / UNKNOWN | PARTIAL | Local state-machine/idempotency tests pass; broker submission and reconciliation remain unavailable. |
| Kill switch / daily risk persistence | PARTIAL | Local state/gates are tested; operational live triggers are not wired end-to-end. |
| Read-only account smoke | NOT_RUN | Account/balance/open-order endpoint is not implemented and was not called. |
| Live order adapter | DISABLED | No KIS order/cancel endpoint was implemented or called. |
| Live preflight | FAIL_CLOSED | `LIVE_READY=false`; market-data connectivity and backtests do not satisfy OOS, Shadow, account-reconciliation, or adapter requirements. |

`TRADING_MODE=shadow` and `LIVE_TRADING_ENABLED=false` remain in effect. Nothing in this report authorizes paper or live trading.

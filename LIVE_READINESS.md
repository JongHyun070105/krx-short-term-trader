# Live readiness

Final: **MICRO_LIVE_NOT_READY**

| Gate | State | Evidence |
|---|---|---|
| Secret safe / `.env` ignored | PASS (targeted scan) | `.env` remains ignored and untracked and was not read. Targeted AWS/private-key/GitHub/Slack/Google/OpenAI patterns had 0 matches across tracked Git history; this is not a universal secret detector. |
| KIS token/auth | PASS | `uv run krx-trader auth-test --read-only` returned token available; following real KIS GET requests were accepted. No token value was printed. |
| Quote | PASS | Post-session 005930 response parsed at 2026-09-28 13:02 KST. Two pre-market quote GETs had redacted `KisApiError`; this later read-only retry succeeded. Retrieval time is not a provider quote timestamp. |
| Daily market data | PASS | Earlier 005930 and 000660 each returned 17 ordered daily bars, 2026-09-01–23. Post-session 005930 daily-data smoke exited successfully. Research set quality summary: 0 duplicates, 0 DQ errors. |
| Minute market data | PASS | Historical 005930 2026-09-23 returned 380 regular-session bars, 09:00–15:19 KST; timestamp convention is `bar_start`. The 15:20–15:30 closing auction is excluded. Prospective 2026-09-28 capture added 512 minute rows across three monitored symbols through 12:58 KST. |
| KOSPI / KOSDAQ index | PASS | Daily index code 0001 and 1001 each returned 50 bars for 2026-07-14–2026-09-23. |
| Official universe | PASS | KIS official KOSPI/KOSDAQ master refresh: 3,545 listings, 2,592 common stocks. Market fields include instrument type, halt, management and warning indicators. |
| Market scanner | PASS | Earlier volume/turnover rank union returned 6 eligible candidates outside regular hours. In the prospective session, 8 live cycles ran; the final shortlist had one eligible candidate (047040). Outputs verify live parsing/ranking, not edge. |
| Strategy rankers | PASS | Actual Breakout and Pullback daily-bar scans each returned 6 candidates with fixed 0–1 priority scores and reason codes; no data failures. Scores do not imply entry. |
| Historical dataset and DQ | PASS | 30/30 symbols, 17 sessions each, 193,446 minute bars, 0 duplicate rows, 0 DQ errors. Parquet and provenance sidecars are ignored by Git. |
| Backtest / portfolio accounting | PASS | Four real-data 15m/30m Breakout/Pullback portfolios completed at 100,000 KRW; each recorded 0 trades and ₩0 PnL. These are execution results, not strategy passes. |
| OOS / final partition | INSUFFICIENT_SAMPLE | 0 OOS trades vs fixed minimum 30. Final 2026-09-17–23 partition is contaminated by an earlier invalid 31-symbol run; no promotion allowed. |
| Cost stress | FAIL / NO EVIDENCE | 1.5x and 2.0x runs completed but each had 0 trades and 0% return; positive cost robustness is unproven. |
| Scanner / regime A/B | INCONCLUSIVE | All eligible, Top 10 Regime ON, and Top 10 Regime OFF each had 0 portfolio fills. The sample cannot establish scanner uplift. |
| Realtime Shadow infrastructure | PASS | 2026-09-28 09:00:00–13:00:00 KST, clean frozen SHA `53cfc0662d9cf9537325578e35f50d55b325dc1b`, 8 scanner cycles, 50 decisions, 0 simulated fills, 0 API/stale errors, 0 restarts, 0 order API calls; replay parity 0/50 mismatches. |
| Strategy Shadow promotion | NO | All 50 live decisions were `HOLD / REGIME_BLOCK` under HIGH_VOL. This validates collection/decision/replay infrastructure, not strategy alpha; `ALPHA=UNPROVEN`, no candidate promoted. |
| Risk sizing / gates | PARTIAL | Phase 1 unit coverage remains. Expanded 100K feasibility matrix found more mechanical fills at relaxed diagnostic caps/risk but no positive Validation PnL; the frozen research sizing scenario is not a live recommendation. |
| Bot-owned cash / positions | PARTIAL | SQLite still tracks unique fills; broker account reconciliation was not added or used. Backtest portfolio stayed within 100,000 KRW and a 20,000 KRW order cap. |
| Order lifecycle / UNKNOWN | PARTIAL | Local state-machine/idempotency tests pass; broker submission and reconciliation remain unavailable. |
| Kill switch / daily risk persistence | PARTIAL | Local state/gates are tested; operational live triggers are not wired end-to-end. |
| Read-only account smoke | NOT_RUN | Account/balance/open-order endpoint is not implemented and was not called. |
| Live order adapter | DISABLED | No KIS order/cancel endpoint was implemented or called. |
| Live preflight | FAIL_CLOSED | `LIVE_READY=false`; market-data connectivity and backtests do not satisfy OOS, Shadow, account-reconciliation, or adapter requirements. |

`TRADING_MODE=shadow` and `LIVE_TRADING_ENABLED=false` remain in effect. Account/balance/open-order endpoints were not called. The post-session smoke used token-only auth, quote, daily market data, and market scan GETs only. Nothing in this report authorizes paper or live trading.

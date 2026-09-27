# Results

No real-market backtest, OOS validation, cost stress test, or shadow session has been run. No strategy is a candidate for shadow or live use.

| Strategy | Data period | Trades | Net return | MDD | PF | Expectancy | Cost stress | Verdict |
|---|---|---:|---:|---:|---:|---:|---|---|
| Breakout + volume | NOT_RUN | N/A | N/A | N/A | N/A | N/A | NOT_RUN | CANDIDATE / UNASSESSED |
| Pullback + rebreak | NOT_RUN | N/A | N/A | N/A | N/A | N/A | NOT_RUN | CANDIDATE / UNASSESSED |

Offline golden PnL and signal tests establish code behavior only. They are not evidence of alpha. The final untouched test partition has not been evaluated or used for tuning.

Initial promotion gate, fixed before results: at least 30 OOS trades, OOS expectancy > 0, PF > 1, MDD <= 3%, positive net return at both 1.5x and 2.0x modeled costs, and positive expectancy/PF > 1 across the configured parameter neighborhood. See `evaluate_strategy_gate` in `src/krx_trader/backtest/validation.py`.

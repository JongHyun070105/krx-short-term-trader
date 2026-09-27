# KRX Short-Term Trader

Research-first tooling for KRX common stocks. The current release provides local configuration checks, KIS read-only market-data adapters, deterministic bar validation/resampling, two shared signal rules, a cost-aware next-bar backtester, a replay harness, and a bot-owned SQLite fill ledger. It does not establish that either strategy has an edge.

## Current status

See [STATUS.md](STATUS.md), [RESULTS.md](RESULTS.md), and [LIVE_READINESS.md](LIVE_READINESS.md). Initial state is `ALPHA=UNPROVEN`, `BACKTEST=NOT_RUN`, `OOS=NOT_RUN`, `SHADOW=NOT_STARTED`, `PAPER=OUT_OF_SCOPE`, and `LIVE=DISABLED`.

## Architecture

```text
KIS read-only market data / supplied OHLCV
  -> quality checks -> KST session bars -> shared strategy functions
  -> decision + risk sizing -> next-bar backtest or shadow replay
  -> SQLite decisions, orders, fills, bot-owned positions, audit events
```

Signals do not submit orders. An order is not a fill; bot positions change only from unique fill records. SQLite does not import existing account holdings as bot-owned. The live boundary requires `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, `--confirm-live`, the hard capital cap, and a per-order cap. The KIS order adapter itself is unavailable in this release, so live submission remains blocked.

## Install

Python 3.11 or newer is required. `uv` is recommended:

```bash
uv sync --extra dev
cp .env.example .env
uv run krx-trader doctor
uv run krx-trader status
```

Fill `.env` locally. The doctor prints only whether each credential is present; it does not print credential values or contact KIS. `.env`, token cache, runtime database, market data, logs, and test caches are ignored by Git.

## Environment and operating mode

Required local KIS fields are `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO` (first eight digits), `KIS_ACCOUNT_PRODUCT_CODE` (two digits, normally `01`), and `KIS_HTS_ID`. Paper trading is out of scope and has no configuration path.

Defaults are `TRADING_MODE=shadow`, `LIVE_TRADING_ENABLED=false`, `MAX_LIVE_CAPITAL_KRW=100000`, and `MAX_ORDER_NOTIONAL_KRW=20000`. The live capital cap cannot be configured above 100,000 KRW. Fee, tax, and slippage defaults in `.env.example` are assumptions, not verified KIS terms; replace them with the account's current schedule before interpreting backtests.

## KIS API

The adapter follows Korea Investment's [official open-trading-api repository](https://github.com/koreainvestment/open-trading-api). Implemented REST methods are token reuse/cache, current quote, daily OHLCV, and paged intraday bars. Token cache is under ignored `runtime/` with owner-only permissions. GET retries are bounded; errors omit response bodies and credentials. The official minute-bar example describes a 120-row response limit and up to one year of retained history; actual account/API availability is unverified here ([official minute-bar example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py)).

The WebSocket adapter supports approval-key acquisition, KRX trade subscription/unsubscription, heartbeat, stale-feed detection, bounded backoff, and shutdown. The REST minute-bar method requires the caller to explicitly confirm whether returned timestamps label the minute start or end; that mapping has not been verified against a live response. No WebSocket connection was opened in this work. No KIS account, balance, buying-power, order, fill-notice, or order-history endpoint is implemented or called.

To perform token-only connectivity locally, after reviewing the request scope:

```bash
uv run krx-trader auth-test --read-only
```

This sends credentials to KIS for token issuance/reuse. It prints no token. It does not read an account or submit an order.

## Data and strategies

Historical CSV input must contain `timestamp,open,high,low,close,volume`; timestamps must be ISO-8601 with timezone offset. Data is validated for ordering, duplicates, OHLC consistency, positive prices, volume, and timezone. Minute bars resample from the 09:00 KST session boundary; incomplete buckets are discarded.

Implemented shared rules:

- `breakout`: prior completed range high plus volume confirmation.
- `pullback`: impulse, structure-preserving lower-volume pullback, then local rebreak.

Both return explicit `HOLD` reasons and suppress new long entries in `DOWN` and `HIGH_VOL` regimes. Missing or insufficient regime history also blocks entry. Backtest and replay default to requiring both KOSPI and KOSDAQ daily index CSVs; only prior index sessions are considered for each stock bar. Use `--regime-filter off` only for the explicit OFF-vs-ON comparison. Regime thresholds and strategy defaults are initial research parameters, not calibrated recommendations.

## Backtest and replay

```bash
uv run krx-trader backtest breakout --input data/bars.csv --symbol 005930 --kospi-index-input data/kospi.csv --kosdaq-index-input data/kosdaq.csv
uv run krx-trader shadow-replay breakout --input data/bars.csv --symbol 005930 --kospi-index-input data/kospi.csv --kosdaq-index-input data/kosdaq.csv
```

The fill model uses a completed-bar signal and the next bar's open, then applies explicit slippage, fees, and sell tax. Stops use an adverse gap-aware approximation. A position still open at the end is marked to market and reported separately; it is not silently treated as a completed trade. Synthetic fixtures are used only in tests.

Chronological 55/20/25 splitting and a fixed candidate gate are available in the validation module. The fixed gate requires at least 30 OOS trades, positive expectancy, profit factor above 1, drawdown no greater than 3%, positive 1.5x and 2.0x cost-stress returns, and positive nearby-parameter expectancy/PF. These are initial governance thresholds and must not be changed after looking at a final holdout. No real-data validation has been run.

`shadow-replay` only replays a supplied data file; it is not a realtime shadow collector. It does not call KIS or submit broker orders. Live-market Shadow remains `NOT_STARTED` until data-source provenance, stream parsing, operational monitoring, and session evidence are validated.

## Risk and live safety

Sizing starts from the configured cash risk budget divided by stop distance, then applies one-share granularity, available bot cash, exposure, per-order cap, price band, fees, tax reserve, and slippage. Zero shares means skip. Daily risk state survives restarts in ignored `runtime/`; corrupt risk state fails closed.

`krx-trader live-preflight` always reports `LIVE_READY=false` until account reconciliation, OOS, cost stress, Shadow, and a reviewed live adapter exist. There is no `live run` command in this release. Order state transitions include `UNKNOWN`; a submission timeout must be reconciled before another attempt.

## Known limitations

- No real KIS request, account smoke, realtime collection, or market-session run was performed.
- No historical dataset was supplied in the checkout, so backtest/OOS/cost-stress results are `NOT_RUN`.
- The realtime WebSocket callback receives raw provider messages; completed-bar parsing and end-to-end Shadow collection are not yet connected.
- No account/buying-power/open-order reconciliation, KIS order/cancel adapter, or complete live preflight exists.
- No full market-wide universe scanner or Parquet cache exists; CSV is the current manual research input.
- The cost defaults are assumptions and need account-specific verification.

The attached `v7_rebalancing.zip` was reviewed only as an idea source; its hard-coded balance, fake-success order methods, random prices, TODO execution path, and partial accounting were not copied. The supplied education PDF informed the emphasis on regime/risk filters, HOLD logging, execution costs, kill switch, audit trail, and time-separated validation; its example indicator values were not adopted as truth.

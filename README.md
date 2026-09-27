# KRX Short-Term Trader

Research-first tooling for KRX common stocks. The current release provides local configuration checks, KIS read-only market-data adapters, deterministic bar validation/resampling, two shared signal rules, a cost-aware next-bar backtester, a replay harness, and a bot-owned SQLite fill ledger. It does not establish that either strategy has an edge.

## Current status

See [STATUS.md](STATUS.md), [RESULTS.md](RESULTS.md), and [LIVE_READINESS.md](LIVE_READINESS.md). Phase 2 recorded `ALPHA=UNPROVEN`, `BACKTEST=PASS` (research run completed with no fills), `OOS=INSUFFICIENT_SAMPLE`, `SHADOW=NOT_PROMOTED`, `PAPER=OUT_OF_SCOPE`, and `LIVE=DISABLED`.

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

Defaults are `TRADING_MODE=shadow`, `LIVE_TRADING_ENABLED=false`, `MAX_LIVE_CAPITAL_KRW=100000`, and `MAX_ORDER_NOTIONAL_KRW=20000`. The live capital cap cannot be configured above 100,000 KRW. Scanner eligibility and sizing both enforce the effective one-share ceiling: the lower of `MAX_PRICE_KRW` and the per-order notional remaining after buy slippage and fee. With the example fee and slippage assumptions, the 20,000 KRW order cap gives an effective scanner ceiling of 19,967 KRW. Sizing takes the assumed buy fill price as its entry input and includes the modeled stop fill, fees, and sell tax in per-share risk. A candidate beyond the order cap reports `UNAFFORDABLE_ONE_SHARE`. Fee, tax, and slippage defaults in `.env.example` are assumptions, not verified KIS terms; replace them with the account's current schedule before interpreting backtests.

## KIS API

The adapter follows Korea Investment's [official open-trading-api repository](https://github.com/koreainvestment/open-trading-api). Read-only REST methods cover token reuse/cache, current quote, daily OHLCV, minute bars, index series, activity rankings, and the official KOSPI/KOSDAQ stock master. GET retries are bounded; a shared persistent limiter records provider cooldowns, and errors omit response bodies and credentials. Actual smoke results and the date limits of this collection are recorded in `LIVE_READINESS.md` and `RESULTS.md`.

Minute timestamps are interpreted as bar starts (`MINUTE_TIMESTAMP_CONVENTION=bar_start`), verified against a live 005930 session: 09:00 through 15:19 KST, with the 15:20–15:30 closing auction excluded. Resampling is anchored at 09:00. The WebSocket adapter remains unverified in a live session. No KIS account, balance, buying-power, order, fill-notice, or order-history endpoint is implemented or called.

To perform token-only connectivity locally, after reviewing the request scope:

```bash
uv run krx-trader auth-test --read-only
```

This sends credentials to KIS for token issuance/reuse. It prints no token. It does not read an account or submit an order.

## Data, universe, and scanner

`universe refresh` downloads the official KIS master and stores it in ignored `data/universe/`. Eligibility filters KOSPI/KOSDAQ common stock listings by active status, price (`MIN_PRICE_KRW` / `MAX_PRICE_KRW`, defaults 1,000–50,000 KRW), trading status where supplied, and configured turnover (default 50 million KRW). ETF/ETN, preferred, SPAC, halted, and unreliable/unrecognized instruments are excluded. A KIS activity shortlist unions volume and turnover ranks, deduplicates symbols, and applies those filters. Breakout and Pullback rankers are separate fixed-weight deep-scan priorities with reason codes; their scores are not entry signals. Shared strategy functions still decide ENTER/HOLD.

```bash
uv run krx-trader universe refresh
uv run krx-trader scan market --top 50
uv run krx-trader scan breakout --top 20
uv run krx-trader scan pullback --top 20
uv run krx-trader data quote 005930
uv run krx-trader data daily 005930 --start 2026-09-01 --end 2026-09-23
uv run krx-trader data minute 005930 --date 2026-09-23
uv run krx-trader data index kospi --start 2026-07-01 --end 2026-09-23
uv run krx-trader data index kosdaq --start 2026-07-01 --end 2026-09-23
```

Daily/index/minute bars use an ignored Parquet cache under `data/`, with per-partition provenance, requested range/session, row count, timestamps, and content hash. `data build-research-set` checkpoints each symbol/session partition and can resume after interruption. Collection is separate from offline backtesting; research commands never call KIS.

## Bars and strategies

Historical CSV input must contain `timestamp,open,high,low,close,volume`; timestamps must be ISO-8601 with timezone offset. Data is validated for ordering, duplicates, OHLC consistency, positive prices, volume, and timezone. Minute bars resample from the 09:00 KST session boundary; incomplete buckets are discarded.

Implemented shared rules:

- `breakout`: prior completed range high plus volume confirmation.
- `pullback`: impulse, structure-preserving lower-volume pullback, then local rebreak.

Both return explicit `HOLD` reasons and suppress new long entries in `DOWN` and `HIGH_VOL` regimes. Missing or insufficient regime history also blocks entry. Backtest and replay default to requiring both KOSPI and KOSDAQ daily index CSVs; only prior index sessions are considered for each stock bar. Use `--regime-filter off` only for the explicit OFF-vs-ON comparison. The volatility measure is the sample standard deviation of daily log returns scaled by `sqrt(N)`, so `REGIME_HIGH_VOL_THRESHOLD=0.025` means 2.5% aggregate volatility across the configured N-session window, not annualized volatility. The threshold and strategy defaults are initial research parameters, not calibrated recommendations.

## Backtest and replay

```bash
uv run krx-trader backtest breakout --input data/bars.csv --symbol 005930 --kospi-index-input data/kospi.csv --kosdaq-index-input data/kosdaq.csv
uv run krx-trader shadow-replay breakout --input data/bars.csv --symbol 005930 --kospi-index-input data/kospi.csv --kosdaq-index-input data/kosdaq.csv
```

The fill model uses a completed-bar signal and the next bar's open, then applies explicit slippage, fees, and sell tax. Stops use an adverse gap-aware approximation. A position still open at the end is marked to market and reported separately; it is not silently treated as a completed trade. Synthetic fixtures are used only in tests.

Offline portfolio research runs Breakout and Pullback independently at 15m and 30m, uses a 100,000 KRW capital cap, 20,000 KRW order cap, share-level quantities, next-bar entries, a deterministic liquidity priority, and separately reports base, 1.5x, and 2.0x modeled costs. It compares eligible-universe vs scanner Top N and regime ON vs OFF, plus concentration and market/price/liquidity buckets. Dataset splits are chronological 55/20/25; one prior session is used only as signal warmup, and fills remain inside each partition. Run:

```bash
uv run krx-trader research breakout --interval 15m
uv run krx-trader research pullback --interval 30m
uv run krx-trader validate
```

The fixed candidate gate requires at least 30 OOS trades, positive expectancy, PF > 1, MDD <= 3%, positive 1.5x/2.0x cost-stress returns, and positive expectancy/PF across the configured parameter neighborhood. These thresholds are not tuned after viewing final results. The current final partition is marked touched in `RESULTS.md`; no Shadow promotion is allowed from it.

`shadow-replay` only replays a supplied data file; it is not a realtime shadow collector. It does not call KIS or submit broker orders. Live-market Shadow remains `NOT_STARTED` until data-source provenance, stream parsing, operational monitoring, and session evidence are validated.

`shadow-live` polls KIS read-only market-rank, minute-data, and daily-index endpoints during a scheduled KST window (default 09:00–13:00). It confirms the session from KIS minute data, refreshes the eligible Top 20 every 30 minutes, deep-monitors up to five candidates, uses only complete session-anchored 15m/30m bars, and logs all Breakout/Pullback decisions. Simulated entries use the next one-minute bar after the decision was observed and apply the configured assumed costs; no broker order endpoint is available to this runner. Current-session bars, historical warmup bars, the exact index inputs, scanner events, resumable state, and the run manifest are written under `runtime/shadow/<run-id>/` so the signal path can be replayed from its captured inputs. At the stop time, open simulated positions receive an `OBSERVATIONAL_MARK` and are not force-closed. Costs remain `ASSUMED`; this evidence validates collection and pipeline behavior, not strategy profitability or live readiness.

Start the command before the scheduled opening if it should wait without querying KIS until 09:00:

```bash
uv run krx-trader shadow-live --run-id shadow-20260928 --start 09:00 --stop 13:00
uv run krx-trader shadow-verify --run-dir runtime/shadow/shadow-20260928
```

An optional frozen `ResearchScenario` JSON file can add a second independent simulated sizing ledger. The command still requires `TRADING_MODE=shadow` and `LIVE_TRADING_ENABLED=false`.

## Risk and live safety

Sizing starts from the configured cash risk budget divided by stop distance, then applies one-share granularity, available bot cash, exposure, per-order cap, price band, fees, tax reserve, and slippage. Zero shares means skip. Daily risk state survives restarts in ignored `runtime/`; corrupt risk state fails closed.

`krx-trader live-preflight` always reports `LIVE_READY=false` until account reconciliation, OOS, cost stress, Shadow, and a reviewed live adapter exist. There is no `live run` command in this release. Order state transitions include `UNKNOWN`; a submission timeout must be reconciled before another attempt.

## Known limitations

- The realtime collector currently uses bounded REST minute polling; WebSocket trade-message parsing is not connected to completed bars.
- No account/buying-power/open-order reconciliation, KIS order/cancel adapter, or complete live preflight exists.
- Historical universe membership comes from the current stock master, so delisting/survivorship bias remains.
- One short recent dataset cannot establish robust alpha; inspect `RESULTS.md` before using any strategy conclusion.
- The cost defaults are assumptions and need account-specific verification.

The supplied `v6_scanner.zip` was reviewed only for the shortlist pipeline concept. Its README claims exceed its code; the scanner mixes unrelated MA/RSI/MACD/Bollinger rules, leaves filters and volume surge incomplete, and its sample KIS order methods report fake success and hard-code a balance. None of its code was copied. Scanner output in this project is only a priority list and never an order signal.

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo

from krx_trader.backtest.costs import CostModel
from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.quality import inspect_bars
from krx_trader.data.resample import resample_session_minutes
from krx_trader.kis.rest import KisApiError, KisRestClient
from krx_trader.market.regime import Regime, classify_regime
from krx_trader.models import Bar, Decision
from krx_trader.research.scenario import ResearchScenario
from krx_trader.risk.sizing import size_long_position
from krx_trader.strategies.breakout import DEFAULT_BREAKOUT_CONFIG, evaluate_breakout
from krx_trader.strategies.pullback import DEFAULT_PULLBACK_CONFIG, evaluate_pullback
from krx_trader.universe.scanner import scan_market

KST = ZoneInfo("Asia/Seoul")
STRATEGIES = ("breakout", "pullback")
INTERVALS = (15, 30)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    with temp.open("r+b") as stream:
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class RealtimeShadowRunner:
    """KIS read-only minute polling with deterministic simulated fills and append-only audit."""

    def __init__(
        self,
        settings: Settings,
        client: KisRestClient,
        *,
        run_id: str,
        scheduled_start: datetime,
        stop_at: datetime,
        research_scenario: ResearchScenario | None = None,
        root: Path = Path("runtime/shadow"),
        index_bars: dict[str, list[Bar]] | None = None,
        scanner: Callable = scan_market,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
        sleeper: Callable[[float], None] = time.sleep,
        poll_seconds: int = 60,
        monitor_top: int = 5,
        cache: ParquetBarCache | None = None,
    ) -> None:
        if settings.trading_mode != "shadow" or settings.live_trading_enabled:
            raise ValueError("realtime Shadow requires TRADING_MODE=shadow and LIVE_TRADING_ENABLED=false")
        if not 10 <= poll_seconds <= 300 or not 1 <= monitor_top <= 10:
            raise ValueError("poll interval or deep-monitor count is outside safe bounds")
        if scheduled_start.tzinfo is None or stop_at.tzinfo is None or scheduled_start >= stop_at:
            raise ValueError("scheduled start and stop must be ordered timezone-aware timestamps")
        if stop_at - scheduled_start > timedelta(hours=4, minutes=5):
            raise ValueError("a realtime Shadow run cannot exceed four hours and five minutes")
        if research_scenario is not None and research_scenario.regime_mode != "on":
            raise ValueError("realtime sizing sensitivity must keep the frozen current regime gate enabled")
        self.settings = settings
        self.client = client
        self.run_id = run_id
        self.scheduled_start = scheduled_start.astimezone(KST)
        self.stop_at = stop_at.astimezone(KST)
        self.research_scenario = research_scenario
        self.directory = root / run_id
        self.events_path = self.directory / "events.jsonl"
        self.bars_path = self.directory / "bars.jsonl"
        self.warmup_bars_path = self.directory / "warmup_bars.jsonl"
        self.index_path = self.directory / "indexes.json"
        self.state_path = self.directory / "state.json"
        self.manifest_path = self.directory / "manifest.json"
        self.scanner = scanner
        self.clock = clock
        self.sleeper = sleeper
        self.poll_seconds = poll_seconds
        self.monitor_top = monitor_top
        self.cache = cache or ParquetBarCache()
        self._warmup: dict[str, list[Bar]] = {}
        self._warmup_attempted: set[str] = set()
        self.index_bars = index_bars or {}
        self._last_scan: datetime | None = None
        self._monitor_symbols: list[str] = []
        self._candidate_rank: dict[str, int] = {}
        self._market_confirmed = False
        self._stop_requested = False
        self._index_refresh_done = bool(index_bars)
        self._last_index_attempt: datetime | None = None
        self._events_seen: set[str] = set()
        self._bars_seen: set[str] = set()
        self._warmup_seen: set[str] = set()
        self._manifest = self._load_or_create_manifest()
        self._state = self._load_state()
        self._market_confirmed = self._manifest.get("market_status") == "OPEN_CONFIRMED_BY_KIS_MINUTE_DATA"
        self._lock_stream = None

    def _safe_settings(self) -> dict[str, object]:
        return {
            "trading_mode": self.settings.trading_mode,
            "live_trading_enabled": self.settings.live_trading_enabled,
            "capital_cap_krw": self.settings.max_live_capital_krw,
            "order_cap_krw": self.settings.max_order_notional_krw,
            "risk_per_trade_pct": self.settings.risk_per_trade_pct,
            "max_positions": self.settings.max_concurrent_positions,
            "max_daily_trades": self.settings.max_daily_trades,
            "min_price_krw": self.settings.min_price_krw,
            "effective_max_price_krw": self.settings.effective_max_price_krw,
            "min_turnover_krw": self.settings.min_turnover_krw,
            "costs_assumed": {
                "status": "ASSUMED",
                "broker_fee_rate": self.settings.broker_fee_rate,
                "sell_tax_rate": self.settings.sell_tax_rate,
                "slippage_bps": self.settings.slippage_bps,
            },
        }

    def _strategy_configuration(self) -> dict[str, object]:
        return {
            "breakout": asdict(DEFAULT_BREAKOUT_CONFIG),
            "pullback": asdict(DEFAULT_PULLBACK_CONFIG),
            "regime_threshold": self.settings.regime_high_vol_threshold,
            "regime_lookbacks": {
                "trend": self.settings.regime_trend_lookback,
                "volatility": self.settings.regime_volatility_lookback,
            },
        }

    def _scanner_configuration(self) -> dict[str, object]:
        metadata_path = Path("data/universe/stocks.metadata.json")
        universe_hash = None
        if metadata_path.is_file():
            try:
                universe_hash = json.loads(metadata_path.read_text(encoding="utf-8")).get("parquet_sha256")
            except (OSError, json.JSONDecodeError):
                universe_hash = None
        return {
            "implementation": getattr(self.scanner, "__name__", type(self.scanner).__name__),
            "rank_sources": ["volume", "turnover"],
            "market_rank_limit": 50,
            "candidate_top_n": 20,
            "refresh_seconds": 1800,
            "deep_monitor_top_n": self.monitor_top,
            "min_turnover_krw": self.settings.min_turnover_krw,
            "min_price_krw": self.settings.min_price_krw,
            "effective_max_price_krw": self.settings.effective_max_price_krw,
            "universe_master_sha256": universe_hash,
        }

    def _git_evidence(self) -> dict[str, object]:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=5
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], check=True, capture_output=True, text=True, timeout=5
            ).stdout.strip()
            return {"git_sha": sha, "working_tree_clean": not dirty}
        except (OSError, subprocess.SubprocessError):
            return {"git_sha": None, "working_tree_clean": False, "git_evidence": "NOT_VERIFIABLE"}

    def _load_or_create_manifest(self) -> dict[str, object]:
        config = self._safe_settings()
        scenario = None if self.research_scenario is None else {
            "name": self.research_scenario.name,
            "capital_krw": self.research_scenario.capital_krw,
            "order_cap_krw": self.research_scenario.order_cap_krw,
            "risk_per_trade_pct": self.research_scenario.risk_per_trade_pct,
            "regime_mode": self.research_scenario.regime_mode,
            "max_positions": self.research_scenario.max_positions,
        }
        strategy_configuration = self._strategy_configuration()
        scanner_configuration = self._scanner_configuration()
        frozen_configuration = {
            "settings": config,
            "research_scenario": scenario,
            "strategy": strategy_configuration,
            "scanner": scanner_configuration,
            "poll_seconds": self.poll_seconds,
        }
        config_hash = _sha(json.dumps(frozen_configuration, sort_keys=True))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        if self.manifest_path.exists():
            existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if existing.get("run_id") != self.run_id or existing.get("config_sha256") != config_hash:
                raise ValueError("run ID already exists with a different frozen configuration")
            previous_status = existing.get("status")
            if previous_status in {"SCHEDULED", "RUNNING"}:
                detected_at = self.clock().astimezone(KST)
                checkpoint_text = (
                    existing.get("last_market_event_at")
                    or existing.get("last_cycle_started_at")
                    or existing.get("started_at")
                    or existing.get("created_at")
                )
                try:
                    checkpoint = datetime.fromisoformat(str(checkpoint_text)).astimezone(KST)
                    gap_seconds = max(0.0, (detected_at - checkpoint).total_seconds())
                except (TypeError, ValueError):
                    checkpoint_text = None
                    gap_seconds = None
                process_start_count = int(existing.get("process_start_count", 1)) + 1
                existing["process_start_count"] = process_start_count
                existing["process_restart_count"] = int(existing.get("process_restart_count", 0)) + 1
                restart_history = existing.setdefault("restart_history", [])
                if not isinstance(restart_history, list):
                    raise ValueError("Shadow restart history is malformed; resume is fail-closed")
                restart_history.append({
                    "process_start_count": process_start_count,
                    "detected_at": detected_at.isoformat(),
                    "previous_status": previous_status,
                    "last_checkpoint_at": checkpoint_text,
                    "gap_since_last_checkpoint_seconds": gap_seconds,
                    "cause": "PROCESS_EXIT_CAUSE_NOT_AVAILABLE",
                })
                _atomic_json(self.manifest_path, existing)
            self._hydrate_dedup_sets()
            return existing
        git = self._git_evidence()
        manifest = {
            "schema_version": 1,
            "run_id": self.run_id,
            "created_at": self.clock().astimezone(KST).isoformat(),
            "scheduled_start": self.scheduled_start.isoformat(),
            "stop_at": self.stop_at.isoformat(),
            "status": "SCHEDULED",
            "process_start_count": 1,
            "process_restart_count": 0,
            "restart_history": [],
            "source": "KIS read-only REST market-rank, minute, and daily-index endpoints",
            "order_api_calls": 0,
            "account_data_collected": False,
            "settings": config,
            "research_scenario": scenario,
            "config_sha256": config_hash,
            "strategy_config": strategy_configuration,
            "scanner_config": scanner_configuration,
            "bar_timestamp_convention": "bar_start; only completed one-minute bars are evaluated",
            "execution": "SIMULATED next one-minute open; assumed costs; no broker order endpoint",
            "scanner_refresh_seconds": scanner_configuration["refresh_seconds"],
            "deep_monitor_top_n": self.monitor_top,
            "poll_seconds": self.poll_seconds,
            **git,
        }
        _atomic_json(self.manifest_path, manifest)
        return manifest

    def _hydrate_dedup_sets(self) -> None:
        for path, destination in (
            (self.events_path, self._events_seen),
            (self.bars_path, self._bars_seen),
            (self.warmup_bars_path, self._warmup_seen),
        ):
            if not path.is_file():
                continue
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError("Shadow audit file has a malformed line; resume is fail-closed") from exc
                    event_id = item.get("event_id")
                    if isinstance(event_id, str):
                        destination.add(event_id)

    def _load_state(self) -> dict[str, object]:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("run_id") != self.run_id:
                raise ValueError("state file run ID does not match")
            return state
        capital = self.settings.max_live_capital_krw
        profiles = {
            "CURRENT_LIVE_LIKE": {
                "cash": capital,
                "trade_count": 0,
                "closed_trades": 0,
                "closed_net_pnl_krw": 0.0,
                "positions": [],
            }
        }
        if self.research_scenario is not None:
            profiles[self.research_scenario.name] = {
                "cash": self.research_scenario.capital_krw,
                "trade_count": 0,
                "closed_trades": 0,
                "closed_net_pnl_krw": 0.0,
                "positions": [],
            }
        return {"run_id": self.run_id, "minute_cursor": {}, "bar_cursor": {}, "pending": [], "profiles": profiles}

    def _persist_state(self) -> None:
        _atomic_json(self.state_path, self._state)

    def _append(self, path: Path, seen: set[str], event_id: str, event: dict[str, object]) -> bool:
        if event_id in seen:
            return False
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"event_id": event_id, **event}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True, default=_json_default, allow_nan=False) + "\n")
            stream.flush()
        seen.add(event_id)
        return True

    def _event(self, kind: str, key: str, payload: dict[str, object], timestamp: datetime | None = None) -> None:
        event_id = _sha(f"{self.run_id}|{kind}|{key}")
        self._append(self.events_path, self._events_seen, event_id, {
            "run_id": self.run_id,
            "event_type": kind,
            "observed_at": (timestamp or self.clock()).astimezone(KST).isoformat(),
            **payload,
        })

    def _persist_minute_bar(self, symbol: str, bar: Bar) -> None:
        event_id = _sha(f"{self.run_id}|{symbol}|{bar.time.isoformat()}")
        self._append(self.bars_path, self._bars_seen, event_id, {
            "run_id": self.run_id,
            "symbol": symbol,
            "timestamp": bar.time.astimezone(KST).isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        })

    def _persist_warmup_bar(self, symbol: str, bar: Bar) -> None:
        event_id = _sha(f"{self.run_id}|warmup|{symbol}|{bar.time.isoformat()}")
        self._append(self.warmup_bars_path, self._warmup_seen, event_id, {
            "run_id": self.run_id,
            "symbol": symbol,
            "timestamp": bar.time.astimezone(KST).isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "data_type": "historical_warmup",
        })

    def _persist_index_snapshot(self) -> None:
        payload = {
            market: [
                {
                    "timestamp": bar.time.astimezone(KST).isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in bars
            ]
            for market, bars in sorted(self.index_bars.items())
        }
        _atomic_json(self.index_path, payload)
        self._manifest["index_snapshot_file"] = self.index_path.name
        self._manifest["index_snapshot_sha256"] = hashlib.sha256(self.index_path.read_bytes()).hexdigest()

    def _refresh_indexes(self, session: date) -> None:
        if self._index_refresh_done:
            return
        now = self.clock().astimezone(KST)
        if self._last_index_attempt is not None and now - self._last_index_attempt < timedelta(minutes=5):
            return
        self._last_index_attempt = now
        start = session - timedelta(days=120)
        try:
            self.index_bars["kospi"] = self.client.get_index_bars("0001", start, session - timedelta(days=1))
            self.index_bars["kosdaq"] = self.client.get_index_bars("1001", start, session - timedelta(days=1))
            self._persist_index_snapshot()
            self._manifest["index_data"] = {
                key: {
                    "rows": len(values),
                    "first": values[0].time.isoformat() if values else None,
                    "last": values[-1].time.isoformat() if values else None,
                    "sha256": _sha(json.dumps([bar.__dict__ if hasattr(bar, "__dict__") else [bar.time.isoformat(),bar.open,bar.high,bar.low,bar.close,bar.volume] for bar in values])),
                }
                for key, values in self.index_bars.items()
            }
            self._index_refresh_done = True
            self._write_manifest()
        except (KisApiError, OSError, ValueError) as exc:
            self._event("INDEX_REFRESH_FAILURE", f"{session}|{type(exc).__name__}", {
                "error_type": type(exc).__name__, "regime_status": "UNAVAILABLE",
            })
            self._manifest["index_refresh_failures"] = int(self._manifest.get("index_refresh_failures", 0)) + 1
            self._write_manifest()

    def _write_manifest(self) -> None:
        _atomic_json(self.manifest_path, self._manifest)

    def _confirmed_regime(self, timestamp: datetime) -> Regime | None:
        session_date = timestamp.astimezone(KST).date()
        results: list[Regime] = []
        for market in ("kospi", "kosdaq"):
            previous = [bar for bar in self.index_bars.get(market, []) if bar.time.astimezone(KST).date() < session_date]
            regime = classify_regime(
                previous,
                trend_lookback=self.settings.regime_trend_lookback,
                volatility_lookback=self.settings.regime_volatility_lookback,
                high_vol_threshold=self.settings.regime_high_vol_threshold,
            )
            if regime is None:
                return None
            results.append(regime)
        if Regime.HIGH_VOL in results:
            return Regime.HIGH_VOL
        if Regime.DOWN in results:
            return Regime.DOWN
        if results == [Regime.UP, Regime.UP]:
            return Regime.UP
        return Regime.NEUTRAL

    def _should_scan(self, now: datetime) -> bool:
        return self._last_scan is None or (now - self._last_scan).total_seconds() >= 1800

    def _scan(self, now: datetime) -> None:
        now = now.astimezone(KST)
        if now >= self.stop_at:
            return
        try:
            candidates, excluded = self.scanner(self.client, self.settings, top_n=20)
            observed_at = self.clock().astimezone(KST)
            if observed_at >= self.stop_at:
                self._monitor_symbols = []
                self._candidate_rank = {}
                self._last_scan = observed_at
                self._event("SCANNER_SKIPPED_AFTER_STOP", observed_at.isoformat(), {
                    "candidate_count": 0,
                    "reason": "SCANNER_RESPONSE_OBSERVED_AT_OR_AFTER_SHADOW_STOP",
                }, observed_at)
                self._manifest["scanner_cycles_skipped_after_stop"] = int(
                    self._manifest.get("scanner_cycles_skipped_after_stop", 0)
                ) + 1
                self._write_manifest()
                return
            self._monitor_symbols = [row.stock.symbol for row in candidates[: self.monitor_top]]
            self._candidate_rank = {row.stock.symbol: index for index, row in enumerate(candidates, start=1)}
            serialized = [
                {
                    "rank": index,
                    "symbol": row.stock.symbol,
                    "market": row.stock.market,
                    "price": row.activity.price,
                    "turnover_krw": row.activity.turnover_krw,
                    "volume": row.activity.volume,
                    "reason_codes": ["KIS_VOLUME_OR_TURNOVER_RANK", "PRICE_AND_TURNOVER_ELIGIBLE"],
                }
                for index, row in enumerate(candidates, start=1)
            ]
            self._event("SCANNER_CYCLE", now.isoformat(), {
                "candidate_count": len(candidates),
                "excluded_for_eligibility": excluded,
                "monitored_symbols": self._monitor_symbols,
                "candidates": serialized,
            }, observed_at)
            self._last_scan = observed_at
            self._manifest["scanner_cycles"] = int(self._manifest.get("scanner_cycles", 0)) + 1
            self._write_manifest()
        except (KisApiError, OSError, ValueError) as exc:
            observed_at = self.clock().astimezone(KST)
            # A failed refresh must not keep yesterday's/currently stale symbols
            # eligible for new decisions.
            self._monitor_symbols = []
            self._candidate_rank = {}
            self._last_scan = observed_at
            self._event("SCANNER_FAILURE", f"{observed_at.isoformat()}|{type(exc).__name__}", {
                "error_type": type(exc).__name__, "candidate_count": 0,
            }, observed_at)
            self._manifest["scanner_failures"] = int(self._manifest.get("scanner_failures", 0)) + 1
            self._write_manifest()

    def _ensure_warmup(self, symbol: str, session: date) -> None:
        if symbol in self._warmup_attempted:
            return
        self._warmup_attempted.add(symbol)
        try:
            end = session - timedelta(days=1)
            daily = self.client.get_daily_bars(symbol, end - timedelta(days=20), end)
            sessions = sorted({bar.time.astimezone(KST).date() for bar in daily})[-4:]
            collected: list[Bar] = []
            for historical_session in sessions:
                if self.cache.contains("minute", symbol, "1m", historical_session):
                    minute_bars = self.cache.load("minute", symbol, "1m", historical_session)
                else:
                    minute_bars = self.client.get_minute_bars(symbol, historical_session)
                    if not minute_bars:
                        continue
                    self.cache.save(
                        minute_bars,
                        kind="minute",
                        symbol=symbol,
                        interval="1m",
                        market="KRX",
                        source="KIS regular-session minute OHLCV warmup; timestamp=bar_start; auction excluded",
                        session_date=historical_session,
                    )
                if inspect_bars(minute_bars):
                    raise ValueError("WARMUP_DATA_QUALITY_FAILURE")
                collected.extend(minute_bars)
            self._warmup[symbol] = collected
            for bar in collected:
                self._persist_warmup_bar(symbol, bar)
            warmup_snapshot = [
                [bar.time.astimezone(KST).isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]
                for bar in collected
            ]
            self._event("WARMUP_READY", f"{symbol}|{session.isoformat()}", {
                "symbol": symbol,
                "historical_sessions": len(sessions),
                "historical_minute_bars": len(collected),
                "resampled_15m_bars": len(resample_session_minutes(collected, 15)),
                "resampled_30m_bars": len(resample_session_minutes(collected, 30)),
                "warmup_snapshot_sha256": _sha(json.dumps(warmup_snapshot, separators=(",", ":"), allow_nan=False)),
            })
        except (KisApiError, OSError, ValueError) as exc:
            self._warmup[symbol] = []
            self._manifest["warmup_failures"] = int(self._manifest.get("warmup_failures", 0)) + 1
            self._event("WARMUP_FAILURE", f"{symbol}|{session.isoformat()}|{type(exc).__name__}", {
                "symbol": symbol,
                "error_type": type(exc).__name__,
                "historical_minute_bars": 0,
            })
            self._write_manifest()

    def _profile_settings(self, profile: str) -> tuple[int, int, float, int, int]:
        if profile == "CURRENT_LIVE_LIKE":
            return (
                self.settings.max_live_capital_krw,
                self.settings.max_order_notional_krw,
                self.settings.risk_per_trade_pct,
                self.settings.max_concurrent_positions,
                self.settings.max_daily_trades,
            )
        if self.research_scenario and profile == self.research_scenario.name:
            return (
                self.research_scenario.capital_krw,
                self.research_scenario.order_cap_krw,
                self.research_scenario.risk_per_trade_pct,
                self.research_scenario.max_positions,
                self.settings.max_daily_trades,
            )
        raise ValueError("unknown Shadow sizing profile")

    def _decision(
        self,
        symbol: str,
        interval: int,
        strategy: str,
        bars: list[Bar],
        regime: Regime | None,
        observed_at: datetime,
    ) -> None:
        bar = bars[-1]
        key = f"{symbol}|{interval}|{strategy}|{bar.time.isoformat()}"
        if observed_at.astimezone(KST) >= self.stop_at:
            self._event("DECISION_SKIPPED_AFTER_STOP", key, {
                "symbol": symbol,
                "strategy": strategy,
                "interval_minutes": interval,
                "market_timestamp": bar.time.astimezone(KST).isoformat(),
                "scheduled_stop": self.stop_at.isoformat(),
                "reason": "OBSERVED_AT_OR_AFTER_SHADOW_STOP",
                "order_api_calls": 0,
            }, observed_at)
            return
        signal = (
            evaluate_breakout(bars, symbol, regime=regime)
            if strategy == "breakout"
            else evaluate_pullback(bars, symbol, regime=regime)
        )
        cursor_key = f"{symbol}|{interval}|{strategy}"
        previous = self._state["bar_cursor"].get(cursor_key)
        if previous is not None and bar.time.isoformat() <= previous:
            return
        self._state["bar_cursor"][cursor_key] = bar.time.isoformat()
        snapshot_payload = [
            [item.time.isoformat(), item.open, item.high, item.low, item.close, item.volume]
            for item in bars
        ]
        snapshot_id = _sha(json.dumps(
            {"symbol": symbol, "interval": interval, "bars": snapshot_payload},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ))
        regime_label = regime.value if regime is not None else None
        sizing: dict[str, dict[str, object]] = {}
        if signal.decision == Decision.ENTER and signal.stop_price is not None:
            costs = CostModel(self.settings.broker_fee_rate, self.settings.sell_tax_rate, self.settings.slippage_bps)
            sized_entry_price = costs.buy_fill_price(bar.close)
            profiles = self._state["profiles"]
            for name, portfolio in profiles.items():
                capital, order_cap, risk_pct, max_positions, _ = self._profile_settings(name)
                exposure = sum(float(p["quantity"]) * float(p["entry_price"]) for p in portfolio["positions"])
                initial = size_long_position(
                    entry_price=sized_entry_price,
                    stop_price=signal.stop_price,
                    bot_cash_krw=float(portfolio["cash"]),
                    current_exposure_krw=exposure,
                    capital_cap_krw=capital,
                    order_cap_krw=order_cap,
                    risk_per_trade_pct=risk_pct,
                    min_price_krw=self.settings.min_price_krw,
                    max_price_krw=min(self.settings.max_price_krw, order_cap),
                    fee_rate=self.settings.broker_fee_rate,
                    sell_tax_rate=self.settings.sell_tax_rate,
                    slippage_bps=self.settings.slippage_bps,
                )
                reason = initial.reason
                if len(portfolio["positions"]) >= max_positions:
                    reason = "MAX_POSITIONS"
                elif portfolio["trade_count"] >= self.settings.max_daily_trades:
                    reason = "DAILY_TRADE_LIMIT"
                sizing[name] = {
                    "quantity_at_signal_close": initial.quantity if reason == "SIZED" else 0,
                    "expected_risk_krw": initial.expected_risk_krw,
                    "order_cap_krw": order_cap,
                    "risk_budget_krw": initial.risk_budget_krw,
                    "reason": reason,
                }
        self._event("DECISION", key, {
            "market_timestamp": bar.time.astimezone(KST).isoformat(),
            "data_timestamp": bar.time.astimezone(KST).isoformat(),
            "strategy": strategy,
            "interval_minutes": interval,
            "symbol": symbol,
            "scanner_rank": self._candidate_rank.get(symbol),
            "scanner_member": symbol in self._candidate_rank,
            "regime": regime_label,
            "decision": signal.decision.value,
            "reason_codes": list(signal.reason_codes),
            "reference_price": signal.reference_price,
            "stop_price": signal.stop_price,
            "signal_bar_close": bar.close,
            "signal_bar_high": bar.high,
            "completed_bar_snapshot_id": snapshot_id,
            "sizing_profiles": sizing,
        }, observed_at)
        if strategy == STRATEGIES[0]:
            self._manifest[f"completed_{interval}m_bars"] = int(self._manifest.get(f"completed_{interval}m_bars", 0)) + 1
        self._manifest["strategy_decisions"] = int(self._manifest.get("strategy_decisions", 0)) + 1
        if signal.decision == Decision.ENTER and signal.stop_price is not None:
            signal_id = _sha(f"{self.run_id}|{key}")
            pending_ids = {str(item["signal_id"]) for item in self._state["pending"]}
            pending = {
                "signal_id": _sha(f"{self.run_id}|{key}"),
                "symbol": symbol,
                "strategy": strategy,
                "interval": interval,
                "signal_time": bar.time.isoformat(),
                "decision_observed_at": observed_at.isoformat(),
                "earliest_fill_time": (observed_at.replace(second=0, microsecond=0) + timedelta(minutes=1)).isoformat(),
                "signal_high": bar.high,
                "signal_close": bar.close,
                "stop_price": signal.stop_price,
                "max_holding_bars": signal.max_holding_bars or 10,
                "scanner_rank": self._candidate_rank.get(symbol),
                "regime": regime_label,
                "snapshot_id": snapshot_id,
            }
            if signal_id not in pending_ids:
                self._state["pending"].append(pending)
            self._event("SIMULATED_ORDER_INTENT", signal_id, {
                "status": "SIMULATED_PENDING_NEXT_EXECUTABLE_BAR",
                "side": "BUY",
                "symbol": symbol,
                "strategy": strategy,
                "interval_minutes": interval,
                "scanner_rank": self._candidate_rank.get(symbol),
                "regime": regime_label,
                "signal_timestamp": bar.time.astimezone(KST).isoformat(),
                "decision_observed_at": observed_at.astimezone(KST).isoformat(),
                "reference_price": signal.reference_price,
                "signal_bar_high": bar.high,
                "stop_price": signal.stop_price,
                "sizing_profiles": sizing,
                "completed_bar_snapshot_id": snapshot_id,
                "order_api_calls": 0,
            }, observed_at)

    def _close_position(self, profile: str, position: dict[str, object], reference_price: float, at: datetime, reason: str) -> None:
        costs = CostModel(self.settings.broker_fee_rate, self.settings.sell_tax_rate, self.settings.slippage_bps)
        quantity = int(position["quantity"])
        exit_price = costs.sell_fill_price(reference_price)
        exit_notional = exit_price * quantity
        sell_cost = costs.sell_cost(exit_notional)
        gross = (exit_price - float(position["entry_price"])) * quantity
        net = gross - float(position["buy_fee_krw"]) - sell_cost
        portfolio = self._state["profiles"][profile]
        portfolio["cash"] = float(portfolio["cash"]) + exit_notional - sell_cost
        portfolio["closed_trades"] = int(portfolio.get("closed_trades", 0)) + 1
        portfolio["closed_net_pnl_krw"] = float(portfolio.get("closed_net_pnl_krw", 0.0)) + net
        portfolio["positions"].remove(position)
        key = f"{position['position_id']}|{at.isoformat()}|{reason}"
        self._event("SIMULATED_EXIT_FILL", key, {
            "status": "SIMULATED",
            "profile": profile,
            "symbol": position["symbol"],
            "strategy": position["strategy"],
            "interval_minutes": position["interval"],
            "quantity": quantity,
            "reference_price": reference_price,
            "fill_price": exit_price,
            "exit_reason": reason,
            "gross_pnl_krw": gross,
            "buy_fee_krw": position["buy_fee_krw"],
            "sell_fee_tax_krw": sell_cost,
            "net_pnl_krw": net,
            "order_api_calls": 0,
        }, at)

    def _manage_minute(self, symbol: str, bar: Bar) -> None:
        for profile, portfolio in self._state["profiles"].items():
            for position in list(portfolio["positions"]):
                if position["symbol"] != symbol:
                    continue
                position["last_mark_price"] = bar.close
                position["last_mark_at"] = bar.time.isoformat()
                if bar.time < datetime.fromisoformat(position["entry_time"]):
                    continue
                exit_pending_at = position.get("exit_pending_at")
                if exit_pending_at is not None and bar.time >= datetime.fromisoformat(str(exit_pending_at)):
                    self._close_position(profile, position, bar.open, bar.time, "MAX_HOLD_TIME_NEXT_OPEN")
                    continue
                position["mfe_krw"] = max(float(position.get("mfe_krw", 0.0)), (bar.high - float(position["entry_price"])) * int(position["quantity"]))
                position["mae_krw"] = min(float(position.get("mae_krw", 0.0)), (bar.low - float(position["entry_price"])) * int(position["quantity"]))
                stop = float(position["stop_price"])
                if bar.low <= stop:
                    reference = min(stop, bar.open)
                    self._close_position(profile, position, reference, bar.time, "STOP_TOUCH")

    def _manage_strategy_bar(
        self, symbol: str, interval: int, strategy: str, bar: Bar, observed_at: datetime
    ) -> None:
        if observed_at.astimezone(KST) >= self.stop_at:
            return
        for profile, portfolio in self._state["profiles"].items():
            for position in list(portfolio["positions"]):
                if position["symbol"] != symbol or position["interval"] != interval or position["strategy"] != strategy:
                    continue
                if position.get("exit_pending_at") is not None:
                    continue
                if bar.time <= datetime.fromisoformat(position["signal_time"]):
                    continue
                position["held_bars"] = int(position.get("held_bars", 0)) + 1
                if position["held_bars"] >= int(position["max_holding_bars"]):
                    earliest_exit = observed_at.astimezone(KST).replace(second=0, microsecond=0) + timedelta(minutes=1)
                    position["exit_pending_at"] = earliest_exit.isoformat()
                    self._event("SIMULATED_EXIT_INTENT", str(position["position_id"]), {
                        "status": "SIMULATED_PENDING_NEXT_EXECUTABLE_BAR",
                        "profile": profile,
                        "symbol": symbol,
                        "strategy": strategy,
                        "interval_minutes": interval,
                        "reason": "MAX_HOLD_TIME",
                        "signal_timestamp": position["signal_time"],
                        "completed_strategy_bar": bar.time.isoformat(),
                        "earliest_exit_time": earliest_exit.isoformat(),
                        "order_api_calls": 0,
                    }, observed_at)

    def _resolve_pending(
        self,
        bars_by_symbol: dict[str, list[Bar]],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        observed_at = (observed_at or self.clock()).astimezone(KST)
        still_pending: list[dict[str, object]] = []
        pending_seen: set[str] = set()
        for intent in self._state["pending"]:
            signal_id = str(intent["signal_id"])
            if signal_id in pending_seen:
                continue
            pending_seen.add(signal_id)
            symbol = str(intent["symbol"])
            signal_time = datetime.fromisoformat(str(intent["signal_time"]))
            earliest = datetime.fromisoformat(str(intent["earliest_fill_time"]))
            executable = next((
                bar for bar in bars_by_symbol.get(symbol, [])
                if max(signal_time, earliest) <= bar.time < self.stop_at
            ), None)
            if executable is None:
                still_pending.append(intent)
                continue
            bar_completed_at = executable.time + timedelta(minutes=1)
            if observed_at - bar_completed_at > timedelta(minutes=2):
                self._event("SIMULATED_ORDER_EXPIRED", signal_id, {
                    "status": "SIMULATED_EXPIRED",
                    "reason": "NEXT_BAR_DATA_OBSERVED_TOO_LATE",
                    "symbol": symbol,
                    "strategy": intent["strategy"],
                    "interval_minutes": intent["interval"],
                    "signal_timestamp": signal_time.isoformat(),
                    "decision_observed_at": intent["decision_observed_at"],
                    "next_bar_timestamp": executable.time.isoformat(),
                    "bar_completed_at": bar_completed_at.isoformat(),
                    "data_age_seconds": (observed_at - bar_completed_at).total_seconds(),
                    "order_api_calls": 0,
                }, observed_at)
                continue
            costs = CostModel(self.settings.broker_fee_rate, self.settings.sell_tax_rate, self.settings.slippage_bps)
            sized_entry_price = costs.buy_fill_price(executable.open)
            if sized_entry_price > executable.high:
                self._event("SIMULATED_ORDER_NOT_FILLED", str(intent["signal_id"]), {
                    "status": "SIMULATED_NOT_FILLED",
                    "reason": "ENTRY_SLIPPAGE_ABOVE_NEXT_BAR_HIGH",
                    "symbol": symbol,
                    "strategy": intent["strategy"],
                    "interval_minutes": intent["interval"],
                    "signal_timestamp": signal_time.isoformat(),
                    "next_bar_timestamp": executable.time.isoformat(),
                    "next_bar_high": executable.high,
                    "expected_entry_fill": sized_entry_price,
                    "order_api_calls": 0,
                }, executable.time)
                continue
            profiles = self._state["profiles"]
            for profile, portfolio in profiles.items():
                capital, order_cap, risk_pct, max_positions, daily_limit = self._profile_settings(profile)
                exposure = sum(float(p["quantity"]) * float(p["entry_price"]) for p in portfolio["positions"])
                sizing = size_long_position(
                    entry_price=sized_entry_price,
                    stop_price=float(intent["stop_price"]),
                    bot_cash_krw=float(portfolio["cash"]),
                    current_exposure_krw=exposure,
                    capital_cap_krw=capital,
                    order_cap_krw=order_cap,
                    risk_per_trade_pct=risk_pct,
                    min_price_krw=self.settings.min_price_krw,
                    max_price_krw=min(self.settings.max_price_krw, order_cap),
                    fee_rate=self.settings.broker_fee_rate,
                    sell_tax_rate=self.settings.sell_tax_rate,
                    slippage_bps=self.settings.slippage_bps,
                )
                reason = sizing.reason
                if len(portfolio["positions"]) >= max_positions:
                    reason = "MAX_POSITIONS"
                elif int(portfolio["trade_count"]) >= daily_limit:
                    reason = "DAILY_TRADE_LIMIT"
                if reason != "SIZED" or sizing.quantity < 1:
                    self._event("SIMULATED_ORDER_HELD", f"{intent['signal_id']}|{profile}", {
                        "status": "SIMULATED_HELD",
                        "profile": profile,
                        "symbol": symbol,
                        "strategy": intent["strategy"],
                        "interval_minutes": intent["interval"],
                        "reason": reason,
                        "quantity": 0,
                        "next_bar_timestamp": executable.time.isoformat(),
                        "order_api_calls": 0,
                    }, executable.time)
                    continue
                quantity = sizing.quantity
                entry_price = costs.buy_fill_price(executable.open)
                notional = entry_price * quantity
                buy_fee = costs.buy_cost(notional)
                portfolio["cash"] = float(portfolio["cash"]) - notional - buy_fee
                portfolio["trade_count"] = int(portfolio["trade_count"]) + 1
                position_id = _sha(f"{intent['signal_id']}|{profile}|fill")
                position = {
                    "position_id": position_id,
                    "profile": profile,
                    "symbol": symbol,
                    "strategy": intent["strategy"],
                    "interval": int(intent["interval"]),
                    "quantity": quantity,
                    "entry_time": executable.time.isoformat(),
                    "signal_time": signal_time.isoformat(),
                    "entry_reference_price": executable.open,
                    "entry_price": entry_price,
                    "buy_fee_krw": buy_fee,
                    "stop_price": float(intent["stop_price"]),
                    "max_holding_bars": int(intent["max_holding_bars"]),
                    "held_bars": 0,
                    "mfe_krw": 0.0,
                    "mae_krw": 0.0,
                }
                portfolio["positions"].append(position)
                self._event("SIMULATED_ENTRY_FILL", f"{intent['signal_id']}|{profile}", {
                    "status": "SIMULATED",
                    "profile": profile,
                    "side": "BUY",
                    "symbol": symbol,
                    "strategy": intent["strategy"],
                    "interval_minutes": intent["interval"],
                    "scanner_rank": intent["scanner_rank"],
                    "regime": intent["regime"],
                    "signal_timestamp": signal_time.isoformat(),
                    "next_bar_timestamp": executable.time.isoformat(),
                    "reference_price": executable.open,
                    "fill_price": entry_price,
                    "stop_price": intent["stop_price"],
                    "quantity": quantity,
                    "risk_budget_krw": sizing.risk_budget_krw,
                    "expected_risk_krw": sizing.expected_risk_krw,
                    "market_snapshot_id": intent["snapshot_id"],
                    "order_api_calls": 0,
                }, executable.time)
                self._manage_minute(symbol, executable)
        self._state["pending"] = still_pending

    def _index_latest_hash(self) -> None:
        self._manifest["minute_bars_count"] = len(self._bars_seen)
        if self.bars_path.exists():
            self._manifest["minute_bars_sha256"] = hashlib.sha256(self.bars_path.read_bytes()).hexdigest()
        if self.warmup_bars_path.exists():
            self._manifest["warmup_bars_count"] = len(self._warmup_seen)
            self._manifest["warmup_bars_sha256"] = hashlib.sha256(self.warmup_bars_path.read_bytes()).hexdigest()
        if self.index_path.exists():
            self._manifest["index_snapshot_sha256"] = hashlib.sha256(self.index_path.read_bytes()).hexdigest()
        if self.events_path.exists():
            self._manifest["events_sha256"] = hashlib.sha256(self.events_path.read_bytes()).hexdigest()

    def cycle(self, now: datetime | None = None) -> None:
        now = (now or self.clock()).astimezone(KST)
        session = now.date()
        self._refresh_indexes(session)
        anchor: list[Bar] = []
        try:
            anchor_observed_at = self.clock().astimezone(KST)
            anchor_cutoff = min(anchor_observed_at, self.stop_at)
            anchor = [
                bar for bar in self.client.get_minute_bars("005930", session)
                if bar.time + timedelta(minutes=1) <= anchor_cutoff
            ]
        except (KisApiError, OSError, ValueError) as exc:
            self._event("MARKET_STATUS_CHECK", f"{now.isoformat()}|{type(exc).__name__}", {
                "status": "NOT_CONFIRMED",
                "error_type": type(exc).__name__,
            }, now)
            self._manifest["market_status"] = "NOT_CONFIRMED"
            self._write_manifest()
            if now >= now.replace(hour=9, minute=15, second=0, microsecond=0):
                self._stop_requested = True
            return
        if not anchor:
            self._event("MARKET_STATUS_CHECK", now.isoformat(), {"status": "NOT_CONFIRMED", "completed_anchor_bars": 0}, now)
            self._manifest["market_status"] = "NOT_CONFIRMED"
            self._write_manifest()
            if now >= now.replace(hour=9, minute=15, second=0, microsecond=0):
                self._stop_requested = True
            return
        if not self._market_confirmed:
            self._market_confirmed = True
            self._manifest["market_status"] = "OPEN_CONFIRMED_BY_KIS_MINUTE_DATA"
            self._manifest["first_market_bar"] = anchor[0].time.isoformat()
            self._event("MARKET_STATUS_CHECK", now.isoformat(), {
                "status": "OPEN_CONFIRMED_BY_KIS_MINUTE_DATA",
                "symbol": "005930",
                "first_bar": anchor[0].time.isoformat(),
                "latest_bar": anchor[-1].time.isoformat(),
            }, now)
            self._write_manifest()
        first_scan_after_open = now >= now.replace(hour=9, minute=5, second=0, microsecond=0)
        if first_scan_after_open and self._should_scan(now):
            self._scan(now)
        for symbol in self._monitor_symbols:
            self._ensure_warmup(symbol, session)
        bars_by_symbol: dict[str, list[Bar]] = {}
        fetched_at = self.clock().astimezone(KST)
        data_cutoff = min(fetched_at, self.stop_at)
        for symbol in dict.fromkeys(["005930", *self._monitor_symbols]):
            try:
                current = anchor if symbol == "005930" else self.client.get_minute_bars(symbol, session)
                current = [bar for bar in current if bar.time + timedelta(minutes=1) <= data_cutoff]
                if inspect_bars(current):
                    raise ValueError("CURRENT_SESSION_DATA_QUALITY_FAILURE")
                cursor = self._state["minute_cursor"].get(symbol)
                fresh = [bar for bar in current if cursor is None or bar.time.isoformat() > cursor]
                for bar in fresh:
                    self._persist_minute_bar(symbol, bar)
                    self._manage_minute(symbol, bar)
                if fresh:
                    self._state["minute_cursor"][symbol] = fresh[-1].time.isoformat()
                bars_by_symbol[symbol] = [*self._warmup.get(symbol, []), *current]
                latest = current[-1].time if current else None
                stale = latest is None or fetched_at - (latest + timedelta(minutes=1)) > timedelta(minutes=3)
                elapsed = (
                    max(0, int((latest - latest.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() // 60) + 1)
                    if latest
                    else 0
                )
                expected_times = {
                    latest.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(minutes=index)
                    for index in range(elapsed)
                } if latest else set()
                observed_times = {bar.time.astimezone(KST) for bar in current}
                missing_times = sorted(expected_times - observed_times)
                for missing_time in missing_times:
                    gap_key = f"{symbol}|{missing_time.isoformat()}"
                    gap_event_id = _sha(f"{self.run_id}|DATA_GAP|{gap_key}")
                    if gap_event_id not in self._events_seen:
                        self._event("DATA_GAP", gap_key, {
                            "symbol": symbol,
                            "missing_minute": missing_time.isoformat(),
                            "quality_status": "INCOMPLETE_SESSION_DATA",
                        }, fetched_at)
                        self._manifest["missing_minute_count"] = int(self._manifest.get("missing_minute_count", 0)) + 1
                self._event("DATA_HEALTH", f"{symbol}|{fetched_at.isoformat()}", {
                    "symbol": symbol,
                    "rows_returned": len(current),
                    "new_rows": len(fresh),
                    "latest_completed_bar": latest.isoformat() if latest else None,
                    "stale": stale,
                    "expected_elapsed_regular_minutes": elapsed,
                    "missing_regular_minutes": len(missing_times),
                    "duplicates_or_quality_errors": 0,
                    "api_errors": 0,
                }, fetched_at)
                if stale:
                    self._manifest["stale_incidents"] = int(self._manifest.get("stale_incidents", 0)) + 1
                self._manifest["completed_minute_bars"] = int(self._manifest.get("completed_minute_bars", 0)) + len(fresh)
            except (KisApiError, OSError, ValueError) as exc:
                self._event("DATA_FAILURE", f"{symbol}|{fetched_at.isoformat()}|{type(exc).__name__}", {
                    "symbol": symbol,
                    "error_type": type(exc).__name__,
                    "api_errors": 1,
                }, fetched_at)
                self._manifest["api_errors"] = int(self._manifest.get("api_errors", 0)) + 1
        for symbol in self._monitor_symbols:
            minute_bars = bars_by_symbol.get(symbol)
            if minute_bars is None:
                continue
            for interval in INTERVALS:
                completed = resample_session_minutes(minute_bars, interval)
                today_completed = [bar for bar in completed if bar.time.astimezone(KST).date() == session]
                for strategy in STRATEGIES:
                    cursor_key = f"{symbol}|{interval}|{strategy}"
                    previous = self._state["bar_cursor"].get(cursor_key)
                    new_completed = [bar for bar in today_completed if previous is None or bar.time.isoformat() > previous]
                    for end_bar in new_completed:
                        strategy_bars = completed[: completed.index(end_bar) + 1]
                        self._manage_strategy_bar(symbol, interval, strategy, end_bar, fetched_at)
                        regime = self._confirmed_regime(end_bar.time)
                        self._decision(symbol, interval, strategy, strategy_bars, regime, fetched_at)
        self._resolve_pending(bars_by_symbol, observed_at=fetched_at)
        self._manifest["poll_cycles"] = int(self._manifest.get("poll_cycles", 0)) + 1
        self._manifest["last_market_event_at"] = fetched_at.isoformat()
        self._index_latest_hash()
        self._write_manifest()
        self._persist_state()
        self._sync_evidence()

    def _sync_evidence(self) -> None:
        for path in (
            self.events_path,
            self.bars_path,
            self.warmup_bars_path,
            self.index_path,
            self.state_path,
            self.manifest_path,
        ):
            if path.is_file():
                fd = os.open(path, os.O_RDWR)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

    def run(self) -> dict[str, object]:
        lock_path = self.directory / ".lock"
        self._lock_stream = lock_path.open("a+")
        try:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_stream.close()
            raise RuntimeError("a process already owns this Shadow run ID") from None
        try:
            while self.clock().astimezone(KST) < self.scheduled_start:
                self.sleeper(min(5.0, max(0.0, (self.scheduled_start - self.clock().astimezone(KST)).total_seconds())))
            self._manifest["status"] = "RUNNING"
            started_at = self.clock().astimezone(KST).isoformat()
            if not isinstance(self._manifest.get("started_at"), str):
                self._manifest["started_at"] = started_at
            self._manifest["last_process_started_at"] = started_at
            self._write_manifest()
            process_start_count = int(self._manifest.get("process_start_count", 1))
            self._event("RUN_STARTED", f"{self.run_id}|process-{process_start_count}", {
                "status": "RUNNING",
                "process_start_count": process_start_count,
                "process_restart_count": int(self._manifest.get("process_restart_count", 0)),
                "recovery": (self._manifest.get("restart_history") or [None])[-1] if process_start_count > 1 else None,
                "order_api_calls": 0,
            })
            while self.clock().astimezone(KST) < self.stop_at:
                cycle_started = self.clock().astimezone(KST)
                self.cycle(cycle_started)
                if self._stop_requested:
                    self._finish("MARKET_NOT_CONFIRMED")
                    return self._manifest
                remaining = (self.stop_at - self.clock().astimezone(KST)).total_seconds()
                if remaining > 0:
                    self.sleeper(min(self.poll_seconds, remaining))
            self._finish("STOP_TIME_REACHED")
            return self._manifest
        except KeyboardInterrupt:
            self._finish("INTERRUPTED_GRACEFULLY")
            return self._manifest
        finally:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            self._lock_stream.close()

    def _finish(self, reason: str) -> None:
        now = self.clock().astimezone(KST)
        for intent in self._state["pending"]:
            self._event("SIMULATED_ORDER_EXPIRED", str(intent["signal_id"]), {
                "status": "SIMULATED_EXPIRED",
                "reason": "SHADOW_WINDOW_STOP",
                "symbol": intent["symbol"],
                "strategy": intent["strategy"],
                "interval_minutes": intent["interval"],
                "signal_timestamp": intent["signal_time"],
                "order_api_calls": 0,
            }, now)
        self._state["pending"] = []
        open_positions = [
            {"profile": profile, **{k: v for k, v in position.items() if k not in {"buy_fee_krw"}}}
            for profile, portfolio in self._state["profiles"].items()
            for position in portfolio["positions"]
        ]
        for position in open_positions:
            self._event("OBSERVATIONAL_MARK", str(position["position_id"]), {
                "policy": "MARK_OPEN_AT_13",
                "profile": position["profile"],
                "symbol": position["symbol"],
                "strategy": position["strategy"],
                "quantity": position["quantity"],
                "entry_price": position["entry_price"],
                "mark_price": position.get("last_mark_price"),
                "mark_timestamp": position.get("last_mark_at"),
                "position_status": "OPEN_NOT_FORCE_CLOSED",
            }, now)
        failure_count = sum(int(self._manifest.get(key, 0)) for key in (
            "api_errors", "scanner_failures", "index_refresh_failures", "warmup_failures",
            "stale_incidents", "missing_minute_count",
        ))
        self._manifest["status"] = (
            "PARTIAL"
            if reason == "INTERRUPTED_GRACEFULLY"
            else "PASS"
            if self._market_confirmed and failure_count == 0
            else "PARTIAL"
            if self._market_confirmed
            else "MARKET_NOT_CONFIRMED"
        )
        self._manifest["stop_reason"] = reason
        self._manifest["stopped_at"] = now.isoformat()
        self._manifest["open_positions_at_stop"] = open_positions
        self._manifest["profile_summaries"] = {
            name: {
                "cash_krw": round(float(profile["cash"]), 2),
                "closed_trades": int(profile.get("closed_trades", int(profile["trade_count"]) - len(profile["positions"]))),
                "closed_net_pnl_krw": round(float(profile.get("closed_net_pnl_krw", 0.0)), 2),
                "open_positions": len(profile["positions"]),
                "open_marked_pnl_krw": round(sum(
                    (float(position.get("last_mark_price", position["entry_price"])) - float(position["entry_price"]))
                    * int(position["quantity"])
                    - float(position["buy_fee_krw"])
                    for position in profile["positions"]
                ), 2),
            }
            for name, profile in self._state["profiles"].items()
        }
        self._index_latest_hash()
        self._persist_state()
        self._write_manifest()
        self._event("RUN_STOPPED", reason, {
            "status": self._manifest["status"],
            "open_positions": len(open_positions),
            "order_api_calls": 0,
        }, now)
        self._index_latest_hash()
        self._write_manifest()
        self._sync_evidence()


def scheduled_market_window(today: date, start: str = "09:00", stop: str = "13:00") -> tuple[datetime, datetime]:
    try:
        start_time = datetime_time.fromisoformat(start)
        stop_time = datetime_time.fromisoformat(stop)
    except ValueError as exc:
        raise ValueError("session times must be HH:MM") from exc
    if start_time.tzinfo is not None or stop_time.tzinfo is not None or start_time >= stop_time:
        raise ValueError("session start/stop must be ordered KST wall-clock times")
    return (
        datetime.combine(today, start_time, KST),
        datetime.combine(today, stop_time, KST),
    )

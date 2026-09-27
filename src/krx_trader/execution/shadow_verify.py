from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from krx_trader.data.resample import resample_session_minutes
from krx_trader.market.regime import Regime, classify_regime
from krx_trader.models import Bar, Decision, Signal
from krx_trader.strategies.breakout import BreakoutConfig, evaluate_breakout
from krx_trader.strategies.pullback import PullbackConfig, evaluate_pullback

KST = ZoneInfo("Asia/Seoul")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"malformed JSONL at {path.name}:{line_number}") from None
            if not isinstance(row, dict):
                raise TypeError(f"invalid JSONL row at {path.name}:{line_number}")
            rows.append(row)
    return rows


def _bar(row: dict[str, Any]) -> Bar:
    return Bar(
        datetime.fromisoformat(str(row["timestamp"])),
        float(row["open"]),
        float(row["high"]),
        float(row["low"]),
        float(row["close"]),
        int(row["volume"]),
    )


def _index_bar(row: dict[str, Any]) -> Bar:
    return _bar(row)


def _combined_regime(indexes: dict[str, list[Bar]], signal_date: date, config: dict[str, Any]) -> Regime | None:
    lookbacks = config.get("regime_lookbacks", {})
    regimes: list[Regime] = []
    for market in ("kospi", "kosdaq"):
        previous = [bar for bar in indexes.get(market, []) if bar.time.astimezone(KST).date() < signal_date]
        regime = classify_regime(
            previous,
            trend_lookback=int(lookbacks.get("trend", 20)),
            volatility_lookback=int(lookbacks.get("volatility", 20)),
            high_vol_threshold=float(config.get("regime_threshold", 0.025)),
        )
        if regime is None:
            return None
        regimes.append(regime)
    if Regime.HIGH_VOL in regimes:
        return Regime.HIGH_VOL
    if Regime.DOWN in regimes:
        return Regime.DOWN
    if regimes == [Regime.UP, Regime.UP]:
        return Regime.UP
    return Regime.NEUTRAL


def _scanner_rank_at(events: list[dict[str, Any]], symbol: str, decision_at: datetime) -> int | None:
    scans = [
        event for event in events
        if event.get("event_type") in {"SCANNER_CYCLE", "SCANNER_FAILURE"}
        and datetime.fromisoformat(str(event["observed_at"])).astimezone(KST) <= decision_at
    ]
    if not scans:
        return None
    latest = max(scans, key=lambda event: datetime.fromisoformat(str(event["observed_at"])).astimezone(KST))
    if latest.get("event_type") == "SCANNER_FAILURE":
        return None
    return next((int(row["rank"]) for row in latest.get("candidates", []) if row.get("symbol") == symbol), None)


def _same_number(actual: Any, expected: float | None) -> bool:
    if expected is None:
        return actual is None
    try:
        return abs(float(actual) - expected) <= 1e-8
    except (TypeError, ValueError):
        return False


def verify_shadow_run(run_dir: Path) -> dict[str, Any]:
    """Replay captured Shadow decisions using only their persisted market inputs."""
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("Shadow run manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    events_path = run_dir / "events.jsonl"
    bars_path = run_dir / "bars.jsonl"
    warmup_path = run_dir / "warmup_bars.jsonl"
    indexes_path = run_dir / "indexes.json"
    events = _read_jsonl(events_path)
    bars = _read_jsonl(bars_path)
    warmup = _read_jsonl(warmup_path)
    capture_checks = {
        "minute_bar_count": len(bars) == manifest.get("minute_bars_count", len(bars)),
        "warmup_bar_count": len(warmup) == manifest.get("warmup_bars_count", len(warmup)),
        "run_id_consistency": all(
            row.get("run_id") == manifest.get("run_id")
            for row in [*events, *bars, *warmup]
        ),
        "no_order_api_calls": manifest.get("order_api_calls") == 0,
        "no_account_data": manifest.get("account_data_collected") is False,
    }
    file_checks: dict[str, dict[str, Any]] = {}
    for path, key in (
        (events_path, "events_sha256"),
        (bars_path, "minute_bars_sha256"),
        (warmup_path, "warmup_bars_sha256"),
        (indexes_path, "index_snapshot_sha256"),
    ):
        if key not in manifest:
            optional_missing = path == warmup_path and not path.is_file()
            file_checks[path.name] = {"status": "NOT_PRESENT_OPTIONAL" if optional_missing else "MISSING_EXPECTED_HASH"}
        elif not path.is_file():
            file_checks[path.name] = {"status": "MISSING_FILE"}
        else:
            actual_hash = _sha(path.read_bytes())
            expected_hash = manifest[key]
            file_checks[path.name] = {
                "status": "PASS" if actual_hash == expected_hash else "HASH_MISMATCH",
                "sha256": actual_hash,
            }
    indexes: dict[str, list[Bar]] = {}
    if indexes_path.is_file():
        raw_indexes = json.loads(indexes_path.read_text(encoding="utf-8"))
        indexes = {market: [_index_bar(row) for row in rows] for market, rows in raw_indexes.items()}
    all_minutes: dict[str, dict[datetime, Bar]] = {}
    for row in [*warmup, *bars]:
        symbol = str(row["symbol"])
        bar = _bar(row)
        destination = all_minutes.setdefault(symbol, {})
        previous = destination.get(bar.time)
        if previous is not None and previous != bar:
            raise ValueError(f"conflicting captured bars for {symbol} at {bar.time.isoformat()}")
        destination[bar.time] = bar

    strategy_config = manifest.get("strategy_config", {})
    failures: list[dict[str, Any]] = []
    decisions = [event for event in events if event.get("event_type") == "DECISION"]
    for event in decisions:
        symbol = str(event.get("symbol", ""))
        strategy = str(event.get("strategy", ""))
        interval = int(event.get("interval_minutes", 0))
        signal_time = datetime.fromisoformat(str(event.get("data_timestamp", event.get("market_timestamp"))))
        observed_at = datetime.fromisoformat(str(event["observed_at"])).astimezone(KST)
        reasons: list[str] = []
        if event.get("market_timestamp") != event.get("data_timestamp"):
            reasons.append("MARKET_DATA_TIMESTAMP_MISMATCH")
        minute_map = all_minutes.get(symbol, {})
        minutes = sorted((bar for bar in minute_map.values()), key=lambda bar: bar.time)
        completed = resample_session_minutes(minutes, interval)
        strategy_bars = [bar for bar in completed if bar.time <= signal_time]
        if not strategy_bars or strategy_bars[-1].time != signal_time:
            reasons.append("COMPLETED_BAR_INPUT_MISSING")
            if reasons:
                failures.append({"symbol": symbol, "strategy": strategy, "interval": interval, "timestamp": signal_time.isoformat(), "reasons": reasons})
            continue
        snapshot_payload = [
            [bar.time.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]
            for bar in strategy_bars
        ]
        snapshot_id = hashlib.sha256(json.dumps(
            {"symbol": symbol, "interval": interval, "bars": snapshot_payload},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()).hexdigest()
        if snapshot_id != event.get("completed_bar_snapshot_id"):
            reasons.append("COMPLETED_BAR_SNAPSHOT_MISMATCH")
        scanner_rank = _scanner_rank_at(events, symbol, observed_at)
        scanner_member = scanner_rank is not None
        if scanner_member != bool(event.get("scanner_member")) or scanner_rank != event.get("scanner_rank"):
            reasons.append("SCANNER_MEMBERSHIP_MISMATCH")
        regime = _combined_regime(indexes, signal_time.astimezone(KST).date(), strategy_config)
        if (regime.value if regime is not None else None) != event.get("regime"):
            reasons.append("REGIME_MISMATCH")
        if not scanner_member:
            expected = Signal(signal_time, symbol, strategy, Decision.HOLD, ("SCANNER_EXCLUDED",))
        elif strategy == "breakout":
            expected = evaluate_breakout(
                strategy_bars, symbol, regime=regime,
                config=BreakoutConfig(**strategy_config.get("breakout", {})),
            )
        elif strategy == "pullback":
            expected = evaluate_pullback(
                strategy_bars, symbol, regime=regime,
                config=PullbackConfig(**strategy_config.get("pullback", {})),
            )
        else:
            reasons.append("UNKNOWN_STRATEGY")
            expected = Signal(signal_time, symbol, strategy, Decision.HOLD, ("UNKNOWN_STRATEGY",))
        if expected.decision.value != event.get("decision"):
            reasons.append("DECISION_MISMATCH")
        if list(expected.reason_codes) != event.get("reason_codes"):
            reasons.append("REASON_MISMATCH")
        if not _same_number(event.get("reference_price"), expected.reference_price):
            reasons.append("REFERENCE_PRICE_MISMATCH")
        if not _same_number(event.get("stop_price"), expected.stop_price):
            reasons.append("STOP_PRICE_MISMATCH")
        if reasons:
            failures.append({
                "symbol": symbol,
                "strategy": strategy,
                "interval": interval,
                "timestamp": signal_time.isoformat(),
                "reasons": reasons,
            })

    hash_ok = all(item.get("status") in {"PASS", "NOT_PRESENT_OPTIONAL"} for item in file_checks.values())
    capture_ok = all(capture_checks.values())
    status = "PASS" if hash_ok and capture_ok and not failures and decisions else "NO_DECISIONS" if hash_ok and capture_ok and not decisions else "FAIL"
    report: dict[str, Any] = {
        "run_id": manifest.get("run_id"),
        "status": status,
        "run_git_sha": manifest.get("git_sha"),
        "run_working_tree_clean": manifest.get("working_tree_clean"),
        "decision_count": len(decisions),
        "parity_mismatches": len(failures),
        "file_checks": file_checks,
        "capture_checks": capture_checks,
        "mismatches": failures,
        "captured_minute_rows": len(bars),
        "captured_warmup_rows": len(warmup),
        "index_rows": {market: len(rows) for market, rows in indexes.items()},
    }
    return report

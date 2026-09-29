from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.quality import inspect_bars
from krx_trader.kis.auth import TokenManager
from krx_trader.kis.rest import KisRestClient
from krx_trader.kis.transport import UrllibTransport
from krx_trader.models import Bar
from krx_trader.research.phase5_backfill import (
    HistoricalBarsClient,
    assert_safe_research_date,
    run_phase5_backfill,
)
from krx_trader.research.phase6 import assert_external_evidence_allowed

KST = ZoneInfo("Asia/Seoul")
SAFE_START = date(2026, 4, 17)
DEVELOPMENT_END = date(2026, 6, 30)
SECONDARY_START = date(2026, 7, 1)
SAFE_END = date(2026, 7, 27)
EXTERNAL_START = date(2026, 1, 5)
EXTERNAL_END = date(2026, 4, 16)
LOCKED_HOLDOUT_START = date(2026, 7, 28)
SESSION_OPEN = time(9, 0)
SESSION_CLOSE = time(15, 20)
EXPECTED_SESSION_MINUTES = 380
EXTREME_GAP_THRESHOLD_PCT = 20.0
NEAR_APPROX_LIMIT_PCT = 28.5
MIN_MEANINGFUL_GAP_PCT = 1.0
FEE_PER_SIDE = 0.00015
SELL_TAX = 0.0020
SLIPPAGE_PER_SIDE = 15.0 / 10_000
ROUND_TRIP_COST_PCT = (2 * FEE_PER_SIDE + SELL_TAX + 2 * SLIPPAGE_PER_SIDE) * 100
STRESS_MULTIPLIERS = (1.0, 1.5, 2.0)
OUTPUT_DIR = Path("runtime/research/phase8")
COHORT_PATH = Path("runtime/research/phase4/cohort-manifest.json")
SPLIT_PATH = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json")
ACQUISITION_PATH = OUTPUT_DIR / "phase8-acquisition-manifest.json"

CHECKPOINT_MINUTES = {
    "09:15": 15,
    "09:30": 30,
    "10:00": 60,
    "11:00": 120,
    "14:00": 300,
}
GAP_BUCKETS = (
    (-math.inf, -5.0, "<= -5%"),
    (-5.0, -3.0, "-5% to -3%"),
    (-3.0, -1.0, "-3% to -1%"),
    (-1.0, 0.0, "-1% to 0%"),
    (0.0, 1.0, "0 to +1%"),
    (1.0, 3.0, "+1% to +3%"),
    (3.0, 5.0, "+3% to +5%"),
    (5.0, math.inf, ">= +5%"),
)


@dataclass(frozen=True, slots=True)
class Phase8Dataset:
    cohort_symbols: tuple[str, ...]
    market_by_symbol: dict[str, str]
    period_name: str
    period_sessions: tuple[date, ...]
    minutes_by_symbol_session: dict[tuple[str, date], tuple[Bar, ...]]
    daily_by_symbol: dict[str, tuple[Bar, ...]]
    partition_hashes: dict[str, str]
    daily_safe_hashes: dict[str, str]
    dq: dict[str, Any]
    dataset_hash: str
    cohort_hash: str
    period_split_hash: str


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNAVAILABLE"


def assert_phase8_development_session(session: date) -> None:
    assert_safe_research_date(session)
    if not SAFE_START <= session <= DEVELOPMENT_END:
        raise ValueError("Phase 8 Development access is limited to 2026-04-17 through 2026-06-30")


def assert_phase8_secondary_allowed(
    *, development_candidate: bool, exact_rule_frozen: bool, sessions: Iterable[date]
) -> None:
    requested = list(sessions)
    if not development_candidate or not exact_rule_frozen:
        raise ValueError("Phase 8 Secondary is locked until an exact Development candidate is frozen")
    if not requested or min(requested) < SECONDARY_START or max(requested) > SAFE_END:
        raise ValueError("Phase 8 Secondary access must remain within 2026-07-01 through 2026-07-27")
    for session in requested:
        assert_safe_research_date(session)


def assert_phase8_external_allowed(
    *, candidate_preregistered: bool, strategy_freeze_commit: str | None, sessions: Iterable[date]
) -> None:
    assert_external_evidence_allowed(
        candidate_preregistered=candidate_preregistered,
        strategy_freeze_commit=strategy_freeze_commit,
        sessions=sessions,
    )


def _period_sessions(split_path: Path, split_name: str) -> list[date]:
    manifest = json.loads(split_path.read_text(encoding="utf-8"))
    split_key = "development" if split_name == "development" else "validation"
    sessions = sorted({date.fromisoformat(item) for item in manifest["splits"][split_key]["sessions"]})
    if not sessions:
        raise ValueError(f"Phase 8 {split_name} sessions are missing")
    if split_name == "development":
        for session in sessions:
            assert_phase8_development_session(session)
    elif split_name == "secondary":
        if any(not SECONDARY_START <= item <= SAFE_END for item in sessions):
            raise ValueError("Secondary split manifest exceeds Phase 8 safe-period bounds")
        for session in sessions:
            assert_safe_research_date(session)
    else:
        raise ValueError("split_name must be development or secondary")
    return sessions


def previous_valid_close(daily_bars: Sequence[Bar], session: date) -> tuple[date, float] | None:
    """Return the latest positive daily close before session; callers bound input to safe dates."""
    valid = [
        bar for bar in daily_bars
        if bar.time.astimezone(KST).date() < session and math.isfinite(bar.close) and bar.close > 0
    ]
    if not valid:
        return None
    latest_date = max(item.time.astimezone(KST).date() for item in valid)
    latest = [item for item in valid if item.time.astimezone(KST).date() == latest_date]
    if len(latest) != 1:
        return None
    return latest_date, float(latest[0].close)


def session_open_bar(minute_bars: Sequence[Bar], session: date) -> Bar | None:
    open_time = datetime.combine(session, SESSION_OPEN, KST)
    matches = [item for item in minute_bars if item.time.astimezone(KST) == open_time]
    return matches[0] if len(matches) == 1 and matches[0].open > 0 else None


def opening_gap_pct(current_open: float, prior_close: float) -> float:
    if current_open <= 0 or prior_close <= 0:
        raise ValueError("opening gap prices must be positive")
    return (current_open / prior_close - 1.0) * 100.0


def gap_bucket(gap_pct: float) -> str:
    if gap_pct <= -5.0:
        return "<= -5%"
    if gap_pct <= -3.0:
        return "-5% to -3%"
    if gap_pct <= -1.0:
        return "-3% to -1%"
    if gap_pct < 0.0:
        return "-1% to 0%"
    if gap_pct < 1.0:
        return "0 to +1%"
    if gap_pct < 3.0:
        return "+1% to +3%"
    if gap_pct < 5.0:
        return "+3% to +5%"
    return ">= +5%"


def _bar_close_at(minute_bars: Sequence[Bar], checkpoint: datetime) -> Bar | None:
    expected_bar_start = checkpoint - timedelta(minutes=1)
    found = [item for item in minute_bars if item.time.astimezone(KST) == expected_bar_start]
    return found[0] if len(found) == 1 else None


def _window_bars(minute_bars: Sequence[Bar], session: date, minutes: int) -> list[Bar]:
    start = datetime.combine(session, SESSION_OPEN, KST)
    end = start + timedelta(minutes=minutes)
    return [item for item in minute_bars if start <= item.time.astimezone(KST) < end]


def _is_complete_window(bars: Sequence[Bar], session: date, minutes: int) -> bool:
    start = datetime.combine(session, SESSION_OPEN, KST)
    expected = [start + timedelta(minutes=index) for index in range(minutes)]
    actual = [item.time.astimezone(KST) for item in bars]
    return actual == expected


def _range_metrics(minute_bars: Sequence[Bar], session: date, minutes: int) -> dict[str, Any]:
    bars = _window_bars(minute_bars, session, minutes)
    complete = _is_complete_window(bars, session, minutes)
    if not bars or not complete:
        return {"complete": False, "observed_minutes": len(bars), "expected_minutes": minutes,
                "high": None, "low": None, "close": None, "width_pct": None,
                "close_position": None}
    high = max(item.high for item in bars)
    low = min(item.low for item in bars)
    close = bars[-1].close
    return {
        "complete": True,
        "observed_minutes": len(bars),
        "expected_minutes": minutes,
        "high": high,
        "low": low,
        "close": close,
        "width_pct": (high - low) / bars[0].open * 100.0,
        "close_position": (close - low) / (high - low) if high > low else 0.5,
    }


def _touch_previous_close(bars: Sequence[Bar], prior_close: float, gap_pct: float) -> bool:
    if gap_pct > 0:
        return any(item.low <= prior_close for item in bars)
    if gap_pct < 0:
        return any(item.high >= prior_close for item in bars)
    return False


def _gap_state(first_hour: Sequence[Bar], prior_close: float, open_price: float, gap_pct: float) -> str | None:
    if gap_pct == 0 or len(first_hour) != 60:
        return None
    close = first_hour[-1].close
    high = max(item.high for item in first_hour)
    low = min(item.low for item in first_hour)
    if gap_pct > 0:
        filled = low <= prior_close
        if filled:
            return "GAP_REVERSED" if close < prior_close else "GAP_FULLY_FILLED"
        if prior_close < close < open_price:
            return "GAP_PARTIALLY_FILLED"
        if high > open_price and close >= open_price:
            return "GAP_EXTENDED"
        return "GAP_HELD"
    filled = high >= prior_close
    if filled:
        return "GAP_REVERSED" if close > prior_close else "GAP_FULLY_FILLED"
    if open_price < close < prior_close:
        return "GAP_PARTIALLY_FILLED"
    if low < open_price and close <= open_price:
        return "GAP_EXTENDED"
    return "GAP_HELD"


def _liquidity_bucket(daily_bars: Sequence[Bar], session: date) -> tuple[str, float | None, int]:
    prior = [item for item in daily_bars if item.time.astimezone(KST).date() < session]
    prior = sorted(prior, key=lambda item: item.time.astimezone(KST))[-20:]
    if len(prior) < 5:
        return "UNKNOWN", None, len(prior)
    average = statistics.fmean(item.close * item.volume for item in prior)
    label = "LOW_LT_50M" if average < 50_000_000 else (
        "MID_50M_250M" if average < 250_000_000 else (
            "HIGH_250M_1B" if average < 1_000_000_000 else "VERY_HIGH_GE_1B"
        )
    )
    return label, average, len(prior)


def _price_bucket(price: float) -> str:
    if price < 10_000:
        return "LT_10K"
    if price < 30_000:
        return "10K_30K"
    if price < 50_000:
        return "30K_50K"
    return "GE_50K"


def _gap_fraction(price: float, prior_close: float, open_price: float) -> float | None:
    gap = open_price - prior_close
    return (price - prior_close) / gap if gap else None


def build_opening_gap_events(
    *,
    minutes_by_symbol_session: dict[tuple[str, date], tuple[Bar, ...]],
    daily_by_symbol: dict[str, tuple[Bar, ...]],
    market_by_symbol: dict[str, str],
    period_name: str = "development",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for (symbol, session), minute_tuple in sorted(minutes_by_symbol_session.items()):
        if period_name == "development":
            assert_phase8_development_session(session)
        elif period_name == "secondary":
            if not SECONDARY_START <= session <= SAFE_END:
                raise ValueError("Phase 8 Secondary event escaped the locked safe Secondary range")
            assert_safe_research_date(session)
        else:
            raise ValueError("period_name must be development or secondary")
        minute_bars = sorted(
            [item for item in minute_tuple if SESSION_OPEN <= item.time.astimezone(KST).time() < SESSION_CLOSE],
            key=lambda item: item.time,
        )
        daily_prior = previous_valid_close(daily_by_symbol.get(symbol, ()), session)
        prior_minute_sessions = [
            day for candidate_symbol, day in minutes_by_symbol_session
            if candidate_symbol == symbol and SAFE_START <= day < session
        ]
        minute_prior: tuple[date, float, str] | None = None
        if prior_minute_sessions:
            prior_day = max(prior_minute_sessions)
            prior_bars = sorted(minutes_by_symbol_session[(symbol, prior_day)], key=lambda item: item.time)
            eligible_prior = [
                item for item in prior_bars
                if SESSION_OPEN <= item.time.astimezone(KST).time() < SESSION_CLOSE
            ]
            if eligible_prior:
                final_expected = datetime.combine(prior_day, SESSION_CLOSE, KST) - timedelta(minutes=1)
                final_bar = next((item for item in eligible_prior if item.time.astimezone(KST) == final_expected), None)
                if final_bar is not None:
                    minute_prior = (prior_day, float(final_bar.close), "KIS_PREVIOUS_CONTINUOUS_SESSION_15_19_CLOSE_PROXY")
                else:
                    final_bar = eligible_prior[-1]
                    minute_prior = (prior_day, float(final_bar.close), "KIS_PREVIOUS_CONTINUOUS_SESSION_LAST_AVAILABLE_CLOSE_PROXY")
        if daily_prior is not None and (minute_prior is None or daily_prior[0] >= minute_prior[0]):
            prior = (*daily_prior, "KIS_DAILY_CLOSE_RAW")
        elif minute_prior is not None:
            prior = minute_prior
        else:
            prior = None
        if prior is None:
            excluded["NO_SAFE_PREVIOUS_SESSION_CLOSE"] += 1
            continue
        open_bar = session_open_bar(minute_bars, session)
        if open_bar is None:
            excluded["NO_EXACT_0900_OPEN"] += 1
            continue
        if any(issue for issue in inspect_bars(minute_bars)):
            excluded["INVALID_MINUTE_SESSION"] += 1
            continue

        prior_session, prior_close, prior_close_source = prior
        open_price = float(open_bar.open)
        gap_pct = opening_gap_pct(open_price, prior_close)
        first15 = _range_metrics(minute_bars, session, 15)
        first30 = _range_metrics(minute_bars, session, 30)
        first_hour = _window_bars(minute_bars, session, 60)
        first_hour_complete = _is_complete_window(first_hour, session, 60)
        state = _gap_state(first_hour, prior_close, open_price, gap_pct) if first_hour_complete else None
        end_points: dict[str, dict[str, Any] | None] = {}
        for label, minutes in CHECKPOINT_MINUTES.items():
            checkpoint = datetime.combine(session, SESSION_OPEN, KST) + timedelta(minutes=minutes)
            bar = _bar_close_at(minute_bars, checkpoint)
            end_points[label] = (
                {"close": float(bar.close), "timestamp": bar.time.astimezone(KST).isoformat()}
                if bar is not None else None
            )
        final_bar = minute_bars[-1] if minute_bars else None
        final_price = float(final_bar.close) if final_bar is not None else None
        returns: dict[str, float | None] = {}
        retained: dict[str, float | None] = {}
        forward_after_next_open: dict[str, float | None] = {}
        for label, point in end_points.items():
            price = point["close"] if point else None
            returns[label] = (price / open_price - 1.0) * 100.0 if price else None
            retained[label] = _gap_fraction(price, prior_close, open_price) if price else None
            if label in CHECKPOINT_MINUTES:
                boundary = datetime.combine(session, SESSION_OPEN, KST) + timedelta(minutes=CHECKPOINT_MINUTES[label])
                executable = next_executable_open(minute_bars, boundary)
                forward_after_next_open[label] = (
                    (final_price / executable.open - 1.0) * 100.0
                    if executable is not None and final_bar is not None else None
                )
        returns["CONTINUOUS_FINAL"] = (
            (final_price / open_price - 1.0) * 100.0 if final_price else None
        )
        retained["CONTINUOUS_FINAL"] = (
            _gap_fraction(final_price, prior_close, open_price) if final_price else None
        )

        highs = [(item.high, index, item) for index, item in enumerate(minute_bars)]
        lows = [(item.low, index, item) for index, item in enumerate(minute_bars)]
        max_high, _, high_bar = max(highs, key=lambda row: row[0])
        min_low, _, low_bar = min(lows, key=lambda row: row[0])
        time_to_mfe = int((high_bar.time.astimezone(KST) - datetime.combine(session, SESSION_OPEN, KST)).total_seconds() // 60)
        time_to_mae = int((low_bar.time.astimezone(KST) - datetime.combine(session, SESSION_OPEN, KST)).total_seconds() // 60)
        excursions = {
            "mfe_pct": (max_high / open_price - 1.0) * 100.0,
            "mae_pct": (min_low / open_price - 1.0) * 100.0,
            "time_to_mfe_minutes": time_to_mfe,
            "time_to_mae_minutes": time_to_mae,
            "mfe_timestamp": high_bar.time.astimezone(KST).isoformat(),
            "mae_timestamp": low_bar.time.astimezone(KST).isoformat(),
        }

        gap_direction = 1 if gap_pct > 0 else -1 if gap_pct < 0 else 0
        gap_fills: dict[str, bool | None] = {}
        for label, window in (("15M", 15), ("30M", 30), ("60M", 60), ("SESSION", 380)):
            observed = _window_bars(minute_bars, session, window)
            touched = _touch_previous_close(observed, prior_close, gap_pct)
            gap_fills[label] = touched if touched else (
                False if _is_complete_window(observed, session, window) else None
            ) if gap_direction else None
        gap_fill_fraction = {
            label: (1.0 - value if value is not None else None)
            for label, value in retained.items()
        }
        range_data = {"15M": first15, "30M": first30}
        for item in range_data.values():
            close = item["close"]
            item["gap_retained_fraction"] = (
                _gap_fraction(close, prior_close, open_price)
                if close is not None else None
            )

        expected = [datetime.combine(session, SESSION_OPEN, KST) + timedelta(minutes=i) for i in range(380)]
        actual = [item.time.astimezone(KST) for item in minute_bars]
        exact_session = actual == expected
        confidence = (
            "HIGH_CONFIDENCE" if exact_session and first_hour_complete else
            "PARTIAL" if len(minute_bars) >= 361 and first_hour_complete else
            "UNRELIABLE"
        )
        suspicious = abs(gap_pct) >= EXTREME_GAP_THRESHOLD_PCT
        price_limit_context = (
            "NEAR_APPROX_30_PERCENT_BAND" if abs(gap_pct) >= NEAR_APPROX_LIMIT_PCT else
            "NOT_NEAR_APPROX_30_PERCENT_BAND"
        )
        liquidity, trailing_turnover, liquidity_sessions = _liquidity_bucket(
            daily_by_symbol.get(symbol, ()), session
        )
        event = {
            "symbol": symbol,
            "market": market_by_symbol.get(symbol, "UNKNOWN"),
            "session": session.isoformat(),
            "previous_session": prior_session.isoformat(),
            "previous_close": prior_close,
            "prior_close_source": prior_close_source,
            "current_open": open_price,
            "gap_pct": gap_pct,
            "gap_direction": "UP" if gap_direction > 0 else "DOWN" if gap_direction < 0 else "FLAT",
            "gap_bucket": gap_bucket(gap_pct),
            "suspicious_extreme_gap": suspicious,
            "corporate_action_status": "UNVERIFIED_SUSPICIOUS_PRICE_JUMP" if suspicious else "UNVERIFIED_NO_EXTREME_JUMP",
            "price_limit_context": price_limit_context,
            "price_limit_semantics": "approximate absolute 30% gap band only; exact applicable limit unavailable",
            "open_timestamp": open_bar.time.astimezone(KST).isoformat(),
            "market_price_bucket": _price_bucket(open_price),
            "liquidity_bucket": liquidity,
            "trailing_turnover_proxy_krw": trailing_turnover,
            "trailing_turnover_sessions": liquidity_sessions,
            "observed_minutes": len(minute_bars),
            "expected_minutes": EXPECTED_SESSION_MINUTES,
            "first_hour_observed_minutes": len(first_hour),
            "first_hour_complete": first_hour_complete,
            "session_complete": exact_session,
            "data_confidence": confidence,
            "checkpoint_prices": end_points,
            "open_to_checkpoint_return_pct": returns,
            "gap_retained_fraction": retained,
            "gap_fill_fraction": gap_fill_fraction,
            "forward_next_bar_open_to_continuous_final_pct": forward_after_next_open,
            "gap_fill": gap_fills,
            "opening_ranges": range_data,
            "first_hour_state": state,
            "continuous_final_timestamp": final_bar.time.astimezone(KST).isoformat() if final_bar else None,
            "continuous_final_price": final_price,
            "excursions_from_open": excursions,
            "longer_horizon_daily_close_return_pct": {},
        }
        events.append(event)
    return events, dict(excluded)


def _daily_return_horizons(
    events: list[dict[str, Any]],
    daily_by_symbol: dict[str, tuple[Bar, ...]],
    session_calendar: Sequence[date],
) -> None:
    """Attach outcomes only when the exact next Development sessions have daily closes."""
    calendar = tuple(sorted(set(session_calendar)))
    calendar_index = {session: index for index, session in enumerate(calendar)}
    for event in events:
        session = date.fromisoformat(event["session"])
        daily = sorted(daily_by_symbol.get(event["symbol"], ()), key=lambda item: item.time)
        bars_by_session: dict[date, list[Bar]] = defaultdict(list)
        for item in daily:
            bars_by_session[item.time.astimezone(KST).date()].append(item)
        open_price = float(event["current_open"])
        outcomes: dict[str, float | None] = {}
        statuses: dict[str, str] = {}
        for horizon, label in ((1, "NEXT_SESSION_CLOSE"), (3, "THREE_SESSION_CLOSE")):
            if event.get("suspicious_extreme_gap"):
                outcomes[label] = None
                statuses[label] = "SUSPICIOUS_OPENING_GAP_OR_CORPORATE_ACTION"
                continue
            index = calendar_index.get(session)
            if index is None:
                outcomes[label] = None
                statuses[label] = "SESSION_OUTSIDE_DEVELOPMENT_CALENDAR"
                continue
            future_sessions = calendar[index + 1:index + horizon + 1]
            if len(future_sessions) != horizon:
                outcomes[label] = None
                statuses[label] = "SAFE_DAILY_HORIZON_UNAVAILABLE"
                continue
            daily_path = [bars_by_session.get(day, []) for day in (session, *future_sessions)]
            if any(len(rows) != 1 for rows in daily_path):
                outcomes[label] = None
                statuses[label] = "SAFE_DAILY_HORIZON_UNAVAILABLE"
                continue
            session_bar = daily_path[0][0]
            future_bars = [rows[0] for rows in daily_path[1:]]
            prior_close = float(event.get("previous_close", 0.0))
            path_prices = ([prior_close] if prior_close > 0 else []) + [
                bar.close for bar in (session_bar, *future_bars)
            ]
            suspicious_step = any(
                abs(right / left - 1.0) * 100.0 >= EXTREME_GAP_THRESHOLD_PCT
                for left, right in pairwise(path_prices) if left > 0
            )
            if suspicious_step:
                outcomes[label] = None
                statuses[label] = "SUSPICIOUS_RAW_PRICE_JUMP_DURING_HORIZON"
                continue
            outcomes[label] = (future_bars[-1].close / open_price - 1.0) * 100.0
            statuses[label] = "CLEAN_RAW_DAILY_HORIZON"
        event["longer_horizon_daily_close_return_pct"] = outcomes
        event["longer_horizon_status"] = statuses


def _development_daily_bars(
    cache_root: Path, symbols: Sequence[str], period_end: date
) -> tuple[dict[str, tuple[Bar, ...]], dict[str, str], dict[str, int]]:
    """Read only safe rows through period_end; external and holdout date rows are filtered out."""
    lower = datetime.combine(SAFE_START, time.min, KST).isoformat()
    upper = datetime.combine(period_end + timedelta(days=1), time.min, KST).isoformat()
    daily_by_symbol: dict[str, tuple[Bar, ...]] = {}
    safe_hashes: dict[str, str] = {}
    failures: Counter[str] = Counter()
    for symbol in symbols:
        path = cache_root / "daily" / f"{symbol}-1d.parquet"
        if not path.is_file():
            failures["DAILY_CACHE_MISSING"] += 1
            continue
        try:
            table = pq.read_table(path, filters=[("timestamp", ">=", lower), ("timestamp", "<", upper)])
            rows = sorted(table.to_pylist(), key=lambda row: row["timestamp"])
            bars = tuple(
                Bar(
                    time=datetime.fromisoformat(row["timestamp"]),
                    open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                    close=float(row["close"]), volume=int(row["volume"]),
                ) for row in rows
            )
            if inspect_bars(list(bars)):
                failures["DAILY_DEVELOPMENT_ROWS_INVALID"] += 1
                continue
            if bars:
                daily_by_symbol[symbol] = bars
                safe_hashes[symbol] = canonical_sha256([
                    [bar.time.astimezone(KST).isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]
                    for bar in bars
                ])
            else:
                failures["NO_DAILY_DEVELOPMENT_ROWS"] += 1
        except (OSError, ValueError, KeyError, TypeError):
            failures["DAILY_CACHE_VALIDATION_FAILURE"] += 1
    return daily_by_symbol, safe_hashes, dict(failures)


def load_phase8_period_dataset(
    *,
    period_name: str = "development",
    cohort_manifest_path: Path = COHORT_PATH,
    split_manifest_path: Path = SPLIT_PATH,
    acquisition_manifest_path: Path = ACQUISITION_PATH,
    cache_root: Path = Path("data"),
) -> tuple[Phase8Dataset, dict[str, Any]]:
    cohort_data = json.loads(cohort_manifest_path.read_text(encoding="utf-8"))["cohort_60"]
    symbols = tuple(cohort_data["symbols"])
    if len(symbols) != 60 or len(set(symbols)) != 60:
        raise ValueError("Phase 8 requires the frozen 60-symbol cohort")
    markets = {symbol: ("KOSPI" if index < 30 else "KOSDAQ") for index, symbol in enumerate(symbols)}
    sessions = _period_sessions(split_manifest_path, period_name)
    period_end = DEVELOPMENT_END if period_name == "development" else SAFE_END
    split_data = json.loads(split_manifest_path.read_text(encoding="utf-8"))
    split_key = "development" if period_name == "development" else "validation"
    split_hash = canonical_sha256(split_data["splits"][split_key])
    cache = ParquetBarCache(cache_root)
    minute_rows: dict[tuple[str, date], tuple[Bar, ...]] = {}
    partition_hashes: dict[str, str] = {}
    invalid: dict[str, str] = {}
    absent = 0
    observed = 0
    missing_minutes = 0
    for symbol in symbols:
        for session in sessions:
            if period_name == "development":
                assert_phase8_development_session(session)
            else:
                if not SECONDARY_START <= session <= SAFE_END:
                    raise ValueError("Phase 8 Secondary partitions escaped the locked safe-period range")
                assert_safe_research_date(session)
            path = cache.partition_path("minute", symbol, "1m", session)
            sidecar = path.with_suffix(".metadata.json")
            if not path.is_file() and not sidecar.is_file():
                absent += 1
                continue
            if not path.is_file() or not sidecar.is_file():
                invalid[f"{symbol}:{session.isoformat()}"] = "PARQUET_OR_METADATA_MISSING"
                continue
            try:
                bars = cache.load("minute", symbol, "1m", session)
                metadata = json.loads(sidecar.read_text(encoding="utf-8"))
                if metadata.get("symbol") != symbol or metadata.get("interval") != "1m":
                    raise ValueError("partition provenance identity mismatch")
                if any(item.time.astimezone(KST).date() != session for item in bars):
                    raise ValueError("partition includes a different session date")
                minute_rows[(symbol, session)] = tuple(bars)
                partition_hashes[f"{symbol}:{session.isoformat()}"] = str(metadata["sha256"])
                observed += len(bars)
                missing_minutes += max(0, EXPECTED_SESSION_MINUTES - len(bars))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                invalid[f"{symbol}:{session.isoformat()}"] = type(exc).__name__
    daily, daily_hashes, daily_failures = _development_daily_bars(cache_root, symbols, period_end)
    cohort_hash = canonical_sha256(list(symbols))
    dataset_hash = canonical_sha256({
        "development_minute_partitions": partition_hashes,
        "safe_development_daily_rows": daily_hashes,
        "cohort_hash": cohort_hash,
        "period_name": period_name,
        "period_split_hash": split_hash,
    })
    acquisition = (
        json.loads(acquisition_manifest_path.read_text(encoding="utf-8"))
        if acquisition_manifest_path.is_file() else {}
    )
    acquisition_results = acquisition.get("symbol_results", {})
    complete_symbols = sorted(
        symbol for symbol, result in acquisition_results.items()
        if result.get("status") == "SUCCESS" and result.get("succeeded_sessions") == acquisition.get("total_sessions")
    )
    partial_symbols = sorted(
        symbol for symbol, result in acquisition_results.items()
        if symbol not in complete_symbols and int(result.get("succeeded_sessions", 0)) > 0
    )
    not_acquired = sorted(set(symbols) - set(complete_symbols) - set(partial_symbols))
    holdout = {"holdout_partitions_opened": 0, "state": "LOCKED_NOT_EVALUATED"}
    dq = {
        "artifact": "phase8-dq",
        "source_git_sha": current_git_sha(),
        "provider": "KIS official read-only historical OHLCV",
        "period": {"name": period_name,
                   "start": sessions[0].isoformat(), "end": sessions[-1].isoformat(),
                   "sessions": len(sessions)},
        "cohort": {"target_symbols": len(symbols), "complete_safe_symbols": len(complete_symbols),
                   "partial_safe_symbols": len(partial_symbols), "not_acquired_symbols": len(not_acquired),
                   "complete_symbol_list": complete_symbols, "partial_symbol_list": partial_symbols,
                   "not_acquired_symbol_list": not_acquired},
        "development_minute_partitions": {"expected": len(symbols) * len(sessions),
                                          "verified_present": len(partition_hashes), "absent": absent,
                                          "invalid": invalid, "observed_minutes": observed,
                                          "expected_minute_absences": missing_minutes,
                                          "synthetic_minutes_added": 0,
                                          "absence_cause": "UNKNOWN; no synthetic no-trade or halt labels"},
        "development_daily_cache": {"symbols_with_safe_rows": len(daily), "safe_row_hashes": daily_hashes,
                                    "failures": daily_failures},
        "analysis_secondary_partitions_opened": len(partition_hashes) if period_name == "secondary" else 0,
        "external_partitions_opened": 0,
        "holdout_integrity": holdout,
    }
    dataset = Phase8Dataset(
        symbols, markets, period_name, tuple(sessions), minute_rows, daily, partition_hashes, daily_hashes,
        dq, dataset_hash, cohort_hash, split_hash,
    )
    return dataset, acquisition


def load_phase8_development_dataset(**kwargs) -> tuple[Phase8Dataset, dict[str, Any]]:
    return load_phase8_period_dataset(period_name="development", **kwargs)


def next_executable_open(minute_bars: Sequence[Bar], completed_at: datetime) -> Bar | None:
    """Use the first bar beginning at the completed signal boundary, never the signal bar close."""
    boundary = completed_at.astimezone(KST)
    matches = [item for item in minute_bars if item.time.astimezone(KST) == boundary]
    return matches[0] if len(matches) == 1 and matches[0].open > 0 else None


def _values(values: Iterable[float | None]) -> list[float]:
    return [float(item) for item in values if item is not None and math.isfinite(float(item))]


def _mean(values: Iterable[float | None]) -> float | None:
    selected = _values(values)
    return statistics.fmean(selected) if selected else None


def _median(values: Iterable[float | None]) -> float | None:
    selected = _values(values)
    return statistics.median(selected) if selected else None


def _profit_factor(values: Iterable[float | None]) -> float | str | None:
    selected = _values(values)
    gains = sum(item for item in selected if item > 0)
    losses = -sum(item for item in selected if item < 0)
    if not selected or losses == 0:
        return "INF" if gains > 0 else None
    return gains / losses


def summarize_returns(values: Sequence[float | None]) -> dict[str, Any]:
    selected = _values(values)
    return {
        "count": len(selected),
        "mean_pct": _mean(selected),
        "median_pct": _median(selected),
        "win_rate_pct": (sum(item > 0 for item in selected) / len(selected) * 100.0) if selected else None,
        "profit_factor_gross": _profit_factor(selected),
    }


def _meaningful(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in events
        if abs(float(item["gap_pct"])) >= MIN_MEANINGFUL_GAP_PCT
    ]


def _confidence_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in events if item["data_confidence"] == "HIGH_CONFIDENCE"]


def _clean_confidence_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in _confidence_events(events)
        if not item["suspicious_extreme_gap"] and item["prior_close_source"] == "KIS_DAILY_CLOSE_RAW"
    ]


def _directional_value(event: dict[str, Any], return_value: float | None) -> float | None:
    if return_value is None:
        return None
    direction = 1 if event["gap_direction"] == "UP" else -1 if event["gap_direction"] == "DOWN" else 0
    return return_value * direction if direction else None


def _aggregate_events(events: Sequence[dict[str, Any]], horizon: str) -> dict[str, Any]:
    values = [item["open_to_checkpoint_return_pct"].get(horizon) for item in events]
    directional = [_directional_value(item, value) for item, value in zip(events, values)]
    return {
        **summarize_returns(values),
        "mean_gap_direction_aligned_pct": _mean(directional),
        "mean_return_minus_cost_pct": (
            _mean(values) - ROUND_TRIP_COST_PCT if _mean(values) is not None else None
        ),
    }


def _event_horizon(event: dict[str, Any], horizon: str) -> float | None:
    return event["open_to_checkpoint_return_pct"].get(horizon)


def _gap_bucket_analysis(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for _, _, label in GAP_BUCKETS:
        rows = [item for item in events if item["gap_bucket"] == label]
        output[label] = {
            "count": len(rows),
            "gap_mean_pct": _mean(item["gap_pct"] for item in rows),
            "up_count": sum(item["gap_direction"] == "UP" for item in rows),
            "down_count": sum(item["gap_direction"] == "DOWN" for item in rows),
            "outcomes": {
                horizon: _aggregate_events(rows, horizon)
                for horizon in (*CHECKPOINT_MINUTES.keys(), "CONTINUOUS_FINAL")
            },
            "high_confidence_count": sum(item["data_confidence"] == "HIGH_CONFIDENCE" for item in rows),
            "high_confidence_non_suspicious_daily_close_count": len(_clean_confidence_events(rows)),
            "suspicious_extreme_gap_count": sum(item["suspicious_extreme_gap"] for item in rows),
        }
    return {
        "period": [SAFE_START.isoformat(), DEVELOPMENT_END.isoformat()],
        "gap_definition": "current 09:00 minute-bar open / latest safe prior close - 1; source is KIS daily close when current, else labeled prior-session minute-close proxy",
        "boundary_policy": "exact -5/-3/-1/+1/+3/+5 percent values enter the bucket beginning at that boundary; zero enters 0 to +1 percent",
        "buckets": output,
    }


def _gap_fill_analysis(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for direction in ("UP", "DOWN"):
        rows = [item for item in events if item["gap_direction"] == direction]
        horizon_summary: dict[str, Any] = {}
        for horizon in ("15M", "30M", "60M", "SESSION"):
            known = [item for item in rows if item["gap_fill"][horizon] is not None]
            touched = [item for item in known if item["gap_fill"][horizon] is True]
            horizon_summary[horizon] = {
                "events": len(rows),
                "known_observations": len(known),
                "full_gap_fill_count": len(touched),
                "full_gap_fill_rate_pct": len(touched) / len(known) * 100.0 if known else None,
                "mean_partial_fill_fraction_at_checkpoint": _mean(
                    item["gap_fill_fraction"].get(
                        {"15M": "09:15", "30M": "09:30", "60M": "10:00", "SESSION": "CONTINUOUS_FINAL"}[horizon]
                    ) for item in known
                ),
            }
        result[direction] = horizon_summary
    return {
        "definition": "gap-up fills when an observed low touches prior close; gap-down fills when an observed high touches prior close",
        "partial_fill_fraction": "one minus the gap retained fraction at the checkpoint close; may be below zero or above one",
        "unknown_when_incomplete": True,
        "by_gap_direction": result,
    }


def _opening_range_analysis(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in ("15M", "30M"):
        for direction in ("UP", "DOWN"):
            rows = [item for item in events if item["gap_direction"] == direction and item["opening_ranges"][horizon]["complete"]]
            ranges = [item["opening_ranges"][horizon] for item in rows]
            result[f"{direction}_{horizon}"] = {
                "count": len(rows),
                "mean_range_width_pct": _mean(item["width_pct"] for item in ranges),
                "median_range_width_pct": _median(item["width_pct"] for item in ranges),
                "mean_close_position": _mean(item["close_position"] for item in ranges),
                "mean_gap_retained_fraction": _mean(item["gap_retained_fraction"] for item in ranges),
                "no_range_break_trade_created": True,
            }
    return {
        "definition": "fixed first 15 and first 30 minutes from 09:00; range statistics require every minute in the window",
        "groups": result,
    }


def _first_hour_state_analysis(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    states = ("GAP_HELD", "GAP_EXTENDED", "GAP_PARTIALLY_FILLED", "GAP_FULLY_FILLED", "GAP_REVERSED")
    result: dict[str, Any] = {}
    for direction in ("UP", "DOWN"):
        for state in states:
            rows = [item for item in events if item["gap_direction"] == direction and item["first_hour_state"] == state]
            result[f"{direction}_{state}"] = {
                "count": len(rows),
                "high_confidence_count": sum(item["data_confidence"] == "HIGH_CONFIDENCE" for item in rows),
                "return_open_to_10_00": summarize_returns([_event_horizon(item, "10:00") for item in rows]),
                "return_open_to_11_00": summarize_returns([_event_horizon(item, "11:00") for item in rows]),
                "return_open_to_continuous_final": summarize_returns([_event_horizon(item, "CONTINUOUS_FINAL") for item in rows]),
                "mean_gap_retained_fraction_10_00": _mean(item["gap_retained_fraction"].get("10:00") for item in rows),
            }
    return {
        "states_use_only_completed_first_hour": True,
        "first_hour_end": "10:00 Asia/Seoul",
        "state_definition": {
            "GAP_HELD": "no prior-close touch; first-hour close remains on original gap side without an extension condition",
            "GAP_EXTENDED": "no prior-close touch; first-hour extreme extends beyond open and close remains beyond open",
            "GAP_PARTIALLY_FILLED": "no prior-close touch; close moves from open toward prior close",
            "GAP_FULLY_FILLED": "prior close touched and first-hour close has not crossed it",
            "GAP_REVERSED": "prior close touched and first-hour close crosses beyond prior close",
        },
        "groups": result,
    }


def _split_analysis(events: Sequence[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in _meaningful(events):
        groups[str(item.get(key, "UNKNOWN"))].append(item)
    return {
        "gap_event_definition": "absolute opening gap >= 1 percent",
        "groups": {
            label: {
                "count": len(rows),
                "up_count": sum(item["gap_direction"] == "UP" for item in rows),
                "down_count": sum(item["gap_direction"] == "DOWN" for item in rows),
                "high_confidence_count": sum(item["data_confidence"] == "HIGH_CONFIDENCE" for item in rows),
                "return_open_to_10_00": _aggregate_events(rows, "10:00"),
                "return_open_to_continuous_final": _aggregate_events(rows, "CONTINUOUS_FINAL"),
                "suspicious_extreme_gap_count": sum(item["suspicious_extreme_gap"] for item in rows),
            }
            for label, rows in sorted(groups.items())
        },
    }


def _prior_close_source_analysis(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in events:
        if abs(item["gap_pct"]) >= MIN_MEANINGFUL_GAP_PCT:
            groups[item["prior_close_source"]].append(item)
    return {
        "gap_event_definition": "absolute opening gap >= 1 percent",
        "source_semantics": {
            "KIS_DAILY_CLOSE_RAW": "raw daily close from KIS daily endpoint; may include closing auction",
            "KIS_PREVIOUS_CONTINUOUS_SESSION_15_19_CLOSE_PROXY": "last regular-session minute close at 15:19; closing auction excluded",
            "KIS_PREVIOUS_CONTINUOUS_SESSION_LAST_AVAILABLE_CLOSE_PROXY": "last observed prior-session regular minute close before 15:19; less complete",
        },
        "groups": {
            source: {
                "count": len(rows),
                "high_confidence_non_suspicious_count": len(_clean_confidence_events(rows)),
                "suspicious_extreme_gap_count": sum(item["suspicious_extreme_gap"] for item in rows),
                "return_open_to_10_00": _aggregate_events(rows, "10:00"),
                "return_open_to_continuous_final": _aggregate_events(rows, "CONTINUOUS_FINAL"),
            }
            for source, rows in sorted(groups.items())
        },
        "primary_sensitivity_source": "KIS_DAILY_CLOSE_RAW only",
    }


def _matched_controls(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    controls: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in events:
        if abs(item["gap_pct"]) < MIN_MEANINGFUL_GAP_PCT and item["open_to_checkpoint_return_pct"].get("10:00") is not None:
            key = (item["session"], item["market"], item["market_price_bucket"], item["liquidity_bucket"])
            controls[key].append(item)
    deltas: list[float] = []
    paired_controls: list[float] = []
    paired_events: list[float] = []
    pairs = 0
    for event in _meaningful(events):
        value = event["open_to_checkpoint_return_pct"].get("10:00")
        if value is None:
            continue
        key = (event["session"], event["market"], event["market_price_bucket"], event["liquidity_bucket"])
        reference = _mean(item["open_to_checkpoint_return_pct"].get("10:00") for item in controls.get(key, []))
        if reference is not None:
            pairs += 1
            paired_events.append(value)
            paired_controls.append(reference)
            deltas.append(value - reference)
    return {
        "control_definition": "same session, same market, same coarse open-price bucket and trailing 20-safe-session close-times-volume bucket; control absolute gap < 1 percent",
        "matched_event_count": pairs,
        "mean_matched_gap_event_return_open_to_10_00_pct": _mean(paired_events),
        "mean_matched_non_gap_control_return_open_to_10_00_pct": _mean(paired_controls),
        "mean_event_minus_control_pct_points": _mean(deltas),
        "positive_pair_share_pct": (sum(item > 0 for item in deltas) / len(deltas) * 100.0) if deltas else None,
        "minimum_control_count_per_match": 1,
        "interpretation": "cross-sectional matched descriptive control; not a causal estimate",
    }


def _concentration(events: Sequence[dict[str, Any]], return_key: str) -> dict[str, Any]:
    positive = [item for item in events if (value := item["open_to_checkpoint_return_pct"].get(return_key)) is not None and value > 0]
    total = sum(float(item["open_to_checkpoint_return_pct"][return_key]) for item in positive)
    if total <= 0:
        return {"positive_event_count": 0, "total_positive_return_pct": 0.0}

    def top_share(key: str, limit: int) -> dict[str, Any]:
        groups: dict[str, float] = defaultdict(float)
        for item in positive:
            groups[str(item[key])] += float(item["open_to_checkpoint_return_pct"][return_key])
        ordered = sorted(groups.items(), key=lambda pair: pair[1], reverse=True)
        value = sum(amount for _, amount in ordered[:limit])
        return {"keys": [key for key, _ in ordered[:limit]], "positive_return_contribution_pct": value,
                "share_of_positive_return_pct": value / total * 100.0}

    ordered_events = sorted(positive, key=lambda item: item["open_to_checkpoint_return_pct"][return_key], reverse=True)
    event_contribution = sum(item["open_to_checkpoint_return_pct"][return_key] for item in ordered_events[:5])
    return {
        "positive_event_count": len(positive),
        "total_positive_return_pct": total,
        "top_event_share_pct": float(ordered_events[0]["open_to_checkpoint_return_pct"][return_key]) / total * 100.0,
        "top_5_events_share_pct": event_contribution / total * 100.0,
        "top_symbol": top_share("symbol", 1),
        "top_3_symbols": top_share("symbol", 3),
        "top_day": top_share("session", 1),
        "top_5_days": top_share("session", 5),
    }


def _monthly_stability(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for month in ("2026-04", "2026-05", "2026-06"):
        rows = [item for item in _meaningful(events) if item["session"].startswith(month)]
        result[month] = {
            "count": len(rows),
            "gap_up_count": sum(item["gap_direction"] == "UP" for item in rows),
            "gap_down_count": sum(item["gap_direction"] == "DOWN" for item in rows),
            "return_open_to_10_00": _aggregate_events(rows, "10:00"),
            "return_open_to_continuous_final": _aggregate_events(rows, "CONTINUOUS_FINAL"),
            "high_confidence_continuous_final": summarize_returns([
                _event_horizon(item, "CONTINUOUS_FINAL") for item in _clean_confidence_events(rows)
            ]),
        }
    return result


def _magnitude_analysis(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    buckets = ((0.0, 1.0, "<1%"), (1.0, 3.0, "1-3%"), (3.0, 5.0, "3-5%"), (5.0, math.inf, ">=5%"))
    groups: dict[str, list[dict[str, Any]]] = {}
    for lower, upper, label in buckets:
        groups[label] = [item for item in events if lower <= abs(item["gap_pct"]) < upper]
    eligible = [(name, _mean(_directional_value(item, _event_horizon(item, "10:00")) for item in rows))
                for name, rows in groups.items() if len(rows) >= 20]
    values = [value for _, value in eligible if value is not None]
    monotone_up = len(values) >= 3 and all(left <= right for left, right in pairwise(values))
    monotone_down = len(values) >= 3 and all(left >= right for left, right in pairwise(values))
    high = _clean_confidence_events(events)
    high_eligible = []
    for lower, upper, name in buckets:
        high_group = [item for item in high if lower <= abs(item["gap_pct"]) < upper]
        if len(high_group) >= 20:
            high_eligible.append((name, _mean(
                _directional_value(item, _event_horizon(item, "10:00")) for item in high_group
            )))
    high_vals = [value for _, value in high_eligible if value is not None]
    high_same_shape = (
        (monotone_up and len(high_vals) >= 3 and all(a <= b for a, b in pairwise(high_vals)))
        or (monotone_down and len(high_vals) >= 3 and all(a >= b for a, b in pairwise(high_vals)))
    )
    return {
        "direction_aligned_return": "gap-direction * open-to-10:00 return; positive means continuation, negative means reversal",
        "groups": {
            label: {"count": len(rows), "mean_direction_aligned_10m_pct": _mean(
                _directional_value(item, _event_horizon(item, "10:00")) for item in rows
            )}
            for label, rows in groups.items()
        },
        "broad_monotonic_shape": "STRONGER_CONTINUATION_AS_GAP_GROWS" if monotone_up else
                                 "STRONGER_REVERSAL_AS_GAP_GROWS" if monotone_down else "NO_STABLE_MONOTONE_SHAPE",
        "high_confidence_same_shape": high_same_shape,
        "stable_relationship": "YES" if high_same_shape and (monotone_up or monotone_down) else
                               "WEAK" if monotone_up or monotone_down else "NO",
    }


def _candidate_metric(
    events: Sequence[dict[str, Any]],
    minutes_by_symbol_session: dict[tuple[str, date], tuple[Bar, ...]],
    variant: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    for event in events:
        gap = float(event["gap_pct"])
        if (event["suspicious_extreme_gap"] or event["data_confidence"] != "HIGH_CONFIDENCE"
                or event["prior_close_source"] != "KIS_DAILY_CLOSE_RAW"):
            continue
        session = date.fromisoformat(event["session"])
        bars = minutes_by_symbol_session.get((event["symbol"], session), ())
        if variant == "GAP_A":
            checkpoint = "09:30"
            opening_range = event["opening_ranges"]["30M"]
            if not (gap >= MIN_MEANINGFUL_GAP_PCT and opening_range["complete"]
                    and opening_range["close"] > event["current_open"]
                    and opening_range["close"] > event["previous_close"]):
                continue
            signal = "gap-up >= 1%; completed first 30 minutes close above both 09:00 open and prior close"
        elif variant == "GAP_B":
            checkpoint = "10:00"
            first_hour = _window_bars(bars, session, 60)
            if not (gap <= -MIN_MEANINGFUL_GAP_PCT and _is_complete_window(first_hour, session, 60)
                    and first_hour[-1].close > event["current_open"]):
                continue
            signal = "gap-down <= -1%; completed first hour closes above 09:00 open"
        else:
            raise ValueError("variant must be GAP_A or GAP_B")
        boundary = datetime.combine(session, time.fromisoformat(checkpoint), KST)
        entry_bar = next_executable_open(bars, boundary)
        final_price = event["continuous_final_price"]
        if entry_bar is None or final_price is None or entry_bar.open <= 0:
            continue
        gross = (float(final_price) / entry_bar.open - 1.0) * 100.0
        trades.append({
            "symbol": event["symbol"], "market": event["market"], "session": event["session"],
            "gap_pct": gap, "signal": signal, "signal_completed_at": boundary.isoformat(),
            "entry_time": entry_bar.time.astimezone(KST).isoformat(), "entry_price": float(entry_bar.open),
            "exit_time": event["continuous_final_timestamp"], "exit_price": float(final_price),
            "gross_return_pct": gross, "data_confidence": event["data_confidence"],
        })
    gross_returns = [item["gross_return_pct"] for item in trades]
    net_1x = [item - ROUND_TRIP_COST_PCT for item in gross_returns]
    month_values: dict[str, list[float]] = defaultdict(list)
    symbol_values: dict[str, list[float]] = defaultdict(list)
    day_values: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        month_values[trade["session"][:7]].append(trade["gross_return_pct"])
        symbol_values[trade["symbol"]].append(trade["gross_return_pct"])
        day_values[trade["session"]].append(trade["gross_return_pct"])
    positive = [value for value in gross_returns if value > 0]
    positive_total = sum(positive)
    top_symbol_share = max((sum(max(0.0, value) for value in values) for values in symbol_values.values()), default=0.0) / positive_total * 100.0 if positive_total else None
    top_day_share = max((sum(max(0.0, value) for value in values) for values in day_values.values()), default=0.0) / positive_total * 100.0 if positive_total else None
    monthly_positive = sum(bool(values) and statistics.fmean(values) > 0 for values in month_values.values())
    gross_mean = _mean(gross_returns)
    net_mean = _mean(net_1x)
    pf = _profit_factor(net_1x)
    gate = {
        "events_at_least_30": len(trades) >= 30,
        "gross_expectancy_positive": gross_mean is not None and gross_mean > 0,
        "net_expectancy_positive_at_1x_assumed_costs": net_mean is not None and net_mean > 0,
        "net_profit_factor_above_1": pf == "INF" or (isinstance(pf, (float, int)) and pf > 1.0),
        "high_confidence_subset_same_direction": gross_mean is not None and gross_mean > 0,
        "monthly_direction_stable_at_least_2_of_3": monthly_positive >= 2,
        "top_symbol_positive_contribution_share_le_40pct": top_symbol_share is not None and top_symbol_share <= 40.0,
        "top_day_positive_contribution_share_le_20pct": top_day_share is not None and top_day_share <= 20.0,
    }
    enough = len(trades) >= 30
    if not enough or gross_mean is None or gross_mean <= 0:
        status = "NOT_CREATED"
    elif all(gate.values()):
        status = "RESEARCH_CANDIDATE"
    else:
        status = "REJECTED"
    summary = {
        "artifact": f"{variant.lower().replace('_', '-')}-development",
        "status": status,
        "candidate_rule": signal if trades else (
            "GAP-A: gap-up >= 1%; completed first 30m close above open and prior close; next 09:30 bar open; continuous-session final exit"
            if variant == "GAP_A" else
            "GAP-B: gap-down <= -1%; completed first hour closes above open; next 10:00 bar open; continuous-session final exit"
        ),
        "entry_timing": "next executable one-minute bar open after completed signal interval; no same-bar fill",
        "exit": "last available continuous-session minute close; not optimized",
        "cost_assumptions": {"fee_per_side_pct": FEE_PER_SIDE * 100.0, "sell_tax_pct": SELL_TAX * 100.0,
                             "slippage_bps_per_side": 15.0, "round_trip_pct": ROUND_TRIP_COST_PCT,
                             "label": "ASSUMED"},
        "events": len(trades),
        "gross": summarize_returns(gross_returns),
        "net_at_1x_assumed_costs": summarize_returns(net_1x),
        "net_cost_stress_mean_pct": {
            f"{multiplier:.1f}x": _mean(value - ROUND_TRIP_COST_PCT * multiplier for value in gross_returns)
            for multiplier in STRESS_MULTIPLIERS
        },
        "gross_to_round_trip_cost_ratio": gross_mean / ROUND_TRIP_COST_PCT if gross_mean is not None and ROUND_TRIP_COST_PCT else None,
        "break_even_round_trip_friction_pct": gross_mean,
        "monthly_gross_means_pct": {key: _mean(values) for key, values in sorted(month_values.items())},
        "concentration": {"top_symbol_positive_contribution_share_pct": top_symbol_share,
                          "top_day_positive_contribution_share_pct": top_day_share},
        "development_gate": gate,
    }
    return summary, trades


def _candidate_diagnostics(
    events: Sequence[dict[str, Any]],
    minutes_by_symbol_session: dict[tuple[str, date], tuple[Bar, ...]],
) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
    return {
        variant: _candidate_metric(events, minutes_by_symbol_session, variant)
        for variant in ("GAP_A", "GAP_B")
    }


def _candidate_status(summary: dict[str, Any]) -> bool:
    return summary.get("status") == "RESEARCH_CANDIDATE"


def _cost_stress(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in ("10:00", "11:00", "14:00", "CONTINUOUS_FINAL"):
        rows = _meaningful(events)
        gross = [_event_horizon(item, horizon) for item in rows]
        mean = _mean(gross)
        clean_rows = _clean_confidence_events(rows)
        clean_values = [_event_horizon(item, horizon) for item in clean_rows]
        clean_mean = _mean(clean_values)
        result[horizon] = {
            "descriptive_event_count": len(_values(gross)),
            "suspicious_extreme_gap_count": sum(item["suspicious_extreme_gap"] for item in rows),
            "gross_mean_pct": mean,
            "cost_multipliers": {
                f"{multiplier:.1f}x": {
                    "assumed_round_trip_cost_pct": ROUND_TRIP_COST_PCT * multiplier,
                    "mean_net_pct": mean - ROUND_TRIP_COST_PCT * multiplier if mean is not None else None,
                }
                for multiplier in STRESS_MULTIPLIERS
            },
            "gross_to_1x_round_trip_cost_ratio": mean / ROUND_TRIP_COST_PCT if mean is not None else None,
            "high_confidence_non_suspicious_daily_close": {
                "count": len(_values(clean_values)),
                "gross_mean_pct": clean_mean,
                "net_mean_pct": clean_mean - ROUND_TRIP_COST_PCT if clean_mean is not None else None,
                "gross_to_1x_round_trip_cost_ratio": clean_mean / ROUND_TRIP_COST_PCT if clean_mean is not None else None,
            },
            "interpretation": "descriptive mark-to-horizon only, not a strategy backtest",
        }
    return {
        "assumptions": {"broker_fee_per_side_pct": FEE_PER_SIDE * 100.0,
                        "sell_tax_pct": SELL_TAX * 100.0,
                        "slippage_bps_per_side": 15.0,
                        "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
                        "status": "ASSUMED"},
        "horizons": result,
    }


def _swing_summary(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    rows = _meaningful(events)
    highconfidence_clean = _clean_confidence_events(rows)
    result: dict[str, Any] = {}
    for label in ("NEXT_SESSION_CLOSE", "THREE_SESSION_CLOSE"):
        clean_rows = [item for item in rows if item.get("longer_horizon_status", {}).get(label) == "CLEAN_RAW_DAILY_HORIZON"]
        clean_confidence = [item for item in highconfidence_clean
                            if item.get("longer_horizon_status", {}).get(label) == "CLEAN_RAW_DAILY_HORIZON"]
        values = [item["longer_horizon_daily_close_return_pct"].get(label) for item in clean_rows]
        high = [item["longer_horizon_daily_close_return_pct"].get(label)
                for item in clean_confidence]
        result[label] = {
            "eligible_event_count": len(rows),
            "descriptive_outcome_count": len(_values(values)),
            "suspicious_opening_gap_excluded_count": sum(
                item.get("longer_horizon_status", {}).get(label) == "SUSPICIOUS_OPENING_GAP_OR_CORPORATE_ACTION"
                for item in rows
            ),
            "suspicious_raw_price_jump_excluded_count": sum(
                item.get("longer_horizon_status", {}).get(label) == "SUSPICIOUS_RAW_PRICE_JUMP_DURING_HORIZON"
                for item in rows
            ),
            "gross": summarize_returns(values),
            "high_confidence_gross": summarize_returns(high),
            "net_at_1x_assumed_costs_mean_pct": _mean(values) - ROUND_TRIP_COST_PCT if _mean(values) is not None else None,
            "overlap_note": "event outcomes overlap and are not independent trades",
        }
    one_day = result["NEXT_SESSION_CLOSE"]["gross"]["mean_pct"]
    three_day = result["THREE_SESSION_CLOSE"]["gross"]["mean_pct"]
    high_one = result["NEXT_SESSION_CLOSE"]["high_confidence_gross"]["mean_pct"]
    suggests = (one_day is not None and one_day > ROUND_TRIP_COST_PCT and high_one is not None and high_one > 0)
    weak = any(value is not None and value > 0 for value in (one_day, three_day))
    return {
        "horizons": result,
        "classification": "CANDIDATE_FOR_SEPARATE_PHASE" if suggests else "DESCRIPTIVE_ONLY" if weak else "NONE",
        "not_merged_into_intraday_candidate": True,
    }


def _answer_questions(
    events: Sequence[dict[str, Any]],
    candidates: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
    matched: dict[str, Any],
    magnitude: dict[str, Any],
    cost_stress: dict[str, Any],
    swing: dict[str, Any],
    secondary_status: str,
) -> dict[str, Any]:
    meaningful = _meaningful(events)
    up_60 = _mean(_event_horizon(item, "10:00") for item in meaningful if item["gap_direction"] == "UP")
    down_60 = _mean(_event_horizon(item, "10:00") for item in meaningful if item["gap_direction"] == "DOWN")
    epsilon = 0.05
    up_direction = "CONTINUATION" if up_60 is not None and up_60 > epsilon else (
        "REVERSAL" if up_60 is not None and up_60 < -epsilon else "NEITHER"
    )
    down_direction = "CONTINUATION" if down_60 is not None and down_60 < -epsilon else (
        "REVERSAL" if down_60 is not None and down_60 > epsilon else "NEITHER"
    )
    if up_direction == down_direction and up_direction in {"CONTINUATION", "REVERSAL"}:
        q1 = up_direction
    elif {up_direction, down_direction} == {"CONTINUATION", "REVERSAL"}:
        q1 = "MIXED"
    elif up_direction == "NEITHER" and down_direction == "NEITHER":
        q1 = "NEITHER"
    else:
        q1 = "MIXED"

    def after_first_hour(item: dict[str, Any]) -> float | None:
        return item["forward_next_bar_open_to_continuous_final_pct"].get("10:00")
    accepted = [item for item in meaningful if item["first_hour_state"] in {"GAP_HELD", "GAP_EXTENDED"}
                and after_first_hour(item) is not None]
    unaccepted = [item for item in meaningful if item["first_hour_state"] in {"GAP_PARTIALLY_FILLED", "GAP_FULLY_FILLED", "GAP_REVERSED"}
                  and after_first_hour(item) is not None]
    acceptance_aligned = [_directional_value(item, after_first_hour(item)) for item in accepted]
    unaccepted_aligned = [_directional_value(item, after_first_hour(item)) for item in unaccepted]
    acceptance_delta = (
        _mean(acceptance_aligned) - _mean(unaccepted_aligned)
        if _mean(acceptance_aligned) is not None and _mean(unaccepted_aligned) is not None else None
    )
    high_accepted = _clean_confidence_events(accepted)
    q3 = "YES" if (
        len(accepted) >= 30 and _mean(acceptance_aligned) is not None
        and _mean(acceptance_aligned) >= ROUND_TRIP_COST_PCT and acceptance_delta is not None
        and acceptance_delta >= ROUND_TRIP_COST_PCT
        and (_mean(_directional_value(item, after_first_hour(item)) for item in high_accepted) or -math.inf) > 0
    ) else "INSUFFICIENT" if len(accepted) < 30 else "NO"

    def after_30(item: dict[str, Any]) -> float | None:
        return item["forward_next_bar_open_to_continuous_final_pct"].get("09:30")
    filled = [item for item in meaningful if item["gap_fill"]["30M"] is True and after_30(item) is not None]
    not_filled = [item for item in meaningful if item["gap_fill"]["30M"] is False and after_30(item) is not None]
    fill_fade = [-float(_directional_value(item, after_30(item))) for item in filled]
    no_fill_fade = [-float(_directional_value(item, after_30(item))) for item in not_filled]
    fill_delta = _mean(fill_fade) - _mean(no_fill_fade) if _mean(fill_fade) is not None and _mean(no_fill_fade) is not None else None
    high_filled = _clean_confidence_events(filled)
    q4 = "YES" if (
        len(filled) >= 30 and _mean(fill_fade) is not None and _mean(fill_fade) >= ROUND_TRIP_COST_PCT
        and fill_delta is not None and fill_delta >= ROUND_TRIP_COST_PCT
        and (_mean(-float(_directional_value(item, after_30(item))) for item in high_filled) or -math.inf) > 0
    ) else "INSUFFICIENT" if len(filled) < 30 else "NO"

    buckets = _gap_bucket_analysis(events)["buckets"].values()
    credible_gross = []
    for group in buckets:
        if group["count"] < 30 or group["high_confidence_non_suspicious_daily_close_count"] < 30:
            continue
        for horizon in ("10:00", "11:00", "14:00", "CONTINUOUS_FINAL"):
            all_mean = group["outcomes"][horizon]["mean_pct"]
            if all_mean is not None and all_mean > ROUND_TRIP_COST_PCT:
                credible_gross.append(all_mean)
    q5 = "YES" if credible_gross else "NO"
    high = _clean_confidence_events(meaningful)
    all_directional = _mean(_directional_value(item, _event_horizon(item, "10:00")) for item in meaningful)
    high_directional = _mean(_directional_value(item, _event_horizon(item, "10:00")) for item in high)
    q6 = "INSUFFICIENT" if len(high) < 30 else "YES" if all_directional is not None and high_directional is not None and all_directional * high_directional > 0 else "NO"
    q7 = "YES" if any(_candidate_status(summary) for summary, _ in candidates.values()) else "NO"
    family = "CONTINUE" if q7 == "YES" else "INSUFFICIENT" if len(meaningful) < 30 else "REJECT"
    anatomy = "PASS" if q7 == "YES" else "INSUFFICIENT" if len(meaningful) < 30 else (
        "WEAK" if q5 == "YES" or magnitude["stable_relationship"] == "WEAK" else "FAIL"
    )
    return {
        "Q1_opening_gap_direction": q1,
        "Q1_gap_up_mean_open_to_10_00_pct": up_60,
        "Q1_gap_down_mean_open_to_10_00_pct": down_60,
        "Q2_gap_magnitude_stable_relationship": magnitude["stable_relationship"],
        "Q3_first_hour_acceptance_materially_improves_future_return": q3,
        "Q3_acceptance_count": len(accepted),
        "Q3_accepted_vs_unaccepted_gap_direction_aligned_delta_pct": acceptance_delta,
        "Q4_early_30m_gap_fill_predicts_subsequent_reversal": q4,
        "Q4_filled_count": len(filled),
        "Q4_fill_vs_no_fill_fade_aligned_delta_pct": fill_delta,
        "Q5_gross_move_large_enough_relative_to_costs": q5,
        "Q5_round_trip_cost_pct_assumed": ROUND_TRIP_COST_PCT,
        "Q6_result_survives_high_confidence_non_suspicious_subset": q6,
        "Q7_gap_a_or_gap_b_qualifies_on_development": q7,
        "Q8_secondary_preserves_candidate": "YES" if secondary_status == "PASS" else "NO" if secondary_status == "FAIL" else "NOT_RUN",
        "Q9_external_validation_passes": "NOT_AVAILABLE",
        "Q10_100k_execution_feasible": "NOT_RUN",
        "Q11_separate_1_3_day_swing_study_evidence": swing["classification"],
        "Q12_future_shadow_candidate_ready": "NO",
        "opening_gap_anatomy": anatomy,
        "opening_gap_family": family,
        "gap_a_status": candidates["GAP_A"][0]["status"],
        "gap_b_status": candidates["GAP_B"][0]["status"],
        "secondary_status": secondary_status,
        "matched_control_delta_10_00_pct_points": matched["mean_event_minus_control_pct_points"],
        "interpretation": "development anatomy only; no strategy, Secondary, external, Holdout, or Shadow promotion unless its explicit gate passes",
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _write_jsonl_gz(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
        for row in rows:
            compressed.write((json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode())
    temporary.replace(path)


def _phase6_reconciliation(
    *, phase6_dir: Path = Path("runtime/research/phase6"), output_path: Path | None = None
) -> dict[str, Any]:
    index_path = phase6_dir / "phase6-artifact-index.json"
    manifest_path = phase6_dir / "phase6-acquisition-manifest.json"
    summary_path = phase6_dir / "phase6-summary.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entry = next((item for item in index.get("files", [])
                  if item.get("file") == "phase6-acquisition-manifest.json"), None)
    if not entry:
        raise ValueError("Phase 6 artifact index lacks its historical acquisition-manifest entry")
    indexed_hash = str(entry["sha256"])
    indexed_bytes = int(entry["bytes"])
    current_hash = sha256_file(manifest_path) if manifest_path.is_file() else None
    current_bytes = manifest_path.stat().st_size if manifest_path.is_file() else None
    candidates = [path for path in Path("runtime").rglob("*phase6*acquisition*manifest*")
                  if path.is_file() and path != manifest_path]
    matching_candidates = [str(path) for path in candidates if sha256_file(path) == indexed_hash]
    summary_entry = next((item for item in index.get("files", [])
                          if item.get("file") == "phase6-summary.json"), None)
    if not summary_entry:
        raise ValueError("Phase 6 artifact index lacks its historical summary entry")
    indexed_summary_hash = str(summary_entry["sha256"])
    summary_hash_before = sha256_file(summary_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    payload = {
        "schema_version": 1,
        "artifact": "phase6-integrity-reconciliation",
        "created_at": datetime.now(KST).isoformat(),
        "phase6_artifact_integrity": "DEGRADED_RECONCILED" if current_hash != indexed_hash else "INTACT",
        "indexed_manifest": {"path": str(manifest_path), "sha256": indexed_hash, "bytes": indexed_bytes},
        "current_manifest": {"path": str(manifest_path), "sha256": current_hash, "bytes": current_bytes,
                             "matches_index": current_hash == indexed_hash},
        "original_manifest_bytes_available": current_hash == indexed_hash or bool(matching_candidates),
        "alternate_copy_candidates_found": [str(path) for path in candidates],
        "copies_matching_original_indexed_hash": matching_candidates,
        "exact_restoration_claimed": False,
        "mutation_source": {
            "phase": 7,
            "chain": [
                "Phase 7 safe-period acquisition invocation (exact command not recorded in the repository)",
                "src/krx_trader/research/phase6.py run_phase6_acquisition",
                "src/krx_trader/research/phase5_backfill.py run_phase5_backfill",
            ],
            "output_path_used": "runtime/research/phase6/phase6-acquisition-manifest.json",
            "cause": "Phase 7 safe-period acquisition reused the Phase 6 collector without redirecting its status output; the exact top-level invocation is not preserved in tracked source.",
        },
        "phase6_summary": {"sha256_indexed": indexed_summary_hash,
                            "sha256_current": summary_hash_before,
                            "matches_artifact_index": summary_hash_before == indexed_summary_hash,
                            "verdicts": summary.get("verdicts", {}),
                            "summary_and_verdict_unchanged": summary_hash_before == indexed_summary_hash},
        "phase6_artifact_index": {"sha256": sha256_file(index_path), "rewritten": False},
        "scope": "reconciliation record only; original failed/superseded evidence was not edited",
    }
    if output_path is not None:
        _write_json(output_path, payload)
    summary_hash_after = sha256_file(summary_path)
    if summary_hash_after != summary_hash_before or sha256_file(index_path) != payload["phase6_artifact_index"]["sha256"]:
        raise RuntimeError("Phase 6 historical summary or artifact index changed during reconciliation")
    return payload


def run_phase8_acquisition(
    *,
    cohort_manifest_path: Path = COHORT_PATH,
    split_manifest_path: Path = SPLIT_PATH,
    cache_root: Path = Path("data"),
    status_output_path: Path = ACQUISITION_PATH,
    max_new_requests: int = 6,
    min_request_interval: float = 4.0,
    client: HistoricalBarsClient | None = None,
) -> dict[str, Any]:
    """Resume only the safe KIS cache and route every acquisition status write into Phase 8."""
    if max_new_requests < 0:
        raise ValueError("max_new_requests cannot be negative")
    if min_request_interval < 4.0:
        raise ValueError("Phase 8 will not reduce the observed shared 4-second request interval")
    if client is None:
        settings = Settings.from_env()
        if settings.trading_mode != "shadow" or settings.live_trading_enabled:
            raise ValueError("Phase 8 historical data requires shadow mode with live trading disabled")
        if not settings.kis_app_key or not settings.kis_app_secret:
            raise ValueError("KIS market-data credentials are unavailable")
        transport = UrllibTransport()
        token_manager = TokenManager(
            settings.kis_app_key, settings.kis_app_secret, transport, cache_path=None
        )
        client = KisRestClient(
            settings.kis_app_key, settings.kis_app_secret, token_manager, transport,
            min_request_interval=min_request_interval,
        )
    state = run_phase5_backfill(
        cohort_manifest_path=cohort_manifest_path,
        split_manifest_path=split_manifest_path,
        cache_root=cache_root,
        status_output_path=status_output_path,
        target_cohort="cohort_60",
        min_request_interval=min_request_interval,
        client=client,
        max_new_requests=max_new_requests,
        artifact_name="phase8-acquisition-manifest",
    )
    state.update({
        "artifact": "phase8-acquisition-manifest",
        "git_sha": current_git_sha(),
        "provider": "KIS official read-only historical OHLCV",
        "period": {"start": SAFE_START.isoformat(), "end": SAFE_END.isoformat(), "sessions": int(state["total_sessions"])},
        "timestamp_convention": "Asia/Seoul; minute timestamp is bar start; closing auction excluded",
        "daily_price_parameter": "FID_ORG_ADJ_PRC=1 (raw/original daily prices, not adjusted)",
        "daily_minute_price_source_note": "current adapter uses KIS daily and minute endpoints; historical cache sidecars do not record the adjustment parameter",
        "observed_persistent_rate_limit_interval_seconds": 4.0,
        "configured_minimum_request_interval_seconds": min_request_interval,
        "bounded_new_request_budget": max_new_requests,
        "holdout_partitions_opened": 0,
        "external_partitions_opened": 0,
        "runtime_write_scope": "runtime/research/phase8/phase8-acquisition-manifest.json only, plus safe cache partitions and shared KIS limiter state",
    })
    _write_json(status_output_path, state)
    return state


def _strategy_config_hash() -> str:
    return canonical_sha256({
        "gap_anchor": "prior_safe_daily_close_vs_09:00_minute_open",
        "minimum_gap_pct": MIN_MEANINGFUL_GAP_PCT,
        "gap_a": "gap-up >= 1%; first 30m close > open and prior close; next 09:30 minute open; continuous final exit",
        "gap_b": "gap-down <= -1%; first hour close > open; next 10:00 minute open; continuous final exit",
        "corporate_action_policy": f"flag absolute gap >= {EXTREME_GAP_THRESHOLD_PCT}%; exclude from candidates and high-confidence-clean sensitivity",
        "cost_assumptions": {"broker_fee_per_side": FEE_PER_SIDE, "sell_tax": SELL_TAX,
                             "slippage_per_side": SLIPPAGE_PER_SIDE},
    })


def _candidate_hypothesis(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate": "GAP-A" if summary["artifact"].startswith("gap-a") else "GAP-B",
        "status": summary["status"],
        "rule": summary["candidate_rule"],
        "development_events": summary["events"],
        "development_gross_mean_pct": summary["gross"]["mean_pct"],
        "development_net_mean_pct": summary["net_at_1x_assumed_costs"]["mean_pct"],
        "development_pf": summary["net_at_1x_assumed_costs"]["profit_factor_gross"],
        "development_gate": summary["development_gate"],
        "secondary_status": "NOT_RUN_NO_DEVELOPMENT_SURVIVOR",
        "external_status": "NOT_AVAILABLE_NO_SECONDARY_SURVIVOR",
        "shadow_next_session": "NO",
    }


def _not_run(artifact: str, reason: str, *, opened: int = 0) -> dict[str, Any]:
    return {"artifact": artifact, "status": "NOT_RUN", "reason": reason,
            "partitions_opened": opened, "holdout_partitions_opened": 0,
            "external_partitions_opened": 0}


def run_phase8_study(
    *,
    output_dir: Path = OUTPUT_DIR,
    cache_root: Path = Path("data"),
    cohort_manifest_path: Path = COHORT_PATH,
    split_manifest_path: Path = SPLIT_PATH,
    acquisition_manifest_path: Path = ACQUISITION_PATH,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    reconciliation = _phase6_reconciliation(output_path=output_dir / "phase6-integrity-reconciliation.json")
    dataset, acquisition = load_phase8_development_dataset(
        cohort_manifest_path=cohort_manifest_path,
        split_manifest_path=split_manifest_path,
        acquisition_manifest_path=acquisition_manifest_path,
        cache_root=cache_root,
    )
    events, exclusions = build_opening_gap_events(
        minutes_by_symbol_session=dataset.minutes_by_symbol_session,
        daily_by_symbol=dataset.daily_by_symbol,
        market_by_symbol=dataset.market_by_symbol,
    )
    _daily_return_horizons(events, dataset.daily_by_symbol, dataset.period_sessions)
    source_counts = Counter(item["prior_close_source"] for item in events)
    dataset.dq["opening_gap_reference_source_counts"] = dict(source_counts)
    dataset.dq["sessions_without_valid_open_or_prior_close"] = exclusions
    buckets = _gap_bucket_analysis(events)
    gap_fill = _gap_fill_analysis(events)
    opening_range = _opening_range_analysis(events)
    first_hour = _first_hour_state_analysis(events)
    market_split = _split_analysis(events, "market")
    price_split = _split_analysis(events, "market_price_bucket")
    liquidity_split = _split_analysis(events, "liquidity_bucket")
    monthly = _monthly_stability(events)
    matched = _matched_controls(events)
    magnitude = _magnitude_analysis(_meaningful(events))
    price_source_split = _prior_close_source_analysis(events)
    cost_stress = _cost_stress(events)
    swing = _swing_summary(events)

    # Complete Development anatomy before evaluating either of the two fixed candidate examples.
    candidates = _candidate_diagnostics(events, dataset.minutes_by_symbol_session)
    candidate_rows = {variant: value[0] for variant, value in candidates.items()}
    qualified_dev = [key for key, value in candidate_rows.items() if _candidate_status(value)]
    if qualified_dev:
        # Secondary data remains sealed until the exact Development rule has passed its full gate.
        secondary_dataset, _ = load_phase8_period_dataset(
            period_name="secondary", cohort_manifest_path=cohort_manifest_path,
            split_manifest_path=split_manifest_path,
            acquisition_manifest_path=acquisition_manifest_path, cache_root=cache_root,
        )
        assert_phase8_secondary_allowed(
            development_candidate=True, exact_rule_frozen=True, sessions=secondary_dataset.period_sessions
        )
        secondary_events, secondary_exclusions = build_opening_gap_events(
            minutes_by_symbol_session=secondary_dataset.minutes_by_symbol_session,
            daily_by_symbol=secondary_dataset.daily_by_symbol,
            market_by_symbol=secondary_dataset.market_by_symbol,
            period_name="secondary",
        )
        secondary_results: dict[str, Any] = {}
        for variant in qualified_dev:
            summary, trades = _candidate_metric(
                secondary_events, secondary_dataset.minutes_by_symbol_session, variant
            )
            values = [row["gross_return_pct"] for row in trades]
            net = [value - ROUND_TRIP_COST_PCT for value in values]
            pf = _profit_factor(net)
            passed = len(trades) >= 30 and (_mean(values) or 0.0) > 0 and (_mean(net) or 0.0) > 0 and (
                pf == "INF" or (isinstance(pf, (int, float)) and pf > 1.0)
            )
            secondary_results[variant] = {
                "status": "PASS" if passed else "FAIL", "summary": summary,
                "events": len(trades), "mean_gross_pct": _mean(values),
                "mean_net_pct": _mean(net), "net_pf": pf,
            }
        secondary_status = "PASS" if all(item["status"] == "PASS" for item in secondary_results.values()) else "FAIL"
        secondary_artifact = {
            "artifact": "phase8-secondary-diagnostic", "status": secondary_status,
            "period": [SECONDARY_START.isoformat(), SAFE_END.isoformat()],
            "frozen_development_rules": qualified_dev,
            "results": secondary_results,
            "excluded": secondary_exclusions,
            "partitions_opened": len(secondary_dataset.partition_hashes),
            "holdout_partitions_opened": 0, "external_partitions_opened": 0,
        }
    else:
        secondary_status = "NOT_RUN"
        secondary_artifact = _not_run("phase8-secondary-diagnostic", "no Development survivor", opened=0)
    answers = _answer_questions(events, candidates, matched, magnitude, cost_stress, swing, secondary_status)
    concentrations = {
        horizon: _concentration(_meaningful(events), horizon)
        for horizon in ("10:00", "CONTINUOUS_FINAL")
    }
    anatomy = {
        "artifact": "opening-gap-anatomy",
        "status": answers["opening_gap_anatomy"],
        "primary_interval": "session open + completed 15-minute bars; 30-minute is not separately optimized",
        "valid_event_count": len(events),
        "meaningful_gap_event_count_abs_ge_1pct": len(_meaningful(events)),
        "high_confidence_count_abs_ge_1pct": len(_clean_confidence_events(_meaningful(events))),
        "excluded_sessions": exclusions,
        "gap_up_open_to_10_00_mean_pct": _mean(_event_horizon(item, "10:00") for item in _meaningful(events) if item["gap_direction"] == "UP"),
        "gap_down_open_to_10_00_mean_pct": _mean(_event_horizon(item, "10:00") for item in _meaningful(events) if item["gap_direction"] == "DOWN"),
        "outcomes_by_horizon": {horizon: _aggregate_events(_meaningful(events), horizon)
                                for horizon in (*CHECKPOINT_MINUTES.keys(), "CONTINUOUS_FINAL")},
        "matched_non_gap_control": matched,
        "prior_close_source_split": price_source_split,
        "magnitude_shape": magnitude,
        "high_confidence_sensitivity": {
            "all_valid": {horizon: _aggregate_events(_meaningful(events), horizon)
                          for horizon in ("10:00", "CONTINUOUS_FINAL")},
            "high_confidence_non_suspicious": {horizon: _aggregate_events(_clean_confidence_events(_meaningful(events)), horizon)
                                               for horizon in ("10:00", "CONTINUOUS_FINAL")},
        },
        "concentration_positive_event_contribution": concentrations,
        "cost_assumptions": {"round_trip_pct": ROUND_TRIP_COST_PCT, "label": "ASSUMED"},
        "cost_stress": cost_stress,
        "swing_descriptive": swing,
    }
    hypotheses = {
        "artifact": "gap-hypotheses",
        "maximum_variants": 2,
        "primary_event_anchor": "prior safe trading-session raw daily close vs same-session 09:00 raw minute open",
        "hypothesis_selection_after_development_anatomy": True,
        "GAP-A": _candidate_hypothesis(candidate_rows["GAP_A"]),
        "GAP-B": _candidate_hypothesis(candidate_rows["GAP_B"]),
        "breakout_v3": "NOT_USED",
        "relative_strength": "NOT_USED",
        "vwap_reclaim": "NOT_USED",
        "mean_reversion": "NOT_USED",
    }
    preregistration_path = output_dir / "phase8-preregistration.json"
    if preregistration_path.exists():
        # Do not erase an earlier Phase 8 preregistration; its identity is immutable.
        preregistration_status = "EXISTING_PRESERVED"
    else:
        preregistration_status = "NOT_CREATED_NO_SECONDARY_SURVIVOR"
    external = _not_run("external-validation", "no Secondary-surviving preregistered candidate; external block remains unopened")
    feasibility = _not_run("100k-feasibility", "no candidate survived Development and Secondary")
    dataset_manifest = {
        "artifact": "phase8-dataset-manifest", "source_git_sha": current_git_sha(),
        "research_code_sha256": sha256_file(Path(__file__)), "dataset_sha256": dataset.dataset_hash,
        "collector_code_sha256": sha256_file(Path(__file__).with_name("phase5_backfill.py")),
        "partition_index_sha256": canonical_sha256(dataset.partition_hashes),
        "cohort_sha256": dataset.cohort_hash, "development_split_sha256": dataset.period_split_hash,
        "strategy_config_sha256": _strategy_config_hash(),
        "provider": "KIS official read-only historical OHLCV",
        "primary_period": {"start": SAFE_START.isoformat(), "end": DEVELOPMENT_END.isoformat(),
                           "sessions": len(dataset.period_sessions)},
        "acquisition_period": {"start": SAFE_START.isoformat(), "end": SAFE_END.isoformat(),
                               "sessions": acquisition.get("total_sessions", 0)},
        "price_semantics": {
            "daily": "current KIS adapter requests FID_ORG_ADJ_PRC=1, documented as unadjusted raw/original prices",
            "minute": "KIS regular-session minute OHLCV, Asia/Seoul bar_start; close auction excluded",
            "cache_limitation": "existing daily sidecars do not persist FID_ORG_ADJ_PRC; semantics are derived from the adapter source",
            "prior_close_selection": "latest daily close within safe Development when at least as recent as available prior minute data; otherwise prior safe session last minute close, with source stored on every event",
            "daily_vs_minute_limit": "daily close may include the closing auction while minute source ends at 15:19; sources are reported separately and only exact daily-close events enter the high-confidence candidate sensitivity",
        },
        "corporate_action_treatment": "no action calendar in available cache; flag absolute gap >= 20 percent as suspicious; never candidate eligible",
        "cost_assumptions": {"fee_per_side_pct": FEE_PER_SIDE * 100,
                             "sell_tax_pct": SELL_TAX * 100,
                             "slippage_bps_per_side": 15,
                             "round_trip_pct": ROUND_TRIP_COST_PCT,
                             "status": "ASSUMED"},
        "external_block": {"start": EXTERNAL_START.isoformat(), "end": EXTERNAL_END.isoformat(), "partitions_opened": 0},
        "holdout": {"start": LOCKED_HOLDOUT_START.isoformat(), "end": "2026-08-28", "partitions_opened": 0,
                    "state": "LOCKED_NOT_EVALUATED"},
        "secondary_analysis_partitions_opened": 0 if secondary_status == "NOT_RUN" else secondary_artifact["partitions_opened"],
        "acquisition_manifest_sha256": (
            sha256_file(acquisition_manifest_path) if acquisition_manifest_path.is_file() else None
        ),
    }
    summary = {
        "schema_version": 1, "artifact": "phase8-summary", "created_at": datetime.now(KST).isoformat(),
        "phase8_status": "ANATOMY_COMPLETE_NO_STRATEGY" if not qualified_dev else "DEVELOPMENT_CANDIDATE_PENDING_SECONDARY",
        "git_sha": current_git_sha(), "dataset_sha256": dataset.dataset_hash,
        "phase6_artifact_integrity": reconciliation["phase6_artifact_integrity"],
        "opening_gap_anatomy": answers["opening_gap_anatomy"],
        "opening_gap_family": answers["opening_gap_family"],
        "gap_a": candidate_rows["GAP_A"]["status"], "gap_b": candidate_rows["GAP_B"]["status"],
        "answers": answers,
        "cohort_coverage": dataset.dq["cohort"],
        "events": {"valid": len(events), "meaningful_abs_gap_ge_1pct": len(_meaningful(events)),
                   "excluded_sessions": exclusions},
        "secondary": secondary_artifact,
        "preregistration": preregistration_status,
        "external_validation": external,
        "100k_feasibility": feasibility,
        "holdout_partitions_opened": 0,
        "external_partitions_opened": 0,
        "shadow_next_session": "NO",
        "alpha": "UNPROVEN", "paper": "OUT_OF_SCOPE", "live": "DISABLED", "private_api": "DISABLED",
    }

    artifacts: dict[str, Any] = {
        "phase6-integrity-reconciliation.json": reconciliation,
        "phase8-dataset-manifest.json": dataset_manifest,
        "phase8-dq.json": dataset.dq,
        "opening-gap-anatomy.json": anatomy,
        "gap-buckets.json": buckets,
        "gap-fill-analysis.json": gap_fill,
        "opening-range-analysis.json": opening_range,
        "first-hour-state-analysis.json": first_hour,
        "market-split.json": market_split,
        "price-split.json": price_split,
        "price-source-split.json": price_source_split,
        "liquidity-split.json": liquidity_split,
        "monthly-stability.json": monthly,
        "gap-hypotheses.json": hypotheses,
        "gap-a-development.json": candidate_rows["GAP_A"],
        "gap-b-development.json": candidate_rows["GAP_B"],
        "secondary-diagnostic.json": secondary_artifact,
        "external-validation.json": external,
        "cost-stress.json": cost_stress,
        "100k-feasibility.json": feasibility,
        "swing-descriptive.json": swing,
        "phase8-summary.json": summary,
    }
    for filename, payload in artifacts.items():
        _write_json(output_dir / filename, payload)
    _write_jsonl_gz(output_dir / "opening-gap-events.jsonl.gz", events)
    index_files: dict[str, dict[str, Any]] = {}
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and path.name != "phase8-artifact-index.json":
            index_files[path.name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    artifact_index = {
        "artifact": "phase8-artifact-index", "created_at": datetime.now(KST).isoformat(),
        "source_git_sha": current_git_sha(), "dataset_sha256": dataset.dataset_hash,
        "cohort_sha256": dataset.cohort_hash, "strategy_config_sha256": _strategy_config_hash(),
        "files": index_files, "holdout_partitions_opened": 0, "external_partitions_opened": 0,
    }
    _write_json(output_dir / "phase8-artifact-index.json", artifact_index)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 8 opening gap / first-hour Development anatomy")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--cache", type=Path, default=Path("data"))
    parser.add_argument("--acquire", action="store_true", help="bounded resumable safe-period KIS acquisition (writes Phase 8 state only)")
    parser.add_argument("--max-new-requests", type=int, default=6)
    args = parser.parse_args()
    if args.acquire:
        run_phase8_acquisition(cache_root=args.cache, max_new_requests=args.max_new_requests)
    summary = run_phase8_study(output_dir=args.output, cache_root=args.cache)
    print(json.dumps({
        "phase8_status": summary["phase8_status"],
        "opening_gap_anatomy": summary["opening_gap_anatomy"],
        "opening_gap_family": summary["opening_gap_family"],
        "gap_a": summary["gap_a"], "gap_b": summary["gap_b"],
        "holdout_partitions_opened": summary["holdout_partitions_opened"],
    }, indent=2))


if __name__ == "__main__":
    main()

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
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from krx_trader.backtest.costs import CostModel
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.quality import KST, inspect_bars
from krx_trader.models import Bar
from krx_trader.research.phase5_backfill import assert_safe_research_date
from krx_trader.research.phase6 import SafeDataset, load_safe_dataset

SAFE_START = date(2026, 4, 17)
DEVELOPMENT_END = date(2026, 6, 30)
SECONDARY_START = date(2026, 7, 1)
SAFE_END = date(2026, 7, 27)
LOCKED_HOLDOUT_START = date(2026, 7, 28)
SESSION_OPEN = time(9, 0)
SESSION_CLOSE = time(15, 20)
EXPECTED_SESSION_MINUTES = 380
BASE_COST = CostModel(broker_fee_rate=0.00015, sell_tax_rate=0.0020, slippage_bps=15.0)
VWAP_SOURCE = "OHLCV_PROXY"
VWAP_FORMULA = "session_cumsum(((high+low+close)/3)*observed_volume)/session_cumsum(observed_volume)"
TIME_BUCKETS = (
    (time(9, 0), time(9, 30), "09:00-09:30"),
    (time(9, 30), time(10, 0), "09:30-10:00"),
    (time(10, 0), time(11, 0), "10:00-11:00"),
    (time(11, 0), time(12, 0), "11:00-12:00"),
    (time(12, 0), time(13, 0), "12:00-13:00"),
    (time(13, 0), time(14, 0), "13:00-14:00"),
    (time(14, 0), time(15, 0), "14:00-15:00"),
    (time(15, 0), time(15, 30), "15:00+"),
)
TIME_BUCKET_LABELS = tuple(item[2] for item in TIME_BUCKETS)


@dataclass(frozen=True, slots=True)
class VwapPoint:
    timestamp: datetime
    vwap: float | None
    cumulative_volume: int
    cumulative_proxy_turnover: float


@dataclass(frozen=True, slots=True)
class SessionQuality:
    symbol: str
    market: str
    session: str
    observed_minute_count: int
    expected_minute_count: int
    missing_slots: int
    volume_sum: int
    turnover_proxy_sum: float
    first_observed_timestamp: str | None
    last_observed_timestamp: str | None
    classification: str


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNAVAILABLE"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def representative_price(bar: Bar) -> float:
    """Typical price used only as a clearly labelled turnover proxy."""
    return (bar.high + bar.low + bar.close) / 3.0


def compute_session_vwap(bars: Sequence[Bar]) -> list[VwapPoint]:
    """Return causal session cumulative VWAP_PROXY points; never fills absent minutes."""
    if not bars:
        return []
    issues = inspect_bars(list(bars))
    if issues:
        raise ValueError("VWAP input contains invalid OHLCV bars")
    ordered = sorted(bars, key=lambda item: item.time.astimezone(KST))
    session_dates = {bar.time.astimezone(KST).date() for bar in ordered}
    if len(session_dates) != 1:
        raise ValueError("compute_session_vwap accepts one symbol/session at a time")
    cumulative_volume = 0
    cumulative_proxy_turnover = 0.0
    result: list[VwapPoint] = []
    previous_time: datetime | None = None
    for bar in ordered:
        local = bar.time.astimezone(KST)
        if not SESSION_OPEN <= local.time() < SESSION_CLOSE:
            continue
        if previous_time is not None and local <= previous_time:
            raise ValueError("VWAP input timestamps must be unique and increasing")
        previous_time = local
        if bar.volume > 0:
            cumulative_volume += bar.volume
            cumulative_proxy_turnover += representative_price(bar) * bar.volume
        value = cumulative_proxy_turnover / cumulative_volume if cumulative_volume else None
        result.append(VwapPoint(local, value, cumulative_volume, cumulative_proxy_turnover))
    return result


def compute_intraday_vwap(bars: Iterable[Bar]) -> dict[date, list[VwapPoint]]:
    """Group bars by KST session before cumulative calculation, resetting every day."""
    by_session: dict[date, list[Bar]] = defaultdict(list)
    for bar in bars:
        by_session[bar.time.astimezone(KST).date()].append(bar)
    return {
        session: compute_session_vwap(values)
        for session, values in sorted(by_session.items())
    }


def _session_classification(bars: Sequence[Bar]) -> tuple[int, int, str]:
    day = bars[0].time.astimezone(KST).date() if bars else None
    if day is None:
        return 0, EXPECTED_SESSION_MINUTES, "UNRELIABLE"
    minute_slots = {
        int((bar.time.astimezone(KST) - datetime.combine(day, SESSION_OPEN, KST)).total_seconds() // 60)
        for bar in bars
        if bar.time.astimezone(KST).date() == day
        and SESSION_OPEN <= bar.time.astimezone(KST).time() < SESSION_CLOSE
    }
    observed = len(minute_slots)
    missing = max(0, EXPECTED_SESSION_MINUTES - observed)
    ordered = sorted(bar.time.astimezone(KST) for bar in bars)
    endpoints_ok = bool(ordered) and ordered[0].time() == SESSION_OPEN and ordered[-1].time() == time(15, 19)
    volume_sum = sum(bar.volume for bar in bars)
    if observed == EXPECTED_SESSION_MINUTES and endpoints_ok and volume_sum > 0:
        classification = "HIGH_CONFIDENCE"
    elif observed >= 361 and volume_sum > 0:
        classification = "PARTIAL"
    else:
        classification = "UNRELIABLE"
    return observed, missing, classification


def _time_bucket(bar_close: datetime, interval_minutes: int) -> str:
    start = bar_close.astimezone(KST) - timedelta(minutes=interval_minutes)
    for left, right, name in TIME_BUCKETS:
        if left <= start.time() < right:
            return name
    return "OUT_OF_SESSION"


def _liquidity_bucket(turnover_proxy: float) -> str:
    if turnover_proxy < 10_000_000:
        return "LT_10M_KRW"
    if turnover_proxy < 50_000_000:
        return "10M_50M_KRW"
    if turnover_proxy < 200_000_000:
        return "50M_200M_KRW"
    return "GE_200M_KRW"


def _time_below_bucket(count: int) -> str:
    if count <= 0:
        return "0_BARS"
    if count == 1:
        return "1_BAR"
    if count == 2:
        return "2_BARS"
    if count <= 4:
        return "3_4_BARS"
    return "5_PLUS_BARS"


def _vwap_age_bucket(age_minutes: float) -> str:
    if age_minutes < 30:
        return "LT_30M"
    if age_minutes < 45:
        return "30_45M"
    if age_minutes < 60:
        return "45_60M"
    return "GE_60M"


def _excursion_bucket(pct: float | None) -> str:
    if pct is None:
        return "NO_PRIOR_BELOW_BAR"
    if pct > -0.25:
        return "SHALLOW_GT_0_25_PCT"
    if pct > -0.75:
        return "MODERATE_0_25_0_75_PCT"
    return "DEEP_GE_0_75_PCT"


def _session_direction(return_pct: float) -> str:
    if return_pct < -0.5:
        return "DOWN"
    if return_pct > 0.5:
        return "UP"
    return "NEAR_FLAT"


def build_interval_observations(
    bars_by_symbol: dict[str, Sequence[Bar]],
    *,
    market_by_symbol: dict[str, str],
    interval_minutes: int,
) -> list[dict[str, Any]]:
    """Create completed interval observations using only same-session observed minutes."""
    if interval_minutes not in {15, 30}:
        raise ValueError("Phase 7 intervals are 15m or 30m")
    rows: list[dict[str, Any]] = []
    for symbol in sorted(bars_by_symbol):
        market = market_by_symbol[symbol]
        sessions = compute_intraday_vwap(bars_by_symbol[symbol])
        by_date: dict[date, list[Bar]] = defaultdict(list)
        for bar in bars_by_symbol[symbol]:
            local = bar.time.astimezone(KST)
            if SESSION_OPEN <= local.time() < SESSION_CLOSE:
                by_date[local.date()].append(bar)
        for session, minute_bars in sorted(by_date.items()):
            vwap_points = sessions.get(session, [])
            point_by_time = {item.timestamp: item for item in vwap_points}
            session_open_price = minute_bars[0].open
            session_quality_observed, session_missing_slots, confidence = _session_classification(minute_bars)
            raw_buckets: dict[datetime, list[Bar]] = defaultdict(list)
            for bar in minute_bars:
                local = bar.time.astimezone(KST)
                offset = int((local - datetime.combine(session, SESSION_OPEN, KST)).total_seconds() // 60)
                raw_buckets[offset // interval_minutes].append(bar)
            completed: list[dict[str, Any]] = []
            bucket_count = EXPECTED_SESSION_MINUTES // interval_minutes
            for bucket_index in range(bucket_count):
                expected_start = datetime.combine(session, SESSION_OPEN, KST) + timedelta(
                    minutes=bucket_index * interval_minutes
                )
                expected_times = [expected_start + timedelta(minutes=i) for i in range(interval_minutes)]
                values = sorted(raw_buckets.get(bucket_index, []), key=lambda item: item.time)
                actual_times = [item.time.astimezone(KST) for item in values]
                if actual_times != expected_times:
                    continue
                close_time = expected_start + timedelta(minutes=interval_minutes)
                last_observed_minute = close_time - timedelta(minutes=1)
                point = point_by_time.get(last_observed_minute)
                if point is None or point.vwap is None or point.cumulative_volume <= 0:
                    continue
                interval_volume = sum(item.volume for item in values)
                interval_turnover_proxy = sum(representative_price(item) * item.volume for item in values)
                completed.append({
                    "bar": Bar(
                        close_time,
                        values[0].open,
                        max(item.high for item in values),
                        min(item.low for item in values),
                        values[-1].close,
                        interval_volume,
                    ),
                    "vwap": point.vwap,
                    "cumulative_volume": point.cumulative_volume,
                    "cumulative_proxy_turnover": point.cumulative_proxy_turnover,
                    "interval_turnover_proxy": interval_turnover_proxy,
                })
            reclaim_count = 0
            for index, item in enumerate(completed):
                bar: Bar = item["bar"]
                local_close = bar.time.astimezone(KST)
                vwap = float(item["vwap"])
                previous = completed[index - 1] if index else None
                previous_bar: Bar | None = previous["bar"] if previous else None
                previous_close_to_vwap = None
                if previous is not None and previous["vwap"]:
                    previous_close_to_vwap = (previous_bar.close / float(previous["vwap"]) - 1.0) * 100.0
                above_count = sum(1 for old in completed[:index + 1] if old["bar"].close > old["vwap"])
                below_count = sum(1 for old in completed[:index + 1] if old["bar"].close < old["vwap"])
                consecutive_below = 0
                excursion_pct: float | None = None
                sequence_gap_censored = False
                newer_time = local_close
                for old in reversed(completed[:index]):
                    old_bar: Bar = old["bar"]
                    if consecutive_below and newer_time - old_bar.time != timedelta(minutes=interval_minutes):
                        sequence_gap_censored = True
                        break
                    old_vwap = float(old["vwap"])
                    if old_bar.close >= old_vwap:
                        break
                    consecutive_below += 1
                    value = (old_bar.low / old_vwap - 1.0) * 100.0
                    excursion_pct = value if excursion_pct is None else min(excursion_pct, value)
                    newer_time = old_bar.time
                if index and local_close - completed[index - 1]["bar"].time != timedelta(minutes=interval_minutes):
                    sequence_gap_censored = True
                slope_60m = None
                target_prior = local_close - timedelta(minutes=60)
                prior_candidates = [old for old in completed[:index] if old["bar"].time == target_prior]
                if prior_candidates and float(prior_candidates[-1]["vwap"]) > 0:
                    slope_60m = (vwap / float(prior_candidates[-1]["vwap"]) - 1.0) * 100.0
                slope_state = (
                    "RISING" if slope_60m is not None and slope_60m > 0.05
                    else "FALLING" if slope_60m is not None and slope_60m < -0.05
                    else "FLAT" if slope_60m is not None
                    else "UNAVAILABLE"
                )
                trailing = completed[max(0, index - 3):index + 1]
                recent_high = max(old["bar"].high for old in trailing)
                recent_low = min(old["bar"].low for old in trailing)
                closes = [old["bar"].close for old in trailing]
                log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
                vol = statistics.pstdev(log_returns) * 100.0 if len(log_returns) > 1 else None
                prior_turnover = [float(old["interval_turnover_proxy"]) for old in completed[max(0, index - 8):index]]
                relative_turnover = (
                    float(item["interval_turnover_proxy"]) / statistics.mean(prior_turnover)
                    if prior_turnover and statistics.mean(prior_turnover) > 0 else None
                )
                prior_volume = [float(old["bar"].volume) for old in completed[max(0, index - 8):index]]
                relative_volume = (
                    bar.volume / statistics.mean(prior_volume)
                    if prior_volume and statistics.mean(prior_volume) > 0 else None
                )
                session_high = max(old["bar"].high for old in completed[:index + 1])
                session_low = min(old["bar"].low for old in completed[:index + 1])
                bar_start = local_close - timedelta(minutes=interval_minutes)
                down_cross = bool(
                    bar.volume > 0
                    and previous is not None
                    and previous_bar.close >= float(previous["vwap"])
                    and bar.close < vwap
                )
                up_reclaim = bool(
                    bar.volume > 0
                    and previous is not None
                    and previous_bar.close <= float(previous["vwap"])
                    and bar.close > vwap
                )
                prior_completed = completed[:index]
                prior_down_cross = any(
                    old_index > 0
                    and float(prior_completed[old_index - 1]["bar"].close) >= float(prior_completed[old_index - 1]["vwap"])
                    and float(old["bar"].close) < float(old["vwap"])
                    for old_index, old in enumerate(prior_completed)
                )
                early_high = max(
                    (old["bar"].high for old in prior_completed
                     if (old["bar"].time - timedelta(minutes=interval_minutes)).time() < time(10, 0)),
                    default=session_open_price,
                )
                early_strength_then_loss = (
                    up_reclaim and prior_down_cross and early_high / session_open_price - 1.0 >= 0.005
                )
                if up_reclaim:
                    reclaim_count += 1
                    reclaim_sequence = reclaim_count
                    reclaim_type = "FIRST_RECLAIM" if reclaim_count == 1 else (
                        "SECOND_RECLAIM" if reclaim_count == 2 else "THIRD_PLUS_RECLAIM"
                    )
                else:
                    reclaim_sequence = None
                    reclaim_type = None
                if slope_60m is None:
                    slope_state = "UNAVAILABLE"
                row = {
                    "symbol": symbol,
                    "market": market,
                    "session": session.isoformat(),
                    "timestamp": local_close.isoformat(),
                    "bar_start": bar_start.isoformat(),
                    "interval_minutes": interval_minutes,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "turnover_proxy_krw": float(item["interval_turnover_proxy"]),
                    "session_vwap_proxy": vwap,
                    "cumulative_volume": int(item["cumulative_volume"]),
                    "cumulative_turnover_proxy_krw": float(item["cumulative_proxy_turnover"]),
                    "body_return_pct": (bar.close / bar.open - 1.0) * 100.0,
                    "close_location_value": (bar.close - bar.low) / (bar.high - bar.low) if bar.high > bar.low else 0.5,
                    "lower_wick_pct": (min(bar.open, bar.close) - bar.low) / bar.open * 100.0,
                    "upper_wick_pct": (bar.high - max(bar.open, bar.close)) / bar.open * 100.0,
                    "bar_range_pct": (bar.high / bar.low - 1.0) * 100.0,
                    "close_to_vwap_pct": (bar.close / vwap - 1.0) * 100.0,
                    "high_to_vwap_pct": (bar.high / vwap - 1.0) * 100.0,
                    "low_to_vwap_pct": (bar.low / vwap - 1.0) * 100.0,
                    "previous_close_to_vwap_pct": previous_close_to_vwap,
                    "vwap_slope_60m_pct": slope_60m,
                    "vwap_slope_state": slope_state,
                    "bars_above_vwap": above_count,
                    "bars_below_vwap": below_count,
                    "consecutive_bars_below_vwap": consecutive_below,
                    "time_below_vwap_minutes": consecutive_below * interval_minutes,
                    "time_below_gap_censored": sequence_gap_censored,
                    "time_below_measure": "consecutive completed bars observed; gaps terminate the run",
                    "max_prior_excursion_below_vwap_pct": excursion_pct,
                    "time_below_bucket": _time_below_bucket(consecutive_below),
                    "excursion_bucket": _excursion_bucket(excursion_pct),
                    "session_return_pct": (bar.close / session_open_price - 1.0) * 100.0,
                    "session_direction": _session_direction((bar.close / session_open_price - 1.0) * 100.0),
                    "distance_from_session_high_pct": (bar.close / session_high - 1.0) * 100.0,
                    "distance_from_session_low_pct": (bar.close / session_low - 1.0) * 100.0,
                    "recent_range_4_pct": (recent_high / recent_low - 1.0) * 100.0,
                    "recent_volatility_4_pct": vol,
                    "relative_turnover_8": relative_turnover,
                    "relative_volume_8": relative_volume,
                    "liquidity_bucket": _liquidity_bucket(float(item["interval_turnover_proxy"])),
                    "time_of_day_bucket": _time_bucket(local_close, interval_minutes),
                    "vwap_age_minutes": (local_close - datetime.combine(session, SESSION_OPEN, KST)).total_seconds() / 60.0,
                    "vwap_age_bucket": _vwap_age_bucket((local_close - datetime.combine(session, SESSION_OPEN, KST)).total_seconds() / 60.0),
                    "data_confidence": confidence,
                    "session_observed_minutes": session_quality_observed,
                    "session_missing_slots": session_missing_slots,
                    "down_cross": down_cross,
                    "up_reclaim": up_reclaim,
                    "reclaim_sequence": reclaim_sequence,
                    "reclaim_type": reclaim_type,
                    "reclaim_event_id": f"{symbol}:{session.isoformat()}:{reclaim_sequence}" if up_reclaim else None,
                    "early_reclaim_context": (
                        "EARLY_STRENGTH_VWAP_LOSS_RECLAIM" if early_strength_then_loss
                        else "MORNING_WEAKNESS_FIRST_RECLAIM" if up_reclaim and reclaim_sequence == 1
                        and bar_start.time() < time(10, 30)
                        else "OTHER_RECLAIM_CONTEXT" if up_reclaim else None
                    ),
                }
                rows.append(row)
    rows.sort(key=lambda row: (row["session"], row["timestamp"], row["symbol"]))
    return rows


def attach_forward_outcomes(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach descriptive future outcomes in a separate pass; features stay point-in-time."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["symbol"], row["session"])].append(row)
    result: list[dict[str, Any]] = []
    for key in sorted(grouped):
        values = sorted(grouped[key], key=lambda row: row["timestamp"])
        for index, original in enumerate(values):
            row = dict(original)
            for horizon in (1, 2, 4, 8):
                future = values[index + 1:index + 1 + horizon]
                valid = len(future) == horizon
                prefix = f"forward_{horizon}bar"
                if not valid:
                    row[f"{prefix}_gross_return_pct"] = None
                    row[f"{prefix}_mfe_pct"] = None
                    row[f"{prefix}_mae_pct"] = None
                    row[f"{prefix}_time_to_mfe_bars"] = None
                    row[f"{prefix}_time_to_mae_bars"] = None
                    continue
                entry = float(future[0]["open"])
                highs = [float(item["high"]) for item in future]
                lows = [float(item["low"]) for item in future]
                last_close = float(future[-1]["close"])
                best_index = max(range(len(highs)), key=highs.__getitem__)
                worst_index = min(range(len(lows)), key=lows.__getitem__)
                row[f"{prefix}_gross_return_pct"] = (last_close / entry - 1.0) * 100.0
                if horizon == 4:
                    row["forward_4bar_entry_reference"] = entry
                    row["forward_4bar_exit_reference"] = last_close
                row[f"{prefix}_mfe_pct"] = (max(highs) / entry - 1.0) * 100.0
                row[f"{prefix}_mae_pct"] = (min(lows) / entry - 1.0) * 100.0
                row[f"{prefix}_time_to_mfe_bars"] = best_index + 1
                row[f"{prefix}_time_to_mae_bars"] = worst_index + 1
            for horizon in (1, 2, 4):
                future = values[index + 1:index + 1 + horizon]
                row[f"vwap_recross_within_{horizon}bar"] = (
                    any(float(item["close"]) < float(item["session_vwap_proxy"]) for item in future)
                    if len(future) == horizon else None
                )
            next_bar = values[index + 1] if index + 1 < len(values) else None
            next_two = values[index + 1:index + 3]
            row["acceptance_next_close_above_vwap"] = (
                bool(float(next_bar["close"]) > float(next_bar["session_vwap_proxy"])) if next_bar else None
            )
            row["acceptance_next_low_touch_hold"] = (
                bool(
                    float(next_bar["low"]) <= float(next_bar["session_vwap_proxy"])
                    and float(next_bar["close"]) > float(next_bar["session_vwap_proxy"])
                ) if next_bar else None
            )
            row["acceptance_next_close_above_reclaim"] = (
                bool(float(next_bar["close"]) > float(original["close"])) if next_bar else None
            )
            row["acceptance_two_consecutive_closes_above_vwap"] = (
                bool(all(float(item["close"]) > float(item["session_vwap_proxy"]) for item in next_two))
                if len(next_two) == 2 else None
            )
            for acceptance_name, confirmed_at in (("a", 1), ("d", 2)):
                confirmation_rows = values[index + 1:index + 1 + confirmed_at]
                confirmed = (
                    bool(confirmation_rows)
                    and all(float(item["close"]) > float(item["session_vwap_proxy"]) for item in confirmation_rows)
                )
                for horizon in (1, 2, 4):
                    post = values[index + 1 + confirmed_at:index + 1 + confirmed_at + horizon]
                    key_name = f"vwap_recross_after_acceptance_{acceptance_name}_within_{horizon}bar"
                    row[key_name] = (
                        any(float(item["close"]) < float(item["session_vwap_proxy"]) for item in post)
                        if confirmed and len(post) == horizon else None
                    )
                for horizon in (1, 2, 4):
                    post = values[index + 1 + confirmed_at:index + 1 + confirmed_at + horizon]
                    key_name = f"acceptance_{acceptance_name}_entry_{horizon}bar_gross_return_pct"
                    row[key_name] = (
                        (float(post[-1]["close"]) / float(post[0]["open"]) - 1.0) * 100.0
                        if confirmed and len(post) == horizon else None
                    )
            for confirmation_bars in (1, 2):
                post = values[index + 1 + confirmation_bars:index + 1 + confirmation_bars + 4]
                row[f"post_{confirmation_bars}bar_confirmation_entry_4bar_gross_return_pct"] = (
                    (float(post[-1]["close"]) / float(post[0]["open"]) - 1.0) * 100.0
                    if len(post) == 4 else None
                )
            result.append(row)
    return result


def _metric_summary(rows: Sequence[dict[str, Any]], *, event_only: bool = False) -> dict[str, Any]:
    eligible = [
        row for row in rows
        if (not event_only or row.get("up_reclaim"))
        and row.get("forward_4bar_gross_return_pct") is not None
    ]
    result: dict[str, Any] = {"count": len(eligible)}
    for horizon in (1, 2, 4, 8):
        key = f"forward_{horizon}bar_gross_return_pct"
        values = [float(row[key]) for row in eligible if row.get(key) is not None]
        result[f"forward_{horizon}bar"] = {
            "count": len(values),
            "mean_pct": statistics.mean(values) if values else None,
            "median_pct": statistics.median(values) if values else None,
            "positive_rate": sum(value > 0 for value in values) / len(values) if values else None,
        }
    for horizon in (1, 2, 4):
        values = [row[f"vwap_recross_within_{horizon}bar"] for row in eligible]
        values = [value for value in values if value is not None]
        result[f"failure_within_{horizon}bar_rate"] = (
            sum(bool(value) for value in values) / len(values) if values else None
        )
    return result


def _summarize_groups(
    rows: Sequence[dict[str, Any]],
    *,
    key: str,
    event_only: bool = True,
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if event_only and not row.get("up_reclaim"):
            continue
        value = row.get(key)
        if value is None:
            continue
        groups[str(value)].append(row)
    return {name: _metric_summary(values, event_only=event_only) for name, values in sorted(groups.items())}


def _matched_baseline(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    event_rows = [row for row in rows if row.get("up_reclaim") and row.get("forward_4bar_gross_return_pct") is not None]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("up_reclaim") or row.get("forward_4bar_gross_return_pct") is None:
            continue
        group = (row["market"], row["time_of_day_bucket"], row["liquidity_bucket"])
        groups[group].append(row)
    matched: list[tuple[dict[str, Any], float, int]] = []
    for event in event_rows:
        group = (event["market"], event["time_of_day_bucket"], event["liquidity_bucket"])
        controls = [
            row for row in groups.get(group, [])
            if (row["symbol"], row["session"]) != (event["symbol"], event["session"])
        ]
        if controls:
            matched.append((event, statistics.mean(float(row["forward_4bar_gross_return_pct"]) for row in controls), len(controls)))
    event_returns = [float(event["forward_4bar_gross_return_pct"]) for event, _, _ in matched]
    baseline_returns = [baseline for _, baseline, _ in matched]
    deltas = [float(event["forward_4bar_gross_return_pct"]) - baseline for event, baseline, _ in matched]
    return {
        "matching_rule": "same market, completed-bar time bucket, and fixed 15m turnover-proxy bucket; excludes the event symbol-session",
        "event_count_with_control": len(matched),
        "mean_control_bars_per_event": statistics.mean(n for _, _, n in matched) if matched else None,
        "event_mean_4bar_gross_pct": statistics.mean(event_returns) if event_returns else None,
        "matched_baseline_mean_4bar_gross_pct": statistics.mean(baseline_returns) if baseline_returns else None,
        "mean_event_minus_matched_baseline_pct": statistics.mean(deltas) if deltas else None,
        "positive_delta_event_share": sum(value > 0 for value in deltas) / len(deltas) if deltas else None,
        "independent_observations_note": "overlapping descriptive bar outcomes are not independent trades",
    }


def _acceptance_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    events = [row for row in rows if row.get("up_reclaim")]
    definitions = {
        "A_next_close_above_vwap": "acceptance_next_close_above_vwap",
        "B_next_low_touch_and_close_above": "acceptance_next_low_touch_hold",
        "C_next_close_above_reclaim_close": "acceptance_next_close_above_reclaim",
        "D_two_consecutive_closes_above_vwap": "acceptance_two_consecutive_closes_above_vwap",
    }
    output: dict[str, Any] = {}
    for name, field in definitions.items():
        accepted = [row for row in events if row.get(field) is True]
        outcome: dict[str, Any] = {"definition": field, "events": len(accepted), "event_share": len(accepted) / len(events) if events else None}
        delay = 2 if name.startswith("D_") else 1
        delayed_key = f"post_{delay}bar_confirmation_entry_4bar_gross_return_pct"
        delayed = [float(row[delayed_key]) for row in accepted if row.get(delayed_key) is not None]
        immediate = [float(row["forward_4bar_gross_return_pct"]) for row in accepted if row.get("forward_4bar_gross_return_pct") is not None]
        outcome["confirmation_delay_bars"] = delay
        outcome["immediate_event_entry_4bar_outcome_count"] = len(immediate)
        outcome["post_confirmation_4bar_outcome_count"] = len(delayed)
        outcome["immediate_event_entry_4bar_mean_gross_pct"] = statistics.mean(immediate) if immediate else None
        outcome["next_executable_bar_after_confirmation_4bar_mean_gross_pct"] = statistics.mean(delayed) if delayed else None
        outcome["confirmation_entry_mean_delta_vs_immediate_pct"] = (
            statistics.mean(delayed) - statistics.mean(immediate) if delayed and immediate else None
        )
        for horizon in (1, 2, 4):
            recross_key = f"vwap_recross_after_acceptance_{'d' if name.startswith('D_') else 'a'}_within_{horizon}bar"
            failures = [row[recross_key] for row in accepted if row.get(recross_key) is not None]
            outcome[f"post_confirmation_{horizon}bar_failure_rate"] = (
                sum(bool(value) for value in failures) / len(failures) if failures else None
            )
        output[name] = outcome
    unrestricted = [row for row in events if row.get("vwap_recross_within_4bar") is not None]
    total_failure_rate = (
        sum(bool(row["vwap_recross_within_4bar"]) for row in unrestricted) / len(unrestricted)
        if unrestricted else None
    )
    output["unrestricted_reclaim_failure_within_4bar_rate"] = total_failure_rate
    return output


def _strategy_gate(anatomy: dict[str, Any]) -> dict[str, Any]:
    event_count = anatomy["event_count_with_4bar_outcome"]
    gross = anatomy["reclaim_summary"]["forward_4bar"]["mean_pct"]
    baseline_delta = anatomy["matched_baseline"]["mean_event_minus_matched_baseline_pct"]
    high = anatomy["high_confidence_reclaim_summary"]["forward_4bar"]["mean_pct"]
    high_count = anatomy["high_confidence_reclaim_summary"]["count"]
    failure = anatomy["failure_rates"]["within_4bar"]
    accepted = anatomy["acceptance"]["A_next_close_above_vwap"].get("post_confirmation_4bar_failure_rate")
    criteria = {
        "event_sample_at_least_100": event_count >= 100,
        "positive_four_bar_gross_opportunity": gross is not None and gross > 0,
        "positive_matched_baseline_improvement": baseline_delta is not None and baseline_delta > 0,
        "high_confidence_same_positive_direction": high_count >= 10 and high is not None and high > 0,
        "acceptance_reduces_four_bar_failure_by_10pp": (
            failure is not None and accepted is not None and failure - accepted >= 0.10
        ),
    }
    passed = sum(criteria.values())
    status = "PASS" if all(criteria.values()) else "WEAK" if passed >= 3 else "FAIL"
    if event_count < 100:
        status = "INSUFFICIENT"
    return {"status": status, "criteria": criteria, "criteria_passed": passed, "criteria_total": len(criteria),
            "strategy_variants_created": 0 if status != "PASS" else "ANATOMY_SUPPORT_REQUIRES_EXPLICIT_VARIANT_DESIGN"}


def analyze_development(rows: Sequence[dict[str, Any]], *, interval_minutes: int) -> dict[str, Any]:
    for row in rows:
        row["session_month"] = row["session"][:7]
    events = [row for row in rows if row.get("up_reclaim")]
    evaluated = [row for row in events if row.get("forward_4bar_gross_return_pct") is not None]
    high = [row for row in evaluated if row["data_confidence"] == "HIGH_CONFIDENCE"]
    accept = _acceptance_summary(rows)
    sequence_groups = _summarize_groups(evaluated, key="reclaim_type")
    result = {
        "interval_minutes": interval_minutes,
        "development_only": True,
        "observation_count": len(rows),
        "reclaim_event_count": len(events),
        "event_count_with_4bar_outcome": len(evaluated),
        "reclaim_summary": _metric_summary(evaluated, event_only=True),
        "matched_baseline": _matched_baseline(rows),
        "first_vs_repeated_reclaim": {
            "groups": sequence_groups,
            "first_mean_minus_repeated_mean_4bar_pct": (
                sequence_groups.get("FIRST_RECLAIM", {}).get("forward_4bar", {}).get("mean_pct", 0.0)
                - statistics.mean([
                    summary["forward_4bar"]["mean_pct"] for name, summary in sequence_groups.items()
                    if name in {"SECOND_RECLAIM", "THIRD_PLUS_RECLAIM"} and summary["forward_4bar"]["mean_pct"] is not None
                ])
                if sequence_groups.get("FIRST_RECLAIM", {}).get("forward_4bar", {}).get("mean_pct") is not None
                and any(name in sequence_groups and sequence_groups[name]["forward_4bar"]["mean_pct"] is not None
                        for name in ("SECOND_RECLAIM", "THIRD_PLUS_RECLAIM"))
                else None
            ),
        },
        "time_below_vwap": _summarize_groups(evaluated, key="time_below_bucket"),
        "excursion_depth": _summarize_groups(evaluated, key="excursion_bucket"),
        "vwap_slope": _summarize_groups(evaluated, key="vwap_slope_state"),
        "vwap_age": _summarize_groups(evaluated, key="vwap_age_bucket"),
        "reclaim_bar_quality": {
            "body_return_pct_mean": statistics.mean(float(row["body_return_pct"]) for row in evaluated) if evaluated else None,
            "close_location_value_mean": statistics.mean(float(row["close_location_value"]) for row in evaluated) if evaluated else None,
            "lower_wick_pct_mean": statistics.mean(float(row["lower_wick_pct"]) for row in evaluated) if evaluated else None,
            "upper_wick_pct_mean": statistics.mean(float(row["upper_wick_pct"]) for row in evaluated) if evaluated else None,
            "bar_range_pct_mean": statistics.mean(float(row["bar_range_pct"]) for row in evaluated) if evaluated else None,
            "relative_volume_8_mean": statistics.mean(
                float(row["relative_volume_8"]) for row in evaluated if row["relative_volume_8"] is not None
            ) if any(row["relative_volume_8"] is not None for row in evaluated) else None,
            "relative_turnover_8_mean": statistics.mean(
                float(row["relative_turnover_8"]) for row in evaluated if row["relative_turnover_8"] is not None
            ) if any(row["relative_turnover_8"] is not None for row in evaluated) else None,
        },
        "acceptance": accept,
        "failure_rates": {
            "within_1bar": _metric_summary(evaluated, event_only=True)["failure_within_1bar_rate"],
            "within_2bar": _metric_summary(evaluated, event_only=True)["failure_within_2bar_rate"],
            "within_4bar": _metric_summary(evaluated, event_only=True)["failure_within_4bar_rate"],
        },
        "time_of_day": _summarize_groups(evaluated, key="time_of_day_bucket"),
        "liquidity": _summarize_groups(evaluated, key="liquidity_bucket"),
        "market_split": _summarize_groups(evaluated, key="market"),
        "data_confidence": _summarize_groups(evaluated, key="data_confidence"),
        "session_direction": _summarize_groups(evaluated, key="session_direction"),
        "month": _summarize_groups(evaluated, key="session_month"),
        "interval_cost_hurdle": _descriptive_cost_stress(evaluated),
        "notes": [
            "Anatomy outcomes are descriptive overlapping bar observations, not independent trades.",
            "15m is primary; 30m is a Development-only structural check.",
            "No indicator filters or parameter search were used.",
        ],
    }
    result["high_confidence_reclaim_summary"] = _metric_summary(high, event_only=True)
    result["high_confidence_reclaim_count"] = len(high)
    result["early_strength_then_loss_vs_morning_weakness"] = _early_strength_summary(evaluated)
    result["event_concentration"] = _event_concentration(evaluated)
    result["gate"] = _strategy_gate(result)
    return result


def _descriptive_cost_stress(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Four-bar event-hold cost illustration; this is not a selected strategy."""
    result: dict[str, Any] = {
        "classification": "DESCRIPTIVE_EVENT_HOLD_NOT_STRATEGY_BACKTEST",
        "base_assumptions": {"broker_fee_rate": 0.00015, "sell_tax_rate": 0.0020, "slippage_bps_per_side": 15.0},
        "round_trip_cost_approx_bps": 53.0,
        "multipliers": {},
    }
    for multiplier in (1.0, 1.5, 2.0):
        net_returns = []
        gross_returns = []
        for event in events:
            gross = event.get("forward_4bar_gross_return_pct")
            entry = event.get("forward_4bar_entry_reference")
            exit_price = event.get("forward_4bar_exit_reference")
            if gross is None or entry is None or exit_price is None:
                continue
            slip = BASE_COST.slippage_bps * multiplier / 10_000
            fee = BASE_COST.broker_fee_rate * multiplier
            tax = BASE_COST.sell_tax_rate * multiplier
            buy_fill = float(entry) * (1 + slip)
            sell_fill = float(exit_price) * (1 - slip)
            net = (sell_fill * (1 - fee - tax) - buy_fill * (1 + fee)) / float(entry) * 100.0
            gross_returns.append(float(gross))
            net_returns.append(net)
        result["multipliers"][str(multiplier)] = {
            "event_count": len(net_returns),
            "mean_gross_pct": statistics.mean(gross_returns) if gross_returns else None,
            "mean_net_pct": statistics.mean(net_returns) if net_returns else None,
            "positive_net_rate": sum(value > 0 for value in net_returns) / len(net_returns) if net_returns else None,
        }
    return result


def _early_strength_summary(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    groups = defaultdict(list)
    for row in events:
        label = row.get("early_reclaim_context")
        if label:
            groups[label].append(row)
    return {name: _metric_summary(values, event_only=True) for name, values in sorted(groups.items())}


def _event_concentration(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    values = [row for row in events if row.get("forward_4bar_gross_return_pct") is not None]
    positive_total = sum(max(0.0, float(row["forward_4bar_gross_return_pct"])) for row in values)
    best = sorted(values, key=lambda row: float(row["forward_4bar_gross_return_pct"]), reverse=True)

    def grouped_share(key: str) -> list[dict[str, Any]]:
        groups: dict[str, float] = defaultdict(float)
        for row in values:
            groups[str(row[key])] += max(0.0, float(row["forward_4bar_gross_return_pct"]))
        return [
            {"key": name, "positive_gross_sum_pct": amount,
             "share_of_positive_gross_pct": amount / positive_total * 100.0 if positive_total else None}
            for name, amount in sorted(groups.items(), key=lambda item: (-item[1], item[0]))
        ]

    return {
        "classification": "DESCRIPTIVE_OVERLAPPING_EVENT_OUTCOMES_NOT_TRADES",
        "event_count": len(values),
        "positive_gross_component_sum_pct": positive_total,
        "top_event_shares": {
            "top_1_pct": max(0.0, float(best[0]["forward_4bar_gross_return_pct"])) / positive_total * 100.0 if positive_total and best else None,
            "top_5_pct": sum(max(0.0, float(row["forward_4bar_gross_return_pct"])) for row in best[:5]) / positive_total * 100.0 if positive_total else None,
        },
        "symbols_ranked": grouped_share("symbol"),
        "days_ranked": grouped_share("session"),
    }


def _safe_session_dq(dataset: SafeDataset, *, cache_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = ParquetBarCache(cache_root)
    bars_by_symbol_session: dict[tuple[str, date], list[Bar]] = defaultdict(list)
    for symbol, bars in dataset.bars_by_symbol.items():
        for bar in bars:
            local = bar.time.astimezone(KST)
            if SAFE_START <= local.date() <= SAFE_END:
                assert_safe_research_date(local.date())
                bars_by_symbol_session[(symbol, local.date())].append(bar)
    for symbol in dataset.partial_symbols:
        for session in dataset.sessions:
            assert_safe_research_date(session)
            if not SAFE_START <= session <= SAFE_END:
                raise ValueError("safe dataset loader returned a date outside Phase 7 bounds")
            path = cache.partition_path("minute", symbol, "1m", session)
            sidecar = path.with_suffix(".metadata.json")
            if path.exists() and sidecar.exists():
                bars_by_symbol_session[(symbol, session)].extend(cache.load("minute", symbol, "1m", session))
    dq_rows: list[dict[str, Any]] = []
    for (symbol, session), bars in sorted(bars_by_symbol_session.items()):
        if not SAFE_START <= session <= SAFE_END or session >= LOCKED_HOLDOUT_START:
            raise ValueError("attempted to inspect a protected session")
        observed, missing, classification = _session_classification(bars)
        ordered = sorted(bar.time.astimezone(KST) for bar in bars)
        volume_sum = sum(bar.volume for bar in bars)
        proxy_sum = sum(representative_price(bar) * bar.volume for bar in bars)
        dq_rows.append(asdict(SessionQuality(
            symbol=symbol,
            market=dataset.market_by_symbol[symbol],
            session=session.isoformat(),
            observed_minute_count=observed,
            expected_minute_count=EXPECTED_SESSION_MINUTES,
            missing_slots=missing,
            volume_sum=volume_sum,
            turnover_proxy_sum=proxy_sum,
            first_observed_timestamp=ordered[0].isoformat() if ordered else None,
            last_observed_timestamp=ordered[-1].isoformat() if ordered else None,
            classification=classification,
        )))
    counts = Counter(row["classification"] for row in dq_rows)
    summary = {
        "session_records": len(dq_rows),
        "classification_counts": dict(sorted(counts.items())),
        "observed_minutes": sum(row["observed_minute_count"] for row in dq_rows),
        "expected_minutes": len(dq_rows) * EXPECTED_SESSION_MINUTES,
        "missing_slots": sum(row["missing_slots"] for row in dq_rows),
        "volume_sum": sum(row["volume_sum"] for row in dq_rows),
        "turnover_proxy_sum_krw": sum(row["turnover_proxy_sum"] for row in dq_rows),
        "missing_absence_cause": "UNKNOWN; provider omission, no-trade interval, halt, session semantics, or retrieval issue",
        "synthetic_minutes_added": 0,
    }
    return dq_rows, summary


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=_json_default, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl_gz(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
        for row in rows:
            payload = json.dumps(
                row, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                default=_json_default, allow_nan=False,
            )
            stream.write((payload + "\n").encode("utf-8"))


def _cohort_market_counts(dataset: SafeDataset) -> dict[str, dict[str, int]]:
    result = {}
    for label, symbols in (
        ("complete", dataset.complete_symbols),
        ("partial", dataset.partial_symbols),
        ("not_acquired", dataset.missing_symbols),
    ):
        counts = Counter(dataset.market_by_symbol[symbol] for symbol in symbols)
        result[label] = {"total": len(symbols), "KOSPI": counts.get("KOSPI", 0), "KOSDAQ": counts.get("KOSDAQ", 0)}
    return result


def _partition_index_delta(
    current: dict[str, str], previous: dict[str, str]
) -> dict[str, Any]:
    added = {key: value for key, value in current.items() if key not in previous}
    changed = sorted(key for key in current.keys() & previous.keys() if current[key] != previous[key])
    by_symbol = Counter(key.split(":", 1)[0] for key in added)
    return {
        "previous_snapshot_available": bool(previous),
        "newly_available_safe_partitions": len(added),
        "newly_available_partitions_by_symbol": dict(sorted(by_symbol.items())),
        "changed_existing_partition_count": len(changed),
        "changed_existing_partition_keys": changed,
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _acquisition_reconciliation(
    *,
    dataset: SafeDataset,
    current_partition_index: dict[str, str],
    previous_dataset_manifest: dict[str, Any],
    acquisition_manifest: dict[str, Any],
    acquisition_request_interval_seconds: float | None,
    observed_rate_limit_interval_seconds: float | None,
    acquisition_stop_reason: str | None,
) -> dict[str, Any]:
    previous_index = previous_dataset_manifest.get("partition_hashes", {})
    if not isinstance(previous_index, dict):
        previous_index = {}
    delta = _partition_index_delta(current_partition_index, previous_index)
    previous_cohort = previous_dataset_manifest.get("cohort", {})
    current_counts = {
        "complete": len(dataset.complete_symbols),
        "partial": len(dataset.partial_symbols),
        "not_acquired": len(dataset.missing_symbols),
    }
    previous_counts = {
        "complete": previous_cohort.get("available_complete_symbols"),
        "partial": previous_cohort.get("available_partial_symbols"),
        "not_acquired": previous_cohort.get("not_acquired_symbols"),
    }
    collector_results = acquisition_manifest.get("symbol_results", {})
    if not isinstance(collector_results, dict):
        collector_results = {}
    recorded_new_partitions = sum(
        max(0, int(result.get("succeeded_sessions", 0)) - int(result.get("skipped_existing", 0)))
        for result in collector_results.values()
        if isinstance(result, dict)
    )
    collector_index_path = Path("runtime/research/phase6/phase6-artifact-index.json")
    collector_index = _read_json_object(collector_index_path)
    indexed_files = collector_index.get("files", [])
    expected_entry = next(
        (entry for entry in indexed_files if isinstance(entry, dict)
         and entry.get("file") == "phase6-acquisition-manifest.json"),
        None,
    ) if isinstance(indexed_files, list) else None
    current_manifest_path = Path("runtime/research/phase6/phase6-acquisition-manifest.json")
    current_manifest_sha = sha256_file(current_manifest_path) if current_manifest_path.is_file() else None
    return {
        "artifact": "phase7-acquisition-manifest",
        "status": "COMPLETE" if not dataset.missing_symbols and not dataset.partial_symbols else "PARTIAL",
        "network_acquisition_attempted": bool(collector_results),
        "network_acquisition_scope": (
            "KIS official read-only regular-session historical OHLCV; safe-period dates only; no account or order endpoints"
            if collector_results else "no acquisition attempt found in the Phase 6 collector manifest"
        ),
        "request_interval_seconds": acquisition_request_interval_seconds,
        "observed_persistent_rate_limit_interval_seconds": observed_rate_limit_interval_seconds,
        "stop_reason": acquisition_stop_reason,
        "complete_symbols": current_counts["complete"],
        "partial_symbols": current_counts["partial"],
        "not_acquired_symbols": current_counts["not_acquired"],
        "previous_complete_to_current": {
            "previous_snapshot": previous_counts,
            "current_snapshot": current_counts,
        },
        "reused_existing_valid_partitions_reported_by_collector": sum(
            int(result.get("skipped_existing", 0))
            for result in collector_results.values()
            if isinstance(result, dict)
        ),
        **delta,
        "new_partitions_recorded_by_collector_results": recorded_new_partitions,
        "new_partitions_present_in_cache_but_not_recorded_by_completed_collector_result": max(
            0, delta["newly_available_safe_partitions"] - recorded_new_partitions
        ),
        "collector_manifest": {
            "path": str(current_manifest_path),
            "status": acquisition_manifest.get("status", "MISSING"),
            "symbol_result_count": len(collector_results),
            "current_sha256": current_manifest_sha,
            "artifact_index_expected_entry": expected_entry,
            "matches_preserved_phase6_artifact_index": bool(
                expected_entry
                and current_manifest_sha == expected_entry.get("sha256")
                and current_manifest_path.stat().st_size == expected_entry.get("bytes")
            ),
            "interpretation": (
                "the collector writes to a Phase 6-owned output path; its observed status and current hash are reported "
                "against the unchanged Phase 6 artifact-index entry"
            ),
        },
        "resume_ready": True,
        "holdout_partitions_opened": 0,
        "external_partitions_opened": 0,
    }


def _build_formula_audit(*, source_git_sha: str, code_sha256: str, sample_schema: list[str]) -> dict[str, Any]:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required <= set(sample_schema):
        raise ValueError("minute partition schema is missing required OHLCV fields")
    return {
        "artifact": "vwap-formula-audit",
        "source_git_sha": source_git_sha,
        "research_code_sha256": code_sha256,
        "observed_minute_partition_fields": sample_schema,
        "observed_turnover_fields": [],
        "upstream_minute_parser_retained_fields": ["OHLC", "cntg_vol"],
        "exact_or_proxy": "PROXY",
        "vwap_source": VWAP_SOURCE,
        "vwap_formula": VWAP_FORMULA,
        "interval_proxy_turnover_formula": "sum(((high+low+close)/3)*observed_volume) over observed 1m bars in a complete 15m/30m bar",
        "session_reset": "per symbol and KST session date, beginning at 09:00; no prior session carry",
        "point_in_time": "each bar snapshot uses observed minute bars with bar_start <= the completed interval's final minute; no future prices or volumes",
        "zero_volume": "volume contributes zero; VWAP remains null while cumulative volume is zero; no signal with null VWAP",
        "missing_intervals": "not imputed; incomplete 15m/30m bars are omitted; session VWAP includes only observed minute volume and proxy turnover",
        "session_semantics": "KIS cached 1m bars, Asia/Seoul bar_start timestamps, continuous regular session 09:00-15:19; 15:20 closing auction excluded",
        "accuracy_limitation": "OHLC typical-price x volume is not transaction-level VWAP; no actual traded value is present in the cached Parquet or retained minute-parser model",
    }


def run_phase7_study(
    *,
    output_dir: Path = Path("runtime/research/phase7"),
    cache_root: Path = Path("data"),
    cohort_manifest_path: Path = Path("runtime/research/phase4/cohort-manifest.json"),
    split_manifest_path: Path = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json"),
    development_only: bool = True,
    acquisition_request_interval_seconds: float | None = None,
    observed_rate_limit_interval_seconds: float | None = None,
    acquisition_stop_reason: str | None = None,
) -> dict[str, Any]:
    """Run Phase 7 Development anatomy; never opens Holdout or external partitions."""
    del development_only  # Phase 7 starts and stops at Development anatomy in this pass.
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC).isoformat()
    source_git_sha = current_git_sha()
    code_sha256 = sha256_file(Path(__file__))
    dataset = load_safe_dataset(
        cohort_manifest_path=cohort_manifest_path,
        split_manifest_path=split_manifest_path,
        cache_root=cache_root,
    )
    if not dataset.development_sessions or not dataset.secondary_sessions:
        raise ValueError("Phase 7 requires recorded Development and Secondary safe partitions")
    if min(dataset.development_sessions) != SAFE_START or max(dataset.development_sessions) != DEVELOPMENT_END:
        raise ValueError("Phase 7 Development split does not match the preregistered safe period")
    if min(dataset.secondary_sessions) != SECONDARY_START or max(dataset.secondary_sessions) != SAFE_END:
        raise ValueError("Phase 7 Secondary split does not match the preregistered safe period")
    for session in (*dataset.development_sessions, *dataset.secondary_sessions):
        assert_safe_research_date(session)
        if session >= LOCKED_HOLDOUT_START:
            raise ValueError("Phase 7 safe session list overlaps protected Holdout")
    if dataset.dq["holdout_integrity"]["holdout_partitions_opened"] != 0:
        raise ValueError("Phase 7 loader reports protected Holdout access")

    dq_rows, dq_summary = _safe_session_dq(dataset, cache_root=cache_root)
    safe_rows = [
        row for row in dq_rows
        if SAFE_START <= date.fromisoformat(row["session"]) <= SAFE_END
    ]
    safe_partition_index = {
        key: value for key, value in sorted(dataset.partition_hashes.items())
        if SAFE_START <= date.fromisoformat(key.rsplit(":", 1)[1]) <= SAFE_END
    }
    expected_partition_count = len(dataset.complete_symbols) * len(dataset.sessions)
    complete_present = sum(
        1 for key in safe_partition_index
        if key.split(":", 1)[0] in dataset.complete_symbols
    )
    if complete_present != expected_partition_count:
        raise ValueError("Phase 7 complete-symbol safe partition inventory is incomplete")

    partition_index_sha = canonical_sha256(safe_partition_index)
    cohort_sha = canonical_sha256(list(dataset.cohort_symbols))
    dataset_sha = canonical_sha256({"partition_hashes": safe_partition_index, "cohort_sha256": cohort_sha})
    cohort_counts = _cohort_market_counts(dataset)
    previous_dataset_manifest = _read_json_object(output_dir / "phase7-dataset-manifest.json")
    phase6_acquisition_manifest = _read_json_object(
        Path("runtime/research/phase6/phase6-acquisition-manifest.json")
    )
    acquisition_manifest = _acquisition_reconciliation(
        dataset=dataset,
        current_partition_index=safe_partition_index,
        previous_dataset_manifest=previous_dataset_manifest,
        acquisition_manifest=phase6_acquisition_manifest,
        acquisition_request_interval_seconds=acquisition_request_interval_seconds,
        observed_rate_limit_interval_seconds=observed_rate_limit_interval_seconds,
        acquisition_stop_reason=acquisition_stop_reason,
    )
    acquisition_manifest.update({
        "source_git_sha": source_git_sha,
        "research_code_sha256": code_sha256,
        "dataset_sha256": dataset_sha,
        "partition_index_sha256": partition_index_sha,
        "cohort_sha256": cohort_sha,
        "vwap_source": VWAP_SOURCE,
        "vwap_formula": VWAP_FORMULA,
        "exact_or_proxy": "PROXY",
        "period": {"start": SAFE_START.isoformat(), "end": SAFE_END.isoformat()},
        "provider": "KIS official read-only historical regular-session OHLCV in the local cache",
        "timestamp_convention": "Asia/Seoul; minute timestamps label bar start; closing auction excluded",
        "cost_assumptions": {"broker_fee_rate": 0.00015, "sell_tax_rate": 0.0020,
                             "slippage_bps_per_side": 15.0, "stress_multipliers": [1.0, 1.5, 2.0]},
        "holdout_integrity": {"state": "LOCKED_NOT_EVALUATED", "holdout_partitions_opened": 0},
        "external_block_integrity": {"state": "NOT_ACCESSED", "external_partitions_opened": 0},
        "complete_symbol_count_by_market": cohort_counts["complete"],
    })
    cohort_snapshot = {
        "target_symbols": len(dataset.cohort_symbols),
        "available_complete_symbols": len(dataset.complete_symbols),
        "available_partial_symbols": len(dataset.partial_symbols),
        "not_acquired_symbols": len(dataset.missing_symbols),
        "market_counts": cohort_counts,
        "complete_symbol_list": list(dataset.complete_symbols),
        "partial_symbol_list": list(dataset.partial_symbols),
        "not_acquired_symbol_list": list(dataset.missing_symbols),
        "development_sessions": len(dataset.development_sessions),
        "secondary_sessions": len(dataset.secondary_sessions),
        "total_safe_sessions": len(dataset.sessions),
        "development_range": [SAFE_START.isoformat(), DEVELOPMENT_END.isoformat()],
        "secondary_range": [SECONDARY_START.isoformat(), SAFE_END.isoformat()],
        "safe_partition_count": len(safe_partition_index),
        "complete_symbol_partition_count": complete_present,
        "loaded_1m_rows_complete_symbols": sum(len(bars) for bars in dataset.bars_by_symbol.values()),
        "holdout_partitions_opened": 0,
        "external_partitions_opened": 0,
    }
    common = {
        "source_git_sha": source_git_sha,
        "research_code_sha256": code_sha256,
        "dataset_sha256": dataset_sha,
        "partition_index_sha256": partition_index_sha,
        "cohort_sha256": cohort_sha,
        "strategy_config_sha256": "NONE_ANATOMY_ONLY",
        "vwap_source": VWAP_SOURCE,
        "vwap_formula": VWAP_FORMULA,
        "exact_or_proxy": "PROXY",
        "period": {"development": [SAFE_START.isoformat(), DEVELOPMENT_END.isoformat()],
                   "secondary": [SECONDARY_START.isoformat(), SAFE_END.isoformat()]},
        "provider": "KIS regular-session minute OHLCV from the local safe-period Parquet cache",
        "timestamp_convention": "Asia/Seoul; bar_start; continuous session 09:00-15:19; auction excluded",
        "cost_assumptions": {"broker_fee_rate": 0.00015, "sell_tax_rate": 0.0020, "slippage_bps_per_side": 15.0,
                             "stress_multipliers": [1.0, 1.5, 2.0]},
        "holdout_integrity": {"state": "LOCKED_NOT_EVALUATED", "holdout_partitions_opened": 0},
        "external_block_integrity": {"state": "NOT_ACCESSED", "external_partitions_opened": 0},
    }

    acquisition_manifest.update(common)
    acquisition_manifest["artifact"] = "phase7-acquisition-manifest"
    acquisition_manifest["range"] = [SAFE_START.isoformat(), SAFE_END.isoformat()]
    dataset_manifest = {
        **common,
        "artifact": "phase7-dataset-manifest",
        "cohort": cohort_snapshot,
        "partition_count_including_partial_symbol": len(safe_partition_index),
        "partition_index_sha256": partition_index_sha,
        "dataset_sha256": dataset_sha,
        "dq_summary": dq_summary,
        "partition_hashes": safe_partition_index,
    }
    dq_manifest = {
        **common,
        "artifact": "phase7-dq",
        "session_confidence_rule": {
            "HIGH_CONFIDENCE": "all 380 expected 1m timestamps 09:00-15:19 observed, endpoints correct, cumulative volume > 0",
            "PARTIAL": "at least 361/380 expected timestamps (95%) and cumulative volume > 0, but not HIGH_CONFIDENCE",
            "UNRELIABLE": "below 95% observed coverage, wrong endpoints, or zero cumulative volume",
            "interpretation": "coverage classification only; does not prove whether absent slots are no-trade or provider omissions",
        },
        "summary": dq_summary,
        "session_records": safe_rows,
    }
    # Verify a representative safe partition's physical schema without reading any protected date.
    first_symbol = dataset.complete_symbols[0]
    first_session = dataset.sessions[0]
    first_path = ParquetBarCache(cache_root).partition_path("minute", first_symbol, "1m", first_session)
    import pyarrow.parquet as pq

    sample_schema = list(pq.ParquetFile(first_path).schema_arrow.names)
    formula_audit = {**common, **_build_formula_audit(source_git_sha=source_git_sha, code_sha256=code_sha256,
                                                      sample_schema=sample_schema)}

    development_bars = {
        symbol: tuple(bar for bar in dataset.bars_by_symbol[symbol]
                      if SAFE_START <= bar.time.astimezone(KST).date() <= DEVELOPMENT_END)
        for symbol in dataset.complete_symbols
    }
    observations_15m = build_interval_observations(
        development_bars, market_by_symbol=dataset.market_by_symbol, interval_minutes=15
    )
    observations_30m = build_interval_observations(
        development_bars, market_by_symbol=dataset.market_by_symbol, interval_minutes=30
    )
    outcomes_15m = attach_forward_outcomes(observations_15m)
    outcomes_30m = attach_forward_outcomes(observations_30m)
    anatomy_15m = analyze_development(outcomes_15m, interval_minutes=15)
    anatomy_30m = analyze_development(outcomes_30m, interval_minutes=30)
    anatomy_status = anatomy_15m["gate"]["status"]
    has_candidate = anatomy_status == "PASS"

    secondary = {
        **common,
        "artifact": "secondary-diagnostic",
        "status": "NOT_RUN_NEEDS_FROZEN_DEVELOPMENT_CANDIDATE" if has_candidate else "NOT_RUN_NO_DEVELOPMENT_CANDIDATE",
        "candidate_count": 0,
        "strategy_features_or_outcomes_computed": False,
        "reason": "Secondary is reserved for a frozen candidate that passes the Development anatomy gate.",
    }
    external = {
        **common,
        "artifact": "external-validation",
        "status": "NOT_AVAILABLE_NO_SECONDARY_SURVIVING_FROZEN_CANDIDATE",
        "candidate_count": 0,
        "external_data_opened": False,
        "reason": "No preregistered, Secondary-surviving candidate; external one-shot guard remains closed.",
    }
    preregistration = {
        **common,
        "artifact": "phase7-preregistration",
        "status": "NOT_CREATED_NO_QUALIFIED_CANDIDATE" if not has_candidate else "REQUIRES_VARIANT_FREEZE",
        "variant_count": 0,
        "reason": "A preregistration is created only after a frozen strategy candidate survives Development and Secondary.",
    }
    cost_stress = {
        **common,
        "artifact": "cost-stress",
        "status": "DESCRIPTIVE_EVENT_HOLD_ONLY",
        "intervals": {"15m_development": anatomy_15m["interval_cost_hurdle"],
                      "30m_development": anatomy_30m["interval_cost_hurdle"]},
        "strategy_backtest_run": False,
    }
    feasibility = {
        **common,
        "artifact": "100k-feasibility",
        "status": "NOT_RUN_NO_QUALIFIED_CANDIDATE" if not has_candidate else "REQUIRES_STRATEGY_IMPLEMENTATION",
        "portfolio_backtest_run": False,
        "capital_krw": 100_000,
        "order_cap_krw": 20_000,
        "risk_per_trade_pct": 0.25,
        "max_positions": 2,
        "whole_shares": True,
    }
    failure = {
        **common,
        "artifact": "vwap-reclaim-failure",
        "15m_development": {"failure_rates": anatomy_15m["failure_rates"], "acceptance": anatomy_15m["acceptance"]},
        "30m_development": {"failure_rates": anatomy_30m["failure_rates"], "acceptance": anatomy_30m["acceptance"]},
    }
    time_buckets = {
        **common,
        "artifact": "vwap-time-buckets",
        "mandatory_bucket_order": list(TIME_BUCKET_LABELS),
        "15m_development": anatomy_15m["time_of_day"],
        "30m_development": anatomy_30m["time_of_day"],
    }
    liquidity_buckets = {
        **common,
        "artifact": "vwap-liquidity-buckets",
        "bucket_rule_krw_per_completed_bar": ["<10M", "10M-50M", "50M-200M", ">=200M"],
        "15m_development": anatomy_15m["liquidity"],
        "30m_development": anatomy_30m["liquidity"],
    }
    market_split = {
        **common,
        "artifact": "vwap-market-split",
        "cohort_market_counts": cohort_counts,
        "15m_development": anatomy_15m["market_split"],
        "30m_development": anatomy_30m["market_split"],
    }
    hypotheses = {
        **common,
        "artifact": "vwap-hypotheses",
        "status": anatomy_status,
        "hypotheses": [
            {"id": "VWAP-RECLAIM-01", "question": "Do completed-bar reclaims beat comparable bars after same market/time/liquidity matching?",
             "evidence": anatomy_15m["matched_baseline"]},
            {"id": "VWAP-RECLAIM-02", "question": "Does first reclaim outperform repeats?",
             "evidence": anatomy_15m["first_vs_repeated_reclaim"]},
            {"id": "VWAP-RECLAIM-03", "question": "Does pre-reclaim time below or excursion depth alter outcomes?",
             "time_below": anatomy_15m["time_below_vwap"], "excursion_depth": anatomy_15m["excursion_depth"]},
            {"id": "VWAP-RECLAIM-04", "question": "Does post-reclaim acceptance reduce later VWAP recross failure and improve gross returns?",
             "evidence": anatomy_15m["acceptance"]},
            {"id": "VWAP-RECLAIM-05", "question": "Does the observed direction persist in high-confidence sessions?",
             "evidence": anatomy_15m["high_confidence_reclaim_summary"]},
        ],
    }
    summary = {
        **common,
        "artifact": "phase7-summary",
        "phase7_status": "ANATOMY_COMPLETE_NO_STRATEGY" if not has_candidate else "ANATOMY_PASS_VARIANTS_PENDING",
        "alpha": "UNPROVEN",
        "vwap_reclaim_anatomy": anatomy_status,
        "vwap_a": "NOT_CREATED" if not has_candidate else "RESEARCH_CANDIDATE_PENDING",
        "vwap_b": "NOT_CREATED" if not has_candidate else "RESEARCH_CANDIDATE_PENDING",
        "vwap_reclaim_family": "INSUFFICIENT" if anatomy_status == "INSUFFICIENT" else "REJECT" if anatomy_status == "FAIL" else "CONTINUE" if anatomy_status == "PASS" else "INSUFFICIENT",
        "shadow_next_session": "NO",
        "acquisition": acquisition_manifest,
        "dataset": cohort_snapshot,
        "dq_summary": dq_summary,
        "development_15m": anatomy_15m,
        "development_30m": anatomy_30m,
        "secondary_status": secondary["status"],
        "external_status": external["status"],
        "strategy_variants_created": False,
        "preregistration_state": preregistration["status"],
        "questions": _answer_questions(anatomy_15m, anatomy_status),
        "created_at": started_at,
    }

    artifacts: dict[str, Any] = {
        "phase7-acquisition-manifest.json": acquisition_manifest,
        "phase7-dataset-manifest.json": dataset_manifest,
        "phase7-dq.json": dq_manifest,
        "vwap-formula-audit.json": formula_audit,
        "vwap-anatomy-15m.json": {**common, **anatomy_15m},
        "vwap-anatomy-30m.json": {**common, **anatomy_30m},
        "vwap-reclaim-failure.json": failure,
        "vwap-time-buckets.json": time_buckets,
        "vwap-liquidity-buckets.json": liquidity_buckets,
        "vwap-market-split.json": market_split,
        "vwap-hypotheses.json": hypotheses,
        "secondary-diagnostic.json": secondary,
        "external-validation.json": external,
        "cost-stress.json": cost_stress,
        "100k-feasibility.json": feasibility,
        "phase7-summary.json": summary,
    }
    for name, payload in artifacts.items():
        _write_json(output_dir / name, payload)
    _write_jsonl_gz(output_dir / "vwap-observations-15m.jsonl.gz", outcomes_15m)
    _write_jsonl_gz(output_dir / "vwap-observations-30m.jsonl.gz", outcomes_30m)
    _write_jsonl_gz(output_dir / "vwap-events-15m.jsonl.gz", [row for row in outcomes_15m if row.get("up_reclaim")])
    _write_jsonl_gz(output_dir / "vwap-events-30m.jsonl.gz", [row for row in outcomes_30m if row.get("up_reclaim")])
    index_payload = {
        **common,
        "artifact": "phase7-artifact-index",
        "files": {},
        "content_sha256": canonical_sha256(summary),
        "holdout_partitions_opened": 0,
    }
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and path.name != "phase7-artifact-index.json":
            index_payload["files"][path.name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    _write_json(output_dir / "phase7-artifact-index.json", index_payload)
    return summary


def _answer_questions(anatomy: dict[str, Any], status: str) -> dict[str, str]:
    matched = anatomy["matched_baseline"]
    gross = anatomy["reclaim_summary"]["forward_4bar"]["mean_pct"]
    baseline_delta = matched["mean_event_minus_matched_baseline_pct"]
    first_diff = anatomy["first_vs_repeated_reclaim"]["first_mean_minus_repeated_mean_4bar_pct"]
    time_groups = anatomy["time_below_vwap"]
    bucket_order = ("1_BAR", "2_BARS", "3_4_BARS", "5_PLUS_BARS")
    time_means = [
        time_groups[name]["forward_4bar"]["mean_pct"] for name in bucket_order
        if name in time_groups and time_groups[name]["forward_4bar"]["mean_pct"] is not None
        and time_groups[name]["count"] >= 10
    ]
    slope_groups = anatomy["vwap_slope"]
    slope_vals = [group["forward_4bar"]["mean_pct"] for group in slope_groups.values()
                  if group["forward_4bar"]["mean_pct"] is not None and group["count"] >= 10]
    accepted = anatomy["acceptance"]["A_next_close_above_vwap"]
    acceptance_delayed_gross = accepted["next_executable_bar_after_confirmation_4bar_mean_gross_pct"]
    acceptance_delta_vs_all = (
        acceptance_delayed_gross - gross
        if acceptance_delayed_gross is not None and gross is not None else None
    )
    all_dir = anatomy["reclaim_summary"]["forward_4bar"]["mean_pct"]
    high_dir = anatomy["high_confidence_reclaim_summary"]["forward_4bar"]["mean_pct"]
    stress = anatomy["interval_cost_hurdle"]["multipliers"]["1.0"]["mean_net_pct"]
    enough_events = anatomy["event_count_with_4bar_outcome"] >= 30
    q1 = "INSUFFICIENT" if not enough_events or baseline_delta is None else "YES" if baseline_delta > 0 and gross is not None and gross > 0 else "NO"
    q2 = "INSUFFICIENT" if first_diff is None else "YES" if first_diff > 0 else "NO"
    monotone = len(time_means) >= 2 and (
        all(left <= right for left, right in pairwise(time_means))
        or all(left >= right for left, right in pairwise(time_means))
    )
    q3 = "INSUFFICIENT" if len(time_means) < 2 else (
        "YES" if monotone and max(time_means) - min(time_means) >= 0.10
        else "WEAK" if max(time_means) - min(time_means) >= 0.10
        else "NO"
    )
    q4 = "INSUFFICIENT" if accepted["events"] < 10 else "YES" if accepted.get("post_confirmation_4bar_failure_rate") is not None and anatomy["failure_rates"]["within_4bar"] - accepted["post_confirmation_4bar_failure_rate"] >= 0.10 else "NO"
    q5 = "YES" if (
        acceptance_delayed_gross is not None and gross is not None
        and acceptance_delayed_gross > gross and acceptance_delayed_gross > 0
    ) else "NO"
    q6 = "INSUFFICIENT" if anatomy["high_confidence_reclaim_count"] < 10 else (
        "YES" if all_dir is not None and high_dir is not None and all_dir > 0 and high_dir > 0 else "NO"
    )
    return {
        "Q1_reclaims_beat_matched_baseline": q1,
        "Q2_first_beats_repeated": q2,
        "Q3_time_below_matters": q3,
        "Q4_acceptance_reduces_failure": q4,
        "Q5_acceptance_improves_gross_expectancy": q5,
        "Q6_high_confidence_same_direction": q6,
        "Q7_vwap_a_positive_before_costs": "NO" if status != "PASS" else "NOT_RUN_VARIANTS_PENDING",
        "Q8_vwap_a_or_b_positive_after_costs": "NO" if stress is None or stress <= 0 or status != "PASS" else "NOT_RUN_VARIANTS_PENDING",
        "Q9_secondary_preserves_direction": "NOT_RUN",
        "Q10_untouched_external_validation_passes": "NOT_AVAILABLE",
        "Q11_100k_whole_share_feasibility": "NOT_RUN",
        "Q12_future_prospective_shadow_candidate": "NO",
        "development_4bar_event_mean_gross_pct": gross,
        "development_matched_baseline_delta_pct": baseline_delta,
        "development_high_confidence_4bar_mean_pct": high_dir,
        "acceptance_a_post_confirmation_4bar_mean_gross_pct": acceptance_delayed_gross,
        "acceptance_a_post_confirmation_delta_vs_all_reclaims_pct": acceptance_delta_vs_all,
        "acceptance_a_paired_delay_delta_vs_immediate_within_accepted_events_pct": accepted["confirmation_entry_mean_delta_vs_immediate_pct"],
        "development_4bar_event_hold_base_cost_mean_net_pct": stress,
        "vwap_slope_buckets_with_minimum_n10": slope_vals,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fail-closed Phase 7 VWAP reclaim Development anatomy")
    parser.add_argument("--output", type=Path, default=Path("runtime/research/phase7"))
    parser.add_argument("--cache", type=Path, default=Path("data"))
    parser.add_argument("--acquisition-request-interval", type=float)
    parser.add_argument("--observed-rate-limit-interval", type=float)
    parser.add_argument("--acquisition-stop-reason")
    args = parser.parse_args()
    summary = run_phase7_study(
        output_dir=args.output,
        cache_root=args.cache,
        acquisition_request_interval_seconds=args.acquisition_request_interval,
        observed_rate_limit_interval_seconds=args.observed_rate_limit_interval,
        acquisition_stop_reason=args.acquisition_stop_reason,
    )
    print(json.dumps({
        "phase7_status": summary["phase7_status"],
        "vwap_reclaim_anatomy": summary["vwap_reclaim_anatomy"],
        "development_15m_events": summary["development_15m"]["event_count_with_4bar_outcome"],
        "holdout_partitions_opened": summary["holdout_integrity"]["holdout_partitions_opened"],
    }, indent=2))


if __name__ == "__main__":
    main()

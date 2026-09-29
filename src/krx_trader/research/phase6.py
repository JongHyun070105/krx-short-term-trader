from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.metrics import calculate_metrics
from krx_trader.backtest.portfolio import run_portfolio_backtest
from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.quality import inspect_bars
from krx_trader.models import Bar, Decision, Signal
from krx_trader.research.phase5_backfill import LOCKED_HOLDOUT_START, assert_safe_research_date
from krx_trader.strategies.mean_reversion import (
    DEFAULT_MR_CONFIG,
    MeanReversionConfig,
    MeanReversionVariant,
    ReversalObservation,
    evaluate_mean_reversion,
    preregistration_mr_config,
)

KST = ZoneInfo("Asia/Seoul")
SAFE_START = date(2026, 4, 17)
SAFE_END = date(2026, 7, 27)
EXTERNAL_START = date(2026, 1, 5)
EXTERNAL_END = date(2026, 4, 16)
EXPECTED_MINUTES_PER_SESSION = 380
BASE_COST = CostModel(broker_fee_rate=0.00015, sell_tax_rate=0.0020, slippage_bps=15.0)
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
RANK_BUCKETS = (
    (0.0, 10.0, "0-10%"),
    (10.0, 20.0, "10-20%"),
    (20.0, 40.0, "20-40%"),
    (40.0, 60.0, "40-60%"),
    (60.0, 80.0, "60-80%"),
    (80.0, 90.0, "80-90%"),
    (90.0, 100.01, "90-100%"),
)


@dataclass(frozen=True, slots=True)
class ResampledResearchBar:
    bar: Bar
    observed_minute_count: int
    expected_minute_count: int
    first_observed_minute: datetime
    last_observed_minute: datetime
    freshness_minutes: float
    completeness: float
    turnover_krw: float


@dataclass(frozen=True, slots=True)
class SafeDataset:
    cohort_symbols: tuple[str, ...]
    complete_symbols: tuple[str, ...]
    partial_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]
    market_by_symbol: dict[str, str]
    sessions: tuple[date, ...]
    development_sessions: tuple[date, ...]
    secondary_sessions: tuple[date, ...]
    bars_by_symbol: dict[str, tuple[Bar, ...]]
    partition_hashes: dict[str, str]
    dq: dict[str, Any]
    dataset_hash: str
    cohort_hash: str


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


def safe_research_sessions(split_manifest_path: Path) -> tuple[list[date], list[date]]:
    """Read only safe split keys and reject any date that reaches the protected boundary."""
    manifest = json.loads(split_manifest_path.read_text(encoding="utf-8"))
    splits = manifest.get("splits", {})
    development = sorted(date.fromisoformat(day) for day in splits.get("development", {}).get("sessions", []))
    secondary = sorted(date.fromisoformat(day) for day in splits.get("validation", {}).get("sessions", []))
    if not development or not secondary:
        raise ValueError("safe Development and Secondary sessions are required")
    for session in [*development, *secondary]:
        assert_safe_research_date(session)
        if not SAFE_START <= session <= SAFE_END:
            raise ValueError("research sessions exceed the Phase 6 safe-period bounds")
    return development, secondary


def assert_external_evidence_allowed(
    *, candidate_preregistered: bool, strategy_freeze_commit: str | None, sessions: Iterable[date]
) -> None:
    requested = list(sessions)
    if not candidate_preregistered:
        raise ValueError("external evidence is locked until a Secondary-surviving candidate is preregistered")
    if not strategy_freeze_commit or strategy_freeze_commit in {"", "UNAVAILABLE", "WORKTREE"}:
        raise ValueError("external evidence requires a completed strategy-freeze commit")
    for session in requested:
        if SAFE_START <= session <= SAFE_END or session >= LOCKED_HOLDOUT_START:
            raise ValueError("external block overlaps protected or safe-period data")
    if not requested or min(requested) < EXTERNAL_START or max(requested) > EXTERNAL_END:
        raise ValueError("external evidence requests must stay inside the untouched external block")


def load_safe_dataset(
    *,
    cohort_manifest_path: Path = Path("runtime/research/phase4/cohort-manifest.json"),
    split_manifest_path: Path = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json"),
    cache_root: Path = Path("data"),
) -> SafeDataset:
    cohort_data = json.loads(cohort_manifest_path.read_text(encoding="utf-8"))["cohort_60"]
    symbols = list(cohort_data["symbols"])
    if len(symbols) != 60 or len(set(symbols)) != len(symbols):
        raise ValueError("Phase 6 cohort must contain 60 unique symbols")
    markets = {symbol: ("KOSPI" if index < 30 else "KOSDAQ") for index, symbol in enumerate(symbols)}
    development, secondary = safe_research_sessions(split_manifest_path)
    sessions = sorted(set(development + secondary))
    cache = ParquetBarCache(cache_root)
    bars_by_symbol: dict[str, tuple[Bar, ...]] = {}
    partition_hashes: dict[str, str] = {}
    verified_partitions = 0
    rows = 0
    missing_slots = 0
    absent_partition_slots = 0
    invalid_partitions: dict[str, str] = {}
    partial_partitions = 0
    freshness_by_symbol: dict[str, dict[str, int]] = {}
    existing_counts: dict[str, int] = defaultdict(int)

    for symbol in symbols:
        symbol_bars: list[Bar] = []
        present_count = 0
        symbol_freshness: dict[str, int] = {"fresh": 0, "stale_or_sparse": 0}
        for session in sessions:
            path = cache.partition_path("minute", symbol, "1m", session)
            meta_path = path.with_suffix(".metadata.json")
            key = f"{symbol}:{session.isoformat()}"
            if not path.is_file() and not meta_path.is_file():
                absent_partition_slots += 1
                continue
            existing_counts[symbol] += 1
            if not path.is_file() or not meta_path.is_file():
                invalid_partitions[key] = "PARQUET_OR_METADATA_MISSING"
                continue
            try:
                bars = cache.load("minute", symbol, "1m", session)
                issues = inspect_bars(bars)
                if issues:
                    invalid_partitions[key] = "DATA_QUALITY_FAILURE"
                    continue
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                if metadata.get("symbol") != symbol or metadata.get("interval") != "1m":
                    invalid_partitions[key] = "PROVENANCE_IDENTITY_MISMATCH"
                    continue
                if any(bar.time.astimezone(KST).date() != session for bar in bars):
                    invalid_partitions[key] = "PARTITION_DATE_MISMATCH"
                    continue
                present_count += 1
                verified_partitions += 1
                rows += len(bars)
                missing_slots += max(0, EXPECTED_MINUTES_PER_SESSION - len(bars))
                partition_hashes[key] = str(metadata["sha256"])
                symbol_bars.extend(bars)
                partial = len(bars) < EXPECTED_MINUTES_PER_SESSION
                if partial:
                    partial_partitions += 1
                    symbol_freshness["stale_or_sparse"] += 1
                else:
                    symbol_freshness["fresh"] += 1
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                invalid_partitions[key] = "CACHE_VALIDATION_FAILURE"
        if present_count == len(sessions):
            bars_by_symbol[symbol] = tuple(sorted(symbol_bars, key=lambda bar: bar.time))
        elif present_count:
            freshness_by_symbol[symbol] = symbol_freshness

    complete = sorted(bars_by_symbol)
    partial = sorted(symbol for symbol in symbols if symbol not in bars_by_symbol and existing_counts[symbol] > 0)
    missing = sorted(set(symbols) - set(complete) - set(partial))
    safe_holdout = {"holdout_partitions_opened": 0, "state": "LOCKED_NOT_EVALUATED"}
    dataset_hash = canonical_sha256(partition_hashes)
    cohort_hash = canonical_sha256(symbols)
    dq = {
        "schema_version": 1,
        "artifact": "phase6-dq",
        "git_sha": current_git_sha(),
        "provider": "KIS official read-only historical OHLCV",
        "period": {"start": SAFE_START.isoformat(), "end": SAFE_END.isoformat(), "sessions": len(sessions)},
        "timestamp_convention": "Asia/Seoul; minute timestamps label bar start; no synthetic minutes",
        "cohort": {"target_symbols": len(symbols), "complete_symbols": len(complete),
                   "partial_symbols": len(partial), "not_acquired_symbols": len(missing),
                   "kospi_complete": sum(markets[s] == "KOSPI" for s in complete),
                   "kosdaq_complete": sum(markets[s] == "KOSDAQ" for s in complete),
                   "complete_symbol_list": complete, "partial_symbol_list": partial,
                   "not_acquired_symbol_list": missing},
        "partitions": {"expected_for_target": len(symbols) * len(sessions),
                       "verified_present": verified_partitions,
                       "absent_symbol_sessions": absent_partition_slots,
                       "invalid_partitions": invalid_partitions,
                       "partial_present_partitions": partial_partitions,
                       "rows": rows, "expected_minute_absences": missing_slots,
                       "absence_cause": "UNKNOWN; provider omission, no-trade interval, halt, session semantics, or retrieval issue",
                       "synthetic_bars": 0},
        "partition_index_hash": dataset_hash,
        "dataset_hash": dataset_hash,
        "cohort_hash": cohort_hash,
        "market_by_symbol": markets,
        "partial_symbol_partition_shape": freshness_by_symbol,
        "holdout_integrity": safe_holdout,
    }
    return SafeDataset(tuple(symbols), tuple(complete), tuple(partial), tuple(missing), markets,
                       tuple(sessions), tuple(development), tuple(secondary), bars_by_symbol,
                       partition_hashes, dq, dataset_hash, cohort_hash)


def resample_with_quality(bars: Iterable[Bar], interval_minutes: int) -> list[ResampledResearchBar]:
    if interval_minutes not in {15, 30}:
        raise ValueError("Phase 6 supports 15m primary and 30m diagnostic intervals")
    by_session_bucket: dict[tuple[date, int], list[Bar]] = defaultdict(list)
    for bar in bars:
        local = bar.time.astimezone(KST)
        if time(9, 0) <= local.time() < time(15, 20):
            offset = int((local - local.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() // 60)
            by_session_bucket[(local.date(), offset // interval_minutes)].append(bar)
    result: list[ResampledResearchBar] = []
    for (session, bucket_index), values in sorted(by_session_bucket.items()):
        values.sort(key=lambda item: item.time)
        start = datetime.combine(session, time(9, 0), KST) + timedelta(minutes=bucket_index * interval_minutes)
        expected_count = min(interval_minutes, max(0, EXPECTED_MINUTES_PER_SESSION - bucket_index * interval_minutes))
        if expected_count <= 0:
            continue
        expected_last = start + timedelta(minutes=expected_count - 1)
        traded = [bar for bar in values if bar.volume > 0]
        freshness = (expected_last - traded[-1].time.astimezone(KST)).total_seconds() / 60 if traded else float("inf")
        value = Bar(
            time=start + timedelta(minutes=expected_count),
            open=values[0].open,
            high=max(item.high for item in values),
            low=min(item.low for item in values),
            close=values[-1].close,
            volume=sum(item.volume for item in values),
        )
        result.append(ResampledResearchBar(
            bar=value,
            observed_minute_count=len(values),
            expected_minute_count=expected_count,
            first_observed_minute=values[0].time.astimezone(KST),
            last_observed_minute=values[-1].time.astimezone(KST),
            freshness_minutes=max(0.0, freshness),
            completeness=len(values) / expected_count,
            turnover_krw=sum(item.close * item.volume for item in values),
        ))
    return result


def _usable(bar: ResampledResearchBar, *, clean_only: bool) -> bool:
    if bar.turnover_krw <= 0 or bar.freshness_minutes > 1.0:
        return False
    return not clean_only or bar.completeness >= DEFAULT_MR_CONFIG.minimum_clean_completeness


def _rank_percentiles(values: dict[str, float]) -> dict[str, float]:
    """Assign equal values the same midrank percentile, independent of mapping order."""
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    n = len(ordered)
    ranks: dict[str, float] = {}
    index = 0
    while index < n:
        end = index + 1
        while end < n and ordered[end][1] == ordered[index][1]:
            end += 1
        average_rank = ((index + 1) + end) / 2
        pct = average_rank / n * 100.0
        for symbol, _ in ordered[index:end]:
            ranks[symbol] = pct
        index = end
    return ranks


def time_of_day_bucket(timestamp: datetime, interval_minutes: int) -> str:
    local = timestamp.astimezone(KST) - timedelta(minutes=interval_minutes)
    local_time = local.time()
    for start, end, label in TIME_BUCKETS:
        if start <= local_time < end:
            return label
    return "OUT_OF_SESSION"


def _median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def build_point_in_time_snapshots(
    bars_by_symbol: dict[str, tuple[ResampledResearchBar, ...] | list[ResampledResearchBar]],
    *,
    interval_minutes: int,
    market_by_symbol: dict[str, str],
    minimum_participants: int,
    clean_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if minimum_participants < 2:
        raise ValueError("cross-sectional minimum must be at least two participants")
    by_symbol_time = {
        symbol: {row.bar.time: row for row in values}
        for symbol, values in bars_by_symbol.items()
    }
    timeline = sorted({timestamp for values in by_symbol_time.values() for timestamp in values})
    period = timedelta(minutes=interval_minutes)
    participant_counts: dict[str, int] = {}
    snapshots: list[dict[str, Any]] = []
    rank_history: dict[tuple[str, datetime], float] = {}
    row_history: dict[tuple[str, datetime], dict[str, Any]] = {}

    for timestamp in timeline:
        current: dict[str, ResampledResearchBar] = {}
        active_features: dict[str, dict[str, Any]] = {}
        for symbol in sorted(by_symbol_time):
            row = by_symbol_time[symbol].get(timestamp)
            if row is None or not _usable(row, clean_only=clean_only):
                continue
            current[symbol] = row
            bar = row.bar
            previous = by_symbol_time[symbol].get(timestamp - period)
            previous_previous = by_symbol_time[symbol].get(timestamp - period * 2)
            short_ref = by_symbol_time[symbol].get(timestamp - period * 3)
            medium_ref = by_symbol_time[symbol].get(timestamp - period * 6)
            local_refs = [by_symbol_time[symbol].get(timestamp - period * offset) for offset in (1, 2, 3)]
            if not all(ref is not None and ref.bar.time.date() == timestamp.date() and _usable(ref, clean_only=clean_only)
                       for ref in (previous, short_ref, medium_ref)):
                continue
            assert previous is not None and short_ref is not None and medium_ref is not None
            session_rows = [r for r in by_symbol_time[symbol].values()
                            if r.bar.time.date() == timestamp.date() and r.bar.time <= timestamp
                            and _usable(r, clean_only=clean_only)]
            prior_rows = sorted((r for r in session_rows if r.bar.time < timestamp), key=lambda r: r.bar.time)
            local_window_valid = all(ref is not None and ref.bar.time.date() == timestamp.date()
                                     and _usable(ref, clean_only=clean_only) for ref in local_refs)
            local_window = [ref for ref in local_refs if ref is not None]
            local_low = min([item.bar.low for item in local_window] + [bar.low]) if local_window_valid else None
            structural_refs = local_refs[:2]
            stop_price = (min([ref.bar.low for ref in structural_refs if ref is not None] + [bar.low])
                          if all(ref is not None and ref.bar.time.date() == timestamp.date()
                                 and _usable(ref, clean_only=clean_only) for ref in structural_refs)
                          else None)
            previous_return = (
                (previous.bar.close - previous_previous.bar.close) / previous_previous.bar.close
                if previous_previous is not None and previous_previous.bar.close > 0
                and previous_previous.bar.time.date() == timestamp.date()
                and _usable(previous_previous, clean_only=clean_only) else None
            )
            current_bar_return = (bar.close - previous.bar.close) / previous.bar.close
            close_location = (bar.close - bar.low) / (bar.high - bar.low) if bar.high > bar.low else None
            lower_wick = (min(bar.open, bar.close) - bar.low) / (bar.high - bar.low) if bar.high > bar.low else None
            upper_wick = (bar.high - max(bar.open, bar.close)) / (bar.high - bar.low) if bar.high > bar.low else None
            session_low = min(item.bar.low for item in session_rows)
            session_high = max(item.bar.high for item in session_rows)
            stopped_making_local_low = (
                bar.low >= min(item.bar.low for item in local_window) if local_window_valid else None
            )
            prior_turnovers = [item.turnover_krw for item in prior_rows[-20:]]
            recent_returns = [
                (right.bar.close - left.bar.close) / left.bar.close
                for left, right in zip(prior_rows[-20:-1], prior_rows[-19:])
                if left.bar.close > 0
            ]
            recent_range = [
                (item.bar.high - item.bar.low) / item.bar.close
                for item in [*prior_rows[-5:], row] if item.bar.close > 0
            ]
            session_open_minute = next((item for item in by_symbol_time[symbol].values()
                if item.bar.time.date() == timestamp.date()
                and item.first_observed_minute.time() == time(9, 0)), None)
            active_features[symbol] = {
                "bar": row,
                "previous_bar": previous,
                "prior_short_return": (previous.bar.close - short_ref.bar.close) / short_ref.bar.close,
                "short_window_return": (bar.close - short_ref.bar.close) / short_ref.bar.close,
                "medium_window_return": (bar.close - medium_ref.bar.close) / medium_ref.bar.close,
                "previous_bar_return": previous_return,
                "current_bar_return": current_bar_return,
                "session_open_return": ((bar.close - session_open_minute.bar.open) / session_open_minute.bar.open
                    if session_open_minute and session_open_minute.bar.open > 0 else None),
                "distance_from_local_low": bar.close / local_low - 1 if local_low is not None and local_low > 0 else None,
                "distance_from_session_low": bar.close / session_low - 1 if session_low > 0 else None,
                "distance_from_session_high": bar.close / session_high - 1 if session_high > 0 else None,
                "close_location": close_location,
                "body_fraction": abs(bar.close - bar.open) / (bar.high - bar.low) if bar.high > bar.low else None,
                "signed_body_fraction": (bar.close - bar.open) / (bar.high - bar.low) if bar.high > bar.low else None,
                "body_return": (bar.close - bar.open) / bar.open if bar.open > 0 else None,
                "lower_wick_fraction": lower_wick,
                "upper_wick_fraction": upper_wick,
                "relative_turnover": row.turnover_krw / statistics.median(prior_turnovers) if prior_turnovers and statistics.median(prior_turnovers) > 0 else None,
                "recent_realized_range": statistics.mean(recent_range) if recent_range else None,
                "recent_realized_volatility": statistics.pstdev(recent_returns) if len(recent_returns) >= 2 else None,
                "stopped_making_local_low": stopped_making_local_low,
                "negative_return_magnitude_decreased": (
                    None if previous_return is None else current_bar_return < 0 and previous_return < 0
                    and abs(current_bar_return) < abs(previous_return)
                ),
                "close_above_previous": bar.close > previous.bar.close,
                "positive_current_bar": current_bar_return > 0,
                "close_in_upper_half": close_location >= 0.5 if close_location is not None else None,
                "lower_wick_rejection": lower_wick >= 0.4 if lower_wick is not None else None,
                "freshness_minutes": row.freshness_minutes,
                "completeness": row.completeness,
                "observed_minute_count": row.observed_minute_count,
                "expected_minute_count": row.expected_minute_count,
                "first_observed_minute": row.first_observed_minute.isoformat(),
                "last_observed_minute": row.last_observed_minute.isoformat(),
                "turnover_krw": row.turnover_krw,
                "session_low": session_low,
                "previous_session_low_distance": None,
            }

        participant_counts[timestamp.isoformat()] = len(active_features)
        if len(active_features) < minimum_participants:
            continue
        short_ranks = _rank_percentiles({s: f["short_window_return"] for s, f in active_features.items()})
        medium_ranks = _rank_percentiles({s: f["medium_window_return"] for s, f in active_features.items()})
        turnover_ranks = _rank_percentiles({s: f["turnover_krw"] for s, f in active_features.items()})
        median_market_return = statistics.median(f["medium_window_return"] for f in active_features.values())
        returns = sorted(f["medium_window_return"] for f in active_features.values())
        dispersion = returns[min(len(returns) - 1, math.ceil(0.9 * len(returns)) - 1)] - returns[max(0, math.floor(0.1 * len(returns)))]
        for symbol in sorted(active_features):
            item = active_features[symbol]
            current_rank = short_ranks[symbol]
            prior_rank = rank_history.get((symbol, timestamp - period))
            rank_delta = current_rank - prior_rank if prior_rank is not None else None
            prior_delta = None
            if prior_rank is not None:
                previous_previous_rank = rank_history.get((symbol, timestamp - period * 2))
                if previous_previous_rank is not None:
                    prior_delta = prior_rank - previous_previous_rank
            prev_snapshot = row_history.get((symbol, timestamp - period))
            low_distance = item["distance_from_session_low"]
            prev_low_distance = prev_snapshot.get("distance_from_session_low") if prev_snapshot else None
            session_low_recovery = (
                None if prev_low_distance is None or low_distance is None else low_distance > prev_low_distance
            )
            row = {
                "timestamp": timestamp.isoformat(),
                "session_date": timestamp.date().isoformat(),
                "symbol": symbol,
                "market": market_by_symbol.get(symbol, "UNKNOWN"),
                "open": item["bar"].bar.open,
                "high": item["bar"].bar.high,
                "low": item["bar"].bar.low,
                "close": item["bar"].bar.close,
                "time_of_day": time_of_day_bucket(timestamp, interval_minutes),
                "participant_count": len(active_features),
                "minimum_participants": minimum_participants,
                "short_window_return": item["short_window_return"],
                "medium_window_return": item["medium_window_return"],
                "percentile_short": current_rank,
                "percentile_medium": medium_ranks[symbol],
                "prior_percentile_short": prior_rank,
                "rank_delta_points": rank_delta,
                "rank_acceleration_points": rank_delta - prior_delta if rank_delta is not None and prior_delta is not None else None,
                "prior_short_return": item["prior_short_return"],
                "session_open_return": item["session_open_return"],
                "distance_from_local_low": item["distance_from_local_low"],
                "distance_from_session_low": low_distance,
                "distance_from_session_high": item["distance_from_session_high"],
                "session_low_distance_recovery": session_low_recovery,
                "current_bar_return": item["current_bar_return"],
                "body_fraction": item["body_fraction"],
                "signed_body_fraction": item["signed_body_fraction"],
                "body_return": item["body_return"],
                "close_location": item["close_location"],
                "lower_wick_fraction": item["lower_wick_fraction"],
                "upper_wick_fraction": item["upper_wick_fraction"],
                "turnover_krw": item["turnover_krw"],
                "turnover_percentile": turnover_ranks[symbol],
                "relative_turnover": item["relative_turnover"],
                "recent_realized_range": item["recent_realized_range"],
                "recent_realized_volatility": item["recent_realized_volatility"],
                "market_median_return": median_market_return,
                "cross_sectional_dispersion": dispersion,
                "observed_minute_count": item["observed_minute_count"],
                "expected_minute_count": item["expected_minute_count"],
                "first_observed_minute": item["first_observed_minute"],
                "last_observed_minute": item["last_observed_minute"],
                "freshness_minutes": item["freshness_minutes"],
                "completeness": item["completeness"],
                "stabilization": {
                    "new_local_low": (not item["stopped_making_local_low"]
                                      if item["stopped_making_local_low"] is not None else None),
                    "lower_low_stopped": item["stopped_making_local_low"],
                    "negative_return_magnitude_decreased": item["negative_return_magnitude_decreased"],
                    "close_above_previous": item["close_above_previous"],
                    "positive_current_bar": item["positive_current_bar"],
                    "close_in_upper_half": item["close_in_upper_half"],
                    "lower_wick_rejection": item["lower_wick_rejection"],
                    "rank_deterioration_stopped": None if rank_delta is None else rank_delta >= 0,
                    "rank_improvement": None if rank_delta is None else rank_delta >= 10.0,
                    "session_low_distance_recovery": session_low_recovery,
                },
                "stop_price": stop_price,
            }
            snapshots.append(row)
            row_history[(symbol, timestamp)] = row
            rank_history[(symbol, timestamp)] = current_rank
    return snapshots, participant_counts


def attach_forward_outcomes(
    snapshots: list[dict[str, Any]],
    bars_by_symbol: dict[str, tuple[ResampledResearchBar, ...] | list[ResampledResearchBar]],
    *,
    interval_minutes: int,
    clean_only: bool = False,
) -> list[dict[str, Any]]:
    """Add labels after point-in-time features are frozen; every label starts next bar."""
    rank_lookup = {(row["symbol"], datetime.fromisoformat(row["timestamp"])): row["percentile_short"]
                   for row in snapshots}
    enriched: list[dict[str, Any]] = []
    for snapshot in snapshots:
        row = dict(snapshot)
        timestamp = datetime.fromisoformat(row["timestamp"])
        symbol_bars = sorted(
            (item for item in bars_by_symbol[row["symbol"]]
             if item.bar.time.date() == timestamp.date() and item.bar.time > timestamp
             and _usable(item, clean_only=clean_only)),
            key=lambda item: item.bar.time,
        )
        for horizon in (1, 2, 4, 8):
            row[f"ret_fwd_{horizon}"] = None
            row[f"future_rank_change_{horizon}"] = None
        row.update({"mfe_10bar": None, "mae_10bar": None,
                    "time_to_mfe_bars": None, "time_to_mae_bars": None,
                    "entry_delay_intervals": None})
        if not symbol_bars:
            enriched.append(row)
            continue
        entry_open = symbol_bars[0].bar.open
        if entry_open <= 0:
            enriched.append(row)
            continue
        row["entry_delay_intervals"] = (symbol_bars[0].bar.time - timestamp).total_seconds() / (60 * interval_minutes)
        for horizon in (1, 2, 4, 8):
            if len(symbol_bars) >= horizon:
                close = symbol_bars[horizon - 1].bar.close
                row[f"ret_fwd_{horizon}"] = (close - entry_open) / entry_open
                future_rank = rank_lookup.get((row["symbol"], symbol_bars[horizon - 1].bar.time))
                if future_rank is not None:
                    row[f"future_rank_change_{horizon}"] = future_rank - row["percentile_short"]
        window = symbol_bars[:10]
        highs = [(bar.bar.high - entry_open) / entry_open for bar in window]
        lows = [(bar.bar.low - entry_open) / entry_open for bar in window]
        row["mfe_10bar"] = max(highs)
        row["mae_10bar"] = min(lows)
        row["time_to_mfe_bars"] = highs.index(max(highs)) + 1
        row["time_to_mae_bars"] = lows.index(min(lows)) + 1
        enriched.append(row)
    return enriched


def _mean_or_none(values: Iterable[float]) -> float | None:
    items = list(values)
    return statistics.mean(items) if items else None


def summarize_observations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"sample_size": len(rows)}
    for horizon in (1, 2, 4, 8):
        returns = [float(row[f"ret_fwd_{horizon}"]) for row in rows if row.get(f"ret_fwd_{horizon}") is not None]
        summary[f"n_fwd_{horizon}"] = len(returns)
        summary[f"mean_fwd_{horizon}_return"] = _mean_or_none(returns)
        summary[f"win_rate_fwd_{horizon}"] = sum(value > 0 for value in returns) / len(returns) if returns else None
        summary[f"positive_fwd_{horizon}_count"] = sum(value > 0 for value in returns)
        summary[f"negative_fwd_{horizon}_count"] = sum(value < 0 for value in returns)
    for field in ("mfe_10bar", "mae_10bar", "time_to_mfe_bars", "time_to_mae_bars"):
        summary[f"mean_{field}"] = _mean_or_none(float(row[field]) for row in rows if row.get(field) is not None)
    for horizon in (1, 2, 4, 8):
        summary[f"mean_future_rank_change_{horizon}"] = _mean_or_none(
            float(row[f"future_rank_change_{horizon}"])
            for row in rows if row.get(f"future_rank_change_{horizon}") is not None
        )
    return summary


def _rank_bucket(percentile: float) -> str:
    for lower, upper, label in RANK_BUCKETS:
        if lower <= percentile < upper:
            return label
    return "90-100%" if percentile >= 100 else "UNKNOWN"


def _price_bucket(price: float) -> str:
    if price < 1_000:
        return "<1,000"
    if price < 3_000:
        return "1,000-3,000"
    if price < 5_000:
        return "3,000-5,000"
    if price < 10_000:
        return "5,000-10,000"
    if price < 30_000:
        return "10,000-30,000"
    return "30,000+"


def _slice_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key, "UNKNOWN"))].append(row)
    return {label: summarize_observations(items) for label, items in sorted(buckets.items())}


def anatomy_summary(rows: list[dict[str, Any]], *, interval_name: str, minimum_participants: int,
                    participant_counts: dict[str, int], clean_only: bool) -> dict[str, Any]:
    rank_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rank_groups[_rank_bucket(float(row["percentile_short"]))].append(row)
    rank_summary = {label: summarize_observations(rank_groups.get(label, [])) for _, _, label in RANK_BUCKETS}

    laggard = [row for row in rows if row.get("prior_percentile_short") is not None
               and float(row["prior_percentile_short"]) <= DEFAULT_MR_CONFIG.laggard_percentile
               and float(row["prior_short_return"]) < 0]
    stabilization_fields = (
        "new_local_low", "lower_low_stopped", "negative_return_magnitude_decreased",
        "close_above_previous", "positive_current_bar", "close_in_upper_half",
        "lower_wick_rejection", "rank_deterioration_stopped", "rank_improvement",
        "session_low_distance_recovery",
    )
    stabilization: dict[str, Any] = {}
    for field in stabilization_fields:
        yes = [row for row in laggard if row["stabilization"].get(field) is True]
        no = [row for row in laggard if row["stabilization"].get(field) is False]
        unavailable = [row for row in laggard if row["stabilization"].get(field) is None]
        stabilization[field] = {
            "yes": summarize_observations(yes),
            "no": summarize_observations(no),
            "unavailable": summarize_observations(unavailable),
        }
    blind_laggards = [row for row in rows if row.get("prior_percentile_short") is not None
                      and row["prior_percentile_short"] <= DEFAULT_MR_CONFIG.laggard_percentile
                      and row["prior_short_return"] < 0]
    relative_recovery = [row for row in rows if row.get("prior_percentile_short") is not None
                         and row.get("rank_delta_points") is not None
                         and row["prior_percentile_short"] <= DEFAULT_MR_CONFIG.laggard_percentile
                         and row["prior_short_return"] < 0
                         and row["rank_delta_points"] >= DEFAULT_MR_CONFIG.minimum_rank_recovery_points]
    combined_recovery = [row for row in relative_recovery if row["current_bar_return"] > 0]
    stabilized_recovery = [row for row in combined_recovery
                           if row["stabilization"].get("lower_low_stopped") is True]

    tod = {label: summarize_observations([row for row in rows if row["time_of_day"] == label])
           for _, _, label in TIME_BUCKETS}
    market = {label: summarize_observations([row for row in rows if row["market"] == label])
              for label in ("KOSPI", "KOSDAQ")}
    liquidity_rows = []
    for row in rows:
        pct = float(row["turnover_percentile"])
        row_copy = dict(row)
        row_copy["liquidity_bucket"] = (
            "0-20%" if pct < 20 else "20-40%" if pct < 40 else "40-60%" if pct < 60
            else "60-80%" if pct < 80 else "80-100%"
        )
        liquidity_rows.append(row_copy)
    liquidity = _slice_summary(liquidity_rows, "liquidity_bucket")
    extreme_bins = {"session_return_le_-8%": [], "-8_to_-5%": [], "-5_to_-3%": [], "above_-3%": []}
    for row in rows:
        value = row.get("session_open_return")
        if value is None:
            continue
        if value <= -0.08:
            extreme_bins["session_return_le_-8%"].append(row)
        elif value <= -0.05:
            extreme_bins["-8_to_-5%"].append(row)
        elif value <= -0.03:
            extreme_bins["-5_to_-3%"].append(row)
        else:
            extreme_bins["above_-3%"].append(row)
    time_counts = {key: value for key, value in sorted(participant_counts.items())}
    participant_values = list(time_counts.values())
    return {
        "schema_version": 1,
        "interval": interval_name,
        "clean_data_only": clean_only,
        "observation_count": len(rows),
        "timestamp_count": len(participant_counts),
        "minimum_participants": minimum_participants,
        "participant_count_by_timestamp": time_counts,
        "participant_count_summary": {
            "minimum": min(participant_values) if participant_values else None,
            "median": statistics.median(participant_values) if participant_values else None,
            "maximum": max(participant_values) if participant_values else None,
        },
        "rank_bucket_study": rank_summary,
        "stabilization_among_prior_laggards": stabilization,
        "rank_recovery": {
            "blind_laggard_baseline": summarize_observations(blind_laggards),
            "relative_only": summarize_observations(relative_recovery),
            "relative_plus_absolute_reversal": summarize_observations(combined_recovery),
            "stabilized_relative_plus_absolute_reversal": summarize_observations(stabilized_recovery),
            "relative_rule": "prior rank <= 20 percentile and current rank improves >= 10 percentile points",
            "absolute_confirmation": "current completed bar return > 0",
        },
        "time_of_day": tod,
        "market_split": market,
        "traded_value_buckets": liquidity,
        "extreme_session_losers": {label: summarize_observations(items) for label, items in extreme_bins.items()},
    }


def _variant_signals(rows: list[dict[str, Any]], variant: MeanReversionVariant,
                     config: MeanReversionConfig) -> dict[tuple[str, datetime], Signal]:
    signals: dict[tuple[str, datetime], Signal] = {}
    for row in rows:
        if (row.get("prior_percentile_short") is None or row.get("rank_delta_points") is None
                or row.get("stop_price") is None):
            continue
        observation = ReversalObservation(
            timestamp=datetime.fromisoformat(row["timestamp"]),
            symbol=row["symbol"],
            close_price=float(row["close"] if "close" in row else row["_close"]),
            stop_price=float(row["stop_price"]),
            prior_percentile=float(row["prior_percentile_short"]),
            prior_short_return=float(row["prior_short_return"]),
            rank_delta_points=float(row["rank_delta_points"]),
            current_bar_return=float(row["current_bar_return"]),
            close_location=row["close_location"],
            turnover_percentile=float(row["turnover_percentile"]),
            freshness_minutes=float(row["freshness_minutes"]),
            completeness=float(row["completeness"]),
            stopped_making_local_low=bool(row["stabilization"]["lower_low_stopped"]),
        )
        signal = evaluate_mean_reversion(observation, variant=variant, config=config)
        if signal is not None:
            signals[(row["symbol"], observation.timestamp)] = signal
    return signals


def simulate_variant(
    rows: list[dict[str, Any]],
    bars_by_symbol: dict[str, tuple[ResampledResearchBar, ...] | list[ResampledResearchBar]],
    *,
    variant: MeanReversionVariant,
    config: MeanReversionConfig = DEFAULT_MR_CONFIG,
    cost_multiplier: float = 1.0,
    clean_only: bool = False,
) -> dict[str, Any]:
    if cost_multiplier not in {1.0, 1.5, 2.0}:
        raise ValueError("Phase 6 cost multipliers are fixed at 1.0x, 1.5x, and 2.0x")
    signals = _variant_signals(rows, variant, config)
    feature_by_key = {(row["symbol"], datetime.fromisoformat(row["timestamp"])): row for row in rows}
    trades: list[dict[str, Any]] = []
    for symbol in sorted(bars_by_symbol):
        usable = sorted((item for item in bars_by_symbol[symbol] if _usable(item, clean_only=clean_only)),
                        key=lambda item: item.bar.time)
        by_day: dict[date, list[ResampledResearchBar]] = defaultdict(list)
        for item in usable:
            by_day[item.bar.time.date()].append(item)
        flat_after: datetime | None = None
        for timestamp in sorted(ts for sym, ts in signals if sym == symbol):
            signal = signals[(symbol, timestamp)]
            if flat_after is not None and timestamp < flat_after:
                continue
            future = [item for item in by_day.get(timestamp.date(), []) if item.bar.time > timestamp]
            if not future:
                continue
            entry_bar = future[0].bar
            if entry_bar.open <= float(signal.stop_price or 0):
                continue
            entry_reference = entry_bar.open
            entry_fill = BASE_COST.buy_fill_price(entry_reference, cost_multiplier)
            stop = float(signal.stop_price or 0)
            window = future[:config.max_holding_bars]
            exit_bar = window[-1].bar
            exit_reference = exit_bar.close
            exit_reason = "SEGMENT_END" if len(window) < config.max_holding_bars else "TIME_EXIT"
            held = len(window)
            for index, item in enumerate(window, start=1):
                bar = item.bar
                if bar.low <= stop:
                    exit_bar = bar
                    exit_reference = min(bar.open, stop)
                    exit_reason = "STOP"
                    held = index
                    break
            exit_fill = BASE_COST.sell_fill_price(exit_reference, cost_multiplier)
            gross = exit_reference - entry_reference
            buy_fee = entry_fill * BASE_COST.broker_fee_rate * cost_multiplier
            sell_fee = exit_fill * BASE_COST.broker_fee_rate * cost_multiplier
            sell_tax = exit_fill * BASE_COST.sell_tax_rate * cost_multiplier
            slippage = (entry_fill - entry_reference) + (exit_reference - exit_fill)
            net = gross - buy_fee - sell_fee - sell_tax - slippage
            feature = feature_by_key.get((symbol, timestamp), {})
            trades.append({
                "variant": variant.value,
                "symbol": symbol,
                "signal_time": timestamp.isoformat(),
                "entry_time": entry_bar.time.isoformat(),
                "exit_time": exit_bar.time.isoformat(),
                "entry_delay_intervals": (entry_bar.time - timestamp).total_seconds() / 900,
                "entry_reference": entry_reference,
                "entry_fill": entry_fill,
                "stop_price": stop,
                "exit_reference": exit_reference,
                "exit_fill": exit_fill,
                "quantity": 1,
                "market": feature.get("market", "UNKNOWN"),
                "price_bucket": _price_bucket(entry_reference),
                "turnover_percentile": feature.get("turnover_percentile"),
                "bars_held": held,
                "exit_reason": exit_reason,
                "gross_pnl_per_share_krw": gross,
                "fee_krw": buy_fee + sell_fee,
                "sell_tax_krw": sell_tax,
                "slippage_krw": slippage,
                "net_pnl_per_share_krw": net,
                "gross_return_pct": gross / entry_reference * 100,
                "net_return_pct": net / entry_reference * 100,
                "time_of_day": feature_by_key.get((symbol, timestamp), {}).get("time_of_day", "UNKNOWN"),
            })
            flat_after = exit_bar.time
    summary = _trade_summary(trades, variant=variant, cost_multiplier=cost_multiplier)
    summary["trades_detail"] = trades
    return summary


def _top_share(trades: list[dict[str, Any]], key: str) -> tuple[str | None, float | None]:
    profit: dict[str, float] = defaultdict(float)
    for row in trades:
        if row["net_pnl_per_share_krw"] > 0:
            value = str(row[key])
            if key == "signal_time":
                value = value[:10]
            profit[value] += float(row["net_pnl_per_share_krw"])
    total = sum(profit.values())
    if total <= 0 or not profit:
        return None, None
    leader = max(profit, key=profit.get)
    return leader, profit[leader] / total


def _trade_summary(trades: list[dict[str, Any]], *, variant: MeanReversionVariant,
                   cost_multiplier: float) -> dict[str, Any]:
    gross = [float(item["gross_return_pct"]) for item in trades]
    net = [float(item["net_return_pct"]) for item in trades]
    positive_net = [value for value in net if value > 0]
    negative_net = [value for value in net if value < 0]
    pf_unbounded = not negative_net and bool(positive_net)
    symbol, symbol_share = _top_share(trades, "symbol")
    day, day_share = _top_share(trades, "signal_time")
    time_bucket, time_share = _top_share(trades, "time_of_day")
    return {
        "variant": variant.value,
        "cost_multiplier": cost_multiplier,
        "closed_trades": len(trades),
        "gross_expectancy_pct": _mean_or_none(gross),
        "net_expectancy_pct": _mean_or_none(net),
        "gross_total_return_pct": sum(gross),
        "net_total_return_pct": sum(net),
        "win_rate_net": sum(value > 0 for value in net) / len(net) if net else None,
        "profit_factor_net": sum(positive_net) / abs(sum(negative_net)) if negative_net else None,
        "profit_factor_unbounded": pf_unbounded,
        "top_profitable_symbol": symbol,
        "top_symbol_positive_pnl_share": symbol_share,
        "top_profitable_signal_time": day,
        "top_day_positive_pnl_share": day_share,
        "top_time_bucket": time_bucket,
        "top_time_bucket_positive_pnl_share": time_share,
        "distinct_trade_symbols": len({row["symbol"] for row in trades}),
        "distinct_trade_days": len({row["signal_time"][:10] for row in trades}),
        "distinct_time_buckets": len({row["time_of_day"] for row in trades}),
        "trades_detail": [],
    }


def _trade_bucket_summary(trades: list[dict[str, Any]], *, key: str,
                          variant: MeanReversionVariant) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade.get(key, "UNKNOWN"))].append(trade)
    return {label: _trade_summary(items, variant=variant, cost_multiplier=1.0)
            for label, items in sorted(grouped.items())}


def promotion_gate(primary: dict[str, Any], clean: dict[str, Any], *,
                   minimum_research_breadth_ok: bool = True) -> dict[str, Any]:
    concentration_ok = (
        (primary.get("top_symbol_positive_pnl_share") or 0) <= 0.5
        and (primary.get("top_day_positive_pnl_share") or 0) <= 0.5
        and (primary.get("top_time_bucket_positive_pnl_share") or 0) <= 0.6
        and int(primary.get("distinct_time_buckets") or 0) >= 3
    )
    checks = {
        "minimum_45_complete_symbol_breadth": minimum_research_breadth_ok,
        "minimum_30_development_trades": int(primary.get("closed_trades", 0)) >= 30,
        "gross_expectancy_positive": (primary.get("gross_expectancy_pct") or 0) > 0,
        "net_expectancy_positive": (primary.get("net_expectancy_pct") or 0) > 0,
        "profit_factor_above_one": (primary.get("profit_factor_net") or 0) > 1 or primary.get("profit_factor_unbounded") is True,
        "clean_data_same_positive_direction": (clean.get("gross_expectancy_pct") or 0) > 0
            and (clean.get("net_expectancy_pct") or 0) > 0,
        "not_dominated_by_symbol_day_or_time": concentration_ok,
    }
    return {"status": "PASS" if all(checks.values()) else "REJECTED", "checks": checks}


def _anatomy_supports_variant(primary_anatomy: dict[str, Any], clean_anatomy: dict[str, Any],
                              *, variant: MeanReversionVariant) -> tuple[bool, dict[str, Any]]:
    primary_recovery = primary_anatomy["rank_recovery"]
    clean_recovery = clean_anatomy["rank_recovery"]
    key = ("relative_plus_absolute_reversal" if variant is MeanReversionVariant.MR_A
           else "stabilized_relative_plus_absolute_reversal")
    primary = primary_recovery[key]
    clean = clean_recovery[key]
    baseline = primary_recovery["blind_laggard_baseline"]
    baseline_return = baseline.get("mean_fwd_4_return")
    primary_return = primary.get("mean_fwd_4_return")
    clean_return = clean.get("mean_fwd_4_return")
    checks = {
        "development_sample_at_least_30": primary.get("n_fwd_4", 0) >= 30,
        "development_forward_4bar_positive": primary_return is not None and primary_return > 0,
        "development_beats_blind_laggard": baseline_return is not None and primary_return is not None
            and primary_return > baseline_return,
        "clean_data_same_positive_direction": clean.get("n_fwd_4", 0) >= 30
            and clean_return is not None and clean_return > 0,
    }
    return all(checks.values()), {"status": "SUPPORTED" if all(checks.values()) else "NOT_SUPPORTED",
                                  "sample": primary, "clean_sample": clean,
                                  "blind_laggard_baseline": baseline, "checks": checks}


def _portfolio_replay(rows: list[dict[str, Any]],
                      bars_by_symbol: dict[str, tuple[ResampledResearchBar, ...] | list[ResampledResearchBar]],
                      *, variant: MeanReversionVariant,
                      config: MeanReversionConfig = DEFAULT_MR_CONFIG) -> dict[str, Any]:
    signals = _variant_signals(rows, variant, config)
    usable_bars: dict[str, list[Bar]] = {
        symbol: [item.bar for item in sorted(items, key=lambda item: item.bar.time) if _usable(item, clean_only=False)]
        for symbol, items in bars_by_symbol.items()
    }
    usable_bars = {symbol: bars for symbol, bars in usable_bars.items() if bars}

    def signal_fn(symbol: str, history: list[Bar]) -> Signal:
        current = history[-1]
        found = signals.get((symbol, current.time))
        if found is not None:
            return found
        return Signal(timestamp=current.time, symbol=symbol, strategy_id=f"mean_reversion_{variant.value.lower().replace('-', '_')}",
                      decision=Decision.HOLD, reason_codes=("NO_PHASE6_SIGNAL",), reference_price=current.close)

    if not usable_bars:
        return {"status": "NOT_RUN_NO_BARS"}
    result = run_portfolio_backtest(
        usable_bars, signal_fn,
        starting_cash_krw=100_000,
        capital_cap_krw=100_000,
        order_cap_krw=20_000,
        risk_per_trade_pct=0.25,
        max_concurrent_positions=2,
        cost_model=BASE_COST,
    )
    metrics = calculate_metrics(result)
    entry_decisions = sum(decision.decision == Decision.ENTER for decision in result.decisions)
    traded_symbols = {trade.symbol for trade in result.trades}
    timeline = sorted({bar.time for values in usable_bars.values() for bar in values})
    utilization_samples = []
    for timestamp in timeline:
        invested = sum(trade.entry_price * trade.quantity for trade in result.trades
                       if trade.entry_time <= timestamp <= trade.exit_time)
        utilization_samples.append(invested / 100_000 * 100)
    profit_factor: float | str | None = metrics.profit_factor
    if isinstance(profit_factor, float) and math.isinf(profit_factor):
        profit_factor = "INF"
    return {
        "status": "RUN",
        "capital_krw": 100_000,
        "order_cap_krw": 20_000,
        "risk_pct": 0.25,
        "max_positions": 2,
        "whole_shares": True,
        "fills": len(result.trades),
        "rejections_estimate": max(0, entry_decisions - len(result.trades)),
        "entry_decisions": entry_decisions,
        "net_pnl_krw": sum(trade.net_pnl_krw for trade in result.trades),
        "return_pct": metrics.total_return_pct,
        "profit_factor": profit_factor,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "average_capital_utilization_pct": statistics.mean(utilization_samples) if utilization_samples else 0.0,
        "peak_capital_utilization_pct": max(utilization_samples, default=0.0),
        "idle_cash_krw": result.ending_cash_krw,
        "traded_symbols": sorted(traded_symbols),
        "orders_api_calls": 0,
        "paper_or_live_trading": False,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


def _write_resampled_quality_gz(
    path: Path,
    bars_by_symbol: dict[str, list[ResampledResearchBar]],
    *,
    interval_minutes: int,
    market_by_symbol: dict[str, str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as stream:
        for symbol in sorted(bars_by_symbol):
            for value in sorted(bars_by_symbol[symbol], key=lambda item: item.bar.time):
                bar = value.bar
                stream.write(json.dumps({
                    "symbol": symbol,
                    "market": market_by_symbol.get(symbol, "UNKNOWN"),
                    "timestamp": bar.time.isoformat(),
                    "interval_minutes": interval_minutes,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "turnover_krw": value.turnover_krw,
                    "observed_minute_count": value.observed_minute_count,
                    "expected_minute_count": value.expected_minute_count,
                    "first_observed_minute": value.first_observed_minute.isoformat(),
                    "last_observed_minute": value.last_observed_minute.isoformat(),
                    "freshness_minutes": value.freshness_minutes if math.isfinite(value.freshness_minutes) else None,
                    "completeness": value.completeness,
                    "rank_population_eligible": _usable(value, clean_only=False),
                    "high_freshness_completeness_eligible": _usable(value, clean_only=True),
                }, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


def _artifact_entry(path: Path) -> dict[str, Any]:
    return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def run_phase6_acquisition(*, min_request_interval: float = 0.4) -> dict[str, Any]:
    """Resume the established safe-period collector and keep credentials in its settings loader."""
    settings = Settings.from_env()
    if settings.trading_mode != "shadow" or settings.live_trading_enabled:
        raise ValueError("historical market-data collection requires shadow mode with live trading disabled")
    if not settings.kis_app_key or not settings.kis_app_secret:
        raise ValueError("KIS market-data credentials are unavailable")
    from krx_trader.research.phase5_backfill import run_phase5_backfill

    state = run_phase5_backfill(
        status_output_path=Path("runtime/research/phase6/phase6-acquisition-manifest.json"),
        min_request_interval=min_request_interval,
    )
    dataset = load_safe_dataset()
    return annotate_acquisition_manifest(state, dataset=dataset, min_request_interval=min_request_interval)


def annotate_acquisition_manifest(
    state: dict[str, Any], *, dataset: SafeDataset, min_request_interval: float = 0.4,
    manifest_path: Path = Path("runtime/research/phase6/phase6-acquisition-manifest.json"),
) -> dict[str, Any]:
    state.update({
        "git_sha": current_git_sha(),
        "provider": "KIS official read-only historical OHLCV",
        "period": {"start": SAFE_START.isoformat(), "end": SAFE_END.isoformat(), "sessions": len(dataset.sessions)},
        "timestamp_convention": "Asia/Seoul; minute timestamps label bar start; closing auction excluded",
        "dataset_hash": dataset.dataset_hash,
        "partition_index_hash": dataset.dataset_hash,
        "cohort_hash": dataset.cohort_hash,
        "cost_assumptions": asdict(BASE_COST),
        "holdout_partitions_opened": 0,
        "acquisition_config_hash": canonical_sha256({
            "cohort": list(dataset.cohort_symbols),
            "safe_start": SAFE_START.isoformat(),
            "safe_end": SAFE_END.isoformat(),
            "min_request_interval_seconds": min_request_interval,
        }),
    })
    _write_json(manifest_path, state)
    return state


def _build_interval_data(dataset: SafeDataset, interval: int) -> dict[str, list[ResampledResearchBar]]:
    return {
        symbol: resample_with_quality(bars, interval)
        for symbol, bars in dataset.bars_by_symbol.items()
    }


def _interval_quality_summary(bars_by_symbol: dict[str, list[ResampledResearchBar]], interval: int) -> dict[str, Any]:
    all_rows = [row for values in bars_by_symbol.values() for row in values]
    valid = [row for row in all_rows if _usable(row, clean_only=False)]
    clean = [row for row in all_rows if _usable(row, clean_only=True)]
    return {
        "interval_minutes": interval,
        "resampled_bar_count": len(all_rows),
        "fresh_trade_bar_count": len(valid),
        "high_freshness_completeness_count": len(clean),
        "stale_or_zero_turnover_excluded": len(all_rows) - len(valid),
        "freshness_minutes": {
            "median": statistics.median(row.freshness_minutes for row in valid) if valid else None,
            "p95": sorted(row.freshness_minutes for row in valid)[min(len(valid) - 1, math.ceil(0.95 * len(valid)) - 1)] if valid else None,
            "maximum": max((row.freshness_minutes for row in valid), default=None),
        },
        "completeness": {
            "median": statistics.median(row.completeness for row in valid) if valid else None,
            "p10": sorted(row.completeness for row in valid)[max(0, math.floor(0.1 * len(valid)))] if valid else None,
            "high_freshness_and_completeness_threshold": DEFAULT_MR_CONFIG.minimum_clean_completeness,
        },
        "no_forward_fill": True,
        "missing_current_interval_symbols_excluded_from_rank": True,
    }


def _with_metadata(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    return {**metadata, **payload}


def _summary_gate_candidates(development: dict[str, Any], secondary: dict[str, Any]) -> list[str]:
    passed: list[str] = []
    for variant in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B):
        dev = development.get(variant.value, {})
        sec = secondary.get(variant.value, {})
        if dev.get("gate", {}).get("status") != "PASS":
            continue
        primary = sec.get("primary", {})
        clean = sec.get("clean", {})
        sec_gate = promotion_gate(primary, clean)
        sec["gate"] = sec_gate
        same_direction = (
            (primary.get("gross_expectancy_pct") or 0) > 0
            and (primary.get("net_expectancy_pct") or 0) > 0
            and sec_gate["status"] == "PASS"
        )
        sec["same_direction_as_development"] = same_direction
        if same_direction:
            passed.append(variant.value)
    return passed


def run_external_validation(
    *,
    candidate_variants: list[str],
    cohort_symbols: list[str],
    market_by_symbol: dict[str, str],
    minimum_participants: int,
    config: MeanReversionConfig,
    output_dir: Path,
    development: dict[str, Any],
    secondary: dict[str, Any],
    strategy_config_hash: str,
) -> dict[str, Any]:
    """Open the external block only after a recorded candidate and real freeze commit exist."""
    prereg_path = output_dir / "phase6-preregistration.json"
    if not prereg_path.is_file():
        raise ValueError("external validation requires the Phase 6 preregistration artifact")
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    freeze_sha = str(prereg.get("strategy_freeze_commit", ""))
    if prereg.get("candidate_variants") != candidate_variants:
        raise ValueError("external candidate list does not match preregistration")
    if prereg.get("strategy_config_hash") != strategy_config_hash:
        raise ValueError("external strategy configuration differs from preregistration")
    assert_external_evidence_allowed(candidate_preregistered=bool(candidate_variants),
                                     strategy_freeze_commit=freeze_sha,
                                     sessions=[EXTERNAL_START, EXTERNAL_END])
    subprocess.run(["git", "cat-file", "-e", f"{freeze_sha}^{{commit}}"], check=True,
                   capture_output=True, text=True)

    settings = Settings.from_env()
    if settings.trading_mode != "shadow" or settings.live_trading_enabled:
        raise ValueError("external historical collection requires shadow mode with live trading disabled")
    if not settings.kis_app_key or not settings.kis_app_secret:
        return {"status": "NOT_AVAILABLE", "reason": "KIS_MARKET_DATA_CREDENTIALS_UNAVAILABLE",
                "external_block_opened": False, "strategy_freeze_commit": freeze_sha}

    from krx_trader.kis.auth import TokenManager
    from krx_trader.kis.rest import KisRestClient
    from krx_trader.kis.transport import UrllibTransport

    transport = UrllibTransport()
    manager = TokenManager(settings.kis_app_key, settings.kis_app_secret, transport)
    client = KisRestClient(settings.kis_app_key, settings.kis_app_secret, manager, transport,
                           min_request_interval=0.4)
    cache = ParquetBarCache()
    daily_by_symbol: dict[str, list[date]] = {}
    daily_failures: dict[str, str] = {}
    for index, symbol in enumerate(cohort_symbols, start=1):
        try:
            daily_bars = client.get_daily_bars(symbol, EXTERNAL_START, EXTERNAL_END)
            daily_by_symbol[symbol] = sorted({bar.time.date() for bar in daily_bars})
            print(f"External daily range {index}/{len(cohort_symbols)}: {symbol} ({len(daily_by_symbol[symbol])} sessions)", flush=True)
        except (OSError, RuntimeError, ValueError) as exc:
            daily_by_symbol[symbol] = []
            daily_failures[symbol] = type(exc).__name__
    sessions = sorted({session for values in daily_by_symbol.values() for session in values})
    assert_external_evidence_allowed(candidate_preregistered=True, strategy_freeze_commit=freeze_sha,
                                     sessions=sessions)
    if not sessions:
        return {"status": "NOT_AVAILABLE", "reason": "NO_EXTERNAL_DAILY_SESSIONS",
                "external_block_opened": True, "daily_failures": daily_failures,
                "strategy_freeze_commit": freeze_sha}

    acquisition_path = output_dir / "external-acquisition-manifest.json"
    acquisition: dict[str, Any] = {
        "schema_version": 1,
        "artifact": "phase6-external-acquisition-manifest",
        "git_sha": current_git_sha(),
        "strategy_freeze_commit": freeze_sha,
        "strategy_config_hash": strategy_config_hash,
        "provider": "KIS official read-only historical OHLCV",
        "period": {"start": EXTERNAL_START.isoformat(), "end": EXTERNAL_END.isoformat(),
                   "session_count": len(sessions)},
        "timestamp_convention": "Asia/Seoul; minute bar_start; closing auction excluded",
        "target_symbols": cohort_symbols,
        "daily_failures": daily_failures,
        "symbol_results": {},
        "holdout_partitions_opened": 0,
        "external_block_opened": True,
        "status": "RUNNING",
    }
    _write_json(acquisition_path, acquisition)
    partition_hashes: dict[str, str] = {}
    external_bars: dict[str, tuple[Bar, ...]] = {}
    complete_symbols: list[str] = []
    row_count = 0
    missing_minute_slots = 0
    for index, symbol in enumerate(cohort_symbols, start=1):
        symbol_bars: list[Bar] = []
        failures: dict[str, str] = {}
        succeeded = 0
        requested_sessions = daily_by_symbol.get(symbol, [])
        for session in requested_sessions:
            assert_external_evidence_allowed(candidate_preregistered=True,
                                             strategy_freeze_commit=freeze_sha, sessions=[session])
            path = cache.partition_path("minute", symbol, "1m", session)
            metadata_path = path.with_suffix(".metadata.json")
            try:
                if path.is_file() and metadata_path.is_file():
                    bars = cache.load("minute", symbol, "1m", session)
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                else:
                    bars = client.get_minute_bars(symbol, session)
                    if not bars:
                        failures[session.isoformat()] = "EMPTY_MINUTE_RESPONSE"
                        continue
                    cache.save(bars, kind="minute", symbol=symbol, interval="1m", market=market_by_symbol[symbol],
                               source="KIS read-only external historical minute OHLCV; timestamp=bar_start; preregistered Phase 6",
                               session_date=session)
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if inspect_bars(bars):
                    failures[session.isoformat()] = "DATA_QUALITY_FAILURE"
                    continue
                symbol_bars.extend(bars)
                succeeded += 1
                row_count += len(bars)
                missing_minute_slots += max(0, EXPECTED_MINUTES_PER_SESSION - len(bars))
                partition_hashes[f"{symbol}:{session.isoformat()}"] = str(metadata["sha256"])
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                failures[session.isoformat()] = type(exc).__name__
        if requested_sessions and succeeded == len(requested_sessions) and not failures:
            complete_symbols.append(symbol)
            external_bars[symbol] = tuple(sorted(symbol_bars, key=lambda bar: bar.time))
        acquisition["symbol_results"][symbol] = {
            "status": "SUCCESS" if requested_sessions and succeeded == len(requested_sessions) and not failures else "PARTIAL",
            "daily_sessions": len(requested_sessions),
            "minute_partitions": succeeded,
            "failed_sessions": len(failures),
            "failures": failures,
        }
        acquisition["completed_symbols"] = len(complete_symbols)
        acquisition["partition_count"] = len(partition_hashes)
        acquisition["dataset_hash"] = canonical_sha256(partition_hashes)
        acquisition["partition_index_hash"] = acquisition["dataset_hash"]
        acquisition["cohort_hash"] = canonical_sha256(cohort_symbols)
        _write_json(acquisition_path, acquisition)
        print(f"External minute range {index}/{len(cohort_symbols)}: {symbol} ({succeeded}/{len(requested_sessions)} sessions)", flush=True)
    acquisition["status"] = "COMPLETE" if len(complete_symbols) == len(cohort_symbols) else "PARTIAL"
    acquisition["finished_at"] = datetime.now(KST).isoformat()
    _write_json(acquisition_path, acquisition)

    external_dataset_hash = canonical_sha256(partition_hashes)
    metadata = {
        "git_sha": current_git_sha(),
        "strategy_freeze_commit": freeze_sha,
        "dataset_hash": external_dataset_hash,
        "partition_index_hash": external_dataset_hash,
        "cohort_hash": canonical_sha256(cohort_symbols),
        "strategy_config_hash": strategy_config_hash,
        "provider": "KIS official read-only historical OHLCV",
        "period": {"start": EXTERNAL_START.isoformat(), "end": EXTERNAL_END.isoformat(),
                   "sessions": len(sessions)},
        "timestamp_convention": "Asia/Seoul; minute bar_start; completed interval end",
        "cost_assumptions": {"baseline": asdict(BASE_COST), "stress_multipliers": [1.0, 1.5, 2.0]},
        "holdout_partitions_opened": 0,
        "external_block_opened": True,
    }
    external_dq = {
        **metadata,
        "complete_symbols": complete_symbols,
        "complete_symbol_count": len(complete_symbols),
        "target_symbol_count": len(cohort_symbols),
        "kospi_complete": sum(market_by_symbol[symbol] == "KOSPI" for symbol in complete_symbols),
        "kosdaq_complete": sum(market_by_symbol[symbol] == "KOSDAQ" for symbol in complete_symbols),
        "verified_partitions": len(partition_hashes),
        "rows": row_count,
        "expected_minute_absences": missing_minute_slots,
        "absence_cause": "UNKNOWN; no synthetic minute bars",
        "daily_failures": daily_failures,
    }
    _write_json(output_dir / "external-validation-dq.json", external_dq)
    if len(complete_symbols) < 1:
        return {"status": "NOT_AVAILABLE", "reason": "NO_COMPLETE_EXTERNAL_SYMBOLS",
                "external_block_opened": True, "data_quality": external_dq,
                "strategy_freeze_commit": freeze_sha}

    external_bars_15 = {symbol: resample_with_quality(values, 15) for symbol, values in external_bars.items()}
    external_clean_bars = {symbol: [row for row in values if _usable(row, clean_only=True)]
                           for symbol, values in external_bars_15.items()}
    snapshots, participant_counts = build_point_in_time_snapshots(
        external_bars_15, interval_minutes=15, market_by_symbol=market_by_symbol,
        minimum_participants=minimum_participants,
    )
    clean_snapshots, clean_participants = build_point_in_time_snapshots(
        external_clean_bars, interval_minutes=15, market_by_symbol=market_by_symbol,
        minimum_participants=minimum_participants, clean_only=True,
    )
    snapshots = attach_forward_outcomes(snapshots, external_bars_15, interval_minutes=15)
    clean_snapshots = attach_forward_outcomes(clean_snapshots, external_clean_bars,
                                              interval_minutes=15, clean_only=True)
    _write_jsonl_gz(output_dir / "mean-reversion-external-observations-15m.jsonl.gz", snapshots)
    results: dict[str, Any] = {}
    passed: list[str] = []
    for variant_text in candidate_variants:
        variant = MeanReversionVariant(variant_text)
        primary = simulate_variant(snapshots, external_bars_15, variant=variant, config=config)
        clean = simulate_variant(clean_snapshots, external_clean_bars, variant=variant,
                                 config=config, clean_only=True)
        stress_15 = simulate_variant(snapshots, external_bars_15, variant=variant,
                                    config=config, cost_multiplier=1.5)
        stress_20 = simulate_variant(snapshots, external_bars_15, variant=variant,
                                    config=config, cost_multiplier=2.0)
        dev = development[variant_text]["primary"]
        sec = secondary[variant_text]["primary"]
        checks = {
            "minimum_45_complete_symbols": len(complete_symbols) >= 45,
            "closed_trades_at_least_30": primary["closed_trades"] >= 30,
            "gross_expectancy_positive": (primary.get("gross_expectancy_pct") or 0) > 0,
            "net_expectancy_positive": (primary.get("net_expectancy_pct") or 0) > 0,
            "profit_factor_above_one": (primary.get("profit_factor_net") or 0) > 1 or primary.get("profit_factor_unbounded") is True,
            "1_5x_cost_expectancy_positive": (stress_15.get("net_expectancy_pct") or 0) > 0,
            "same_direction_as_development_secondary": all(
                (sample.get("gross_expectancy_pct") or 0) > 0 and (sample.get("net_expectancy_pct") or 0) > 0
                for sample in (dev, sec)),
            "not_dominated_by_symbol_day": (primary.get("top_symbol_positive_pnl_share") or 0) <= 0.5
                and (primary.get("top_day_positive_pnl_share") or 0) <= 0.5,
            "clean_data_sensitivity_same_direction": (clean.get("gross_expectancy_pct") or 0) > 0
                and (clean.get("net_expectancy_pct") or 0) > 0,
        }
        passed_variant = all(checks.values())
        if passed_variant:
            passed.append(variant_text)
        results[variant_text] = {
            "primary": primary,
            "clean": clean,
            "cost_stress_1_5x": stress_15,
            "cost_stress_2_0x": stress_20,
            "checks": checks,
            "status": "PASS" if passed_variant else "FAIL",
        }
    return {
        **metadata,
        "status": "PASS" if passed else "FAIL",
        "candidate_variants": candidate_variants,
        "passing_variants": passed,
        "data_quality": external_dq,
        "participant_count_summary": {
            "timestamps": len(participant_counts),
            "median": statistics.median(participant_counts.values()) if participant_counts else None,
            "high_freshness_completeness_timestamps": len(clean_participants),
        },
        "results": results,
        "rules_changed_after_external": False,
    }


def run_phase6(
    *,
    output_dir: Path = Path("runtime/research/phase6"),
    cohort_manifest_path: Path = Path("runtime/research/phase4/cohort-manifest.json"),
    split_manifest_path: Path = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json"),
    cache_root: Path = Path("data"),
    config: MeanReversionConfig = DEFAULT_MR_CONFIG,
) -> dict[str, Any]:
    """Run safe-period DQ, Development anatomy, frozen candidate gates, and eligible diagnostics."""
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = load_safe_dataset(cohort_manifest_path=cohort_manifest_path,
                                split_manifest_path=split_manifest_path, cache_root=cache_root)
    acquisition_path = output_dir / "phase6-acquisition-manifest.json"
    if acquisition_path.is_file():
        acquisition_state = json.loads(acquisition_path.read_text(encoding="utf-8"))
        annotate_acquisition_manifest(acquisition_state, dataset=dataset, manifest_path=acquisition_path)
    source_sha = current_git_sha()
    min_participants = max(15, math.ceil(len(dataset.complete_symbols) * 0.5))
    strategy_config = {
        "strategy": {variant.value: preregistration_mr_config(variant, config)
                     for variant in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B)},
        "primary_interval_minutes": 15,
        "diagnostic_interval_minutes": 30,
        "minimum_participants": min_participants,
        "minimum_complete_cohort_breadth": 45,
        "maximum_complete_cohort_breadth": 60,
        "bar_validity": {"freshness_minutes_max": 1.0, "positive_turnover_required": True},
        "clean_sensitivity": {"minimum_completeness": config.minimum_clean_completeness,
                              "freshness_minutes_max": config.maximum_freshness_minutes},
        "rank_recovery_points": config.minimum_rank_recovery_points,
        "costs": asdict(BASE_COST),
    }
    config_hash = canonical_sha256(strategy_config)
    metadata = {
        "git_sha": source_sha,
        "dataset_hash": dataset.dataset_hash,
        "partition_index_hash": dataset.dataset_hash,
        "cohort_hash": dataset.cohort_hash,
        "strategy_config_hash": config_hash,
        "provider": "KIS official read-only historical OHLCV",
        "period": {"safe_start": SAFE_START.isoformat(), "safe_end": SAFE_END.isoformat(),
                   "development": [dataset.development_sessions[0].isoformat(), dataset.development_sessions[-1].isoformat()],
                   "secondary_diagnostic": [dataset.secondary_sessions[0].isoformat(), dataset.secondary_sessions[-1].isoformat()]},
        "timestamp_convention": "Asia/Seoul; minute bar_start; resampled timestamp is completed interval end",
        "cost_assumptions": {"baseline": asdict(BASE_COST), "stress_multipliers": [1.0, 1.5, 2.0]},
        "holdout_integrity": {"holdout_partitions_opened": 0, "state": "LOCKED_NOT_EVALUATED"},
        "created_at": datetime.now(KST).isoformat(),
    }
    _write_json(output_dir / "phase6-dq.json", _with_metadata(dataset.dq, metadata))

    snapshot = {
        "schema_version": 1,
        "artifact": "phase6-safe-dataset-snapshot",
        **metadata,
        "complete_symbols": list(dataset.complete_symbols),
        "partial_symbols": list(dataset.partial_symbols),
        "not_acquired_symbols": list(dataset.missing_symbols),
        "partition_hashes": dataset.partition_hashes,
        "bars_loaded_for_complete_symbols": sum(len(rows) for rows in dataset.bars_by_symbol.values()),
    }
    _write_json(output_dir / "phase6-dataset-snapshot.json", snapshot)

    by_symbol_15 = _build_interval_data(dataset, 15)
    by_symbol_30 = _build_interval_data(dataset, 30)
    quality = {"15m": _interval_quality_summary(by_symbol_15, 15),
               "30m": _interval_quality_summary(by_symbol_30, 30)}
    _write_json(output_dir / "phase6-resampled-dq.json", _with_metadata(quality, metadata))
    _write_resampled_quality_gz(output_dir / "phase6-resampled-bars-15m.jsonl.gz", by_symbol_15,
                                interval_minutes=15, market_by_symbol=dataset.market_by_symbol)
    _write_resampled_quality_gz(output_dir / "phase6-resampled-bars-30m.jsonl.gz", by_symbol_30,
                                interval_minutes=30, market_by_symbol=dataset.market_by_symbol)

    dev_dates = set(dataset.development_sessions)
    secondary_dates = set(dataset.secondary_sessions)
    dev_bars: dict[str, list[ResampledResearchBar]] = {
        symbol: [row for row in values if row.bar.time.date() in dev_dates]
        for symbol, values in by_symbol_15.items()
    }
    dev_bars_30 = {symbol: [row for row in values if row.bar.time.date() in dev_dates]
                   for symbol, values in by_symbol_30.items()}
    dev_bars = {symbol: values for symbol, values in dev_bars.items() if values}
    dev_bars_30 = {symbol: values for symbol, values in dev_bars_30.items() if values}
    clean_dev_bars = {symbol: [row for row in values if _usable(row, clean_only=True)]
                      for symbol, values in dev_bars.items()}
    clean_dev_bars = {symbol: values for symbol, values in clean_dev_bars.items() if values}
    clean_dev_bars_30 = {symbol: [row for row in values if _usable(row, clean_only=True)]
                         for symbol, values in dev_bars_30.items()}
    clean_dev_bars_30 = {symbol: values for symbol, values in clean_dev_bars_30.items() if values}

    primary_rows, participant_counts = build_point_in_time_snapshots(
        dev_bars, interval_minutes=15, market_by_symbol=dataset.market_by_symbol,
        minimum_participants=min_participants,
    )
    clean_primary_rows, clean_participant_counts = build_point_in_time_snapshots(
        clean_dev_bars, interval_minutes=15, market_by_symbol=dataset.market_by_symbol,
        minimum_participants=min_participants, clean_only=True,
    )
    primary_rows = attach_forward_outcomes(primary_rows, dev_bars, interval_minutes=15)
    clean_primary_rows = attach_forward_outcomes(clean_primary_rows, clean_dev_bars,
                                                interval_minutes=15, clean_only=True)

    diagnostic_rows, diagnostic_participants = build_point_in_time_snapshots(
        dev_bars_30, interval_minutes=30, market_by_symbol=dataset.market_by_symbol,
        minimum_participants=min_participants,
    )
    clean_diagnostic_rows, clean_diagnostic_participants = build_point_in_time_snapshots(
        clean_dev_bars_30, interval_minutes=30, market_by_symbol=dataset.market_by_symbol,
        minimum_participants=min_participants, clean_only=True,
    )
    diagnostic_rows = attach_forward_outcomes(diagnostic_rows, dev_bars_30, interval_minutes=30)
    clean_diagnostic_rows = attach_forward_outcomes(clean_diagnostic_rows, clean_dev_bars_30,
                                                    interval_minutes=30, clean_only=True)

    anatomy15 = {
        "all_valid_data": anatomy_summary(primary_rows, interval_name="15m", minimum_participants=min_participants,
                                          participant_counts=participant_counts, clean_only=False),
        "high_freshness_completeness": anatomy_summary(clean_primary_rows, interval_name="15m",
                                                        minimum_participants=min_participants,
                                                        participant_counts=clean_participant_counts, clean_only=True),
    }
    anatomy30 = {
        "all_valid_data": anatomy_summary(diagnostic_rows, interval_name="30m",
                                          minimum_participants=min_participants,
                                          participant_counts=diagnostic_participants, clean_only=False),
        "high_freshness_completeness": anatomy_summary(clean_diagnostic_rows, interval_name="30m",
                                                        minimum_participants=min_participants,
                                                        participant_counts=clean_diagnostic_participants, clean_only=True),
    }
    _write_json(output_dir / "mean-reversion-anatomy-15m.json", _with_metadata(anatomy15, metadata))
    _write_json(output_dir / "mean-reversion-anatomy-30m.json", _with_metadata(anatomy30, metadata))
    _write_jsonl_gz(output_dir / "mean-reversion-observations-15m.jsonl.gz", primary_rows)
    _write_jsonl_gz(output_dir / "mean-reversion-observations-15m-clean.jsonl.gz", clean_primary_rows)
    _write_jsonl_gz(output_dir / "mean-reversion-observations-30m.jsonl.gz", diagnostic_rows)
    _write_jsonl_gz(output_dir / "mean-reversion-observations-30m-clean.jsonl.gz", clean_diagnostic_rows)

    hypotheses = {
        "schema_version": 1,
        "artifact": "mr-hypotheses",
        **metadata,
        "primary_interval": "15m",
        "secondary_diagnostic_interval": "30m",
        "blind_laggard_0_10": anatomy15["all_valid_data"]["rank_bucket_study"].get("0-10%"),
        "blind_laggard_10_20": anatomy15["all_valid_data"]["rank_bucket_study"].get("10-20%"),
        "rank_recovery": anatomy15["all_valid_data"]["rank_recovery"],
        "rank_recovery_clean": anatomy15["high_freshness_completeness"]["rank_recovery"],
        "stabilization_comparison": anatomy15["all_valid_data"]["stabilization_among_prior_laggards"],
        "stabilization_comparison_clean": anatomy15["high_freshness_completeness"]["stabilization_among_prior_laggards"],
        "rule_policy": "No thresholds were selected by Secondary Diagnostic; MR rules fixed at 20th rank, 10-point recovery, and >=20th traded-value percentile.",
    }
    variant_support: dict[str, Any] = {}
    for variant in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B):
        supported, detail = _anatomy_supports_variant(
            anatomy15["all_valid_data"], anatomy15["high_freshness_completeness"], variant=variant
        )
        variant_support[variant.value] = detail
        detail["created_for_development_test"] = supported
    created_variants = {variant for variant in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B)
                        if variant_support[variant.value]["created_for_development_test"]}
    hypotheses["variant_support"] = variant_support
    hypotheses["created_variants"] = [variant.value for variant in sorted(created_variants, key=lambda item: item.value)]
    _write_json(output_dir / "mr-hypotheses.json", hypotheses)

    development: dict[str, Any] = {}
    cost_stress: dict[str, Any] = {}
    for variant in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B):
        if variant not in created_variants:
            development[variant.value] = {
                "status": "NOT_CREATED_ANATOMY_GATE",
                "reason": "Development anatomy did not show a sufficiently sampled positive rebound that beat blind laggard selection on clean data.",
                "gate": {"status": "INSUFFICIENT", "checks": variant_support[variant.value]["checks"]},
            }
            for multiplier in (1.0, 1.5, 2.0):
                cost_stress[f"{variant.value}_{multiplier}x"] = {"status": "NOT_RUN_ANATOMY_GATE",
                                                                   "cost_multiplier": multiplier}
            continue
        primary = simulate_variant(primary_rows, dev_bars, variant=variant, config=config)
        clean = simulate_variant(clean_primary_rows, clean_dev_bars, variant=variant,
                                 config=config, clean_only=True)
        gate = promotion_gate(primary, clean, minimum_research_breadth_ok=len(dataset.complete_symbols) >= 45)
        if (not gate["checks"]["minimum_45_complete_symbol_breadth"]
                or not gate["checks"]["minimum_30_development_trades"]):
            gate["status"] = "INSUFFICIENT"
        development[variant.value] = {"primary": primary, "clean": clean, "gate": gate}
        for multiplier in (1.0, 1.5, 2.0):
            result = simulate_variant(primary_rows, dev_bars, variant=variant,
                                      config=config, cost_multiplier=multiplier)
            cost_stress[f"{variant.value}_{multiplier}x"] = {
                key: result[key] for key in ("closed_trades", "gross_expectancy_pct", "net_expectancy_pct",
                                             "profit_factor_net", "cost_multiplier")
            }
    for variant in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B):
        name = variant.value.lower()
        _write_json(output_dir / f"{name}-development.json",
                    _with_metadata(development[variant.value], metadata))
    _write_json(output_dir / "cost-stress.json", _with_metadata(cost_stress, metadata))

    secondary: dict[str, Any] = {}
    development_survivors = [variant for variant in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B)
                             if development[variant.value]["gate"]["status"] == "PASS"]
    if development_survivors:
        secondary_bars = {symbol: [row for row in values if row.bar.time.date() in secondary_dates]
                          for symbol, values in by_symbol_15.items()}
        secondary_bars = {symbol: values for symbol, values in secondary_bars.items() if values}
        clean_secondary_bars = {symbol: [row for row in values if _usable(row, clean_only=True)]
                                 for symbol, values in secondary_bars.items()}
        clean_secondary_bars = {symbol: values for symbol, values in clean_secondary_bars.items() if values}
        sec_rows, _ = build_point_in_time_snapshots(
            secondary_bars, interval_minutes=15, market_by_symbol=dataset.market_by_symbol,
            minimum_participants=min_participants,
        )
        sec_clean_rows, _ = build_point_in_time_snapshots(
            clean_secondary_bars, interval_minutes=15, market_by_symbol=dataset.market_by_symbol,
            minimum_participants=min_participants, clean_only=True,
        )
        sec_rows = attach_forward_outcomes(sec_rows, secondary_bars, interval_minutes=15)
        sec_clean_rows = attach_forward_outcomes(sec_clean_rows, clean_secondary_bars,
                                                 interval_minutes=15, clean_only=True)
        for variant in development_survivors:
            primary = simulate_variant(sec_rows, secondary_bars, variant=variant, config=config)
            clean = simulate_variant(sec_clean_rows, clean_secondary_bars, variant=variant,
                                     config=config, clean_only=True)
            secondary[variant.value] = {"primary": primary, "clean": clean,
                                        "same_direction_as_development": False}
    secondary_survivors = _summary_gate_candidates(development, secondary)
    secondary_payload = {
        **metadata,
        "status": "RUN" if development_survivors else "NOT_RUN_NO_DEVELOPMENT_SURVIVORS",
        "development_survivors": [variant.value for variant in development_survivors],
        "secondary_survivors": secondary_survivors,
        "rules_changed_after_development": False,
        "results": secondary,
        "holdout_integrity": {"holdout_partitions_opened": 0, "state": "LOCKED_NOT_EVALUATED"},
    }
    _write_json(output_dir / "secondary-diagnostic.json", secondary_payload)

    preregistration: dict[str, Any] | None = None
    external: dict[str, Any] = {"status": "NOT_AVAILABLE", "reason": "NO_SECONDARY_SURVIVING_CANDIDATE",
                                "external_block_opened": False}
    portfolio: dict[str, Any] = {variant.value: {"status": "NOT_RUN_NO_CANDIDATE"}
                                 for variant in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B)}
    stability: dict[str, Any] = {"status": "NOT_RUN_NO_CANDIDATE"}
    if secondary_survivors:
        preregistration = {
            "schema_version": 1,
            "artifact": "phase6-preregistration",
            **metadata,
            "candidate_variants": secondary_survivors,
            "exact_rules": {variant: preregistration_mr_config(MeanReversionVariant(variant), config)
                            for variant in secondary_survivors},
            "cohort": {"symbols": list(dataset.complete_symbols), "markets": dataset.dq["cohort"],
                       "cohort_hash": dataset.cohort_hash},
            "data_semantics": strategy_config["bar_validity"],
            "entry": "signal after completed bar; next valid same-session bar open",
            "exit": "structural low of signal and prior two valid bars; max 10 same-session bars",
            "risk": {"signal_level_quantity": 1, "portfolio_capital_krw": 100000,
                     "order_cap_krw": 20000, "risk_pct": 0.25, "max_positions": 2},
            "costs": {"baseline": asdict(BASE_COST), "stress_multipliers": [1.0, 1.5, 2.0]},
            "external_block": {"start": EXTERNAL_START.isoformat(), "end": EXTERNAL_END.isoformat()},
            "strategy_freeze_commit": source_sha,
            "external_evidence_allowed": True,
        }
        _write_json(output_dir / "phase6-preregistration.json", preregistration)
        external = run_external_validation(
            candidate_variants=secondary_survivors,
            cohort_symbols=list(dataset.complete_symbols),
            market_by_symbol=dataset.market_by_symbol,
            minimum_participants=min_participants,
            config=config,
            output_dir=output_dir,
            development=development,
            secondary=secondary,
            strategy_config_hash=config_hash,
        )
        for variant_text in secondary_survivors:
            variant = MeanReversionVariant(variant_text)
            rows = primary_rows
            portfolio[variant_text] = _portfolio_replay(rows, dev_bars, variant=variant, config=config)
        month_slices: dict[str, Any] = {}
        for month in (4, 5, 6):
            month_dates = {day for day in dataset.development_sessions if day.month == month}
            month_rows = [row for row in primary_rows if date.fromisoformat(row["session_date"]) in month_dates]
            month_bars = {symbol: [item for item in values if item.bar.time.date() in month_dates]
                          for symbol, values in dev_bars.items()}
            month_bars = {symbol: values for symbol, values in month_bars.items() if values}
            month_slices[f"2026-{month:02d}"] = {
                variant: simulate_variant(month_rows, month_bars, variant=MeanReversionVariant(variant), config=config)
                for variant in secondary_survivors
            }
        stability = {"status": "RUN", "chronological_development_months": month_slices,
                     "time_of_day": {
                         variant: _trade_bucket_summary(development[variant]["primary"]["trades_detail"],
                                                        key="time_of_day",
                                                        variant=MeanReversionVariant(variant))
                         for variant in secondary_survivors
                     },
                     "price_bucket": {
                         variant: _trade_bucket_summary(development[variant]["primary"]["trades_detail"],
                                                        key="price_bucket",
                                                        variant=MeanReversionVariant(variant))
                         for variant in secondary_survivors
                     },
                     "traded_value_bucket": {
                         variant: _trade_bucket_summary(development[variant]["primary"]["trades_detail"],
                                                        key="turnover_percentile",
                                                        variant=MeanReversionVariant(variant))
                         for variant in secondary_survivors
                     },
                     "market": {
                         variant: _trade_bucket_summary(development[variant]["primary"]["trades_detail"],
                                                        key="market",
                                                        variant=MeanReversionVariant(variant))
                         for variant in secondary_survivors
                     },
                     "symbol_and_day_concentration": {
                         variant: {key: development[variant]["primary"].get(key)
                                   for key in ("top_profitable_symbol", "top_symbol_positive_pnl_share",
                                               "top_profitable_signal_time", "top_day_positive_pnl_share",
                                               "distinct_trade_symbols", "distinct_trade_days")}
                         for variant in secondary_survivors
                     }}

    _write_json(output_dir / "external-validation.json", _with_metadata(external, metadata))
    _write_json(output_dir / "100k-feasibility.json", _with_metadata(portfolio, metadata))
    _write_json(output_dir / "phase6-stability.json", _with_metadata(stability, metadata))

    blind_bottom_20 = summarize_observations([row for row in primary_rows if row["percentile_short"] < 20])
    bottom_next = blind_bottom_20.get("mean_fwd_1_return")
    bottom_4 = blind_bottom_20.get("mean_fwd_4_return")
    recovery = anatomy15["all_valid_data"]["rank_recovery"]
    blind_recovery_baseline = recovery["blind_laggard_baseline"]
    recovery_baseline_4 = blind_recovery_baseline.get("mean_fwd_4_return")
    recovery_combined = recovery["stabilized_relative_plus_absolute_reversal"]
    recovery_combined_4 = recovery_combined.get("mean_fwd_4_return")
    clean_bottom_20 = summarize_observations([row for row in clean_primary_rows if row["percentile_short"] < 20])
    clean_bottom_4 = clean_bottom_20.get("mean_fwd_4_return")
    enough = int(blind_bottom_20.get("n_fwd_4", 0)) >= 30
    q1 = "INSUFFICIENT" if not enough or bottom_4 is None else (
        "YES" if bottom_next is not None and bottom_next > 0 and bottom_4 > 0 else
        "NO" if bottom_next is not None and bottom_next <= 0 and bottom_4 <= 0 else "WEAK"
    )
    q2 = "INSUFFICIENT" if recovery_baseline_4 is None or recovery_combined_4 is None \
        or recovery_combined.get("n_fwd_4", 0) < 30 else (
        "YES" if recovery_combined_4 > recovery_baseline_4 and recovery_combined_4 > 0 else "NO"
    )
    q3 = "INSUFFICIENT" if clean_bottom_4 is None or bottom_4 is None or clean_bottom_20.get("n_fwd_4", 0) < 30 else (
        "YES" if clean_bottom_4 > 0 and bottom_4 > 0 else "NO"
    )
    q4 = any((development[v.value]["primary"].get("gross_expectancy_pct") or 0) > 0
             for v in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B))
    q5 = any((development[v.value]["primary"].get("net_expectancy_pct") or 0) > 0
             for v in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B))
    q6 = any(secondary.get(v, {}).get("same_direction_as_development") for v in secondary)
    q7 = "YES" if external.get("status") == "PASS" else ("NOT_AVAILABLE" if external.get("status") == "NOT_AVAILABLE" else "NO")
    portfolio_metrics = [portfolio.get(variant, {}) for variant in secondary_survivors]
    if not secondary_survivors or not any(item.get("fills", 0) > 0 for item in portfolio_metrics):
        q8 = "NO"
    elif any(item.get("net_pnl_krw", 0) > 0 and
             (item.get("profit_factor") == "INF" or (item.get("profit_factor") or 0) > 1)
             for item in portfolio_metrics):
        q8 = "YES"
    else:
        q8 = "CONSTRAINED"
    q9 = "YES" if q7 == "YES" and q8 == "YES" else "NO"
    variant_verdicts: dict[str, str] = {}
    for variant in (MeanReversionVariant.MR_A, MeanReversionVariant.MR_B):
        item = development.get(variant.value, {})
        gate_status = item.get("gate", {}).get("status")
        if variant not in created_variants or gate_status == "INSUFFICIENT":
            variant_verdicts[variant.value] = "INSUFFICIENT"
        elif gate_status == "PASS":
            variant_verdicts[variant.value] = (
                "VALIDATION_CANDIDATE" if variant.value in secondary_survivors else "RESEARCH_CANDIDATE"
            )
        else:
            variant_verdicts[variant.value] = "REJECTED"
    family_verdict = (
        "VALIDATION_CANDIDATE" if secondary_survivors else
        "RESEARCH_CANDIDATE" if development_survivors else
        "INSUFFICIENT" if not created_variants or len(dataset.complete_symbols) < 45 else
        "REJECTED"
    )
    summary = {
        "schema_version": 1,
        "artifact": "phase6-summary",
        **metadata,
        "verdicts": {
            "PHASE6_DATA_ACQUISITION": "COMPLETE" if len(dataset.complete_symbols) == 60 else
                "PARTIAL" if len(dataset.complete_symbols) >= 45 else "INSUFFICIENT",
            "MEAN_REVERSION_ANATOMY": "INSUFFICIENT_BREADTH" if len(dataset.complete_symbols) < 45 else "COMPLETE",
            "MR_A": variant_verdicts[MeanReversionVariant.MR_A.value],
            "MR_B": variant_verdicts[MeanReversionVariant.MR_B.value],
            "MEAN_REVERSION_FAMILY": family_verdict,
            "EXTERNAL_VALIDATION": external.get("status", "NOT_AVAILABLE"),
            "100K": q8,
            "SHADOW_NEXT_SESSION": q9,
            "ALPHA": "UNPROVEN",
            "LIVE": "DISABLED",
        },
        "questions": {
            "Q1_sample": blind_bottom_20,
            "Q2_baseline_sample": blind_recovery_baseline,
            "Q2_stabilized_recovery_sample": recovery_combined,
            "Q1_bottom_ranked_stocks_rebound": q1,
            "Q2_stabilization_and_rank_recovery_outperform_blind_laggards": q2,
            "Q3_effect_persists_on_clean_data": q3,
            "Q4_any_variant_positive_gross_expectancy": "YES" if q4 else "NO",
            "Q5_any_variant_positive_net_expectancy": "YES" if q5 else "NO",
            "Q6_secondary_preserves_direction": "YES" if q6 else "NO",
            "Q7_untouched_external_evidence": q7,
            "Q8_100k_whole_share_feasibility": q8,
            "Q9_future_shadow_candidate": q9,
        },
        "cohort": {"complete": len(dataset.complete_symbols), "partial": len(dataset.partial_symbols),
                   "not_acquired": len(dataset.missing_symbols), "complete_symbols": list(dataset.complete_symbols),
                   "holdout_partitions_opened": 0},
        "metric_definitions": {"forward_return": "next valid same-session bar open to 1/2/4/8 subsequent valid bar close",
                               "cost_adjusted_trade": "one share; actual next valid bar open; structural low or max 10 bars"},
        "development_candidates": development,
        "secondary_diagnostic": secondary_payload,
        "external_validation": external,
        "portfolio_100k": portfolio,
    }
    _write_json(output_dir / "phase6-summary.json", summary)

    artifact_names = [
        "phase6-acquisition-manifest.json", "phase6-dq.json", "phase6-dataset-snapshot.json",
        "phase6-resampled-dq.json", "mean-reversion-anatomy-15m.json",
        "phase6-resampled-bars-15m.jsonl.gz", "phase6-resampled-bars-30m.jsonl.gz",
        "mean-reversion-anatomy-30m.json", "mean-reversion-observations-15m.jsonl.gz",
        "mean-reversion-observations-15m-clean.jsonl.gz", "mean-reversion-observations-30m.jsonl.gz",
        "mean-reversion-observations-30m-clean.jsonl.gz", "mr-hypotheses.json", "mr-a-development.json",
        "mr-b-development.json", "secondary-diagnostic.json", "external-validation.json",
        "external-validation-dq.json", "external-acquisition-manifest.json",
        "mean-reversion-external-observations-15m.jsonl.gz", "cost-stress.json",
        "100k-feasibility.json", "phase6-stability.json", "phase6-summary.json",
    ]
    if preregistration is not None:
        artifact_names.append("phase6-preregistration.json")
    artifact_files = [output_dir / name for name in artifact_names if (output_dir / name).is_file()]
    index = {
        "schema_version": 1,
        "artifact": "phase6-artifact-index",
        **metadata,
        "files": [_artifact_entry(path) for path in sorted(artifact_files)],
        "source_dataset_hash": dataset.dataset_hash,
        "source_partition_count": len(dataset.partition_hashes),
        "cohort_hash": dataset.cohort_hash,
        "strategy_config_hash": config_hash,
        "holdout_partitions_opened": 0,
    }
    _write_json(output_dir / "phase6-artifact-index.json", index)
    summary["artifact_index_sha256"] = hashlib.sha256((output_dir / "phase6-artifact-index.json").read_bytes()).hexdigest()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 6 safe-period laggard rebound research")
    parser.add_argument("--acquire", action="store_true", help="resume safe-period KIS minute-data acquisition")
    parser.add_argument("--request-interval", type=float, default=0.4)
    args = parser.parse_args()
    try:
        if args.acquire:
            state = run_phase6_acquisition(min_request_interval=args.request_interval)
            completed = sum(item.get("status") == "SUCCESS" for item in state.get("symbol_results", {}).values())
            print(json.dumps({"acquisition_status": state.get("status"), "completed_symbols": completed,
                              "target_symbols": state.get("total_symbols"),
                              "holdout_dates_requested": state.get("holdout_integrity", {}).get("holdout_dates_requested")},
                             ensure_ascii=False))
        else:
            summary = run_phase6()
            print(json.dumps({"verdicts": summary["verdicts"], "questions": summary["questions"],
                              "holdout_partitions_opened": 0}, ensure_ascii=False))
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"Phase 6 failed: {type(exc).__name__}; details redacted")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

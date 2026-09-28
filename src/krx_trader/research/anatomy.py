from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.metrics import calculate_metrics
from krx_trader.backtest.portfolio import run_portfolio_backtest
from krx_trader.data.resample import resample_session_minutes
from krx_trader.models import Bar, Decision, Signal

KST = ZoneInfo("Asia/Seoul")
SEGMENTS = ("development", "validation")
INTERVAL_MINUTES = {"15m": 15, "30m": 30}
FEATURE_SCHEMA_VERSION = "phase3-breakout-context-v2"
DEVELOPMENT_START = date(2026, 4, 17)
DEVELOPMENT_END = date(2026, 6, 30)
VALIDATION_START = date(2026, 7, 1)
VALIDATION_END = date(2026, 7, 27)
PHASE25_MANIFEST = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json")
DEFAULT_OUTPUT = Path("runtime/research/phase3")
BASELINE_ARTIFACT = "phase25-clean-{segment}-breakout-{interval}-signal-v1.json"
PRICE_BUCKETS = ((1_000, 10_000, "1k-10k"), (10_000, 20_000, "10k-20k"),
                 (20_000, 30_000, "20k-30k"), (30_000, 50_000, "30k-50k"))
TIME_BUCKETS = (
    (time(9, 0), time(9, 30), "09:00-09:30"),
    (time(9, 30), time(10, 0), "09:30-10:00"),
    (time(10, 0), time(11, 0), "10:00-11:00"),
    (time(11, 0), time(12, 0), "11:00-12:00"),
    (time(12, 0), time(13, 0), "12:00-13:00"),
    (time(13, 0), time(14, 0), "13:00-14:00"),
    (time(14, 0), time(15, 0), "14:00-15:00"),
    (time(15, 0), time(15, 31), "15:00+"),
)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def time_bucket(timestamp: datetime) -> str:
    local_time = timestamp.astimezone(KST).time().replace(tzinfo=None)
    for start, end, label in TIME_BUCKETS:
        if start <= local_time < end:
            return label
    return "OUTSIDE_REGULAR_SESSION"


def candle_features(bar: Bar) -> dict[str, float | None]:
    bar_range = bar.high - bar.low
    if bar_range <= 0:
        return {
            "breakout_bar_range_pct": 0.0,
            "body_ratio": 0.0,
            "upper_wick_ratio": 0.0,
            "lower_wick_ratio": 0.0,
            "close_location_value": None,
        }
    body_high = max(bar.open, bar.close)
    body_low = min(bar.open, bar.close)
    return {
        "breakout_bar_range_pct": bar_range / bar.close * 100 if bar.close else None,
        "body_ratio": abs(bar.close - bar.open) / bar_range,
        "upper_wick_ratio": (bar.high - body_high) / bar_range,
        "lower_wick_ratio": (body_low - bar.low) / bar_range,
        "close_location_value": (bar.close - bar.low) / bar_range,
    }


def extract_signal_context(
    bars_through_signal: list[Bar],
    *,
    previous_session_close: float | None,
    session_open: float | None,
    scanner_score: float | None = None,
    scanner_rank: int | None = None,
    prior_attempts: int = 0,
) -> dict[str, float | int | str | None]:
    """Calculate context using only bars through the completed signal bar."""
    if not bars_through_signal:
        raise ValueError("signal context requires at least one completed bar")
    bar = bars_through_signal[-1]
    history = bars_through_signal[:-1]
    recent = history[-20:]
    candle = candle_features(bar)
    prior_high = max((item.high for item in recent), default=None)
    prior_low = min((item.low for item in recent), default=None)
    prior_mean_volume = statistics.fmean(item.volume for item in recent) if recent else None
    short_mean_volume = statistics.fmean(item.volume for item in recent[-5:]) if recent else None
    prior_turnovers = [item.close * item.volume for item in recent]
    prior_mean_turnover = statistics.fmean(prior_turnovers) if prior_turnovers else None
    close_returns = [
        (right.close / left.close - 1) * 100
        for left, right in zip(bars_through_signal[-21:-1], bars_through_signal[-20:])
        if left.close > 0
    ]
    slope_values = bars_through_signal[-10:]
    if len(slope_values) >= 2:
        xs = list(range(len(slope_values)))
        mean_x = statistics.fmean(xs)
        mean_y = statistics.fmean(math.log(item.close) for item in slope_values if item.close > 0)
        denominator = sum((x - mean_x) ** 2 for x in xs)
        slope = (
            100 * sum((x - mean_x) * (math.log(item.close) - mean_y)
                      for x, item in zip(xs, slope_values)) / denominator
            if denominator and all(item.close > 0 for item in slope_values) else None
        )
    else:
        slope = None
    true_ranges: list[float] = []
    for index, current in enumerate(bars_through_signal[-14:]):
        absolute_index = len(bars_through_signal) - min(14, len(bars_through_signal)) + index
        prior_close = bars_through_signal[absolute_index - 1].close if absolute_index > 0 else current.open
        true_ranges.append(max(current.high - current.low, abs(current.high - prior_close),
                               abs(current.low - prior_close)))
    session_bars = [item for item in bars_through_signal
                    if item.time.astimezone(KST).date() == bar.time.astimezone(KST).date()]
    session_high_index = max(range(len(session_bars)), key=lambda index: session_bars[index].high)
    day_high = session_bars[session_high_index].high
    previous_close_change = (
        (bar.close / previous_session_close - 1) * 100
        if previous_session_close and previous_session_close > 0 else None
    )
    open_change = (bar.close / session_open - 1) * 100 if session_open and session_open > 0 else None
    return {
        "time_of_day": time_bucket(bar.time),
        "day_of_week": bar.time.astimezone(KST).strftime("%A"),
        "signal_price": bar.close,
        "previous_close": previous_session_close,
        "open_gap_pct": (
            (session_open / previous_session_close - 1) * 100
            if session_open and previous_session_close and previous_session_close > 0 else None
        ),
        "intraday_return_from_open_pct": open_change,
        "distance_from_day_open_pct": open_change,
        "distance_from_previous_close_pct": previous_close_change,
        "distance_from_recent_high_pct": (
            (bar.close / prior_high - 1) * 100 if prior_high and prior_high > 0 else None
        ),
        "breakout_distance_pct": (
            (bar.close / prior_high - 1) * 100 if prior_high and prior_high > 0 else None
        ),
        "breakout_bar_return_pct": (bar.close / bar.open - 1) * 100 if bar.open else None,
        **candle,
        "close": bar.close,
        "high": bar.high,
        "low": bar.low,
        "volume": bar.volume,
        "relative_volume": (
            bar.volume / prior_mean_volume if prior_mean_volume and prior_mean_volume > 0 else None
        ),
        "volume_vs_previous_bar": (
            bar.volume / history[-1].volume if history and history[-1].volume > 0 else None
        ),
        "volume_vs_recent_mean": (
            bar.volume / short_mean_volume if short_mean_volume and short_mean_volume > 0 else None
        ),
        "turnover_krw": bar.close * bar.volume,
        "relative_turnover": (
            bar.close * bar.volume / prior_mean_turnover
            if prior_mean_turnover and prior_mean_turnover > 0 else None
        ),
        "recent_realized_volatility_pct": statistics.pstdev(close_returns) if len(close_returns) >= 2 else None,
        "atr_like_range_pct": (
            statistics.fmean(true_ranges) / bar.close * 100 if true_ranges and bar.close else None
        ),
        "number_of_prior_breakout_attempts": prior_attempts,
        "bars_since_intraday_high": len(session_bars) - 1 - session_high_index,
        "distance_from_intraday_high_pct": (bar.close / day_high - 1) * 100 if day_high else None,
        "distance_from_recent_low_pct": (
            (bar.close / prior_low - 1) * 100 if prior_low and prior_low > 0 else None
        ),
        "trend_slope_pct_per_bar": slope,
        "scanner_score_prior_turnover_krw": scanner_score,
        "scanner_rank": scanner_rank,
        "market_index_direction": None,
        "market_index_short_term_return_pct": None,
        "regime_raw_volatility_pct": None,
        "market_index_context_status": "NOT_READ_HOLDOUT_ROW_GROUP_OVERLAPS",
        "price_bucket": next(
            (label for lower, upper, label in PRICE_BUCKETS if lower <= bar.close < upper),
            "OUTSIDE_1K_50K",
        ),
    }


def extract_context_at_index(
    bars: list[Bar],
    signal_index: int,
    *,
    previous_session_close: float | None = None,
    session_open: float | None = None,
    scanner_score: float | None = None,
    scanner_rank: int | None = None,
    prior_attempts: int = 0,
) -> dict[str, float | int | str | None]:
    if signal_index < 0 or signal_index >= len(bars):
        raise IndexError("signal index is outside the available bar sequence")
    return extract_signal_context(
        bars[:signal_index + 1], previous_session_close=previous_session_close,
        session_open=session_open, scanner_score=scanner_score,
        scanner_rank=scanner_rank, prior_attempts=prior_attempts,
    )


def _load_frozen_partitions(
    cache_root: Path, manifest_path: Path
) -> tuple[
    dict[str, list[Bar]], dict[str, dict[date, list[Bar]]], dict[str, str], set[tuple[str, date]]
]:
    """Load only development and validation partitions; never enumerate/read holdout files."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("fresh_holdout_state") != "LOCKED_NOT_EVALUATED":
        raise ValueError("Phase 3 requires Fresh Holdout to remain LOCKED_NOT_EVALUATED")
    splits = manifest.get("splits")
    if not isinstance(splits, dict):
        raise TypeError("frozen manifest has no split map")
    allowed_dates: set[date] = set()
    split_dates: dict[str, list[date]] = {}
    for segment in SEGMENTS:
        details = splits.get(segment)
        if not isinstance(details, dict) or not isinstance(details.get("sessions"), list):
            raise TypeError(f"frozen manifest is missing {segment} sessions")
        days = [date.fromisoformat(value) for value in details["sessions"]]
        if days != sorted(set(days)):
            raise ValueError(f"frozen {segment} sessions are invalid")
        split_dates[segment] = days
        allowed_dates.update(days)
    if not all(DEVELOPMENT_START <= day <= DEVELOPMENT_END for day in split_dates["development"]):
        raise ValueError("development partitions exceed the frozen Phase 3 development window")
    if not all(VALIDATION_START <= day <= VALIDATION_END for day in split_dates["validation"]):
        raise ValueError("validation partitions exceed the frozen Phase 3 validation window")
    if set(split_dates["development"]) & set(split_dates["validation"]):
        raise ValueError("development and validation sessions overlap")
    if not allowed_dates:
        raise ValueError("no permitted Phase 3 sessions")
    symbols = manifest.get("succeeded_symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("frozen manifest has no succeeded symbol list")
    expected: dict[tuple[str, str], str] = {}
    for record in manifest.get("partition_hashes", []):
        if not isinstance(record, str):
            raise TypeError("invalid frozen partition hash record")
        symbol, day_text, digest = record.split(":", 2)
        if date.fromisoformat(day_text) in allowed_dates:
            expected[(symbol, day_text)] = digest

    bars_by_symbol: dict[str, list[Bar]] = {}
    bars_by_session: dict[str, dict[date, list[Bar]]] = {}
    selected_hashes: dict[str, str] = {}
    incomplete_partitions: set[tuple[str, date]] = set()
    expected_minutes = manifest.get("quality", {}).get(
        "expected_minutes_per_session_adjusted_for_documented_kospi_halts", {}
    )
    for symbol in sorted(symbols):
        symbol_bars: list[Bar] = []
        daily: dict[date, list[Bar]] = {}
        for day in sorted(allowed_dates):
            key = (symbol, day.isoformat())
            expected_hash = expected.get(key)
            path = cache_root / "minute" / symbol / f"{day.isoformat()}.parquet"
            sidecar = path.with_suffix(".metadata.json")
            if expected_hash is None or not path.is_file() or not sidecar.is_file():
                raise ValueError(f"frozen permitted partition missing: {symbol}:{day.isoformat()}")
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != expected_hash or metadata.get("sha256") != expected_hash:
                raise ValueError(f"frozen permitted partition hash mismatch: {symbol}:{day.isoformat()}")
            rows = pq.read_table(path).to_pylist()
            session_bars = [
                Bar(datetime.fromisoformat(row["timestamp"]), float(row["open"]), float(row["high"]),
                    float(row["low"]), float(row["close"]), int(row["volume"]))
                for row in rows
            ]
            expected_for_day = expected_minutes.get(day.isoformat())
            if expected_for_day is not None and len(session_bars) != int(expected_for_day):
                incomplete_partitions.add((symbol, day))
            daily[day] = session_bars
            symbol_bars.extend(session_bars)
            selected_hashes[f"{symbol}:{day.isoformat()}"] = expected_hash
        symbol_bars.sort(key=lambda item: item.time)
        bars_by_symbol[symbol] = symbol_bars
        bars_by_session[symbol] = daily
    if len(selected_hashes) != len(symbols) * len(allowed_dates):
        raise ValueError("not all development/validation partitions were verified")
    return bars_by_symbol, bars_by_session, selected_hashes, incomplete_partitions


def _scanner_scores(
    bars_by_symbol: dict[str, list[Bar]], interval: str
) -> dict[tuple[str, datetime], tuple[float, int]]:
    per_time: dict[datetime, list[tuple[str, float]]] = defaultdict(list)
    for symbol, bars in bars_by_symbol.items():
        resampled = resample_session_minutes(bars, INTERVAL_MINUTES[interval])
        bars_by_symbol[symbol] = resampled
        for index, bar in enumerate(resampled):
            trailing = resampled[max(0, index - 19):index + 1]
            score = statistics.fmean(item.close * item.volume for item in trailing)
            per_time[bar.time].append((symbol, score))
    result: dict[tuple[str, datetime], tuple[float, int]] = {}
    for timestamp, values in per_time.items():
        for rank, (symbol, score) in enumerate(sorted(values, key=lambda item: (-item[1], item[0])), 1):
            result[(symbol, timestamp)] = (score, rank)
    return result


def _prior_attempts(bars: list[Bar], index: int, lookback: int = 20) -> int:
    current_day = bars[index].time.astimezone(KST).date()
    first = max(0, index - 20)
    count = 0
    for candidate in range(first, index):
        if bars[candidate].time.astimezone(KST).date() != current_day:
            continue
        previous = bars[max(0, candidate - lookback):candidate]
        if previous and bars[candidate].close > max(item.high for item in previous):
            count += 1
    return count


def _gap_near_event(bars: list[Bar], index: int, end_time: datetime, interval: int) -> bool:
    start_index = max(0, index - 20)
    relevant = [item for item in bars[start_index:] if item.time <= end_time]
    for left, right in pairwise(relevant):
        left_time = left.time.astimezone(KST)
        right_time = right.time.astimezone(KST)
        if left_time.date() == right_time.date() and (right_time - left_time) > timedelta(minutes=interval):
            return True
    return False


def _feature_values(
    signal: dict,
    interval_bars: dict[str, list[Bar]],
    minute_by_session: dict[str, dict[date, list[Bar]]],
    scanner: dict[tuple[str, datetime], tuple[float, int]],
    interval: str,
    incomplete_partitions: set[tuple[str, date]] | None = None,
) -> dict:
    symbol = str(signal["symbol"])
    timestamp = datetime.fromisoformat(signal["signal_time"]).astimezone(KST)
    bars = interval_bars[symbol]
    indices = {bar.time: index for index, bar in enumerate(bars)}
    index = indices.get(timestamp)
    if index is None:
        raise ValueError(f"signal bar missing from frozen resample: {symbol}:{timestamp.isoformat()}")
    session_day = timestamp.date()
    previous_days = [day for day in minute_by_session[symbol] if day < session_day]
    previous_close = None
    if previous_days:
        last_previous = minute_by_session[symbol][max(previous_days)]
        if last_previous:
            previous_close = last_previous[-1].close
    day_bars = minute_by_session[symbol].get(session_day, [])
    session_open = day_bars[0].open if day_bars else None
    score, rank = scanner.get((symbol, timestamp), (None, None))
    context = extract_context_at_index(
        bars,
        index,
        previous_session_close=previous_close,
        session_open=session_open,
        scanner_score=score,
        scanner_rank=rank,
        prior_attempts=_prior_attempts(bars, index),
    )
    # The quality flag may inspect outcome timestamps, but never enters a strategy filter.
    execution = signal.get("execution", {})
    signal_excursion = signal.get("signal_path_excursion", {})
    outcome_times = [
        datetime.fromisoformat(value).astimezone(KST)
        for value in (execution.get("exit_time"), signal_excursion.get("mfe_timestamp"),
                      signal_excursion.get("mae_timestamp")) if value
    ]
    outcome_end = max(outcome_times, default=timestamp)
    history_start = max(0, index - 20)
    relevant_bars = [bar for bar in bars[history_start:] if bar.time <= outcome_end]
    touched_days = {bar.time.astimezone(KST).date() for bar in relevant_bars}
    incomplete_window = any(
        (symbol, day) in (incomplete_partitions or set()) for day in touched_days
    )
    context["near_data_gap"] = incomplete_window or _gap_near_event(
        bars, index, outcome_end, INTERVAL_MINUTES[interval]
    )
    bar_indices = {bar.time: position for position, bar in enumerate(bars)}
    excursion_time = signal.get("signal_path_excursion", {}).get("mfe_timestamp")
    if excursion_time:
        mfe_index = bar_indices.get(datetime.fromisoformat(excursion_time).astimezone(KST))
        context["time_to_mfe_bars"] = max(0, mfe_index - index) if mfe_index is not None else None
    entry_time = execution.get("entry_time")
    exit_time = execution.get("exit_time")
    if execution.get("exit_reason") == "STOP" and entry_time and exit_time:
        entry_index = bar_indices.get(datetime.fromisoformat(entry_time).astimezone(KST))
        exit_index = bar_indices.get(datetime.fromisoformat(exit_time).astimezone(KST))
        context["time_to_failure_bars"] = (
            max(1, exit_index - entry_index) if entry_index is not None and exit_index is not None else None
        )
    next_index = index + 1
    next_bar = bars[next_index] if next_index < len(bars) else None
    context["outcome_next_bar_volume_drop"] = (
        next_bar.volume < bars[index].volume * .5
        if next_bar and next_bar.time.astimezone(KST).date() == timestamp.date() else None
    )
    context["market"] = signal.get("market", "UNKNOWN")
    context["signal_timestamp"] = timestamp.isoformat()
    context["interval"] = interval
    return context


def _effect_size(winners: list[float], losers: list[float]) -> float | None:
    if not winners or not losers:
        return None
    wins = sum(1 for left in winners for right in losers if left > right)
    ties = sum(1 for left in winners for right in losers if left == right)
    return (wins - (len(winners) * len(losers) - wins - ties)) / (len(winners) * len(losers))


def _feature_comparison(records: list[dict]) -> dict:
    closed = [row for row in records if row["outcome"] in {"WIN", "LOSS"}]
    names = [
        "relative_volume", "breakout_distance_pct", "body_ratio", "upper_wick_ratio",
        "distance_from_intraday_high_pct", "open_gap_pct", "intraday_return_from_open_pct",
        "turnover_krw", "relative_turnover", "recent_realized_volatility_pct",
        "atr_like_range_pct", "trend_slope_pct_per_bar", "scanner_rank",
        "distance_from_previous_close_pct", "close_location_value",
    ]
    output: dict[str, dict] = {}
    for name in names:
        wins = [float(row[name]) for row in closed if row["outcome"] == "WIN" and row.get(name) is not None]
        losses = [float(row[name]) for row in closed if row["outcome"] == "LOSS" and row.get(name) is not None]
        win_median, loss_median = _percentile(wins, 0.5), _percentile(losses, 0.5)
        output[name] = {
            "winner": {"n": len(wins), "p25": _percentile(wins, .25), "p50": win_median,
                       "p75": _percentile(wins, .75)},
            "loser": {"n": len(losses), "p25": _percentile(losses, .25), "p50": loss_median,
                      "p75": _percentile(losses, .75)},
            "median_difference_winner_minus_loser": (
                win_median - loss_median if win_median is not None and loss_median is not None else None
            ),
            "cliffs_delta": _effect_size(wins, losses),
        }
    return output


def _trade_metrics(records: list[dict]) -> dict:
    closed = [row for row in records if row["execution_status"] == "CLOSED"]
    unfilled = [row for row in records if row["execution_status"] == "UNFILLED"]
    opened = [row for row in records if row["execution_status"] == "OPEN_AT_SEGMENT_END"]
    net = [float(row["net_pnl_krw"]) for row in closed]
    winners = [value for value in net if value > 0]
    losers = [value for value in net if value < 0]
    mfe = [float(row["mfe_pct"]) for row in closed if row.get("mfe_pct") is not None]
    mae = [float(row["mae_pct"]) for row in closed if row.get("mae_pct") is not None]
    signal_mfe = [float(row["signal_mfe_pct"]) for row in records if row.get("signal_mfe_pct") is not None]
    signal_mae = [float(row["signal_mae_pct"]) for row in records if row.get("signal_mae_pct") is not None]
    gross = sum(float(row["gross_pnl_krw"] or 0) for row in closed)
    fees = sum(float(row["fee_krw"] or 0) for row in closed)
    tax = sum(float(row["tax_krw"] or 0) for row in closed)
    slippage = sum(float(row["slippage_krw"] or 0) for row in closed)
    return {
        "signals": len(records), "filled_closed": len(closed), "unfilled": len(unfilled),
        "open": len(opened), "wins": len(winners), "losses": len(losers),
        "scratch": sum(row["outcome"] == "SCRATCH" for row in records),
        "stop_outs": sum(row["exit_reason"] == "STOP" for row in closed),
        "time_exits": sum(row["exit_reason"] == "TIME_EXIT" for row in closed),
        "profit_factor": sum(winners) / abs(sum(losers)) if losers else None,
        "expectancy_krw": statistics.fmean(net) if net else None,
        "net_pnl_krw": sum(net), "gross_pnl_krw": gross,
        "fee_krw": fees, "tax_krw": tax, "slippage_krw": slippage,
        "cost_drag_krw": fees + tax + slippage,
        "mfe_pct": {"p25": _percentile(mfe, .25), "p50": _percentile(mfe, .5),
                     "p75": _percentile(mfe, .75)},
        "mae_pct": {"p25": _percentile(mae, .25), "p50": _percentile(mae, .5),
                     "p75": _percentile(mae, .75)},
        "signal_path_mfe_pct": {
            "p25": _percentile(signal_mfe, .25), "p50": _percentile(signal_mfe, .5),
            "p75": _percentile(signal_mfe, .75),
        },
        "signal_path_mae_pct": {
            "p25": _percentile(signal_mae, .25), "p50": _percentile(signal_mae, .5),
            "p75": _percentile(signal_mae, .75),
        },
        "false_breakout_rate_pct": (
            sum(bool(row.get("false_breakout")) for row in records) / len(records) * 100 if records else None
        ),
        "follow_through_rate_pct": {
            str(threshold): (
                sum(float(row.get("signal_mfe_pct") or 0) >= threshold for row in records) / len(records) * 100
                if records else None
            ) for threshold in (.5, 1.0, 2.0)
        },
        "average_bars_to_mfe": statistics.fmean(
            float(row["time_to_mfe_bars"]) for row in records if row.get("time_to_mfe_bars") is not None
        ) if any(row.get("time_to_mfe_bars") is not None for row in records) else None,
        "average_bars_to_failure": statistics.fmean(
            float(row["time_to_failure_bars"]) for row in closed if row.get("time_to_failure_bars") is not None
        ) if any(row.get("time_to_failure_bars") is not None for row in closed) else None,
        "median_bars_to_mfe": _percentile([
            float(row["time_to_mfe_bars"]) for row in records
            if row.get("time_to_mfe_bars") is not None
        ], .5),
        "median_bars_to_failure": _percentile([
            float(row["time_to_failure_bars"]) for row in closed
            if row.get("time_to_failure_bars") is not None
        ], .5),
        "overnight_exit_count": sum(
            bool(row.get("exit_time")) and bool(row.get("signal_timestamp")) and
            datetime.fromisoformat(row["exit_time"]).astimezone(KST).date() !=
            datetime.fromisoformat(row["signal_timestamp"]).astimezone(KST).date()
            for row in closed
        ),
        "overnight_exit_rate_pct": (
            sum(
                bool(row.get("exit_time")) and bool(row.get("signal_timestamp")) and
                datetime.fromisoformat(row["exit_time"]).astimezone(KST).date() !=
                datetime.fromisoformat(row["signal_timestamp"]).astimezone(KST).date()
                for row in closed
            ) / len(closed) * 100 if closed else None
        ),
    }


def _session_cluster_bootstrap(records: list[dict], *, seed: int = 20260928, replicates: int = 2000) -> dict:
    closed = [row for row in records if row["execution_status"] == "CLOSED"]
    by_session: dict[str, list[float]] = defaultdict(list)
    for row in closed:
        by_session[str(row["signal_timestamp"])[:10]].append(float(row["net_pnl_krw"]))
    sessions = sorted(by_session)
    if len(closed) < 30 or len(sessions) < 8:
        return {"status": "NOT_RUN_SMALL_SAMPLE", "closed_trades": len(closed), "sessions": len(sessions)}
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(replicates):
        sample = [rng.choice(sessions) for _ in sessions]
        values = [pnl for day in sample for pnl in by_session[day]]
        if values:
            estimates.append(statistics.fmean(values))
    return {
        "status": "DESCRIPTIVE_SESSION_CLUSTER_BOOTSTRAP",
        "closed_trades": len(closed),
        "sessions": len(sessions),
        "replicates": len(estimates),
        "seed": seed,
        "expectancy_ci_95_pctile_krw": [
            _percentile(estimates, .025), _percentile(estimates, .975)
        ],
        "warning": "descriptive only; fixed current-listing cohort with partial data",
    }


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    mean_left, mean_right = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    sum_left = sum((value - mean_left) ** 2 for value in left)
    sum_right = sum((value - mean_right) ** 2 for value in right)
    denominator = math.sqrt(sum_left * sum_right)
    return numerator / denominator if denominator else None


def _feature_correlations(records: list[dict]) -> dict:
    names = [
        "relative_volume", "relative_turnover", "turnover_krw", "breakout_bar_range_pct",
        "body_ratio", "upper_wick_ratio", "distance_from_intraday_high_pct",
        "intraday_return_from_open_pct",
    ]
    result: dict[str, dict] = {}
    for left_index, left_name in enumerate(names):
        result[left_name] = {}
        for right_name in names[left_index + 1:]:
            pairs = [(float(row[left_name]), float(row[right_name])) for row in records
                     if row.get(left_name) is not None and row.get(right_name) is not None]
            result[left_name][right_name] = {
                "n": len(pairs),
                "pearson_r": _pearson([a for a, _ in pairs], [b for _, b in pairs]),
            }
    return result


def _group_metrics(records: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        groups[str(row.get(key) if row.get(key) is not None else "UNKNOWN")].append(row)
    return {label: _trade_metrics(rows) for label, rows in sorted(groups.items())}


def _event_row(signal: dict, context: dict, segment: str, interval: str) -> dict:
    execution = signal.get("execution", {})
    status = execution.get("status", "UNKNOWN")
    net = execution.get("net_pnl_krw")
    outcome = "UNFILLED" if status == "UNFILLED" else "OPEN" if status == "OPEN_AT_SEGMENT_END" else (
        "WIN" if net is not None and float(net) > 0 else
        "LOSS" if net is not None and float(net) < 0 else "SCRATCH"
    )
    exit_reason = execution.get("exit_reason")
    if outcome in {"UNFILLED", "OPEN"}:
        exit_label = outcome
    elif exit_reason == "STOP":
        exit_label = "STOPPED"
    elif exit_reason == "TIME_EXIT":
        exit_label = "TIME_EXIT_WIN" if outcome == "WIN" else "TIME_EXIT_LOSS" if outcome == "LOSS" else "TIME_EXIT_SCRATCH"
    else:
        exit_label = str(exit_reason or "CLOSED")
    signal_excursion = signal.get("signal_path_excursion", {})
    signal_time = datetime.fromisoformat(signal["signal_time"]).astimezone(KST)
    mfe_time_text = signal_excursion.get("mfe_timestamp")
    time_to_mfe = None
    if mfe_time_text:
        mfe_time = datetime.fromisoformat(mfe_time_text).astimezone(KST)
        time_to_mfe = max(0, round((mfe_time - signal_time).total_seconds() / 60 / INTERVAL_MINUTES[interval]))
    entry_time_text = execution.get("entry_time")
    exit_time_text = execution.get("exit_time")
    time_to_failure = None
    if exit_reason == "STOP" and entry_time_text and exit_time_text:
        start = datetime.fromisoformat(entry_time_text).astimezone(KST)
        end = datetime.fromisoformat(exit_time_text).astimezone(KST)
        time_to_failure = max(1, round((end - start).total_seconds() / 60 / INTERVAL_MINUTES[interval]))
    row = {
        "signal_id": hashlib.sha256(
            f"{signal['symbol']}|{interval}|{signal['signal_time']}".encode()
        ).hexdigest()[:20],
        "split": segment,
        "symbol": signal["symbol"],
        "market": signal.get("market", "UNKNOWN"),
        "interval": interval,
        "signal_timestamp": signal["signal_time"],
        "signal_price": signal.get("signal_close"),
        "next_bar_open": execution.get("entry_reference"),
        "actual_entry_price": execution.get("entry_fill"),
        "stop_price": execution.get("stop_price", signal.get("stop_price")),
        "stop_distance_pct": execution.get("stop_distance_pct"),
        "holding_limit": 10,
        "exit_time": exit_time_text,
        "exit_price": execution.get("exit_fill"),
        "exit_reason": exit_reason,
        "exit_label": exit_label,
        "gross_pnl_krw": execution.get("gross_pnl_krw"),
        "fee_krw": execution.get("fees_krw"),
        "tax_krw": execution.get("tax_krw"),
        "slippage_krw": execution.get("slippage_krw"),
        "net_pnl_krw": net,
        "return_pct": execution.get("net_return_pct"),
        "mfe_pct": execution.get("mfe_pct"),
        "mae_pct": execution.get("mae_pct"),
        "signal_mfe_pct": signal_excursion.get("mfe_pct"),
        "signal_mae_pct": signal_excursion.get("mae_pct"),
        "signal_mfe_gt_1pct": float(signal_excursion.get("mfe_pct") or 0) >= 1,
        "signal_mfe_gt_2pct": float(signal_excursion.get("mfe_pct") or 0) >= 2,
        "signal_mae_lt_minus_1pct": float(signal_excursion.get("mae_pct") or 0) <= -1,
        "signal_mae_lt_minus_2pct": float(signal_excursion.get("mae_pct") or 0) <= -2,
        "time_to_mfe_bars": time_to_mfe,
        "time_to_failure_bars": time_to_failure,
        "execution_status": status,
        "outcome": outcome,
        "filled": status != "UNFILLED",
        "data_quality_status": signal_excursion.get("status", "UNKNOWN"),
    }
    row.update(context)
    return row


def _false_breakouts(records: list[dict], dev_thresholds: dict[str, float]) -> dict[str, dict]:
    losing = [row for row in records if row["outcome"] == "LOSS"]
    predicates = {
        "IMMEDIATE_REJECTION": lambda row: row["exit_reason"] == "STOP" and
            row.get("time_to_failure_bars") is not None and row["time_to_failure_bars"] <= 2,
        "NO_FOLLOW_THROUGH": lambda row: row.get("signal_mfe_pct") is not None and
            float(row["signal_mfe_pct"]) < dev_thresholds.get("signal_mfe_p25_pct", -math.inf),
        "LATE_ENTRY": lambda row: float(row.get("intraday_return_from_open_pct") or 0) >= dev_thresholds.get("late_return_p75_pct", math.inf),
        "WICK_BREAKOUT": lambda row: float(row.get("upper_wick_ratio") or 0) >= .5 or
            float(row.get("close_location_value") or 0) < .5,
        "VOLUME_SPIKE_FADE": lambda row: float(row.get("relative_volume") or 0) >= 3 and
            row.get("outcome_next_bar_volume_drop") is True,
        "LOW_LIQUIDITY": lambda row: float(row.get("turnover_krw") or 0) < dev_thresholds.get("turnover_p25_krw", -math.inf),
        "OVEREXTENDED": lambda row: float(row.get("intraday_return_from_open_pct") or 0) >= dev_thresholds.get("overextension_p90_pct", math.inf),
        "REPEATED_BREAKOUT": lambda row: int(row.get("number_of_prior_breakout_attempts") or 0) >= 2,
    }
    output: dict[str, dict] = {}
    for name, predicate in predicates.items():
        count = sum(predicate(row) for row in losing)
        output[name] = {
            "losing_trades": count,
            "loss_trade_share_pct": count / len(losing) * 100 if losing else None,
            "categories_overlap": True,
        }
    output["MARKET_AGAINST"] = {
        "losing_trades": None,
        "loss_trade_share_pct": None,
        "categories_overlap": True,
        "status": "NOT_AVAILABLE_WITHOUT_READING_LOCKED_HOLDOUT_ROW_GROUP",
    }
    return output


def _thresholds_from_dev(records: list[dict]) -> dict[str, float]:
    def values(field: str) -> list[float]:
        return [float(row[field]) for row in records if row.get(field) is not None]
    turnover = values("turnover_krw")
    open_return = values("intraday_return_from_open_pct")
    signal_mfe = values("signal_mfe_pct")
    signal_mae = values("signal_mae_pct")
    return {
        "late_return_p75_pct": _percentile(open_return, .75) or 0.0,
        "overextension_p90_pct": _percentile(open_return, .90) or 0.0,
        "turnover_p25_krw": _percentile(turnover, .25) or 0.0,
        "liquidity_p33_krw": _percentile(turnover, .33) or 0.0,
        "liquidity_p67_krw": _percentile(turnover, .67) or 0.0,
        "signal_mfe_p25_pct": _percentile(signal_mfe, .25) or 0.0,
        "signal_mae_p50_pct": _percentile(signal_mae, .50) or 0.0,
    }


def _bucket_records(records: list[dict], thresholds: dict[str, float]) -> dict[str, dict]:
    def rvol_bucket(value: float | None) -> str:
        if value is None:
            return "UNKNOWN"
        return "<1.0" if value < 1 else "1.0-1.5" if value < 1.5 else "1.5-2.0" if value < 2 else "2.0-3.0" if value < 3 else ">=3.0"

    def breakout_bucket(value: float | None) -> str:
        if value is None:
            return "UNKNOWN"
        return "<0" if value < 0 else "0-0.2%" if value < .2 else "0.2-0.5%" if value < .5 else "0.5-1.0%" if value < 1 else ">1.0%"

    def gap_bucket(value: float | None) -> str:
        if value is None:
            return "UNKNOWN"
        return "<0%" if value < 0 else "0-1%" if value < 1 else "1-3%" if value < 3 else "3-5%" if value < 5 else ">5%"

    for row in records:
        row["relative_volume_bucket"] = rvol_bucket(row.get("relative_volume"))
        row["breakout_distance_bucket"] = breakout_bucket(row.get("breakout_distance_pct"))
        row["gap_bucket"] = gap_bucket(row.get("open_gap_pct"))
        turnover = row.get("turnover_krw")
        row["liquidity_bucket"] = (
            "LOW" if turnover is not None and turnover < thresholds["liquidity_p33_krw"] else
            "MEDIUM" if turnover is not None and turnover < thresholds["liquidity_p67_krw"] else
            "HIGH" if turnover is not None else "UNKNOWN"
        )
        close_location = row.get("close_location_value")
        row["close_location_bucket"] = (
            "LOW" if close_location is not None and close_location < 1 / 3 else
            "MID" if close_location is not None and close_location < 2 / 3 else
            "HIGH" if close_location is not None else "UNKNOWN"
        )
    return {
        "time_of_day": _group_metrics(records, "time_of_day"),
        "relative_volume": _group_metrics(records, "relative_volume_bucket"),
        "breakout_distance": _group_metrics(records, "breakout_distance_bucket"),
        "candle_close_location": _group_metrics(records, "close_location_bucket"),
        "gap": _group_metrics(records, "gap_bucket"),
        "price": _group_metrics(records, "price_bucket"),
        "liquidity": _group_metrics(records, "liquidity_bucket"),
        "market": _group_metrics(records, "market"),
    }


def _read_signal_artifact(path: Path, segment: str, interval: str, expected_dataset_hash: str) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("segment") != segment or payload.get("interval") != interval:
        raise ValueError(f"baseline diagnostic does not match {segment}/{interval}")
    if payload.get("fresh_holdout_state") != "LOCKED_NOT_EVALUATED":
        raise ValueError("baseline artifact does not preserve the locked holdout state")
    if payload.get("dataset_sha256") != expected_dataset_hash:
        raise ValueError("baseline artifact references a different frozen dataset")
    return payload


def run_breakout_anatomy(
    *,
    interval: str,
    manifest_path: Path = PHASE25_MANIFEST,
    cache_root: Path = Path("data"),
    report_root: Path = DEFAULT_OUTPUT,
    git_sha: str | None = None,
) -> dict:
    if interval not in INTERVAL_MINUTES:
        raise ValueError("interval must be 15m or 30m")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_sha = str(manifest.get("dataset_sha256", ""))
    if len(source_sha) != 64:
        raise ValueError("frozen dataset manifest has no valid SHA-256")
    bars_by_symbol, minute_by_session, partition_hashes, incomplete_partitions = _load_frozen_partitions(
        cache_root, manifest_path
    )
    interval_bars: dict[str, list[Bar]] = {
        symbol: list(bars) for symbol, bars in bars_by_symbol.items()
    }
    scanner = _scanner_scores(interval_bars, interval)
    artifact_root = manifest_path.parent
    records: list[dict] = []
    segment_artifacts: dict[str, dict] = {}
    for segment in SEGMENTS:
        artifact_path = artifact_root / BASELINE_ARTIFACT.format(segment=segment, interval=interval)
        artifact = _read_signal_artifact(artifact_path, segment, interval, source_sha)
        segment_artifacts[segment] = artifact
        for signal in artifact.get("signal_records", []):
            context = _feature_values(
                signal, interval_bars, minute_by_session, scanner, interval, incomplete_partitions
            )
            expected_top10 = bool(signal.get("scanner_top10_member"))
            if context.get("scanner_rank") is not None and (context["scanner_rank"] <= 10) != expected_top10:
                raise ValueError("reconstructed fixed scanner rank differs from the Phase 2.5 signal artifact")
            records.append(_event_row(signal, context, segment, interval))
    records.sort(key=lambda row: (row["split"], row["signal_timestamp"], row["symbol"]))
    dev_rows = [row for row in records if row["split"] == "development"]
    thresholds = _thresholds_from_dev(dev_rows)
    for row in records:
        excursion = row.get("signal_mfe_pct")
        adverse_excursion = row.get("signal_mae_pct")
        row["false_breakout"] = bool(
            excursion is not None and adverse_excursion is not None and
            float(excursion) < thresholds["signal_mfe_p25_pct"] and
            float(adverse_excursion) <= thresholds["signal_mae_p50_pct"]
        )
        entry_time = datetime.fromisoformat(row["signal_timestamp"]).astimezone(KST)
        row["opening_signal"] = entry_time.time() < time(10, 0)
        row["midday_signal"] = time(11, 30) <= entry_time.time() < time(13, 30)
        row["late_signal"] = entry_time.time() >= time(14, 30)
    buckets = {segment: _bucket_records(
        [row for row in records if row["split"] == segment], thresholds
    ) for segment in SEGMENTS}

    report = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "run_id": f"phase3-breakout-anatomy-{interval}",
        "git_sha": git_sha,
        "feature_module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dataset_sha256": source_sha,
        "selected_partition_hash_sha256": hashlib.sha256(
            "\n".join(f"{key}:{value}" for key, value in sorted(partition_hashes.items())).encode()
        ).hexdigest(),
        "selected_partition_count": len(partition_hashes),
        "incomplete_selected_partition_count": len(incomplete_partitions),
        "source_artifact_sha256": {
            segment: hashlib.sha256(
                (artifact_root / BASELINE_ARTIFACT.format(segment=segment, interval=interval)).read_bytes()
            ).hexdigest() for segment in SEGMENTS
        },
        "interval": interval,
        "split_sessions": {
            segment: len(manifest["splits"][segment]["sessions"]) for segment in SEGMENTS
        },
        "split_periods": {
            segment: [manifest["splits"][segment]["start"], manifest["splits"][segment]["end"]]
            for segment in SEGMENTS
        },
        "holdout_integrity": {
            "state": manifest["fresh_holdout_state"],
            "allowed_segments": list(SEGMENTS),
            "post_validation_partitions_opened": 0,
            "index_context": "NOT_READ; frozen index parquet row group spans beyond validation end",
        },
        "cohort": {
            "symbols": len(manifest["succeeded_symbols"]),
            "market": "KOSPI current-listing proxy cohort",
            "survivorship_bias": manifest.get("survivorship_bias", "current listing cohort"),
            "dataset_quality": manifest.get("quality", {}).get("status", "UNKNOWN"),
        },
        "baseline_parameters": segment_artifacts["development"].get("strategy_parameters"),
        "outcome_definitions": {
            "WIN_LOSS_SCRATCH": "closed one-share diagnostic net PnL positive, negative, or zero",
            "false_breakout": "signal-path MFE below Development P25 and MAE at or below Development median; thresholds frozen from Development per interval",
            "follow_through": "signal-path MFE reaches 0.5%, 1.0%, or 2.0%",
            "mfe_mae_reference": "completed signal bar close, next max-holding window from original diagnostic",
        },
        "dev_derived_thresholds": thresholds,
        "selected_minute_rows": sum(len(values) for values in bars_by_symbol.values()),
        "baseline": {
            segment: _trade_metrics([row for row in records if row["split"] == segment])
            for segment in SEGMENTS
        },
        "data_gap_sensitivity": {
            segment: {
                "all": _trade_metrics([row for row in records if row["split"] == segment]),
                "complete_near_window_only": _trade_metrics([
                    row for row in records if row["split"] == segment and not row["near_data_gap"]
                ]),
                "excluded_near_data_gap": sum(
                    row["split"] == segment and row["near_data_gap"] for row in records
                ),
            } for segment in SEGMENTS
        },
        "winner_loser_feature_comparison": {
            segment: _feature_comparison([row for row in records if row["split"] == segment])
            for segment in SEGMENTS
        },
        "feature_correlations": {
            segment: _feature_correlations([row for row in records if row["split"] == segment])
            for segment in SEGMENTS
        },
        "session_cluster_bootstrap": {
            segment: _session_cluster_bootstrap([row for row in records if row["split"] == segment])
            for segment in SEGMENTS
        },
        "bucket_performance": buckets,
        "false_breakout_categories": {
            segment: _false_breakouts([row for row in records if row["split"] == segment], thresholds)
            for segment in SEGMENTS
        },
        "session_flags": {
            segment: {
                flag: _trade_metrics([row for row in records if row["split"] == segment and row[field]])
                for flag, field in (("opening_0900_1000", "opening_signal"),
                                    ("midday_1130_1330", "midday_signal"),
                                    ("late_after_1430", "late_signal"))
            } for segment in SEGMENTS
        },
        "event_count": len(records),
    }
    report_root.mkdir(parents=True, exist_ok=True)
    event_path = report_root / f"breakout-signals-{interval}.parquet"
    table = pa.Table.from_pylist(records)
    pq.write_table(table, event_path, compression="zstd")
    report_path = report_root / f"breakout-anatomy-{interval}.json"
    report["event_dataset_path"] = str(event_path)
    report["event_dataset_sha256"] = hashlib.sha256(event_path.read_bytes()).hexdigest()
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return report


def _cost_stress(records: list[dict]) -> dict:
    closed = [row for row in records if row["execution_status"] == "CLOSED"]
    output = {}
    for multiplier in (1.0, 1.5, 2.0):
        values = [
            float(row["gross_pnl_krw"] or 0) - multiplier * (
                float(row["fee_krw"] or 0) + float(row["tax_krw"] or 0) + float(row["slippage_krw"] or 0)
            ) for row in closed
        ]
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value < 0]
        output[f"{multiplier:.1f}x"] = {
            "closed_trades": len(values),
            "net_pnl_krw": sum(values),
            "expectancy_krw": statistics.fmean(values) if values else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        }
    return output


def _variant_metrics(records: list[dict], baseline_count: int, sessions: int) -> dict:
    result = _trade_metrics(records)
    result["trade_retention_pct"] = len(records) / baseline_count * 100 if baseline_count else None
    result["trades_per_session"] = result["filled_closed"] / sessions if sessions else None
    result["trades_per_week"] = result["filled_closed"] / sessions * 5 if sessions else None
    result["cost_stress"] = _cost_stress(records)
    return result


def _overextension_filter(row: dict, limit_pct: float) -> bool:
    value = row.get("intraday_return_from_open_pct")
    return value is not None and float(value) <= limit_pct


def _one_share_risk_krw(entry_reference: float, stop_price: float, cost: CostModel) -> float:
    entry = cost.buy_fill_price(entry_reference)
    if stop_price <= 0 or stop_price >= entry:
        return math.inf
    stop_fill = cost.sell_fill_price(stop_price)
    return (
        entry - stop_fill
        + cost.buy_cost(entry)
        + cost.sell_cost(stop_fill)
    )


def _one_share_order_cap_eligible(entry_reference: float, order_cap: float, cost: CostModel) -> bool:
    entry = cost.buy_fill_price(entry_reference)
    return 1_000 <= entry <= 50_000 and entry * (1 + cost.broker_fee_rate) <= order_cap


def run_breakout_v2_comparison(
    *,
    interval: str,
    report_root: Path = DEFAULT_OUTPUT,
) -> dict:
    if interval not in INTERVAL_MINUTES:
        raise ValueError("interval must be 15m or 30m")
    anatomy_path = report_root / f"breakout-anatomy-{interval}.json"
    events_path = report_root / f"breakout-signals-{interval}.parquet"
    if not anatomy_path.is_file() or not events_path.is_file():
        raise ValueError("run Phase 3 breakout anatomy before comparing v2 hypotheses")
    anatomy = json.loads(anatomy_path.read_text(encoding="utf-8"))
    if anatomy.get("schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError("anatomy feature schema is incompatible")
    records = pq.read_table(events_path).to_pylist()
    if any(row.get("split") not in SEGMENTS for row in records):
        raise ValueError("event dataset contains a non-permitted split")
    p75 = float(anatomy["dev_derived_thresholds"]["late_return_p75_pct"])
    sessions_by_split = anatomy["split_sessions"]
    split_results: dict[str, dict] = {}
    for segment in SEGMENTS:
        rows = [row for row in records if row["split"] == segment]
        split_results[segment] = {"baseline": _variant_metrics(rows, len(rows), sessions_by_split[segment])}
        if interval == "15m":
            kept = [row for row in rows if _overextension_filter(row, p75)]
            removed = [row for row in rows if not _overextension_filter(row, p75)]
            split_results[segment].update({
                "v2_a_not_overextended": _variant_metrics(kept, len(rows), sessions_by_split[segment]),
                "filter_attribution": {
                    "filter": "intraday_return_from_open_pct <= development_p75",
                    "threshold_pct_frozen_from_development": p75,
                    "removed_signals": len(removed),
                    "removed_signal_pct": len(removed) / len(rows) * 100 if rows else None,
                    "removed_closed_trade_metrics": _variant_metrics(
                        [row for row in removed if row["execution_status"] == "CLOSED"],
                        len(removed),
                        sessions_by_split[segment],
                    ),
                    "kept_closed_trade_metrics": _variant_metrics(
                        [row for row in kept if row["execution_status"] == "CLOSED"],
                        len(rows), sessions_by_split[segment],
                    ),
                },
            })

    dev = [row for row in records if row["split"] == "development"]
    val = [row for row in records if row["split"] == "validation"]
    sensitivity = {}
    if interval == "15m":
        for threshold in (p75 - 1.0, p75, p75 + 1.0):
            sensitivity[f"{threshold:.4f}%"] = {
                segment: _variant_metrics(
                    [row for row in (dev if segment == "development" else val)
                     if _overextension_filter(row, threshold)],
                    len(dev if segment == "development" else val), sessions_by_split[segment],
                ) for segment in SEGMENTS
            }
    comparison = {
        "schema_version": "phase3-breakout-v2-v1",
        "interval": interval,
        "dataset_sha256": anatomy["dataset_sha256"],
        "selected_partition_hash_sha256": anatomy["selected_partition_hash_sha256"],
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "holdout_integrity": anatomy["holdout_integrity"],
        "candidate_count": 1 if interval == "15m" else 0,
        "candidate_status": "REJECTED" if interval == "15m" else "NO_CANDIDATE_SELECTED",
        "hypothesis": {
            "name": "V2-A_NOT_OVEREXTENDED_15M" if interval == "15m" else None,
            "rule": "completed signal bar close <= session open * (1 + frozen Development P75 intraday return)",
            "threshold_pct": p75 if interval == "15m" else None,
            "source_evidence": (
                anatomy["winner_loser_feature_comparison"]["development"]["intraday_return_from_open_pct"],
                anatomy["winner_loser_feature_comparison"]["validation"]["intraday_return_from_open_pct"],
            ) if interval == "15m" else None,
            "status_reason": (
                "Dev and Validation both show lower winner median intraday extension, but Validation net expectancy remains negative"
                if interval == "15m" else
                "Development/Validation separation was too weak or unstable to promote this filter on 30m"
            ),
        },
        "development_validation": split_results,
        "nearby_threshold_sensitivity_percentage_points": sensitivity,
        "promotion_gates": ({
            "validation_expectancy_gt_zero": split_results["validation"]["v2_a_not_overextended"]["expectancy_krw"] > 0,
            "validation_pf_gt_one": (split_results["validation"]["v2_a_not_overextended"]["profit_factor"] or 0) > 1,
            "validation_at_least_30_closed": split_results["validation"]["v2_a_not_overextended"]["filled_closed"] >= 30,
            "validation_1_5x_cost_positive": split_results["validation"]["v2_a_not_overextended"]["cost_stress"]["1.5x"]["net_pnl_krw"] > 0,
            "validation_false_breakout_rate_reduced": split_results["validation"]["v2_a_not_overextended"]["false_breakout_rate_pct"] <
            split_results["validation"]["baseline"]["false_breakout_rate_pct"],
            "development_expectancy_improved": split_results["development"]["v2_a_not_overextended"]["expectancy_krw"] >
            split_results["development"]["baseline"]["expectancy_krw"],
            "nearby_thresholds_keep_validation_improvement_direction": all(
                values["validation"]["expectancy_krw"] > split_results["validation"]["baseline"]["expectancy_krw"]
                for values in sensitivity.values()
            ),
        } if interval == "15m" else {}),
        "verdict": "FAILED_GENERALIZATION_NO_EDGE_AFTER_COST" if interval == "15m" else "NO_VALIDATION_CANDIDATE",
    }
    target = report_root / f"breakout-v2-comparison-{interval}.json"
    comparison["report_path"] = str(target)
    target.write_text(json.dumps(comparison, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return comparison


def run_breakout_v2_validation(report_root: Path = DEFAULT_OUTPUT) -> dict:
    reports = {
        interval: json.loads((report_root / f"breakout-v2-comparison-{interval}.json").read_text(encoding="utf-8"))
        for interval in INTERVAL_MINUTES
    }
    source = json.loads((report_root / "breakout-anatomy-15m.json").read_text(encoding="utf-8"))
    portfolio_report = run_breakout_v2_portfolio(report_root=report_root)
    feature_comparisons = source["winner_loser_feature_comparison"]
    hypothesis_log = {
        "schema_version": "phase3-v2-hypotheses-v1",
        "dataset_sha256": source["dataset_sha256"],
        "fresh_holdout_state": source["holdout_integrity"]["state"],
        "hypotheses_screened": 3,
        "development_variants_run": 1,
        "validation_candidates_run": 1,
        "hypotheses": [
            {
                "name": "V2-A_NOT_OVEREXTENDED_15M",
                "rule": "intraday return from session open no greater than Development P75",
                "source_evidence": {
                    "development": feature_comparisons["development"]["intraday_return_from_open_pct"],
                    "validation": feature_comparisons["validation"]["intraday_return_from_open_pct"],
                },
                "threshold_pct": reports["15m"]["hypothesis"]["threshold_pct"],
                "development_evidence": reports["15m"]["development_validation"]["development"]["v2_a_not_overextended"],
                "validation_evidence": reports["15m"]["development_validation"]["validation"]["v2_a_not_overextended"],
                "status": "REJECTED_FAILED_GENERALIZATION",
            },
            {
                "name": "V2-B_STRONG_CLOSE_15M",
                "rule": "completed breakout bar close location / upper wick",
                "source_evidence": {
                    "development": feature_comparisons["development"]["upper_wick_ratio"],
                    "validation": feature_comparisons["validation"]["upper_wick_ratio"],
                },
                "screening_only": True,
                "status": "REJECTED_UNSTABLE_DIRECTION",
            },
            {
                "name": "V2-C_VOLUME_PERSISTENCE",
                "rule": "relative volume and relative turnover confirmation",
                "source_evidence": {
                    "development": feature_comparisons["development"]["relative_volume"],
                    "validation": feature_comparisons["validation"]["relative_volume"],
                    "baseline_volume_confirmation": source.get("baseline_parameters"),
                },
                "screening_only": True,
                "status": "REJECTED_NO_INCREMENTAL_SEPARATION",
            },
        ],
        "v2_a_parameters": {
            "threshold_source": "15m Development P75 of intraday return from session open",
            "nearby_sensitivity": reports["15m"]["nearby_threshold_sensitivity_percentage_points"],
        },
    }
    hypothesis_path = report_root / "v2-hypotheses.json"
    hypothesis_path.write_text(
        json.dumps(hypothesis_log, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    hypotheses = {
        "schema_version": "phase3-v2-validation-v1",
        "dataset_sha256": source["dataset_sha256"],
        "fresh_holdout_state": source["holdout_integrity"]["state"],
        "hypotheses_path": str(hypothesis_path),
        "validation": {
            "15m": reports["15m"]["promotion_gates"],
            "30m": reports["30m"]["verdict"],
        },
        "portfolio_100k": portfolio_report,
        "verdict": "BREAKOUT_V2_REJECTED",
        "next_step": "Expand untouched future KRX evidence before reconsidering Breakout; do not open the locked Fresh Holdout",
    }
    target = report_root / "v2-validation.json"
    target.write_text(json.dumps(hypotheses, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    anatomy_summary = {
        "schema_version": "phase3-breakout-anatomy-summary-v1",
        "dataset_sha256": source["dataset_sha256"],
        "fresh_holdout_state": source["holdout_integrity"]["state"],
        "interval_reports": {
            interval: json.loads((report_root / f"breakout-anatomy-{interval}.json").read_text(encoding="utf-8"))
            for interval in INTERVAL_MINUTES
        },
    }
    (report_root / "breakout-anatomy-summary.json").write_text(
        json.dumps(anatomy_summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    return hypotheses


def _peak_portfolio_positions(trades: list, timeline: list[datetime]) -> int:
    entries_by_time: dict[datetime, list[int]] = defaultdict(list)
    for index, trade in enumerate(trades):
        entries_by_time[trade.entry_time].append(index)
    active: set[int] = set()
    peak = 0
    for timestamp in timeline:
        active = {index for index in active if trades[index].exit_time > timestamp}
        for index in entries_by_time.get(timestamp, []):
            active.add(index)
            peak = max(peak, len(active))
            if trades[index].exit_time <= timestamp:
                active.remove(index)
    return peak


def run_breakout_v2_portfolio(
    *,
    report_root: Path = DEFAULT_OUTPUT,
    manifest_path: Path = PHASE25_MANIFEST,
    cache_root: Path = Path("data"),
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bars_1m, _, _, _ = _load_frozen_partitions(cache_root, manifest_path)
    scenarios = ((20_000, .25), (30_000, .50))
    portfolio_results: dict[str, dict] = {}
    for interval, interval_minutes in INTERVAL_MINUTES.items():
        anatomy = json.loads((report_root / f"breakout-anatomy-{interval}.json").read_text(encoding="utf-8"))
        events = pq.read_table(report_root / f"breakout-signals-{interval}.parquet").to_pylist()
        interval_bars = {
            symbol: resample_session_minutes(values, interval_minutes)
            for symbol, values in bars_1m.items()
        }
        portfolio_results[interval] = {}
        arms = ["V1"] + (["V2-A_NOT_OVEREXTENDED"] if interval == "15m" else [])
        for segment in SEGMENTS:
            segment_dates = {
                date.fromisoformat(value) for value in manifest["splits"][segment]["sessions"]
            }
            segment_events = [row for row in events if row["split"] == segment]
            threshold = float(anatomy["dev_derived_thresholds"]["late_return_p75_pct"])
            by_arm: dict[str, dict] = {}
            for arm in arms:
                arm_events = [
                    row for row in segment_events
                    if arm == "V1" or _overextension_filter(row, threshold)
                ]
                signal_map = {
                    (row["symbol"], datetime.fromisoformat(row["signal_timestamp"]).astimezone(KST)): row
                    for row in arm_events
                }
                segment_bars = {
                    symbol: [bar for bar in values if bar.time.astimezone(KST).date() in segment_dates]
                    for symbol, values in interval_bars.items()
                }
                segment_bars = {symbol: values for symbol, values in segment_bars.items() if values}

                def signal_fn(
                    symbol: str,
                    history: list[Bar],
                    *,
                    frozen_signals: dict = signal_map,
                    selected_arm: str = arm,
                ) -> Signal:
                    bar = history[-1]
                    row = frozen_signals.get((symbol, bar.time.astimezone(KST)))
                    if row is None:
                        return Signal(bar.time, symbol, "breakout_phase3", Decision.HOLD, ("NO_FROZEN_ENTRY",))
                    return Signal(
                        bar.time, symbol, "breakout_phase3", Decision.ENTER,
                        ("FROZEN_V1_ENTRY",) if selected_arm == "V1" else ("V2_NOT_OVEREXTENDED",),
                        reference_price=None, stop_price=float(row["stop_price"]), max_holding_bars=10,
                    )

                cost = CostModel(.00015, .002, 15.0)
                result_payload: dict[str, dict] = {}
                for order_cap, risk_pct in scenarios:
                    result = run_portfolio_backtest(
                        segment_bars, signal_fn,
                        starting_cash_krw=100_000, capital_cap_krw=100_000,
                        order_cap_krw=order_cap, risk_per_trade_pct=risk_pct,
                        min_price_krw=1_000, max_price_krw=50_000,
                        max_concurrent_positions=2, cost_model=cost,
                    )
                    metrics = calculate_metrics(result)
                    timeline = sorted({bar.time for values in segment_bars.values() for bar in values})
                    exposure_bar_sum = sum(
                        trade.entry_price * trade.quantity * max(
                            1, sum(trade.entry_time <= timestamp <= trade.exit_time for timestamp in timeline)
                        )
                        for trade in result.trades
                    )
                    average_invested = exposure_bar_sum / len(timeline) if timeline else 0.0
                    peak_position_count = _peak_portfolio_positions(list(result.trades), timeline)
                    signals_sent = sum(signal.decision == Decision.ENTER for signal in result.decisions)
                    one_share_price_eligible = sum(
                        row.get("next_bar_open") is not None and
                        _one_share_order_cap_eligible(float(row["next_bar_open"]), order_cap, cost)
                        for row in arm_events
                    )
                    one_share_risk_eligible = sum(
                        row.get("stop_price") is not None and row.get("next_bar_open") is not None and
                        _one_share_risk_krw(
                            float(row["next_bar_open"]), float(row["stop_price"]), cost
                        ) <=
                        100_000 * risk_pct / 100
                        for row in arm_events
                    )
                    key = f"order_cap_{order_cap}_risk_{risk_pct:.2f}pct"
                    result_payload[key] = {
                        "starting_cash_krw": result.starting_cash_krw,
                        "ending_equity_krw": round(result.equity_curve[-1], 2) if result.equity_curve else result.ending_cash_krw,
                        "net_pnl_krw": round(result.equity_curve[-1] - result.starting_cash_krw, 2) if result.equity_curve else None,
                        "trade_count": metrics.trade_count,
                        "entry_decisions": signals_sent,
                        "portfolio_fills": len(result.trades),
                        "profit_factor": metrics.profit_factor if metrics.profit_factor is None or math.isfinite(metrics.profit_factor) else None,
                        "expectancy_krw": metrics.expectancy_krw,
                        "mdd_pct": metrics.max_drawdown_pct,
                        "cost_drag_krw": metrics.cost_drag_krw,
                        "average_invested_capital_krw": round(average_invested, 2),
                        "average_idle_cash_pct": round(max(0.0, 1 - average_invested / 100_000) * 100, 2),
                        "peak_concurrent_positions": peak_position_count,
                        "portfolio_fill_retention_pct": (
                            len(result.trades) / len(arm_events) * 100 if arm_events else None
                        ),
                        "one_share_price_and_order_cap_eligible_signals": one_share_price_eligible,
                        "one_share_within_risk_budget_signals": one_share_risk_eligible,
                        "whole_share_execution": "ACTUAL_PORTFOLIO_BACKTEST_WITH_FIXED_RESEARCH_SCENARIO",
                    }
                by_arm[arm] = result_payload
            portfolio_results[interval][segment] = by_arm
    return {
        "status": "RESEARCH_ONLY_OFFLINE",
        "starting_capital_krw": 100_000,
        "scanner": "fixed Phase 2.5 top-10 turnover proxy; entry events unchanged except V2-A feature gate",
        "cost_assumptions": {"fee_rate": .00015, "sell_tax_rate": .002, "slippage_bps": 15.0, "status": "ASSUMED"},
        "scenarios": {"20K_0.25pct": {"order_cap_krw": 20_000, "risk_per_trade_pct": .25},
                      "30K_0.50pct": {"order_cap_krw": 30_000, "risk_per_trade_pct": .50}},
        "split_results": portfolio_results,
        "capital_utilization_method": "sum(entry_notional * active_interval_samples) / global_unique_interval_timestamps",
        "live_environment_changed": False,
    }

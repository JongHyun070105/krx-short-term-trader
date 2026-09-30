"""Phase 12 failure anatomy for the rejected Phase 11 market-stress rule.

Only completed sessions in Development and touched Validation are admitted by
the loader. Confirmation has a separate fail-closed authorization check.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import random
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from krx_trader.data.quality import require_healthy_bars
from krx_trader.models import Bar
from krx_trader.research.phase11 import _load_frozen_cohort, non_overlapping_events

KST = ZoneInfo("Asia/Seoul")
POOL_START = date(2023, 1, 2)
POOL_END = date(2025, 6, 30)
WARMUP_START = date(2022, 11, 1)
DEVELOPMENT = (date(2023, 1, 2), date(2024, 6, 28))
VALIDATION = (date(2024, 7, 1), date(2025, 6, 30))
CONFIRMATION = (date(2025, 7, 1), date(2025, 12, 30))
PHASE11_TRIGGER_MAX_RETURN_20D_PCT = -4.0
MAX_NON_SIGNAL_SESSIONS = 2
MIN_BREADTH_COVERAGE = 0.80
BETA_LOOKBACK_SESSIONS = 60
BETA_MIN_OBSERVATIONS = 40
BASE_ROUND_TRIP_COST_PCT = 0.53
BOOTSTRAP_ITERATIONS = 2_000
BOOTSTRAP_SEED = 1212
OUTPUT_ROOT = Path("runtime/research/phase12")
PHASE11_ROOT = Path("runtime/research/phase11")
COHORT_PATH = Path("runtime/research/phase4/cohort-manifest.json")
PHASE11_ADJUSTMENT = "FID_ORG_ADJ_PRC=0 (KIS adjusted daily OHLCV)"
MARKETS = ("KOSPI", "KOSDAQ")

STRESS_DEPTH_BOUNDS = {"mild_floor_pct": -8.0, "moderate_floor_pct": -12.0, "deep_floor_pct": -20.0}
STRESS_SPEED_BOUNDS = {"fast_5d_share_min": 0.50, "slow_5d_share_max": 0.25, "slow_days_since_high_min": 10}
BREADTH_BROAD_SELL_OFF_MIN = 0.70
MECHANISM_GATE = {
    "minimum_independent_macro_episodes_per_period": 8,
    "minimum_market_signal_dates_per_period": 20,
    "minimum_market_specific_episodes_per_market_and_period": 3,
    "minimum_beta_adjusted_residual_after_1x_cost_pct": 0.0,
    "maximum_top_episode_positive_contribution_share_pct": 50.0,
    "required_periods": ["development", "validation"],
    "candidate_count_max": 1,
}


class ConfirmationAccessError(PermissionError):
    """Raised before any Confirmation payload is opened without a valid freeze."""


def authorize_touched_window(start: date, end: date) -> None:
    """Permit warmup plus the two touched periods, never Confirmation or later."""
    if start > end or start < WARMUP_START or end > POOL_END:
        raise ConfirmationAccessError(
            f"Phase 12 touched-data window must stay within {WARMUP_START}..{POOL_END}"
        )


def authorize_confirmation_access(
    preregistration_path: Path,
    *,
    current_git_sha: str,
    freeze_git_sha: str,
) -> dict[str, Any]:
    """Validate a frozen one-shot candidate before a caller opens Confirmation."""
    if not preregistration_path.is_file() or not current_git_sha or current_git_sha != freeze_git_sha:
        raise ConfirmationAccessError("Confirmation requires a committed, pushed candidate freeze")
    if _remote_main_sha() != freeze_git_sha:
        raise ConfirmationAccessError("candidate freeze is not the current origin/main commit")
    try:
        registration = json.loads(preregistration_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfirmationAccessError("candidate preregistration is unavailable or invalid") from exc
    if (
        registration.get("candidate_status") != "RESEARCH_CANDIDATE"
        or registration.get("pre_confirmation_freeze_sha") != freeze_git_sha
        or registration.get("confirmation_period") != ["2025-07-01", "2025-12-30"]
        or not registration.get("candidate_rule")
        or not registration.get("anatomy_artifact_hashes")
    ):
        raise ConfirmationAccessError("Confirmation preregistration does not match the frozen candidate")
    return registration


def guarded_confirmation_read(
    partition_path: Path,
    expected_symbol: str,
    expected_market: str,
    preregistration_path: Path,
    *,
    current_git_sha: str,
    freeze_git_sha: str,
) -> list[Bar] | None:
    """Read only one exact Confirmation-period partition after the pushed freeze."""
    authorize_confirmation_access(
        preregistration_path,
        current_git_sha=current_git_sha,
        freeze_git_sha=freeze_git_sha,
    )
    cache_root = (PHASE11_ROOT / "daily_cache").resolve()
    if expected_symbol in MARKETS:
        if expected_market != expected_symbol:
            raise ConfirmationAccessError("index symbol and market must match")
        expected_path = cache_root / "indexes" / f"{expected_symbol}-1d-kis-index.parquet"
    else:
        if expected_market not in MARKETS or Path(expected_symbol).name != expected_symbol:
            raise ConfirmationAccessError("invalid stock symbol or market")
        expected_path = cache_root / "daily" / f"{expected_symbol}-1d-adjusted.parquet"
    resolved_path = partition_path.resolve()
    if not resolved_path.is_relative_to(cache_root) or resolved_path != expected_path.resolve():
        raise ConfirmationAccessError("Confirmation reader accepts only the matching Phase 11 cache partition")
    loaded = _read_bounded_partition(
        resolved_path,
        start=CONFIRMATION[0],
        end=CONFIRMATION[1],
        expected_symbol=expected_symbol,
        expected_market=expected_market,
        access_scope="confirmation",
    )
    return loaded[0] if loaded is not None else None


def _mean(values: Iterable[float | int | None]) -> float | None:
    valid = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.fmean(valid) if valid else None


def _stdev(values: Iterable[float | int | None], *, population: bool = True) -> float | None:
    valid = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if len(valid) < 2:
        return None
    return (statistics.pstdev if population else statistics.stdev)(valid)


def decompose_returns(stock_return_pct: float, market_return_pct: float) -> dict[str, float]:
    """Simple matched-index decomposition; this is not a factor-alpha model."""
    return {
        "stock_return_pct": float(stock_return_pct),
        "market_return_pct": float(market_return_pct),
        "excess_return_pct": float(stock_return_pct) - float(market_return_pct),
    }


def beta_adjusted_residual(
    stock_return_pct: float | None,
    market_return_pct: float | None,
    beta: float | None,
) -> float | None:
    if stock_return_pct is None or market_return_pct is None or beta is None:
        return None
    return float(stock_return_pct) - float(beta) * float(market_return_pct)


def rolling_beta(
    stock_returns_pct: Iterable[float | None],
    market_returns_pct: Iterable[float | None],
    *,
    minimum_observations: int = BETA_MIN_OBSERVATIONS,
) -> float | None:
    """OLS slope on paired past returns; missing observations are excluded."""
    pairs = [
        (float(market), float(stock))
        for stock, market in zip(stock_returns_pct, market_returns_pct, strict=True)
        if stock is not None and market is not None
        and math.isfinite(float(stock)) and math.isfinite(float(market))
    ]
    if len(pairs) < minimum_observations:
        return None
    market_mean = statistics.fmean(pair[0] for pair in pairs)
    stock_mean = statistics.fmean(pair[1] for pair in pairs)
    variance = sum((market - market_mean) ** 2 for market, _ in pairs)
    if variance <= 0:
        return None
    return sum((market - market_mean) * (stock - stock_mean) for market, stock in pairs) / variance


def build_breadth_observation(
    *,
    market: str,
    session: date,
    eligible_count: int,
    returns_by_window: dict[int, dict[str, float | None]],
    minimum_coverage: float = MIN_BREADTH_COVERAGE,
) -> dict[str, Any]:
    """Summarize only observed symbol returns; missing values never enter a denominator."""
    if market not in MARKETS or eligible_count < 1 or not 0.0 <= minimum_coverage <= 1.0:
        raise ValueError("invalid market breadth parameters")
    values: dict[int, list[float]] = {}
    counts: dict[str, int] = {}
    for days in (1, 5, 20):
        window = returns_by_window.get(days, {})
        observed = [float(value) for value in window.values() if value is not None and math.isfinite(float(value))]
        values[days] = observed
        counts[f"{days}d"] = len(observed)
    one_day = values[1]
    observed_count = len(one_day)
    coverage = observed_count / eligible_count

    def fraction(days: int, predicate: Any) -> float | None:
        sample = values[days]
        return sum(predicate(value) for value in sample) / len(sample) if sample else None

    result: dict[str, Any] = {
        "market": market,
        "date": session.isoformat(),
        "eligible_cohort_count": eligible_count,
        "observed_count": observed_count,
        "coverage_ratio": coverage,
        "data_quality": "PASS" if coverage >= minimum_coverage else "LOW_COVERAGE",
        "minimum_coverage_threshold": minimum_coverage,
        "return_observed_counts": counts,
        "fraction_positive_today": fraction(1, lambda value: value > 0),
        "fraction_negative_today": fraction(1, lambda value: value < 0),
        "fraction_5d_return_positive": fraction(5, lambda value: value > 0),
        "fraction_5d_return_negative": fraction(5, lambda value: value < 0),
        "fraction_5d_return_le_minus_5pct": fraction(5, lambda value: value <= -5.0),
        "fraction_20d_return_le_minus_10pct": fraction(20, lambda value: value <= -10.0),
        "cross_sectional_median_1d_return_pct": statistics.median(values[1]) if values[1] else None,
        "cross_sectional_median_5d_return_pct": statistics.median(values[5]) if values[5] else None,
        "cross_sectional_median_20d_return_pct": statistics.median(values[20]) if values[20] else None,
        "cross_sectional_dispersion_1d_pct": _stdev(values[1]),
        "cross_sectional_dispersion_5d_pct": _stdev(values[5]),
        "cross_sectional_dispersion_20d_pct": _stdev(values[20]),
        "cross_sectional_downside_dispersion_1d_pct": _stdev(value for value in values[1] if value < 0),
        "cross_sectional_downside_dispersion_5d_pct": _stdev(value for value in values[5] if value < 0),
        "cross_sectional_downside_dispersion_20d_pct": _stdev(value for value in values[20] if value < 0),
    }
    result["breadth_state"] = (
        "BROAD_SELLOFF"
        if result["fraction_5d_return_negative"] is not None
        and result["fraction_5d_return_negative"] >= BREADTH_BROAD_SELL_OFF_MIN
        else "NARROW_SELLOFF" if result["fraction_5d_return_negative"] is not None else None
    )
    return result


def group_stress_episodes(
    market: str,
    signal_dates: Iterable[date],
    sessions: list[date],
    *,
    max_non_signal_sessions: int = MAX_NON_SIGNAL_SESSIONS,
) -> list[dict[str, Any]]:
    """Group qualifying dates when at most the configured number of sessions are absent."""
    if market not in MARKETS or max_non_signal_sessions < 0:
        raise ValueError("invalid stress episode grouping parameters")
    ordered_sessions = sorted(set(sessions))
    session_index = {session: index for index, session in enumerate(ordered_sessions)}
    dates = sorted(set(signal_dates))
    if any(day not in session_index for day in dates):
        raise ValueError("stress signal date is not in the market session calendar")
    groups: list[list[date]] = []
    for day in dates:
        if not groups:
            groups.append([day])
            continue
        prior = groups[-1][-1]
        gap = session_index[day] - session_index[prior] - 1
        if gap <= max_non_signal_sessions:
            groups[-1].append(day)
        else:
            groups.append([day])
    output: list[dict[str, Any]] = []
    for number, group in enumerate(groups, start=1):
        start_index = session_index[group[0]]
        end_index = session_index[group[-1]]
        output.append({
            "episode_id": f"{market}-{number:03d}-{group[0].isoformat()}",
            "market": market,
            "episode_start": group[0].isoformat(),
            "episode_end": group[-1].isoformat(),
            "duration_sessions": end_index - start_index + 1,
            "non_signal_sessions_inside": end_index - start_index + 1 - len(group),
            "signal_dates": [day.isoformat() for day in group],
        })
    return output


def _iso_midnight(day: date) -> str:
    return datetime.combine(day, datetime.min.time(), KST).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_bars_sha256(bars: Iterable[Bar], *, start: date, end: date) -> str:
    """Hash only materialized rows within the authorized date window."""
    rows = [
        {
            "timestamp": bar.time.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "turnover_krw": bar.turnover_krw,
        }
        for bar in bars
        if start <= bar.time.astimezone(KST).date() <= end
    ]
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_bounded_partition(
    path: Path,
    *,
    start: date,
    end: date,
    expected_symbol: str,
    expected_market: str,
    access_scope: Literal["touched", "confirmation"] = "touched",
) -> tuple[list[Bar], str] | None:
    """Read only an explicitly authorized bounded date range from a Phase 11 partition."""
    if access_scope == "touched":
        authorize_touched_window(start, end)
    elif access_scope == "confirmation":
        if (start, end) != CONFIRMATION:
            raise ConfirmationAccessError("Confirmation reader is limited to 2025-07-01..2025-12-30")
    else:
        raise ValueError(f"unsupported Phase 12 input scope: {access_scope}")
    sidecar = path.with_suffix(".metadata.json")
    if not path.is_file() or not sidecar.is_file():
        return None
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    if (
        metadata.get("symbol") != expected_symbol
        or metadata.get("market") != expected_market
        or metadata.get("interval") not in {"1d-adjusted", "1d-kis-index"}
    ):
        raise ValueError(f"Phase 11 cache provenance mismatch: {path.name}")
    table = pq.read_table(
        path,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover_krw"],
        filters=[("timestamp", ">=", _iso_midnight(start)), ("timestamp", "<=", _iso_midnight(end))],
    )
    rows = table.to_pylist()
    bars = [
        Bar(
            time=datetime.fromisoformat(row["timestamp"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
            turnover_krw=int(row["turnover_krw"]) if row["turnover_krw"] is not None else None,
        )
        for row in rows
    ]
    bars.sort(key=lambda bar: bar.time)
    if any(not start <= bar.time.astimezone(KST).date() <= end for bar in bars):
        raise ValueError("bounded Phase 12 loader returned a protected-period row")
    if bars:
        require_healthy_bars(bars)
    source_hash = _bounded_bars_sha256(bars, start=start, end=end)
    return bars, source_hash


def _load_phase12_inputs(
    *,
    cache_root: Path,
    cohort_path: Path,
    start: date = WARMUP_START,
    end: date = POOL_END,
) -> dict[str, Any]:
    authorize_touched_window(start, end)
    cohort_name, symbols, market_by_symbol, cohort_sha = _load_frozen_cohort(cohort_path)
    prices: dict[str, list[Bar]] = {}
    indexes: dict[str, list[Bar]] = {}
    input_hashes: list[dict[str, str]] = [{"path": cohort_path.as_posix(), "sha256": _sha256_file(cohort_path)}]
    missing: list[str] = []
    for symbol in sorted(symbols):
        market = market_by_symbol[symbol]
        partition = cache_root / "daily" / f"{symbol}-1d-adjusted.parquet"
        loaded = _read_bounded_partition(
            partition, start=start, end=end, expected_symbol=symbol, expected_market=market,
        )
        if loaded is None or not loaded[0]:
            missing.append(symbol)
            continue
        prices[symbol] = loaded[0]
        input_hashes.append({"path": partition.as_posix(), "sha256": loaded[1], "hash_scope": "bounded_rows"})
    for market in MARKETS:
        partition = cache_root / "indexes" / f"{market}-1d-kis-index.parquet"
        loaded = _read_bounded_partition(
            partition, start=start, end=end, expected_symbol=market, expected_market=market,
        )
        if loaded is None or not loaded[0]:
            raise ValueError(f"Phase 12 requires a complete {market} index partition")
        indexes[market] = loaded[0]
        input_hashes.append({"path": partition.as_posix(), "sha256": loaded[1], "hash_scope": "bounded_rows"})
    input_hashes.sort(key=lambda item: item["path"])
    dataset_sha = hashlib.sha256(
        json.dumps(input_hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "cohort_name": cohort_name,
        "symbols": sorted(symbols),
        "market_by_symbol": market_by_symbol,
        "cohort_sha256": cohort_sha,
        "prices": prices,
        "indexes": indexes,
        "missing_symbols": missing,
        "input_hashes": input_hashes,
        "dataset_sha256": dataset_sha,
    }


def _return_at(
    bars_by_date: dict[date, Bar], sessions: list[date], index: int, lookback: int,
) -> float | None:
    if index < lookback:
        return None
    window = sessions[index - lookback:index + 1]
    bars = [bars_by_date.get(day) for day in window]
    if any(bar is None for bar in bars):
        return None
    prior = bars[0]
    current = bars[-1]
    if prior is None or current is None or prior.close <= 0:
        return None
    return (current.close / prior.close - 1.0) * 100.0


def _volatility_at(
    bars_by_date: dict[date, Bar], sessions: list[date], index: int, lookback: int,
) -> float | None:
    if index < lookback:
        return None
    returns: list[float] = []
    for position in range(index - lookback + 1, index + 1):
        current = bars_by_date.get(sessions[position])
        prior = bars_by_date.get(sessions[position - 1])
        if current is None or prior is None or prior.close <= 0:
            return None
        returns.append(current.close / prior.close - 1.0)
    return statistics.stdev(returns) * 100.0 if len(returns) >= 2 else None


def _rolling_price_context(
    bars_by_date: dict[date, Bar], sessions: list[date], index: int, lookback: int,
) -> dict[str, float | int | None]:
    if index + 1 < lookback:
        return {
            f"rolling_{lookback}d_drawdown_pct": None,
            f"distance_from_{lookback}d_high_pct": None,
            f"days_since_{lookback}d_high": None,
        }
    bars = [bars_by_date.get(day) for day in sessions[index - lookback + 1:index + 1]]
    if any(bar is None for bar in bars):
        return {
            f"rolling_{lookback}d_drawdown_pct": None,
            f"distance_from_{lookback}d_high_pct": None,
            f"days_since_{lookback}d_high": None,
        }
    valid = [bar for bar in bars if bar is not None]
    current = valid[-1]
    highest_close = max(bar.close for bar in valid)
    highest_high = max(bar.high for bar in valid)
    high_position = max(i for i, bar in enumerate(valid) if bar.close == highest_close)
    return {
        f"rolling_{lookback}d_drawdown_pct": (current.close / highest_close - 1.0) * 100.0,
        f"distance_from_{lookback}d_high_pct": (current.close / highest_high - 1.0) * 100.0,
        f"days_since_{lookback}d_high": len(valid) - 1 - high_position,
    }


def _range_at(
    bars_by_date: dict[date, Bar], sessions: list[date], index: int, lookback: int,
) -> float | None:
    if index + 1 < lookback:
        return None
    bars = [bars_by_date.get(day) for day in sessions[index - lookback + 1:index + 1]]
    if any(bar is None for bar in bars):
        return None
    valid = [bar for bar in bars if bar is not None]
    close = valid[-1].close
    if close <= 0:
        return None
    return (max(bar.high for bar in valid) - min(bar.low for bar in valid)) / close * 100.0


def _median_prior_value(
    bars_by_date: dict[date, Bar], sessions: list[date], index: int, field: str, lookback: int,
) -> float | None:
    if index < lookback:
        return None
    values: list[float] = []
    for day in sessions[index - lookback:index]:
        bar = bars_by_date.get(day)
        value = getattr(bar, field) if bar is not None else None
        if value is not None and math.isfinite(float(value)):
            values.append(float(value))
    if len(values) < math.ceil(lookback * 0.75):
        return None
    return float(statistics.median(values))


def _tercile_thresholds(values: Iterable[float | None]) -> list[float] | None:
    ordered = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if len(ordered) < 3:
        return None

    def q(probability: float) -> float:
        position = (len(ordered) - 1) * probability
        low = math.floor(position)
        high = math.ceil(position)
        if low == high:
            return ordered[low]
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return [q(1 / 3), q(2 / 3)]


def _tercile_state(value: float | None, thresholds: list[float] | None) -> str | None:
    if value is None or thresholds is None:
        return None
    if value <= thresholds[0]:
        return "LOW"
    if value <= thresholds[1]:
        return "MID"
    return "HIGH"


def classify_stress_depth(return_20d_pct: float | None) -> str | None:
    if return_20d_pct is None or return_20d_pct > PHASE11_TRIGGER_MAX_RETURN_20D_PCT:
        return None
    if return_20d_pct > STRESS_DEPTH_BOUNDS["mild_floor_pct"]:
        return "MILD"
    if return_20d_pct > STRESS_DEPTH_BOUNDS["moderate_floor_pct"]:
        return "MODERATE"
    if return_20d_pct > STRESS_DEPTH_BOUNDS["deep_floor_pct"]:
        return "DEEP"
    return "EXTREME"


def classify_stress_speed(
    return_5d_pct: float | None,
    return_20d_pct: float | None,
    days_since_20d_high: int | None,
) -> str | None:
    if return_5d_pct is None or return_20d_pct is None or return_20d_pct >= 0:
        return None
    five_day_share = max(0.0, -return_5d_pct) / abs(return_20d_pct)
    if five_day_share >= STRESS_SPEED_BOUNDS["fast_5d_share_min"]:
        return "FAST_SHOCK"
    if (
        return_5d_pct < 0
        and five_day_share <= STRESS_SPEED_BOUNDS["slow_5d_share_max"]
        and days_since_20d_high is not None
        and days_since_20d_high >= STRESS_SPEED_BOUNDS["slow_days_since_high_min"]
    ):
        return "SLOW_GRIND"
    return "MEDIUM_DECLINE"


def build_market_state_rows(
    *,
    prices_by_symbol: dict[str, list[Bar]],
    indexes: dict[str, list[Bar]],
    market_by_symbol: dict[str, str],
    minimum_breadth_coverage: float = MIN_BREADTH_COVERAGE,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[dict[str, Any]]], dict[str, list[date]]]:
    """Build deterministic completed-session market and frozen-cohort state rows."""
    symbols_by_market = {
        market: sorted(symbol for symbol in market_by_symbol if market_by_symbol[symbol] == market)
        for market in MARKETS
    }
    states: list[dict[str, Any]] = []
    stock_states: dict[tuple[str, str], list[dict[str, Any]]] = {}
    sessions_by_market: dict[str, list[date]] = {}
    stock_maps = {
        symbol: {bar.time.astimezone(KST).date(): bar for bar in bars}
        for symbol, bars in prices_by_symbol.items()
    }
    for market in MARKETS:
        market_bars = sorted(indexes[market], key=lambda bar: bar.time)
        index_map = {bar.time.astimezone(KST).date(): bar for bar in market_bars}
        sessions = sorted(index_map)
        sessions_by_market[market] = sessions
        for index, session in enumerate(sessions):
            if session < POOL_START or session > POOL_END:
                continue
            current_index_bar = index_map[session]
            returns_by_window: dict[int, dict[str, float | None]] = {1: {}, 5: {}, 20: {}}
            rows_by_symbol: dict[str, dict[str, Any]] = {}
            for symbol in symbols_by_market[market]:
                bars_by_date = stock_maps.get(symbol, {})
                current = bars_by_date.get(session)
                if current is None:
                    continue
                returns = {days: _return_at(bars_by_date, sessions, index, days) for days in (1, 5, 20)}
                for days, value in returns.items():
                    returns_by_window[days][symbol] = value
                turnover_prior = _median_prior_value(bars_by_date, sessions, index, "turnover_krw", 20)
                volume_prior = _median_prior_value(bars_by_date, sessions, index, "volume", 20)
                volatility20 = _volatility_at(bars_by_date, sessions, index, 20)
                rows_by_symbol[symbol] = {
                    "symbol": symbol,
                    "market": market,
                    "date": session.isoformat(),
                    "return_1d_pct": returns[1],
                    "return_5d_pct": returns[5],
                    "return_20d_pct": returns[20],
                    "volatility_20d_pct": volatility20,
                    "turnover_krw": current.turnover_krw,
                    "volume": current.volume,
                    "turnover_ratio_20d": (
                        current.turnover_krw / turnover_prior
                        if current.turnover_krw is not None and turnover_prior not in (None, 0.0) else None
                    ),
                    "volume_ratio_20d": (
                        current.volume / volume_prior if volume_prior not in (None, 0.0) else None
                    ),
                }
            breadth = build_breadth_observation(
                market=market,
                session=session,
                eligible_count=len(symbols_by_market[market]),
                returns_by_window=returns_by_window,
                minimum_coverage=minimum_breadth_coverage,
            )
            turn_values = [
                row["turnover_krw"] for row in rows_by_symbol.values() if row["turnover_krw"] is not None
            ]
            volume_values = [row["volume"] for row in rows_by_symbol.values() if row["volume"] is not None]
            prior_market_turnovers: list[float] = []
            if index >= 20:
                for prior_day in sessions[index - 20:index]:
                    prior_values = [
                        bar.turnover_krw
                        for symbol in symbols_by_market[market]
                        if (bar := stock_maps.get(symbol, {}).get(prior_day)) is not None
                        and bar.turnover_krw is not None
                    ]
                    if prior_values:
                        prior_market_turnovers.append(float(sum(prior_values)))
            prior_market_median = (
                statistics.median(prior_market_turnovers)
                if len(prior_market_turnovers) >= 15 else None
            )
            current_market_turnover = sum(turn_values) if turn_values else None
            record: dict[str, Any] = {
                "market": market,
                "date": session.isoformat(),
                "timestamp_semantics": "completed KRX session close; KIS session date at 00:00 Asia/Seoul",
                "index_close": current_index_bar.close,
                "return_1d_pct": _return_at(index_map, sessions, index, 1),
                "return_3d_pct": _return_at(index_map, sessions, index, 3),
                "return_5d_pct": _return_at(index_map, sessions, index, 5),
                "return_10d_pct": _return_at(index_map, sessions, index, 10),
                "return_20d_pct": _return_at(index_map, sessions, index, 20),
                "return_40d_pct": _return_at(index_map, sessions, index, 40),
                "realized_volatility_5d_pct": _volatility_at(index_map, sessions, index, 5),
                "realized_volatility_10d_pct": _volatility_at(index_map, sessions, index, 10),
                "realized_volatility_20d_pct": _volatility_at(index_map, sessions, index, 20),
                "distance_from_20d_high_pct": None,
                "distance_from_60d_high_pct": None,
                "rolling_20d_drawdown_pct": None,
                "rolling_60d_drawdown_pct": None,
                "days_since_20d_high": None,
                "days_since_60d_high": None,
                "range_5d_pct": _range_at(index_map, sessions, index, 5),
                "range_20d_pct": _range_at(index_map, sessions, index, 20),
                "market_turnover_krw": current_market_turnover,
                "market_turnover_ratio_20d": (
                    current_market_turnover / prior_market_median
                    if current_market_turnover is not None and prior_market_median not in (None, 0.0) else None
                ),
                "market_turnover_observed_symbols": len(turn_values),
                "market_volume": sum(volume_values) if volume_values else None,
                "market_volume_observed_symbols": len(volume_values),
                "market_turnover_missing_not_filled": True,
                "market_volume_missing_not_filled": True,
                **breadth,
            }
            record.update(_rolling_price_context(index_map, sessions, index, 20))
            record.update(_rolling_price_context(index_map, sessions, index, 60))
            record["stress_signal"] = (
                record["return_20d_pct"] is not None
                and record["return_20d_pct"] <= PHASE11_TRIGGER_MAX_RETURN_20D_PCT
            )
            record["stress_depth_state"] = classify_stress_depth(record["return_20d_pct"])
            record["stress_speed_state"] = classify_stress_speed(
                record["return_5d_pct"], record["return_20d_pct"], record["days_since_20d_high"],
            )
            record["breadth_primary_eligible"] = record["data_quality"] == "PASS"
            states.append(record)
            stock_states[(market, session.isoformat())] = [rows_by_symbol[symbol] for symbol in sorted(rows_by_symbol)]
    states.sort(key=lambda row: (row["date"], row["market"]))
    return states, stock_states, sessions_by_market


def _period_name(day: date) -> str | None:
    if DEVELOPMENT[0] <= day <= DEVELOPMENT[1]:
        return "development"
    if VALIDATION[0] <= day <= VALIDATION[1]:
        return "validation"
    return None


def _past_beta_at(
    symbol_bars: dict[date, Bar],
    index_bars: dict[date, Bar],
    sessions: list[date],
    index: int,
) -> float | None:
    first_return = max(1, index - BETA_LOOKBACK_SESSIONS + 1)
    stock_returns: list[float | None] = []
    market_returns: list[float | None] = []
    for position in range(first_return, index + 1):
        session = sessions[position]
        prior_session = sessions[position - 1]
        stock_now, stock_before = symbol_bars.get(session), symbol_bars.get(prior_session)
        market_now, market_before = index_bars.get(session), index_bars.get(prior_session)
        stock_returns.append(
            (stock_now.close / stock_before.close - 1.0) * 100.0
            if stock_now is not None and stock_before is not None and stock_before.close > 0 else None
        )
        market_returns.append(
            (market_now.close / market_before.close - 1.0) * 100.0
            if market_now is not None and market_before is not None and market_before.close > 0 else None
        )
    return rolling_beta(stock_returns, market_returns, minimum_observations=BETA_MIN_OBSERVATIONS)


def _beta_state(value: float | None) -> str | None:
    if value is None:
        return None
    if value <= 0.75:
        return "LOW_BETA"
    if value <= 1.25:
        return "MID_BETA"
    return "HIGH_BETA"


def build_phase11_style_events(
    *,
    states: list[dict[str, Any]],
    stock_states: dict[tuple[str, str], list[dict[str, Any]]],
    prices_by_symbol: dict[str, list[Bar]],
    indexes: dict[str, list[Bar]],
    sessions_by_market: dict[str, list[date]],
    market_by_symbol: dict[str, str],
    horizon_sessions: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recompute the Phase 11 signal only inside touched windows and closed exits."""
    if horizon_sessions not in (3, 5, 10):
        raise ValueError("Phase 12 descriptive horizons are limited to 3, 5, or 10 sessions")
    stock_maps = {
        symbol: {bar.time.astimezone(KST).date(): bar for bar in bars}
        for symbol, bars in prices_by_symbol.items()
    }
    index_maps = {
        market: {bar.time.astimezone(KST).date(): bar for bar in bars}
        for market, bars in indexes.items()
    }
    raw_events: list[dict[str, Any]] = []
    for market in MARKETS:
        sessions = sessions_by_market[market]
        session_positions = {day: index for index, day in enumerate(sessions)}
        for signal in states:
            if signal["market"] != market or not signal["stress_signal"]:
                continue
            signal_day = date.fromisoformat(signal["date"])
            period = _period_name(signal_day)
            if period is None:
                continue
            position = session_positions[signal_day]
            exit_position = position + horizon_sessions
            if exit_position >= len(sessions):
                continue
            path = sessions[position + 1:exit_position + 1]
            period_end = DEVELOPMENT[1] if period == "development" else VALIDATION[1]
            if len(path) != horizon_sessions or path[-1] > period_end:
                continue
            index_entry = index_maps[market].get(path[0])
            index_exit = index_maps[market].get(path[-1])
            if index_entry is None or index_exit is None or index_entry.open <= 0:
                continue
            matched_market_return = (index_exit.close / index_entry.open - 1.0) * 100.0
            for feature in stock_states.get((market, signal["date"]), []):
                symbol = feature["symbol"]
                bars_by_date = stock_maps.get(symbol, {})
                path_bars = [bars_by_date.get(day) for day in path]
                if any(bar is None for bar in path_bars):
                    continue
                entry_bar = path_bars[0]
                exit_bar = path_bars[-1]
                if entry_bar is None or exit_bar is None or entry_bar.open <= 0:
                    continue
                stock_return = (exit_bar.close / entry_bar.open - 1.0) * 100.0
                beta = _past_beta_at(
                    bars_by_date, index_maps[market], sessions, position,
                )
                decomposition = decompose_returns(stock_return, matched_market_return)
                event = {
                    "period": period,
                    "market": market,
                    "symbol": symbol,
                    "signal_date": signal["date"],
                    "entry_date": path[0].isoformat(),
                    "exit_date": path[-1].isoformat(),
                    "horizon_sessions": horizon_sessions,
                    "signal_index_return_5d_pct": signal["return_5d_pct"],
                    "signal_index_return_20d_pct": signal["return_20d_pct"],
                    "signal_depth_state": signal["stress_depth_state"],
                    "signal_speed_state": signal["stress_speed_state"],
                    "signal_volatility_20d_pct": signal["realized_volatility_20d_pct"],
                    "signal_volatility_regime": signal.get("volatility_regime"),
                    "signal_breadth_state": signal.get("breadth_state"),
                    "signal_breadth_5d_negative_fraction": signal.get("fraction_5d_return_negative"),
                    "signal_breadth_coverage_ratio": signal.get("coverage_ratio"),
                    "signal_breadth_primary_eligible": signal.get("breadth_primary_eligible", False),
                    "signal_dispersion_20d_pct": signal.get("cross_sectional_dispersion_20d_pct"),
                    "signal_dispersion_regime": signal.get("dispersion_regime"),
                    "signal_market_turnover_regime": signal.get("market_turnover_regime"),
                    "stock_return_20d_pct": feature.get("return_20d_pct"),
                    "stock_volatility_20d_pct": feature.get("volatility_20d_pct"),
                    "stock_turnover_krw": feature.get("turnover_krw"),
                    "stock_turnover_ratio_20d": feature.get("turnover_ratio_20d"),
                    "stock_volume_ratio_20d": feature.get("volume_ratio_20d"),
                    "stock_beta_60d": beta,
                    "stock_beta_state": _beta_state(beta),
                    "stock_return_3d_pct": decomposition["stock_return_pct"],
                    "matched_market_return_3d_pct": decomposition["market_return_pct"],
                    "market_excess_return_3d_pct": decomposition["excess_return_pct"],
                    "beta_adjusted_residual_3d_pct": beta_adjusted_residual(
                        stock_return, matched_market_return, beta,
                    ),
                }
                raw_events.append(event)
    raw_events.sort(key=lambda row: (row["period"], row["signal_date"], row["market"], row["symbol"]))
    nonoverlap_source = [
        {
            **event,
            "gross_return_pct": event["stock_return_3d_pct"],
        }
        for event in raw_events
    ]
    selected = non_overlapping_events(nonoverlap_source)
    selected_keys = {
        (event["period"], event["market"], event["symbol"], event["signal_date"]) for event in selected
    }
    for event in raw_events:
        key = (event["period"], event["market"], event["symbol"], event["signal_date"])
        event["non_overlapping"] = key in selected_keys
    return raw_events, [event for event in raw_events if event["non_overlapping"]]


def _period_signal_rows(states: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    return [
        row for row in states
        if row["stress_signal"] and _period_name(date.fromisoformat(row["date"])) == period
    ]


def _assign_development_regimes(states: list[dict[str, Any]]) -> dict[str, Any]:
    development_rows = [
        row for row in states
        if _period_name(date.fromisoformat(row["date"])) == "development"
        and row["data_quality"] == "PASS"
    ]
    volatility_thresholds = _tercile_thresholds(row["realized_volatility_20d_pct"] for row in development_rows)
    dispersion_thresholds = _tercile_thresholds(
        row["cross_sectional_dispersion_20d_pct"] for row in development_rows
    )
    liquidity_thresholds = _tercile_thresholds(row["market_turnover_ratio_20d"] for row in development_rows)
    for row in states:
        vol = row["realized_volatility_20d_pct"]
        dispersion = row["cross_sectional_dispersion_20d_pct"]
        row["volatility_regime"] = _tercile_state(vol, volatility_thresholds)
        row["dispersion_regime"] = _tercile_state(dispersion, dispersion_thresholds)
        liquidity_state = _tercile_state(row["market_turnover_ratio_20d"], liquidity_thresholds)
        row["market_turnover_regime"] = (
            "HIGH_TURNOVER_SHOCK" if liquidity_state == "HIGH" else liquidity_state
        )
    return {
        "volatility_20d_pct_tercile_cutoffs_development": volatility_thresholds,
        "dispersion_20d_pct_tercile_cutoffs_development": dispersion_thresholds,
        "market_turnover_ratio_20d_tercile_cutoffs_development": liquidity_thresholds,
        "threshold_population": "all Development daily market states with breadth coverage >= 80%; no outcome data",
    }


def _assign_event_characteristic_states(
    events: list[dict[str, Any]], thresholds: dict[str, list[float] | None] | None = None,
) -> dict[str, list[float] | None]:
    fields = {
        "stock_return_20d_pct": "recent_stock_return_20d_state",
        "stock_turnover_ratio_20d": "stock_turnover_ratio_20d_state",
        "stock_volatility_20d_pct": "stock_volatility_20d_state",
    }
    if thresholds is None:
        development_events = [event for event in events if event["period"] == "development"]
        thresholds = {
            field: _tercile_thresholds(event.get(field) for event in development_events)
            for field in fields
        }
    for event in events:
        for field, state_key in fields.items():
            event[state_key] = _tercile_state(event.get(field), thresholds[field])
    return thresholds


def _macro_cluster_episodes(episodes: list[dict[str, Any]]) -> None:
    """Join overlapping KOSPI/KOSDAQ episode intervals for dependence-aware resampling."""
    parent = list(range(len(episodes)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    for left in range(len(episodes)):
        start_left = date.fromisoformat(episodes[left]["episode_start"])
        end_left = date.fromisoformat(episodes[left]["episode_end"])
        for right in range(left + 1, len(episodes)):
            if episodes[left]["period"] != episodes[right]["period"]:
                continue
            if episodes[left]["market"] == episodes[right]["market"]:
                continue
            start_right = date.fromisoformat(episodes[right]["episode_start"])
            end_right = date.fromisoformat(episodes[right]["episode_end"])
            if start_left <= end_right and start_right <= end_left:
                union(left, right)
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(episodes)):
        groups[find(index)].append(index)
    ordered_groups = sorted(
        groups.values(), key=lambda indexes: min(episodes[index]["episode_start"] for index in indexes),
    )
    for number, indexes in enumerate(ordered_groups, start=1):
        cluster_id = f"{episodes[indexes[0]]['period']}-SC{number:03d}"
        for index in indexes:
            episodes[index]["macro_stress_cluster_id"] = cluster_id


def build_stress_episodes(
    *,
    states: list[dict[str, Any]],
    sessions_by_market: dict[str, list[date]],
    nonoverlap_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    event_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in nonoverlap_events:
        event_by_key[(event["period"], event["market"], event["signal_date"])].append(event)
    state_lookup = {(row["market"], row["date"]): row for row in states}
    for period, interval in (("development", DEVELOPMENT), ("validation", VALIDATION)):
        for market in MARKETS:
            period_states = [
                row for row in states
                if row["market"] == market
                and interval[0].isoformat() <= row["date"] <= interval[1].isoformat()
            ]
            signal_dates = [date.fromisoformat(row["date"]) for row in period_states if row["stress_signal"]]
            grouped = group_stress_episodes(
                market,
                signal_dates,
                sessions_by_market[market],
                max_non_signal_sessions=MAX_NON_SIGNAL_SESSIONS,
            )
            session_indexes = {day: index for index, day in enumerate(sessions_by_market[market])}
            for group in grouped:
                start = date.fromisoformat(group["episode_start"])
                end = date.fromisoformat(group["episode_end"])
                first_index, last_index = session_indexes[start], session_indexes[end]
                interval_dates = sessions_by_market[market][first_index:last_index + 1]
                interval_rows = [
                    state_lookup[(market, day.isoformat())]
                    for day in interval_dates if (market, day.isoformat()) in state_lookup
                ]
                peak = min(
                    interval_rows,
                    key=lambda row: row["return_20d_pct"] if row["return_20d_pct"] is not None else math.inf,
                )
                episode_events = [
                    event
                    for signal_date in group["signal_dates"]
                    for event in event_by_key.get((period, market, signal_date), [])
                ]
                end_position = session_indexes[end]
                recovery_rows = []
                for offset in range(21):
                    position = end_position + offset
                    if position >= len(sessions_by_market[market]):
                        break
                    recovery_date = sessions_by_market[market][position]
                    if recovery_date > interval[1]:
                        break
                    row = state_lookup.get((market, recovery_date.isoformat()))
                    if row is not None:
                        end_close = state_lookup.get((market, end.isoformat()), {}).get("index_close")
                        recovery_rows.append({
                            "sessions_after_episode_end": offset,
                            "date": recovery_date.isoformat(),
                            "index_return_from_episode_end_pct": (
                                (row["index_close"] / end_close - 1.0) * 100.0
                                if end_close and end_close > 0 else None
                            ),
                        })
                breadth_values = [row["fraction_5d_return_negative"] for row in interval_rows]
                dispersion_values = [row["cross_sectional_dispersion_20d_pct"] for row in interval_rows]
                vol_values = [row["realized_volatility_20d_pct"] for row in interval_rows]
                market_forward = [event["matched_market_return_3d_pct"] for event in episode_events]
                stock_forward = [event["stock_return_3d_pct"] for event in episode_events]
                excess_forward = [event["market_excess_return_3d_pct"] for event in episode_events]
                residual_forward = [event["beta_adjusted_residual_3d_pct"] for event in episode_events]
                record = {
                    **group,
                    "period": period,
                    "stress_depth_state": classify_stress_depth(peak["return_20d_pct"]),
                    "stress_speed_state": classify_stress_speed(
                        peak["return_5d_pct"], peak["return_20d_pct"], peak["days_since_20d_high"],
                    ),
                    "maximum_drawdown_20d_pct": min(
                        (row["rolling_20d_drawdown_pct"] for row in interval_rows
                         if row["rolling_20d_drawdown_pct"] is not None), default=None,
                    ),
                    "minimum_20d_return_pct": min(
                        (row["return_20d_pct"] for row in interval_rows if row["return_20d_pct"] is not None),
                        default=None,
                    ),
                    "maximum_realized_volatility_20d_pct": max(
                        (value for value in vol_values if value is not None), default=None,
                    ),
                    "volatility_regime_at_peak_stress": peak.get("volatility_regime"),
                    "minimum_breadth_5d_negative_fraction": min(
                        (value for value in breadth_values if value is not None), default=None,
                    ),
                    "breadth_state_at_minimum": min(
                        (row for row in interval_rows if row["fraction_5d_return_negative"] is not None),
                        key=lambda row: row["fraction_5d_return_negative"], default={},
                    ).get("breadth_state"),
                    "maximum_dispersion_20d_pct": max(
                        (value for value in dispersion_values if value is not None), default=None,
                    ),
                    "dispersion_regime_at_peak_stress": peak.get("dispersion_regime"),
                    "raw_stock_event_count": sum(
                        1 for event in nonoverlap_events
                        if event["period"] == period and event["market"] == market
                        and event["signal_date"] in group["signal_dates"]
                    ),
                    "non_overlapping_stock_event_count": len(episode_events),
                    "forward_market_return_3d_mean_pct": _mean(market_forward),
                    "forward_stock_return_3d_mean_pct": _mean(stock_forward),
                    "market_excess_return_3d_mean_pct": _mean(excess_forward),
                    "beta_adjusted_residual_3d_mean_pct": _mean(residual_forward),
                    "outcome_class": (
                        "SUCCESSFUL" if _mean(stock_forward) is not None and _mean(stock_forward) > 0
                        else "FAILED" if _mean(stock_forward) is not None else "NO_EXECUTABLE_EVENTS"
                    ),
                    "recovery_path": recovery_rows,
                    "recovery_path_complete_20_sessions": len(recovery_rows) == 21,
                    "recovery_path_censored_at_touched_period_end": len(recovery_rows) < 21,
                }
                episodes.append(record)
    episodes.sort(key=lambda row: (row["period"], row["episode_start"], row["market"]))
    _macro_cluster_episodes(episodes)
    episode_lookup = {
        (row["period"], row["market"], signal_date): row["episode_id"]
        for row in episodes for signal_date in row["signal_dates"]
    }
    macro_lookup = {row["episode_id"]: row["macro_stress_cluster_id"] for row in episodes}
    for event in nonoverlap_events:
        key = (event["period"], event["market"], event["signal_date"])
        event["episode_id"] = episode_lookup.get(key)
        event["macro_stress_cluster_id"] = macro_lookup.get(event["episode_id"])
    return episodes


def _mean_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    stock = [event["stock_return_3d_pct"] for event in events]
    market = [event["matched_market_return_3d_pct"] for event in events]
    excess = [event["market_excess_return_3d_pct"] for event in events]
    residual = [event["beta_adjusted_residual_3d_pct"] for event in events]
    positive = [value for value in stock if value is not None and value > 0]
    negative = [-value for value in stock if value is not None and value < 0]
    stock_mean = _mean(stock)
    return {
        "event_count": len(events),
        "unique_signal_dates": len({event["signal_date"] for event in events}),
        "market_signal_date_count": len({(event["market"], event["signal_date"]) for event in events}),
        "gross_mean_pct": stock_mean,
        "gross_median_pct": statistics.median([value for value in stock if value is not None]) if any(
            value is not None for value in stock
        ) else None,
        "net_mean_pct_at_1x_cost": stock_mean - BASE_ROUND_TRIP_COST_PCT if stock_mean is not None else None,
        "net_mean_pct_at_1_5x_cost": stock_mean - BASE_ROUND_TRIP_COST_PCT * 1.5 if stock_mean is not None else None,
        "net_mean_pct_at_2x_cost": stock_mean - BASE_ROUND_TRIP_COST_PCT * 2.0 if stock_mean is not None else None,
        "matched_market_return_mean_pct": _mean(market),
        "market_excess_mean_pct": _mean(excess),
        "beta_adjusted_residual_mean_pct": _mean(residual),
        "win_rate_pct": (
            sum(value > 0 for value in stock if value is not None)
            / sum(value is not None for value in stock) * 100.0
            if any(value is not None for value in stock) else None
        ),
        "payoff_ratio": statistics.fmean(positive) / statistics.fmean(negative) if positive and negative else None,
        "mean_rolling_beta": _mean(event["stock_beta_60d"] for event in events),
        "low_beta_event_count": sum(event.get("stock_beta_state") == "LOW_BETA" for event in events),
        "mid_beta_event_count": sum(event.get("stock_beta_state") == "MID_BETA" for event in events),
        "high_beta_event_count": sum(event.get("stock_beta_state") == "HIGH_BETA" for event in events),
        "independent_macro_episode_count": len({
            event.get("macro_stress_cluster_id") for event in events
            if event.get("macro_stress_cluster_id") is not None
        }),
    }


def _period_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    periods: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        periods[event["period"]].append(event)
    return {period: _mean_metrics(group) for period, group in sorted(periods.items())}


def _group_metrics(events: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        value = event.get(key)
        groups[str(value) if value is not None else "UNAVAILABLE"].append(event)
    output: list[dict[str, Any]] = []
    for state, rows in sorted(groups.items()):
        output.append({"state": state, **_period_metrics(rows)})
    return output


def _summarize_distribution(values: Iterable[float | int | None]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not ordered:
        return {
            "n": 0, "mean": None, "median": None, "p05": None, "p10": None,
            "p25": None, "p75": None, "p90": None, "p95": None,
        }

    def q(probability: float) -> float:
        position = (len(ordered) - 1) * probability
        low, high = math.floor(position), math.ceil(position)
        return ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {
        "n": len(ordered),
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "p05": q(0.05),
        "p10": q(0.10),
        "p25": q(0.25),
        "p75": q(0.75),
        "p90": q(0.90),
        "p95": q(0.95),
    }


def build_development_validation_shift(
    *,
    states: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare touched-period feature distributions; outcome features are diagnostic only."""
    stress_rows = [row for row in states if row["stress_signal"] and row["breadth_primary_eligible"]]
    episode_duration = {
        (episode["period"], episode["market"], day): episode["duration_sessions"]
        for episode in episodes for day in episode["signal_dates"]
    }
    signal_features: dict[str, dict[str, list[float | None]]] = {
        "development": defaultdict(list), "validation": defaultdict(list),
    }
    for row in stress_rows:
        period = _period_name(date.fromisoformat(row["date"]))
        if period is None:
            continue
        values: dict[str, float | None] = {
            "market_return_20d_pct": row["return_20d_pct"],
            "market_return_5d_pct": row["return_5d_pct"],
            "realized_volatility_20d_pct": row["realized_volatility_20d_pct"],
            "breadth_fraction_5d_negative": row["fraction_5d_return_negative"],
            "dispersion_20d_pct": row["cross_sectional_dispersion_20d_pct"],
            "rolling_drawdown_20d_pct": row["rolling_20d_drawdown_pct"],
            "stress_duration_sessions": episode_duration.get((period, row["market"], row["date"])),
            "log1p_market_turnover_krw": (
                math.log1p(row["market_turnover_krw"]) if row["market_turnover_krw"] is not None else None
            ),
            "market_turnover_ratio_20d": row.get("market_turnover_ratio_20d"),
        }
        for key, value in values.items():
            signal_features[period][key].append(value)
    event_features: dict[str, dict[str, list[float | None]]] = {
        "development": defaultdict(list), "validation": defaultdict(list),
    }
    for event in events:
        period = event["period"]
        for key, source in (
            ("rolling_beta_60d", "stock_beta_60d"),
            ("stock_turnover_ratio_20d", "stock_turnover_ratio_20d"),
            ("market_adjusted_rebound_pct", "market_excess_return_3d_pct"),
            ("beta_adjusted_residual_pct", "beta_adjusted_residual_3d_pct"),
        ):
            event_features[period][key].append(event.get(source))
    combined: dict[str, dict[str, list[float | None]]] = {
        period: defaultdict(list) for period in ("development", "validation")
    }
    for period in combined:
        for key, values in signal_features[period].items():
            combined[period][key].extend(values)
        for key, values in event_features[period].items():
            combined[period][key].extend(values)
    all_keys = sorted(set().union(*(items.keys() for items in combined.values())))
    summaries: dict[str, Any] = {}
    for key in all_keys:
        dev = _summarize_distribution(combined["development"].get(key, []))
        val = _summarize_distribution(combined["validation"].get(key, []))
        dev_sd = _stdev(combined["development"].get(key, []))
        val_sd = _stdev(combined["validation"].get(key, []))
        if dev_sd is not None and val_sd is not None and dev["n"] + val["n"] > 2:
            pooled = math.sqrt(
                ((dev["n"] - 1) * dev_sd**2 + (val["n"] - 1) * val_sd**2)
                / (dev["n"] + val["n"] - 2)
            )
        else:
            pooled = None
        smd = (val["mean"] - dev["mean"]) / pooled if pooled not in (None, 0.0) else None
        summaries[key] = {
            "development": dev,
            "validation": val,
            "validation_minus_development_mean": (
                val["mean"] - dev["mean"] if val["mean"] is not None and dev["mean"] is not None else None
            ),
            "standardized_mean_difference": smd,
            "evidence_kind": "forward outcome diagnostic" if "rebound" in key or "residual" in key else "completed signal-date feature",
        }
    shift_ranking = sorted(
        (
            {"feature": key, "absolute_standardized_mean_difference": abs(value["standardized_mean_difference"])}
            for key, value in summaries.items() if value["standardized_mean_difference"] is not None
        ),
        key=lambda item: (-item["absolute_standardized_mean_difference"], item["feature"]),
    )
    return {
        "features": summaries,
        "largest_standardized_shifts": shift_ranking,
        "low_coverage_market_signal_rows_excluded": sum(
            row["stress_signal"] and not row["breadth_primary_eligible"] for row in states
        ),
        "note": "Market feature samples are market-signal dates; stock beta/liquidity/outcome samples are executed stock events. No signal rule is selected from these shifts.",
    }


def build_episode_concentration(episodes: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Quantify how much stock return contribution is carried by a few episodes."""
    result: dict[str, Any] = {}
    for period in ("development", "validation"):
        by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            if event["period"] == period and event.get("episode_id"):
                by_episode[event["episode_id"]].append(event)
        contributions = []
        for episode in episodes:
            if episode["period"] != period:
                continue
            trades = by_episode.get(episode["episode_id"], [])
            if not trades:
                continue
            trade_returns = [event["stock_return_3d_pct"] for event in trades]
            contributions.append({
                "episode_id": episode["episode_id"],
                "market": episode["market"],
                "macro_stress_cluster_id": episode["macro_stress_cluster_id"],
                "signal_date_count": len(episode["signal_dates"]),
                "non_overlapping_event_count": len(trades),
                "gross_return_point_contribution_pct": sum(trade_returns),
                "positive_trade_contribution_pct": sum(max(value, 0.0) for value in trade_returns),
                "mean_gross_return_pct": _mean(trade_returns),
            })
        contributions.sort(key=lambda row: (-row["gross_return_point_contribution_pct"], row["episode_id"]))
        positive_total = sum(row["positive_trade_contribution_pct"] for row in contributions)
        net_total = sum(row["gross_return_point_contribution_pct"] for row in contributions)
        top: dict[str, Any] = {}
        for number in (1, 3, 5):
            selected = contributions[:number]
            point_sum = sum(row["gross_return_point_contribution_pct"] for row in selected)
            positive_sum = sum(row["positive_trade_contribution_pct"] for row in selected)
            top[f"top_{number}"] = {
                "episode_count": len(selected),
                "gross_return_point_contribution_pct": point_sum,
                "share_of_total_positive_trade_contribution_pct": (
                    positive_sum / positive_total * 100.0 if positive_total else None
                ),
                "share_of_total_net_return_points_pct": point_sum / net_total * 100.0 if net_total else None,
                "episodes": selected,
            }
        result[period] = {
            "episode_count_with_executions": len(contributions),
            "total_net_return_points_pct": net_total,
            "total_positive_trade_contribution_pct": positive_total,
            "top_contribution_share_pct": (
                contributions[0]["positive_trade_contribution_pct"] / positive_total * 100.0
                if contributions and positive_total else None
            ),
            **top,
            "all_episode_contributions": contributions,
        }
    return result


def build_episode_cluster_bootstrap(
    events: list[dict[str, Any]],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Resample joint overlapping stress-episode clusters, never individual rows."""
    results: dict[str, Any] = {}
    metrics = {
        "gross_stock_return_pct": "stock_return_3d_pct",
        "matched_market_return_pct": "matched_market_return_3d_pct",
        "market_excess_return_pct": "market_excess_return_3d_pct",
        "beta_adjusted_residual_pct": "beta_adjusted_residual_3d_pct",
    }
    for period in ("development", "validation"):
        period_events = [
            row for row in events
            if row["period"] == period and row.get("macro_stress_cluster_id") is not None
        ]
        by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in period_events:
            by_cluster[event["macro_stress_cluster_id"]].append(event)
        clusters = sorted(by_cluster)
        if len(clusters) < 2:
            results[period] = {"status": "INSUFFICIENT_INDEPENDENT_CLUSTERS", "cluster_count": len(clusters)}
            continue
        rng = random.Random(seed + (1 if period == "development" else 2))
        samples: dict[str, list[float]] = {metric: [] for metric in metrics}
        for _ in range(iterations):
            chosen = [rng.choice(clusters) for _ in clusters]
            draw = [event for cluster in chosen for event in by_cluster[cluster]]
            for metric, field in metrics.items():
                value = _mean(event.get(field) for event in draw)
                if value is not None:
                    samples[metric].append(value)
        results[period] = {
            "status": "PASS",
            "cluster_count": len(clusters),
            "event_count": len(period_events),
            "iterations": iterations,
            "seed": seed + (1 if period == "development" else 2),
            "interval_level_pct": 90,
            "estimates": {
                metric: {
                    "lower_90_pct": _summarize_distribution(values)["p05"],
                    "median_pct": _summarize_distribution(values)["median"],
                    "upper_90_pct": _summarize_distribution(values)["p95"],
                }
                for metric, values in samples.items()
            },
            "method": "resample joint cross-market stress-episode clusters with replacement; retain all stock events per sampled cluster",
            "p_values_computed": False,
        }
    return results


def build_chronological_blocks(
    *,
    states: list[dict[str, Any]],
    events: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
) -> dict[str, Any]:
    blocks = ((2023, 1), (2023, 2), (2024, 1), (2024, 2), (2025, 1))
    by_block: dict[str, dict[str, Any]] = {}
    for year, half in blocks:
        label = f"{year}-H{half}"
        block_states = [
            row for row in states
            if date.fromisoformat(row["date"]).year == year
            and (1 if date.fromisoformat(row["date"]).month <= 6 else 2) == half
            and row["stress_signal"]
        ]
        block_events = [
            row for row in events
            if date.fromisoformat(row["signal_date"]).year == year
            and (1 if date.fromisoformat(row["signal_date"]).month <= 6 else 2) == half
        ]
        block_episodes = [row for row in episodes if row["episode_start"][:4] == str(year)
                          and int(row["episode_start"][5:7]) in (range(1, 7) if half == 1 else range(7, 13))]
        unique_market_dates = {(row["market"], row["date"]): row for row in block_states}
        outcomes = _mean_metrics(block_events)
        by_block[label] = {
            "market_signal_date_count": len(unique_market_dates),
            "unique_signal_dates": len({day for _, day in unique_market_dates}),
            "stress_episode_count": len(block_episodes),
            "mean_stress_depth_20d_return_pct": _mean(row["return_20d_pct"] for row in block_states),
            "mean_realized_volatility_20d_pct": _mean(row["realized_volatility_20d_pct"] for row in block_states),
            "mean_breadth_5d_negative_fraction": _mean(row["fraction_5d_return_negative"] for row in block_states),
            "mean_cross_sectional_dispersion_20d_pct": _mean(
                row["cross_sectional_dispersion_20d_pct"] for row in block_states
            ),
            "index_forward_3d_return_pct_stock_trade_weighted": outcomes["matched_market_return_mean_pct"],
            "stock_forward_3d_return_pct": outcomes["gross_mean_pct"],
            "market_adjusted_stock_forward_3d_pct": outcomes["market_excess_mean_pct"],
            "beta_adjusted_residual_3d_pct": outcomes["beta_adjusted_residual_mean_pct"],
            "non_overlapping_stock_event_count": len(block_events),
        }
    return {
        "blocks": by_block,
        "interpretation": "Touched Development and Validation split into the five requested chronological half-years.",
    }


def build_episode_outcome_comparison(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "minimum_20d_return_pct",
        "maximum_drawdown_20d_pct",
        "maximum_realized_volatility_20d_pct",
        "minimum_breadth_5d_negative_fraction",
        "maximum_dispersion_20d_pct",
        "duration_sessions",
        "forward_market_return_3d_mean_pct",
        "market_excess_return_3d_mean_pct",
        "beta_adjusted_residual_3d_mean_pct",
    )
    output: dict[str, Any] = {}
    for period in ("development", "validation"):
        output[period] = {}
        for label in ("SUCCESSFUL", "FAILED", "NO_EXECUTABLE_EVENTS"):
            selected = [
                episode for episode in episodes
                if episode["period"] == period and episode["outcome_class"] == label
            ]
            output[period][label] = {
                "episode_count": len(selected),
                "features": {
                    field: _summarize_distribution(episode.get(field) for episode in selected)
                    for field in fields
                },
                "episode_ids": [episode["episode_id"] for episode in selected],
            }
    return {
        "classification_rule": "SUCCESSFUL when the episode's non-overlap mean gross stock return is > 0%; FAILED when <= 0%; no return threshold was fit.",
        "by_period": output,
    }


def build_cost_model_audit() -> dict[str, Any]:
    return {
        "as_of": "2026-09-30",
        "official_research_assumption_preserved": {
            "broker_fee_each_side_pct": 0.015,
            "sell_tax_pct": 0.20,
            "slippage_each_side_pct": 0.15,
            "round_trip_cost_pct": BASE_ROUND_TRIP_COST_PCT,
        },
        "components": [
            {
                "component": "broker_fee_each_side",
                "assumption_pct": 0.015,
                "status": "ASSUMED",
                "evidence": "No account type or fee schedule is pinned to this historical cohort. The official KIS page shows account/channel-specific rates; its BanKIS KRX domestic stock online rate is 0.0140527% as of 2025-10-27, while branch-account rates differ.",
                "source": "https://securities.koreainvestment.com/main/customer/guide/_static/TF04ae010000.jsp?tab=3",
            },
            {
                "component": "sell_transaction_tax",
                "assumption_pct": 0.20,
                "status": "VERIFIED_CURRENT",
                "evidence": "KIS public fee/tax schedule lists KOSPI sale tax 0.05% plus 0.15% special rural tax and KOSDAQ sale tax 0.20%; the schedule is market-specific and shown on the current customer guide page.",
                "source": "https://securities.koreainvestment.com/main/customer/guide/_static/TF04ae010000.jsp?tab=3",
            },
            {
                "component": "slippage_each_side",
                "assumption_pct": 0.15,
                "status": "CONSERVATIVE",
                "evidence": "Fixed 15 bp per side research haircut; no historical execution or order-book sample verifies realized slippage. It does not guarantee a conservative bound for illiquid names or stress sessions.",
                "source": None,
            },
        ],
        "round_trip_calculation_pct": "2 * 0.015 + 0.20 + 2 * 0.15 = 0.53",
        "historical_results_changed": False,
    }


def _episode_top_positive_share(events: list[dict[str, Any]]) -> float | None:
    by_cluster: dict[str, float] = defaultdict(float)
    total_positive = 0.0
    for event in events:
        value = event["stock_return_3d_pct"]
        if value is None:
            continue
        positive = max(value, 0.0)
        total_positive += positive
        if event.get("macro_stress_cluster_id") is not None:
            by_cluster[event["macro_stress_cluster_id"]] += positive
    return max(by_cluster.values(), default=0.0) / total_positive * 100.0 if total_positive else None


MECHANISM_CANDIDATES = (
    ("BROAD_CAPITULATION_REBOUND", "signal_breadth_state", "BROAD_SELLOFF"),
    ("VOLATILITY_SHOCK_RECOVERY", "signal_volatility_regime", "HIGH"),
    ("DISPERSION_COMPRESSION_RECOVERY", "signal_dispersion_regime", "LOW"),
    ("FAST_SHOCK_RECOVERY", "signal_speed_state", "FAST_SHOCK"),
    ("LIQUIDITY_SHOCK_NORMALIZATION", "signal_market_turnover_regime", "HIGH_TURNOVER_SHOCK"),
    ("HIGH_BETA_STRESS_REBOUND", "stock_beta_state", "HIGH_BETA"),
)


def evaluate_new_mechanism_gate(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply one fixed evidence gate to simple one-feature mechanism hypotheses."""
    eligible_events = [event for event in events if event.get("signal_breadth_primary_eligible")]
    tested: list[dict[str, Any]] = []
    for name, field, condition in MECHANISM_CANDIDATES:
        by_period: dict[str, Any] = {}
        qualified_both = True
        direction_both = True
        for period in ("development", "validation"):
            selected = [
                event for event in eligible_events
                if event["period"] == period and event.get(field) == condition
            ]
            metrics = _mean_metrics(selected)
            concentration = _episode_top_positive_share(selected)
            market_episode_counts = {
                market: len({
                    event.get("episode_id") for event in selected
                    if event["market"] == market and event.get("episode_id") is not None
                })
                for market in MARKETS
            }
            enough = (
                metrics["independent_macro_episode_count"]
                >= MECHANISM_GATE["minimum_independent_macro_episodes_per_period"]
                and metrics["market_signal_date_count"] >= MECHANISM_GATE["minimum_market_signal_dates_per_period"]
                and {event["market"] for event in selected} == set(MARKETS)
                and all(
                    count >= MECHANISM_GATE["minimum_market_specific_episodes_per_market_and_period"]
                    for count in market_episode_counts.values()
                )
            )
            direction = (
                metrics["gross_mean_pct"] is not None and metrics["gross_mean_pct"] > 0
                and metrics["market_excess_mean_pct"] is not None and metrics["market_excess_mean_pct"] > 0
                and metrics["beta_adjusted_residual_mean_pct"] is not None
                and metrics["beta_adjusted_residual_mean_pct"] > BASE_ROUND_TRIP_COST_PCT
            )
            not_concentrated = concentration is not None and concentration <= MECHANISM_GATE[
                "maximum_top_episode_positive_contribution_share_pct"
            ]
            passes = enough and direction and not_concentrated
            qualified_both = qualified_both and passes
            direction_both = direction_both and direction
            by_period[period] = {
                **metrics,
                "market_specific_episode_counts": market_episode_counts,
                "top_macro_episode_positive_contribution_share_pct": concentration,
                "minimum_sample_gate_pass": enough,
                "direction_and_market_adjusted_scale_gate_pass": direction,
                "concentration_gate_pass": not_concentrated,
                "period_gate_pass": passes,
            }
        tested.append({
            "mechanism": name,
            "single_feature_condition": {field: condition},
            "by_period": by_period,
            "same_direction_and_scale_in_both_periods": direction_both,
            "passes_all_new_mechanism_gates": qualified_both,
        })
    passing = [result for result in tested if result["passes_all_new_mechanism_gates"]]
    direction_consistent = [result for result in tested if result["same_direction_and_scale_in_both_periods"]]
    selected = passing[0] if passing else None
    status = "SUPPORTED" if selected else "WEAK" if direction_consistent else "NOT_SUPPORTED"
    candidate = None
    if selected:
        candidate = {
            "name": selected["mechanism"],
            "feature_condition": selected["single_feature_condition"],
            "market_state": "Matched KOSPI/KOSDAQ index 20-session return <= -4% at the completed signal close.",
            "signal_timing": "Evaluate after the signal session is complete; no same-session entry.",
            "entry": "Next market session open.",
            "exit": "Close of the third session beginning with the entry session.",
            "holding_horizon_sessions": 3,
            "market_scope": ["KOSPI", "KOSDAQ"],
            "cohort": "Phase 4 frozen current-listing cohort; 100 symbols, 50 per market.",
            "data_source": "Phase 11 KIS adjusted daily OHLCV and KOSPI/KOSDAQ daily indexes.",
            "cost_model": "0.015% fee per side + 0.20% sell tax + 0.15% slippage per side = 0.53% round trip.",
            "missing_data_rule": "No forward fill or zero fill; omit symbol/date when required signal, entry, exit, or beta inputs are missing.",
            "episode_duplication_rule": "Stress signal dates within a market merge when separated by at most two non-signal sessions; per-symbol executions do not overlap.",
            "status": "RESEARCH_CANDIDATE",
        }
    return {
        "market_stress_mechanism": status,
        "candidate_status": "RESEARCH_CANDIDATE" if candidate else "NOT_CREATED",
        "candidate": candidate,
        "mechanism_gate": MECHANISM_GATE,
        "tested_mechanisms_in_predeclared_structural_order": tested,
        "selection_rule": "Choose the first mechanism in the fixed interpretability order that passes both touched-period gates; never rank by return size.",
        "confirmation_access_authorized": False,
    }


def build_root_cause(
    *,
    period_metrics: dict[str, Any],
    shift: dict[str, Any],
    concentration: dict[str, Any],
    episodes: list[dict[str, Any]],
    hypothesis: dict[str, Any],
) -> dict[str, Any]:
    dev = period_metrics.get("development", {})
    val = period_metrics.get("validation", {})
    stock_change = (
        val.get("gross_mean_pct") - dev.get("gross_mean_pct")
        if val.get("gross_mean_pct") is not None and dev.get("gross_mean_pct") is not None else None
    )
    market_change = (
        val.get("matched_market_return_mean_pct") - dev.get("matched_market_return_mean_pct")
        if val.get("matched_market_return_mean_pct") is not None
        and dev.get("matched_market_return_mean_pct") is not None else None
    )
    excess_dev = dev.get("market_excess_mean_pct")
    excess_val = val.get("market_excess_mean_pct")
    explanation_ratio = (
        dev.get("matched_market_return_mean_pct") / dev.get("gross_mean_pct") * 100.0
        if dev.get("gross_mean_pct") not in (None, 0.0)
        and dev.get("matched_market_return_mean_pct") is not None else None
    )
    market_beta = (
        "STRONG" if explanation_ratio is not None and explanation_ratio >= 70.0
        and (excess_dev is None or excess_dev < BASE_ROUND_TRIP_COST_PCT)
        else "MODERATE" if explanation_ratio is not None and explanation_ratio >= 40.0
        else "WEAK"
    )
    depth_shift = shift["features"].get("market_return_20d_pct", {}).get("standardized_mean_difference")
    composition_features = (
        "market_return_5d_pct", "realized_volatility_20d_pct", "breadth_fraction_5d_negative",
        "dispersion_20d_pct", "stress_duration_sessions", "rolling_drawdown_20d_pct",
    )
    composition_shifts = {
        feature: shift["features"].get(feature, {}).get("standardized_mean_difference")
        for feature in composition_features
    }
    strongest_composition_shift = max(
        (abs(value) for value in composition_shifts.values() if value is not None), default=None,
    )
    stress_composition = "STRONG" if strongest_composition_shift is not None and strongest_composition_shift >= 0.5 else (
        "MODERATE" if strongest_composition_shift is not None and strongest_composition_shift >= 0.2 else "WEAK"
    )
    episode_root = {}
    for period in ("development", "validation"):
        shares = concentration.get(period, {}).get("top_1", {}).get(
            "share_of_total_positive_trade_contribution_pct"
        )
        episode_root[period] = shares
    top1 = max((value for value in episode_root.values() if value is not None), default=None)
    concentration_status = "STRONG" if top1 is not None and top1 >= 50.0 else (
        "MODERATE" if top1 is not None and top1 >= 30.0 else "WEAK"
    )
    shift_features = shift["features"]
    def shift_strength(name: str) -> str:
        value = shift_features.get(name, {}).get("standardized_mean_difference")
        if value is None:
            return "WEAK"
        return "STRONG" if abs(value) >= 0.5 else "MODERATE" if abs(value) >= 0.2 else "WEAK"
    episode_regimes: dict[str, dict[str, Any]] = {}
    for period in ("development", "validation"):
        period_episodes = [episode for episode in episodes if episode["period"] == period]
        episode_regimes[period] = {
            "episode_count": len(period_episodes),
            "depth_state_counts": {
                state: sum(episode["stress_depth_state"] == state for episode in period_episodes)
                for state in ("MILD", "MODERATE", "DEEP", "EXTREME")
            },
            "speed_state_counts": {
                state: sum(episode["stress_speed_state"] == state for episode in period_episodes)
                for state in ("FAST_SHOCK", "MEDIUM_DECLINE", "SLOW_GRIND")
            },
            "fast_shock_episode_share_pct": (
                sum(episode["stress_speed_state"] == "FAST_SHOCK" for episode in period_episodes)
                / len(period_episodes) * 100.0 if period_episodes else None
            ),
            "mean_episode_minimum_20d_return_pct": _mean(
                episode["minimum_20d_return_pct"] for episode in period_episodes
            ),
        }
    factors = [
        {
            "rank": 1,
            "explanation": "MARKET_BETA_EFFECT",
            "strength": market_beta,
            "quantitative_evidence": {
                "development_stock_gross_mean_pct": dev.get("gross_mean_pct"),
                "development_matched_market_mean_pct": dev.get("matched_market_return_mean_pct"),
                "development_market_contribution_share_pct": explanation_ratio,
                "development_market_excess_mean_pct": excess_dev,
                "validation_market_excess_mean_pct": excess_val,
                "gross_mean_change_pct_points": stock_change,
                "matched_market_mean_change_pct_points": market_change,
            },
        },
        {
            "rank": 2,
            "explanation": "STRESS_COMPOSITION_SHIFT",
            "strength": stress_composition,
            "quantitative_evidence": {
                "market_20d_return_standardized_mean_difference": depth_shift,
                "largest_absolute_composition_feature_shift": strongest_composition_shift,
                "composition_feature_standardized_mean_differences": composition_shifts,
                "episode_depth_and_speed_distribution": episode_regimes,
                "largest_distribution_shifts": shift["largest_standardized_shifts"][:5],
            },
        },
        {
            "rank": 3,
            "explanation": "STRESS_DEPTH_SHIFT",
            "strength": "STRONG" if depth_shift is not None and abs(depth_shift) >= 0.5 else (
                "MODERATE" if depth_shift is not None and abs(depth_shift) >= 0.2 else "WEAK"
            ),
            "quantitative_evidence": {
                "signal_date_20d_return_standardized_mean_difference": depth_shift,
                "episode_distribution": episode_regimes,
            },
        },
        {
            "rank": 4,
            "explanation": "STRESS_SPEED_SHIFT",
            "strength": "STRONG" if (
                episode_regimes["development"]["fast_shock_episode_share_pct"] is not None
                and episode_regimes["validation"]["fast_shock_episode_share_pct"] is not None
                and abs(
                    episode_regimes["validation"]["fast_shock_episode_share_pct"]
                    - episode_regimes["development"]["fast_shock_episode_share_pct"]
                ) >= 25.0
            ) else "WEAK",
            "causal_support_for_phase11_decay": "WEAK; the validation episodes became faster, but FAST_SHOCK returns did not have the Development-positive pattern.",
            "quantitative_evidence": episode_regimes,
        },
        {
            "rank": 5,
            "explanation": "VOLATILITY_SHIFT",
            "strength": shift_strength("realized_volatility_20d_pct"),
            "quantitative_evidence": shift_features.get("realized_volatility_20d_pct"),
        },
        {
            "rank": 6,
            "explanation": "BREADTH_SHIFT",
            "strength": shift_strength("breadth_fraction_5d_negative"),
            "quantitative_evidence": shift_features.get("breadth_fraction_5d_negative"),
        },
        {
            "rank": 7,
            "explanation": "DISPERSION_SHIFT",
            "strength": shift_strength("dispersion_20d_pct"),
            "quantitative_evidence": shift_features.get("dispersion_20d_pct"),
        },
        {
            "rank": 8,
            "explanation": "EPISODE_CONCENTRATION",
            "strength": concentration_status,
            "quantitative_evidence": episode_root,
        },
        {
            "rank": 9,
            "explanation": "CURRENT_LISTING_SURVIVORSHIP_AND_DATA_LIMITATION",
            "strength": "MODERATE",
            "quantitative_evidence": {
                "cohort": "100 current listings only; 50 KOSPI and 50 KOSDAQ",
                "dense_multi_year_symbols": 90,
                "historical_point_in_time_universe": False,
                "delisted_coverage": False,
                "flow_data_available": False,
                "causal_direction": "May bias both period estimates; available evidence cannot attribute the Development-to-Validation change to this limitation.",
            },
        },
    ]
    return {
        "verdict": "Failure anatomy complete for touched Development and Validation; Phase 11 candidate remains rejected.",
        "development_minus_validation_gross_decay_pct_points": -stock_change if stock_change is not None else None,
        "market_component_change_pct_points": market_change,
        "market_beta_explanation": market_beta,
        "stress_composition_shift": stress_composition,
        "ranked_explanations": factors,
        "episode_regime_distributions": episode_regimes,
        "new_mechanism_verdict": hypothesis["market_stress_mechanism"],
        "phase11_candidate_status": "REJECTED",
        "confirmation_read": "NOT_RUN; no new mechanism passed the candidate gate." if not hypothesis["candidate"] else "PENDING_FROZEN_CANDIDATE",
    }


def _analysis_metadata(
    *,
    source_git_sha: str,
    code_sha256: str,
    dataset_sha256: str,
    cohort_sha256: str,
    config_sha256: str,
) -> dict[str, Any]:
    return {
        "source_git_sha": source_git_sha,
        "phase12_source_code_sha256": code_sha256,
        "dataset_sha256": dataset_sha256,
        "cohort_sha256": cohort_sha256,
        "config_sha256": config_sha256,
        "period_scope": {
            "development": [item.isoformat() for item in DEVELOPMENT],
            "touched_validation": [item.isoformat() for item in VALIDATION],
            "confirmation": "NOT_READ",
            "external_2026": "NOT_READ",
            "holdout_2026": "NOT_READ",
        },
        "price_convention": PHASE11_ADJUSTMENT,
        "timestamp_semantics": "completed KRX daily session, KIS date at 00:00 Asia/Seoul",
        "cost_assumptions_round_trip_pct": BASE_ROUND_TRIP_COST_PCT,
        "generated_at_kst": datetime.now(KST).isoformat(),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl_gzip(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as compressed:
        for row in rows:
            compressed.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
            compressed.write(b"\n")
    path.write_bytes(buffer.getvalue())


def _initial_previous_phase_snapshot(output_root: Path) -> dict[str, Any]:
    path = output_root / "phase12-previous-artifact-snapshot.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    base = output_root.parent
    entries = []
    for number in range(5, 12):
        root = base / f"phase{number}"
        if root.is_dir():
            for artifact in sorted(
                item for item in root.rglob("*") if item.is_file() and _is_prior_manifest_or_index(item)
            ):
                entries.append({
                    "path": artifact.as_posix(),
                    "sha256": _sha256_file(artifact),
                    "bytes": artifact.stat().st_size,
                })
    snapshot = {
        "schema_version": 1,
        "purpose": "Phase 12 immutable baseline of Phase 5-11 runtime manifests and artifact indexes",
        "files": len(entries),
        "total_bytes": sum(row["bytes"] for row in entries),
        "snapshot_sha256": hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "artifacts": entries,
    }
    _write_json(path, snapshot)
    return snapshot


def _is_prior_manifest_or_index(path: Path) -> bool:
    name = path.name.lower()
    return "manifest" in name or "index" in name


def _compare_previous_phase_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    expected = {item["path"]: item["sha256"] for item in snapshot.get("artifacts", [])}
    actual: dict[str, str] = {}
    phase_base = Path("runtime/research")
    for number in range(5, 12):
        root = phase_base / f"phase{number}"
        if root.is_dir():
            for path in sorted(
                item for item in root.rglob("*") if item.is_file() and _is_prior_manifest_or_index(item)
            ):
                actual[path.as_posix()] = _sha256_file(path)
    changed = sorted(path for path in expected.keys() & actual.keys() if expected[path] != actual[path])
    missing = sorted(expected.keys() - actual.keys())
    added = sorted(actual.keys() - expected.keys())
    canonical = json.dumps(snapshot.get("artifacts", []), sort_keys=True, separators=(",", ":")).encode()
    manifest_valid = hashlib.sha256(canonical).hexdigest() == snapshot.get("snapshot_sha256")
    return {
        "status": "PASS" if not changed and not missing and not added and manifest_valid else "FAIL",
        "baseline_snapshot_sha256": snapshot.get("snapshot_sha256"),
        "baseline_file_count": len(expected),
        "current_file_count": len(actual),
        "changed_files": changed,
        "missing_files": missing,
        "added_files": added,
        "snapshot_manifest_valid": manifest_valid,
    }


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _remote_main_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "ls-remote", "origin", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    fields = result.stdout.split()
    return fields[0] if len(fields) >= 2 and fields[1] == "refs/heads/main" else None


def _three_day_market_returns(states: list[dict[str, Any]], episodes: list[dict[str, Any]]) -> dict[str, Any]:
    del episodes
    rows = [row for row in states if row["stress_signal"]]
    summary: dict[str, Any] = {}
    for period in ("development", "validation"):
        selected = [row for row in rows if _period_name(date.fromisoformat(row["date"])) == period]
        summary[period] = {
            "stress_market_session_count": len(selected),
            "breadth_quality_pass_count": sum(row["breadth_primary_eligible"] for row in selected),
            "breadth_quality_excluded_count": sum(not row["breadth_primary_eligible"] for row in selected),
            "mean_20d_market_return_pct": _mean(row["return_20d_pct"] for row in selected),
            "mean_5d_market_return_pct": _mean(row["return_5d_pct"] for row in selected),
            "mean_realized_volatility_20d_pct": _mean(row["realized_volatility_20d_pct"] for row in selected),
            "mean_breadth_5d_negative_fraction_quality_pass_only": _mean(
                row["fraction_5d_return_negative"] for row in selected if row["breadth_primary_eligible"]
            ),
            "mean_dispersion_20d_pct_quality_pass_only": _mean(
                row["cross_sectional_dispersion_20d_pct"] for row in selected if row["breadth_primary_eligible"]
            ),
        }
    return summary


def _phase12_config() -> dict[str, Any]:
    return {
        "development": [item.isoformat() for item in DEVELOPMENT],
        "validation": [item.isoformat() for item in VALIDATION],
        "confirmation": [item.isoformat() for item in CONFIRMATION],
        "protected_external": ["2026-01-05", "2026-04-16"],
        "protected_holdout": ["2026-07-28", "2026-08-28"],
        "signal": {"feature": "matched index 20-session return", "rule": "<= -4%", "retuning": False},
        "episode_grouping": {
            "maximum_non_signal_market_sessions_between_signals": MAX_NON_SIGNAL_SESSIONS,
            "market_specific": True,
            "reset_at_touched_period_boundary": True,
        },
        "breadth_quality_minimum_coverage": MIN_BREADTH_COVERAGE,
        "breadth_broad_sell_off_fraction_5d_negative": BREADTH_BROAD_SELL_OFF_MIN,
        "stress_depth_intervals_pct": {
            "MILD": "(-8, -4]", "MODERATE": "(-12, -8]", "DEEP": "(-20, -12]", "EXTREME": "<= -20",
        },
        "stress_speed": {
            "FAST_SHOCK": "negative 5d return contributes >= 50% of absolute 20d decline",
            "SLOW_GRIND": "negative 5d return contributes <= 25% and >= 10 sessions since 20d high",
            "MEDIUM_DECLINE": "other qualifying stress dates",
        },
        "volatility_and_dispersion": "LOW/MID/HIGH terciles cut from all breadth-quality-passing Development daily observations, then held fixed for Validation.",
        "market_turnover_state": "LOW/MID/HIGH terciles of current market cohort turnover divided by the prior 20-session median, cut on breadth-quality-passing Development sessions.",
        "rolling_beta": {
            "window_sessions": BETA_LOOKBACK_SESSIONS,
            "minimum_paired_daily_returns": BETA_MIN_OBSERVATIONS,
            "input_window_ends_at_completed_signal_date": True,
            "states": {"LOW_BETA": "<=0.75", "MID_BETA": "(0.75,1.25]", "HIGH_BETA": ">1.25"},
        },
        "outcome": {
            "signal_after_completed_close": True,
            "entry": "next-session open",
            "exit": "third session close including entry session as session 1",
            "period_boundary": "omit any signal whose exit is after its touched period end",
            "non_overlap": "per symbol; next entry must be after prior exit",
        },
        "bootstrap": {"cluster": "overlapping cross-market stress episode cluster", "iterations": BOOTSTRAP_ITERATIONS, "seed": BOOTSTRAP_SEED},
        "cost_model": {"fee_each_side_pct": 0.015, "sell_tax_pct": 0.20, "slippage_each_side_pct": 0.15, "round_trip_pct": 0.53},
        "candidate_gate": MECHANISM_GATE,
        "mechanism_priority": [name for name, _, _ in MECHANISM_CANDIDATES],
        "no_confirmation_before_freeze": True,
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
    }


def _build_artifact_index(output_root: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for path in sorted(item for item in output_root.iterdir() if item.is_file()):
        if path.name in {"phase12-artifact-index.json", "artifact-integrity.json"}:
            continue
        entries.append({"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": _sha256_file(path)})
    return {
        "artifact": "phase12-artifact-index.json",
        "algorithm": "SHA-256",
        "artifact_metadata": metadata,
        "artifacts": entries,
    }


def _verify_phase12_index(output_root: Path, index: dict[str, Any]) -> dict[str, Any]:
    checked = 0
    failures = []
    for entry in index["artifacts"]:
        path = Path(entry["path"])
        if not path.is_file():
            failures.append({"path": entry["path"], "reason": "MISSING"})
            continue
        checked += 1
        actual = _sha256_file(path)
        if actual != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            failures.append({"path": entry["path"], "reason": "HASH_OR_SIZE_MISMATCH"})
    index_path = output_root / "phase12-artifact-index.json"
    return {
        "status": "PASS" if not failures else "FAIL",
        "artifact_index_sha256": _sha256_file(index_path) if index_path.is_file() else None,
        "checked_artifact_count": checked,
        "failures": failures,
        "integrity_file_self_hash_excluded": True,
    }


def run_phase12(
    *,
    cache_root: Path = PHASE11_ROOT / "daily_cache",
    cohort_path: Path = COHORT_PATH,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, Any]:
    """Run touched-period Phase 12 analysis and write all artifacts under Phase 12."""
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot = _initial_previous_phase_snapshot(output_root)
    inputs = _load_phase12_inputs(cache_root=cache_root, cohort_path=cohort_path)
    states, stock_states, sessions_by_market = build_market_state_rows(
        prices_by_symbol=inputs["prices"],
        indexes=inputs["indexes"],
        market_by_symbol=inputs["market_by_symbol"],
    )
    regime_thresholds = _assign_development_regimes(states)
    raw_events, nonoverlap_events = build_phase11_style_events(
        states=states,
        stock_states=stock_states,
        prices_by_symbol=inputs["prices"],
        indexes=inputs["indexes"],
        sessions_by_market=sessions_by_market,
        market_by_symbol=inputs["market_by_symbol"],
        horizon_sessions=3,
    )
    stock_state_thresholds = _assign_event_characteristic_states(raw_events)
    _assign_event_characteristic_states(nonoverlap_events, stock_state_thresholds)
    episodes = build_stress_episodes(
        states=states,
        sessions_by_market=sessions_by_market,
        nonoverlap_events=nonoverlap_events,
    )
    raw_key_to_nonoverlap = {
        (row["period"], row["market"], row["symbol"], row["signal_date"]): row
        for row in nonoverlap_events
    }
    episode_by_signal = {
        (episode["period"], episode["market"], signal_date): (
            episode["episode_id"], episode["macro_stress_cluster_id"],
        )
        for episode in episodes for signal_date in episode["signal_dates"]
    }
    for event in raw_events:
        key = (event["period"], event["market"], event["symbol"], event["signal_date"])
        event["non_overlapping"] = key in raw_key_to_nonoverlap
        event["episode_id"], event["macro_stress_cluster_id"] = episode_by_signal.get(
            (event["period"], event["market"], event["signal_date"]), (None, None),
        )
    nonoverlap_events.sort(key=lambda row: (row["period"], row["signal_date"], row["market"], row["symbol"]))
    # Episodes are outcome-aware only after deterministic date-only grouping.
    for episode in episodes:
        raw_episode_events = [
            event for event in raw_events
            if event["period"] == episode["period"] and event["market"] == episode["market"]
            and event["signal_date"] in episode["signal_dates"]
        ]
        episode["raw_stock_event_count"] = len(raw_episode_events)
    shift = build_development_validation_shift(states=states, episodes=episodes, events=nonoverlap_events)
    concentration = build_episode_concentration(episodes, nonoverlap_events)
    bootstrap = build_episode_cluster_bootstrap(nonoverlap_events)
    blocks = build_chronological_blocks(states=states, events=nonoverlap_events, episodes=episodes)
    successful_failed = build_episode_outcome_comparison(episodes)
    period_metrics = _period_metrics(nonoverlap_events)
    market_split: dict[str, Any] = {}
    for market in MARKETS:
        market_split[market] = _period_metrics([event for event in nonoverlap_events if event["market"] == market])
    beta_analysis = {
        "by_beta_state": _group_metrics(nonoverlap_events, "stock_beta_state"),
        "by_period": _period_metrics(nonoverlap_events),
        "beta_window_sessions": BETA_LOOKBACK_SESSIONS,
        "minimum_paired_returns": BETA_MIN_OBSERVATIONS,
        "beta_uses_completed_returns_through_signal_date_only": True,
        "interpretation": "Beta-adjusted residual is diagnostic only; it is not formal factor alpha.",
    }
    primary_events = [event for event in nonoverlap_events if event["signal_breadth_primary_eligible"]]
    hypothesis = evaluate_new_mechanism_gate(primary_events)
    root_cause = build_root_cause(
        period_metrics=period_metrics,
        shift=shift,
        concentration=concentration,
        episodes=episodes,
        hypothesis=hypothesis,
    )
    volatility = {
        "development_tercile_thresholds": regime_thresholds["volatility_20d_pct_tercile_cutoffs_development"],
        "development_definition": regime_thresholds["threshold_population"],
        "by_state": _group_metrics(primary_events, "signal_volatility_regime"),
        "daily_stress_observations": _three_day_market_returns(states, episodes),
    }
    dispersion = {
        "development_tercile_thresholds": regime_thresholds["dispersion_20d_pct_tercile_cutoffs_development"],
        "development_definition": regime_thresholds["threshold_population"],
        "by_state": _group_metrics(primary_events, "signal_dispersion_regime"),
        "low_dispersion_is_descriptive_only": True,
    }
    liquidity = {
        "development_tercile_thresholds": regime_thresholds[
            "market_turnover_ratio_20d_tercile_cutoffs_development"
        ],
        "feature_definition": "Current observed frozen-cohort market turnover divided by the median of the prior 20 market sessions; at least 15 prior observations required.",
        "by_state": _group_metrics(primary_events, "signal_market_turnover_regime"),
        "missing_turnover": "missing source values are excluded; no zero fill or market-data forward fill",
    }
    stock_characteristics = {
        "development_tercile_cutoffs": stock_state_thresholds,
        "by_recent_stock_return_20d": _group_metrics(primary_events, "recent_stock_return_20d_state"),
        "by_stock_turnover_ratio_20d": _group_metrics(primary_events, "stock_turnover_ratio_20d_state"),
        "by_stock_volatility_20d": _group_metrics(primary_events, "stock_volatility_20d_state"),
        "by_rolling_beta_state": _group_metrics(primary_events, "stock_beta_state"),
        "valuation_state": "NOT_AVAILABLE_POINT_IN_TIME_SAFE_DATA; cheap/expensive buckets were not constructed.",
        "descriptive_only": True,
    }
    breadth_by_period: dict[str, Any] = {}
    for period in ("development", "validation"):
        rows = [row for row in states if row["stress_signal"] and _period_name(date.fromisoformat(row["date"])) == period]
        breadth_by_period[period] = {
            "market_signal_date_count": len(rows),
            "primary_quality_pass_count": sum(row["breadth_primary_eligible"] for row in rows),
            "low_coverage_excluded_count": sum(not row["breadth_primary_eligible"] for row in rows),
            "mean_coverage_ratio": _mean(row["coverage_ratio"] for row in rows),
            "market_split": {
                market: {
                    "signal_date_count": sum(row["market"] == market for row in rows),
                    "quality_pass_count": sum(row["market"] == market and row["breadth_primary_eligible"] for row in rows),
                    "mean_coverage_ratio": _mean(row["coverage_ratio"] for row in rows if row["market"] == market),
                    "mean_fraction_negative_today": _mean(
                        row["fraction_negative_today"] for row in rows
                        if row["market"] == market and row["breadth_primary_eligible"]
                    ),
                    "mean_fraction_5d_negative": _mean(
                        row["fraction_5d_return_negative"] for row in rows
                        if row["market"] == market and row["breadth_primary_eligible"]
                    ),
                    "mean_fraction_5d_le_minus_5pct": _mean(
                        row["fraction_5d_return_le_minus_5pct"] for row in rows
                        if row["market"] == market and row["breadth_primary_eligible"]
                    ),
                    "mean_fraction_20d_le_minus_10pct": _mean(
                        row["fraction_20d_return_le_minus_10pct"] for row in rows
                        if row["market"] == market and row["breadth_primary_eligible"]
                    ),
                }
                for market in MARKETS
            },
            "by_breadth_state": _group_metrics(
                [event for event in primary_events if event["period"] == period], "signal_breadth_state",
            ),
        }
    breadth = {
        "minimum_coverage_ratio": MIN_BREADTH_COVERAGE,
        "coverage_denominator": "frozen cohort size for the market",
        "missing_symbol_observation": "excluded from each feature denominator; never forward-filled or zero-filled",
        "periods": breadth_by_period,
    }
    depth_and_speed = {
        "depth_intervals_pct": _phase12_config()["stress_depth_intervals_pct"],
        "speed_definitions": _phase12_config()["stress_speed"],
        "by_depth_state": _group_metrics(primary_events, "signal_depth_state"),
        "by_speed_state": _group_metrics(primary_events, "signal_speed_state"),
        "episode_distribution": root_cause["episode_regime_distributions"],
        "no_threshold_search": True,
    }
    market_stock = {
        "by_period": period_metrics,
        "by_market": market_split,
        "by_period_and_market": {
            period: {
                market: _mean_metrics([
                    event for event in nonoverlap_events
                    if event["period"] == period and event["market"] == market
                ])
                for market in MARKETS
            }
            for period in ("development", "validation")
        },
        "return_definition": "Each stock enters at next-session open and exits at the horizon-th session close; matched index uses that same entry-session open and exit-session close.",
        "excess_definition": "stock forward return minus matched index forward return, in percentage points.",
        "beta_adjustment_definition": "stock forward return minus rolling pre-signal beta times matched index forward return; diagnostic, not alpha.",
        "raw_phase11_style_event_counts": {
            period: sum(event["period"] == period for event in raw_events)
            for period in ("development", "validation")
        },
        "non_overlapping_event_counts": {
            period: sum(event["period"] == period for event in nonoverlap_events)
            for period in ("development", "validation")
        },
        "unique_market_signal_dates": {
            period: len({(event["market"], event["signal_date"]) for event in raw_events if event["period"] == period})
            for period in ("development", "validation")
        },
        "low_coverage_events_excluded_from_mechanism_inference": sum(
            not event["signal_breadth_primary_eligible"] for event in raw_events
        ),
    }
    candidate_registration = {
        "status": "NOT_CREATED" if hypothesis["candidate"] is None else "PENDING_PRE_CONFIRMATION_FREEZE",
        "candidate_status": hypothesis["candidate_status"],
        "candidate_rule": hypothesis["candidate"],
        "confirmation_period": ["2025-07-01", "2025-12-30"],
        "source_git_sha": _git_sha(),
        "pre_confirmation_freeze_sha": None,
        "anatomy_artifact_hashes": {},
        "confirmation_access_authorized": False,
        "reason": "No mechanism passed both touched-period gates." if hypothesis["candidate"] is None else "Must be frozen and pushed before a one-shot Confirmation read.",
    }
    confirmation = {
        "status": "NOT_RUN" if hypothesis["candidate"] is None else "PENDING_FREEZE",
        "period": ["2025-07-01", "2025-12-30"],
        "candidate_status": hypothesis["candidate_status"],
        "read": False,
        "reason": "No genuinely new mechanism passed the predeclared gate." if hypothesis["candidate"] is None else "Not opened by the touched-period analysis run; freeze required first.",
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
    }
    data_quality_source = json.loads((PHASE11_ROOT / "phase11-daily-dataset.json").read_text(encoding="utf-8"))
    cost_audit = build_cost_model_audit()
    cost_audit["artifact_metadata"] = {}
    config = _phase12_config()
    config_sha = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    code_sha = _sha256_file(Path(__file__))
    metadata = _analysis_metadata(
        source_git_sha=_git_sha(),
        code_sha256=code_sha,
        dataset_sha256=inputs["dataset_sha256"],
        cohort_sha256=inputs["cohort_sha256"],
        config_sha256=config_sha,
    )
    dataset_manifest = {
        "artifact": "phase12-dataset-manifest.json",
        "artifact_metadata": metadata,
        "schema_version": 1,
        "dataset_sha256": inputs["dataset_sha256"],
        "cohort_sha256": inputs["cohort_sha256"],
        "cohort_name": inputs["cohort_name"],
        "eligible_cohort_count": len(inputs["symbols"]),
        "cohort_market_counts": {market: sum(value == market for value in inputs["market_by_symbol"].values()) for market in MARKETS},
        "loaded_symbol_count": len(inputs["prices"]),
        "missing_price_symbols": inputs["missing_symbols"],
        "dense_multi_year_symbol_count_from_phase11_quality_manifest": data_quality_source.get("symbols_with_dense_multi_year_span"),
        "current_listing_survivorship_bias": True,
        "point_in_time_historical_universe": False,
        "delisted_listing_coverage": False,
        "flow_data": "NOT_AVAILABLE",
        "warmup_window": [WARMUP_START.isoformat(), (POOL_START - timedelta(days=1)).isoformat()],
        "touched_data_window": [POOL_START.isoformat(), POOL_END.isoformat()],
        "input_rows_after_date_guard": {
            "market_index_rows": {market: len(inputs["indexes"][market]) for market in MARKETS},
            "stock_rows": {symbol: len(bars) for symbol, bars in sorted(inputs["prices"].items())},
        },
        "source_files": inputs["input_hashes"],
        "input_period_guard": "No Phase 12 Bar object is materialized outside 2022-11-01..2025-06-30.",
        "feature_definitions": config,
        "timestamp_semantics": "completed daily KRX session; KIS timestamp date is 00:00 Asia/Seoul",
    }
    breadth["artifact_metadata"] = metadata
    volatility["artifact_metadata"] = metadata
    dispersion["artifact_metadata"] = metadata
    liquidity["artifact_metadata"] = metadata
    stock_characteristics["artifact_metadata"] = metadata
    depth_and_speed["artifact_metadata"] = metadata
    market_stock["artifact_metadata"] = metadata
    beta_analysis["artifact_metadata"] = metadata
    shift["artifact_metadata"] = metadata
    concentration["artifact_metadata"] = metadata
    bootstrap_artifact = {
        "artifact_metadata": metadata,
        "periods": bootstrap,
        "uncertainty_note": "Episode-cluster resampling only; no individual-stock-row bootstrap and no p-value testing.",
    }
    chronological = {"artifact_metadata": metadata, **blocks}
    episode_artifact = {
        "artifact_metadata": metadata,
        "episode_grouping": config["episode_grouping"],
        "depth_states": config["stress_depth_intervals_pct"],
        "episodes": episodes,
        "episode_count_by_period_and_market": {
            period: {market: sum(row["period"] == period and row["market"] == market for row in episodes) for market in MARKETS}
            for period in ("development", "validation")
        },
        "successful_vs_failed": successful_failed,
    }
    hypothesis["artifact_metadata"] = metadata
    candidate_registration["artifact_metadata"] = metadata
    confirmation["artifact_metadata"] = metadata
    cost_audit["artifact_metadata"] = metadata
    root_cause["artifact_metadata"] = metadata
    source_summary = {
        "phase11_original_official_metrics": {
            "development_gross_pct": 1.046,
            "validation_gross_pct": 0.479,
            "validation_kospi_gross_pct": 0.997,
            "validation_kosdaq_gross_pct": 0.194,
            "provenance": "Historical Phase 11 RESULTS.md and supplied current research status; preserved without recalculation or mutation.",
        },
        "phase12_recomputed_metrics": period_metrics,
    }
    previous_immutability = _compare_previous_phase_snapshot(snapshot)
    summary = {
        "artifact_metadata": metadata,
        "status_matrix": {
            "PHASE11_CANDIDATE": "REJECTED",
            "PHASE12_FAILURE_ANATOMY": "COMPLETE",
            "MARKET_BETA_EXPLANATION": root_cause["market_beta_explanation"],
            "STRESS_COMPOSITION_SHIFT": root_cause["stress_composition_shift"],
            "MARKET_STRESS_MECHANISM": hypothesis["market_stress_mechanism"],
            "PHASE12_CANDIDATE": hypothesis["candidate_status"],
            "CONFIRMATION": confirmation["status"],
            "EXTERNAL_2026": "NOT_READ",
            "HOLDOUT_2026": "NOT_READ",
            "SHADOW_NEXT_SESSION": "NO",
            "ALPHA": "UNPROVEN",
            "LIVE": "DISABLED",
        },
        "phase11_official_baseline_and_phase12_recomputation": source_summary,
        "development_vs_validation_decay": {
            "official_development_gross_pct": 1.046,
            "official_validation_gross_pct": 0.479,
            "official_gross_decay_pct_points": 0.567,
            "phase12_decomposition": period_metrics,
        },
        "market_episode_counts": episode_artifact["episode_count_by_period_and_market"],
        "market_adjusted_stock_result": period_metrics,
        "root_cause_ranking": root_cause["ranked_explanations"],
        "mechanism_decision": hypothesis["market_stress_mechanism"],
        "candidate_created": hypothesis["candidate"] is not None,
        "candidate_rule": hypothesis["candidate"],
        "confirmation": confirmation,
        "external_2026": "NOT_READ",
        "protected_holdout": "NOT_READ",
        "current_listing_survivorship_bias": True,
        "prior_phase_artifacts_immutable": previous_immutability["status"],
    }
    _write_jsonl_gzip(output_root / "phase12-market-state.jsonl.gz", states)
    _write_jsonl_gzip(output_root / "phase12-candidate-events.jsonl.gz", raw_events)
    _write_json(output_root / "phase12-dataset-manifest.json", dataset_manifest)
    _write_json(output_root / "phase12-stress-episodes.json", episode_artifact)
    _write_json(output_root / "phase12-chronological-blocks.json", chronological)
    _write_json(output_root / "phase12-breadth-analysis.json", breadth)
    _write_json(output_root / "phase12-volatility-analysis.json", volatility)
    _write_json(output_root / "phase12-dispersion-analysis.json", dispersion)
    _write_json(output_root / "phase12-liquidity-analysis.json", liquidity)
    _write_json(output_root / "phase12-stock-characteristics-analysis.json", stock_characteristics)
    _write_json(output_root / "phase12-stress-depth-speed-analysis.json", depth_and_speed)
    _write_json(output_root / "phase12-market-stock-decomposition.json", market_stock)
    _write_json(output_root / "phase12-beta-analysis.json", beta_analysis)
    _write_json(output_root / "phase12-development-validation-shift.json", shift)
    _write_json(output_root / "phase12-episode-concentration.json", concentration)
    _write_json(output_root / "phase12-bootstrap.json", bootstrap_artifact)
    _write_json(output_root / "phase12-cost-model-audit.json", cost_audit)
    _write_json(output_root / "phase12-root-cause.json", root_cause)
    _write_json(output_root / "phase12-hypothesis.json", hypothesis)
    _write_json(output_root / "phase12-preregistration.json", candidate_registration)
    _write_json(output_root / "phase12-confirmation.json", confirmation)
    _write_json(output_root / "phase12-summary.json", summary)
    index = _build_artifact_index(output_root, metadata)
    _write_json(output_root / "phase12-artifact-index.json", index)
    integrity = _verify_phase12_index(output_root, index)
    integrity["previous_phase_artifact_immutability"] = _compare_previous_phase_snapshot(snapshot)
    integrity["artifact_metadata"] = metadata
    _write_json(output_root / "artifact-integrity.json", integrity)
    return summary


if __name__ == "__main__":
    result = run_phase12()
    print(json.dumps(result["status_matrix"], ensure_ascii=False, indent=2, sort_keys=True))

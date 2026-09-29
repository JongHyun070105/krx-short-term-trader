from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import subprocess
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.quality import require_healthy_bars
from krx_trader.models import Bar
from krx_trader.research.phase9_integrity import (
    artifact_index,
    market_by_cohort_order,
    verify_artifact_index,
)

KST = ZoneInfo("Asia/Seoul")
DEVELOPMENT_START = date(2026, 4, 17)
DEVELOPMENT_END = date(2026, 6, 30)
PHASE9_ROOT = Path("runtime/research/phase9")
OUTPUT_ROOT = Path("runtime/research/phase10")
DAILY_CACHE_ROOT = PHASE9_ROOT / "reconciliation" / "safe-daily-cache"
DAILY_INTERVAL = "1d-adjusted"
BASE_ROUND_TRIP_COST_PCT = 0.53
COST_MULTIPLIERS = (1.0, 1.5, 2.0)
HORIZONS = (1, 2, 3, 5)
PRIMARY_HORIZONS = (2, 3, 5)
MINIMUM_PROMOTION_SAMPLE = 30
PROMOTION_MIN_GROSS_PCT = 1.0
MAX_CONCENTRATION_SHARE = 0.20

RETURN_3D_BINS = (
    (-math.inf, -8.0, "<= -8%"), (-8.0, -4.0, "-8% to -4%"),
    (-4.0, -2.0, "-4% to -2%"), (-2.0, 0.0, "-2% to 0%"),
    (0.0, 2.0, "0 to +2%"), (2.0, 4.0, "+2% to +4%"),
    (4.0, 8.0, "+4% to +8%"), (8.0, math.inf, ">= +8%"),
)
RETURN_5D_BINS = (
    (-math.inf, -10.0, "<= -10%"), (-10.0, -5.0, "-10% to -5%"),
    (-5.0, -2.0, "-5% to -2%"), (-2.0, 0.0, "-2% to 0%"),
    (0.0, 2.0, "0 to +2%"), (2.0, 5.0, "+2% to +5%"),
    (5.0, 10.0, "+5% to +10%"), (10.0, math.inf, ">= +10%"),
)


def assert_development_date(session: date) -> None:
    if not DEVELOPMENT_START <= session <= DEVELOPMENT_END:
        raise ValueError("Phase 10 outcomes and observations are limited to Development dates")


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNAVAILABLE"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bucket(value: float | None, bins: tuple[tuple[float, float, str], ...]) -> str | None:
    if value is None or not math.isfinite(value):
        return None
    for index, (lower, upper, label) in enumerate(bins):
        if (index == 0 and value <= upper) or (index == len(bins) - 1 and value >= lower):
            return label
        if lower <= value < upper:
            return label
    return bins[-1][2]


def return_3d_bucket(value: float | None) -> str | None:
    return _bucket(value, RETURN_3D_BINS)


def return_5d_bucket(value: float | None) -> str | None:
    return _bucket(value, RETURN_5D_BINS)


def _percentile_rank(value: float, peers: list[float]) -> float:
    ordered = sorted(peers)
    if len(ordered) == 1:
        return 50.0
    first = next(index for index, item in enumerate(ordered) if item == value)
    last = len(ordered) - 1 - next(index for index, item in enumerate(reversed(ordered)) if item == value)
    return ((first + last) / 2) / (len(ordered) - 1) * 100.0


def _sample_std(values: list[float]) -> float | None:
    return statistics.stdev(values) * 100.0 if len(values) >= 2 else None


def _window_range(bars: list[Bar]) -> tuple[float | None, float | None, float | None]:
    if not bars:
        return None, None, None
    high = max(bar.high for bar in bars)
    low = min(bar.low for bar in bars)
    close = bars[-1].close
    width = (high - low) / close * 100.0 if close > 0 else None
    location = (close - low) / (high - low) if high > low else 0.5
    return width, location, high


def _trailing_return(by_date: dict[date, Bar], sessions: list[date], index: int, days: int) -> float | None:
    if index < days:
        return None
    prior = by_date.get(sessions[index - days])
    current = by_date.get(sessions[index])
    if prior is None or current is None or prior.close <= 0:
        return None
    return (current.close / prior.close - 1.0) * 100.0


def _features_for_symbol(
    symbol: str,
    market: str,
    bars: list[Bar],
    sessions: list[date],
) -> list[dict[str, Any]]:
    by_date = {bar.time.astimezone(KST).date(): bar for bar in bars}
    if len(by_date) != len(bars):
        raise ValueError(f"duplicate daily date for {symbol}")
    output: list[dict[str, Any]] = []
    for index, session in enumerate(sessions):
        if session < DEVELOPMENT_START:
            continue
        assert_development_date(session)
        current = by_date.get(session)
        if current is None:
            continue
        row: dict[str, Any] = {
            "symbol": symbol,
            "date": session.isoformat(),
            "month": session.strftime("%B"),
            "market": market,
            "price_adjusted_close": current.close,
            "volume": current.volume,
            "liquidity_proxy_adjusted_close_x_volume_krw": current.close * current.volume,
            "price_bucket": (
                "LT_10K" if current.close < 10_000 else
                "10K_30K" if current.close < 30_000 else
                "30K_50K" if current.close < 50_000 else "GE_50K"
            ),
            "liquidity_bucket_adjusted_close_x_volume": (
                "LOW_LT_50M" if current.close * current.volume < 50_000_000 else
                "MID_50M_250M" if current.close * current.volume < 250_000_000 else
                "HIGH_250M_1B" if current.close * current.volume < 1_000_000_000
                else "VERY_HIGH_GE_1B"
            ),
            "volume_ratio_vs_prior_5d_median": None,
            "volume_ratio_status": "UNAVAILABLE_INSUFFICIENT_PRIOR_DAYS",
        }
        for days in (1, 2, 3, 5):
            row[f"return_{days}d_pct"] = _trailing_return(by_date, sessions, index, days)
        for days in (3, 5):
            window_dates = sessions[index - days + 1:index + 1] if index >= days - 1 else []
            window = [by_date[item] for item in window_dates if item in by_date]
            returns: list[float] = []
            if index >= days:
                for current_index in range(index - days + 1, index + 1):
                    prior_bar = by_date.get(sessions[current_index - 1])
                    current_bar = by_date.get(sessions[current_index])
                    if prior_bar is None or current_bar is None or prior_bar.close <= 0:
                        returns = []
                        break
                    returns.append(current_bar.close / prior_bar.close - 1.0)
            width, location, _ = _window_range(window)
            row[f"range_{days}d_pct"] = width
            row[f"realized_volatility_{days}d_pct"] = _sample_std(returns)
            row[f"close_location_{days}d"] = location
        five_day = [by_date.get(item) for item in sessions[index - 4:index + 1]] if index >= 4 else []
        five_day_bars = [bar for bar in five_day if bar is not None]
        high_5d = max((bar.high for bar in five_day_bars), default=None)
        low_5d = min((bar.low for bar in five_day_bars), default=None)
        row["distance_from_5d_high_pct"] = (
            (current.close / high_5d - 1.0) * 100.0 if high_5d else None
        )
        row["distance_from_5d_low_pct"] = (
            (current.close / low_5d - 1.0) * 100.0 if low_5d else None
        )
        if index >= 5:
            prior_volumes = [by_date[sessions[pos]].volume for pos in range(index - 5, index)]
            median_volume = statistics.median(prior_volumes)
            if median_volume > 0:
                row["volume_ratio_vs_prior_5d_median"] = current.volume / median_volume
                row["volume_ratio_status"] = "DESCRIPTIVE_RAW_DAILY_VOLUME_ONLY"
        row["return_3d_bucket"] = return_3d_bucket(row["return_3d_pct"])
        row["return_5d_bucket"] = return_5d_bucket(row["return_5d_pct"])
        row["volatility_state"] = None
        row["range_position_state"] = (
            "NEAR_LOW" if row["close_location_5d"] is not None and row["close_location_5d"] <= 1 / 3
            else "MIDDLE" if row["close_location_5d"] is not None
            and row["close_location_5d"] <= 2 / 3
            else "NEAR_HIGH" if row["close_location_5d"] is not None else None
        )
        row["cross_sectional_return_3d_percentile"] = None
        row["cross_sectional_volatility_5d_percentile"] = None
        output.append(row)
    return output


def _assign_cross_sectional_states(rows: list[dict[str, Any]]) -> None:
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_session[row["date"]].append(row)
    for group in by_session.values():
        returns = [row["return_3d_pct"] for row in group if row["return_3d_pct"] is not None]
        volatilities = [
            row["realized_volatility_5d_pct"] for row in group
            if row["realized_volatility_5d_pct"] is not None
        ]
        for row in group:
            value = row["return_3d_pct"]
            if value is not None and returns:
                row["cross_sectional_return_3d_percentile"] = _percentile_rank(value, returns)
            volatility = row["realized_volatility_5d_pct"]
            if volatility is not None and volatilities:
                percentile = _percentile_rank(volatility, volatilities)
                row["cross_sectional_volatility_5d_percentile"] = percentile
                row["volatility_state"] = (
                    "LOW" if percentile <= 100 / 3 else "MID" if percentile <= 200 / 3 else "HIGH"
                )


def _outcomes_for_symbol(
    bars: list[Bar], sessions: list[date], symbol: str
) -> dict[tuple[str, int], dict[str, Any]]:
    by_date = {bar.time.astimezone(KST).date(): bar for bar in bars}
    outcomes: dict[tuple[str, int], dict[str, Any]] = {}
    for index, session in enumerate(sessions):
        assert_development_date(session)
        for horizon in HORIZONS:
            entry_index = index + 1
            exit_index = index + horizon
            if exit_index >= len(sessions):
                continue
            window_dates = sessions[entry_index:exit_index + 1]
            window = [by_date.get(day) for day in window_dates]
            if len(window) != horizon or any(bar is None for bar in window):
                continue
            entry = window[0]
            exit_bar = window[-1]
            if entry.open <= 0:
                continue
            gross = (exit_bar.close / entry.open - 1.0) * 100.0
            mfe = (max(bar.high for bar in window) / entry.open - 1.0) * 100.0
            mae = (min(bar.low for bar in window) / entry.open - 1.0) * 100.0
            outcomes[(session.isoformat(), horizon)] = {
                "symbol": symbol,
                "signal_date": session.isoformat(),
                "entry_date": window_dates[0].isoformat(),
                "exit_date": window_dates[-1].isoformat(),
                "horizon_days": horizon,
                "entry_open": entry.open,
                "exit_close": exit_bar.close,
                "gross_return_pct": gross,
                "mfe_pct": mfe,
                "mae_pct": mae,
            }
    return outcomes


def build_daily_observations(
    *,
    bars_by_symbol: dict[str, list[Bar]],
    symbols: list[str],
    market_by_symbol: dict[str, str],
    sessions: list[date],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered_sessions = sorted(sessions)
    if not ordered_sessions or ordered_sessions[0] < DEVELOPMENT_START \
            or ordered_sessions[-1] > DEVELOPMENT_END:
        raise ValueError("Phase 10 requires the exact safe Development session interval")
    feature_rows: list[dict[str, Any]] = []
    outcome_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        bars = sorted(bars_by_symbol.get(symbol, []), key=lambda bar: bar.time)
        require_healthy_bars(bars)
        earlier_dates = sorted({
            bar.time.astimezone(KST).date() for bar in bars
            if bar.time.astimezone(KST).date() < ordered_sessions[0]
        })
        if earlier_dates and earlier_dates != [date.fromordinal(ordered_sessions[0].toordinal() - 1)]:
            raise ValueError("Phase 10 permits only the immediately preceding session as feature pre-roll")
        feature_sessions = sorted(set(earlier_dates).union(ordered_sessions))
        feature_rows.extend(
            _features_for_symbol(symbol, market_by_symbol[symbol], bars, feature_sessions)
        )
        outcome_map = _outcomes_for_symbol(bars, ordered_sessions, symbol)
        outcome_rows.extend(outcome_map.values())
    _assign_cross_sectional_states(feature_rows)
    feature_lookup = {(row["symbol"], row["date"]): row for row in feature_rows}
    for outcome in outcome_rows:
        feature = feature_lookup.get((outcome["symbol"], outcome["signal_date"]))
        if feature is None:
            outcome["return_3d_bucket"] = None
            outcome["return_5d_bucket"] = None
            outcome["volatility_state"] = None
            outcome["range_position_state"] = None
            outcome["market"] = market_by_symbol[outcome["symbol"]]
            outcome["month"] = date.fromisoformat(outcome["signal_date"]).strftime("%B")
        else:
            for key in ("return_3d_bucket", "return_5d_bucket", "volatility_state", "range_position_state"):
                outcome[key] = feature[key]
            outcome["market"] = feature["market"]
            outcome["month"] = feature["month"]
    return feature_rows, outcome_rows


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _payoff_ratio(values: list[float]) -> float | None:
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses == 0:
        return None
    return wins / losses


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["gross_return_pct"]) for row in rows]
    gross_mean = statistics.fmean(values) if values else None
    monthly: dict[str, dict[str, Any]] = {}
    for month in ("April", "May", "June"):
        subset = [row for row in rows if row["month"] == month]
        month_values = [float(row["gross_return_pct"]) for row in subset]
        monthly[month] = {
            "n": len(subset),
            "mean_gross_pct": statistics.fmean(month_values) if month_values else None,
            "median_gross_pct": statistics.median(month_values) if month_values else None,
            "win_rate_pct": sum(value > 0 for value in month_values) / len(month_values) * 100
            if month_values else None,
        }
    market = {}
    for name in ("KOSPI", "KOSDAQ"):
        subset = [row for row in rows if row["market"] == name]
        vals = [float(row["gross_return_pct"]) for row in subset]
        market[name] = {
            "n": len(subset),
            "mean_gross_pct": statistics.fmean(vals) if vals else None,
            "median_gross_pct": statistics.median(vals) if vals else None,
            "win_rate_pct": sum(value > 0 for value in vals) / len(vals) * 100 if vals else None,
        }
    by_symbol: dict[str, int] = defaultdict(int)
    by_day: dict[str, int] = defaultdict(int)
    for row in rows:
        by_symbol[row["symbol"]] += 1
        by_day[row["signal_date"]] += 1
    count = len(values)
    return {
        "n": count,
        "gross_mean_pct": gross_mean,
        "gross_median_pct": statistics.median(values) if values else None,
        "win_rate_pct": sum(value > 0 for value in values) / count * 100 if count else None,
        "p10_gross_pct": _percentile(values, 10),
        "p90_gross_pct": _percentile(values, 90),
        "payoff_ratio": _payoff_ratio(values),
        "net_mean_pct_by_cost_multiplier": {
            str(multiplier): gross_mean - BASE_ROUND_TRIP_COST_PCT * multiplier
            if gross_mean is not None else None
            for multiplier in COST_MULTIPLIERS
        },
        "monthly": monthly,
        "market": market,
        "max_symbol_share": max(by_symbol.values(), default=0) / count if count else None,
        "max_signal_day_share": max(by_day.values(), default=0) / count if count else None,
    }


def _non_overlapping(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Simulate one position per symbol; a new signal enters after a prior exit."""
    selected: list[dict[str, Any]] = []
    last_exit: dict[str, date] = {}
    for row in sorted(rows, key=lambda item: (item["symbol"], item["signal_date"])):
        entry = date.fromisoformat(row["entry_date"])
        exit_day = date.fromisoformat(row["exit_date"])
        if entry <= last_exit.get(row["symbol"], date.min):
            continue
        selected.append(row)
        last_exit[row["symbol"]] = exit_day
    return selected


def _map_by_return_state(
    outcomes: list[dict[str, Any]],
    *,
    feature_key: str,
    bins: tuple[tuple[float, float, str], ...],
) -> list[dict[str, Any]]:
    output = []
    bucket_key = f"{feature_key}_bucket"
    for bucket in [item[2] for item in bins]:
        for horizon in PRIMARY_HORIZONS:
            group = [row for row in outcomes if row[bucket_key] == bucket
                     and row["horizon_days"] == horizon]
            item = {bucket_key: bucket, "horizon_days": horizon, **_aggregate(group)}
            item["non_overlapping_execution"] = _aggregate(_non_overlapping(group))
            item["non_overlapping_trade_count"] = item["non_overlapping_execution"]["n"]
            output.append(item)
    return output


def _map_dimension(outcomes: list[dict[str, Any]], feature_key: str) -> list[dict[str, Any]]:
    values = sorted({row[feature_key] for row in outcomes if row.get(feature_key) is not None})
    result = []
    for value in values:
        for horizon in PRIMARY_HORIZONS:
            group = [row for row in outcomes if row.get(feature_key) == value
                     and row["horizon_days"] == horizon]
            result.append({feature_key: value, "horizon_days": horizon, **_aggregate(group)})
    return result


def _promotion_candidates(
    return_maps: list[tuple[str, list[dict[str, Any]]]]
) -> list[dict[str, Any]]:
    candidates = []
    for feature_key, return_map in return_maps:
        for row in return_map:
            stats = row["non_overlapping_execution"]
            monthly_positive = sum(
                month["n"] >= 5 and (month["mean_gross_pct"] or 0) > 0
                for month in stats["monthly"].values()
            )
            market_means = [
                info["mean_gross_pct"] for info in stats["market"].values()
                if info["n"] >= 10 and info["mean_gross_pct"] is not None
            ]
            pass_gate = (
                stats["n"] >= MINIMUM_PROMOTION_SAMPLE
                and (stats["gross_mean_pct"] or 0) >= PROMOTION_MIN_GROSS_PCT
                and (stats["net_mean_pct_by_cost_multiplier"]["1.0"] or 0) > 0
                and (stats["net_mean_pct_by_cost_multiplier"]["2.0"] or 0) > 0
                and (stats["payoff_ratio"] or 0) > 1
                and monthly_positive >= 2
                and (stats["max_symbol_share"] or 0) <= MAX_CONCENTRATION_SHARE
                and (stats["max_signal_day_share"] or 0) <= MAX_CONCENTRATION_SHARE
                and all(value > 0 for value in market_means)
            )
            if pass_gate:
                bucket_key = f"{feature_key}_bucket"
                bucket = row[bucket_key]
                candidates.append({
                    "family": "DAILY_REVERSAL" if bucket.startswith(("<=", "-"))
                    else "MULTIDAY_CONTINUATION",
                    "return_feature": feature_key,
                    bucket_key: bucket,
                    "horizon_days": row["horizon_days"],
                    "development_statistics": stats,
                })
    return sorted(
        candidates,
        key=lambda row: (
            row["development_statistics"]["net_mean_pct_by_cost_multiplier"]["2.0"],
            row["development_statistics"]["gross_mean_pct"],
        ),
        reverse=True,
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def run_phase10_opportunity_map(
    *,
    cohort_manifest_path: Path = Path("runtime/research/phase4/cohort-manifest.json"),
    split_manifest_path: Path = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json"),
    phase9_root: Path = PHASE9_ROOT,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, Any]:
    if output_root.resolve() != OUTPUT_ROOT.resolve():
        raise ValueError("Phase 10 artifacts must remain under runtime/research/phase10")
    phase9_summary = json.loads((phase9_root / "phase9-summary.json").read_text(encoding="utf-8"))
    phase9_index = json.loads((phase9_root / "phase9-artifact-index.json").read_text(encoding="utf-8"))
    phase9_integrity = verify_artifact_index(phase9_root, phase9_index)
    if not phase9_summary.get("phase10_allowed_to_start") \
            or phase9_summary.get("research_platform") not in {"READY", "READY_WITH_LIMITATIONS"}:
        raise ValueError("Phase 10 is locked because Phase 9 research platform is not ready")
    if phase9_integrity["status"] != "PASS":
        raise ValueError("Phase 10 is locked because Phase 9 artifact integrity failed")
    if phase9_summary.get("daily_alignment_convention", "").find("FID_ORG_ADJ_PRC=0") < 0:
        raise ValueError("Phase 10 requires the Phase 9 adjusted daily convention")

    cohort = json.loads(cohort_manifest_path.read_text(encoding="utf-8"))["cohort_60"]
    symbols = list(cohort["symbols"])
    market_by_symbol = market_by_cohort_order(symbols)
    split = json.loads(split_manifest_path.read_text(encoding="utf-8"))
    sessions = sorted(
        date.fromisoformat(value) for value in split["splits"]["development"]["sessions"]
    )
    if not sessions or sessions[0] != DEVELOPMENT_START or sessions[-1] != DEVELOPMENT_END:
        raise ValueError("Phase 10 requires the exact Development session manifest")
    for session in sessions:
        assert_development_date(session)

    cache = ParquetBarCache(DAILY_CACHE_ROOT)
    bars_by_symbol: dict[str, list[Bar]] = {}
    input_hashes: dict[str, str] = {}
    for symbol in symbols:
        path = cache.partition_path("daily", symbol, DAILY_INTERVAL)
        bars = cache.load("daily", symbol, DAILY_INTERVAL)
        dates = [bar.time.astimezone(KST).date() for bar in bars]
        allowed_dates = {date.fromordinal(sessions[0].toordinal() - 1), *sessions}
        if any(day not in allowed_dates for day in dates):
            raise ValueError("Phase 10 daily input contains a date outside Development and one-day pre-roll")
        if not set(sessions).issubset(set(dates)):
            raise ValueError(f"Phase 10 adjusted daily source is incomplete for {symbol}")
        bars_by_symbol[symbol] = bars
        input_hashes[symbol] = _sha256(path)

    features, outcomes = build_daily_observations(
        bars_by_symbol=bars_by_symbol, symbols=symbols,
        market_by_symbol=market_by_symbol, sessions=sessions,
    )
    return_3d_map = _map_by_return_state(
        outcomes, feature_key="return_3d", bins=RETURN_3D_BINS
    )
    return_5d_map = _map_by_return_state(
        outcomes, feature_key="return_5d", bins=RETURN_5D_BINS
    )
    volatility_map = _map_dimension(outcomes, "volatility_state")
    range_map = _map_dimension(outcomes, "range_position_state")
    price_map = _map_dimension(outcomes, "price_bucket")
    liquidity_map = _map_dimension(outcomes, "liquidity_bucket_adjusted_close_x_volume")
    return_maps = [("return_3d", return_3d_map), ("return_5d", return_5d_map)]
    candidates = _promotion_candidates(return_maps)
    all_return_states = return_3d_map + return_5d_map
    best_observed = max(
        (row for row in all_return_states
         if row["non_overlapping_trade_count"] >= MINIMUM_PROMOTION_SAMPLE),
        key=lambda row: (row["non_overlapping_execution"]["gross_mean_pct"] or -math.inf),
        default=None,
    )
    if candidates:
        map_status = "PASS"
        daily_a = "RESEARCH_CANDIDATE"
        selected_candidate = candidates[0]
        external_status = "PENDING_SECONDARY"
    else:
        meaningful_gross = any(
            row["n"] >= MINIMUM_PROMOTION_SAMPLE
            and (row["gross_mean_pct"] or 0) >= BASE_ROUND_TRIP_COST_PCT
            for row in all_return_states
        )
        map_status = "WEAK" if meaningful_gross else "FAIL"
        daily_a = "REJECTED" if meaningful_gross else "NOT_CREATED"
        selected_candidate = None
        external_status = "NOT_RUN"

    summary = {
        "artifact": "phase10-daily-opportunity-map",
        "source_git_sha": _git_sha(),
        "phase9_source_git_sha": phase9_summary["source_git_sha"],
        "phase9_research_platform": phase9_summary["research_platform"],
        "phase9_artifact_integrity": phase9_integrity["status"],
        "source_convention": "KIS inquire-daily-itemchartprice, FID_ORG_ADJ_PRC=0 adjusted only",
        "scope": {"start": sessions[0].isoformat(), "end": sessions[-1].isoformat(),
                  "sessions": len(sessions), "symbols": len(symbols)},
        "dataset_sha256": hashlib.sha256(json.dumps(input_hashes, sort_keys=True).encode()).hexdigest(),
        "phase9_artifact_index_sha256": _sha256(phase9_root / "phase9-artifact-index.json"),
        "development_only": True,
        "secondary_read": False,
        "external_block_outcomes_read": False,
        "protected_holdout_payload_or_metadata_reads": 0,
        "observation_count": len(features),
        "outcome_count": len(outcomes),
        "primary_map": "trailing 3-day adjusted close return bucket × 2/3/5-session holding horizon",
        "secondary_maps": [
            "trailing 5-day return", "same-day cross-sectional 5-day realized volatility tercile",
            "5-day range position", "price bucket", "adjusted-close-times-volume liquidity proxy bucket",
        ],
        "entry_exit": "Signal after completed D; enter D+1 open; exit D+h close; no D-close fills",
        "round_trip_cost_pct": BASE_ROUND_TRIP_COST_PCT,
        "cost_multipliers": list(COST_MULTIPLIERS),
        "promotion_gate": {
            "minimum_non_overlapping_trades": MINIMUM_PROMOTION_SAMPLE,
            "minimum_gross_mean_pct": PROMOTION_MIN_GROSS_PCT,
            "net_positive_at_cost_multipliers": [1.0, 2.0],
            "payoff_ratio_gt": 1.0,
            "monthly_positive_months_minimum": 2,
            "max_symbol_or_day_share": MAX_CONCENTRATION_SHARE,
            "market_means_must_not_be_opposite_when_n_at_least": 10,
        },
        "best_broad_state_by_non_overlapping_gross": best_observed,
        "promotion_candidates": candidates,
        "selected_candidate": selected_candidate,
        "phase10_opportunity_map": map_status,
        "daily_a": daily_a,
        "secondary_result": "NOT_RUN" if not candidates else "PENDING_ONE_SHOT",
        "external_validation": external_status,
        "alpha": "UNPROVEN",
        "live": "DISABLED",
        "limitations": [
            "Minute-price adjustment remains UNKNOWN; Phase 10 uses adjusted daily bars only.",
            "Liquidity is an adjusted-close times raw-volume descriptive proxy.",
            "No index intraday source was available for an intraday market cross-check.",
            "This is descriptive Development anatomy; it is not a validated or live strategy.",
        ],
    }

    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_csv(output_root / "daily-features.csv", features)
    _write_csv(output_root / "daily-outcomes.csv", outcomes)
    _write_json(output_root / "phase10-opportunity-map.json", {
        "return_3d_by_horizon": return_3d_map,
        "return_5d_by_horizon": return_5d_map,
        "volatility_state_by_horizon": volatility_map,
        "range_position_by_horizon": range_map,
        "price_bucket_by_horizon": price_map,
        "liquidity_proxy_bucket_by_horizon": liquidity_map,
    })
    _write_json(output_root / "phase10-summary.json", summary)
    report = [
        "# Phase 10 Daily Opportunity Map",
        "",
        f"- Research platform: **{phase9_summary['research_platform']}**",
        f"- Scope: Development only, {sessions[0]} to {sessions[-1]}, {len(symbols)} symbols",
        f"- Source: {summary['source_convention']}",
        f"- Observation/outcome rows: {len(features)} / {len(outcomes)}",
        f"- Map status: **{map_status}**; DAILY-A: **{daily_a}**",
        f"- External outcomes: **{external_status}**",
        "- Entry: next-session open after completed signal day; exit at fixed horizon close.",
        "- Holdout payload and metadata reads: **0**; Secondary reads: **0**.",
        "",
        "The full return, volatility, range, market, monthly, cost, and concentration tables are in phase10-opportunity-map.json.",
        "No parameter search or future-feature construction was used.",
    ]
    (output_root / "phase10-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    indexed = [
        "daily-features.csv", "daily-outcomes.csv", "phase10-opportunity-map.json",
        "phase10-summary.json", "phase10-report.md",
    ]
    index = artifact_index(output_root, indexed)
    _write_json(output_root / "phase10-artifact-index.json", index)
    integrity = verify_artifact_index(output_root, index)
    _write_json(output_root / "artifact-integrity.json", integrity)
    return summary


if __name__ == "__main__":
    result = run_phase10_opportunity_map()
    print(json.dumps(result, indent=2, sort_keys=True))

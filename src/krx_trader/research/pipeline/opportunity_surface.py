from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

from krx_trader.research.pipeline.config import EXPECTED_COST_PCT, HORIZONS, TARGETS
from krx_trader.research.pipeline.panel import PanelRecord


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def edge_to_friction_ratio(
    edge_pct: float | None, friction_pct: float = EXPECTED_COST_PCT
) -> float | None:
    if edge_pct is None or friction_pct <= 0:
        return None
    return abs(edge_pct) / friction_pct


def _cross_sectional_summaries(records: list[PanelRecord], target: str) -> list[float]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for record in records:
        value = record.targets.get(target)
        if value is not None:
            grouped[(record.signal_date.isoformat(), record.market)].append(value)
    summaries = []
    for values in grouped.values():
        if len(values) >= 2:
            low, high = percentile(values, 0.1), percentile(values, 0.9)
            if low is not None and high is not None:
                summaries.append(high - low)
    return summaries


def calculate_opportunity_surface(
    records: list[PanelRecord], *, friction_pct: float = EXPECTED_COST_PCT
) -> dict[str, Any]:
    results = []
    for horizon in HORIZONS:
        horizon_rows = [record for record in records if record.horizon_sessions == horizon]
        for target in TARGETS:
            values = [
                record.targets[target]
                for record in horizon_rows
                if record.targets.get(target) is not None
            ]
            if not values:
                results.append(
                    {
                        "target": target,
                        "horizon_sessions": horizon,
                        "sample_size": 0,
                        "unique_dates": 0,
                        "stock_observations": 0,
                        "status": "UNAVAILABLE",
                    }
                )
                continue
            numeric = [float(value) for value in values]
            cross_sectional = _cross_sectional_summaries(horizon_rows, target)
            absolute_moves = [abs(value) for value in numeric]
            mean_value = statistics.mean(numeric)
            median_value = statistics.median(numeric)
            typical_move = statistics.median(absolute_moves)
            cross_spread = statistics.mean(cross_sectional) if cross_sectional else None
            upper_tail = percentile(absolute_moves, 0.9)
            results.append(
                {
                    "target": target,
                    "horizon_sessions": horizon,
                    "mean_pct": mean_value,
                    "median_pct": median_value,
                    "sample_stddev_pct": statistics.stdev(numeric) if len(numeric) >= 2 else 0.0,
                    "p10_pct": percentile(numeric, 0.1),
                    "p25_pct": percentile(numeric, 0.25),
                    "p75_pct": percentile(numeric, 0.75),
                    "p90_pct": percentile(numeric, 0.9),
                    "positive_rate": sum(value > 0 for value in numeric) / len(numeric),
                    "mean_cross_sectional_dispersion_pct": (
                        statistics.mean(cross_sectional) if cross_sectional else None
                    ),
                    "cross_sectional_spread_pct": cross_spread,
                    "typical_absolute_move_pct": typical_move,
                    "upper_tail_absolute_move_pct": upper_tail,
                    "edge_to_friction_ratio": {
                        "typical_absolute_move": edge_to_friction_ratio(typical_move, friction_pct),
                        "cross_sectional_spread": edge_to_friction_ratio(
                            cross_spread, friction_pct
                        ),
                        "upper_tail_absolute_move": edge_to_friction_ratio(
                            upper_tail, friction_pct
                        ),
                    },
                    "unique_dates": len({record.signal_date for record in horizon_rows}),
                    "stock_observations": len(numeric),
                    "date_clusters": len({record.signal_date for record in horizon_rows}),
                    "non_overlap_observations": count_non_overlapping(horizon_rows),
                    "expected_friction_pct": friction_pct,
                    "status": "DESCRIPTIVE_SCREEN",
                }
            )
    return {
        "artifact": "opportunity-surface",
        "scope": "pre-strategy unconditional target variation; no factor-state or rule selection",
        "results": results,
    }


def count_non_overlapping(records: list[PanelRecord]) -> int:
    by_symbol: dict[str, list[PanelRecord]] = defaultdict(list)
    for record in records:
        by_symbol[record.symbol].append(record)
    kept = 0
    for rows in by_symbol.values():
        last_exit = None
        for record in sorted(rows, key=lambda row: row.signal_date):
            if last_exit is None or record.entry_date > last_exit:
                kept += 1
                last_exit = record.exit_date
    return kept


def safe_json_number(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)

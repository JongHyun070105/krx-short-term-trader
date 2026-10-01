from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from krx_trader.research.pipeline.config import HORIZONS, MARKET_SCOPES, TARGETS
from krx_trader.research.pipeline.factor_registry import FactorSpec
from krx_trader.research.pipeline.panel import PanelRecord


@dataclass(frozen=True)
class DailyIC:
    factor_id: str
    family_id: str
    target: str
    horizon: int
    market_scope: str
    signal_date: str
    value: float
    participants: int


def average_ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda pair: pair[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        rank = ((cursor + 1) + end) / 2
        for index, _ in indexed[cursor:end]:
            ranks[index] = rank
        cursor = end
    return ranks


def spearman(values_x: list[float], values_y: list[float]) -> float | None:
    if len(values_x) != len(values_y) or len(values_x) < 2:
        return None
    ranks_x, ranks_y = average_ranks(values_x), average_ranks(values_y)
    mean_x, mean_y = statistics.mean(ranks_x), statistics.mean(ranks_y)
    centered_x = [value - mean_x for value in ranks_x]
    centered_y = [value - mean_y for value in ranks_y]
    denom_x = math.sqrt(sum(value * value for value in centered_x))
    denom_y = math.sqrt(sum(value * value for value in centered_y))
    if denom_x == 0 or denom_y == 0:
        return None
    return sum(x * y for x, y in zip(centered_x, centered_y, strict=True)) / (denom_x * denom_y)


def _market_date_ics(
    records: list[PanelRecord],
    factor_values: dict[str, list[float | None]],
    spec: FactorSpec,
    target: str,
    horizon: int,
    minimum_participants: int,
) -> dict[tuple[str, str], tuple[float, int]]:
    grouped: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    values = factor_values[spec.factor_id]
    for index, record in enumerate(records):
        if record.horizon_sessions != horizon:
            continue
        x = values[index]
        y = record.targets.get(target)
        if x is None or y is None:
            continue
        grouped[(record.signal_date.isoformat(), record.market)].append((x, float(y)))
    result = {}
    for key, pairs in grouped.items():
        if len(pairs) < minimum_participants:
            continue
        ic = spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
        if ic is not None:
            result[key] = (ic, len(pairs))
    return result


def build_daily_ics(
    records: list[PanelRecord],
    specs: list[FactorSpec],
    factor_values: dict[str, list[float | None]],
    *,
    minimum_participants: int,
) -> list[DailyIC]:
    daily: list[DailyIC] = []
    for spec in specs:
        for target in TARGETS:
            for horizon in HORIZONS:
                grouped = _market_date_ics(
                    records, factor_values, spec, target, horizon, minimum_participants
                )
                for scope in MARKET_SCOPES:
                    by_date: dict[str, list[tuple[float, int]]] = defaultdict(list)
                    for (day, market), (value, participants) in grouped.items():
                        if scope == "ALL" or market == scope:
                            by_date[day].append((value, participants))
                    for day, items in by_date.items():
                        daily.append(
                            DailyIC(
                                factor_id=spec.factor_id,
                                family_id=spec.family_id,
                                target=target,
                                horizon=horizon,
                                market_scope=scope,
                                signal_date=day,
                                value=statistics.mean(value for value, _ in items),
                                participants=sum(n for _, n in items),
                            )
                        )
    return daily


def cluster_mean_ci(values: list[float], *, confidence: float = 0.90) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    mean = statistics.mean(values)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    z = 1.6448536269514722 if confidence == 0.90 else 1.959963984540054
    return mean - z * standard_error, mean + z * standard_error


def summarize_daily_ics(daily: list[DailyIC]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, int, str], list[DailyIC]] = defaultdict(list)
    for item in daily:
        grouped[(item.factor_id, item.target, item.horizon, item.market_scope)].append(item)
    rows = []
    for (factor, target, horizon, scope), values in sorted(grouped.items()):
        series = [item.value for item in values]
        mean = statistics.mean(series)
        stddev = statistics.stdev(series) if len(series) >= 2 else 0.0
        ci = cluster_mean_ci(series) if len(series) >= 40 else None
        rows.append(
            {
                "factor_id": factor,
                "target": target,
                "horizon_sessions": horizon,
                "market_scope": scope,
                "number_of_dates": len(series),
                "number_of_market_date_groups": len(series),
                "mean_ic": mean,
                "median_ic": statistics.median(series),
                "ic_stddev": stddev,
                "positive_ic_fraction": sum(value > 0 for value in series) / len(series),
                "icir": mean / stddev * math.sqrt(len(series))
                if len(series) >= 40 and stddev > 0
                else None,
                "signal_date_cluster_ci90": list(ci) if ci is not None else None,
                "ci_method": "signal-date-cluster normal interval; family bootstrap is recorded in viability artifact",
            }
        )
    return {
        "artifact": "factor-predictability",
        "daily_ic_unit": "signal date; market-date ICs are averaged within date for ALL",
        "results": rows,
    }


def daily_ic_index(daily: list[DailyIC]) -> dict[tuple[str, str, int, str, str], DailyIC]:
    return {
        (item.factor_id, item.target, item.horizon, item.market_scope, item.signal_date): item
        for item in daily
    }

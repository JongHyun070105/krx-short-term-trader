from __future__ import annotations

import math
import statistics
from collections import defaultdict
from itertools import pairwise
from typing import Any

from krx_trader.research.pipeline.config import HORIZONS, MARKET_SCOPES, TARGETS
from krx_trader.research.pipeline.factor_registry import FactorSpec
from krx_trader.research.pipeline.panel import PanelRecord
from krx_trader.research.pipeline.predictability import average_ranks, spearman


def _quantile_number(rank: float, participants: int) -> int:
    return max(1, min(5, math.floor((rank - 1) / participants * 5) + 1))


def build_quantile_artifacts(
    records: list[PanelRecord],
    specs: list[FactorSpec],
    factor_values: dict[str, list[float | None]],
    *,
    minimum_participants: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    record_index = {record.key: index for index, record in enumerate(records)}
    group_results: dict[tuple[str, str, int, str, str], list[dict[int, list[float]]]] = {}
    max_distinct: dict[tuple[str, str, int, str], int] = defaultdict(int)
    for spec in specs:
        for horizon in HORIZONS:
            values = factor_values[spec.factor_id]
            for target in TARGETS:
                target_groups: dict[tuple[str, str], list[tuple[PanelRecord, float, float]]] = (
                    defaultdict(list)
                )
                for record in records:
                    if record.horizon_sessions != horizon:
                        continue
                    factor = values[record_index[record.key]]
                    outcome = record.targets.get(target)
                    if factor is None or outcome is None:
                        continue
                    target_groups[(record.signal_date.isoformat(), record.market)].append(
                        (record, factor, float(outcome))
                    )
                for (day, market), items in target_groups.items():
                    if len(items) < minimum_participants:
                        continue
                    ranks = average_ranks([factor for _, factor, _ in items])
                    q_values: dict[int, list[float]] = {q: [] for q in range(1, 6)}
                    for rank, (_, _, outcome) in zip(ranks, items, strict=True):
                        q_values[_quantile_number(rank, len(items))].append(outcome)
                    q_means = {
                        q: statistics.mean(q_values[q]) if q_values[q] else None
                        for q in range(1, 6)
                    }
                    distinct = len({factor for _, factor, _ in items})
                    key = (spec.factor_id, target, horizon, day, market)
                    group_results.setdefault(key, []).append(
                        {
                            "means": q_means,
                            "counts": {q: len(q_values[q]) for q in range(1, 6)},
                            "distinct_levels": distinct,
                        }
                    )
                    max_distinct[(spec.factor_id, target, horizon, market)] = max(
                        max_distinct[(spec.factor_id, target, horizon, market)], distinct
                    )

    spread_rows = []
    monotonicity_rows = []
    per_surface: dict[tuple[str, str, int], dict[str, dict[int, list[tuple[str, float, int]]]]] = (
        defaultdict(dict)
    )
    for (factor, target, horizon, day, market), values in group_results.items():
        summary = values[0]
        for scope in MARKET_SCOPES:
            if scope != "ALL" and scope != market:
                continue
            container = per_surface[(factor, target, horizon)].setdefault(
                scope, {q: [] for q in range(1, 6)}
            )
            for q in range(1, 6):
                mean = summary["means"][q]
                count = summary["counts"][q]
                if mean is not None:
                    container[q].append((day, mean, count))

    for (factor, target, horizon), scopes in sorted(per_surface.items()):
        for scope, quantiles in sorted(scopes.items()):
            q_summaries = []
            mean_by_q: dict[int, float] = {}
            for q in range(1, 6):
                entries = quantiles[q]
                cluster_means = [entry[1] for entry in entries]
                raw_count = sum(entry[2] for entry in entries)
                mean = statistics.mean(cluster_means) if cluster_means else None
                if mean is not None:
                    mean_by_q[q] = mean
                q_summaries.append(
                    {
                        "quantile": q,
                        "mean_outcome_pct": mean,
                        "raw_observations": raw_count,
                        "market_date_clusters": len(entries),
                        "unique_dates": len({entry[0] for entry in entries}),
                    }
                )
            spread = mean_by_q[5] - mean_by_q[1] if 1 in mean_by_q and 5 in mean_by_q else None
            reverse_spread = -spread if spread is not None else None
            key = (factor, target, horizon, scope)
            max_levels = max_distinct.get((factor, target, horizon, scope), 0)
            if scope == "ALL":
                max_levels = max(
                    (
                        max_distinct.get((factor, target, horizon, market), 0)
                        for market in ("KOSPI", "KOSDAQ")
                    ),
                    default=0,
                )
            spread_rows.append(
                {
                    "factor_id": factor,
                    "target": target,
                    "horizon_sessions": horizon,
                    "market_scope": scope,
                    "quantiles": q_summaries,
                    "q5_minus_q1_pct": spread,
                    "q1_minus_q5_pct": reverse_spread,
                    "distinct_factor_levels_max": max_levels,
                }
            )
            ordered = [mean_by_q.get(q) for q in range(1, 6)]
            usable = [(q, value) for q, value in enumerate(ordered, start=1) if value is not None]
            qcorr = (
                spearman([float(q) for q, _ in usable], [float(value) for _, value in usable])
                if len(usable) >= 3
                else None
            )
            inversions_positive = sum(
                1
                for left, right in pairwise(ordered)
                if left is not None and right is not None and right < left
            )
            inversions_negative = sum(
                1
                for left, right in pairwise(ordered)
                if left is not None and right is not None and right > left
            )
            middle = mean_by_q.get(3)
            positive_consistent = (
                middle is not None
                and mean_by_q.get(1) is not None
                and mean_by_q.get(5) is not None
                and mean_by_q[1] <= middle
                and mean_by_q[5] >= middle
            )
            negative_consistent = (
                middle is not None
                and mean_by_q.get(1) is not None
                and mean_by_q.get(5) is not None
                and mean_by_q[1] >= middle
                and mean_by_q[5] <= middle
            )
            monotonicity_rows.append(
                {
                    "factor_id": factor,
                    "target": target,
                    "horizon_sessions": horizon,
                    "market_scope": scope,
                    "quantile_number_spearman": qcorr,
                    "adjacent_inversions_positive": inversions_positive,
                    "adjacent_inversions_negative": inversions_negative,
                    "extreme_vs_middle_consistency_positive": positive_consistent,
                    "extreme_vs_middle_consistency_negative": negative_consistent,
                    "distinct_factor_levels_max": max_levels,
                    "status": "INSUFFICIENT_DISTINCT_LEVELS" if max_levels < 5 else "MEASURED",
                }
            )
    return (
        {"artifact": "quantile-spreads", "quantile_count": 5, "results": spread_rows},
        {"artifact": "factor-monotonicity", "results": monotonicity_rows},
    )

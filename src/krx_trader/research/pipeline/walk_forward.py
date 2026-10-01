from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date
from typing import Any

from krx_trader.research.pipeline.config import WALK_FORWARD_BLOCKS
from krx_trader.research.pipeline.factor_registry import FactorSpec
from krx_trader.research.pipeline.opportunity_surface import (
    count_non_overlapping,
    edge_to_friction_ratio,
)
from krx_trader.research.pipeline.panel import PanelRecord
from krx_trader.research.pipeline.predictability import DailyIC, average_ranks
from krx_trader.research.pipeline.quantile_spreads import _quantile_number


def quantile_effect(
    records: list[PanelRecord],
    spec: FactorSpec,
    factor_values: dict[str, list[float | None]],
    *,
    target: str,
    horizon: int,
    market_scope: str,
    direction: str | None = None,
    minimum_participants: int = 10,
    date_window: tuple[date, date] | None = None,
) -> dict[str, Any]:
    record_index = {record.key: index for index, record in enumerate(records)}
    grouped: dict[tuple[str, str], list[tuple[PanelRecord, float, float]]] = defaultdict(list)
    for record in records:
        if date_window is not None and not date_window[0] <= record.signal_date <= date_window[1]:
            continue
        if record.horizon_sessions != horizon or record.targets.get(target) is None:
            continue
        value = factor_values[spec.factor_id][record_index[record.key]]
        if value is not None:
            grouped[(record.signal_date.isoformat(), record.market)].append(
                (record, value, float(record.targets[target]))
            )
    cluster_means: dict[tuple[str, str], dict[int, float]] = {}
    cluster_rows: dict[tuple[str, str], dict[int, list[PanelRecord]]] = {}
    for (day, market), items in grouped.items():
        if len(items) < minimum_participants:
            continue
        ranks = average_ranks([value for _, value, _ in items])
        bucket_values: dict[int, list[float]] = {q: [] for q in range(1, 6)}
        bucket_records: dict[int, list[PanelRecord]] = {q: [] for q in range(1, 6)}
        for rank, (record, _, outcome) in zip(ranks, items, strict=True):
            bucket = _quantile_number(rank, len(items))
            bucket_values[bucket].append(outcome)
            bucket_records[bucket].append(record)
        cluster_means[(day, market)] = {
            q: statistics.mean(values) for q, values in bucket_values.items() if values
        }
        cluster_rows[(day, market)] = bucket_records
    if market_scope in {"KOSPI", "KOSDAQ"}:
        keys = [key for key in cluster_means if key[1] == market_scope]
        cluster_means = {key: cluster_means[key] for key in keys}
        cluster_rows = {key: cluster_rows[key] for key in keys}
    else:
        by_date: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for key in cluster_means:
            by_date[key[0]].append(key)
        pooled_means: dict[tuple[str, str], dict[int, float]] = {}
        pooled_rows: dict[tuple[str, str], dict[int, list[PanelRecord]]] = {}
        for day, keys in by_date.items():
            pooled_key = (day, "ALL")
            pooled_means[pooled_key] = {}
            pooled_rows[pooled_key] = {q: [] for q in range(1, 6)}
            for q in range(1, 6):
                means = [cluster_means[key][q] for key in keys if q in cluster_means[key]]
                if means:
                    pooled_means[pooled_key][q] = statistics.mean(means)
                for key in keys:
                    pooled_rows[pooled_key][q].extend(cluster_rows[key][q])
        cluster_means, cluster_rows = pooled_means, pooled_rows
    q_means: dict[int, float] = {}
    q_samples: dict[int, int] = {}
    q_dates: dict[int, set[str]] = {}
    for q in range(1, 6):
        values = [means[q] for means in cluster_means.values() if q in means]
        if values:
            q_means[q] = statistics.mean(values)
        selected = [row for key, groups in cluster_rows.items() for row in groups[q]]
        q_samples[q] = len(selected)
        q_dates[q] = {row.signal_date.isoformat() for row in selected}
    selected_direction = direction or spec.orientation_hypotheses[0]
    selected_q = 5 if selected_direction == "positive" else 1
    selected_rows = [record for groups in cluster_rows.values() for record in groups[selected_q]]
    selected_target_mean = q_means.get(selected_q)
    absolute_mean = selected_target_mean if target == "ABSOLUTE_RETURN" else None
    spread = q_means.get(5, 0.0) - q_means.get(1, 0.0) if 1 in q_means and 5 in q_means else None
    return {
        "quantile_means": q_means,
        "quantile_samples": q_samples,
        "quantile_unique_dates": {q: len(days) for q, days in q_dates.items()},
        "q5_minus_q1_pct": spread,
        "selected_long_only_quantile": selected_q,
        "selected_quantile_target_mean_pct": selected_target_mean,
        "selected_long_only_gross_mean_pct": absolute_mean,
        "selected_long_only_edge_to_friction_ratio": edge_to_friction_ratio(absolute_mean),
        "selected_observations": len(selected_rows),
        "selected_unique_dates": len({row.signal_date for row in selected_rows}),
        "selected_date_clusters": len({row.signal_date for row in selected_rows}),
        "selected_non_overlap_observations": count_non_overlapping(selected_rows),
        "selected_records": selected_rows,
        "max_distinct_levels": max(
            (
                len(
                    {
                        factor_values[spec.factor_id][record_index[record.key]]
                        for record in rows[selected_q]
                    }
                )
                for rows in cluster_rows.values()
                if rows.get(selected_q)
            ),
            default=0,
        ),
    }


def _family_block_summary(
    records: list[PanelRecord],
    daily_ics: list[DailyIC],
    spec: FactorSpec,
    factor_values: dict[str, list[float | None]],
    *,
    target: str,
    horizon: int,
    scope: str,
    direction: str,
    minimum_participants: int,
) -> dict[str, Any]:
    sign = 1.0 if direction == "positive" else -1.0
    output = []
    record_index = {record.key: index for index, record in enumerate(records)}
    ic_by_date: dict[str, list[float]] = defaultdict(list)
    for item in daily_ics:
        if (
            item.factor_id == spec.factor_id
            and item.target == target
            and item.horizon == horizon
            and item.market_scope == scope
        ):
            ic_by_date[item.signal_date].append(item.value)
    for block in WALK_FORWARD_BLOCKS:
        start, end = date.fromisoformat(block["start"]), date.fromisoformat(block["end"])
        block_rows = [
            record
            for record in records
            if start <= record.signal_date <= end
            and record.horizon_sessions == horizon
            and record.targets.get(target) is not None
            and factor_values[spec.factor_id][record_index[record.key]] is not None
        ]
        block_daily = [
            sign * statistics.mean(values)
            for day, values in ic_by_date.items()
            if start <= date.fromisoformat(day) <= end and values
        ]
        effect = quantile_effect(
            records,
            spec,
            factor_values,
            target=target,
            horizon=horizon,
            market_scope=scope,
            direction=direction,
            minimum_participants=minimum_participants,
            date_window=(start, end),
        )
        spread = effect["q5_minus_q1_pct"]
        oriented_spread = sign * spread if spread is not None else None
        oriented_ic = statistics.mean(block_daily) if block_daily else None
        output.append(
            {
                "block": block["name"],
                "role": block["role"],
                "start": block["start"],
                "end": block["end"],
                "mean_ic": (oriented_ic * sign) if oriented_ic is not None else None,
                "oriented_mean_ic": oriented_ic,
                "q5_minus_q1_pct": spread,
                "oriented_quantile_spread_pct": oriented_spread,
                "target_direction": "POSITIVE"
                if oriented_spread is not None and oriented_spread > 0
                else "NON_POSITIVE_OR_UNAVAILABLE",
                "sample_size": len(block_rows),
                "unique_dates": len({row.signal_date for row in block_rows}),
                "non_overlap_observations": effect["selected_non_overlap_observations"],
                "edge_to_friction_ratio": effect["selected_long_only_edge_to_friction_ratio"],
            }
        )
    valid = [row for row in output if row["unique_dates"] >= 20]
    sign_passes = sum(
        row["oriented_quantile_spread_pct"] is not None and row["oriented_quantile_spread_pct"] > 0
        for row in valid
    )
    ic_passes = sum(
        row["oriented_mean_ic"] is not None and row["oriented_mean_ic"] > 0 for row in valid
    )
    spread_passes = sign_passes
    sufficiently_sampled = sum(row["unique_dates"] >= 20 for row in output)
    first_effect = next(
        (
            abs(row["oriented_quantile_spread_pct"])
            for row in output
            if row["oriented_quantile_spread_pct"] is not None
        ),
        None,
    )
    last_effect = next(
        (
            abs(row["oriented_quantile_spread_pct"])
            for row in reversed(output)
            if row["oriented_quantile_spread_pct"] is not None
        ),
        None,
    )
    decay = None if first_effect in (None, 0) or last_effect is None else last_effect / first_effect
    return {
        "factor_id": spec.factor_id,
        "family_id": spec.family_id,
        "target": target,
        "horizon_sessions": horizon,
        "market_scope": scope,
        "semantic_direction": direction,
        "blocks": output,
        "stability_components": {
            "sign_consistency": {
                "positive_blocks": sign_passes,
                "eligible_blocks": len(valid),
                "fraction": sign_passes / len(valid) if valid else None,
            },
            "ic_consistency": {
                "positive_blocks": ic_passes,
                "eligible_blocks": len(valid),
                "fraction": ic_passes / len(valid) if valid else None,
            },
            "spread_consistency": {
                "positive_blocks": spread_passes,
                "eligible_blocks": len(valid),
                "fraction": spread_passes / len(valid) if valid else None,
            },
            "market_consistency": "reported separately in market-adjusted gate",
            "effect_size_decay_last_over_first": decay,
            "sample_sufficiency": {
                "blocks_with_at_least_20_dates": sufficiently_sampled,
                "required_blocks": len(output),
            },
        },
        "stable_blocks": sign_passes,
        "required_blocks": len(output),
    }


def walk_forward_summary(
    records: list[PanelRecord],
    daily_ics: list[DailyIC],
    selected_surfaces: dict[str, dict[str, Any]],
    specs_by_id: dict[str, FactorSpec],
    factor_values: dict[str, list[float | None]],
    *,
    minimum_participants: int,
) -> dict[str, Any]:
    results = []
    for family_id, surface in sorted(selected_surfaces.items()):
        spec = specs_by_id[surface["factor_id"]]
        result = _family_block_summary(
            records,
            daily_ics,
            spec,
            factor_values,
            target=surface["target"],
            horizon=surface["horizon_sessions"],
            scope=surface["market_scope"],
            direction=surface["direction"],
            minimum_participants=minimum_participants,
        )
        result["family_id"] = family_id
        results.append(result)
    return {
        "artifact": "walk-forward-stability",
        "blocks": [dict(block) for block in WALK_FORWARD_BLOCKS],
        "phase13_h2_challenge_in_core_blocks": False,
        "results": results,
    }

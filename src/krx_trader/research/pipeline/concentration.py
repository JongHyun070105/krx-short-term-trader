from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from typing import Any

import numpy as np

from krx_trader.research.pipeline.config import CLUSTER_BOOTSTRAP_REPETITIONS, RANDOM_SEED
from krx_trader.research.pipeline.factor_registry import FactorSpec
from krx_trader.research.pipeline.panel import PanelRecord
from krx_trader.research.pipeline.walk_forward import quantile_effect


def _shares(contributions: dict[str, float]) -> list[tuple[str, float]]:
    total = sum(contributions.values())
    if total <= 0:
        return []
    return sorted(
        ((key, value / total * 100) for key, value in contributions.items()),
        key=lambda item: item[1],
        reverse=True,
    )


def _cluster_means(records: list[PanelRecord], target: str) -> dict[str, float]:
    by_date_market: dict[tuple[str, str], list[float]] = defaultdict(list)
    for record in records:
        value = record.targets.get(target)
        if value is not None:
            by_date_market[(record.signal_date.isoformat(), record.market)].append(float(value))
    by_date: dict[str, list[float]] = defaultdict(list)
    for (day, _market), values in by_date_market.items():
        if values:
            by_date[day].append(statistics.mean(values))
    return {day: statistics.mean(values) for day, values in by_date.items() if values}


def date_cluster_bootstrap_ci(
    records: list[PanelRecord],
    *,
    target: str,
    repetitions: int = CLUSTER_BOOTSTRAP_REPETITIONS,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    clusters = _cluster_means(records, target)
    values = np.asarray(list(clusters.values()), dtype=np.float64)
    if len(values) < 2:
        return {
            "target": target,
            "clusters": len(values),
            "ci90_pct": None,
            "status": "INSUFFICIENT_DATE_CLUSTERS",
        }
    digest = hashlib.sha256(f"{seed}:{target}".encode()).digest()
    local_seed = int.from_bytes(digest[:4], "big")
    rng = np.random.default_rng(local_seed)
    sample_indices = rng.integers(0, len(values), size=(repetitions, len(values)))
    means = values[sample_indices].mean(axis=1)
    low, high = np.quantile(means, [0.05, 0.95])
    return {
        "target": target,
        "clusters": len(values),
        "observed_cluster_mean_pct": float(values.mean()),
        "ci90_pct": [float(low), float(high)],
        "repetitions": repetitions,
        "seed": seed,
        "cluster": "signal_date; all market subgroups are averaged within date",
        "bootstrap_sha256": hashlib.sha256(means.tobytes()).hexdigest(),
        "status": "COMPUTED",
    }


def concentration_analysis(
    records: list[PanelRecord],
    selected_surfaces: dict[str, dict[str, Any]],
    specs_by_id: dict[str, FactorSpec],
    factor_values: dict[str, list[float | None]],
) -> dict[str, Any]:
    results = []
    for family_id, surface in sorted(selected_surfaces.items()):
        effect = quantile_effect(
            records,
            specs_by_id[surface["factor_id"]],
            factor_values,
            target="ABSOLUTE_RETURN",
            horizon=surface["horizon_sessions"],
            market_scope=surface["market_scope"],
            direction=surface["direction"],
        )
        selected = effect["selected_records"]
        positive = [
            (record, float(record.targets["ABSOLUTE_RETURN"]))
            for record in selected
            if record.targets.get("ABSOLUTE_RETURN") is not None
            and float(record.targets["ABSOLUTE_RETURN"]) > 0
        ]
        observation = sorted((value for _, value in positive), reverse=True)
        symbols: dict[str, float] = defaultdict(float)
        dates: dict[str, float] = defaultdict(float)
        for record, value in positive:
            symbols[record.symbol] += value
            dates[record.signal_date.isoformat()] += value
        symbol_shares, date_shares = _shares(symbols), _shares(dates)
        positive_total = sum(observation)
        results.append(
            {
                "family_id": family_id,
                "factor_id": surface["factor_id"],
                "target": "ABSOLUTE_RETURN",
                "horizon_sessions": surface["horizon_sessions"],
                "market_scope": surface["market_scope"],
                "orientation": surface["direction"],
                "stock_observations": len(selected),
                "positive_contribution_observations": len(positive),
                "unique_signal_dates": len({record.signal_date for record in selected}),
                "date_clusters": len({record.signal_date for record in selected}),
                "non_overlap_observations": effect["selected_non_overlap_observations"],
                "top_observation_share_pct": observation[0] / positive_total * 100
                if positive_total
                else None,
                "top_five_observation_share_pct": sum(observation[:5]) / positive_total * 100
                if positive_total
                else None,
                "top_symbol": symbol_shares[0][0] if symbol_shares else None,
                "top_symbol_share_pct": symbol_shares[0][1] if symbol_shares else None,
                "top_three_symbols": [
                    {"symbol": symbol, "share_pct": share} for symbol, share in symbol_shares[:3]
                ],
                "top_three_symbols_share_pct": sum(share for _, share in symbol_shares[:3])
                if symbol_shares
                else None,
                "top_date": date_shares[0][0] if date_shares else None,
                "top_date_share_pct": date_shares[0][1] if date_shares else None,
                "top_five_dates": [
                    {"date": day, "share_pct": share} for day, share in date_shares[:5]
                ],
                "top_five_dates_share_pct": sum(share for _, share in date_shares[:5])
                if date_shares
                else None,
                "contribution_basis": "positive absolute forward-return contribution within the selected broad factor quantile",
            }
        )
    return {"artifact": "concentration-analysis", "results": results}


def family_cluster_bootstrap(
    records: list[PanelRecord],
    selected_surfaces: dict[str, dict[str, Any]],
    specs_by_id: dict[str, FactorSpec],
    factor_values: dict[str, list[float | None]],
    *,
    repetitions: int = CLUSTER_BOOTSTRAP_REPETITIONS,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    results = []
    targets = ("ABSOLUTE_RETURN", "SIMPLE_EXCESS_RETURN", "BETA_RESIDUAL_RETURN")
    for family_id, surface in sorted(selected_surfaces.items()):
        spec = specs_by_id[surface["factor_id"]]
        per_target = []
        for target in targets:
            effect = quantile_effect(
                records,
                spec,
                factor_values,
                target=target,
                horizon=surface["horizon_sessions"],
                market_scope=surface["market_scope"],
                direction=surface["direction"],
            )
            selected = effect["selected_records"]
            ci = date_cluster_bootstrap_ci(
                selected,
                target=target,
                repetitions=repetitions,
                seed=seed + int(hashlib.sha256(family_id.encode()).hexdigest()[:6], 16),
            )
            per_target.append(
                {
                    "target": target,
                    "selected_quantile": effect["selected_long_only_quantile"],
                    "selected_observations": effect["selected_observations"],
                    **ci,
                }
            )
        results.append(
            {
                "family_id": family_id,
                "factor_id": surface["factor_id"],
                "horizon_sessions": surface["horizon_sessions"],
                "market_scope": surface["market_scope"],
                "orientation": surface["direction"],
                "target_date_cluster_bootstrap": per_target,
                "post_selection_descriptive_interval": True,
            }
        )
    return {
        "artifact": "family-date-cluster-bootstrap",
        "repetitions": repetitions,
        "seed": seed,
        "cluster_unit": "signal_date",
        "stock_rows_bootstrapped_independently": False,
        "results": results,
    }

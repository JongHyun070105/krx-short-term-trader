from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from krx_trader.research.pipeline.config import HORIZONS, RANDOM_SEED, TARGETS
from krx_trader.research.pipeline.factor_registry import FactorSpec
from krx_trader.research.pipeline.panel import PanelRecord
from krx_trader.research.pipeline.predictability import average_ranks, spearman


@dataclass(frozen=True)
class FamilySurface:
    horizon: int
    dates: tuple[str, ...]
    markets: tuple[str, ...]
    lengths: np.ndarray
    factors: np.ndarray
    targets: np.ndarray


def _normalized_rank(values: list[float]) -> list[float]:
    ranks = average_ranks(values)
    center = sum(ranks) / len(ranks) if ranks else 0.0
    centered = [rank - center for rank in ranks]
    norm = math.sqrt(sum(value * value for value in centered))
    return [value / norm for value in centered] if norm else [0.0] * len(values)


def _family_blocks(
    records: list[PanelRecord],
    specs: list[FactorSpec],
    factor_values: dict[str, list[float | None]],
    *,
    minimum_participants: int,
) -> list[FamilySurface]:
    by_key = {record.key: index for index, record in enumerate(records)}
    by_horizon: dict[int, dict[tuple[str, str], dict[str, PanelRecord]]] = {}
    for horizon in HORIZONS:
        groups: dict[tuple[str, str], dict[str, PanelRecord]] = {}
        for record in records:
            if record.horizon_sessions == horizon:
                groups.setdefault((record.signal_date.isoformat(), record.market), {})[
                    record.symbol
                ] = record
        by_horizon[horizon] = groups
    common_group_keys = set.intersection(*(set(by_horizon[horizon]) for horizon in HORIZONS))
    surfaces = []
    for horizon in HORIZONS:
        groups = []
        for group_key in sorted(common_group_keys):
            horizon_symbols = [by_horizon[each][group_key] for each in HORIZONS]
            symbol_sets = [set(group) for group in horizon_symbols]
            common_symbols = set.intersection(*symbol_sets)
            valid_symbols = []
            for symbol in sorted(common_symbols):
                rows_for_symbol = [group[symbol] for group in horizon_symbols]
                indices = [by_key[row.key] for row in rows_for_symbol]
                if any(
                    any(row.targets.get(target) is None for target in TARGETS)
                    for row in rows_for_symbol
                ):
                    continue
                if any(
                    factor_values[spec.factor_id][index] is None
                    for spec in specs
                    for index in indices
                ):
                    continue
                valid_symbols.append((symbol, rows_for_symbol, indices))
            if len(valid_symbols) >= minimum_participants:
                groups.append((group_key, valid_symbols))
        if not groups:
            continue
        factor_count = len(specs)
        max_n = max(len(symbols) for _, symbols in groups)
        factor_matrix = np.zeros((factor_count, len(groups), max_n), dtype=np.float64)
        target_matrix = np.zeros((len(TARGETS), len(groups), max_n), dtype=np.float64)
        lengths = np.zeros(len(groups), dtype=np.int32)
        dates = []
        markets = []
        for group_index, ((day, market), symbols) in enumerate(groups):
            lengths[group_index] = len(symbols)
            dates.append(day)
            markets.append(market)
            row_for_horizon_position = HORIZONS.index(horizon)
            for spec_index, spec in enumerate(specs):
                raw_values = [
                    float(factor_values[spec.factor_id][indices[row_for_horizon_position]])
                    for _, _, indices in symbols
                ]
                factor_matrix[spec_index, group_index, : len(symbols)] = _normalized_rank(
                    raw_values
                )
            for target_index, target in enumerate(TARGETS):
                raw_targets = [
                    float(rows[row_for_horizon_position].targets[target]) for _, rows, _ in symbols
                ]
                target_matrix[target_index, group_index, : len(symbols)] = _normalized_rank(
                    raw_targets
                )
        surfaces.append(
            FamilySurface(
                horizon=horizon,
                dates=tuple(dates),
                markets=tuple(markets),
                lengths=lengths,
                factors=factor_matrix,
                targets=target_matrix,
            )
        )
    return surfaces


def _scope_means(correlations: np.ndarray, markets: tuple[str, ...]) -> dict[str, np.ndarray]:
    result = {}
    for scope in ("ALL", "KOSPI", "KOSDAQ"):
        indices = [
            index for index, market in enumerate(markets) if scope == "ALL" or market == scope
        ]
        result[scope] = (
            correlations[:, :, indices].mean(axis=2)
            if indices
            else np.zeros(correlations.shape[:2])
        )
    return result


def _oriented_surface_max(
    spec_indices: list[int],
    scopes: dict[str, np.ndarray],
    specs: list[FactorSpec],
) -> tuple[float, dict[str, Any]]:
    best = -math.inf
    selection: dict[str, Any] = {}
    for scope in ("ALL", "KOSPI", "KOSDAQ"):
        values = scopes[scope]
        for spec_index in spec_indices:
            spec = specs[spec_index]
            for direction in spec.orientation_hypotheses:
                sign = 1.0 if direction == "positive" else -1.0
                for target_index, target in enumerate(TARGETS):
                    value = float(values[spec_index, target_index]) * sign
                    if value > best:
                        best = value
                        selection = {
                            "factor_id": spec.factor_id,
                            "family_id": spec.family_id,
                            "target": target,
                            "horizon_sessions": None,
                            "market_scope": scope,
                            "direction": direction,
                            "mean_ic": float(values[spec_index, target_index]),
                            "oriented_mean_ic": value,
                        }
    return best, selection


def _family_observed_max(
    surfaces: list[FamilySurface], specs: list[FactorSpec]
) -> tuple[float, dict[str, Any], int]:
    family_by_id = {spec.factor_id: spec.family_id for spec in specs}
    best_stat = -math.inf
    best_selection: dict[str, Any] = {}
    group_count = 0
    for surface in surfaces:
        correlations = np.einsum("vgn,tgn->vtg", surface.factors, surface.targets, optimize=True)
        scoped = _scope_means(correlations, surface.markets)
        for scope_values in scoped.values():
            group_count = max(group_count, len(surface.markets))
        for spec_index, spec in enumerate(specs):
            for scope, values in scoped.items():
                for direction in spec.orientation_hypotheses:
                    sign = 1.0 if direction == "positive" else -1.0
                    for target_index, target in enumerate(TARGETS):
                        ic = float(values[spec_index, target_index])
                        oriented = sign * ic
                        if oriented > best_stat:
                            best_stat = oriented
                            best_selection = {
                                "factor_id": spec.factor_id,
                                "family_id": family_by_id[spec.factor_id],
                                "target": target,
                                "horizon_sessions": surface.horizon,
                                "market_scope": scope,
                                "direction": direction,
                                "mean_ic": ic,
                                "oriented_mean_ic": oriented,
                            }
    return best_stat, best_selection, group_count


def _stable_seed(base_seed: int, label: str) -> int:
    digest = hashlib.sha256(f"{base_seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    return (
        ordered[low]
        if low == high
        else ordered[low] * (high - position) + ordered[high] * (position - low)
    )


def permutation_max_statistics(
    records: list[PanelRecord],
    specs: list[FactorSpec],
    factor_values: dict[str, list[float | None]],
    *,
    permutations: int,
    seed: int = RANDOM_SEED,
    minimum_participants: int = 10,
    minimum_ic_threshold: float = 0.02,
) -> dict[str, Any]:
    if permutations < 1:
        raise ValueError("permutations must be positive")
    if not specs:
        raise ValueError("family max-statistic requires at least one factor")
    surfaces = _family_blocks(
        records, specs, factor_values, minimum_participants=minimum_participants
    )
    if not surfaces:
        return {
            "observed_family_best_statistic": None,
            "null_max_distribution": [],
            "empirical_p": None,
            "status": "INSUFFICIENT_COMMON_MARKET_DATE_GROUPS",
        }
    observed, selected, groups = _family_observed_max(surfaces, specs)
    selected_permutation_maxima: list[float] = []
    rng = np.random.default_rng(_stable_seed(seed, specs[0].family_id))
    for _ in range(permutations):
        permutation_maximum = -math.inf
        for surface in surfaces:
            group_count = len(surface.markets)
            max_n = surface.factors.shape[2]
            random_keys = rng.random((group_count, max_n))
            positions = np.arange(max_n)[None, :]
            random_keys[positions >= surface.lengths[:, None]] = np.inf
            permutation_indices = np.argsort(random_keys, axis=1, kind="stable")
            shuffled = np.take_along_axis(
                surface.factors, np.broadcast_to(permutation_indices, surface.factors.shape), axis=2
            )
            correlations = np.einsum("vgn,tgn->vtg", shuffled, surface.targets, optimize=True)
            scoped = _scope_means(correlations, surface.markets)
            for spec_index, spec in enumerate(specs):
                for scope_values in scoped.values():
                    for direction in spec.orientation_hypotheses:
                        sign = 1.0 if direction == "positive" else -1.0
                        oriented = scope_values[spec_index] * sign
                        if oriented.size:
                            permutation_maximum = max(permutation_maximum, float(np.max(oriented)))
        selected_permutation_maxima.append(permutation_maximum)
    exceedances = sum(value >= observed for value in selected_permutation_maxima)
    empirical_p = (1 + exceedances) / (permutations + 1)
    digest_payload = ",".join(f"{value:.12g}" for value in selected_permutation_maxima)
    return {
        "status": "COMPUTED",
        "method": "CROSS_SECTIONAL_WITHIN_MARKET_DATE_PERMUTATION",
        "family_id": specs[0].family_id,
        "factor_universe": [spec.factor_id for spec in specs],
        "observed_family_best_statistic": observed,
        "observed_best_surface": selected,
        "null_max_distribution": selected_permutation_maxima,
        "null_max_median": _percentile(selected_permutation_maxima, 0.50),
        "null_max_p90": _percentile(selected_permutation_maxima, 0.90),
        "null_max_p95": _percentile(selected_permutation_maxima, 0.95),
        "null_max_p99": _percentile(selected_permutation_maxima, 0.99),
        "empirical_exceedance_rate": exceedances / permutations,
        "empirical_p": empirical_p,
        "randomized_surface_max_at_least_minimum_ic_rate": (
            sum(value >= minimum_ic_threshold for value in selected_permutation_maxima)
            / permutations
        ),
        "minimum_ic_threshold": minimum_ic_threshold,
        "permutations": permutations,
        "seed": seed,
        "family_seed": _stable_seed(seed, specs[0].family_id),
        "eligible_market_date_groups": groups,
        "maximum_distribution_sha256": hashlib.sha256(digest_payload.encode("utf-8")).hexdigest(),
        "orientation_hypotheses_searched": sum(len(spec.orientation_hypotheses) for spec in specs),
        "targets": list(TARGETS),
        "horizons_sessions": list(HORIZONS),
        "market_scopes": ["ALL", "KOSPI", "KOSDAQ"],
        "same_family_search_surface_for_every_permutation": True,
    }


def negative_control_values(records: list[PanelRecord], seed: int = RANDOM_SEED) -> list[float]:
    values = []
    for record in records:
        payload = f"{seed}:{record.symbol}:{record.signal_date.isoformat()}".encode()
        raw = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
        values.append(raw / ((1 << 64) - 1))
    return values


def deterministic_permutation_digest(result: dict[str, Any]) -> str:
    values = result.get("null_max_distribution", [])
    payload = ",".join(f"{float(value):.12g}" for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def time_dislocation_null(
    records: list[PanelRecord],
    spec: FactorSpec,
    factor_values: dict[str, list[float | None]],
    *,
    permutations: int = 500,
    seed: int = RANDOM_SEED,
    minimum_shift_sessions: int = 60,
) -> dict[str, Any]:
    """Circular-shift one frozen continuous factor within each symbol's safe history."""
    chosen = [
        index
        for index, record in enumerate(records)
        if record.horizon_sessions == 5
        and record.targets.get("ABSOLUTE_RETURN") is not None
        and factor_values[spec.factor_id][index] is not None
    ]
    by_symbol: dict[str, list[int]] = {}
    for index in chosen:
        by_symbol.setdefault(records[index].symbol, []).append(index)
    symbol_series = {
        symbol: sorted(indices, key=lambda index: records[index].signal_date)
        for symbol, indices in by_symbol.items()
    }
    if not symbol_series or any(
        len(indices) <= 2 * minimum_shift_sessions for indices in symbol_series.values()
    ):
        return {"status": "INSUFFICIENT_SAFE_HISTORY", "permutations": permutations}
    observed_symbol_ics = []
    for indices in symbol_series.values():
        x = [float(factor_values[spec.factor_id][index]) for index in indices]
        y = [float(records[index].targets["ABSOLUTE_RETURN"]) for index in indices]
        result = spearman(x, y)
        if result is not None:
            observed_symbol_ics.append(result)
    observed = sum(observed_symbol_ics) / len(observed_symbol_ics)
    rng = np.random.default_rng(_stable_seed(seed, "time-dislocation:" + spec.factor_id))
    null_values = []
    for _ in range(permutations):
        symbol_ics = []
        for indices in symbol_series.values():
            count = len(indices)
            offset = int(rng.integers(minimum_shift_sessions, count - minimum_shift_sessions))
            x = [
                float(factor_values[spec.factor_id][indices[(i - offset) % count]])
                for i in range(count)
            ]
            y = [float(records[index].targets["ABSOLUTE_RETURN"]) for index in indices]
            result = spearman(x, y)
            if result is not None:
                symbol_ics.append(result)
        null_values.append(sum(symbol_ics) / len(symbol_ics) if symbol_ics else 0.0)
    exceedances = sum(value >= observed for value in null_values)
    payload = ",".join(f"{value:.12g}" for value in null_values)
    return {
        "status": "COMPUTED",
        "method": "WITHIN_SYMBOL_CIRCULAR_FACTOR_SHIFT",
        "factor_id": spec.factor_id,
        "target": "ABSOLUTE_RETURN",
        "horizon_sessions": 5,
        "observed_mean_within_symbol_spearman": observed,
        "null_median": _percentile(null_values, 0.5),
        "null_p90": _percentile(null_values, 0.9),
        "null_p95": _percentile(null_values, 0.95),
        "null_p99": _percentile(null_values, 0.99),
        "empirical_p_one_sided": (1 + exceedances) / (permutations + 1),
        "permutations": permutations,
        "seed": seed,
        "minimum_shift_sessions": minimum_shift_sessions,
        "symbols": len(symbol_series),
        "date_range": [
            min(
                records[index].signal_date
                for indices in symbol_series.values()
                for index in indices
            ).isoformat(),
            max(
                records[index].signal_date
                for indices in symbol_series.values()
                for index in indices
            ).isoformat(),
        ],
        "protected_period_rows_used": 0,
        "null_distribution_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "role": "secondary time-alignment diagnostic; cross-sectional family max null remains primary",
    }

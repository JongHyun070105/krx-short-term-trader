from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from krx_trader.research.pipeline.evidence_ledger import (
    EXTERNAL_2026,
    HOLDOUT_2026,
    PHASE13_CONFIRMATION,
    SAFE_PANEL_END,
    SAFE_PANEL_START,
    evidence_periods,
    historical_research_ledger,
)
from krx_trader.research.pipeline.factor_registry import all_factor_specs, registry_payload

PIPELINE_VERSION = "research-pipeline-v2"
RANDOM_SEED = 20261001
CROSS_SECTIONAL_PERMUTATIONS = 1_000
TIME_DISLOCATION_PERMUTATIONS = 500
CLUSTER_BOOTSTRAP_REPETITIONS = 2_000
EXPECTED_COST_PCT = 0.53
TARGETS = ("ABSOLUTE_RETURN", "SIMPLE_EXCESS_RETURN", "BETA_RESIDUAL_RETURN")
HORIZONS = (3, 5, 10)
MARKET_SCOPES = ("ALL", "KOSPI", "KOSDAQ")
WALK_FORWARD_BLOCKS = (
    {"name": "2023_H1", "start": "2023-01-02", "end": "2023-06-30", "role": "DISCOVERY"},
    {"name": "2023_H2", "start": "2023-07-01", "end": "2023-12-31", "role": "DISCOVERY"},
    {"name": "2024_H1", "start": "2024-01-01", "end": "2024-06-28", "role": "DISCOVERY"},
    {"name": "2024_H2", "start": "2024-07-01", "end": "2024-12-31", "role": "TOUCHED_REPLICATION"},
    {"name": "2025_H1", "start": "2025-01-01", "end": "2025-06-30", "role": "TOUCHED_REPLICATION"},
)


def default_config() -> dict[str, Any]:
    registry = registry_payload()
    specs = all_factor_specs()
    direction_hypotheses = sum(len(spec.orientation_hypotheses) for spec in specs)
    return {
        "artifact": "pipeline-v2-config",
        "version": PIPELINE_VERSION,
        "factor_registry_artifact": "factor-registry.json",
        "factor_registry_sha256_is_recorded_separately": True,
        "metrics": {
            "opportunity": [
                "mean",
                "median",
                "sample_stddev",
                "p10",
                "p25",
                "p75",
                "p90",
                "positive_rate",
                "mean_cross_sectional_dispersion",
                "unique_dates",
                "stock_observations",
            ],
            "predictability": [
                "daily_market_date_spearman_ic",
                "mean_ic",
                "median_ic",
                "ic_stddev",
                "positive_ic_fraction",
                "icir",
                "signal_date_cluster_confidence_interval",
            ],
            "quantiles": [1, 2, 3, 4, 5],
            "monotonicity": [
                "spearman_quantile_number_vs_quantile_mean",
                "adjacent_quantile_inversions",
                "extreme_vs_middle_consistency",
            ],
            "walk_forward": [
                "mean_ic",
                "q5_minus_q1",
                "target_direction",
                "sample_size",
                "unique_dates",
                "edge_to_friction_ratio",
            ],
            "independence": [
                "stock_observations",
                "unique_signal_dates",
                "date_clusters",
                "non_overlap_observations",
            ],
            "concentration": [
                "top_observation_share",
                "top_five_observation_share",
                "top_symbol",
                "top_three_symbols",
                "top_date",
                "top_five_dates",
            ],
        },
        "thresholds": {
            "minimum_participants_per_market_date": 10,
            "minimum_family_observations": 200,
            "minimum_unique_signal_dates_for_promising": 40,
            "minimum_date_clusters_for_promising": 40,
            "minimum_block_dates_for_directional_stability": 20,
            "minimum_predictive_abs_mean_ic": 0.02,
            "minimum_monotone_adjacent_steps": 3,
            "maximum_adjacent_quantile_inversions": 1,
            "minimum_stable_blocks": 4,
            "required_stability_blocks": 5,
            "maximum_market_component_share_pct": 80.0,
            "minimum_market_adjusted_effect_pct": 0.10,
            "maximum_top_five_date_contribution_share_pct": 50.0,
            "maximum_top_three_symbol_contribution_share_pct": 50.0,
            "selection_adjusted_empirical_p_max": 0.05,
            "minimum_economic_edge_to_friction_ratio": 1.0,
            "minimum_distinct_predictive_surfaces": 3,
            "minimum_distinct_target_types": 2,
            "minimum_distinct_horizons": 2,
        },
        "null_models": {
            "random_generator": {
                "library": "numpy",
                "version": np.__version__,
                "bit_generator": "PCG64",
                "reproducibility_scope": "fixed NumPy version, config, inputs, and seed; Generator bit streams can change between versions",
            },
            "cross_sectional": {
                "method": "within-market-date permutation of factor values across eligible symbols",
                "permutations": CROSS_SECTIONAL_PERMUTATIONS,
                "seed": RANDOM_SEED,
                "statistic": "maximum absolute mean Spearman IC across the complete family surface",
                "surfaces": ["factor_variants", "orientations", "targets", "horizons", "markets"],
                "empirical_p": "(1 + null_maxima_ge_observed) / (permutations + 1)",
            },
            "time_dislocation": {
                "method": "circular shift of factor history within symbol",
                "permutations": TIME_DISLOCATION_PERMUTATIONS,
                "seed": RANDOM_SEED,
                "minimum_shift_sessions": 60,
                "factor": "raw_trailing_return_5d",
                "target": "ABSOLUTE_RETURN",
                "horizon_sessions": 5,
                "market_scope": "ALL",
                "role": "pre-registered secondary temporal-alignment diagnostic; cross-sectional family max null is primary",
            },
            "negative_control": {
                "method": "stable SHA-256 pseudo-rank(symbol, signal_date, seed)",
                "seed": RANDOM_SEED,
                "pipeline_passes_allowed": 0,
            },
        },
        "cluster_inference": {
            "cluster": "signal_date; preserve all stocks and market subgroups for a sampled date",
            "bootstrap_repetitions": CLUSTER_BOOTSTRAP_REPETITIONS,
            "seed": RANDOM_SEED,
            "never_resample_stock_rows_independently": True,
        },
        "walk_forward_blocks": [dict(block) for block in WALK_FORWARD_BLOCKS],
        "historical_challenge_block": {
            "start": PHASE13_CONFIRMATION[0].isoformat(),
            "end": PHASE13_CONFIRMATION[1].isoformat(),
            "role": "HISTORICAL_CHALLENGE_SET",
            "included_in_phase14_factor_inputs": False,
        },
        "factor_families": sorted({spec.family_id for spec in specs}),
        "factor_definition_count": len(specs),
        "direction_hypothesis_count": direction_hypotheses,
        "targets": list(TARGETS),
        "horizons_sessions": list(HORIZONS),
        "market_scopes": list(MARKET_SCOPES),
        "quantile_states": [1, 2, 3, 4, 5],
        "factor_registry_factor_count": registry["factor_count"],
        "cost_assumptions": {
            "conservative_research_round_trip_pct": EXPECTED_COST_PCT,
            "status": "historical comparison baseline; not rewritten",
            "fee_each_side_pct": 0.015,
            "sell_tax_equivalent_pct": 0.20,
            "slippage_each_side_pct": 0.15,
        },
        "viability_logic": {
            "allowed_final_states": ["FAIL", "WEAK", "PROMISING_FOR_FUTURE_STUDY"],
            "promising_requires_all": [
                "predictive structure across multiple targets and horizons",
                "at least three directionally coherent factor/target/horizon surfaces",
                "broad quantile monotonicity rather than one isolated state",
                "at least four of five predeclared blocks with the same direction",
                "selection-aware family max empirical p at or below 0.05",
                "absolute long-only scale at least 0.53% gross",
                "positive, non-trivial simple-excess and beta-residual structure",
                "market component share at or below 80%",
                "at least forty signal-date clusters",
                "top-five-date and top-three-symbol contribution shares at or below 50%",
            ],
            "conditioning_only_families_cannot_be_promising": True,
            "survivorship_and_price_vintage_limitations_remain_visible": True,
            "no_strategy_synthesis": True,
        },
        "analysis_date_window": {
            "start": SAFE_PANEL_START.isoformat(),
            "end": SAFE_PANEL_END.isoformat(),
        },
        "excluded_periods": [
            {
                "start": EXTERNAL_2026[0].isoformat(),
                "end": EXTERNAL_2026[1].isoformat(),
                "status": "UNREAD_EXTERNAL",
            },
            {
                "start": HOLDOUT_2026[0].isoformat(),
                "end": HOLDOUT_2026[1].isoformat(),
                "status": "OUTCOME_UNREAD_METADATA_EXPOSED",
            },
        ],
    }


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def write_static_phase14_artifacts(output_root: Path) -> dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=False)
    artifacts = {
        "research-ledger.json": historical_research_ledger(),
        "evidence-periods.json": evidence_periods(),
        "factor-registry.json": registry_payload(),
    }
    for name, value in artifacts.items():
        (output_root / name).write_bytes(json_bytes(value))
    registry_sha = hashlib.sha256((output_root / "factor-registry.json").read_bytes()).hexdigest()
    dof = {
        "artifact": "research-degrees-of-freedom",
        "schema_version": 1,
        "source": "frozen Phase 14 factor registry and pipeline-v2-config",
        "factor_definitions": len(all_factor_specs()),
        "factor_families": len({spec.family_id for spec in all_factor_specs()}),
        "directional_interpretations": sum(
            len(spec.orientation_hypotheses) for spec in all_factor_specs()
        ),
        "horizons": list(HORIZONS),
        "targets": list(TARGETS),
        "market_scopes": list(MARKET_SCOPES),
        "quantile_states": [1, 2, 3, 4, 5],
        "directional_metric_cells": sum(
            len(spec.orientation_hypotheses) for spec in all_factor_specs()
        )
        * len(HORIZONS)
        * len(TARGETS)
        * len(MARKET_SCOPES),
        "quantile_state_cells": len(all_factor_specs())
        * len(HORIZONS)
        * len(TARGETS)
        * len(MARKET_SCOPES)
        * 5,
        "formal_fdr_or_fwer_correction": "NOT_IMPLEMENTED; family max-statistic permutation is primary",
        "registry_sha256": registry_sha,
    }
    (output_root / "research-degrees-of-freedom.json").write_bytes(json_bytes(dof))
    config_path = output_root / "pipeline-v2-config.json"
    config_path.write_bytes(json_bytes(default_config()))
    config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
    (output_root / "pipeline-v2-config.sha256").write_text(
        f"{config_sha}  pipeline-v2-config.json\n", encoding="utf-8"
    )
    return {"factor_registry_sha256": registry_sha, "pipeline_config_sha256": config_sha}


def load_frozen_config(output_root: Path) -> tuple[dict[str, Any], str]:
    config_path = output_root / "pipeline-v2-config.json"
    sha_path = output_root / "pipeline-v2-config.sha256"
    if not config_path.is_file() or not sha_path.is_file():
        raise FileNotFoundError("frozen Phase 14 config and SHA-256 sidecar are required")
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    expected = sha_path.read_text(encoding="utf-8").split()[0]
    if digest != expected:
        raise ValueError("frozen Phase 14 pipeline config SHA-256 mismatch")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return config, digest

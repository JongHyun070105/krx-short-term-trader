from __future__ import annotations

from typing import Any

from krx_trader.research.pipeline.config import HORIZONS, MARKET_SCOPES, TARGETS
from krx_trader.research.pipeline.factor_registry import FactorSpec


def multiple_testing_audit(
    specs: list[FactorSpec],
    null_results: dict[str, dict[str, Any]],
    *,
    historical_ledger: dict[str, Any],
) -> dict[str, Any]:
    family_ids = sorted({spec.family_id for spec in specs})
    orientations = sum(len(spec.orientation_hypotheses) for spec in specs)
    state_variants = sum(spec.source_type == "HISTORICAL_INTERACTION_STATE" for spec in specs)
    quantile_state_cells = len(specs) * 5 * len(HORIZONS) * len(TARGETS) * len(MARKET_SCOPES)
    metric_cells = orientations * len(HORIZONS) * len(TARGETS) * len(MARKET_SCOPES)
    null_family_rows = [
        {
            "family_id": family_id,
            "observed_family_best_statistic": value.get("observed_family_best_statistic"),
            "null_max_p95": value.get("null_max_p95"),
            "empirical_p": value.get("empirical_p"),
            "selection_adjusted_pass": bool(
                value.get("empirical_p") is not None and value["empirical_p"] <= 0.05
            ),
        }
        for family_id, value in sorted(null_results.items())
        if family_id != "negative_control"
    ]
    return {
        "artifact": "multiple-testing-audit",
        "historical_research_counts": historical_ledger["counts"],
        "phase14_search_surface": {
            "factor_definitions": len(specs),
            "factor_families": len(family_ids),
            "directional_interpretations": orientations,
            "horizons": list(HORIZONS),
            "targets": list(TARGETS),
            "market_scopes_including_pooled": list(MARKET_SCOPES),
            "fixed_quantile_states": 5,
            "raw_directional_metric_cells": metric_cells,
            "raw_quantile_state_cells": quantile_state_cells,
            "historical_interaction_state_variants": state_variants,
            "candidate_selection_opportunities": metric_cells + state_variants,
        },
        "effective_family_count": len(family_ids),
        "effective_family_count_definition": "number of frozen semantic factor families; no estimated correlation-based effective count",
        "family_max_statistic_results": null_family_rows,
        "primary_selection_control": "complete within-family max-statistic permutation surface",
        "formal_fdr_or_fwer_method": "NOT_IMPLEMENTED",
        "interpretation": "A selected cell is not reported as if it were the sole historical test.",
    }

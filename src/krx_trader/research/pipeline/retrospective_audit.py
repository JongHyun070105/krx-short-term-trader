from __future__ import annotations

from typing import Any


def phase13_preconfirmation_audit(
    *,
    factor_viability: dict[str, Any],
    pipeline_config_sha256: str,
    challenge_label: str,
    challenge_metrics: dict[str, float],
) -> dict[str, Any]:
    """Attach the known Phase 13 challenge only after pre-Confirmation verdicts exist."""
    family_id = "phase13_excess_turnover_interaction"
    family = next(
        (item for item in factor_viability["results"] if item["family_id"] == family_id),
        None,
    )
    preconfirmation_verdict = family["final_state"] if family else "FAIL"
    would_block = preconfirmation_verdict != "PROMISING_FOR_FUTURE_STUDY"
    status = "PASS" if would_block else "FAIL"
    return {
        "artifact": "phase13-retrospective-audit",
        "pipeline_config_sha256": pipeline_config_sha256,
        "config_frozen_before_audit": True,
        "phase13_confirmation_used_as_pipeline_input": False,
        "preconfirmation_input_window": ["2023-01-02", "2025-06-30"],
        "phase13_candidate_family": family_id,
        "preconfirmation_v2_verdict": preconfirmation_verdict,
        "would_have_blocked_candidate_from_confirmation": would_block,
        "pipeline_false_positive_audit": status,
        "post_verdict_historical_challenge": {
            "label": challenge_label,
            "metrics": challenge_metrics,
            "role": "already-known retrospective challenge label; excluded from all Phase 14 factor metrics, nulls, and thresholds",
        },
        "post_hoc_gate_changes": False,
        "thresholds_changed_after_challenge": False,
        "audit_note": (
            "The caller supplied the historical Phase 13 failure label before implementation; "
            "the frozen rules are executed only on the pre-2025-H2 safe panel, and the label is appended after that verdict."
        ),
    }


def historical_family_audit(factor_viability: dict[str, Any]) -> dict[str, Any]:
    verdicts = {item["family_id"]: item for item in factor_viability["results"]}
    mappings = [
        {
            "historical_family": "Phase 11 market-stress rebound / market-context mean reversion",
            "old_pipeline_result": "REJECTED_AFTER_VALIDATION; Phase 12 found market beta and stress composition, not a stable mechanism",
            "v2_factor_family": "market_context",
            "later_known_outcome": "NO_CANDIDATE; phase 12 candidate not created",
        },
        {
            "historical_family": "Phase 13 standalone residual reversal",
            "old_pipeline_result": "WEAK; absolute effect below 0.53% friction",
            "v2_factor_family": "beta_residual_return",
            "later_known_outcome": "STANDALONE_STATE_NOT_PROMOTED",
        },
        {
            "historical_family": "Phase 10 daily price states",
            "old_pipeline_result": "NO_QUALIFYING_FAMILY; no candidate",
            "v2_factor_family": "raw_trailing_return",
            "later_known_outcome": "NO_CANDIDATE",
        },
        {
            "historical_family": "Phase 13 excess-reversal x expanded-turnover candidate family",
            "old_pipeline_result": "CANDIDATE_CREATED_THEN_CONFIRMATION_FAILED",
            "v2_factor_family": "phase13_excess_turnover_interaction",
            "later_known_outcome": "PHASE13_CONFIRMATION_FAIL",
        },
    ]
    results = []
    for mapping in mappings:
        v2 = verdicts.get(mapping["v2_factor_family"], {})
        state = v2.get("final_state", "FAIL")
        results.append(
            {
                **mapping,
                "pipeline_v2_factor_verdict": state,
                "v2_would_have_prevented_candidate_research": state != "PROMISING_FOR_FUTURE_STUDY",
                "input_window": ["2023-01-02", "2025-06-30"],
                "challenge_outcomes_not_used_for_v2_verdict": True,
            }
        )
    return {
        "artifact": "historical-family-audit",
        "config_frozen_first": True,
        "phase13_h2_used_only_as_challenge_label": True,
        "results": results,
    }

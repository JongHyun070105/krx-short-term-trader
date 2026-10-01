from __future__ import annotations

from datetime import date
from typing import Any

DISCOVERY = (date(2023, 1, 2), date(2024, 6, 28))
TOUCHED_REPLICATION = (date(2024, 7, 1), date(2025, 6, 30))
PHASE13_CONFIRMATION = (date(2025, 7, 1), date(2025, 12, 30))
EXTERNAL_2026 = (date(2026, 1, 5), date(2026, 4, 16))
PHASE10_DEVELOPMENT_2026 = (date(2026, 4, 17), date(2026, 6, 30))
HOLDOUT_2026 = (date(2026, 7, 28), date(2026, 8, 28))
SAFE_PANEL_START = DISCOVERY[0]
SAFE_PANEL_END = TOUCHED_REPLICATION[1]


class ProtectedEvidenceError(ValueError):
    """Raised when a protected or out-of-scope evidence row reaches analysis."""


def classify_period(value: date | str) -> str:
    day = date.fromisoformat(value) if isinstance(value, str) else value
    for interval, name in (
        (DISCOVERY, "HEAVILY_TOUCHED_DISCOVERY"),
        (TOUCHED_REPLICATION, "HEAVILY_TOUCHED_REPLICATION"),
        (PHASE13_CONFIRMATION, "CONSUMED_BY_PHASE13_CONFIRMATION"),
        (EXTERNAL_2026, "UNREAD_EXTERNAL"),
        (PHASE10_DEVELOPMENT_2026, "TOUCHED_PHASE10_DEVELOPMENT_EXCLUDED_FROM_PHASE14"),
        (HOLDOUT_2026, "OUTCOME_UNREAD_METADATA_EXPOSED"),
    ):
        if interval[0] <= day <= interval[1]:
            return name
    if day > HOLDOUT_2026[1]:
        return "PROSPECTIVE_UNREAD"
    return "OUTSIDE_CLASSIFIED_PERIOD"


def assert_phase14_evidence_date(value: date | str) -> None:
    day = date.fromisoformat(value) if isinstance(value, str) else value
    if not SAFE_PANEL_START <= day <= SAFE_PANEL_END:
        period = classify_period(day)
        raise ProtectedEvidenceError(
            f"Phase 14 analysis accepts only {SAFE_PANEL_START} through {SAFE_PANEL_END}; "
            f"received {day} ({period})"
        )


HISTORICAL_STRATEGY_FAMILIES = [
    "Breakout",
    "Pullback",
    "Retest",
    "Relative Strength Continuation",
    "Mean Reversion",
    "VWAP Reclaim",
    "Opening Gap",
    "Market Stress Rebound",
    "Residual / Excess candidate family",
]

HISTORICAL_FACTOR_FAMILIES = [
    "Raw trailing return",
    "Market excess return",
    "Beta residual return",
    "Residual drawdown",
    "Idiosyncratic volatility",
    "Abnormal traded-value activity",
    "Residual cross-sectional rank",
    "Range position",
    "Market context",
    "Breadth",
    "Cross-sectional dispersion",
]

HISTORICAL_INTERACTION_FAMILIES = [
    "Price return x liquidity / turnover (Phase 11)",
    "Excess return x abnormal turnover (Phase 13)",
    "Residual drawdown x idiosyncratic volatility (Phase 13)",
    "Residual rank x abnormal turnover (Phase 13)",
]


def historical_research_ledger() -> dict[str, Any]:
    periods = [
        {"start": "2023-01-02", "end": "2024-06-28", "classification": "HEAVILY_TOUCHED_DISCOVERY"},
        {
            "start": "2024-07-01",
            "end": "2025-06-30",
            "classification": "HEAVILY_TOUCHED_REPLICATION",
        },
        {
            "start": "2025-07-01",
            "end": "2025-12-30",
            "classification": "CONSUMED_BY_PHASE13_CONFIRMATION",
        },
        {"start": "2026-01-05", "end": "2026-04-16", "classification": "UNREAD_EXTERNAL"},
        {
            "start": "2026-04-17",
            "end": "2026-06-30",
            "classification": "TOUCHED_PHASE10_DEVELOPMENT_EXCLUDED_FROM_PHASE14",
        },
        {
            "start": "2026-07-28",
            "end": "2026-08-28",
            "classification": "OUTCOME_UNREAD_METADATA_EXPOSED",
        },
    ]
    return {
        "artifact": "phase14-research-ledger",
        "schema_version": 1,
        "history_is_cumulative": True,
        "family_count_basis": "minimum explicitly named headline families in STATUS, RESULTS, and Phase 10-13 research docs",
        "strategy_families_examined": HISTORICAL_STRATEGY_FAMILIES,
        "factor_families_examined": HISTORICAL_FACTOR_FAMILIES,
        "interaction_families_examined": HISTORICAL_INTERACTION_FAMILIES,
        "counts": {
            "strategy_families_minimum": len(HISTORICAL_STRATEGY_FAMILIES),
            "factor_families_minimum": len(HISTORICAL_FACTOR_FAMILIES),
            "interaction_families_minimum": len(HISTORICAL_INTERACTION_FAMILIES),
            "combined_named_family_labels_minimum": len(HISTORICAL_STRATEGY_FAMILIES)
            + len(HISTORICAL_FACTOR_FAMILIES)
            + len(HISTORICAL_INTERACTION_FAMILIES),
            "directional_interpretations_minimum": 2,
            "historical_market_splits": 2,
            "historical_forward_horizons_sessions_minimum": [1, 2, 3, 5, 10, 20],
            "candidate_rules_explicitly_staged_in_Phase11_and_Phase13": 2,
            "candidate_rule_count_completeness": "LOWER_BOUND; early exploratory variants are not exhaustively enumerable from final records",
            "independent_historical_periods_remaining": 0,
            "unread_external_blocks_available": 1,
            "metadata_exposed_non_pristine_blocks": 1,
        },
        "datasets_touched": [
            "Phase 3-9 intraday OHLCV research/cache artifacts",
            "Phase 10 daily price/opportunity feature and outcome artifacts",
            "Phase 11 daily feature, outcome, flow/liquidity and market-context artifacts",
            "Phase 12 bounded market-stress, breadth, dispersion, volatility and beta artifacts",
            "Phase 13 safe factor panel, factor maps, interactions, confirmation and audit artifacts",
        ],
        "historical_periods": periods,
        "phase14_computation_input": "runtime/research/phase13/phase13-safe-factor-panel.jsonl.gz; source clips the primary panel at 2025-06-30",
        "phase14_computation_exclusions": [
            "2025-07-01 through 2025-12-30 confirmation observations are not pipeline inputs",
            "2026 External period is not read",
            "2026-04-17 through 2026-06-30 Phase 10 Development files are not used because they are outside the common Phase 13 factor panel",
            "2026-07-28 through 2026-08-28 prices, metadata contents, factors, signals, and outcomes are not read",
        ],
        "prospective_period_policy": "No PROSPECTIVE_UNREAD period is explicitly specified in the inspected Phase 10-13 policy artifacts.",
        "survivorship_limitations": {
            "point_in_time_universe": "PARTIAL",
            "point_in_time_price_history": "NOT_AVAILABLE",
            "survivorship_sensitivity": "NOT_TESTABLE",
            "adjusted_price_revision_risk": "PRESENT; KIS adjusted history has no historical-vintage selector",
        },
        "historical_candidate_count_note": "Phase 11 and Phase 13 one-shot candidate rules are confirmed from the final records; this is a documented lower bound, not a claim that earlier exploratory variants were absent.",
    }


def evidence_periods() -> dict[str, Any]:
    return {
        "artifact": "phase14-evidence-periods",
        "schema_version": 1,
        "periods": historical_research_ledger()["historical_periods"],
        "analysis_window": {
            "start": SAFE_PANEL_START.isoformat(),
            "end": SAFE_PANEL_END.isoformat(),
        },
        "confirmation_is_challenge_only": True,
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
    }

from __future__ import annotations

from typing import Any

from krx_trader.research.pipeline.config import EXPECTED_COST_PCT
from krx_trader.research.pipeline.factor_registry import FactorSpec
from krx_trader.research.pipeline.panel import PanelRecord
from krx_trader.research.pipeline.walk_forward import quantile_effect


def economic_feasibility(
    records: list[PanelRecord],
    selected_surfaces: dict[str, dict[str, Any]],
    specs_by_id: dict[str, FactorSpec],
    factor_values: dict[str, list[float | None]],
    *,
    friction_pct: float = EXPECTED_COST_PCT,
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
        gross_edge = effect["selected_long_only_gross_mean_pct"]
        signed_ratio = gross_edge / friction_pct if gross_edge is not None else None
        results.append(
            {
                "family_id": family_id,
                "factor_id": surface["factor_id"],
                "target_horizon_sessions": surface["horizon_sessions"],
                "market_scope": surface["market_scope"],
                "orientation": surface["direction"],
                "selected_long_only_quantile": effect["selected_long_only_quantile"],
                "gross_effect_scale_pct": gross_edge,
                "absolute_long_only_effect_pct": gross_edge,
                "expected_friction_pct": friction_pct,
                "edge_to_friction_ratio": signed_ratio,
                "break_even_friction_pct": gross_edge,
                "gross_above_friction": bool(gross_edge is not None and gross_edge >= friction_pct),
                "economic_scale_grade": (
                    "PASS"
                    if signed_ratio is not None and signed_ratio >= 1.0
                    else "FAIL"
                    if signed_ratio is not None
                    else "UNKNOWN"
                ),
                "observations": effect["selected_observations"],
                "unique_dates": effect["selected_unique_dates"],
                "non_overlap_observations": effect["selected_non_overlap_observations"],
                "cost_assumption_components": {
                    "fee_each_side_pct": {"value": 0.015, "status": "ASSUMED_HISTORICAL_BASELINE"},
                    "sell_tax_equivalent_pct": {
                        "value": 0.20,
                        "status": "HISTORICALLY_DOCUMENTED_COMPONENT",
                    },
                    "slippage_each_side_pct": {"value": 0.15, "status": "CONSERVATIVE_ASSUMPTION"},
                },
                "caveat": "Factor-level screen only; does not define a trade, entry, exit, sizing, or net strategy return.",
            }
        )
    return {
        "artifact": "economic-feasibility",
        "round_trip_cost_pct": friction_pct,
        "interpretation": {
            "ratio_below_1": "generally economically weak",
            "ratio_1_to_2": "requires strong evidence",
            "ratio_above_2": "economically interesting screening scale only",
        },
        "results": results,
    }

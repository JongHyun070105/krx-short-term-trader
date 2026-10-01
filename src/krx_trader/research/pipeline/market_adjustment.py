from __future__ import annotations

from typing import Any

from krx_trader.research.pipeline.config import EXPECTED_COST_PCT
from krx_trader.research.pipeline.factor_registry import FactorSpec
from krx_trader.research.pipeline.panel import PanelRecord
from krx_trader.research.pipeline.walk_forward import quantile_effect


def _oriented(value: float | None, direction: str) -> float | None:
    if value is None:
        return None
    return value if direction == "positive" else -value


def market_adjusted_edges(
    records: list[PanelRecord],
    selected_surfaces: dict[str, dict[str, Any]],
    specs_by_id: dict[str, FactorSpec],
    factor_values: dict[str, list[float | None]],
    *,
    minimum_effect_pct: float = 0.10,
    market_share_warning_pct: float = 80.0,
    friction_pct: float = EXPECTED_COST_PCT,
) -> dict[str, Any]:
    results = []
    for family_id, surface in sorted(selected_surfaces.items()):
        spec = specs_by_id[surface["factor_id"]]
        args = {
            "records": records,
            "spec": spec,
            "factor_values": factor_values,
            "horizon": surface["horizon_sessions"],
            "market_scope": surface["market_scope"],
            "direction": surface["direction"],
        }
        absolute = quantile_effect(target="ABSOLUTE_RETURN", **args)
        market = quantile_effect(target="MATCHED_MARKET_COMPONENT", **args)
        excess = quantile_effect(target="SIMPLE_EXCESS_RETURN", **args)
        residual = quantile_effect(target="BETA_RESIDUAL_RETURN", **args)
        gross_mean = absolute["selected_quantile_target_mean_pct"]
        market_mean = market["selected_quantile_target_mean_pct"]
        share = None
        if gross_mean is not None and abs(gross_mean) >= 0.10 and market_mean is not None:
            share = market_mean / gross_mean * 100
        beta_dominated = (
            "YES"
            if share is not None
            and abs(share) >= market_share_warning_pct
            and gross_mean is not None
            and market_mean is not None
            and gross_mean * market_mean > 0
            else "UNCERTAIN"
            if share is None
            else "NO"
        )
        excess_spread = _oriented(excess["q5_minus_q1_pct"], surface["direction"])
        residual_spread = _oriented(residual["q5_minus_q1_pct"], surface["direction"])
        results.append(
            {
                "family_id": family_id,
                "factor_id": surface["factor_id"],
                "horizon_sessions": surface["horizon_sessions"],
                "market_scope": surface["market_scope"],
                "orientation": surface["direction"],
                "absolute_long_only_mean_pct": gross_mean,
                "matched_market_component_pct": market_mean,
                "market_component_share_pct": share,
                "market_component_share_definition": "mean matched-index return / selected-quantile mean absolute stock return",
                "market_beta_dominated": beta_dominated,
                "simple_excess_q5_minus_q1_pct": excess["q5_minus_q1_pct"],
                "oriented_simple_excess_spread_pct": excess_spread,
                "beta_residual_q5_minus_q1_pct": residual["q5_minus_q1_pct"],
                "oriented_beta_residual_spread_pct": residual_spread,
                "simple_excess_nontrivial": bool(
                    excess_spread is not None and excess_spread >= minimum_effect_pct
                ),
                "beta_residual_nontrivial": bool(
                    residual_spread is not None and residual_spread >= minimum_effect_pct
                ),
                "market_adjusted_edge_grade": (
                    "PASS"
                    if excess_spread is not None
                    and residual_spread is not None
                    and excess_spread >= minimum_effect_pct
                    and residual_spread >= minimum_effect_pct
                    and beta_dominated == "NO"
                    else "FAIL"
                    if excess_spread is not None and residual_spread is not None
                    else "UNKNOWN"
                ),
                "expected_friction_pct": friction_pct,
                "market_split_directions": {},
            }
        )
        row = results[-1]
        split_values = {}
        for market_scope in ("KOSPI", "KOSDAQ"):
            scope_args = {**args, "market_scope": market_scope}
            split_excess = quantile_effect(target="SIMPLE_EXCESS_RETURN", **scope_args)[
                "q5_minus_q1_pct"
            ]
            split_residual = quantile_effect(target="BETA_RESIDUAL_RETURN", **scope_args)[
                "q5_minus_q1_pct"
            ]
            split_values[market_scope] = {
                "oriented_simple_excess_spread_pct": _oriented(split_excess, surface["direction"]),
                "oriented_beta_residual_spread_pct": _oriented(
                    split_residual, surface["direction"]
                ),
            }
        row["market_split_directions"] = split_values
        non_null_splits = [
            item
            for item in split_values.values()
            if item["oriented_simple_excess_spread_pct"] is not None
            and item["oriented_beta_residual_spread_pct"] is not None
        ]
        row["both_markets_same_oriented_direction"] = bool(
            len(non_null_splits) == 2
            and all(
                item["oriented_simple_excess_spread_pct"] > 0
                and item["oriented_beta_residual_spread_pct"] > 0
                for item in non_null_splits
            )
        )
    return {
        "artifact": "market-adjusted-edge",
        "market_component_share_warning_pct": market_share_warning_pct,
        "minimum_market_adjusted_effect_pct": minimum_effect_pct,
        "results": results,
    }

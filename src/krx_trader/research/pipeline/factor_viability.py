from __future__ import annotations

from typing import Any

from krx_trader.research.pipeline.config import default_config
from krx_trader.research.pipeline.factor_registry import FactorSpec


def _match(
    rows: list[dict[str, Any]],
    *,
    factor_id: str,
    target: str,
    horizon: int,
    market_scope: str,
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in rows
            if row.get("factor_id") == factor_id
            and row.get("target") == target
            and row.get("horizon_sessions") == horizon
            and row.get("market_scope") == market_scope
        ),
        None,
    )


def _grade(value: bool | None) -> str:
    if value is None:
        return "UNKNOWN"
    return "PASS" if value else "FAIL"


def evaluate_factor_viability(
    *,
    specs: list[FactorSpec],
    null_results: dict[str, dict[str, Any]],
    predictability: dict[str, Any],
    quantiles: dict[str, Any],
    monotonicity: dict[str, Any],
    walk_forward: dict[str, Any],
    economics: dict[str, Any],
    market_edges: dict[str, Any],
    concentration: dict[str, Any],
    family_bootstrap: dict[str, Any],
    factor_coverage: dict[str, dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frozen = config or default_config()
    thresholds = frozen["thresholds"]
    specs_by_id = {spec.factor_id: spec for spec in specs}
    predictability_rows = predictability["results"]
    quantile_rows = quantiles["results"]
    monotonicity_rows = monotonicity["results"]
    wf_rows = {row["family_id"]: row for row in walk_forward["results"]}
    economic_rows = {row["family_id"]: row for row in economics["results"]}
    market_rows = {row["family_id"]: row for row in market_edges["results"]}
    concentration_rows = {row["family_id"]: row for row in concentration["results"]}
    bootstrap_rows = {row["family_id"]: row for row in family_bootstrap["results"]}
    grouped_specs: dict[str, list[FactorSpec]] = {}
    for spec in specs:
        grouped_specs.setdefault(spec.family_id, []).append(spec)
    results = []
    for family_id, members in sorted(grouped_specs.items()):
        null_result = null_results.get(family_id, {})
        selected = null_result.get("observed_best_surface") or {}
        factor_id = selected.get("factor_id")
        if factor_id not in specs_by_id:
            results.append(
                {
                    "family_id": family_id,
                    "dimensions": {
                        "DATA_QUALITY": "PARTIAL"
                        if any(
                            factor_coverage.get(member.factor_id, {}).get("non_null_rate", 0) >= 0.5
                            for member in members
                        )
                        else "FAIL",
                        "PREDICTABILITY": "FAIL",
                        "MONOTONICITY": "FAIL",
                        "WALK_FORWARD_STABILITY": "FAIL",
                        "NULL_SEPARATION": "FAIL",
                        "MULTIPLE_TESTING_ROBUSTNESS": "FAIL",
                        "ECONOMIC_SCALE": "UNKNOWN",
                        "MARKET_ADJUSTED_EDGE": "UNKNOWN",
                        "INDEPENDENT_SAMPLE_DEPTH": "FAIL",
                        "CONCENTRATION": "UNKNOWN",
                        "SURVIVORSHIP_RISK": "HIGH",
                    },
                    "selected_surface": None,
                    "final_state": "FAIL",
                    "reason": (
                        "No eligible common market-date sample for the frozen family surface; "
                        "the family fails closed for insufficient data."
                    ),
                    "data_coverage": {
                        member.factor_id: factor_coverage.get(member.factor_id)
                        for member in members
                    },
                    "survivorship_and_vintage_warning": (
                        "CURRENT_LISTING_BIASED; PIT_PRICE_HISTORY_NOT_AVAILABLE; "
                        "ADJUSTED_PRICE_REVISION_RISK_PRESENT"
                    ),
                    "strategy_synthesis": "NOT_PERFORMED",
                }
            )
            continue
        selected_target = selected["target"]
        selected_horizon = int(selected["horizon_sessions"])
        selected_scope = selected["market_scope"]
        direction = selected["direction"]
        selected_spec = specs_by_id[factor_id]
        ic_row = _match(
            predictability_rows,
            factor_id=factor_id,
            target=selected_target,
            horizon=selected_horizon,
            market_scope=selected_scope,
        )
        q_row = _match(
            quantile_rows,
            factor_id=factor_id,
            target=selected_target,
            horizon=selected_horizon,
            market_scope=selected_scope,
        )
        mono_row = _match(
            monotonicity_rows,
            factor_id=factor_id,
            target=selected_target,
            horizon=selected_horizon,
            market_scope=selected_scope,
        )
        wf = wf_rows.get(family_id, {})
        stability = wf.get("stability_components", {})
        stable_blocks = wf.get("stable_blocks", 0)
        p_value = null_result.get("empirical_p")
        economic = economic_rows.get(family_id, {})
        market = market_rows.get(family_id, {})
        concentration_row = concentration_rows.get(family_id, {})
        date_count = int(concentration_row.get("unique_signal_dates") or 0)
        cluster_count = int(concentration_row.get("date_clusters") or 0)
        ic_mean = ic_row.get("mean_ic") if ic_row else None
        oriented_ic = (
            (ic_mean if direction == "positive" else -ic_mean) if ic_mean is not None else None
        )
        ci = ic_row.get("signal_date_cluster_ci90") if ic_row else None
        oriented_ci_lower = None
        if ci is not None:
            oriented_ci_lower = ci[0] if direction == "positive" else -ci[1]
        null_pass = (
            p_value is not None and p_value <= thresholds["selection_adjusted_empirical_p_max"]
        )
        ci_pass = oriented_ci_lower is not None and oriented_ci_lower > 0
        predictability_pass = (
            oriented_ic is not None
            and oriented_ic >= thresholds["minimum_predictive_abs_mean_ic"]
            and null_pass
            and ci_pass
        )
        spread = q_row.get("q5_minus_q1_pct") if q_row else None
        oriented_spread = (
            spread if direction == "positive" else -spread if spread is not None else None
        )
        inversion_key = (
            "adjacent_inversions_positive"
            if direction == "positive"
            else "adjacent_inversions_negative"
        )
        inversions = mono_row.get(inversion_key) if mono_row else None
        monotone = (
            mono_row is not None
            and mono_row.get("distinct_factor_levels_max", 0) >= 5
            and inversions is not None
            and 4 - inversions >= thresholds["minimum_monotone_adjacent_steps"]
            and inversions <= thresholds["maximum_adjacent_quantile_inversions"]
            and mono_row.get(
                "extreme_vs_middle_consistency_positive"
                if direction == "positive"
                else "extreme_vs_middle_consistency_negative"
            )
            is True
        )
        stability_pass = stable_blocks >= thresholds["minimum_stable_blocks"]
        economic_pass = (
            economic.get("edge_to_friction_ratio") is not None
            and economic["edge_to_friction_ratio"]
            >= thresholds["minimum_economic_edge_to_friction_ratio"]
        )
        market_pass = market.get("market_adjusted_edge_grade") == "PASS"
        sample_pass = (
            date_count >= thresholds["minimum_unique_signal_dates_for_promising"]
            and cluster_count >= thresholds["minimum_date_clusters_for_promising"]
        )
        top_date_share = concentration_row.get("top_five_dates_share_pct")
        top_symbol_share = concentration_row.get("top_three_symbols_share_pct")
        concentration_pass = (
            top_date_share is not None
            and top_symbol_share is not None
            and top_date_share <= thresholds["maximum_top_five_date_contribution_share_pct"]
            and top_symbol_share <= thresholds["maximum_top_three_symbol_contribution_share_pct"]
        )
        supporting = []
        for candidate in members:
            for target in frozen["targets"]:
                for horizon in frozen["horizons_sessions"]:
                    item = _match(
                        predictability_rows,
                        factor_id=candidate.factor_id,
                        target=target,
                        horizon=horizon,
                        market_scope=selected_scope,
                    )
                    if item is None:
                        continue
                    ic = item["mean_ic"]
                    oriented = ic if direction == "positive" else -ic
                    if oriented >= thresholds["minimum_predictive_abs_mean_ic"]:
                        supporting.append((candidate.factor_id, target, horizon))
        supporting_targets = {item[1] for item in supporting}
        supporting_horizons = {item[2] for item in supporting}
        surface_breadth_pass = (
            len(supporting) >= thresholds["minimum_distinct_predictive_surfaces"]
            and len(supporting_targets) >= thresholds["minimum_distinct_target_types"]
            and len(supporting_horizons) >= thresholds["minimum_distinct_horizons"]
        )
        conditioning_only = selected_spec.semantic_family == "conditioning_only"
        data_grade = (
            "PARTIAL"
            if factor_coverage.get(factor_id, {}).get("non_null_rate", 0) >= 0.5
            else "FAIL"
        )
        dimensions = {
            "DATA_QUALITY": data_grade,
            "PREDICTABILITY": _grade(predictability_pass),
            "MONOTONICITY": _grade(monotone),
            "WALK_FORWARD_STABILITY": _grade(stability_pass),
            "NULL_SEPARATION": _grade(null_pass),
            "MULTIPLE_TESTING_ROBUSTNESS": _grade(null_pass),
            "ECONOMIC_SCALE": _grade(economic_pass),
            "MARKET_ADJUSTED_EDGE": _grade(market_pass),
            "INDEPENDENT_SAMPLE_DEPTH": _grade(sample_pass),
            "CONCENTRATION": _grade(concentration_pass),
            "SURVIVORSHIP_RISK": "HIGH",
        }
        gates_pass = all(
            (
                predictability_pass,
                monotone,
                stability_pass,
                null_pass,
                economic_pass,
                market_pass,
                sample_pass,
                concentration_pass,
                surface_breadth_pass,
                not conditioning_only,
            )
        )
        structured = bool(
            oriented_ic is not None and oriented_ic >= thresholds["minimum_predictive_abs_mean_ic"]
        )
        state = "PROMISING_FOR_FUTURE_STUDY" if gates_pass else "WEAK" if structured else "FAIL"
        results.append(
            {
                "family_id": family_id,
                "selected_surface": selected,
                "selected_quantile_spread_pct": spread,
                "oriented_quantile_spread_pct": oriented_spread,
                "supporting_surface_count": len(supporting),
                "supporting_target_count": len(supporting_targets),
                "supporting_horizon_count": len(supporting_horizons),
                "surface_breadth_pass": surface_breadth_pass,
                "conditioning_only": conditioning_only,
                "dimensions": dimensions,
                "stability_components": stability,
                "null_empirical_p": p_value,
                "economic_edge_to_friction_ratio": economic.get("edge_to_friction_ratio"),
                "market_component_share_pct": market.get("market_component_share_pct"),
                "market_beta_dominated": market.get("market_beta_dominated"),
                "unique_signal_dates": date_count,
                "date_clusters": cluster_count,
                "concentration": {
                    "top_five_dates_share_pct": top_date_share,
                    "top_three_symbols_share_pct": top_symbol_share,
                },
                "data_coverage": factor_coverage.get(factor_id),
                "survivorship_and_vintage_warning": "CURRENT_LISTING_BIASED; PIT_PRICE_HISTORY_NOT_AVAILABLE; ADJUSTED_PRICE_REVISION_RISK_PRESENT",
                "cluster_bootstrap": bootstrap_rows.get(family_id, {}).get(
                    "target_date_cluster_bootstrap"
                ),
                "final_state": state,
                "strategy_synthesis": "NOT_PERFORMED",
            }
        )
    best = next(
        (
            item["family_id"]
            for item in results
            if item["final_state"] == "PROMISING_FOR_FUTURE_STUDY"
        ),
        None,
    )
    structured_any = any(
        item.get("dimensions", {}).get("PREDICTABILITY") == "PASS"
        or item.get("dimensions", {}).get("NULL_SEPARATION") == "PASS"
        for item in results
    )
    return {
        "artifact": "factor-viability",
        "allowed_states": ["FAIL", "WEAK", "PROMISING_FOR_FUTURE_STUDY"],
        "best_factor_family": best,
        "best_factor_viability": next(
            (item["final_state"] for item in results if item["family_id"] == best), "NONE"
        )
        if best
        else "NONE",
        "existing_factor_information": "SUFFICIENT"
        if best
        else "WEAK"
        if structured_any
        else "INSUFFICIENT",
        "next_research_mode": ("STRATEGY_SYNTHESIS" if best else "NEW_INFORMATION_SOURCE"),
        "pipeline_calibration": "PASS"
        if all(
            item["final_state"] != "PROMISING_FOR_FUTURE_STUDY"
            for item in results
            if item["family_id"] == "negative_control"
        )
        else "FAIL",
        "results": results,
    }

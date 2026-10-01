from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class FactorSpec:
    factor_id: str
    family_id: str
    semantic_family: str
    orientation_hypotheses: tuple[str, ...]
    source: str
    lookback: str
    field: str | None
    required_fields: tuple[str, ...]
    limitation: str
    source_type: str = "HISTORICAL_FACTOR"
    left_field: str | None = None
    right_field: str | None = None
    left_state: str | None = None
    right_state: str | None = None
    candidate_reference: bool = False


def _spec(
    factor_id: str,
    family_id: str,
    semantic_family: str,
    directions: tuple[str, ...],
    field: str,
    lookback: str,
    limitation: str,
    *,
    source: str = "src/krx_trader/research/phase13.py:build_phase13_features",
) -> FactorSpec:
    return FactorSpec(
        factor_id=factor_id,
        family_id=family_id,
        semantic_family=semantic_family,
        orientation_hypotheses=directions,
        source=source,
        lookback=lookback,
        field=field,
        required_fields=(field, "date", "symbol", "market"),
        limitation=limitation,
    )


def base_factor_specs() -> list[FactorSpec]:
    specs: list[FactorSpec] = []
    panel_limit = "Current-listing cohort; adjusted-price vintage revisions are not controlled."
    for horizon in (1, 3, 5, 10, 20):
        specs.append(
            _spec(
                f"raw_trailing_return_{horizon}d",
                "raw_trailing_return",
                "momentum",
                ("positive",),
                f"stock_return_{horizon}d_pct",
                f"{horizon} sessions",
                panel_limit,
            )
        )
    for horizon in (1, 3, 5, 10, 20):
        specs.append(
            _spec(
                f"market_excess_return_{horizon}d",
                "market_excess_return",
                "momentum_and_reversal",
                ("positive", "negative"),
                f"excess_return_{horizon}d_pct",
                f"{horizon} sessions",
                panel_limit + " Both historical momentum and reversal interpretations are counted.",
            )
        )
    for horizon in (5, 20):
        specs.append(
            _spec(
                f"beta_residual_return_{horizon}d",
                "beta_residual_return",
                "residual_momentum_and_reversal",
                ("positive", "negative"),
                f"residual_return_{horizon}d_pct",
                f"{horizon} sessions",
                panel_limit + " Phase 13 residual is a diagnostic, not formal asset-pricing alpha.",
            )
        )
    for horizon in (20, 60):
        specs.append(
            _spec(
                f"residual_drawdown_{horizon}d",
                "residual_drawdown",
                "drawdown_recovery",
                ("negative",),
                f"residual_drawdown_{horizon}d_pct",
                f"{horizon} sessions",
                panel_limit
                + " Negative orientation preserves the historical recovery interpretation.",
            )
        )
    specs.extend(
        [
            _spec(
                "idiosyncratic_volatility_20d",
                "idiosyncratic_volatility",
                "two_sided",
                ("positive", "negative"),
                "idio_volatility_20d_pct",
                "20 sessions",
                panel_limit
                + " The feature uses the historical Phase 13 rolling beta residual definition.",
            ),
            _spec(
                "abnormal_traded_value_activity",
                "abnormal_traded_value_activity",
                "two_sided",
                ("positive", "negative"),
                "turnover_ratio_5d_vs_prior20_median",
                "5 sessions vs prior 20-session median",
                panel_limit
                + " Adjusted-close-times-volume is a liquidity proxy, not cash turnover.",
            ),
        ]
    )
    for horizon in (5, 20):
        specs.append(
            _spec(
                f"residual_cross_sectional_rank_{horizon}d",
                "residual_cross_sectional_rank",
                "rank_momentum_and_reversal",
                ("positive", "negative"),
                f"residual_rank_{horizon}d_pct",
                f"{horizon} sessions",
                panel_limit
                + " Ranks are within frozen market/date cohorts and retain participant counts.",
            )
        )
    specs.append(
        _spec(
            "range_position_20d",
            "range_position",
            "two_sided",
            ("positive", "negative"),
            "range_position_20d",
            "20 sessions",
            panel_limit
            + " Range position is descriptive and may overlap momentum/reversal interpretations.",
        )
    )
    for key, field, lookback in (
        ("matched_market_return_5d", "MATCHED_MARKET_RETURN_5D", "5 sessions"),
        ("matched_market_return_20d", "MATCHED_MARKET_RETURN_20D", "20 sessions"),
        ("matched_market_volatility_20d", "MATCHED_MARKET_VOLATILITY_20D", "20 sessions"),
    ):
        specs.append(
            _spec(
                key,
                "market_context",
                "conditioning_only",
                ("positive", "negative"),
                field,
                lookback,
                panel_limit
                + " Market context is diagnostic conditioning only and is ineligible for a family promotion.",
            )
        )
    specs.append(
        _spec(
            "market_breadth_positive",
            "market_breadth",
            "conditioning_only",
            ("positive", "negative"),
            "market_breadth_positive_pct",
            "1 session",
            panel_limit + " Breadth is computed over the available current-listing cohort.",
        )
    )
    specs.append(
        _spec(
            "market_excess_dispersion_1d",
            "market_excess_dispersion",
            "conditioning_only",
            ("positive", "negative"),
            "market_excess_dispersion_1d_pct",
            "1 session",
            panel_limit
            + " Dispersion is cross-sectional stock-minus-index daily return dispersion.",
        )
    )
    return specs


EXCESS_5D_STATES = ("LE_-4", "-4_TO_-2", "-2_TO_0", "0_TO_2", "2_TO_4", "GE_4")
TURNOVER_STATES = ("CONTRACTED", "NORMAL", "EXPANDED")
DRAWDOWN_20D_STATES = ("DEEP_LE_-8", "-8_TO_-4", "-4_TO_0", "AT_HIGH_OR_ABOVE")
IDIO_VOL_STATES = ("LOW", "MID", "HIGH")
RESIDUAL_RANK_STATES = ("LOWER", "MIDDLE", "UPPER")


def interaction_specs() -> list[FactorSpec]:
    limitation = "Historical Phase 13 state definitions are reused; state indicators are discrete and do not establish a strategy."
    definitions = (
        (
            "excess_turnover",
            "excess_return_5d_pct",
            "turnover_ratio_5d_vs_prior20_median",
            EXCESS_5D_STATES,
            TURNOVER_STATES,
        ),
        (
            "residual_drawdown_idio_vol",
            "residual_drawdown_20d_pct",
            "idio_volatility_20d_pct",
            DRAWDOWN_20D_STATES,
            IDIO_VOL_STATES,
        ),
        (
            "residual_rank_turnover",
            "residual_rank_20d_state",
            "turnover_ratio_5d_vs_prior20_median",
            RESIDUAL_RANK_STATES,
            TURNOVER_STATES,
        ),
    )
    specs: list[FactorSpec] = []
    for name, left_field, right_field, left_states, right_states in definitions:
        family_id = f"phase13_{name}_interaction"
        for left_state in left_states:
            for right_state in right_states:
                factor_id = f"{family_id}__{left_state}__{right_state}"
                is_candidate = (
                    name == "excess_turnover"
                    and left_state == "LE_-4"
                    and right_state == "EXPANDED"
                )
                specs.append(
                    FactorSpec(
                        factor_id=factor_id,
                        family_id=family_id,
                        semantic_family="historical_interaction_state",
                        orientation_hypotheses=("positive",)
                        if is_candidate
                        else ("positive", "negative"),
                        source="src/krx_trader/research/phase13.py:FACTOR_BUCKETS and INTERACTION_DEFINITIONS",
                        lookback="Phase 13 fixed state map",
                        field=None,
                        required_fields=(left_field, right_field, "date", "symbol", "market"),
                        limitation=limitation,
                        source_type="HISTORICAL_INTERACTION_STATE",
                        left_field=left_field,
                        right_field=right_field,
                        left_state=left_state,
                        right_state=right_state,
                        candidate_reference=is_candidate,
                    )
                )
    return specs


def all_factor_specs() -> list[FactorSpec]:
    return base_factor_specs() + interaction_specs()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _state(value: Any, field: str, idio_cuts: tuple[float, float] | None) -> str | None:
    if value is None:
        return None
    if field == "excess_return_5d_pct":
        number = _finite(value)
        if number is None:
            return None
        if number < -4:
            return "LE_-4"
        if number < -2:
            return "-4_TO_-2"
        if number < 0:
            return "-2_TO_0"
        if number < 2:
            return "0_TO_2"
        if number < 4:
            return "2_TO_4"
        return "GE_4"
    if field == "turnover_ratio_5d_vs_prior20_median":
        number = _finite(value)
        if number is None:
            return None
        return "CONTRACTED" if number < 0.75 else "NORMAL" if number < 1.5 else "EXPANDED"
    if field == "residual_drawdown_20d_pct":
        number = _finite(value)
        if number is None:
            return None
        if number < -8:
            return "DEEP_LE_-8"
        if number < -4:
            return "-8_TO_-4"
        if number < 0:
            return "-4_TO_0"
        return "AT_HIGH_OR_ABOVE"
    if field == "idio_volatility_20d_pct":
        number = _finite(value)
        if number is None or idio_cuts is None:
            return None
        return "LOW" if number < idio_cuts[0] else "MID" if number < idio_cuts[1] else "HIGH"
    if field == "residual_rank_20d_state":
        return str(value) if value in RESIDUAL_RANK_STATES else None
    return None


def factor_value(
    spec: FactorSpec, features: dict[str, Any], idio_cuts: tuple[float, float] | None
) -> float | None:
    if spec.family_id == "market_context":
        market = str(features.get("market", "")).lower()
        suffix = "return_5d_pct" if spec.factor_id.endswith("5d") else "return_20d_pct"
        if "volatility" in spec.factor_id:
            suffix = "volatility_20d_pct"
        return _finite(features.get(f"{market}_{suffix}"))
    if spec.family_id.startswith("phase13_") and spec.source_type == "HISTORICAL_INTERACTION_STATE":
        left = _state(features.get(spec.left_field or ""), spec.left_field or "", idio_cuts)
        right = _state(features.get(spec.right_field or ""), spec.right_field or "", idio_cuts)
        return (
            float(left == spec.left_state and right == spec.right_state)
            if left is not None and right is not None
            else None
        )
    return _finite(features.get(spec.field or ""))


def registry_payload() -> dict[str, Any]:
    specs = all_factor_specs()
    return {
        "artifact": "phase14-factor-registry",
        "schema_version": 1,
        "source": "Phase 13 pre-Confirmation factor definitions and state maps",
        "factor_count": len(specs),
        "family_count": len({spec.family_id for spec in specs}),
        "directional_hypothesis_count": sum(len(spec.orientation_hypotheses) for spec in specs),
        "state_quantile_count": 5,
        "items": [
            {
                **asdict(spec),
                "orientation_hypotheses": list(spec.orientation_hypotheses),
                "required_fields": list(spec.required_fields),
            }
            for spec in specs
        ],
        "interaction_state_definitions": {
            "excess_return_5d_pct": list(EXCESS_5D_STATES),
            "turnover_ratio_5d_vs_prior20_median": list(TURNOVER_STATES),
            "residual_drawdown_20d_pct": list(DRAWDOWN_20D_STATES),
            "idio_volatility_20d_pct": {
                "states": list(IDIO_VOL_STATES),
                "cuts": "Discovery-only 1/3 and 2/3 empirical quantiles, matching Phase 13",
            },
            "residual_rank_20d_state": list(RESIDUAL_RANK_STATES),
        },
        "candidate_reference": {
            "factor_id": next(spec.factor_id for spec in specs if spec.candidate_reference),
            "market_scope": "KOSPI in the historical Phase 13 candidate; both market splits are retained in the V2 search surface",
            "historical_rule_label": "5d excess return <= -4% x expanded traded-value ratio >= 1.5",
            "entry_exit_or_position_sizing": "NOT_INCLUDED",
        },
        "limitations": [
            "Current-listing cohort is survivorship biased; point-in-time membership is partial.",
            "KIS adjusted prices have no historical vintage control.",
            "Market context, breadth, and dispersion are conditioning diagnostics, not candidate factors.",
        ],
    }

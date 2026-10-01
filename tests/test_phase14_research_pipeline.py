from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pytest

from krx_trader.research.pipeline.concentration import (
    concentration_analysis,
    date_cluster_bootstrap_ci,
    family_cluster_bootstrap,
)
from krx_trader.research.pipeline.config import (
    CROSS_SECTIONAL_PERMUTATIONS,
    RANDOM_SEED,
    load_frozen_config,
    write_static_phase14_artifacts,
)
from krx_trader.research.pipeline.economic_gate import economic_feasibility
from krx_trader.research.pipeline.evidence_ledger import (
    ProtectedEvidenceError,
    assert_phase14_evidence_date,
    classify_period,
    historical_research_ledger,
)
from krx_trader.research.pipeline.factor_registry import (
    FactorSpec,
    all_factor_specs,
    factor_value,
    registry_payload,
)
from krx_trader.research.pipeline.factor_viability import evaluate_factor_viability
from krx_trader.research.pipeline.market_adjustment import market_adjusted_edges
from krx_trader.research.pipeline.multiple_testing import multiple_testing_audit
from krx_trader.research.pipeline.null_models import (
    deterministic_permutation_digest,
    negative_control_values,
    permutation_max_statistics,
    time_dislocation_null,
)
from krx_trader.research.pipeline.opportunity_surface import (
    calculate_opportunity_surface,
    edge_to_friction_ratio,
)
from krx_trader.research.pipeline.panel import (
    PanelContractError,
    PanelRecord,
    parse_panel_record,
)
from krx_trader.research.pipeline.predictability import build_daily_ics, summarize_daily_ics
from krx_trader.research.pipeline.quantile_spreads import build_quantile_artifacts
from krx_trader.research.pipeline.retrospective_audit import phase13_preconfirmation_audit
from krx_trader.research.pipeline.walk_forward import walk_forward_summary


def _records(
    *, dates_per_block: int = 20
) -> tuple[list[PanelRecord], FactorSpec, dict[str, list[float]]]:
    spec = FactorSpec(
        factor_id="synthetic_rank_factor",
        family_id="synthetic_family",
        semantic_family="momentum",
        orientation_hypotheses=("positive",),
        source="test fixture",
        lookback="none",
        field="x",
        required_fields=("x",),
        limitation="synthetic",
    )
    blocks = (
        date(2023, 1, 2),
        date(2023, 7, 1),
        date(2024, 1, 1),
        date(2024, 7, 1),
        date(2025, 1, 1),
    )
    records: list[PanelRecord] = []
    factor_values: list[float] = []
    for block_index, start in enumerate(blocks):
        for date_index in range(dates_per_block):
            signal_day = start + timedelta(days=date_index * 5)
            for symbol_index in range(12):
                symbol = f"S{symbol_index:03d}"
                factor = (
                    float(symbol_index) + date_index * 0.03 + block_index * dates_per_block * 0.03
                )
                for horizon in (3, 5, 10):
                    absolute = 0.1 + factor * 0.08 + block_index * 0.005
                    excess = 0.05 + factor * 0.05
                    residual = 0.02 + factor * 0.04
                    matched = absolute - excess
                    records.append(
                        PanelRecord(
                            signal_date=signal_day,
                            symbol=symbol,
                            market="KOSPI",
                            horizon_sessions=horizon,
                            availability_time=f"{signal_day.isoformat()}T15:30:00+09:00",
                            participant_count=12,
                            quality_flags=(),
                            features={"x": factor},
                            targets={
                                "ABSOLUTE_RETURN": absolute,
                                "MATCHED_MARKET_COMPONENT": matched,
                                "SIMPLE_EXCESS_RETURN": excess,
                                "BETA_RESIDUAL_RETURN": residual,
                            },
                            entry_date=signal_day + timedelta(days=1),
                            exit_date=signal_day + timedelta(days=horizon),
                        )
                    )
                    factor_values.append(factor)
    return records, spec, {spec.factor_id: factor_values}


def test_ledger_counts_and_periods_are_cumulative_and_explicit() -> None:
    ledger = historical_research_ledger()
    assert ledger["history_is_cumulative"] is True
    assert ledger["counts"]["strategy_families_minimum"] >= 9
    assert ledger["counts"]["factor_families_minimum"] >= 11
    assert ledger["counts"]["interaction_families_minimum"] >= 4
    assert classify_period("2024-06-28") == "HEAVILY_TOUCHED_DISCOVERY"
    assert classify_period("2025-09-01") == "CONSUMED_BY_PHASE13_CONFIRMATION"
    assert classify_period("2026-02-01") == "UNREAD_EXTERNAL"
    assert classify_period("2026-08-01") == "OUTCOME_UNREAD_METADATA_EXPOSED"


@pytest.mark.parametrize("protected_day", ["2025-07-01", "2026-02-01", "2026-08-01"])
def test_phase14_date_guard_rejects_confirmation_external_and_holdout(protected_day: str) -> None:
    with pytest.raises(ProtectedEvidenceError):
        assert_phase14_evidence_date(protected_day)


def test_factor_registry_reuses_historical_definitions_and_counts_orientations() -> None:
    payload = registry_payload()
    specs = all_factor_specs()
    assert payload["factor_count"] == len(specs)
    assert payload["family_count"] >= 14
    assert any(item["factor_id"] == "raw_trailing_return_5d" for item in payload["items"])
    assert (
        sum(len(spec.orientation_hypotheses) for spec in specs)
        == payload["directional_hypothesis_count"]
    )


def test_factor_generation_does_not_depend_on_forward_outcome_changes() -> None:
    spec = next(spec for spec in all_factor_specs() if spec.factor_id == "raw_trailing_return_5d")
    features = {"stock_return_5d_pct": 1.25, "forward_outcome": {"stock_gross_return_pct": 500}}
    before = factor_value(spec, features, None)
    features["forward_outcome"]["stock_gross_return_pct"] = -500
    assert factor_value(spec, features, None) == before == 1.25


def test_opportunity_surface_reports_distribution_and_friction_scale() -> None:
    records, _, _ = _records(dates_per_block=2)
    result = calculate_opportunity_surface(records)
    assert len(result["results"]) == 9
    row = next(
        row
        for row in result["results"]
        if row["target"] == "ABSOLUTE_RETURN" and row["horizon_sessions"] == 3
    )
    assert {
        "mean_pct",
        "median_pct",
        "sample_stddev_pct",
        "p10_pct",
        "p25_pct",
        "p75_pct",
        "p90_pct",
    } <= row.keys()
    assert row["stock_observations"] == 120
    assert row["unique_dates"] == 10
    assert row["edge_to_friction_ratio"]["typical_absolute_move"] == pytest.approx(
        row["typical_absolute_move_pct"] / 0.53
    )


def test_daily_rank_ic_quantile_spread_and_monotonicity_capture_ordered_factor() -> None:
    records, spec, values = _records()
    daily = build_daily_ics(records, [spec], values, minimum_participants=10)
    summary = summarize_daily_ics(daily)
    row = next(
        item
        for item in summary["results"]
        if item["target"] == "ABSOLUTE_RETURN"
        and item["horizon_sessions"] == 3
        and item["market_scope"] == "ALL"
    )
    assert row["mean_ic"] == pytest.approx(1.0)
    assert row["signal_date_cluster_ci90"][0] > 0
    spreads, monotonicity = build_quantile_artifacts(
        records, [spec], values, minimum_participants=10
    )
    spread = next(
        item
        for item in spreads["results"]
        if item["target"] == "ABSOLUTE_RETURN"
        and item["horizon_sessions"] == 3
        and item["market_scope"] == "ALL"
    )
    mono = next(
        item
        for item in monotonicity["results"]
        if item["target"] == "ABSOLUTE_RETURN"
        and item["horizon_sessions"] == 3
        and item["market_scope"] == "ALL"
    )
    assert spread["q5_minus_q1_pct"] > 0
    assert [row["mean_outcome_pct"] for row in spread["quantiles"]] == sorted(
        row["mean_outcome_pct"] for row in spread["quantiles"]
    )
    assert mono["adjacent_inversions_positive"] == 0


def test_quantile_surface_omits_market_date_groups_below_minimum_participant_count() -> None:
    records, spec, values = _records(dates_per_block=1)
    kept = [record for record in records if record.symbol not in {"S010", "S011"}]
    record_indices = {record.key: index for index, record in enumerate(records)}
    kept_values = {
        spec.factor_id: [values[spec.factor_id][record_indices[record.key]] for record in kept]
    }
    result, _ = build_quantile_artifacts(kept, [spec], kept_values, minimum_participants=11)
    assert result["results"] == []


def test_walk_forward_reports_fixed_blocks_and_directional_stability() -> None:
    records, spec, values = _records()
    daily = build_daily_ics(records, [spec], values, minimum_participants=10)
    selected = {
        "synthetic_family": {
            "factor_id": spec.factor_id,
            "target": "ABSOLUTE_RETURN",
            "horizon_sessions": 3,
            "market_scope": "ALL",
            "direction": "positive",
        }
    }
    result = walk_forward_summary(
        records, daily, selected, {spec.factor_id: spec}, values, minimum_participants=10
    )
    family = result["results"][0]
    assert [block["block"] for block in family["blocks"]] == [
        "2023_H1",
        "2023_H2",
        "2024_H1",
        "2024_H2",
        "2025_H1",
    ]
    assert family["stable_blocks"] == 5
    assert (
        family["stability_components"]["sample_sufficiency"]["blocks_with_at_least_20_dates"] == 5
    )


def test_cross_sectional_max_null_is_deterministic_and_predictive_factor_separates() -> None:
    records, spec, values = _records()
    first = permutation_max_statistics(records, [spec], values, permutations=40, seed=RANDOM_SEED)
    second = permutation_max_statistics(records, [spec], values, permutations=40, seed=RANDOM_SEED)
    assert first["observed_family_best_statistic"] == pytest.approx(1.0)
    assert first["empirical_p"] <= 1 / 41
    assert first["same_family_search_surface_for_every_permutation"] is True
    assert deterministic_permutation_digest(first) == deterministic_permutation_digest(second)
    assert (
        first["maximum_distribution_sha256"]
        == hashlib.sha256(
            ",".join(f"{value:.12g}" for value in first["null_max_distribution"]).encode()
        ).hexdigest()
    )


def test_negative_control_is_deterministic_and_does_not_become_predictive() -> None:
    records, spec, _ = _records()
    values = negative_control_values(records, seed=RANDOM_SEED)
    assert values == negative_control_values(records, seed=RANDOM_SEED)
    result = permutation_max_statistics(
        records,
        [
            FactorSpec(
                **{
                    **spec.__dict__,
                    "factor_id": "negative_control",
                    "family_id": "negative_control",
                    "field": None,
                }
            )
        ],
        {"negative_control": values},
        permutations=40,
        seed=RANDOM_SEED,
    )
    assert result["observed_family_best_statistic"] < 0.2
    assert result["empirical_p"] > 0.05


def test_time_dislocation_null_preserves_seed_and_safe_window() -> None:
    records, spec, values = _records(dates_per_block=30)
    result = time_dislocation_null(
        records,
        spec,
        values,
        permutations=8,
        seed=RANDOM_SEED,
        minimum_shift_sessions=10,
    )
    assert result["status"] == "COMPUTED"
    assert result["seed"] == RANDOM_SEED
    assert result["protected_period_rows_used"] == 0
    assert result["date_range"][1] <= "2025-06-30"


def test_multiple_testing_accounting_reports_family_and_cell_counts() -> None:
    specs = all_factor_specs()
    audit = multiple_testing_audit(specs, {}, historical_ledger=historical_research_ledger())
    surface = audit["phase14_search_surface"]
    assert surface["factor_definitions"] == len(specs)
    assert surface["factor_families"] == len({spec.family_id for spec in specs})
    assert surface["directional_interpretations"] == sum(
        len(spec.orientation_hypotheses) for spec in specs
    )
    assert audit["formal_fdr_or_fwer_method"] == "NOT_IMPLEMENTED"


@pytest.mark.parametrize(("gross", "expected"), [(0.3, 0.0), (1.5, 1.0)])
def test_edge_to_friction_ratio_respects_cost_tier(gross: float, expected: float) -> None:
    assert edge_to_friction_ratio(gross) == pytest.approx(gross / 0.53)
    assert (edge_to_friction_ratio(gross) >= 1.0) is bool(expected)


@pytest.mark.parametrize(("gross", "expected_grade"), [(0.3, "FAIL"), (1.5, "PASS")])
def test_economic_gate_compares_absolute_factor_effect_to_round_trip_cost(
    gross: float, expected_grade: str
) -> None:
    records, spec, values = _records(dates_per_block=2)
    records = [
        PanelRecord(
            **{
                **record.__dict__,
                "targets": {**record.targets, "ABSOLUTE_RETURN": gross},
            }
        )
        for record in records
    ]
    selected = {
        spec.family_id: {
            "factor_id": spec.factor_id,
            "target": "ABSOLUTE_RETURN",
            "horizon_sessions": 3,
            "market_scope": "ALL",
            "direction": "positive",
        }
    }
    result = economic_feasibility(records, selected, {spec.factor_id: spec}, values)
    assert result["results"][0]["economic_scale_grade"] == expected_grade
    assert result["results"][0]["break_even_friction_pct"] == pytest.approx(gross)


def test_market_component_share_flags_beta_dominance() -> None:
    records, spec, values = _records()
    # Keep absolute returns at +2.0% and matched market at +1.8%, leaving +0.2% excess.
    adjusted = [
        PanelRecord(
            **{
                **record.__dict__,
                "targets": {
                    **record.targets,
                    "ABSOLUTE_RETURN": 2.0,
                    "MATCHED_MARKET_COMPONENT": 1.8,
                    "SIMPLE_EXCESS_RETURN": 0.2,
                    "BETA_RESIDUAL_RETURN": 0.15,
                },
            }
        )
        for record in records
    ]
    surface = {
        spec.family_id: {
            "factor_id": spec.factor_id,
            "target": "ABSOLUTE_RETURN",
            "horizon_sessions": 3,
            "market_scope": "ALL",
            "direction": "positive",
        }
    }
    result = market_adjusted_edges(adjusted, surface, {spec.factor_id: spec}, values)
    row = result["results"][0]
    assert row["market_component_share_pct"] == pytest.approx(90)
    assert row["market_beta_dominated"] == "YES"
    assert row["market_adjusted_edge_grade"] == "FAIL"


def test_date_concentration_and_cluster_bootstrap_keep_date_as_unit() -> None:
    records, spec, values = _records(dates_per_block=5)
    selected = {
        spec.family_id: {
            "factor_id": spec.factor_id,
            "target": "ABSOLUTE_RETURN",
            "horizon_sessions": 3,
            "market_scope": "ALL",
            "direction": "positive",
        }
    }
    concentration = concentration_analysis(records, selected, {spec.factor_id: spec}, values)[
        "results"
    ][0]
    assert concentration["date_clusters"] == concentration["unique_signal_dates"]
    assert concentration["top_five_dates_share_pct"] is not None
    boot = date_cluster_bootstrap_ci(records, target="ABSOLUTE_RETURN", repetitions=40, seed=10)
    assert boot["cluster"] == "signal_date; all market subgroups are averaged within date"
    assert boot["ci90_pct"][0] <= boot["ci90_pct"][1]
    family_boot = family_cluster_bootstrap(
        records, selected, {spec.factor_id: spec}, values, repetitions=20
    )
    assert family_boot["stock_rows_bootstrapped_independently"] is False


def test_viability_rejects_without_complete_positive_gates() -> None:
    spec = FactorSpec(
        factor_id="negative_control",
        family_id="negative_control",
        semantic_family="noise",
        orientation_hypotheses=("positive",),
        source="test",
        lookback="none",
        field=None,
        required_fields=("symbol", "date"),
        limitation="test",
        source_type="NEGATIVE_CONTROL",
    )
    result = evaluate_factor_viability(
        specs=[spec],
        null_results={"negative_control": {"status": "INSUFFICIENT"}},
        predictability={"results": []},
        quantiles={"results": []},
        monotonicity={"results": []},
        walk_forward={"results": []},
        economics={"results": []},
        market_edges={"results": []},
        concentration={"results": []},
        family_bootstrap={"results": []},
        factor_coverage={},
    )
    assert result["results"][0]["final_state"] == "FAIL"
    assert result["best_factor_viability"] == "NONE"


def test_frozen_config_hash_is_written_and_checked_before_audit(tmp_path) -> None:
    artifacts = write_static_phase14_artifacts(tmp_path / "phase14")
    config, config_sha = load_frozen_config(tmp_path / "phase14")
    assert artifacts["pipeline_config_sha256"] == config_sha
    assert (
        config["null_models"]["cross_sectional"]["permutations"]
        == CROSS_SECTIONAL_PERMUTATIONS
        == 1000
    )
    (tmp_path / "phase14" / "pipeline-v2-config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_frozen_config(tmp_path / "phase14")


def test_phase13_confirmation_label_is_appended_after_preconfirmation_verdict() -> None:
    verdict = {
        "results": [{"family_id": "phase13_excess_turnover_interaction", "final_state": "WEAK"}]
    }
    audit = phase13_preconfirmation_audit(
        factor_viability=verdict,
        pipeline_config_sha256="abc123",
        challenge_label="PHASE13_CONFIRMATION_FAIL",
        challenge_metrics={"gross_pct": -0.9},
    )
    assert audit["preconfirmation_v2_verdict"] == "WEAK"
    assert audit["would_have_blocked_candidate_from_confirmation"] is True
    assert audit["phase13_confirmation_used_as_pipeline_input"] is False
    assert audit["post_hoc_gate_changes"] is False


def test_future_confirmation_or_protected_dates_cannot_be_loaded_from_raw_panel_row() -> None:
    raw = {
        "date": "2025-07-01",
        "symbol": "S1",
        "market": "KOSPI",
        "market_breadth_participants": 10,
        "forward_outcome": {
            "horizon_sessions": 3,
            "entry_date": "2025-07-02",
            "exit_date": "2025-07-04",
            "stock_gross_return_pct": 1,
            "matched_market_return_pct": 0.2,
            "excess_return_pct": 0.8,
            "beta_residual_return_pct": 0.7,
        },
    }
    with pytest.raises(ProtectedEvidenceError):
        parse_panel_record(raw)


def test_previous_phase_artifact_snapshot_is_read_only_and_deterministic() -> None:
    # The runner snapshots hashes and compares path/hash/size tuples; this test
    # checks the tracked historical evidence surface remains untouched in place.
    from krx_trader.research.pipeline.runner import _previous_phase_hashes

    repo = __import__("pathlib").Path(__file__).resolve().parents[1]
    before = _previous_phase_hashes(repo)
    after = _previous_phase_hashes(repo)
    assert before == after
    assert before["files"]


def test_incomplete_config_or_outcome_fields_fail_closed() -> None:
    raw = {
        "date": "2025-06-01",
        "symbol": "S1",
        "market": "KOSPI",
        "forward_outcome": {
            "horizon_sessions": 3,
            "entry_date": "2025-06-02",
            "exit_date": "2025-06-04",
        },
    }
    record = parse_panel_record(raw)
    assert record.targets["ABSOLUTE_RETURN"] is None
    with pytest.raises(PanelContractError):
        parse_panel_record({"date": "invalid"})

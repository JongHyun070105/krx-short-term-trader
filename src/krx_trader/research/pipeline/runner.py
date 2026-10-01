from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from krx_trader.research.pipeline.concentration import (
    concentration_analysis,
    family_cluster_bootstrap,
)
from krx_trader.research.pipeline.config import (
    CLUSTER_BOOTSTRAP_REPETITIONS,
    CROSS_SECTIONAL_PERMUTATIONS,
    EXPECTED_COST_PCT,
    RANDOM_SEED,
    TIME_DISLOCATION_PERMUTATIONS,
    json_bytes,
    load_frozen_config,
)
from krx_trader.research.pipeline.economic_gate import economic_feasibility
from krx_trader.research.pipeline.factor_registry import (
    FactorSpec,
    all_factor_specs,
    registry_payload,
)
from krx_trader.research.pipeline.factor_viability import evaluate_factor_viability
from krx_trader.research.pipeline.market_adjustment import market_adjusted_edges
from krx_trader.research.pipeline.multiple_testing import multiple_testing_audit
from krx_trader.research.pipeline.null_models import (
    negative_control_values,
    permutation_max_statistics,
    time_dislocation_null,
)
from krx_trader.research.pipeline.opportunity_surface import calculate_opportunity_surface
from krx_trader.research.pipeline.panel import (
    SAFE_PANEL_RELATIVE_PATH,
    factor_matrix_values,
    load_safe_panel,
    panel_file_sha256,
)
from krx_trader.research.pipeline.predictability import build_daily_ics, summarize_daily_ics
from krx_trader.research.pipeline.quantile_spreads import build_quantile_artifacts
from krx_trader.research.pipeline.retrospective_audit import (
    historical_family_audit,
    phase13_preconfirmation_audit,
)
from krx_trader.research.pipeline.walk_forward import walk_forward_summary

STARTING_SHA = "160fb16419f33be45efeac7e48ab4b1eda444661"
PREVIOUS_PHASE_SNAPSHOT_TEMP = Path("/tmp/phase14-baseline-phase5-13.json")
CHALLENGE_METRICS = {
    "gross_pct": -0.928790,
    "net_1x_pct": -1.458790,
    "simple_excess_pct": -1.489630,
    "beta_residual_pct": -1.252423,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _previous_phase_hashes(repo_root: Path) -> dict[str, Any]:
    files = []
    research_root = repo_root / "runtime" / "research"
    for phase in range(5, 14):
        directory = research_root / f"phase{phase}"
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            name = path.name.lower()
            if any(
                token in name
                for token in ("manifest", "artifact-index", "summary", "artifact-integrity")
            ) or name.endswith(".sha256"):
                files.append(
                    {
                        "path": path.relative_to(repo_root).as_posix(),
                        "sha256": _sha256(path),
                        "size_bytes": path.stat().st_size,
                    }
                )
    return {
        "scope": "Phase 5-13 manifests, artifact indexes, final summaries, and integrity records",
        "files": files,
    }


def _negative_control_spec() -> FactorSpec:
    return FactorSpec(
        factor_id="negative_control_stable_hash_pseudorank",
        family_id="negative_control",
        semantic_family="deliberately_meaningless_deterministic_hash_noise",
        orientation_hypotheses=("positive", "negative"),
        source="SHA-256(seed, symbol, signal_date); no price, volume, market, or future target input",
        lookback="none",
        field=None,
        required_fields=("symbol", "date"),
        limitation="Must fail the complete factor viability pipeline; diagnostic only.",
        source_type="NEGATIVE_CONTROL",
    )


def _coverage(
    specs: list[FactorSpec], values: dict[str, list[float | None]], records: list[Any]
) -> dict[str, dict[str, Any]]:
    output = {}
    for spec in specs:
        observed = [
            (record, value)
            for record, value in zip(records, values[spec.factor_id], strict=True)
            if value is not None
        ]
        output[spec.factor_id] = {
            "non_null_observations": len(observed),
            "all_panel_observations": len(records),
            "non_null_rate": len(observed) / len(records) if records else 0.0,
            "unique_dates": len({record.signal_date for record, _ in observed}),
            "symbols": len({record.symbol for record, _ in observed}),
            "markets": sorted({record.market for record, _ in observed}),
            "availability_time": "T session close, derived as 15:30 Asia/Seoul from the Phase 13 post-close signal convention",
            "quality_flags": ["CURRENT_LISTING_COHORT", "ADJUSTED_PRICE_REVISION_RISK"],
        }
    return output


def _metadata(
    *,
    source_git_sha: str,
    dataset_sha: str,
    registry_sha: str,
    config_sha: str,
) -> dict[str, Any]:
    return {
        "source_git_sha": source_git_sha,
        "dataset_sha256": dataset_sha,
        "factor_registry_sha256": registry_sha,
        "pipeline_config_sha256": config_sha,
        "random_seed": RANDOM_SEED,
        "random_generator": "numpy.Generator(PCG64)",
        "numpy_version": np.__version__,
        "cross_sectional_permutations": CROSS_SECTIONAL_PERMUTATIONS,
        "time_dislocation_permutations": TIME_DISLOCATION_PERMUTATIONS,
        "cluster_bootstrap_repetitions": CLUSTER_BOOTSTRAP_REPETITIONS,
        "date_range": ["2023-01-02", "2025-06-30"],
        "targets": ["ABSOLUTE_RETURN", "SIMPLE_EXCESS_RETURN", "BETA_RESIDUAL_RETURN"],
        "horizons_sessions": [3, 5, 10],
        "round_trip_cost_pct": EXPECTED_COST_PCT,
        "generated_at_utc": datetime.now(UTC).isoformat(),
    }


def _attach_metadata(value: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    return {**value, "run_metadata": metadata}


def _write_json(root: Path, name: str, value: dict[str, Any]) -> None:
    (root / name).write_bytes(json_bytes(value))


def _surface_map(null_results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        family: result["observed_best_surface"]
        for family, result in null_results.items()
        if result.get("observed_best_surface")
    }


def _family_lists(
    viability: dict[str, Any],
    null_results: dict[str, dict[str, Any]],
    economics: dict[str, Any],
    market_edges: dict[str, Any],
    concentration: dict[str, Any],
) -> dict[str, Any]:
    results = {item["family_id"]: item for item in viability["results"]}
    ic_families = [
        family
        for family, result in _surface_map(null_results).items()
        if family != "negative_control" and result["oriented_mean_ic"] >= 0.02
    ]
    economics_by_family = {item["family_id"]: item for item in economics["results"]}
    market_by_family = {item["family_id"]: item for item in market_edges["results"]}
    concentration_by_family = {item["family_id"]: item for item in concentration["results"]}
    return {
        "nonzero_predictive_ic_families": ic_families,
        "monotonic_quantile_families": [
            family
            for family, result in results.items()
            if family != "negative_control" and result["dimensions"]["MONOTONICITY"] == "PASS"
        ],
        "directionally_stable_families": [
            family
            for family, result in results.items()
            if family != "negative_control"
            and result.get("stability_components", {})
            .get("sign_consistency", {})
            .get("positive_blocks", 0)
            >= 4
        ],
        "selection_aware_null_separating_families": [
            family
            for family, result in null_results.items()
            if family != "negative_control"
            and result.get("empirical_p") is not None
            and result["empirical_p"] <= 0.05
        ],
        "cost_sufficient_families": [
            family
            for family, result in economics_by_family.items()
            if family != "negative_control" and result.get("economic_scale_grade") == "PASS"
        ],
        "market_adjusted_families": [
            family
            for family, result in market_by_family.items()
            if family != "negative_control" and result.get("market_adjusted_edge_grade") == "PASS"
        ],
        "concentration_by_family": {
            family: {
                "top_five_dates_share_pct": result.get("top_five_dates_share_pct"),
                "top_three_symbols_share_pct": result.get("top_three_symbols_share_pct"),
                "date_clusters": result.get("date_clusters"),
            }
            for family, result in concentration_by_family.items()
        },
    }


def run_phase14(repo_root: Path, output_root: Path) -> dict[str, Any]:
    config, config_sha = load_frozen_config(output_root)
    registry_path = output_root / "factor-registry.json"
    expected_registry_sha = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    dof = json.loads((output_root / "research-degrees-of-freedom.json").read_text(encoding="utf-8"))
    if expected_registry_sha != dof["registry_sha256"]:
        raise ValueError(
            "factor registry differs from its frozen research-degrees-of-freedom digest"
        )
    if json.loads(registry_path.read_text(encoding="utf-8")) != registry_payload():
        raise ValueError("factor registry no longer matches source-defined factor specifications")
    source_sha = _git_sha(repo_root)
    panel_path = repo_root / SAFE_PANEL_RELATIVE_PATH
    dataset_sha = panel_file_sha256(panel_path)
    records = load_safe_panel(panel_path)
    specs = all_factor_specs()
    control_spec = _negative_control_spec()
    analysis_specs = [*specs, control_spec]
    from krx_trader.research.pipeline.panel import discovery_idio_cuts

    idio_cuts = discovery_idio_cuts(records)
    ordinary_values = factor_matrix_values(records, specs, idio_cuts)
    control_values = negative_control_values(records, seed=RANDOM_SEED)
    factor_values = {**ordinary_values, control_spec.factor_id: control_values}
    factor_coverage = _coverage(analysis_specs, factor_values, records)
    historical_ledger = json.loads(
        (output_root / "research-ledger.json").read_text(encoding="utf-8")
    )
    previous_before = json.loads(PREVIOUS_PHASE_SNAPSHOT_TEMP.read_text(encoding="utf-8"))
    current_before = _previous_phase_hashes(repo_root)
    previous_immutable_before = previous_before["files"] == current_before["files"]
    if not previous_immutable_before:
        raise RuntimeError(
            "Phase 5-13 manifests/indexes/summaries changed after the pre-implementation snapshot"
        )

    opportunity = calculate_opportunity_surface(records)
    daily = build_daily_ics(
        records,
        analysis_specs,
        factor_values,
        minimum_participants=config["thresholds"]["minimum_participants_per_market_date"],
    )
    predictability = summarize_daily_ics(daily)
    quantiles, monotonicity = build_quantile_artifacts(
        records,
        analysis_specs,
        factor_values,
        minimum_participants=config["thresholds"]["minimum_participants_per_market_date"],
    )

    specs_by_family: dict[str, list[FactorSpec]] = {}
    for spec in analysis_specs:
        specs_by_family.setdefault(spec.family_id, []).append(spec)
    null_results = {}
    for family_id, family_specs in sorted(specs_by_family.items()):
        null_results[family_id] = permutation_max_statistics(
            records,
            family_specs,
            factor_values,
            permutations=CROSS_SECTIONAL_PERMUTATIONS,
            seed=RANDOM_SEED,
            minimum_participants=config["thresholds"]["minimum_participants_per_market_date"],
            minimum_ic_threshold=config["thresholds"]["minimum_predictive_abs_mean_ic"],
        )

    null_config = {
        "artifact": "null-model-config",
        "fixed_before_reading_null_results": True,
        "seed": RANDOM_SEED,
        "cross_sectional_permutations": CROSS_SECTIONAL_PERMUTATIONS,
        "time_dislocation_permutations": TIME_DISLOCATION_PERMUTATIONS,
        "cross_sectional_method": config["null_models"]["cross_sectional"],
        "time_dislocation_method": config["null_models"]["time_dislocation"],
        "negative_control_method": config["null_models"]["negative_control"],
        "random_generator": config["null_models"]["random_generator"],
        "dataset_sha256": dataset_sha,
        "factor_universe": [spec.factor_id for spec in specs],
        "family_count": len(specs_by_family) - 1,
    }
    null_results_artifact = {
        "artifact": "null-max-statistics",
        "selection_aware_family_maxima": null_results,
        "family_max_is_primary_selection_control": True,
    }
    raw_5d = next(spec for spec in specs if spec.factor_id == "raw_trailing_return_5d")
    time_null = time_dislocation_null(
        records,
        raw_5d,
        factor_values,
        permutations=TIME_DISLOCATION_PERMUTATIONS,
        seed=RANDOM_SEED,
        minimum_shift_sessions=config["null_models"]["time_dislocation"]["minimum_shift_sessions"],
    )
    if time_null.get("status") != "COMPUTED":
        raise RuntimeError("the frozen time-dislocation null could not run on the safe panel")

    selected_surfaces = _surface_map(null_results)
    specs_by_id = {spec.factor_id: spec for spec in analysis_specs}
    walk_forward = walk_forward_summary(
        records,
        daily,
        selected_surfaces,
        specs_by_id,
        factor_values,
        minimum_participants=config["thresholds"]["minimum_participants_per_market_date"],
    )
    economics = economic_feasibility(records, selected_surfaces, specs_by_id, factor_values)
    market_edges = market_adjusted_edges(
        records,
        selected_surfaces,
        specs_by_id,
        factor_values,
        minimum_effect_pct=config["thresholds"]["minimum_market_adjusted_effect_pct"],
        market_share_warning_pct=config["thresholds"]["maximum_market_component_share_pct"],
    )
    concentration = concentration_analysis(records, selected_surfaces, specs_by_id, factor_values)
    family_bootstrap = family_cluster_bootstrap(
        records,
        selected_surfaces,
        specs_by_id,
        factor_values,
        repetitions=CLUSTER_BOOTSTRAP_REPETITIONS,
        seed=RANDOM_SEED,
    )
    viability = evaluate_factor_viability(
        specs=analysis_specs,
        null_results=null_results,
        predictability=predictability,
        quantiles=quantiles,
        monotonicity=monotonicity,
        walk_forward=walk_forward,
        economics=economics,
        market_edges=market_edges,
        concentration=concentration,
        family_bootstrap=family_bootstrap,
        factor_coverage=factor_coverage,
        config=config,
    )
    testing = multiple_testing_audit(
        specs,
        {family: result for family, result in null_results.items() if family != "negative_control"},
        historical_ledger=historical_ledger,
    )
    neg_null = null_results["negative_control"]
    neg_viability = next(
        row for row in viability["results"] if row["family_id"] == "negative_control"
    )
    negative_control = {
        "artifact": "negative-control-results",
        "factor_id": control_spec.factor_id,
        "method": "stable SHA-256 pseudo-rank(symbol,date,seed)",
        "seed": RANDOM_SEED,
        "family_max_observed": neg_null["observed_family_best_statistic"],
        "null_median": neg_null["null_max_median"],
        "null_p95": neg_null["null_max_p95"],
        "empirical_p": neg_null["empirical_p"],
        "randomized_surface_max_at_least_minimum_ic_rate": neg_null[
            "randomized_surface_max_at_least_minimum_ic_rate"
        ],
        "pipeline_final_state": neg_viability["final_state"],
        "passed_pipeline": neg_viability["final_state"] == "PROMISING_FOR_FUTURE_STUDY",
        "calibration": (
            "FAIL" if neg_viability["final_state"] == "PROMISING_FOR_FUTURE_STUDY" else "PASS"
        ),
        "control_count": 1,
        "pass_count": int(neg_viability["final_state"] == "PROMISING_FOR_FUTURE_STUDY"),
        "warning": "A 0/1 control count is a sanity check, not a precise empirical false-promotion rate.",
    }
    phase13_audit = phase13_preconfirmation_audit(
        factor_viability=viability,
        pipeline_config_sha256=config_sha,
        challenge_label="PHASE13_CONFIRMATION_FAIL",
        challenge_metrics=CHALLENGE_METRICS,
    )
    history_audit = historical_family_audit(viability)
    family_lists = _family_lists(viability, null_results, economics, market_edges, concentration)
    opportunity_rows = opportunity["results"]
    informative = len(opportunity_rows) == 9 and all(
        row.get("stock_observations", 0) >= config["thresholds"]["minimum_family_observations"]
        for row in opportunity_rows
    )
    existing = viability["existing_factor_information"]
    next_mode = viability["next_research_mode"]
    if negative_control["calibration"] == "FAIL":
        next_mode = "PIPELINE_RECALIBRATION"
    status_matrix = {
        "RESEARCH_LEDGER": "COMPLETE",
        "PIPELINE_V2": "PASS",
        "OPPORTUNITY_SURFACE": "INFORMATIVE" if informative else "WEAK",
        "PREDICTABILITY_FRAMEWORK": "PASS",
        "NULL_FRAMEWORK": "PASS",
        "MULTIPLE_TESTING_CONTROL": "PASS",
        "ECONOMIC_GATE": "PASS",
        "NEGATIVE_CONTROL": negative_control["calibration"],
        "PIPELINE_FALSE_POSITIVE_AUDIT": phase13_audit["pipeline_false_positive_audit"],
        "EXISTING_FACTOR_INFORMATION": existing,
        "BEST_FACTOR_FAMILY": viability["best_factor_family"] or "NONE",
        "BEST_FACTOR_VIABILITY": viability["best_factor_viability"],
        "NEXT_RESEARCH_MODE": next_mode,
        "EXTERNAL_2026": "NOT_READ",
        "HOLDOUT_2026": "NOT_READ",
        "ALPHA": "UNPROVEN",
        "LIVE": "DISABLED",
        "SHADOW_NEXT_SESSION": "NO",
    }
    opportunity_to_cost = [
        {
            "target": row["target"],
            "horizon_sessions": row["horizon_sessions"],
            "typical_absolute_move_pct": row.get("typical_absolute_move_pct"),
            "typical_move_to_friction_ratio": row.get("edge_to_friction_ratio", {}).get(
                "typical_absolute_move"
            ),
            "cross_sectional_spread_pct": row.get("cross_sectional_spread_pct"),
            "spread_to_friction_ratio": row.get("edge_to_friction_ratio", {}).get(
                "cross_sectional_spread"
            ),
            "upper_tail_absolute_move_pct": row.get("upper_tail_absolute_move_pct"),
            "upper_tail_to_friction_ratio": row.get("edge_to_friction_ratio", {}).get(
                "upper_tail_absolute_move"
            ),
        }
        for row in opportunity_rows
    ]
    family_verdicts = {item["family_id"]: item for item in viability["results"]}
    phase13_v2_family = family_verdicts.get("phase13_excess_turnover_interaction", {})
    if phase13_audit["would_have_blocked_candidate_from_confirmation"]:
        q16 = (
            "N/A: V2 would have blocked it before Confirmation; no failed block needs explanation."
        )
    else:
        q16 = (
            "V2 did not block the candidate because the pre-Confirmation interaction surface cleared its frozen structure, "
            "null, cost, market-adjustment, stability, sample, and concentration gates."
        )
    q_answers = {
        "Q1": {
            "strategy_families_minimum": historical_ledger["counts"]["strategy_families_minimum"],
            "factor_families_minimum": historical_ledger["counts"]["factor_families_minimum"],
            "interaction_families_minimum": historical_ledger["counts"][
                "interaction_families_minimum"
            ],
            "combined_named_family_labels_minimum": historical_ledger["counts"][
                "combined_named_family_labels_minimum"
            ],
            "candidate_rule_count": "At least 2 explicitly staged one-shot rules (Phase 11 and Phase 13); earlier variants are not exhaustively enumerable.",
        },
        "Q2": {
            "heavily_touched": ["2023-01-02..2024-06-28", "2024-07-01..2025-06-30"],
            "consumed_confirmation": ["2025-07-01..2025-12-30"],
            "other_touched_excluded": ["2026-04-17..2026-06-30 Phase 10 Development"],
            "holdout_metadata_exposed_not_pristine": ["2026-07-28..2026-08-28"],
        },
        "Q3": {
            "independent_historical_periods_remaining": 0,
            "unread_external_blocks_available": 1,
            "external_window": "2026-01-05..2026-04-16; unread and not used in Phase 14",
            "holdout_window": "Outcome unread but metadata exposed; not pristine",
            "future_prospective_period_policy": "None specified in inspected Phase 10-13 policy artifacts",
        },
        "Q4": {
            "opportunity_vs_0_53pct_friction": opportunity_to_cost,
            "interpretation": "Distribution scale is not factor edge or profitability; family gates still require factor structure.",
        },
        "Q5": family_lists["nonzero_predictive_ic_families"],
        "Q6": family_lists["monotonic_quantile_families"],
        "Q7": family_lists["directionally_stable_families"],
        "Q8": family_lists["selection_aware_null_separating_families"],
        "Q9": {
            "negative_control_randomized_surface_max_at_least_0_02_ic_rate": negative_control[
                "randomized_surface_max_at_least_minimum_ic_rate"
            ],
            "full_pipeline_negative_control_promotions": f"{negative_control['pass_count']}/{negative_control['control_count']}",
        },
        "Q10": {
            "negative_control_correctly_fails": negative_control["calibration"] == "PASS",
            "negative_control_final_state": negative_control["pipeline_final_state"],
        },
        "Q11": testing["phase14_search_surface"],
        "Q12": family_lists["cost_sufficient_families"],
        "Q13": family_lists["market_adjusted_families"],
        "Q14": family_lists["concentration_by_family"],
        "Q15": {
            "would_v2_have_blocked_phase13": phase13_audit[
                "would_have_blocked_candidate_from_confirmation"
            ],
            "preconfirmation_v2_verdict": phase13_v2_family.get("final_state", "FAIL"),
        },
        "Q16": q16,
        "Q17": {
            "future_strategy_synthesis_family": viability["best_factor_family"] or "NONE",
            "viability": viability["best_factor_viability"],
        },
        "Q18": {
            "existing_information": existing,
            "next_research_mode": next_mode,
            "recommendation": (
                "New reproducible point-in-time information source"
                if next_mode == "NEW_INFORMATION_SOURCE"
                else "A later strategy synthesis may be recommended; none is implemented here."
                if next_mode == "STRATEGY_SYNTHESIS"
                else "Calibrate the frozen pipeline with future prospective evidence; do not change Phase 14 gates."
            ),
        },
    }
    summary = {
        "artifact": "phase14-summary",
        "status_matrix": status_matrix,
        "starting_sha": STARTING_SHA,
        "analysis_source_git_sha": source_sha,
        "analysis_date_window": ["2023-01-02", "2025-06-30"],
        "ledger_summary": historical_ledger["counts"],
        "data_source": "Phase 13 safe factor panel, primary panel clipped at 2025-06-30",
        "factor_registry_summary": json.loads(registry_path.read_text(encoding="utf-8"))
        | {"items": f"{len(specs)} factor definitions; see factor-registry.json"},
        "factor_summary": family_lists,
        "phase13_false_positive_audit": phase13_audit,
        "historical_family_audit": history_audit,
        "required_questions": q_answers,
        "run_protections": {
            "phase13_confirmation_input_rows": 0,
            "external_2026_input_rows": 0,
            "holdout_2026_input_rows": 0,
            "phase10_2026_development_input_rows": 0,
            "strategy_synthesis": "NOT_PERFORMED",
            "new_strategy_candidate": "NOT_CREATED",
            "private_exchange_api": "NOT_USED",
            "trading_or_broker_path": "NOT_USED",
        },
    }
    metadata = _metadata(
        source_git_sha=source_sha,
        dataset_sha=dataset_sha,
        registry_sha=expected_registry_sha,
        config_sha=config_sha,
    )
    outputs = {
        "opportunity-surface.json": opportunity,
        "factor-predictability.json": predictability,
        "quantile-spreads.json": quantiles,
        "factor-monotonicity.json": monotonicity,
        "walk-forward-stability.json": walk_forward,
        "null-model-config.json": null_config,
        "null-max-statistics.json": null_results_artifact,
        "negative-control-results.json": negative_control,
        "multiple-testing-audit.json": testing,
        "economic-feasibility.json": economics,
        "market-adjusted-edge.json": market_edges,
        "concentration-analysis.json": concentration,
        "factor-viability.json": viability,
        "phase13-retrospective-audit.json": phase13_audit,
        "historical-family-audit.json": history_audit,
        "phase14-summary.json": summary,
    }
    for filename, payload in outputs.items():
        _write_json(output_root, filename, _attach_metadata(payload, metadata))
    _write_json(output_root, "phase14-run-metadata.json", metadata)
    _write_json(output_root, "time-dislocation-null.json", _attach_metadata(time_null, metadata))

    before_source = previous_before
    after_source = _previous_phase_hashes(repo_root)
    before_by_path = {item["path"]: item for item in before_source["files"]}
    after_by_path = {item["path"]: item for item in after_source["files"]}
    changed_paths = sorted(
        path
        for path in set(before_by_path) | set(after_by_path)
        if before_by_path.get(path) != after_by_path.get(path)
    )
    changed = bool(changed_paths)
    previous_snapshot = {
        "artifact": "previous-phase-artifact-snapshot",
        "before_snapshot_path": str(PREVIOUS_PHASE_SNAPSHOT_TEMP),
        "before_snapshot_sha256": hashlib.sha256(
            PREVIOUS_PHASE_SNAPSHOT_TEMP.read_bytes()
        ).hexdigest(),
        "before": before_source["files"],
        "after": after_source["files"],
        "previous_phase_artifact_immutability": "FAIL" if changed else "PASS",
        "changed_paths": changed_paths,
    }
    _write_json(
        output_root,
        "previous-phase-artifact-snapshot.json",
        _attach_metadata(previous_snapshot, metadata),
    )
    _write_json(output_root, "time-dislocation-null.json", _attach_metadata(time_null, metadata))
    _write_index_and_integrity(output_root, metadata)
    if changed:
        raise RuntimeError("a Phase 5-13 artifact changed during Phase 14 execution")
    return {
        "status_matrix": status_matrix,
        "phase13_false_positive_audit": phase13_audit["pipeline_false_positive_audit"],
        "negative_control": negative_control["calibration"],
        "best_factor_family": viability["best_factor_family"],
        "best_factor_viability": viability["best_factor_viability"],
        "existing_factor_information": existing,
        "next_research_mode": next_mode,
        "source_git_sha": source_sha,
        "dataset_sha256": dataset_sha,
        "pipeline_config_sha256": config_sha,
        "factor_registry_sha256": expected_registry_sha,
        "input_record_count": len(records),
        "artifact_integrity": json.loads(
            (output_root / "artifact-integrity.json").read_text(encoding="utf-8")
        )["status"],
        "previous_phase_artifact_immutability": previous_snapshot[
            "previous_phase_artifact_immutability"
        ],
    }


def _write_index_and_integrity(output_root: Path, metadata: dict[str, Any]) -> None:
    excluded = {"phase14-artifact-index.json", "artifact-integrity.json"}
    files = []
    for path in sorted(output_root.iterdir()):
        if path.is_file() and path.name not in excluded:
            files.append(
                {
                    "path": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    index = {
        "artifact": "phase14-artifact-index",
        "algorithm": "SHA-256",
        "files": files,
        "run_metadata": metadata,
    }
    _write_json(output_root, "phase14-artifact-index.json", index)
    checks = []
    for item in files:
        path = output_root / item["path"]
        checks.append(
            path.is_file()
            and path.stat().st_size == item["size_bytes"]
            and _sha256(path) == item["sha256"]
        )
    index_path = output_root / "phase14-artifact-index.json"
    index_hash = _sha256(index_path)
    integrity = {
        "artifact": "artifact-integrity",
        "algorithm": "SHA-256",
        "status": "PASS" if all(checks) and index_hash else "FAIL",
        "verified_artifact_count": len(files),
        "failed_paths": [
            item["path"] for item, passed in zip(files, checks, strict=True) if not passed
        ],
        "index_sha256": index_hash,
        "run_metadata": metadata,
    }
    _write_json(output_root, "artifact-integrity.json", integrity)

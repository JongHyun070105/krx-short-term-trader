from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from krx_trader.data.cache import ParquetBarCache
from krx_trader.research import phase8_opening_gap as phase8
from krx_trader.research.phase9_integrity import (
    DEVELOPMENT_END,
    DEVELOPMENT_START,
    _previous_phase_manifest_paths,
    audit_development_minute_cache,
    compare_manifest_hashes,
    manifest_hashes,
    market_by_cohort_order,
    verify_artifact_index,
)

KST = phase8.KST
PHASE9_ROOT = Path("runtime/research/phase9")
DAILY_CACHE_ROOT = PHASE9_ROOT / "reconciliation" / "safe-daily-cache"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_events(path: Path, events: Iterable[dict[str, Any]]) -> None:
    with path.open("wb") as raw_stream, gzip.GzipFile(
        fileobj=raw_stream, mode="wb", mtime=0
    ) as compressed:
        for event in events:
            line = json.dumps(event, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
            compressed.write(line)


def _read_manifest_baseline(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if separator:
            result[name] = digest
    if not result:
        raise ValueError("Phase 9 previous-phase manifest baseline is missing or invalid")
    return result


def _publish_adjusted_sources(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    published_events = []
    for event in events:
        published = dict(event)
        if published["prior_close_source"] == "KIS_DAILY_CLOSE_RAW":
            published["prior_close_source"] = "KIS_DAILY_CLOSE_ADJUSTED_REPLAY"
        published_status = dict(published.get("longer_horizon_status", {}))
        for key, status in published_status.items():
            if status == "CLEAN_RAW_DAILY_HORIZON":
                published_status[key] = "CLEAN_ADJUSTED_DAILY_HORIZON"
        published["longer_horizon_status"] = published_status
        published_events.append(published)
    return published_events


def run_phase8_data_corrected_replay(
    *,
    cohort_manifest_path: Path = Path("runtime/research/phase4/cohort-manifest.json"),
    split_manifest_path: Path = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json"),
    phase9_root: Path = PHASE9_ROOT,
) -> dict[str, Any]:
    summary_path = phase9_root / "phase9-summary.json"
    index_path = phase9_root / "phase9-artifact-index.json"
    phase9_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    phase9_index = json.loads(index_path.read_text(encoding="utf-8"))
    integrity = verify_artifact_index(phase9_root, phase9_index)
    if integrity["status"] != "PASS":
        raise ValueError("Phase 9 artifacts must verify before the fixed Phase 8 replay")
    if phase9_summary.get("research_platform") not in {"READY", "READY_WITH_LIMITATIONS"}:
        raise ValueError("Phase 8 replay requires the Phase 9 research platform gate")
    if phase9_summary.get("suspicious_gap_classifications", {}).get("ADJUSTMENT_MISMATCH", 0) < 1:
        raise ValueError("No supported price correction is available for a Phase 8 replay")

    cohort = json.loads(cohort_manifest_path.read_text(encoding="utf-8"))["cohort_60"]
    symbols = list(cohort["symbols"])
    market_by_symbol = market_by_cohort_order(symbols)
    split = json.loads(split_manifest_path.read_text(encoding="utf-8"))
    sessions = sorted({
        date.fromisoformat(item) for item in split["splits"]["development"]["sessions"]
    })
    if not sessions or sessions[0] != DEVELOPMENT_START or sessions[-1] != DEVELOPMENT_END:
        raise ValueError("Phase 8 replay requires the exact frozen Development interval")

    previous_hashes = _read_manifest_baseline(phase9_root / "previous-manifest-baseline.sha256")
    current_hashes = manifest_hashes(_previous_phase_manifest_paths())
    immutability_before = compare_manifest_hashes(previous_hashes, current_hashes)
    if immutability_before["status"] != "PASS":
        raise ValueError("A previous Phase manifest changed before the fixed replay")

    minute_by_symbol_session, minute_audit = audit_development_minute_cache(
        symbols=symbols,
        sessions=sessions,
        market_by_symbol=market_by_symbol,
        cache_root=Path("data"),
    )
    daily_cache = ParquetBarCache(DAILY_CACHE_ROOT)
    daily_by_symbol = {}
    daily_hashes = {}
    allowed_daily_dates = {date.fromordinal(DEVELOPMENT_START.toordinal() - 1), *sessions}
    for symbol in symbols:
        daily = tuple(daily_cache.load("daily", symbol, "1d-adjusted"))
        dates = {bar.time.astimezone(KST).date() for bar in daily}
        if not dates.issubset(allowed_daily_dates) or not set(sessions).issubset(dates):
            raise ValueError(f"Adjusted daily replay input is outside the safe interval for {symbol}")
        daily_by_symbol[symbol] = daily
        daily_hashes[symbol] = daily_cache.partition_path(
            "daily", symbol, "1d-adjusted"
        ).read_bytes()
        daily_hashes[symbol] = _sha256_bytes(daily_hashes[symbol])

    events, exclusions = phase8.build_opening_gap_events(
        minutes_by_symbol_session=minute_by_symbol_session,
        daily_by_symbol=daily_by_symbol,
        market_by_symbol=market_by_symbol,
        period_name="development",
    )
    phase8._daily_return_horizons(events, daily_by_symbol, sessions)

    # The frozen Phase 8 analyzers use this legacy label as a daily-source eligibility
    # sentinel. Keep their metric code unchanged, then publish the corrected source label.
    adjusted_events = _publish_adjusted_sources(events)

    meaningful = phase8._meaningful(events)
    source_split = phase8._prior_close_source_analysis(events)
    source_groups = source_split["groups"]
    if "KIS_DAILY_CLOSE_RAW" in source_groups:
        source_groups["KIS_DAILY_CLOSE_ADJUSTED_REPLAY"] = source_groups.pop("KIS_DAILY_CLOSE_RAW")
    source_split["source_semantics"]["KIS_DAILY_CLOSE_ADJUSTED_REPLAY"] = (
        "KIS daily close using FID_ORG_ADJ_PRC=0, compared in the fixed replay"
    )
    source_split["source_semantics"].pop("KIS_DAILY_CLOSE_RAW", None)
    source_split["primary_sensitivity_source"] = "KIS_DAILY_CLOSE_ADJUSTED_REPLAY"

    descriptive = {
        "gap_buckets": phase8._gap_bucket_analysis(events),
        "gap_fill": phase8._gap_fill_analysis(events),
        "opening_range": phase8._opening_range_analysis(events),
        "first_hour_states": phase8._first_hour_state_analysis(events),
        "market_split": phase8._split_analysis(events, "market"),
        "price_split": phase8._split_analysis(events, "market_price_bucket"),
        "liquidity_split": phase8._split_analysis(events, "liquidity_bucket"),
        "monthly_stability": phase8._monthly_stability(events),
        "matched_controls": phase8._matched_controls(events),
        "magnitude": phase8._magnitude_analysis(meaningful),
        "prior_close_source_split": source_split,
        "cost_stress": phase8._cost_stress(events),
        "swing_descriptive": phase8._swing_summary(events),
    }
    baseline_after = manifest_hashes(_previous_phase_manifest_paths())
    immutability_after = compare_manifest_hashes(previous_hashes, baseline_after)
    minute_hashes = minute_audit["verified_partition_sha256"]
    dataset_sha = _sha256_bytes(json.dumps({
        "daily_adjusted": daily_hashes,
        "minute": minute_hashes,
        "cohort": symbols,
        "development_sessions": [day.isoformat() for day in sessions],
        "phase8_config_sha": phase8._strategy_config_hash(),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    source_counts = Counter(item["prior_close_source"] for item in adjusted_events)
    replay = {
        "artifact": "phase8-data-corrected-replay",
        "status": "DATA_CORRECTED_REPLAY",
        "source_git_sha": phase8.current_git_sha(),
        "phase9_git_sha": phase9_summary["source_git_sha"],
        "phase8_code_sha256": phase8.sha256_file(Path(phase8.__file__)),
        "phase8_frozen_strategy_config_sha256": phase8._strategy_config_hash(),
        "dataset_sha256": dataset_sha,
        "cohort_sha256": phase9_summary["cohort_sha256"],
        "development_interval": [sessions[0].isoformat(), sessions[-1].isoformat()],
        "development_symbol_session_partitions": len(minute_by_symbol_session),
        "daily_price_convention": "KIS FID_ORG_ADJ_PRC=0 adjusted daily close; one session before Development retained only as a prior reference",
        "minute_price_adjustment": "UNKNOWN",
        "correction_basis": {
            "phase9_gap_events_ge_20_pct": phase9_summary["suspicious_gaps_ge_20_pct"],
            "phase9_gap_classifications": phase9_summary["suspicious_gap_classifications"],
            "open_alignment": phase9_summary["daily_minute_open_alignment"],
            "fixed_definitions_changed": False,
            "candidate_or_parameter_tuning": False,
        },
        "events": {
            "valid": len(adjusted_events),
            "meaningful_abs_gap_ge_1_pct": len(meaningful),
            "excluded": exclusions,
            "prior_close_source_counts": dict(source_counts),
        },
        "open_to_checkpoint_outcomes": {
            horizon: phase8._aggregate_events(events, horizon)
            for horizon in (*phase8.CHECKPOINT_MINUTES.keys(), "CONTINUOUS_FINAL")
        },
        "descriptive_metrics": descriptive,
        "secondary_analysis": "NOT_RUN",
        "external_validation": "NOT_RUN",
        "holdout_payload_or_metadata_reads": 0,
        "previous_phase_manifest_immutability": {
            "before": immutability_before["status"],
            "after": immutability_after["status"],
            "changed": immutability_after["changed"],
        },
        "phase8_evidence_integrity": "DEGRADED_BUT_USABLE",
        "alpha": "UNPROVEN",
        "live": "DISABLED",
    }
    if immutability_after["status"] != "PASS":
        raise ValueError("A previous Phase manifest changed during the fixed replay")

    event_path = phase9_root / "phase8-data-corrected-events.jsonl.gz"
    report_path = phase9_root / "phase8-data-corrected-replay.json"
    _write_events(event_path, adjusted_events)
    _write_json(report_path, replay)
    return replay


if __name__ == "__main__":
    result = run_phase8_data_corrected_replay()
    print(json.dumps({
        "status": result["status"],
        "events": result["events"],
        "previous_phase_manifest_immutability": result["previous_phase_manifest_immutability"],
        "dataset_sha256": result["dataset_sha256"],
    }, indent=2, sort_keys=True))

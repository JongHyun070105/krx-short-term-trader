"""Fail-closed analysis helpers for Phase 16C KIS response requalification.

This module keeps dated observations separate from other provider output objects.
It does not enable or collect prospective flow evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date
from typing import Any


def _parse_provider_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("-", "").replace("/", "")
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def _output_items(payload: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], int]:
    blocks: dict[str, list[dict[str, Any]]] = {}
    non_object_count = 0
    for name in ("output1", "output2", "output"):
        value = payload.get(name)
        if isinstance(value, dict):
            blocks[name] = [value]
        elif isinstance(value, list):
            blocks[name] = [item for item in value if isinstance(item, dict)]
            non_object_count += sum(not isinstance(item, dict) for item in value)
        elif value is not None:
            non_object_count += 1
    return blocks, non_object_count


def extract_observation_dates(
    payload: dict[str, Any], *, date_field: str
) -> dict[str, Any]:
    """Return date-bearing rows while retaining counts of undated output objects.

    KIS responses can expose several output blocks with different shapes. An
    undated object is evidence to classify, not a market observation to count.
    The schema fingerprint includes key names and block shapes only, never values.
    """
    blocks, non_object_count = _output_items(payload)
    dated_rows: list[tuple[str, dict[str, Any], date]] = []
    undated_by_block: dict[str, int] = {}
    shape: dict[str, list[list[str]]] = {}
    for block_name, rows in blocks.items():
        shape[block_name] = []
        for row in rows:
            keys = sorted(str(key) for key in row)
            shape[block_name].append(keys)
            parsed = _parse_provider_date(row.get(date_field))
            if parsed is None:
                undated_by_block[block_name] = undated_by_block.get(block_name, 0) + 1
            else:
                dated_rows.append((block_name, row, parsed))
    canonical_shape = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    return {
        "dated_rows": dated_rows,
        "sessions": [item[2] for item in dated_rows],
        "object_count": sum(len(rows) for rows in blocks.values()) + non_object_count,
        "dated_record_count": len(dated_rows),
        "undated_object_count": sum(undated_by_block.values()) + non_object_count,
        "undated_by_block": undated_by_block,
        "schema_fingerprint": hashlib.sha256(canonical_shape).hexdigest(),
        "block_object_counts": {name: len(rows) for name, rows in blocks.items()},
    }


def _is_covered(session: date, sessions: set[date], coverage: tuple[date, date] | None) -> bool | None:
    if coverage is None or not coverage[0] <= session <= coverage[1]:
        return None
    return session in sessions


def _same_trailing_window(
    actual_descending: list[date], candidate_sessions: list[date] | None, anchor: date
) -> bool | None:
    if candidate_sessions is None:
        return None
    eligible = sorted({session for session in candidate_sessions if session <= anchor})
    if len(eligible) < len(actual_descending):
        return False
    expected = list(reversed(eligible[-len(actual_descending) :])) if actual_descending else []
    return actual_descending == expected


def analyze_probe_response(
    payload: dict[str, Any],
    *,
    symbol: str,
    market: str,
    anchor: date,
    date_field: str,
    maximum_objects: int,
    anchor_expected: bool,
    market_sessions: list[date] | None,
    market_coverage: tuple[date, date] | None,
    symbol_price_sessions: list[date] | None = None,
    symbol_active_sessions: list[date] | None = None,
    protected_ranges: list[tuple[date, date, str]] | None = None,
    response_sha256: str | None = None,
) -> dict[str, Any]:
    extracted = extract_observation_dates(payload, date_field=date_field)
    sessions = extracted["sessions"]
    ordered = list(sessions)
    unique = set(sessions)
    failures: list[str] = []
    if extracted["object_count"] > maximum_objects:
        failures.append("ROW_COUNT_DRIFT")
    if not sessions and extracted["object_count"]:
        failures.append("SCHEMA_MISMATCH")
    if any(session > anchor for session in sessions):
        failures.append("DATE_NEWER_THAN_ANCHOR")
    if len(unique) != len(sessions):
        failures.append("DUPLICATE_SESSION")
    if anchor_expected and sessions and anchor not in unique:
        failures.append("ANCHOR_NOT_INCLUDED")

    ranges = protected_ranges or []
    protected_rows = sorted(
        {
            session.isoformat()
            for session in sessions
            for start, end, _label in ranges
            if start <= session <= end
        }
    )
    if protected_rows:
        failures.append("PROTECTED_RANGE_BREACH")

    market_values = sorted(set(market_sessions or []))
    market_set = set(market_values)
    if sessions and market_coverage:
        non_sessions = [
            session.isoformat()
            for session in sessions
            if market_coverage[0] <= session <= market_coverage[1] and session not in market_set
        ]
        if non_sessions:
            failures.append("NON_KRX_SESSION_DATE")
    else:
        non_sessions = []

    minimum = min(sessions) if sessions else None
    maximum = max(sessions) if sessions else None
    span_market_sessions: int | None = None
    missing_market_sessions: list[str] | None = None
    if minimum and maximum and market_coverage and market_coverage[0] <= minimum <= maximum <= market_coverage[1]:
        span_market = [session for session in market_values if minimum <= session <= maximum]
        span_market_sessions = len(span_market)
        missing_market_sessions = [session.isoformat() for session in span_market if session not in unique]

    price_count = None
    active_price_count = None
    zero_volume_price_dates: int | None = None
    if minimum and maximum and symbol_price_sessions is not None:
        price_set = set(symbol_price_sessions)
        price_count = sum(minimum <= session <= maximum for session in price_set)
        if symbol_active_sessions is not None:
            active_set = set(symbol_active_sessions)
            active_price_count = sum(minimum <= session <= maximum for session in active_set)
            zero_volume_price_dates = sum(
                session in price_set and session not in active_set for session in unique
            )

    count = len(sessions)
    session_descending = ordered == sorted(ordered, reverse=True)
    session_ascending = ordered == sorted(ordered)
    ordering = "DESCENDING" if session_descending else "ASCENDING" if session_ascending else "UNORDERED"
    if count <= 1:
        ordering = "SINGLE_OR_EMPTY"

    return {
        "symbol": symbol,
        "market": market,
        "requested_anchor": anchor.isoformat(),
        "row_count": extracted["object_count"],
        "dated_observation_count": count,
        "minimum_returned_date": minimum.isoformat() if minimum else None,
        "maximum_returned_date": maximum.isoformat() if maximum else None,
        "calendar_span_days": (maximum - minimum).days if minimum and maximum else None,
        "anchor_present": anchor in unique,
        "rows_newer_than_anchor": sum(session > anchor for session in sessions),
        "krx_session_membership": [
            {"date": session.isoformat(), "is_krx_session": _is_covered(session, market_set, market_coverage)}
            for session in sessions
        ],
        "krx_sessions_spanned": span_market_sessions,
        "symbol_price_observations_spanned": price_count,
        "nonzero_volume_price_observations_spanned": active_price_count,
        "returned_dates_with_zero_volume_price_rows": zero_volume_price_dates,
        "missing_krx_sessions_inside_return_span": missing_market_sessions,
        "ordering": ordering,
        "duplicate_dates": count - len(unique),
        "undated_object_count": extracted["undated_object_count"],
        "undated_by_output_block": extracted["undated_by_block"],
        "output_block_object_counts": extracted["block_object_counts"],
        "schema_fingerprint": extracted["schema_fingerprint"],
        "response_sha256": response_sha256,
        "protected_rows": protected_rows,
        "failures": failures,
        "contract_result": "CONTRACT_MISMATCH" if failures else "EMPIRICALLY_MATCHED",
        "last_n_krx_sessions_match": _same_trailing_window(sessions, market_sessions, anchor),
        "last_n_symbol_price_rows_match": _same_trailing_window(sessions, symbol_price_sessions, anchor),
        "last_n_nonzero_volume_price_rows_match": _same_trailing_window(sessions, symbol_active_sessions, anchor),
    }


def reconstruct_incident(
    event: dict[str, Any],
    *,
    weekday_expected_start: date,
    krx_expected_start_for_recorded_rows: date,
    active_contract: dict[str, Any],
    active_config_sha256: str,
    audited_collector_git_sha: str,
    market_context: str,
) -> dict[str, Any]:
    check = event.get("response_check", {})
    stored_dates = check.get("returned_sessions")
    recoverable = isinstance(stored_dates, list) and all(_parse_provider_date(x) for x in stored_dates)
    dates = [date.fromisoformat(x) for x in stored_dates] if recoverable else []
    return {
        "schema_version": 1,
        "incident_id": "phase16b-kis-per-stock-2025-06-02",
        "event_type": event.get("event_type"),
        "source_id": event.get("source_id"),
        "symbol": event.get("symbol"),
        "market_recorded_in_event": None,
        "market_from_frozen_historical_probe_policy": market_context,
        "requested_anchor": event.get("anchor"),
        "effective_anchor_used_by_validator": check.get("request_anchor"),
        "expected_earliest_session_by_incident_calendar": weekday_expected_start.isoformat(),
        "expected_latest_session_by_incident_calendar": check.get("request_anchor"),
        "reference_earliest_if_using_krx_sessions_for_recorded_dated_rows": krx_expected_start_for_recorded_rows.isoformat(),
        "actual_earliest_returned_session": min(dates).isoformat() if dates else None,
        "actual_latest_returned_session": max(dates).isoformat() if dates else None,
        "incident_date_detail": "RECOVERABLE" if recoverable else "NOT_RECOVERABLE_FROM_STORED_EVIDENCE",
        "row_count_recorded": check.get("row_count"),
        "dated_session_count_recorded": len(dates) if recoverable else None,
        "undated_or_unparseable_object_count": (check.get("row_count", 0) - len(dates)) if recoverable else None,
        "ordering": "DESCENDING" if recoverable and dates == sorted(dates, reverse=True) else "UNKNOWN",
        "duplicate_dates": len(dates) - len(set(dates)) if recoverable else None,
        "validation_failures": list(check.get("failures", [])),
        "validation_code_path": {
            "date_and_envelope_validator": "krx_trader.research.phase16b.verify_response_contract",
            "response_object_flattener": "krx_trader.research.phase16b.extract_dated_rows",
            "historical_calendar_builder": "krx_trader.research.phase16b._historical_probe_calendar",
            "envelope_builder": "krx_trader.research.phase16b.possible_response_session_range",
        },
        "root_cause": {
            "calendar": "Historical probe calendar contains every weekday and no dated KRX closures. For 31 objects including the anchor this put the earliest expected date at 2025-04-21, later than the recorded 2025-04-17 session.",
            "response_shape": "The validator flattened output1/output2/output objects together. Stored metadata has 31 extracted objects but only 30 parseable session dates, so one object had no valid stck_bsop_date. The raw payload was not persisted, so its output block cannot be identified from the incident alone.",
            "symbol_gap": "The 30 stored dates are consecutive local KRX index sessions for the incident span; this incident does not show a symbol-specific observation gap.",
        },
        "protected_rows_recorded": check.get("protected_rows", []),
        "payload_persisted": event.get("payload_persisted"),
        "response_sha256": event.get("response_sha256"),
        "request_hash": "NOT_RETAINED",
        "transport_metadata": {
            "request_count": 1,
            "request_count_basis": "user-provided incident context; not in the violation JSON",
            "retry_count": None,
            "http_status": None,
            "duration_ms": None,
            "provider_request_id": None,
            "none_of_the_unrecorded_fields_were_inferred": True,
        },
        "incident_contract_version_recorded": None,
        "active_contract_context": active_contract,
        "active_config_sha256_context_only": active_config_sha256,
        "collector_git_sha_recorded": None,
        "audited_collector_git_sha_context_only": audited_collector_git_sha,
        "schema_fingerprint_recorded": None,
    }


def finite_pre_network_bound(
    *,
    maximum_observations: int | None,
    maximum_is_provider_documented: bool,
    maximum_missing_sessions_between_observations: int | None,
) -> dict[str, Any]:
    if not maximum_is_provider_documented or maximum_observations is None:
        return {
            "proven": False,
            "bound_sessions": None,
            "reason": "No provider-documented finite observation cap is available.",
        }
    if maximum_missing_sessions_between_observations is None:
        return {
            "proven": False,
            "bound_sessions": None,
            "reason": "A row cap alone does not bound the date span without a proven maximum gap between observations.",
        }
    if maximum_observations < 1 or maximum_missing_sessions_between_observations < 0:
        raise ValueError("observation cap and gap bound are outside their valid ranges")
    bound = maximum_observations + (maximum_observations - 1) * maximum_missing_sessions_between_observations
    return {
        "proven": True,
        "bound_sessions": bound,
        "reason": "Provider observation cap and maximum missing-session gap are both bounded before the request.",
    }


def preflight_old_probe_anchor(
    anchor: date,
    *,
    protected_ranges: list[tuple[date, date, str]],
    upper_anchor_semantics_previously_observed: bool,
) -> dict[str, Any]:
    earliest_protected = min(start for start, _end, _label in protected_ranges)
    allowed = upper_anchor_semantics_previously_observed and anchor < earliest_protected
    return {
        "allowed": allowed,
        "request_decision": "ALLOW_OLD_BACKWARD_ANCHOR" if allowed else "DENY_UNSAFE_OLD_ANCHOR",
        "anchor": anchor.isoformat(),
        "latest_possible_date_basis": "request anchor; prior empirical upper-anchor evidence" if allowed else None,
        "earliest_possible_date": None,
        "reason": "Old diagnostic anchor precedes every protected period; the lower trailing boundary remains unknown." if allowed else "Anchor is protected or upper-anchor behavior lacks prior evidence.",
    }


def preflight_prospective_request(
    anchor: date,
    *,
    protected_ranges: list[tuple[date, date, str]],
    finite_bound: dict[str, Any],
    market_sessions: list[date] | None = None,
    market_coverage: tuple[date, date] | None = None,
) -> dict[str, Any]:
    if not finite_bound.get("proven"):
        return {
            "allowed": False,
            "request_decision": "DEFERRED_RESPONSE_CONTRACT",
            "anchor": anchor.isoformat(),
            "earliest_possible_date": None,
            "reason": "No finite pre-network response envelope is proven.",
        }
    bound_sessions = finite_bound.get("bound_sessions")
    if (
        not isinstance(bound_sessions, int)
        or bound_sessions < 1
        or market_sessions is None
        or market_coverage is None
        or not market_coverage[0] <= anchor <= market_coverage[1]
    ):
        return {
            "allowed": False,
            "request_decision": "DEFERRED_RESPONSE_CONTRACT",
            "anchor": anchor.isoformat(),
            "earliest_possible_date": None,
            "reason": "A complete, exact-session calendar is required before transport.",
        }
    eligible = sorted({session for session in market_sessions if session <= anchor})
    if anchor not in eligible or len(eligible) < bound_sessions:
        return {
            "allowed": False,
            "request_decision": "DEFERRED_RESPONSE_CONTRACT",
            "anchor": anchor.isoformat(),
            "earliest_possible_date": None,
            "reason": "Anchor session or complete bounded lookback is unavailable before transport.",
        }
    earliest = eligible[-bound_sessions]
    if earliest < market_coverage[0]:
        return {
            "allowed": False,
            "request_decision": "DEFERRED_RESPONSE_CONTRACT",
            "anchor": anchor.isoformat(),
            "earliest_possible_date": earliest.isoformat(),
            "reason": "The calendar does not cover the complete bounded response envelope.",
        }
    overlaps = [label for start, end, label in protected_ranges if earliest <= end and anchor >= start]
    if overlaps:
        return {
            "allowed": False,
            "request_decision": "DEFERRED_RESPONSE_CONTRACT",
            "anchor": anchor.isoformat(),
            "earliest_possible_date": earliest.isoformat(),
            "latest_possible_date": anchor.isoformat(),
            "protected_ranges_touched": overlaps,
            "reason": "The complete bounded response envelope intersects protected data.",
        }
    return {
        "allowed": True,
        "request_decision": "ALLOW_PROVEN_BOUNDED_ENVELOPE",
        "anchor": anchor.isoformat(),
        "earliest_possible_date": earliest.isoformat(),
        "latest_possible_date": anchor.isoformat(),
        "protected_ranges_touched": [],
        "reason": "A documented finite response bound fits a fully covered session calendar and avoids protected ranges.",
    }


def observation_window(
    sessions: list[date], *, anchor: date, count: int
) -> dict[str, Any]:
    eligible = sorted({session for session in sessions if session <= anchor})
    selected = eligible[-count:]
    return {
        "requested_count": count,
        "observation_count": len(selected),
        "earliest_session": selected[0].isoformat() if selected else None,
        "latest_session": selected[-1].isoformat() if selected else None,
        "calendar_span_days": (selected[-1] - selected[0]).days if len(selected) > 1 else 0,
    }


def compare_candidate_models(probes: list[dict[str, Any]]) -> dict[str, Any]:
    model_specs = {
        "MODEL_A_LAST_N_KRX_SESSIONS": "Dated rows equal the N latest covered KRX sessions at or before anchor.",
        "MODEL_B_LAST_N_SYMBOL_PRICE_OBSERVATIONS": "Dated rows equal the N latest cached daily price rows for the symbol.",
        "MODEL_C_CALENDAR_WINDOW": "Dated rows fit a 31-calendar-day trailing window.",
        "MODEL_D_PROVIDER_SPECIFIC_BOUNDED_WINDOW": "Observed rows fit the empirical 31-object one-page sample; no vendor guarantee is implied.",
        "MODEL_E_NO_SAFE_FINITE_BOUND": "The finite sample does not establish an advance bound on count and observation gaps.",
    }
    counts = {name: {"explained": 0, "contradicted": 0, "not_assessable": 0} for name in model_specs}
    for probe in probes:
        if probe.get("last_n_krx_sessions_match") is None:
            counts["MODEL_A_LAST_N_KRX_SESSIONS"]["not_assessable"] += 1
        elif probe["last_n_krx_sessions_match"]:
            counts["MODEL_A_LAST_N_KRX_SESSIONS"]["explained"] += 1
        else:
            counts["MODEL_A_LAST_N_KRX_SESSIONS"]["contradicted"] += 1
        if probe.get("last_n_symbol_price_rows_match") is None:
            counts["MODEL_B_LAST_N_SYMBOL_PRICE_OBSERVATIONS"]["not_assessable"] += 1
        elif probe["last_n_symbol_price_rows_match"]:
            counts["MODEL_B_LAST_N_SYMBOL_PRICE_OBSERVATIONS"]["explained"] += 1
        else:
            counts["MODEL_B_LAST_N_SYMBOL_PRICE_OBSERVATIONS"]["contradicted"] += 1
        span = probe.get("calendar_span_days")
        if span is None:
            counts["MODEL_C_CALENDAR_WINDOW"]["not_assessable"] += 1
        elif span <= 31 and probe.get("rows_newer_than_anchor", 1) == 0:
            counts["MODEL_C_CALENDAR_WINDOW"]["explained"] += 1
        else:
            counts["MODEL_C_CALENDAR_WINDOW"]["contradicted"] += 1
        if probe.get("row_count", 0) <= 31 and probe.get("rows_newer_than_anchor", 1) == 0:
            counts["MODEL_D_PROVIDER_SPECIFIC_BOUNDED_WINDOW"]["explained"] += 1
        else:
            counts["MODEL_D_PROVIDER_SPECIFIC_BOUNDED_WINDOW"]["contradicted"] += 1
        # MODEL_E is a pre-network safety conclusion, not a response-fit model.
        counts["MODEL_E_NO_SAFE_FINITE_BOUND"]["not_assessable"] += 1
    spans = [
        probe["calendar_span_days"]
        for probe in probes
        if isinstance(probe.get("calendar_span_days"), int)
    ]
    return {
        "models": [
            {
                "model": name,
                "description": model_specs[name],
                **counts[name],
                "worst_observed_calendar_span_days": max(spans) if spans else None,
                "confidence": "LOW_OR_UNPROVEN" if name in {"MODEL_A_LAST_N_KRX_SESSIONS", "MODEL_B_LAST_N_SYMBOL_PRICE_OBSERVATIONS"} else "OBSERVATIONAL_ONLY",
            }
            for name in model_specs
        ],
        "responses_compared": len(probes),
        "interpretation_limit": "Observational fit across a finite sample does not prove a vendor contract or a safe pre-network lower bound.",
    }


def can_clear_contract_hold(evidence: dict[str, Any]) -> bool:
    required = (
        "root_cause_identified",
        "all_probes_explained",
        "no_contradictory_probes",
        "finite_pre_network_bound_proven",
        "protected_guard_verified",
        "original_failure_regression_covered",
        "documentation_updated",
        "contract_version_updated",
    )
    return all(evidence.get(name) is True for name in required)


def first_safe_anchor_or_unknown(
    *, finite_bound: dict[str, Any], compute_anchor: Callable[[], date | None]
) -> date | None:
    if not finite_bound.get("proven"):
        return None
    return compute_anchor()

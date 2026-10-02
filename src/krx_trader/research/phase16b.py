from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from http.cookiejar import CookieJar
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

from krx_trader.kis.auth import KisAuthError
from krx_trader.kis.rest import KisApiError
from krx_trader.research import phase16

KST = ZoneInfo("Asia/Seoul")
DEFAULT_PHASE16B_CONFIG = phase16.REPO_ROOT / "config/phase16/phase16b-collector-config-phase16b-v4.json"
DEFAULT_PHASE16B_ROOT = phase16.REPO_ROOT / "runtime/research/phase16b"
CONTRACT_CONFIDENCE = {"DOCUMENTED", "EMPIRICALLY_VERIFIED", "PARTIAL", "UNKNOWN"}
RESPONSE_DIRECTIONS = {"BACKWARD", "EXACT", "FORWARD", "UNKNOWN"}
ACTIVATION_STATES = {
    "CONTRACT_UNKNOWN",
    "CONTRACT_PARTIAL",
    "WAITING_FOR_SAFE_DATE",
    "READY",
    "ACTIVE",
    "DEFERRED_RESPONSE_CONTRACT",
    "DEFERRED_PROVIDER_POLICY",
    "ERROR",
}
PROTECTED_RANGE_LABELS = {"EXTERNAL_2026", "HOLDOUT_2026"}
PHASE16B_EVIDENCE_CLASSES = {
    "CONTRACT_VERIFICATION_PROBE",
    "PROSPECTIVE_SESSION_OBSERVED",
    "HISTORICAL_SESSION_REOBSERVED",
    "HISTORICAL_REVISION_PROBE",
}


class ContractError(phase16.Phase16Error):
    pass


@dataclass(frozen=True, slots=True)
class SourceResponseContract:
    source_name: str
    endpoint_id: str
    request_anchor_semantics: str
    response_direction: str
    maximum_rows: int | None
    date_field: str
    anchor_included: bool | None
    session_only: bool | None
    ordering: str
    future_rows_allowed: bool
    response_window_verified: bool
    verification_method: str
    verification_observation_count: int
    earliest_contract_probe: str | None
    latest_contract_probe: str | None
    contract_version: str
    confidence: str
    notes: str
    non_session_behavior: str = "UNKNOWN"
    source_id: str = ""

    def __post_init__(self) -> None:
        if self.response_direction not in RESPONSE_DIRECTIONS:
            raise ValueError("unsupported response_direction")
        if self.confidence not in CONTRACT_CONFIDENCE:
            raise ValueError("unsupported contract confidence")
        if self.maximum_rows is not None and self.maximum_rows < 1:
            raise ValueError("maximum_rows must be positive or null")
        if self.verification_observation_count < 0:
            raise ValueError("verification_observation_count must not be negative")
        if not self.contract_version.strip():
            raise ValueError("contract_version is required")
        for value in (self.earliest_contract_probe, self.latest_contract_probe):
            if value is not None:
                date.fromisoformat(value)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceResponseContract:
        return cls(**value)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def sufficient_for_preflight(self) -> bool:
        return bool(
            self.confidence in {"DOCUMENTED", "EMPIRICALLY_VERIFIED"}
            and self.response_window_verified
            and self.response_direction in {"BACKWARD", "EXACT"}
            and self.maximum_rows is not None
            and self.date_field
            and self.future_rows_allowed is False
            and self.anchor_included is not None
            and self.session_only is True
            and self.request_anchor_semantics in {"DATE_UPPER_BOUND", "EXACT_DATE"}
        )


def versioned_contract_update(
    contract: SourceResponseContract,
    *,
    new_version: str,
    changes: dict[str, Any],
) -> SourceResponseContract:
    critical = {
        "maximum_rows",
        "request_anchor_semantics",
        "response_direction",
        "date_field",
        "anchor_included",
        "session_only",
        "ordering",
        "future_rows_allowed",
        "non_session_behavior",
    }
    if critical.intersection(changes) and new_version == contract.contract_version:
        raise ValueError("response-contract behavior changes require a new contract_version")
    return replace(contract, **changes, contract_version=new_version)


@dataclass(frozen=True, slots=True)
class KRXSessionCalendar:
    sessions: tuple[date, ...]
    coverage_start: date
    coverage_end: date
    source_urls: tuple[str, ...]
    calendar_version: str

    def __post_init__(self) -> None:
        if self.coverage_start > self.coverage_end:
            raise ValueError("calendar coverage is reversed")
        if tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("calendar sessions must be unique and ascending")
        if any(day < self.coverage_start or day > self.coverage_end for day in self.sessions):
            raise ValueError("session lies outside declared calendar coverage")

    def covers(self, start: date, end: date) -> bool:
        return self.coverage_start <= start <= end <= self.coverage_end

    def index(self, session: date) -> int | None:
        try:
            return self.sessions.index(session)
        except ValueError:
            return None


def build_weekday_calendar(
    start: date,
    end: date,
    *,
    official_closures: set[date],
    source_urls: tuple[str, ...],
    calendar_version: str,
) -> KRXSessionCalendar:
    if start > end:
        raise ValueError("calendar range is reversed")
    sessions = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor not in official_closures:
            sessions.append(cursor)
        cursor += timedelta(days=1)
    return KRXSessionCalendar(tuple(sessions), start, end, source_urls, calendar_version)


def possible_response_session_range(
    source_contract: SourceResponseContract,
    request_anchor: date,
    *,
    calendar: KRXSessionCalendar,
) -> dict[str, Any]:
    if not source_contract.sufficient_for_preflight:
        raise ContractError("response contract is insufficient to compute a safe envelope")
    if not calendar.covers(calendar.coverage_start, request_anchor):
        raise ContractError("session calendar does not cover request anchor")
    anchor_index = calendar.index(request_anchor)
    effective_anchor = request_anchor
    if anchor_index is None:
        if source_contract.non_session_behavior != "PREVIOUS_SESSION_FALLBACK":
            raise ContractError("request anchor is not a verified KRX session")
        prior = [idx for idx, session in enumerate(calendar.sessions) if session < request_anchor]
        if not prior:
            raise ContractError("calendar has no verified previous session")
        anchor_index = prior[-1]
        effective_anchor = calendar.sessions[anchor_index]
    assert source_contract.maximum_rows is not None
    count = source_contract.maximum_rows
    if source_contract.response_direction == "EXACT":
        earliest_index = anchor_index
    else:
        lookback = count - 1 if source_contract.anchor_included else count
        earliest_index = anchor_index - lookback
    if earliest_index < 0:
        raise ContractError("session calendar does not provide the complete response lookback")
    earliest = calendar.sessions[earliest_index]
    latest = calendar.sessions[min(len(calendar.sessions) - 1, anchor_index + count - 1)] if source_contract.response_direction == "FORWARD" else effective_anchor
    return {
        "effective_anchor_session": effective_anchor.isoformat(),
        "earliest_possible_session": earliest.isoformat(),
        "latest_possible_session": latest.isoformat(),
        "maximum_session_count": count,
        "confidence": source_contract.confidence,
        "calendar_version": calendar.calendar_version,
    }


def _overlaps(start: date, end: date, protected_ranges: list[tuple[date, date, str]]) -> list[str]:
    return [label for p_start, p_end, label in protected_ranges if start <= p_end and end >= p_start]


def first_safe_anchor_session(
    source_contract: SourceResponseContract,
    *,
    calendar: KRXSessionCalendar,
    protected_ranges: list[tuple[date, date, str]],
) -> date | None:
    if not source_contract.sufficient_for_preflight:
        return None
    last_protected = max(end for _, end, _ in protected_ranges)
    for anchor in calendar.sessions:
        if anchor <= last_protected:
            continue
        try:
            envelope = possible_response_session_range(source_contract, anchor, calendar=calendar)
        except ContractError:
            continue
        start = date.fromisoformat(envelope["earliest_possible_session"])
        end = date.fromisoformat(envelope["latest_possible_session"])
        if not _overlaps(start, end, protected_ranges):
            return anchor
    return None


def request_contract_guard(
    source_contract: SourceResponseContract,
    request_anchor: date,
    *,
    calendar: KRXSessionCalendar,
    protected_ranges: list[tuple[date, date, str]],
    evidence_class: str = "PROSPECTIVE_SESSION_OBSERVED",
) -> dict[str, Any]:
    if evidence_class not in PHASE16B_EVIDENCE_CLASSES:
        return {"allowed": False, "request_decision": "DENY_INVALID_EVIDENCE_CLASS", "reason": "unsupported evidence class"}
    if not source_contract.sufficient_for_preflight:
        return {"allowed": False, "request_decision": "DEFERRED_RESPONSE_CONTRACT", "reason": "response contract is not sufficient"}
    try:
        envelope = possible_response_session_range(source_contract, request_anchor, calendar=calendar)
    except ContractError as exc:
        return {
            "allowed": False,
            "request_decision": "DENY_UNVERIFIED_SESSION",
            "request_anchor": request_anchor.isoformat(),
            "reason": str(exc),
        }
    start = date.fromisoformat(envelope["earliest_possible_session"])
    end = date.fromisoformat(envelope["latest_possible_session"])
    overlaps = _overlaps(start, end, protected_ranges)
    request_in_protected = _overlaps(request_anchor, request_anchor, protected_ranges)
    allowed = not overlaps and not request_in_protected and source_contract.future_rows_allowed is False
    return {
        "allowed": allowed,
        "request_decision": "ALLOW" if allowed else "DENY_PROTECTED_RANGE",
        "source_id": source_contract.source_id,
        "contract_version": source_contract.contract_version,
        "request_anchor": request_anchor.isoformat(),
        "envelope": envelope,
        "protected_overlaps": sorted(set(overlaps + request_in_protected)),
        "evidence_class": evidence_class,
    }


def execute_contract_guarded_request(
    source_contract: SourceResponseContract,
    request_anchor: date,
    *,
    calendar: KRXSessionCalendar,
    protected_ranges: list[tuple[date, date, str]],
    transport: Callable[[], Any],
    evidence_class: str = "PROSPECTIVE_SESSION_OBSERVED",
) -> Any:
    decision = request_contract_guard(
        source_contract,
        request_anchor,
        calendar=calendar,
        protected_ranges=protected_ranges,
        evidence_class=evidence_class,
    )
    if not decision["allowed"]:
        raise ContractError(
            f"request denied before transport: {decision['request_decision']} ({decision.get('reason', decision.get('protected_overlaps'))})"
        )
    return transport()


def activation_state(
    source_contract: SourceResponseContract,
    *,
    first_safe_anchor: date | None,
    latest_completed_session: date | None,
    credentials_available: bool,
    collector_healthy: bool,
    auto_activation: bool,
    provider_policy_allowed: bool = True,
    contract_violation: bool = False,
) -> dict[str, Any]:
    if contract_violation:
        state, reason = "ERROR", "CONTRACT_REVIEW_REQUIRED"
    elif not provider_policy_allowed:
        state, reason = "DEFERRED_PROVIDER_POLICY", "provider policy does not permit collection"
    elif source_contract.confidence == "UNKNOWN":
        state, reason = "CONTRACT_UNKNOWN", "no response-window evidence"
    elif not source_contract.sufficient_for_preflight:
        state, reason = "DEFERRED_RESPONSE_CONTRACT", "response-window contract is insufficient"
    elif first_safe_anchor is None:
        state, reason = "WAITING_FOR_SAFE_DATE", "complete session calendar is unavailable"
    elif latest_completed_session is None:
        state, reason = "WAITING_FOR_SAFE_DATE", "NO_COMPLETED_MARKET_SESSION"
    elif latest_completed_session < first_safe_anchor:
        state, reason = "WAITING_FOR_SAFE_DATE", "latest completed session is before first safe anchor"
    elif not credentials_available:
        state, reason = "READY", "safe anchor reached; credentials unavailable"
    elif not collector_healthy:
        state, reason = "READY", "safe anchor reached; collector health is not valid"
    elif not auto_activation:
        state, reason = "READY", "automatic activation is disabled"
    else:
        state, reason = "ACTIVE", "contract, safe anchor, credentials, health, and policy pass"
    assert state in ACTIVATION_STATES
    return {
        "state": state,
        "reason": reason,
        "first_safe_anchor_session": first_safe_anchor.isoformat() if first_safe_anchor else None,
        "latest_completed_session": latest_completed_session.isoformat() if latest_completed_session else None,
    }


def latest_completed_session(calendar: KRXSessionCalendar, now: datetime, *, close_after: wall_time = wall_time(15, 30)) -> date | None:
    local = now.astimezone(KST)
    if not calendar.coverage_start <= local.date() <= calendar.coverage_end:
        return None
    candidates = [session for session in calendar.sessions if session < local.date()]
    if local.date() in calendar.sessions and local.timetz().replace(tzinfo=None) >= close_after:
        candidates.append(local.date())
    return max(candidates) if candidates else None


def extract_dated_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("output1", "output2", "output"):
        block = payload.get(key)
        if isinstance(block, dict):
            rows.append(block)
        elif isinstance(block, list):
            rows.extend(row for row in block if isinstance(row, dict))
    return rows


def _parse_provider_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("-", "").replace("/", "")
    if not re.fullmatch(r"\d{8}", raw):
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def verify_response_contract(
    payload: dict[str, Any],
    source_contract: SourceResponseContract,
    *,
    request_anchor: date,
    calendar: KRXSessionCalendar,
    protected_ranges: list[tuple[date, date, str]],
) -> dict[str, Any]:
    rows = extract_dated_rows(payload)
    dates = [_parse_provider_date(row.get(source_contract.date_field)) for row in rows]
    failures: list[str] = []
    if any(day is None for day in dates):
        failures.append("DATE_FIELD_INVALID_OR_MISSING")
    parsed = [day for day in dates if day is not None]
    if source_contract.maximum_rows is not None and len(rows) > source_contract.maximum_rows:
        failures.append("MAXIMUM_ROWS_EXCEEDED")
    if source_contract.sufficient_for_preflight:
        try:
            envelope = possible_response_session_range(source_contract, request_anchor, calendar=calendar)
        except ContractError:
            failures.append("RESPONSE_ENVELOPE_UNVERIFIABLE")
        else:
            earliest = date.fromisoformat(envelope["earliest_possible_session"])
            latest = date.fromisoformat(envelope["latest_possible_session"])
            if any(day < earliest or day > latest for day in parsed):
                failures.append("ROW_OUTSIDE_VERIFIED_RESPONSE_ENVELOPE")
    if source_contract.response_direction in {"BACKWARD", "EXACT"} and any(day > request_anchor for day in parsed):
        failures.append("FUTURE_ROW_AFTER_ANCHOR")
    if source_contract.response_direction == "EXACT" and any(day != request_anchor for day in parsed):
        failures.append("ROW_OUTSIDE_EXACT_ANCHOR")
    if source_contract.response_direction == "EXACT" and not rows:
        failures.append("EXACT_ANCHOR_ROW_MISSING")
    if source_contract.anchor_included and rows and request_anchor not in parsed:
        failures.append("ANCHOR_ROW_MISSING")
    if source_contract.anchor_included is False and request_anchor in parsed:
        failures.append("ANCHOR_ROW_UNEXPECTED")
    if source_contract.session_only and any(calendar.index(day) is None for day in parsed):
        failures.append("NON_SESSION_RESPONSE_ROW")
    if source_contract.ordering == "ASCENDING" and parsed != sorted(parsed):
        failures.append("ORDERING_CHANGED")
    if source_contract.ordering == "DESCENDING" and parsed != sorted(parsed, reverse=True):
        failures.append("ORDERING_CHANGED")
    protected_rows = [day.isoformat() for day in parsed if _overlaps(day, day, protected_ranges)]
    if protected_rows:
        failures.append("PROTECTED_RANGE_CONTRACT_BREACH")
    return {
        "valid": not failures,
        "state": "VALID" if not failures else "SOURCE_CONTRACT_VIOLATION",
        "request_anchor": request_anchor.isoformat(),
        "row_count": len(rows),
        "returned_sessions": [day.isoformat() for day in parsed],
        "protected_rows": protected_rows,
        "failures": failures,
    }


def classify_session_row(source_name: str, row: dict[str, Any], *, prospective_start: date) -> str:
    session = _parse_provider_date(row.get("provider_session"))
    if session is None:
        raise ValueError("normalized row is missing provider_session")
    return "PROSPECTIVE_SESSION_OBSERVED" if session >= prospective_start else "HISTORICAL_SESSION_REOBSERVED"


def normalize_dated_rows(
    payload: dict[str, Any],
    *,
    contract: SourceResponseContract,
    source_name: str,
    symbol: str,
    market: str | None,
    response_snapshot_id: str,
    request_anchor: date,
    observed_at: datetime,
    prospective_start: date,
    row_evidence_class: str | None = None,
) -> list[dict[str, Any]]:
    utc, local = phase16.timezone_pair(observed_at)
    normalized = []
    for row in extract_dated_rows(payload):
        session = _parse_provider_date(row.get(contract.date_field))
        if session is None:
            continue
        values = {str(k): v for k, v in row.items() if str(k) != contract.date_field}
        fields = [
            {
                "provider_field": name,
                "semantic_label": None,
                "semantic_status": "UNKNOWN",
                "unit": None,
                "unit_status": "UNKNOWN",
                "value": value,
                "feature_status": "QUARANTINED_PROSPECTIVE",
            }
            for name, value in sorted(values.items())
        ]
        record = {
            "logical_key": f"{source_name}:{symbol}:{session.isoformat()}",
            "source_name": source_name,
            "symbol": symbol,
            "market": market,
            "provider_session": session.isoformat(),
            "request_anchor": request_anchor.isoformat(),
            "observed_at_utc": utc,
            "observed_at_kst": local,
            "response_snapshot_id": response_snapshot_id,
            "evidence_class": row_evidence_class or ("PROSPECTIVE_SESSION_OBSERVED" if session >= prospective_start else "HISTORICAL_SESSION_REOBSERVED"),
            "provider_values": values,
            "provider_schema_fingerprint": phase16.schema_fingerprint(row),
            "fields": fields,
        }
        normalized.append(record)
    return normalized


def _row_values(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("provider_values")
    return values if isinstance(values, dict) else {}


def _snapshot_id_for_payload(
    *,
    source_name: str,
    logical_key: str,
    symbol: str,
    requested_session: date,
    observed_at: datetime,
    payload: dict[str, Any],
    raw_payload: bytes,
) -> str:
    safe_payload, _ = phase16.sanitize_payload(payload)
    safe_raw, _ = phase16.sanitize_received_bytes(raw_payload, persist=True)
    return phase16.make_snapshot_id(
        source_name=source_name,
        logical_key=logical_key,
        symbol=symbol,
        requested_session=requested_session,
        observed_at=observed_at,
        canonical_sha256=phase16.canonical_payload_sha256(safe_payload),
        raw_sha256=hashlib.sha256(safe_raw).hexdigest(),
    )


def classify_row_revision(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    response_hash_changed: bool,
    canonical_response_hash_changed: bool | None = None,
) -> dict[str, Any]:
    if old.get("row_present") is False and new.get("row_present") is True:
        classification = "MISSING_TO_PRESENT"
    elif old.get("row_present") is True and new.get("row_present") is False:
        classification = "PRESENT_TO_MISSING"
    else:
        classification = ""
    old_values, new_values = _row_values(old), _row_values(new)
    old_keys, new_keys = set(old_values), set(new_values)
    added, removed = sorted(new_keys - old_keys), sorted(old_keys - new_keys)
    changed = sorted(key for key in old_keys & new_keys if old_values[key] != new_values[key])
    if not classification and added and removed:
        classification = "SCHEMA_CHANGED"
    elif not classification and added and not changed:
        classification = "FIELD_ADDED"
    elif not classification and removed and not added and not changed:
        classification = "FIELD_REMOVED"
    elif not classification and changed:
        classification = "VALUE_REVISED"
    elif not classification and old.get("provider_schema_fingerprint") and new.get("provider_schema_fingerprint") and old.get("provider_schema_fingerprint") != new.get("provider_schema_fingerprint"):
        classification = "SCHEMA_CHANGED"
    elif not classification and not old_values and not new_values:
        classification = "UNCOMPARABLE"
    elif not classification and response_hash_changed:
        classification = "SEMANTICALLY_IDENTICAL"
    elif not classification:
        classification = "IDENTICAL"
    return {
        "logical_key": old["logical_key"],
        "earlier_response_snapshot_id": old["response_snapshot_id"],
        "later_response_snapshot_id": new["response_snapshot_id"],
        "earlier_observed_at_utc": old["observed_at_utc"],
        "later_observed_at_utc": new["observed_at_utc"],
        "response_level_hash_changed": response_hash_changed,
        "canonical_response_hash_changed": canonical_response_hash_changed,
        "classification": classification,
        "changed_fields": changed,
        "added_fields": added,
        "removed_fields": removed,
        "value_changes": {
            key: {"old": old_values[key], "new": new_values[key]}
            for key in changed
        },
    }


def field_revision_profile(records: list[dict[str, Any]], comparisons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    field_changes: dict[str, list[tuple[float, float | None]]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    revisions: dict[str, int] = defaultdict(int)
    for old, new in pairwise(records):
        old_values, new_values = _row_values(old), _row_values(new)
        for name in set(old_values) | set(new_values):
            counts[name] += 1
            if name not in old_values or name not in new_values or old_values[name] == new_values[name]:
                revisions[name] += int(name not in old_values or name not in new_values)
                continue
            revisions[name] += 1
            try:
                old_num, new_num = float(old_values[name]), float(new_values[name])
            except (TypeError, ValueError):
                continue
            absolute = abs(new_num - old_num)
            relative = absolute / abs(old_num) if old_num else None
            field_changes[name].append((absolute, relative))
    result = []
    for name in sorted(counts):
        values = field_changes.get(name, [])
        result.append({
            "field": name,
            "comparison_count": counts[name],
            "revision_count": revisions[name],
            "revision_rate": revisions[name] / counts[name] if counts[name] else None,
            "median_abs_change": median([item[0] for item in values]) if values else None,
            "median_relative_change": median([item[1] for item in values if item[1] is not None]) if any(item[1] is not None for item in values) else None,
            "semantic_status": "UNKNOWN",
            "unit_status": "UNKNOWN",
            "feature_status": "QUARANTINED_PROSPECTIVE",
        })
    return result


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * q + 0.999999)))
    return ordered[index]


def revision_summary(root: Path) -> dict[str, Any]:
    manifests = phase16.EvidenceStore(root).manifests() if root.exists() else []
    flow = [m for m in manifests if m.get("metadata", {}).get("phase16b_flow") is True]
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    manifest_by_response_reference: dict[str, dict[str, Any]] = {}
    for manifest in flow:
        rel = manifest["normalized_payload_path"]
        normalized = json.loads((root / rel).read_text(encoding="utf-8"))
        for row in normalized:
            by_key[row["logical_key"]].append(row)
            manifest_by_response_reference[row["response_snapshot_id"]] = manifest
    comparisons = []
    for rows in by_key.values():
        rows.sort(key=lambda row: row["observed_at_utc"])
        for old, new in pairwise(rows):
            old_manifest = manifest_by_response_reference[old["response_snapshot_id"]]
            new_manifest = manifest_by_response_reference[new["response_snapshot_id"]]
            comparisons.append(classify_row_revision(
                old,
                new,
                response_hash_changed=old_manifest["raw_payload_sha256"] != new_manifest["raw_payload_sha256"],
                canonical_response_hash_changed=old_manifest["canonical_payload_sha256"] != new_manifest["canonical_payload_sha256"],
            ))
    classifications = defaultdict(int)
    for item in comparisons:
        classifications[item["classification"]] += 1
    response_hash_changes = sum(item["response_level_hash_changed"] for item in comparisons)
    now = datetime.now(UTC)
    revision_ages_days: list[float] = []
    magnitudes: list[float] = []
    relative_magnitudes: list[float] = []
    curves = []
    source_summaries: dict[str, dict[str, Any]] = {}
    source_session_sets: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for logical_key, rows in sorted(by_key.items()):
        rows.sort(key=lambda row: row["observed_at_utc"])
        related = [item for item in comparisons if item["logical_key"] == logical_key]
        first_at = datetime.fromisoformat(rows[0]["observed_at_utc"])
        latest_at = datetime.fromisoformat(rows[-1]["observed_at_utc"])
        revised = [item for item in related if item["classification"] not in {"IDENTICAL", "SEMANTICALLY_IDENTICAL"}]
        first_revision_at = datetime.fromisoformat(revised[0]["later_observed_at_utc"]) if revised else None
        last_revision_at = datetime.fromisoformat(revised[-1]["later_observed_at_utc"]) if revised else None
        age = (now - last_revision_at).total_seconds() if last_revision_at else None
        streak = 0
        for item in reversed(related):
            if item["classification"] not in {"IDENTICAL", "SEMANTICALLY_IDENTICAL"}:
                break
            streak += 1
        session = date.fromisoformat(rows[0]["provider_session"])
        source_id = manifest_by_response_reference.get(rows[0]["response_snapshot_id"], {}).get("metadata", {}).get("source_id", "UNKNOWN")
        if rows[0]["evidence_class"] == "PROSPECTIVE_SESSION_OBSERVED":
            segment = "prospective_event_sessions"
        elif rows[0]["evidence_class"] == "HISTORICAL_REVISION_PROBE":
            segment = "historical_revision_probe_sessions"
        else:
            segment = "historical_reobserved_sessions"
        current = source_summaries.setdefault(source_id, {
            "prospective_event_sessions": 0,
            "historical_reobserved_sessions": 0,
            "historical_revision_probe_sessions": 0,
            "logical_observations": 0,
            "multi_vintage_observations": 0,
            "revision_comparison_count": 0,
            "value_revision_count": 0,
        })
        source_session_sets[source_id][segment].add(session.isoformat())
        current["logical_observations"] += 1
        current["multi_vintage_observations"] += int(len(rows) > 1)
        current["revision_comparison_count"] += len(related)
        current["value_revision_count"] += sum(item["classification"] == "VALUE_REVISED" for item in related)
        if last_revision_at:
            revision_ages_days.append((last_revision_at.date() - session).total_seconds() / 86400)
        for item in revised:
            for values in item.get("value_changes", {}).values():
                try:
                    old_value, new_value = float(values["old"]), float(values["new"])
                except (TypeError, ValueError, KeyError):
                    continue
                magnitude = abs(new_value - old_value)
                magnitudes.append(magnitude)
                if old_value:
                    relative_magnitudes.append(magnitude / abs(old_value))
        curves.append({
            "logical_key": logical_key,
            "provider_session": session.isoformat(),
            "source_id": source_id,
            "evidence_class": rows[0]["evidence_class"],
            "first_observed_at": first_at.isoformat(),
            "latest_observed_at": latest_at.isoformat(),
            "vintage_count": len(rows),
            "revision_count": len(revised),
            "time_to_first_revision_seconds": (first_revision_at - first_at).total_seconds() if first_revision_at else None,
            "time_to_last_observed_revision_seconds": (last_revision_at - first_at).total_seconds() if last_revision_at else None,
            "latest_revision_age_seconds": age,
            "currently_identical_streak": streak,
        })
    for source_id, current in source_summaries.items():
        for segment in (
            "prospective_event_sessions",
            "historical_reobserved_sessions",
            "historical_revision_probe_sessions",
        ):
            current[segment] = len(source_session_sets[source_id][segment])
    return {
        "schema_version": 1,
        "flow_response_snapshot_count": len(flow),
        "logical_observations": len(by_key),
        "multi_vintage_observations": sum(len(rows) > 1 for rows in by_key.values()),
        "revision_comparison_count": len(comparisons),
        "response_level_hash_change_count": response_hash_changes,
        "classification_counts": dict(sorted(classifications.items())),
        "value_revision_count": classifications["VALUE_REVISED"],
        "schema_change_count": classifications["SCHEMA_CHANGED"],
        "missing_to_present_count": classifications["MISSING_TO_PRESENT"],
        "present_to_missing_count": classifications["PRESENT_TO_MISSING"],
        "uncomparable_count": classifications["UNCOMPARABLE"],
        "source_summaries": source_summaries,
        "revision_curve": curves,
        "median_revision_magnitude": median(magnitudes) if magnitudes else None,
        "p90_revision_magnitude": _percentile(magnitudes, 0.9),
        "median_relative_revision_magnitude": median(relative_magnitudes) if relative_magnitudes else None,
        "p90_relative_revision_magnitude": _percentile(relative_magnitudes, 0.9),
        "median_revision_age_days": median(revision_ages_days) if revision_ages_days else None,
        "p90_revision_age_days": _percentile(revision_ages_days, 0.9),
        "comparisons": comparisons,
        "field_revision_profile": [
            {**profile, "logical_key": key}
            for key, rows in by_key.items()
            for profile in field_revision_profile(rows, [item for item in comparisons if item["logical_key"] == key])
        ],
        "interpretation": "SOURCE_REVISION_EVIDENCE_ONLY; no returns or predictive use",
    }


def _json_write(path: Path, value: Any) -> None:
    phase16._atomic_replace(path, phase16._json_bytes(value, pretty=True))


def _calendar_from_config(config: dict[str, Any]) -> KRXSessionCalendar:
    section = config.get("phase16b", {}).get("session_calendar", {})
    closures = {date.fromisoformat(day) for day in section.get("official_closures", [])}
    start, end = (date.fromisoformat(section[key]) for key in ("coverage_start", "coverage_end"))
    return build_weekday_calendar(
        start,
        end,
        official_closures=closures,
        source_urls=tuple(section.get("source_urls", [])),
        calendar_version=str(section.get("calendar_version", "UNKNOWN")),
    )


def _historical_probe_calendar(anchors: list[date]) -> KRXSessionCalendar:
    """Build a deliberately broad weekday envelope for fixed, verified old anchors.

    It treats every weekday as a possible session, so the lookback boundary can
    only move earlier than the true KRX-session boundary. This is used solely to
    prove that a bounded historical probe cannot reach a protected 2026 range.
    """
    if not anchors:
        raise ValueError("at least one fixed historical contract-probe anchor is required")
    start = min(anchors) - timedelta(days=120)
    end = max(anchors)
    return build_weekday_calendar(
        start,
        end,
        official_closures=set(),
        source_urls=("https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd?locale=ko",),
        calendar_version="FIXED-HISTORICAL-PROBE-WEEKDAY-ENVELOPE-v1",
    )


def _probe_guard_contract(contract: SourceResponseContract) -> SourceResponseContract:
    """Use the prior bounded Phase 15 sample only as a probe-envelope guard."""
    return replace(
        contract,
        confidence="EMPIRICALLY_VERIFIED",
        response_window_verified=True,
        maximum_rows=contract.maximum_rows,
        session_only=True,
        future_rows_allowed=False,
    )


def _record_contract_probe_snapshot(root: Path, config_sha256: str, probe: dict[str, Any]) -> None:
    safe_payload = {
        key: probe[key]
        for key in (
            "evidence_class",
            "source_id",
            "symbol",
            "market",
            "anchor",
            "response_sha256",
            "schema_fingerprint",
            "row_count",
            "returned_sessions",
            "anchor_included",
            "future_row_count",
            "session_only",
            "ordering_stable",
            "expected_maximum_rows",
            "response_valid",
            "page_count",
            "observed_at_utc",
        )
    }
    phase16.EvidenceStore(root).create_snapshot(
        evidence_class="CONTRACT_VERIFICATION_PROBE",
        source_name=probe["source_id"],
        provider="Korea Investment & Securities",
        endpoint_id=contracts_from_config(json.loads(DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8")))[probe["source_id"]].endpoint_id,
        request_schema_version=probe["contract_version"],
        logical_key=f"contract-probe:{probe['source_id']}:{probe['symbol']}:{probe['anchor']}",
        payload=safe_payload,
        normalized_payload=safe_payload,
        raw_payload=phase16._json_bytes(safe_payload),
        persist_raw=False,
        raw_representation_basis="STRUCTURAL_CONTRACT_PROBE_METADATA_ONLY",
        observed_at=datetime.fromisoformat(probe["observed_at_utc"]),
        config_sha256=config_sha256,
        collector_git_sha=phase16._git_sha(),
        symbol=probe["symbol"],
        market=probe["market"],
        requested_session=date.fromisoformat(probe["anchor"]),
        record_count=probe["row_count"],
        metadata={
            "source_id": probe["source_id"],
            "evidence_class": "CONTRACT_VERIFICATION_PROBE",
            "provider_response_sha256": probe["response_sha256"],
            "field_values_retained": False,
            "credentials_retained": False,
        },
    )


def contracts_from_config(config: dict[str, Any]) -> dict[str, SourceResponseContract]:
    raw = config.get("phase16b", {}).get("source_contracts", {})
    return {key: SourceResponseContract.from_dict(value) for key, value in raw.items()}


def _config_digest(config: dict[str, Any], explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if DEFAULT_PHASE16B_CONFIG.is_file():
        raw = DEFAULT_PHASE16B_CONFIG.read_bytes()
        if json.loads(raw) == config:
            return hashlib.sha256(raw).hexdigest()
    return hashlib.sha256(phase16._json_bytes(config)).hexdigest()


def flow_preflight(
    config: dict[str, Any],
    *,
    now: datetime,
    credentials_available: bool,
    collector_healthy: bool,
    contract_violation_sources: set[str] | None = None,
    config_sha256: str | None = None,
) -> dict[str, Any]:
    calendar = _calendar_from_config(config)
    protected = phase16.protected_ranges(config)
    completed = latest_completed_session(calendar, now)
    phase_cfg = config.get("phase16b", {})
    source_reports = []
    for source_id, contract in contracts_from_config(config).items():
        first_safe = first_safe_anchor_session(contract, calendar=calendar, protected_ranges=protected)
        state = activation_state(
            contract,
            first_safe_anchor=first_safe,
            latest_completed_session=completed,
            credentials_available=credentials_available or source_id.startswith("krx_"),
            collector_healthy=collector_healthy,
            auto_activation=bool(phase_cfg.get("auto_activation_enabled", False)) and source_id in set(phase_cfg.get("auto_activate_source_ids", [])),
            provider_policy_allowed=bool(config.get("sources", {}).get(source_id, {}).get("policy_allowed", True)),
            contract_violation=source_id in (contract_violation_sources or set()),
        )
        envelope = None
        guard = None
        if completed and contract.sufficient_for_preflight:
            try:
                envelope = possible_response_session_range(contract, completed, calendar=calendar)
                guard = request_contract_guard(contract, completed, calendar=calendar, protected_ranges=protected)
            except ContractError:
                pass
        sessions_until = None
        if completed and first_safe:
            future_sessions = [day for day in calendar.sessions if completed < day < first_safe]
            sessions_until = len(future_sessions) + 1 if completed < first_safe else 0
        source_reports.append({
            "source_id": source_id,
            "source_name": contract.source_name,
            "contract_version": contract.contract_version,
            "contract_confidence": contract.confidence,
            "contract_sufficient": contract.sufficient_for_preflight,
            "contract_state": "CONTRACT_VERIFIED" if contract.sufficient_for_preflight else "CONTRACT_PARTIAL" if contract.confidence != "UNKNOWN" else "CONTRACT_UNKNOWN",
            "latest_completed_krx_session": completed.isoformat() if completed else None,
            "possible_response_start": envelope["earliest_possible_session"] if envelope else None,
            "possible_response_end": envelope["latest_possible_session"] if envelope else None,
            "protected_overlap": guard["protected_overlaps"] if guard else None,
            "first_safe_anchor_session": first_safe.isoformat() if first_safe else None,
            "sessions_until_eligibility": sessions_until,
            "collection_state": state["state"],
            "reason": state["reason"],
            "request_decision": guard["request_decision"] if guard else "DENY_UNVERIFIED_SESSION",
        })
    return {
        "schema_version": 1,
        "status": "NO_COMPLETED_MARKET_SESSION" if completed is None else "PREFLIGHT_ONLY",
        "generated_at_utc": now.astimezone(UTC).isoformat(),
        "generated_at_kst": now.astimezone(KST).isoformat(),
        "collector_config_sha256": _config_digest(config, config_sha256),
        "session_calendar": {
            "version": calendar.calendar_version,
            "coverage_start": calendar.coverage_start.isoformat(),
            "coverage_end": calendar.coverage_end.isoformat(),
            "source_urls": list(calendar.source_urls),
        },
        "latest_completed_krx_session": completed.isoformat() if completed else None,
        "network_accessed": False,
        "sources": source_reports,
    }


def make_krx_request(session: date, market_code: str) -> tuple[str, dict[str, str]]:
    if market_code not in {"STK", "KSQ"}:
        raise ValueError("market_code must be STK (KOSPI) or KSQ (KOSDAQ)")
    path = "/comm/bldAttendant/getJsonData.cmd?bld=dbms/MDC_OUT/STAT/standard/MDCSTAT02202_OUT"
    return path, {
        "locale": "ko_KR",
        "inqTpCd": "2",
        "mktId": market_code,
        "strtDd": session.strftime("%Y%m%d"),
        "endDd": session.strftime("%Y%m%d"),
        "trdVolVal": "1",
        "askBid": "3",
        "detailView": "",
        "segTpCd": "ALL",
        "csvxls_isNo": "false",
        "bld": "dbms/MDC_OUT/STAT/standard/MDCSTAT02202_OUT",
    }


class KRXExactDayTransport:
    """Read the official KRX Data Marketplace daily-trend route for one date."""

    BASE_URL = "https://data.krx.co.kr"
    PAGE_URL = BASE_URL + "/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT022"
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36"

    def __init__(self) -> None:
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def fetch(self, session: date, market_code: str) -> tuple[dict[str, Any], bytes]:
        self._opener.open(Request(self.PAGE_URL, headers={"User-Agent": self.USER_AGENT, "Accept": "text/html,application/xhtml+xml"}), timeout=20).read()
        path, params = make_krx_request(session, market_code)
        url = self.BASE_URL + path
        body = urllib.parse.urlencode(params).encode()
        request = Request(url, data=body, headers={
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json, text/javascript, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": self.BASE_URL,
            "Referer": self.PAGE_URL,
            "X-Requested-With": "XMLHttpRequest",
        })
        raw = self._opener.open(request, timeout=20).read()
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("KRX response is not an object")
        return value, raw


def credentials_available(env_file: Path = phase16.REPO_ROOT / ".env") -> bool:
    names = {"KIS_APP_KEY", "KIS_APP_SECRET"}
    values = {name: os.environ.get(name, "").strip() for name in names}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition("=")
            key = key.removeprefix("export ").strip()
            if separator and key in names:
                values[key] = value.strip().strip("\"'")
    return all(values.values())


def _load_kis_credentials(env_file: Path = phase16.REPO_ROOT / ".env") -> dict[str, str]:
    names = ("KIS_APP_KEY", "KIS_APP_SECRET")
    values = {name: os.environ.get(name, "").strip() for name in names}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition("=")
            key = key.removeprefix("export ").strip()
            if separator and key in names and not values[key]:
                values[key] = value.strip().strip("\"'")
    return values


def _kis_client(config: dict[str, Any]):
    if not credentials_available():
        raise ContractError("KIS public-data credentials unavailable")
    from krx_trader.kis.auth import TokenManager
    from krx_trader.kis.rest import KisRestClient
    from krx_trader.kis.transport import UrllibTransport

    credentials = _load_kis_credentials()
    transport = UrllibTransport()
    tokens = TokenManager(credentials["KIS_APP_KEY"], credentials["KIS_APP_SECRET"], transport, cache_path=None)
    policy = config.get("request_policy", {})
    return KisRestClient(
        credentials["KIS_APP_KEY"],
        credentials["KIS_APP_SECRET"],
        tokens,
        transport,
        min_request_interval=float(policy.get("authenticated_kis_min_interval_seconds", 4)),
        max_retries=0,
        rate_limit_path=phase16.REPO_ROOT / policy.get("shared_limiter_path", "runtime/kis_rate_limit.json"),
    )


def _contract_violation_sources(root: Path) -> set[str]:
    directory = root / "contract-violations"
    sources = set()
    if directory.exists():
        for path in directory.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            source_id = item.get("source_id")
            if isinstance(source_id, str) and item.get("event_type") in {
                "SOURCE_CONTRACT_VIOLATION",
                "PROTECTED_RANGE_CONTRACT_BREACH",
            }:
                sources.add(source_id)
    return sources


def _pagination_contract_violation(exc: Exception) -> bool:
    return isinstance(exc, KisApiError) and "dated-flow pagination exceeded its safe page limit" in str(exc)


def collect_kis_flow(
    *,
    config: dict[str, Any],
    config_sha256: str,
    root: Path,
    source_id: str,
    symbols: list[dict[str, str]],
    anchor: date,
    observed_at: datetime,
    observation_slot: str,
    intentional_reobservation: bool = False,
) -> dict[str, Any]:
    if source_id not in {"kis_per_stock_flow", "kis_program_flow"}:
        raise ValueError("collect_kis_flow supports only per-stock or program source IDs")
    contracts = contracts_from_config(config)
    contract = contracts[source_id]
    calendar = _calendar_from_config(config)
    protected = phase16.protected_ranges(config)
    source_decision = flow_preflight(
        config,
        now=observed_at,
        credentials_available=credentials_available(),
        collector_healthy=phase16.verify_store(root)["valid"],
        contract_violation_sources=_contract_violation_sources(root),
    )
    source_state = next(row for row in source_decision["sources"] if row["source_id"] == source_id)
    if source_state["collection_state"] != "ACTIVE":
        return {
            "status": source_state["collection_state"],
            "reason": source_state["reason"],
            "source_id": source_id,
            "request_count": 0,
            "network_accessed": False,
            "anchor": anchor.isoformat(),
        }
    decision = request_contract_guard(contract, anchor, calendar=calendar, protected_ranges=protected)
    if not decision["allowed"]:
        return {
            "status": decision["request_decision"],
            "source_id": source_id,
            "request_count": 0,
            "network_accessed": False,
            "guard_decision": decision,
        }
    if not symbols:
        return {"status": "EMPTY_COHORT", "source_id": source_id, "request_count": 0, "network_accessed": False}

    store = phase16.EvidenceStore(root)
    with phase16._file_lock(root / "locks/collector.lock", blocking=False) as locked:
        if not locked:
            return {"status": "ALREADY_RUNNING", "source_id": source_id, "request_count": 0, "network_accessed": False}
        manifests = store.manifests()
        same_slot = [
            item for item in manifests
            if item.get("metadata", {}).get("phase16b_flow") is True
            and item.get("metadata", {}).get("source_id") == source_id
            and item.get("requested_session") == anchor.isoformat()
            and item.get("metadata", {}).get("observation_slot") == observation_slot
        ]
        if same_slot and not intentional_reobservation:
            return {
                "status": "ACCIDENTAL_DUPLICATE_JOB",
                "source_id": source_id,
                "request_count": 0,
                "network_accessed": False,
                "anchor": anchor.isoformat(),
                "observation_slot": observation_slot,
            }
        try:
            client = _kis_client(config)
        except ContractError as exc:
            return {"status": "READY", "reason": str(exc), "source_id": source_id, "request_count": 0, "network_accessed": False}
        started = datetime.now(UTC)
        missing = failures = retries = rows_saved = request_count = 0
        response_received_count = provider_error_count = transport_error_count = 0
        schema_failure_count = contract_violation_count = protected_breach_count = null_field_count = 0
        snapshot_ids: list[str] = []
        successful_symbols: list[str] = []
        failed_symbols: list[str] = []
        for item in symbols:
            symbol = str(item["symbol"])
            market = item.get("market")
            request_decision = request_contract_guard(contract, anchor, calendar=calendar, protected_ranges=protected)
            if not request_decision["allowed"]:
                failures += len(symbols) - len(successful_symbols) - len(failed_symbols)
                break
            try:
                request_count += 1
                if source_id == "kis_per_stock_flow":
                    payload = execute_contract_guarded_request(
                        contract,
                        anchor,
                        calendar=calendar,
                        protected_ranges=protected,
                        transport=lambda symbol=symbol: client.get_investor_flow_by_date(symbol, anchor, max_pages=1),
                    )
                else:
                    payload = execute_contract_guarded_request(
                        contract,
                        anchor,
                        calendar=calendar,
                        protected_ranges=protected,
                        transport=lambda symbol=symbol: client.get_program_flow_by_date(symbol, anchor, max_pages=1),
                    )
            except (KisApiError, KisAuthError, OSError, ValueError, TypeError) as exc:
                failures += 1
                failed_symbols.append(symbol)
                pagination_violation = _pagination_contract_violation(exc)
                if pagination_violation:
                    contract_violation_count += 1
                elif isinstance(exc, OSError) or (isinstance(exc, KisApiError) and "TransportError" in str(exc)):
                    transport_error_count += 1
                else:
                    provider_error_count += 1
                failure_event = {
                    "event_type": "SOURCE_CONTRACT_VIOLATION" if pagination_violation else "PROVIDER_REQUEST_FAILURE",
                    "source_id": source_id,
                    "symbol": symbol,
                    "anchor": anchor.isoformat(),
                    "error_type": type(exc).__name__,
                    "request_count": 1,
                    "payload_persisted": False,
                    "reason_code": "PAGINATION_EXCEEDED_VERIFIED_ONE_PAGE_LIMIT" if pagination_violation else None,
                }
                failure_dir = root / ("contract-violations" if pagination_violation else "collection-errors")
                failure_path = failure_dir / f"{hashlib.sha256(phase16._json_bytes(failure_event)).hexdigest()}.json"
                _json_write(failure_path, failure_event)
                break
            response_received_count += 1
            response_check = verify_response_contract(
                payload,
                contract,
                request_anchor=anchor,
                calendar=calendar,
                protected_ranges=protected,
            )
            if not response_check["valid"]:
                contract_violation_count += 1
                protected_breach_count += int("PROTECTED_RANGE_CONTRACT_BREACH" in response_check["failures"])
                schema_failure_count += int("DATE_FIELD_INVALID_OR_MISSING" in response_check["failures"])
                event = {
                    "event_type": "PROTECTED_RANGE_CONTRACT_BREACH" if "PROTECTED_RANGE_CONTRACT_BREACH" in response_check["failures"] else "SOURCE_CONTRACT_VIOLATION",
                    "source_id": source_id,
                    "symbol": symbol,
                    "anchor": anchor.isoformat(),
                    "observed_at_utc": datetime.now(UTC).isoformat(),
                    "response_sha256": phase16.canonical_payload_sha256(payload),
                    "response_check": response_check,
                    "payload_persisted": False,
                    "source_disabled_after_violation": True,
                }
                _json_write(root / "contract-violations" / f"{hashlib.sha256(phase16._json_bytes(event)).hexdigest()}.json", event)
                failures += 1
                failed_symbols.append(symbol)
                break
            response_rows = extract_dated_rows(payload)
            if not response_rows:
                missing += 1
            null_field_count += sum(value is None for row in response_rows for value in row.values())
            raw = phase16._json_bytes(payload)
            raw_sha = hashlib.sha256(raw).hexdigest()
            logical_key = f"flow-response:{source_id}:{symbol}:{anchor.isoformat()}"
            snapshot_preview = _snapshot_id_for_payload(
                source_name=contract.source_name,
                logical_key=logical_key,
                symbol=symbol,
                requested_session=anchor,
                observed_at=observed_at,
                payload=payload,
                raw_payload=raw,
            )
            normalized = normalize_dated_rows(
                payload,
                contract=contract,
                source_name=contract.source_name,
                symbol=symbol,
                market=market,
                response_snapshot_id=snapshot_preview,
                request_anchor=anchor,
                observed_at=observed_at,
                prospective_start=date.fromisoformat(config["phase16b"].get("prospective_era_start", "2026-10-01")),
            )
            snapshot = store.create_snapshot(
                evidence_class="PROSPECTIVE_OBSERVED",
                source_name=contract.source_name,
                provider="Korea Investment & Securities",
                endpoint_id=contract.endpoint_id,
                request_schema_version=contract.contract_version,
                logical_key=logical_key,
                payload=payload,
                normalized_payload=normalized,
                raw_payload=raw,
                persist_raw=True,
                raw_representation_basis="KIS_PUBLIC_MARKET_FLOW_JSON_RESPONSE",
                observed_at=observed_at,
                config_sha256=config_sha256,
                collector_git_sha=phase16._git_sha(),
                symbol=symbol,
                market=market,
                requested_session=anchor,
                provider_session=anchor,
                result_classification="HTTP_200_RESPONSE_CONTRACT_VALID",
                availability_label="OBSERVATION_TIME_ONLY_PROVIDER_PUBLICATION_UNVERIFIED",
                record_count=len(normalized),
                metadata={
                    "phase16b_flow": True,
                    "source_id": source_id,
                    "observation_slot": observation_slot,
                    "response_sha256": raw_sha,
                    "request_anchor": anchor.isoformat(),
                    "contract_version": contract.contract_version,
                    "contract_confidence": contract.confidence,
                    "response_check": response_check,
                    "universe_snapshot_id": _latest_universe_snapshot_id(root),
                    "row_return_status": "NO_ROW_RETURNED" if not response_rows else "ROW_RETURNED",
                    "null_field_count": sum(value is None for row in response_rows for value in row.values()),
                    "null_provider_fields": sorted({
                        str(field) for row in response_rows for field, value in row.items() if value is None
                    }),
                    "field_semantics": "provider fields preserved; unresolved names remain UNKNOWN",
                    "feature_status": "QUARANTINED_PROSPECTIVE",
                },
            )
            snapshot_ids.append(snapshot["snapshot_id"])
            rows_saved += len(normalized)
            if response_rows:
                successful_symbols.append(symbol)
    ended = datetime.now(UTC)
    coverage = len(successful_symbols) / len(symbols) if symbols else 0.0
    coverage_health = "GOOD" if coverage >= 0.95 else "DEGRADED" if coverage >= 0.8 else "POOR"
    integrity = phase16.verify_store(root)
    batch = {
        "schema_version": 1,
        "batch_id": f"phase16b-{source_id}-{anchor.isoformat()}-{observed_at.astimezone(KST):%H%M%S}",
        "source_id": source_id,
        "evidence_class": "PROSPECTIVE_SESSION_OBSERVED",
        "request_anchor": anchor.isoformat(),
        "observation_slot": observation_slot,
        "intentional_reobservation": intentional_reobservation,
        "started_at_utc": started.isoformat(),
        "ended_at_utc": ended.isoformat(),
        "duration_seconds": round((ended - started).total_seconds(), 3),
        "target_count": len(symbols),
        "collection_scope": "FROZEN_RESEARCH_COHORT" if len(symbols) >= 100 else "FIXED_FLOW_PROBE_COHORT",
        "request_count": request_count,
        "retry_count": retries,
        "success_count": len(successful_symbols),
        "response_received_count": response_received_count,
        "missing_count": missing,
        "failure_count": failures,
        "provider_error_count": provider_error_count,
        "transport_error_count": transport_error_count,
        "schema_failure_count": schema_failure_count,
        "contract_violation_count": contract_violation_count,
        "protected_breach_count": protected_breach_count,
        "null_field_count": null_field_count,
        "coverage": coverage,
        "coverage_health": coverage_health,
        "row_count": rows_saved,
        "snapshot_ids": snapshot_ids,
        "successful_symbols": successful_symbols,
        "failed_symbols": failed_symbols,
        "manifest_chain_tip": integrity["chain_tip_sha256"],
        "manifest_chain_valid": integrity["valid"],
    }
    _json_write(root / "phase16b-flow-batches" / f"{batch['batch_id']}.json", batch)
    if snapshot_ids:
        write_activation_witness(
            root=root,
            phase16b_root=DEFAULT_PHASE16B_ROOT,
            config=config,
            source_id=source_id,
            actual_anchor=anchor,
            observed_at=observed_at,
            config_sha256=config_sha256,
            reason=source_state["reason"],
            protected_proof=decision,
        )
        _write_first_flow_witness(
            root,
            snapshot_ids,
            observed_at,
            anchor,
            config_sha256,
            rows_saved,
            len(snapshot_ids),
            integrity["chain_tip_sha256"],
            source_id=source_id,
            symbol_count=len(snapshot_ids),
        )
    return {"status": "COMPLETE" if failures == 0 else "PARTIAL", "network_accessed": request_count > 0, **batch}


def _latest_universe_snapshot_id(root: Path) -> str | None:
    manifests = phase16.EvidenceStore(root).manifests() if root.exists() else []
    universe = [
        item for item in manifests
        if item.get("metadata", {}).get("universe_scope") == "CURRENT_LISTINGS"
        or item.get("source_name") in {"KIS current KOSPI/KOSDAQ stock master", "KIS current listings"}
    ]
    universe.sort(key=lambda item: item.get("observed_at_utc", ""))
    return universe[-1].get("snapshot_id") if universe else None


def collect_historical_revision_probe(
    *,
    config: dict[str, Any],
    config_sha256: str,
    root: Path,
    source_id: str,
    symbol: str,
    market: str,
    anchor: date,
    observed_at: datetime,
    observation_slot: str = "historical-weekly",
    intentional_reobservation: bool = False,
) -> dict[str, Any]:
    contract = contracts_from_config(config)[source_id]
    if source_id not in {"kis_per_stock_flow", "kis_program_flow"} or not contract.sufficient_for_preflight:
        return {"status": "DEFERRED_RESPONSE_CONTRACT", "source_id": source_id, "request_count": 0, "network_accessed": False}
    calendar = _historical_probe_calendar([anchor])
    protected = phase16.protected_ranges(config)
    decision = request_contract_guard(
        contract,
        anchor,
        calendar=calendar,
        protected_ranges=protected,
        evidence_class="HISTORICAL_REVISION_PROBE",
    )
    if not decision["allowed"]:
        return {"status": decision["request_decision"], "source_id": source_id, "request_count": 0, "network_accessed": False, "guard_decision": decision}
    store = phase16.EvidenceStore(root)
    with phase16._file_lock(root / "locks/collector.lock", blocking=False) as locked:
        if not locked:
            return {"status": "ALREADY_RUNNING", "source_id": source_id, "request_count": 0, "network_accessed": False}
        week_key = f"{observed_at.astimezone(KST):%G-W%V}"
        same_week = [
            item for item in store.manifests()
            if item.get("evidence_class") == "HISTORICAL_REVISION_PROBE"
            and item.get("metadata", {}).get("source_id") == source_id
            and item.get("metadata", {}).get("historical_probe_week") == week_key
        ]
        if same_week and not intentional_reobservation:
            return {"status": "ACCIDENTAL_DUPLICATE_JOB", "source_id": source_id, "request_count": 0, "network_accessed": False, "week": week_key}
        try:
            client = _kis_client(config)
        except ContractError as exc:
            return {"status": "READY", "reason": str(exc), "source_id": source_id, "request_count": 0, "network_accessed": False}
        try:
            if source_id == "kis_per_stock_flow":
                payload = execute_contract_guarded_request(
                    contract,
                    anchor,
                    calendar=calendar,
                    protected_ranges=protected,
                    evidence_class="HISTORICAL_REVISION_PROBE",
                    transport=lambda: client.get_investor_flow_by_date(symbol, anchor, max_pages=1),
                )
            else:
                payload = execute_contract_guarded_request(
                    contract,
                    anchor,
                    calendar=calendar,
                    protected_ranges=protected,
                    evidence_class="HISTORICAL_REVISION_PROBE",
                    transport=lambda: client.get_program_flow_by_date(symbol, anchor, max_pages=1),
                )
        except (KisApiError, KisAuthError, OSError, ValueError, TypeError) as exc:
            pagination_violation = _pagination_contract_violation(exc)
            event = {
                "event_type": "SOURCE_CONTRACT_VIOLATION" if pagination_violation else "PROVIDER_REQUEST_FAILURE",
                "source_id": source_id,
                "symbol": symbol,
                "anchor": anchor.isoformat(),
                "error_type": type(exc).__name__,
                "evidence_class": "HISTORICAL_REVISION_PROBE",
                "reason_code": "PAGINATION_EXCEEDED_VERIFIED_ONE_PAGE_LIMIT" if pagination_violation else None,
            }
            event_dir = root / ("contract-violations" if pagination_violation else "collection-errors")
            _json_write(event_dir / f"{hashlib.sha256(phase16._json_bytes(event)).hexdigest()}.json", event)
            return {"status": event["event_type"], "source_id": source_id, "request_count": 1, "network_accessed": True, "error_type": type(exc).__name__, "payload_persisted": False}
        response_check = verify_response_contract(payload, contract, request_anchor=anchor, calendar=calendar, protected_ranges=protected)
        if not response_check["valid"]:
            event = {
                "event_type": "PROTECTED_RANGE_CONTRACT_BREACH" if "PROTECTED_RANGE_CONTRACT_BREACH" in response_check["failures"] else "SOURCE_CONTRACT_VIOLATION",
                "source_id": source_id,
                "symbol": symbol,
                "anchor": anchor.isoformat(),
                "response_sha256": phase16.canonical_payload_sha256(payload),
                "response_check": response_check,
                "payload_persisted": False,
            }
            _json_write(root / "contract-violations" / f"{hashlib.sha256(phase16._json_bytes(event)).hexdigest()}.json", event)
            return {"status": event["event_type"], "source_id": source_id, "request_count": 1, "network_accessed": True, "payload_persisted": False}
        rows = extract_dated_rows(payload)
        raw = phase16._json_bytes(payload)
        raw_sha = hashlib.sha256(raw).hexdigest()
        logical_key = f"flow-response:{source_id}:{symbol}:{anchor.isoformat()}"
        preview = _snapshot_id_for_payload(
            source_name=contract.source_name,
            logical_key=logical_key,
            symbol=symbol,
            requested_session=anchor,
            observed_at=observed_at,
            payload=payload,
            raw_payload=raw,
        )
        normalized = normalize_dated_rows(
            payload,
            contract=contract,
            source_name=contract.source_name,
            symbol=symbol,
            market=market,
            response_snapshot_id=preview,
            request_anchor=anchor,
            observed_at=observed_at,
            prospective_start=date.fromisoformat(config["phase16b"].get("prospective_era_start", "2026-10-01")),
            row_evidence_class="HISTORICAL_REVISION_PROBE",
        )
        snapshot = store.create_snapshot(
            evidence_class="HISTORICAL_REVISION_PROBE",
            source_name=contract.source_name,
            provider="Korea Investment & Securities",
            endpoint_id=contract.endpoint_id,
            request_schema_version=contract.contract_version,
            logical_key=logical_key,
            payload=payload,
            normalized_payload=normalized,
            raw_payload=raw,
            persist_raw=True,
            raw_representation_basis="KIS_PUBLIC_MARKET_FLOW_JSON_RESPONSE",
            observed_at=observed_at,
            config_sha256=config_sha256,
            collector_git_sha=phase16._git_sha(),
            symbol=symbol,
            market=market,
            requested_session=anchor,
            provider_session=anchor,
            result_classification="HISTORICAL_REVISION_PROBE_RESPONSE_VALID",
            availability_label="HISTORICAL_VALUE_OBSERVED_AT_CURRENT_TIME",
            record_count=len(normalized),
            metadata={
                "phase16b_flow": True,
                "source_id": source_id,
                "observation_slot": observation_slot,
                "historical_probe_week": week_key,
                "historical_revision_probe": True,
                "response_sha256": raw_sha,
                "contract_version": contract.contract_version,
                "response_check": response_check,
                "provider_session_is_not_known_at_event_date": True,
                "feature_status": "QUARANTINED_PROSPECTIVE",
            },
        )
    integrity = phase16.verify_store(root)
    result = {
        "schema_version": 1,
        "status": "COMPLETE",
        "source_id": source_id,
        "evidence_class": "HISTORICAL_REVISION_PROBE",
        "symbol": symbol,
        "anchor": anchor.isoformat(),
        "observed_at_utc": observed_at.astimezone(UTC).isoformat(),
        "request_count": 1,
        "retry_count": 0,
        "success_count": 1,
        "missing_count": int(not rows),
        "row_count": len(normalized),
        "response_sha256": raw_sha,
        "snapshot_id": snapshot["snapshot_id"],
        "manifest_chain_tip": integrity["chain_tip_sha256"],
        "manifest_chain_valid": integrity["valid"],
    }
    result_path = DEFAULT_PHASE16B_ROOT / "phase16b-historical-revision-probe-results.json"
    prior = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {"schema_version": 1, "observations": []}
    prior["observations"].append(result)
    prior["cadence"] = "WEEKLY, one fixed symbol and anchor per invocation"
    prior["updated_at_utc"] = datetime.now(UTC).isoformat()
    _json_write(result_path, prior)
    return result


def _frozen_cohort_markets(config: dict[str, Any]) -> list[dict[str, str]]:
    symbols = phase16._read_cohort_symbols(
        phase16.REPO_ROOT / config["cohorts"]["full_research_cohort"]["path"],
        config["cohorts"]["full_research_cohort"]["cohort_sha256"],
    )
    phase16_root = phase16.DEFAULT_ROOT
    manifests = phase16.EvidenceStore(phase16_root).manifests() if phase16_root.exists() else []
    candidates = [
        item for item in manifests
        if item.get("source_name") == "KIS current listings"
        and item.get("evidence_class") == "PROSPECTIVE_OBSERVED"
    ]
    market_by_symbol: dict[str, str] = {}
    if candidates:
        candidates.sort(key=lambda item: item.get("observed_at_utc", ""))
        latest = candidates[-1]
        rows = json.loads((phase16_root / latest["normalized_payload_path"]).read_text(encoding="utf-8"))
        market_by_symbol = {
            str(row["symbol"]): str(row["market"])
            for row in rows
            if isinstance(row, dict) and row.get("symbol") and row.get("market") in {"KOSPI", "KOSDAQ"}
        }
    return [{"symbol": symbol, "market": market_by_symbol.get(symbol, "UNKNOWN")} for symbol in symbols]


def frozen_probe_cohort(config: dict[str, Any]) -> list[dict[str, str]]:
    cohort = config.get("phase16b", {}).get("observation_policy", {}).get("fixed_flow_probe_cohort", {})
    symbols_by_market = cohort.get("symbols_by_market", {})
    actual_hash = hashlib.sha256(phase16._json_bytes(symbols_by_market)).hexdigest()
    if actual_hash != cohort.get("membership_sha256"):
        raise ContractError("frozen flow probe cohort hash mismatch")
    if set(symbols_by_market) != {"KOSPI", "KOSDAQ"}:
        raise ContractError("frozen flow probe cohort must define KOSPI and KOSDAQ")
    flattened = [
        {"symbol": str(symbol), "market": market}
        for market in ("KOSPI", "KOSDAQ")
        for symbol in symbols_by_market[market]
    ]
    symbols = [item["symbol"] for item in flattened]
    if (
        len(symbols_by_market["KOSPI"]) != 6
        or len(symbols_by_market["KOSDAQ"]) != 6
        or len(set(symbols)) != len(symbols)
        or any(not re.fullmatch(r"\d{6}", symbol) for symbol in symbols)
    ):
        raise ContractError("frozen flow probe cohort membership is invalid")
    return flattened


def collect_active_flow_slot(
    *,
    config: dict[str, Any],
    config_sha256: str,
    root: Path,
    slot: str,
    now: datetime,
    intentional_reobservation: bool = False,
    krx_fetch: Callable[[date, str], tuple[dict[str, Any], bytes]] | None = None,
) -> dict[str, Any]:
    allowed_slots = {"probe-close", "probe-evening", "full-evening", "probe-morning", "historical-weekly"}
    if slot not in allowed_slots:
        raise ValueError(f"unsupported scheduled flow slot: {slot}")
    preflight = flow_preflight(
        config,
        now=now,
        credentials_available=credentials_available(),
        collector_healthy=phase16.verify_store(root)["valid"],
        contract_violation_sources=_contract_violation_sources(root),
        config_sha256=config_sha256,
    )
    activation_path = root / "reports/phase16b-source-activation.json"
    _json_write(activation_path, preflight)
    _json_write(root / "contracts/phase16b-source-contracts.json", {
        "schema_version": 1,
        "config_version": config.get("config_version"),
        "config_sha256": config_sha256,
        "contracts": {key: value.as_dict() for key, value in contracts_from_config(config).items()},
    })
    session_calendar = _calendar_from_config(config)
    if now.astimezone(KST).date() not in session_calendar.sessions:
        result = {"status": "NO_COMPLETED_MARKET_SESSION", "slot": slot, "network_accessed": False, "sources": []}
        write_phase16b_artifacts(config=config, config_sha256=config_sha256, root=root, preflight=preflight)
        return result
    if slot == "historical-weekly":
        if now.astimezone(KST).weekday() != 4:
            result = {"status": "NOT_SCHEDULED_DAY", "slot": slot, "network_accessed": False, "sources": []}
            write_phase16b_artifacts(config=config, config_sha256=config_sha256, root=root, preflight=preflight)
            return result
        policy = config.get("phase16b", {}).get("historical_revision_policy", {})
        result = collect_historical_revision_probe(
            config=config,
            config_sha256=config_sha256,
            root=root,
            source_id=policy.get("source_id", "kis_per_stock_flow"),
            symbol=policy.get("symbol", "005930"),
            market=policy.get("market", "KOSPI"),
            anchor=date.fromisoformat(policy.get("anchor", "2025-06-02")),
            observed_at=now,
            observation_slot=slot,
            intentional_reobservation=intentional_reobservation,
        )
        write_phase16b_artifacts(config=config, config_sha256=config_sha256, root=root, preflight=preflight)
        return {"slot": slot, "network_accessed": result.get("network_accessed", result.get("status") == "COMPLETE"), "sources": [result], **{key: value for key, value in result.items() if key not in {"sources", "slot", "network_accessed"}}}
    latest = preflight.get("latest_completed_krx_session")
    if not latest:
        result = {"status": "NO_COMPLETED_MARKET_SESSION", "slot": slot, "network_accessed": False, "sources": []}
        write_phase16b_artifacts(config=config, config_sha256=config_sha256, root=root, preflight=preflight)
        return result
    anchor = date.fromisoformat(latest)
    reports = {item["source_id"]: item for item in preflight["sources"]}
    run_results = []
    if slot == "full-evening" and reports.get("krx_market_flow_exact_day", {}).get("collection_state") == "ACTIVE":
        run_results.append(collect_krx_exact_day(
            config=config,
            root=root,
            session=anchor,
            observed_at=now,
            fetch=krx_fetch,
            config_sha256=config_sha256,
            observation_slot=slot,
            intentional_reobservation=intentional_reobservation,
        ))
    if slot == "full-evening":
        cohorts = {"kis_per_stock_flow": _frozen_cohort_markets(config), "kis_program_flow": _frozen_cohort_markets(config)}
    else:
        probe = frozen_probe_cohort(config)
        cohorts = {"kis_per_stock_flow": probe, "kis_program_flow": []}
    for source_id in ("kis_per_stock_flow", "kis_program_flow"):
        if slot != "full-evening" and source_id == "kis_program_flow":
            run_results.append({"status": "NOT_SCHEDULED_IN_PROBE_SLOT", "source_id": source_id, "request_count": 0, "network_accessed": False})
            continue
        if reports.get(source_id, {}).get("collection_state") != "ACTIVE":
            run_results.append({
                "status": reports.get(source_id, {}).get("collection_state", "CONTRACT_UNKNOWN"),
                "reason": reports.get(source_id, {}).get("reason"),
                "source_id": source_id,
                "request_count": 0,
                "network_accessed": False,
                "anchor": anchor.isoformat(),
            })
            continue
        run_results.append(collect_kis_flow(
            config=config,
            config_sha256=config_sha256,
            root=root,
            source_id=source_id,
            symbols=cohorts[source_id],
            anchor=anchor,
            observed_at=now,
            observation_slot=slot,
            intentional_reobservation=intentional_reobservation,
        ))
    statuses = [result.get("status") for result in run_results]
    result = {
        "status": "COMPLETE" if all(status in {"COMPLETE", "WAITING_FOR_SAFE_DATE", "DEFERRED_RESPONSE_CONTRACT", "CONTRACT_UNKNOWN", "CONTRACT_PARTIAL"} for status in statuses) else "PARTIAL",
        "slot": slot,
        "anchor": anchor.isoformat(),
        "network_accessed": any(item.get("network_accessed") for item in run_results),
        "preflight": preflight,
        "sources": run_results,
    }
    write_phase16b_artifacts(config=config, config_sha256=config_sha256, root=root, preflight=preflight)
    return result


FLOW_SCHEDULES = {
    "probe-close": (16, 20),
    "probe-evening": (20, 20),
    "full-evening": (20, 30),
    "probe-morning": (8, 20),
    "historical-weekly": (20, 45),
}
FLOW_WEEKDAYS = {"historical-weekly": 5}


def build_flow_launchd_plists(root: Path = phase16.DEFAULT_ROOT, python_path: Path | None = None) -> list[dict[str, Any]]:
    python_path = python_path or phase16.REPO_ROOT / ".venv/bin/python"
    output = []
    for slot, (hour, minute) in FLOW_SCHEDULES.items():
        label = f"com.krxtrader.phase16.flow.{slot}"
        log_dir = root / "logs"
        interval = {"Hour": hour, "Minute": minute}
        if slot in FLOW_WEEKDAYS:
            interval["Weekday"] = FLOW_WEEKDAYS[slot]
        output.append({
            "Label": label,
            "ProgramArguments": [
                str(python_path),
                "-m",
                "krx_trader.research.phase16",
                "--root",
                str(root),
                "collect-active-flow",
                "--slot",
                slot,
            ],
            "WorkingDirectory": str(phase16.REPO_ROOT),
            "StartCalendarInterval": interval,
            "RunAtLoad": False,
            "KeepAlive": False,
            "ProcessType": "Background",
            "StandardOutPath": str(log_dir / f"flow-{slot}.stdout.log"),
            "StandardErrorPath": str(log_dir / f"flow-{slot}.stderr.log"),
        })
    return output


def install_flow_launchd(root: Path = phase16.DEFAULT_ROOT, python_path: Path | None = None) -> dict[str, Any]:
    import plistlib
    import subprocess

    launch_agents = Path.home() / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    results = []
    for plist in build_flow_launchd_plists(root, python_path):
        label = plist["Label"]
        path = launch_agents / f"{label}.plist"
        body = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
        if path.exists():
            try:
                existing = plistlib.loads(path.read_bytes())
            except (OSError, plistlib.InvalidFileException):
                results.append({"label": label, "status": "EXISTS_UNMANAGED", "path": str(path)})
                continue
            if existing != plist:
                results.append({"label": label, "status": "EXISTS_DIFFERENT", "path": str(path)})
                continue
            results.append({"label": label, "status": "ALREADY_INSTALLED", "path": str(path)})
            continue
        phase16._atomic_create(path, body)
        try:
            command = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({"label": label, "status": "PLIST_CREATED_NOT_BOOTSTRAPPED", "reason": type(exc).__name__, "path": str(path)})
            continue
        results.append({
            "label": label,
            "status": "ACTIVE" if command.returncode == 0 else "PLIST_CREATED_NOT_BOOTSTRAPPED",
            "returncode": command.returncode,
            "path": str(path),
            "schedule_kst": f"{plist['StartCalendarInterval']['Hour']:02d}:{plist['StartCalendarInterval']['Minute']:02d}",
        })
    return {"status": "ACTIVE" if all(item["status"] in {"ACTIVE", "ALREADY_INSTALLED"} for item in results) else "PARTIAL", "agents": results}


def flow_launchd_status(root: Path = phase16.DEFAULT_ROOT) -> dict[str, Any]:
    import plistlib
    import subprocess

    launch_agents = Path.home() / "Library/LaunchAgents"
    results = []
    for slot, (hour, minute) in FLOW_SCHEDULES.items():
        label = f"com.krxtrader.phase16.flow.{slot}"
        path = launch_agents / f"{label}.plist"
        installed = path.is_file()
        matches = False
        if installed:
            try:
                plist = plistlib.loads(path.read_bytes())
                interval = {"Hour": hour, "Minute": minute}
                if slot in FLOW_WEEKDAYS:
                    interval["Weekday"] = FLOW_WEEKDAYS[slot]
                matches = plist.get("Label") == label and plist.get("StartCalendarInterval") == interval
            except (OSError, plistlib.InvalidFileException):
                matches = False
        try:
            loaded = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            loaded = False
        results.append({
            "label": label,
            "path": str(path),
            "installed": installed,
            "matches_expected": matches,
            "loaded": loaded,
            "schedule_kst": f"{hour:02d}:{minute:02d}",
        })
    return {"status": "PASS" if all(item["matches_expected"] and item["loaded"] for item in results) else "PARTIAL", "agents": results}


def write_contract_probe_result(root: Path, result: dict[str, Any]) -> None:
    path = DEFAULT_PHASE16B_ROOT / "phase16b-contract-probe-results.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema_version": 1, "probes": []}
    previous["probes"].extend(result.get("probes", []))
    for key in (
        "status",
        "credentials_available",
        "network_accessed",
        "contract_summary",
        "protected_period_requests",
        "response_field_values_retained",
        "credentials_retained",
    ):
        if key in result:
            previous[key] = result[key]
    previous["updated_at_utc"] = datetime.now(UTC).isoformat()
    previous["current_run_probe_count"] = len(result.get("probes", []))
    _json_write(path, previous)


def contract_probe_summary(probes: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for probe in probes:
        by_source[probe["source_id"]].append(probe)
    result = {}
    for source_id, items in sorted(by_source.items()):
        result[source_id] = {
            "observation_count": len(items),
            "all_valid": all(item.get("response_valid") is True for item in items),
            "maximum_rows_observed": max((int(item.get("row_count", 0)) for item in items), default=0),
            "all_anchor_rows_included": all(item.get("anchor_included") for item in items),
            "all_rows_at_or_before_anchor": all(item.get("future_row_count", 0) == 0 for item in items),
            "all_session_only": all(item.get("session_only") for item in items),
            "all_ordering_stable": all(item.get("ordering_stable") for item in items),
            "ordering": items[0].get("ordering") if items else "UNKNOWN",
            "earliest_probe": min((item["anchor"] for item in items), default=None),
            "latest_probe": max((item["anchor"] for item in items), default=None),
            "confidence": "EMPIRICALLY_VERIFIED" if items and all(item.get("response_valid") is True for item in items) else "PARTIAL",
        }
    return result


def create_contract_config_revision(
    *,
    current_config: dict[str, Any],
    current_config_path: Path,
    audit_report: dict[str, Any],
) -> tuple[Path, dict[str, Any], str]:
    """Create a new, immutable-on-disk config revision after valid probes."""
    summaries = audit_report.get("contract_summary", {})
    if audit_report.get("status") != "COMPLETE":
        raise ContractError("contract-probe results are not complete")
    if not all(summaries.get(source_id, {}).get("all_valid") is True for source_id in ("kis_per_stock_flow", "kis_program_flow")):
        raise ContractError("both KIS response contracts must pass before creating an activation config")
    prior_bytes = current_config_path.read_bytes()
    parent_sha = hashlib.sha256(prior_bytes).hexdigest()
    revised = json.loads(json.dumps(current_config))
    phase_cfg = revised["phase16b"]
    contracts = contracts_from_config(revised)
    for source_id in ("kis_per_stock_flow", "kis_program_flow"):
        old = contracts[source_id]
        summary = summaries[source_id]
        if summary.get("ordering") != "DESCENDING":
            raise ContractError(f"unexpected KIS response ordering for {source_id}")
        combined_count = int(summary.get("combined_observation_count", summary.get("observation_count", 0)))
        earliest = summary["earliest_probe"]
        latest = summary["latest_probe"]
        changes = {
            "confidence": "EMPIRICALLY_VERIFIED",
            "response_window_verified": True,
            "ordering": "DESCENDING",
            "verification_method": "Phase 15 historical sample plus fixed Phase 16B one-page historical probes; provider docs do not state a finite row cap.",
            "verification_observation_count": combined_count,
            "earliest_contract_probe": earliest,
            "latest_contract_probe": latest,
            "notes": (
                f"Sampled maximum {old.maximum_rows} dated rows in one page across {combined_count} "
                "historical observations; anchor included, backward dates only, descending order. "
                "This is an empirical operational envelope, not an official forever-cap; collector "
                "limits to one page and enters contract review on drift. Non-session anchors remain denied."
            ),
        }
        revised_contract = versioned_contract_update(
            old,
            new_version="kis-per-stock-flow-v2" if source_id == "kis_per_stock_flow" else "kis-program-flow-v2",
            changes=changes,
        )
        phase_cfg["source_contracts"][source_id] = revised_contract.as_dict()
        phase_cfg["source_contracts"][source_id]["earliest_contract_probe"] = earliest
        phase_cfg["source_contracts"][source_id]["latest_contract_probe"] = latest
    current_version = str(revised.get("config_version", "phase16b-v1"))
    version_match = re.search(r"-v(\d+)$", current_version)
    next_version = f"phase16b-v{int(version_match.group(1)) + 1}" if version_match else "phase16b-v2"
    revised["config_version"] = next_version
    revised["parent_config_sha256"] = parent_sha
    phase_cfg["config_version"] = next_version
    phase_cfg["parent_config_sha256"] = parent_sha
    revised["phase16b"]["effective_from"] = datetime.now(KST).date().isoformat()
    revised["effective_from"] = revised["phase16b"]["effective_from"]
    target = current_config_path.with_name(f"phase16b-collector-config-{next_version}.json")
    body = phase16._json_bytes(revised, pretty=True)
    digest = hashlib.sha256(body).hexdigest()
    if target.exists():
        if target.read_bytes() != body:
            raise ContractError("config revision path already contains different bytes")
    else:
        phase16._atomic_create(target, body)
    sidecar = target.with_suffix(".sha256")
    sidecar_body = f"{digest}  {target.name}\n".encode()
    if sidecar.exists():
        if sidecar.read_bytes() != sidecar_body:
            raise ContractError("config revision SHA sidecar differs")
    else:
        phase16._atomic_create(sidecar, sidecar_body)
    return target, revised, digest


def live_kis_contract_probes(
    *,
    root: Path,
    symbols: list[dict[str, str]],
    anchors: list[date],
    config: dict[str, Any] | None = None,
    config_sha256: str | None = None,
) -> dict[str, Any]:
    if not credentials_available():
        return {"status": "NOT_RUN_CREDENTIALS_UNAVAILABLE", "probes": [], "credentials_available": False}
    from krx_trader.kis.auth import KisAuthError, TokenManager
    from krx_trader.kis.rest import KisApiError, KisRestClient
    from krx_trader.kis.transport import UrllibTransport

    credential_values: dict[str, str] = {key: os.environ.get(key, "").strip() for key in ("KIS_APP_KEY", "KIS_APP_SECRET")}
    env_path = phase16.REPO_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        key = key.removeprefix("export ").strip()
        if sep and key in credential_values and not credential_values[key]:
            credential_values[key] = value.strip().strip("\"'")
    transport = UrllibTransport()
    token_manager = TokenManager(credential_values["KIS_APP_KEY"], credential_values["KIS_APP_SECRET"], transport, cache_path=None)
    client = KisRestClient(
        credential_values["KIS_APP_KEY"],
        credential_values["KIS_APP_SECRET"],
        token_manager,
        transport,
        min_request_interval=4.0,
        max_retries=0,
        rate_limit_path=phase16.REPO_ROOT / "runtime/kis_rate_limit.json",
    )
    config = config or json.loads(DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    contracts = contracts_from_config(config)
    if not config_sha256:
        config_sha256 = hashlib.sha256(DEFAULT_PHASE16B_CONFIG.read_bytes()).hexdigest()
    protected = phase16.protected_ranges(config)
    probe_calendar = _historical_probe_calendar(anchors)
    probes = []
    with phase16._file_lock(root / "locks/contract-probe.lock", blocking=False) as locked:
        if not locked:
            return {"status": "ALREADY_RUNNING", "probes": [], "credentials_available": True}
        for item in symbols:
            symbol, market = item["symbol"], item["market"]
            for anchor in anchors:
                for source_id, request, expected_max in (
                    ("kis_per_stock_flow", client.get_investor_flow_by_date, 31),
                    ("kis_program_flow", client.get_program_flow_by_date, 30),
                ):
                    contract = contracts[source_id]
                    decision = request_contract_guard(
                        _probe_guard_contract(contract),
                        anchor,
                        calendar=probe_calendar,
                        protected_ranges=protected,
                        evidence_class="CONTRACT_VERIFICATION_PROBE",
                    )
                    if not decision["allowed"]:
                        probes.append({
                            "evidence_class": "CONTRACT_VERIFICATION_PROBE",
                            "source_id": source_id,
                            "symbol": symbol,
                            "market": market,
                            "anchor": anchor.isoformat(),
                            "request_denied": True,
                            "request_decision": decision["request_decision"],
                            "response_valid": False,
                        })
                        continue
                    try:
                        response = execute_contract_guarded_request(
                            _probe_guard_contract(contract),
                            anchor,
                            calendar=probe_calendar,
                            protected_ranges=protected,
                            evidence_class="CONTRACT_VERIFICATION_PROBE",
                            transport=lambda request=request, symbol=symbol, anchor=anchor: request(symbol, anchor, max_pages=1),
                        )
                    except (KisApiError, KisAuthError, OSError, ValueError, TypeError) as exc:
                        probes.append({
                            "evidence_class": "CONTRACT_VERIFICATION_PROBE",
                            "source_id": source_id,
                            "symbol": symbol,
                            "market": market,
                            "anchor": anchor.isoformat(),
                            "request_error_type": type(exc).__name__,
                            "response_valid": False,
                        })
                        continue
                    probe = _probe_record(
                        source_id,
                        response,
                        symbol,
                        market,
                        anchor,
                        "stck_bsop_date",
                        expected_max,
                        probe_calendar,
                        contract_version=contract.contract_version,
                        ordering=contract.ordering,
                    )
                    probes.append(probe)
                    _record_contract_probe_snapshot(root, config_sha256, probe)
    summary = contract_probe_summary(probes)
    result = {
        "schema_version": 1,
        "status": "COMPLETE" if probes and all(p.get("response_valid") is True for p in probes) else "PARTIAL",
        "credentials_available": True,
        "network_accessed": bool(probes),
        "probes": probes,
        "contract_summary": summary,
        "protected_period_requests": 0,
        "response_field_values_retained": False,
        "credentials_retained": False,
    }
    write_contract_probe_result(root, result)
    return result


def _probe_record(
    source_id: str,
    response: dict[str, Any],
    symbol: str,
    market: str,
    anchor: date,
    date_field: str,
    expected_max: int,
    calendar: KRXSessionCalendar,
    *,
    contract_version: str,
    ordering: str,
) -> dict[str, Any]:
    rows = extract_dated_rows(response)
    dates = [_parse_provider_date(row.get(date_field)) for row in rows]
    parsed = [day for day in dates if day]
    canonical = phase16.canonical_payload_sha256(response)
    is_weekday_only = all(day.weekday() < 5 for day in parsed)
    ordering_stable = parsed == sorted(parsed, reverse=ordering == "DESCENDING")
    record = {
        "evidence_class": "CONTRACT_VERIFICATION_PROBE",
        "source_id": source_id,
        "contract_version": contract_version,
        "symbol": symbol,
        "market": market,
        "anchor": anchor.isoformat(),
        "request_parameters": {"symbol": symbol, "date": anchor.isoformat()},
        "response_sha256": canonical,
        "schema_fingerprint": phase16.schema_fingerprint(response),
        "row_count": len(rows),
        "returned_sessions": [day.isoformat() for day in parsed],
        "anchor_included": anchor in parsed,
        "future_row_count": sum(day > anchor for day in parsed),
        "session_only": is_weekday_only,
        "ordering": ordering,
        "ordering_stable": ordering_stable,
        "expected_maximum_rows": expected_max,
        "response_valid": (
            bool(rows)
            and len(rows) <= expected_max
            and anchor in parsed
            and all(day <= anchor for day in parsed)
            and is_weekday_only
            and ordering_stable
            and len(parsed) == len(rows)
        ),
        "page_count": int(response.get("_phase15_pages", 1)),
        "observed_at_utc": datetime.now(UTC).isoformat(),
    }
    return record


def collect_krx_exact_day(
    *,
    config: dict[str, Any],
    root: Path,
    session: date,
    observed_at: datetime,
    fetch: Callable[[date, str], tuple[dict[str, Any], bytes]] | None = None,
    config_sha256: str | None = None,
    observation_slot: str = "bootstrap",
    intentional_reobservation: bool = False,
) -> dict[str, Any]:
    contracts = contracts_from_config(config)
    contract = contracts["krx_market_flow_exact_day"]
    calendar = _calendar_from_config(config)
    protected = phase16.protected_ranges(config)
    decision = request_contract_guard(contract, session, calendar=calendar, protected_ranges=protected)
    if not decision["allowed"]:
        return {"status": "DEFERRED_RESPONSE_CONTRACT" if decision["request_decision"] == "DEFERRED_RESPONSE_CONTRACT" else decision["request_decision"], "request_count": 0, "network_accessed": False, "guard_decision": decision}
    fetch = fetch or KRXExactDayTransport().fetch
    store = phase16.EvidenceStore(root)
    rows_saved = 0
    hashes: dict[str, str] = {}
    snapshot_ids = []
    with phase16._file_lock(root / "locks/collector.lock", blocking=False) as locked:
        if not locked:
            return {"status": "ALREADY_RUNNING", "request_count": 0, "network_accessed": False}
        existing = store.manifests()
        for market_name in ("KOSPI", "KOSDAQ"):
            key = f"flow-response:{contract.source_id}:{market_name}:{session.isoformat()}"
            prior_same_slot = [
                item for item in existing
                if item.get("logical_key") == key
                and item.get("metadata", {}).get("observation_slot") == observation_slot
            ]
            if prior_same_slot and not intentional_reobservation:
                return {
                    "status": "ACCIDENTAL_DUPLICATE_JOB",
                    "request_count": 0,
                    "network_accessed": False,
                    "source_id": contract.source_id,
                    "request_anchor": session.isoformat(),
                    "observation_slot": observation_slot,
                }
        if config_sha256 is None:
            config_sha256 = hashlib.sha256(phase16._json_bytes(config)).hexdigest()
        request_count = 0
        for market_code, market_name in (("STK", "KOSPI"), ("KSQ", "KOSDAQ")):
            payload, raw = fetch(session, market_code)
            request_count += 1
            response_check = verify_response_contract(payload, contract, request_anchor=session, calendar=calendar, protected_ranges=protected)
            if not response_check["valid"]:
                if "PROTECTED_RANGE_CONTRACT_BREACH" in response_check["failures"]:
                    event = {"event_type": "PROTECTED_RANGE_CONTRACT_BREACH", "source_id": contract.source_id, "market": market_name, "anchor": session.isoformat(), "response_sha256": hashlib.sha256(raw).hexdigest(), "returned_sessions": response_check["protected_rows"], "payload_persisted": False}
                    _json_write(root / "contract-violations" / f"{hashlib.sha256(phase16._json_bytes(event)).hexdigest()}.json", event)
                    return {"status": "PROTECTED_RANGE_CONTRACT_BREACH", "request_count": request_count, "network_accessed": True, "payload_persisted": False, "violation": event}
                event = {
                    "event_type": "SOURCE_CONTRACT_VIOLATION",
                    "source_id": contract.source_id,
                    "market": market_name,
                    "anchor": session.isoformat(),
                    "response_sha256": hashlib.sha256(raw).hexdigest(),
                    "response_check": response_check,
                    "payload_persisted": False,
                }
                _json_write(root / "contract-violations" / f"{hashlib.sha256(phase16._json_bytes(event)).hexdigest()}.json", event)
                return {"status": "SOURCE_CONTRACT_VIOLATION", "request_count": request_count, "network_accessed": True, "response_check": response_check}
            response_sha = hashlib.sha256(raw).hexdigest()
            rows = extract_dated_rows(payload)
            logical_key = f"flow-response:{contract.source_id}:{market_name}:{session.isoformat()}"
            snapshot_id_preview = _snapshot_id_for_payload(
                source_name=contract.source_name,
                logical_key=logical_key,
                symbol=market_name,
                requested_session=session,
                observed_at=observed_at,
                payload=payload,
                raw_payload=raw,
            )
            normal = normalize_dated_rows(
                payload,
                contract=contract,
                source_name=contract.source_name,
                symbol=market_name,
                market=market_name,
                response_snapshot_id=snapshot_id_preview,
                request_anchor=session,
                observed_at=observed_at,
                prospective_start=date(2026, 10, 1),
            )
            snapshot = store.create_snapshot(
                evidence_class="PROSPECTIVE_OBSERVED",
                source_name=contract.source_name,
                provider="Korea Exchange",
                endpoint_id=contract.endpoint_id,
                request_schema_version=contract.contract_version,
                logical_key=logical_key,
                payload=payload,
                normalized_payload=normal,
                raw_payload=raw,
                persist_raw=False,
                raw_representation_basis="KRX_PUBLIC_EXACT_DAY_RESPONSE_SHA_ONLY",
                observed_at=observed_at,
                config_sha256=config_sha256,
                collector_git_sha=phase16._git_sha(),
                symbol=market_name,
                market=market_name,
                requested_session=session,
                provider_session=session,
                result_classification="HTTP_200_EXACT_DAY",
                availability_label="OBSERVED_AFTER_KRX_20KST_FINAL_PUBLICATION_WINDOW",
                record_count=len(rows),
                metadata={
                    "phase16b_flow": True,
                    "source_id": contract.source_id,
                    "response_sha256": response_sha,
                    "request_anchor": session.isoformat(),
                    "request_market_code": market_code,
                    "observation_slot": observation_slot,
                    "contract_version": contract.contract_version,
                    "contract_confidence": contract.confidence,
                    "response_check": response_check,
                    "universe_snapshot_id": _latest_universe_snapshot_id(root),
                    "field_semantics": "provider keys preserved; categories and units remain unresolved at field level",
                    "feature_status": "QUARANTINED_PROSPECTIVE",
                },
            )
            snapshot_ids.append(snapshot["snapshot_id"])
            rows_saved += len(rows)
            hashes[market_name] = response_sha
    integrity = phase16.verify_store(root)
    batch = {
        "schema_version": 1,
        "batch_id": f"phase16b-krx-{session.isoformat()}-{observed_at.astimezone(KST):%H%M%S}",
        "source_id": contract.source_id,
        "evidence_class": "PROSPECTIVE_SESSION_OBSERVED",
        "request_anchor": session.isoformat(),
        "observation_slot": observation_slot,
        "intentional_reobservation": intentional_reobservation,
        "observed_at_utc": observed_at.astimezone(UTC).isoformat(),
        "request_count": request_count,
        "retry_count": 0,
        "success_count": 2,
        "missing_count": 0,
        "failure_count": 0,
        "coverage": 1.0,
        "row_count": rows_saved,
        "snapshot_ids": snapshot_ids,
        "response_sha256_by_market": hashes,
        "universe_snapshot_id": _latest_universe_snapshot_id(root),
        "manifest_chain_tip": integrity["chain_tip_sha256"],
        "manifest_chain_valid": integrity["valid"],
    }
    _json_write(root / "phase16b-flow-batches" / f"{batch['batch_id']}.json", batch)
    write_activation_witness(
        root=root,
        phase16b_root=DEFAULT_PHASE16B_ROOT,
        config=config,
        source_id=contract.source_id,
        actual_anchor=session,
        observed_at=observed_at,
        config_sha256=config_sha256,
        reason="exact-day verified contract and protected-range preflight passed",
        protected_proof=decision,
    )
    _write_first_flow_witness(root, snapshot_ids, observed_at, session, config_sha256, rows_saved, 2, integrity["chain_tip_sha256"])
    return {"status": "COMPLETE", "network_accessed": True, **batch}


def write_activation_witness(
    *,
    root: Path,
    phase16b_root: Path,
    config: dict[str, Any],
    source_id: str,
    actual_anchor: date,
    observed_at: datetime,
    config_sha256: str,
    reason: str,
    protected_proof: dict[str, Any],
) -> dict[str, Any]:
    contract = contracts_from_config(config)[source_id]
    calendar = _calendar_from_config(config)
    first_safe = first_safe_anchor_session(
        contract,
        calendar=calendar,
        protected_ranges=phase16.protected_ranges(config),
    )
    witness = {
        "schema_version": 1,
        "source_id": source_id,
        "contract_version": contract.contract_version,
        "first_safe_anchor_session": first_safe.isoformat() if first_safe else None,
        "actual_activation_anchor": actual_anchor.isoformat(),
        "activation_observed_at_utc": observed_at.astimezone(UTC).isoformat(),
        "activation_observed_at_kst": observed_at.astimezone(KST).isoformat(),
        "reason": reason,
        "protected_range_proof": protected_proof,
        "config_sha256": config_sha256,
        "collector_git_sha": phase16._git_sha(),
    }
    for destination in (root / "source-activation-witness.json", phase16b_root / "source-activation-witness.json"):
        current = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {"schema_version": 1, "sources": {}}
        current.setdefault("sources", {})[source_id] = witness
        _json_write(destination, current)
    return witness


def _write_first_flow_witness(
    root: Path,
    snapshot_ids: list[str],
    observed_at: datetime,
    anchor: date,
    config_sha256: str,
    row_count: int,
    response_count: int,
    manifest_tip: str | None,
    *,
    source_id: str = "krx_market_flow_exact_day",
    symbol_count: int = 2,
) -> None:
    path = root / "first-flow-observation.json"
    if path.exists():
        return
    _json_write(path, {
        "schema_version": 1,
        "source": source_id,
        "observed_at_utc": observed_at.astimezone(UTC).isoformat(),
        "observed_at_kst": observed_at.astimezone(KST).isoformat(),
        "anchor_session": anchor.isoformat(),
        "symbol_count": symbol_count,
        "response_count": response_count,
        "row_count": row_count,
        "snapshot_ids": snapshot_ids,
        "config_sha256": config_sha256,
        "collector_git_sha": phase16._git_sha(),
        "manifest_tip": manifest_tip,
        "evidence_class": "PROSPECTIVE_SESSION_OBSERVED",
        "provider_values_retained_outside_phase16_store": False,
    })


def flow_status(config: dict[str, Any], root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(KST)
    status = flow_preflight(
        config,
        now=now,
        credentials_available=credentials_available(),
        collector_healthy=phase16.verify_store(root)["valid"],
        contract_violation_sources=_contract_violation_sources(root),
    )
    manifests = phase16.EvidenceStore(root).manifests() if root.exists() else []
    flow_manifests = [item for item in manifests if item.get("metadata", {}).get("phase16b_flow") is True]
    prospective_flow_manifests = [item for item in flow_manifests if item.get("evidence_class") == "PROSPECTIVE_OBSERVED"]
    historical_probe_manifests = [item for item in flow_manifests if item.get("evidence_class") == "HISTORICAL_REVISION_PROBE"]
    probe_path = DEFAULT_PHASE16B_ROOT / "phase16b-contract-probe-results.json"
    probe_data = json.loads(probe_path.read_text(encoding="utf-8")) if probe_path.exists() else {"probes": []}
    revision = revision_summary(root)
    prospective_curves = [
        curve for curve in revision["revision_curve"]
        if curve.get("evidence_class") == "PROSPECTIVE_SESSION_OBSERVED"
    ]
    sessions = {curve["provider_session"] for curve in prospective_curves}
    per_stock_curves = [
        curve for curve in revision["revision_curve"]
        if curve.get("source_id") == "kis_per_stock_flow"
        and curve.get("evidence_class") == "PROSPECTIVE_SESSION_OBSERVED"
    ]
    per_stock_sessions = {curve["provider_session"] for curve in per_stock_curves}
    per_stock_multi_vintage_sessions = len({
        curve["provider_session"] for curve in per_stock_curves if curve.get("vintage_count", 0) > 1
    })
    prospective_multi_vintage_sessions = len({
        curve["provider_session"] for curve in prospective_curves if curve.get("vintage_count", 0) > 1
    })
    chain = phase16.verify_store(root)
    batches_path = root / "phase16b-flow-batches"
    batches = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(batches_path.glob("*.json"))] if batches_path.exists() else []
    full_batches = [item for item in batches if item.get("source_id") == "kis_per_stock_flow" and item.get("collection_scope") == "FROZEN_RESEARCH_COHORT"]
    full_coverage = [float(item["coverage"]) for item in full_batches if isinstance(item.get("coverage"), (int, float))]
    full_coverage_health = [item.get("coverage_health") for item in full_batches]
    coverage_acceptable = bool(full_coverage) and all(value >= 0.95 for value in full_coverage)
    schema_events_path = root / "revision/schema-events"
    core_source_name = contracts_from_config(config)["kis_per_stock_flow"].source_name
    core_schema_events = []
    if schema_events_path.exists():
        for path in schema_events_path.glob("*.json"):
            event = json.loads(path.read_text(encoding="utf-8"))
            if event.get("source_name") == core_source_name and event.get("evidence_class") == "PROSPECTIVE_OBSERVED":
                core_schema_events.append(event)
    schema_stable = not core_schema_events
    per_stock_profile = [
        profile for profile in revision["field_revision_profile"]
        if profile.get("logical_key", "").startswith("KIS per-stock investor daily flow:")
    ]
    per_stock_source_summary = revision.get("source_summaries", {}).get("kis_per_stock_flow", {})
    readiness = (
        len(per_stock_sessions) >= 20
        and per_stock_multi_vintage_sessions >= 10
        and chain["valid"]
        and coverage_acceptable
        and schema_stable
    )
    latest_anchor_by_source = {}
    for source_id in {item.get("metadata", {}).get("source_id") for item in prospective_flow_manifests}:
        anchors = [item.get("requested_session") for item in prospective_flow_manifests if item.get("metadata", {}).get("source_id") == source_id and item.get("requested_session")]
        latest_anchor_by_source[source_id] = max(anchors) if anchors else None
    status.update({
        "flow_response_snapshot_count": len(flow_manifests),
        "flow_normalized_row_count": sum(item.get("record_count", 0) for item in flow_manifests),
        "prospective_flow_response_snapshot_count": len(prospective_flow_manifests),
        "historical_revision_probe_snapshot_count": len(historical_probe_manifests),
        "prospective_flow_sessions": len(sessions),
        "multi_vintage_flow_sessions": prospective_multi_vintage_sessions,
        "per_stock_prospective_sessions": len(per_stock_sessions),
        "per_stock_multi_vintage_sessions": per_stock_multi_vintage_sessions,
        "per_stock_historical_reobserved_sessions": per_stock_source_summary.get("historical_reobserved_sessions", 0),
        "per_stock_historical_revision_probe_sessions": per_stock_source_summary.get("historical_revision_probe_sessions", 0),
        "revision_comparison_count": revision["revision_comparison_count"],
        "value_revision_count": revision["value_revision_count"],
        "schema_change_count": revision["schema_change_count"],
        "median_revision_magnitude": revision["median_revision_magnitude"],
        "p90_revision_magnitude": revision["p90_revision_magnitude"],
        "median_revision_age_days": revision["median_revision_age_days"],
        "p90_revision_age_days": revision["p90_revision_age_days"],
        "per_stock_field_revision_profile": per_stock_profile,
        "response_contract_probe_count": len(probe_data.get("probes", [])),
        "latest_anchor_collected_by_source": latest_anchor_by_source,
        "latest_universe_snapshot_id": _latest_universe_snapshot_id(root),
        "full_cohort_coverage": full_coverage,
        "full_cohort_coverage_health": full_coverage_health,
        "coverage_acceptable": coverage_acceptable,
        "schema_stable": schema_stable,
        "core_source_schema_change_count": len(core_schema_events),
        "manifest_chain_valid": chain["valid"],
        "manifest_chain_tip": chain["chain_tip_sha256"],
        "phase17_source_stability_ready": "YES" if readiness else "NO",
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
        "alpha": "UNPROVEN",
        "shadow_next_session": "NO",
        "live": "DISABLED",
    })
    return status


def _verify_preexisting_evidence(root: Path) -> dict[str, Any]:
    baseline_path = DEFAULT_PHASE16B_ROOT / "pre-phase16b-evidence-baseline.json"
    if not baseline_path.is_file():
        return {"status": "NOT_AVAILABLE", "reason": "pre-Phase16B integrity baseline is missing"}
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    errors = []
    checked = 0
    for item in baseline.get("phase16_snapshots", []):
        manifest_path = root / item["manifest_path"]
        if not manifest_path.is_file() or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != item["manifest_file_sha256"]:
            errors.append(f"manifest changed: {item['snapshot_id']}")
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("current_manifest_sha256") != item["manifest_current_sha256"]:
            errors.append(f"manifest identity changed: {item['snapshot_id']}")
        for payload in item.get("payload_files", []):
            path = root / payload["path"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != payload["sha256"]:
                errors.append(f"payload changed: {payload['path']}")
        checked += 1
    chain_path = root / "prospective/manifests/manifest-chain.jsonl"
    lines = chain_path.read_bytes().splitlines(keepends=True) if chain_path.exists() else []
    prefix_sha = hashlib.sha256(b"".join(lines[: baseline.get("phase16_snapshot_count", 0)])).hexdigest() if lines else None
    prefix_matches = prefix_sha == baseline.get("manifest_chain_sha256")
    if not prefix_matches:
        errors.append("original Phase 16 manifest-chain prefix changed")
    previous_phase = phase16.verify_previous_phase_snapshot(root)
    if previous_phase.get("status") != "PASS":
        errors.append("Phase 5-15 artifact snapshot failed verification")
    return {
        "status": "PASS" if not errors else "FAIL",
        "preexisting_phase16_snapshot_count": checked,
        "preexisting_phase16_manifest_prefix_unchanged": prefix_matches,
        "phase5_15": previous_phase,
        "errors": errors,
    }


def write_phase16b_artifacts(
    *,
    config: dict[str, Any],
    config_sha256: str,
    root: Path,
    preflight: dict[str, Any] | None = None,
    schedule_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    phase16b_root = DEFAULT_PHASE16B_ROOT
    phase16b_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    preflight = preflight or flow_preflight(
        config,
        now=datetime.now(KST),
        credentials_available=credentials_available(),
        collector_healthy=phase16.verify_store(root)["valid"],
        contract_violation_sources=_contract_violation_sources(root),
        config_sha256=config_sha256,
    )
    contracts = {key: contract.as_dict() for key, contract in contracts_from_config(config).items()}
    _json_write(root / "contracts/phase16b-source-contracts.json", {
        "schema_version": 1,
        "config_version": config.get("config_version"),
        "config_sha256": config_sha256,
        "contracts": contracts,
    })
    _json_write(root / "contracts/phase16b-config-version.json", {
        "config_version": config.get("config_version"),
        "config_sha256": config_sha256,
        "parent_config_sha256": config.get("parent_config_sha256"),
        "effective_from": config.get("effective_from"),
        "path": DEFAULT_PHASE16B_CONFIG.relative_to(phase16.REPO_ROOT).as_posix(),
    })
    _json_write(phase16b_root / "phase16b-source-contracts.json", {
        "schema_version": 1,
        "config_version": config.get("config_version"),
        "config_sha256": config_sha256,
        "contracts": contracts,
    })
    _json_write(phase16b_root / "phase16b-contract-probe-plan.json", {
        "schema_version": 1,
        **config.get("phase16b", {}).get("probe_policy", {}),
        "guard": "each fixed historical request uses an empirical one-page envelope and is denied if any possible session overlaps a protected range",
        "non_session_anchor_policy": "DENY_UNVERIFIED; no Saturday/Sunday/holiday request is issued",
        "field_values_retained": False,
        "credentials_retained": False,
    })
    _json_write(phase16b_root / "phase16b-source-activation.json", preflight)
    _json_write(phase16b_root / "phase16b-safe-anchor-analysis.json", {
        "schema_version": 1,
        "session_calendar": preflight["session_calendar"],
        "latest_completed_krx_session": preflight.get("latest_completed_krx_session"),
        "sources": [{key: row.get(key) for key in (
            "source_id", "first_safe_anchor_session", "latest_completed_krx_session",
            "possible_response_start", "possible_response_end", "protected_overlap",
            "sessions_until_eligibility", "collection_state", "reason",
        )} for row in preflight["sources"]],
    })
    krx_contract = contracts["krx_market_flow_exact_day"]
    _json_write(phase16b_root / "phase16b-market-flow-exact-day-audit.json", {
        "schema_version": 1,
        "status": "AVAILABLE_EXACT_DAY",
        "source_id": "krx_market_flow_exact_day",
        "contract": krx_contract,
        "official_route": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT022",
        "official_route_name": "시장별 매매동향 (market-by-market trading trends)",
        "official_semantics": "market-specific daily trading volume/value by sell, buy and net; KOSPI and KOSDAQ are separate queries",
        "request_range": "strtDd=endDd=one verified completed KRX session",
        "publication_timing": "KRX states final same-day detail is available after 20:00 KST; scheduler observes at 20:30 or later",
        "empirical_exact_day_probe_count": krx_contract["verification_observation_count"],
        "response_field_semantics": "raw KRX field keys preserved; category-level mapping and units remain UNKNOWN pending field dictionary mapping",
        "substitutes_for_kis": False,
    })
    _json_write(phase16b_root / "phase16b-scheduler-audit.json", schedule_audit or flow_launchd_status(root))
    _json_write(phase16b_root / "phase16b-storage-projection.json", config.get("phase16b", {}).get("storage_projection", {}))
    flow = flow_status(config, root, now=datetime.now(KST))
    integrity = _verify_preexisting_evidence(root)
    readiness = flow.get("phase17_source_stability_ready") == "YES"
    summary = {
        "schema_version": 1,
        "phase16b": "COMPLETE" if integrity["status"] == "PASS" and phase16.verify_store(root)["valid"] else "PARTIAL",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "config_version": config.get("config_version"),
        "config_sha256": config_sha256,
        "parent_config_sha256": config.get("parent_config_sha256"),
        "sources": preflight["sources"],
        "flow_status": flow,
        "phase5_15_integrity": integrity.get("phase5_15"),
        "preexisting_phase16_snapshot_integrity": integrity,
        "manifest_chain_valid": phase16.verify_store(root)["valid"],
        "phase17_source_stability_ready": "YES" if readiness else "NO",
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
        "alpha": "UNPROVEN",
        "shadow_next_session": "NO",
        "live": "DISABLED",
    }
    _json_write(phase16b_root / "phase16b-summary.json", summary)
    _json_write(phase16b_root / "previous-evidence-integrity.json", integrity)
    files = [path for path in sorted(phase16b_root.glob("*.json")) if path.name not in {"phase16b-artifact-index.json", "artifact-integrity.json"}]
    index = {
        "schema_version": 1,
        "artifacts": [
            {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            for path in files
        ],
    }
    _json_write(phase16b_root / "phase16b-artifact-index.json", index)
    all_indexed = files + [phase16b_root / "phase16b-artifact-index.json"]
    _json_write(phase16b_root / "artifact-integrity.json", {
        "schema_version": 1,
        "artifacts": [
            {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            for path in all_indexed
        ],
    })
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phase16b")
    parser.add_argument("--root", type=Path, default=phase16.DEFAULT_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_PHASE16B_CONFIG)
    parser.add_argument("command", choices=("flow-preflight", "revisions", "status", "health"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config_sha = hashlib.sha256(args.config.read_bytes()).hexdigest()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.command == "flow-preflight":
        result = flow_preflight(
            config,
            now=datetime.now(KST),
            credentials_available=credentials_available(),
            collector_healthy=phase16.verify_store(args.root)["valid"],
            config_sha256=config_sha,
        )
        write_phase16b_artifacts(config=config, config_sha256=config_sha, root=args.root, preflight=result)
        _json_write(DEFAULT_PHASE16B_ROOT / "phase16b-safe-anchor-analysis.json", {
            "schema_version": 1,
            "session_calendar": result["session_calendar"],
            "sources": [{k: row.get(k) for k in ("source_id", "first_safe_anchor_session", "latest_completed_krx_session", "possible_response_start", "possible_response_end", "protected_overlap", "sessions_until_eligibility", "collection_state", "reason")} for row in result["sources"]],
        })
    elif args.command == "revisions":
        result = revision_summary(args.root)
        _json_write(DEFAULT_PHASE16B_ROOT / "phase16b-revision-report.json", result)
    elif args.command == "status":
        result = flow_status(config, args.root)
        _json_write(DEFAULT_PHASE16B_ROOT / "phase16b-summary.json", result)
    else:
        status = flow_status(config, args.root)
        result = {"status": "PASS" if status.get("manifest_chain_valid") else "FAIL", "manifest_chain_valid": status.get("manifest_chain_valid"), "source_states": {row["source_id"]: row["collection_state"] for row in status["sources"]}}
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if result.get("status") not in {"FAIL", "FAILED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

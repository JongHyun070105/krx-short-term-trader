from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

QUALIFICATIONS = {
    "QUALIFIED",
    "PARTIAL",
    "REJECTED",
    "NOT_AVAILABLE",
    "PROSPECTIVE_ONLY",
}
REPRODUCIBILITY = {"IDENTICAL", "SEMANTICALLY_IDENTICAL", "REVISED", "UNAVAILABLE"}
PIT_STATES = {"SAFE_INTRADAY", "END_OF_SESSION", "NEXT_SESSION_ONLY", "UNKNOWN", "UNSAFE"}
EXTERNAL_2026 = (date(2026, 1, 5), date(2026, 4, 16))
HOLDOUT_2026 = (date(2026, 7, 28), date(2026, 8, 28))
SCORECARD_FIELDS = (
    "source_name",
    "provider",
    "official_or_third_party",
    "access_method",
    "documentation_url",
    "data_type",
    "earliest_date_tested",
    "latest_date_tested",
    "date_range_support",
    "request_granularity",
    "pagination",
    "rate_limit",
    "auth_required",
    "point_in_time_safety",
    "publication_timestamp",
    "revision_semantics",
    "delisted_support",
    "symbol_lineage_support",
    "reproducibility",
    "missing_data_behavior",
    "license_or_usage_limitation",
    "estimated_full_acquisition_requests",
    "estimated_full_acquisition_time",
    "qualification",
)


class Phase15DateError(ValueError):
    """Raised when a source-audit request targets a protected 2026 evidence block."""


def assert_phase15_source_date(value: date | str) -> None:
    day = date.fromisoformat(value) if isinstance(value, str) else value
    if EXTERNAL_2026[0] <= day <= EXTERNAL_2026[1]:
        raise Phase15DateError("Phase 15 source qualification cannot query 2026 External")
    if HOLDOUT_2026[0] <= day <= HOLDOUT_2026[1]:
        raise Phase15DateError("Phase 15 source qualification cannot query the 2026 Holdout")


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        if all(isinstance(item, dict) for item in items):
            return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return items
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def canonical_response_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def classify_reproducibility(first: Any | None, second: Any | None) -> str:
    if first is None or second is None:
        return "UNAVAILABLE"
    if json.dumps(first, ensure_ascii=False, separators=(",", ":")) == json.dumps(
        second, ensure_ascii=False, separators=(",", ":")
    ):
        return "IDENTICAL"
    if canonical_response_hash(first) == canonical_response_hash(second):
        return "SEMANTICALLY_IDENTICAL"
    return "REVISED"


def classify_response_hashes(
    first_raw: str | None,
    first_canonical: str | None,
    second_raw: str | None,
    second_canonical: str | None,
) -> str:
    if not first_raw or not first_canonical or not second_raw or not second_canonical:
        return "UNAVAILABLE"
    if first_raw == second_raw:
        return "IDENTICAL"
    if first_canonical == second_canonical:
        return "SEMANTICALLY_IDENTICAL"
    return "REVISED"


def derive_qualification(
    *,
    available: bool,
    historical: bool,
    reproducible: bool,
    semantics_documented: bool,
    sufficient_depth: bool,
    timestamp_safe: bool,
    missing_behavior_known: bool,
    acquisition_practical: bool,
    cacheable: bool,
    use_permitted: bool,
) -> str:
    if not available:
        return "NOT_AVAILABLE"
    if not historical:
        return "PROSPECTIVE_ONLY"
    if not use_permitted or not reproducible:
        return "REJECTED"
    requirements = (
        semantics_documented,
        sufficient_depth,
        timestamp_safe,
        missing_behavior_known,
        acquisition_practical,
        cacheable,
    )
    return "QUALIFIED" if all(requirements) else "PARTIAL"


def validate_scorecard(record: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in SCORECARD_FIELDS if field not in record]
    extra = sorted(set(record) - set(SCORECARD_FIELDS))
    if missing or extra:
        raise ValueError(f"invalid source scorecard fields: missing={missing}, extra={extra}")
    if record["qualification"] not in QUALIFICATIONS:
        raise ValueError("source scorecard has an unsupported qualification")
    if record["point_in_time_safety"] not in PIT_STATES:
        raise ValueError("source scorecard has an unsupported point-in-time state")
    if record["reproducibility"] not in REPRODUCIBILITY:
        raise ValueError("source scorecard has an unsupported reproducibility state")
    for field in ("earliest_date_tested", "latest_date_tested"):
        if record[field] is not None:
            date.fromisoformat(record[field])
    if (
        record["earliest_date_tested"]
        and record["latest_date_tested"]
        and record["earliest_date_tested"] > record["latest_date_tested"]
    ):
        raise ValueError("source scorecard date range is reversed")
    if not isinstance(record["documentation_url"], str) or not record["documentation_url"].startswith(
        "https://"
    ):
        raise ValueError("source scorecard documentation_url must be https")
    for field in ("estimated_full_acquisition_requests",):
        value = record[field]
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ValueError(f"{field} must be a non-negative integer or null")
    return dict(record)


def classify_availability(*, known_at: str | None, session_date: date | None) -> str:
    if known_at:
        timestamp = datetime.fromisoformat(known_at)
        if timestamp.tzinfo is None:
            return "UNKNOWN"
        return "SAFE_INTRADAY"
    if session_date is not None:
        return "NEXT_SESSION_ONLY"
    return "UNKNOWN"


def request_manifest_record(
    *,
    source: str,
    endpoint: str,
    parameters: dict[str, Any],
    symbol: str | None,
    requested_date: date,
) -> dict[str, Any]:
    assert_phase15_source_date(requested_date)
    forbidden = {"authorization", "access_token", "appkey", "appsecret", "cookie", "account_no"}
    if forbidden.intersection(key.lower() for key in parameters):
        raise ValueError("request manifest parameters must not contain credentials or account fields")
    safe_parameters = dict(sorted(parameters.items()))
    return {
        "source": source,
        "endpoint": endpoint,
        "parameters": safe_parameters,
        "symbol": symbol,
        "requested_date": requested_date.isoformat(),
        "request_sha256": canonical_response_hash(safe_parameters),
    }


def validate_pilot_rows(
    rows: list[dict[str, Any]],
    *,
    requested_date: date,
    date_field: str,
    allow_prior_sessions: bool = False,
) -> dict[str, Any]:
    dates: list[str] = []
    duplicate_count = 0
    seen: set[bytes] = set()
    invalid_numeric_fields: list[str] = []
    negative_impossible_fields: list[str] = []
    impossible_negative_tokens = {"stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr", "acml_vol", "acml_tr_pbmn"}
    for row in rows:
        key = canonical_json_bytes(row)
        if key in seen:
            duplicate_count += 1
        seen.add(key)
        raw_date = row.get(date_field)
        if raw_date not in (None, ""):
            try:
                value = str(raw_date)
                parsed = (
                    date.fromisoformat(value)
                    if "-" in value
                    else date(int(value[:4]), int(value[4:6]), int(value[6:8]))
                )
            except ValueError:
                invalid_numeric_fields.append(f"{date_field}:invalid-date")
            else:
                dates.append(parsed.isoformat())
        for name, value in row.items():
            if (
                name.lower() in impossible_negative_tokens
                or any(token in name.lower() for token in ("qty", "amt", "price", "vol", "value"))
            ) and value not in (
                None,
                "",
            ):
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    invalid_numeric_fields.append(str(name))
                else:
                    if not math.isfinite(numeric):
                        invalid_numeric_fields.append(str(name))
                    if name.lower() in impossible_negative_tokens and numeric < 0:
                        negative_impossible_fields.append(str(name))
    unique_dates = sorted(set(dates))
    if not unique_dates:
        date_consistent = None
    elif allow_prior_sessions:
        date_consistent = unique_dates[-1] == requested_date.isoformat() and all(
            item <= requested_date.isoformat() for item in unique_dates
        )
    else:
        date_consistent = unique_dates == [requested_date.isoformat()]
    return {
        "row_count": len(rows),
        "duplicate_rows": duplicate_count,
        "returned_dates": unique_dates,
        "requested_date_matches": date_consistent,
        "invalid_numeric_fields": sorted(set(invalid_numeric_fields)),
        "negative_impossible_fields": sorted(set(negative_impossible_fields)),
        "missing_is_preserved": True,
    }


def validate_date_range(start: date, end: date) -> tuple[date, date]:
    if start > end:
        raise ValueError("date range start must not be after end")
    if any(start <= protected_end and protected_start <= end for protected_start, protected_end in (EXTERNAL_2026, HOLDOUT_2026)):
        raise Phase15DateError("Phase 15 date range intersects a protected 2026 evidence block")
    return start, end


def classify_historical_depth(
    tested_dates: list[date], *, required_start: date
) -> str:
    if not tested_dates:
        return "NOT_AVAILABLE"
    if min(tested_dates) <= required_start:
        return "AVAILABLE"
    return "PARTIAL"


def classify_delisted_support(
    *, metadata_found: bool, flow_returned: bool, price_returned: bool
) -> str:
    if metadata_found and flow_returned and price_returned:
        return "AVAILABLE"
    if metadata_found or flow_returned or price_returned:
        return "PARTIAL"
    return "NOT_AVAILABLE"


def estimate_acquisition(
    *,
    symbols: int,
    sessions: int,
    requests_per_symbol_session: int = 1,
    retry_rate: float = 0.01,
    interval_seconds: float = 4.0,
    average_response_bytes: int = 2_000,
) -> dict[str, int | float | str]:
    if min(symbols, sessions, requests_per_symbol_session) < 0:
        raise ValueError("request dimensions cannot be negative")
    if retry_rate < 0 or interval_seconds < 0 or average_response_bytes < 0:
        raise ValueError("cost assumptions cannot be negative")
    requests = symbols * sessions * requests_per_symbol_session
    retries = int(requests * retry_rate + 0.999999)
    seconds = (requests + retries) * interval_seconds
    hours = seconds / 3600
    feasibility = "FAST" if hours < 1 else "MANAGEABLE" if hours < 24 else "EXPENSIVE" if hours < 168 else "IMPRACTICAL"
    return {
        "symbols": symbols,
        "sessions": sessions,
        "requests_per_symbol_session": requests_per_symbol_session,
        "base_requests": requests,
        "expected_retries": retries,
        "total_requests": requests + retries,
        "rate_limit_interval_seconds": interval_seconds,
        "estimated_seconds": int(seconds),
        "estimated_hours": round(hours, 2),
        "estimated_days_continuous": round(hours / 24, 2),
        "estimated_artifact_bytes": requests * average_response_bytes,
        "feasibility": feasibility,
    }


def append_cache_record(path: Path, record: dict[str, Any]) -> bool:
    """Append one redacted hash record; identical request/hash pairs are idempotent."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.seek(0)
            for line in stream:
                try:
                    previous = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    previous.get("request_sha256") == record.get("request_sha256")
                    and previous.get("canonical_response_sha256")
                    == record.get("canonical_response_sha256")
                    and previous.get("query_round") == record.get("query_round")
                    and previous.get("dq_policy_version") == record.get("dq_policy_version")
                    and previous.get("source_git_sha") == record.get("source_git_sha")
                ):
                    return False
            stream.seek(0, os.SEEK_END)
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            return True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_cached_request(path: Path, request_sha256: str, query_round: int) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    found = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                record.get("request_sha256") == request_sha256
                and record.get("query_round") == query_round
            ):
                found = record
    return found


def compare_artifact_snapshot(snapshot: dict[str, Any], repository_root: Path) -> dict[str, Any]:
    results = []
    for entry in snapshot.get("files", []):
        path = repository_root / entry["path"]
        exists = path.is_file()
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if exists else None
        results.append(
            {
                "path": entry["path"],
                "exists": exists,
                "expected_sha256": entry["sha256"],
                "actual_sha256": actual,
                "unchanged": exists and actual == entry["sha256"],
            }
        )
    return {
        "count": len(results),
        "unchanged": sum(item["unchanged"] for item in results),
        "changed": [item["path"] for item in results if item["exists"] and not item["unchanged"]],
        "missing": [item["path"] for item in results if not item["exists"]],
        "status": "PASS" if results and all(item["unchanged"] for item in results) else "FAIL",
        "files": results,
    }


def build_artifact_index(directory: Path, *, exclude: set[str] | None = None) -> dict[str, Any]:
    omitted = exclude or set()
    records = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name in omitted or path.name in {
            "phase15-artifact-index.json",
            "artifact-integrity.json",
        }:
            continue
        records.append(
            {
                "path": str(path.relative_to(directory)),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {"schema_version": 1, "artifact_count": len(records), "files": records}


def verify_artifact_index(directory: Path, index: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for entry in index.get("files", []):
        path = directory / entry["path"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        checks.append(
            {
                "path": entry["path"],
                "exists": path.is_file(),
                "expected_sha256": entry["sha256"],
                "actual_sha256": actual,
                "matches": actual == entry["sha256"],
            }
        )
    return {
        "status": "PASS" if checks and all(item["matches"] for item in checks) else "FAIL",
        "checked": len(checks),
        "failed": [item["path"] for item in checks if not item["matches"]],
        "files": checks,
    }


@dataclass(frozen=True, slots=True)
class SymbolLineage:
    symbol: str
    valid_from: str | None
    valid_to: str | None
    event: str | None
    successor_symbol: str | None
    source_url: str | None


def period_overlaps(start: date, end: date, used_periods: list[dict[str, Any]]) -> bool:
    for period in used_periods:
        used_start = date.fromisoformat(period["start"])
        used_end = date.fromisoformat(period["end"])
        if start <= used_end and used_start <= end:
            return True
    return False


def classify_fresh_period(
    start: date,
    end: date,
    *,
    used_outcome_periods: list[dict[str, Any]],
    prior_source_used: bool,
    price_only_touched_periods: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("fresh period start must not be after end")
    outcome_overlap = period_overlaps(start, end, used_outcome_periods)
    source_overlap = prior_source_used
    price_overlap = period_overlaps(start, end, price_only_touched_periods or [])
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "outcome_overlap": outcome_overlap,
        "new_source_previously_used": source_overlap,
        "price_only_overlap": price_overlap,
        "classification": "CONTAMINATED" if outcome_overlap or source_overlap else "FRESH_WITH_PRICE_CAVEAT" if price_overlap else "FRESH",
    }


def proposed_factor_registry() -> dict[str, Any]:
    entries = [
        ("foreign_net_flow_ratio", "per_stock_investor_flow"),
        ("institution_net_flow_ratio", "per_stock_investor_flow"),
        ("individual_net_flow_ratio", "per_stock_investor_flow"),
        ("flow_persistence_5d", "per_stock_investor_flow"),
        ("flow_acceleration", "per_stock_investor_flow"),
        ("program_net_flow_ratio", "program_trading"),
        ("sector_relative_return", "sector_context"),
        ("days_since_disclosure", "corporate_events"),
    ]
    return {
        "schema_version": 1,
        "status": "PROPOSED",
        "active_factor_registry_modified": False,
        "availability_time_required": True,
        "entries": [
            {
                "factor_id": factor_id,
                "source_family": family,
                "status": "DEFERRED",
                "label": "POTENTIAL_FEATURE_ONLY",
                "availability_time": "UNKNOWN" if factor_id == "days_since_disclosure" else "NEXT_SESSION_ONLY",
                "predictive_result": None,
            }
            for factor_id, family in entries
        ],
    }


def write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temp_path = Path(stream.name)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()

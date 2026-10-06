"""Fail-closed request and response checks for the KRX MDCSTAT023 screen.

The screen is a candidate only.  This module deliberately contains no network
transport or scheduler: the current KRX website terms prohibit unauthorized
automated collection, and no KRX automation authorization was available for
this qualification run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

SOURCE_ID = "krx_exact_day_per_stock_investor"
SCREEN_ID = "MDCSTAT023"
SOURCE_URL = "https://data.krx.co.kr/contents/MDC/STAT/standard/MDCSTAT023.jsp"
REQUEST_VERSION = "MDCSTAT023_SCREEN_FORM_V1"
SCREEN_MAP_VERSION = "MDCSTAT023_HTML_BINDINGS_V1"
PARSER_VERSION = "PHASE16D_RESPONSE_PARSER_V1"
NORMALIZATION_VERSION = "PHASE16D_UNKNOWN_SCALE_QUARANTINE_V1"

PROTECTED_RANGES: tuple[tuple[date, date, str], ...] = (
    (date(2026, 1, 5), date(2026, 4, 16), "EXTERNAL_2026"),
    (date(2026, 7, 28), date(2026, 8, 28), "JUL_AUG_BLOCK"),
)

SERVICE_BY_MODE = {
    "PERIOD_TOTAL": "dbms/MDC/STAT/standard/MDCSTAT02301",
    "DAILY_SUMMARY": "dbms/MDC/STAT/standard/MDCSTAT02302",
    "DAILY_DETAIL": "dbms/MDC/STAT/standard/MDCSTAT02303",
}

_ISIN = re.compile(r"^KR[A-Z0-9]{10}$")
_SHORT_CODE = re.compile(r"^\d{6}$")
_DATE_KEYS = ("TRD_DD", "trdDd", "TRD_DD_STR", "date")


class Phase16DKrxError(ValueError):
    """Base error for an invalid or unqualified KRX request/response."""


class ProtectedRangeDenied(Phase16DKrxError):
    """The exact requested session intersects a protected research period."""


class UnverifiedSessionDenied(Phase16DKrxError):
    """The caller did not prove that the exact date is a KRX session."""


class AuthorizationRequired(Phase16DKrxError):
    """No evidence of provider permission for automated collection was given."""


@dataclass(frozen=True)
class ExactDayRequest:
    source_id: str
    screen_id: str
    request_version: str
    request_mode: str
    session: date
    market: str
    short_code: str
    standard_code: str
    service_id: str
    parameters: Mapping[str, str]


def _protected_labels(
    session: date,
    protected_ranges: Sequence[tuple[date, date, str]],
) -> list[str]:
    return [label for start, end, label in protected_ranges if start <= session <= end]


def build_exact_day_request(
    *,
    session: date,
    market: str,
    short_code: str,
    standard_code: str,
    request_mode: str,
    session_is_verified: Callable[[date], bool],
    measure: str = "volume",
    side: str = "net",
    detailed: bool = False,
    protected_ranges: Sequence[tuple[date, date, str]] = PROTECTED_RANGES,
) -> ExactDayRequest:
    """Build one one-security, one-session request from observed screen fields.

    Market is retained as local evidence metadata; the official screen has no
    market request field.  Both dates are always the same session.  The caller
    must provide a verified KRX-session lookup before this function succeeds.
    """
    if type(session) is not date:
        raise Phase16DKrxError("session must be a datetime.date")
    if not callable(session_is_verified) or not session_is_verified(session):
        raise UnverifiedSessionDenied("session is not verified by a KRX session calendar")
    labels = _protected_labels(session, protected_ranges)
    if labels:
        raise ProtectedRangeDenied(
            "request denied before transport for protected period(s): " + ",".join(labels)
        )
    if market not in {"KOSPI", "KOSDAQ"}:
        raise Phase16DKrxError("market must be an explicitly classified KOSPI or KOSDAQ symbol")
    if not _SHORT_CODE.fullmatch(short_code):
        raise Phase16DKrxError("short_code must be a six-digit KRX code")
    if not _ISIN.fullmatch(standard_code):
        raise Phase16DKrxError("standard_code must be a 12-character KR standard code")

    mode = request_mode.upper()
    if mode not in SERVICE_BY_MODE:
        raise Phase16DKrxError(f"unsupported screen request mode: {request_mode}")
    if measure not in {"volume", "value"}:
        raise Phase16DKrxError("measure must be volume or value")
    if side not in {"sell", "buy", "net"}:
        raise Phase16DKrxError("side must be sell, buy, or net")
    if mode == "PERIOD_TOTAL" and (measure != "volume" or side != "net"):
        # MDCSTAT02301 exposes all three sides and both measures in one table.
        # Its measure/side selectors are not needed for the grid request.
        raise Phase16DKrxError(
            "period total returns all measures and sides; selectors must be defaults"
        )
    if mode == "DAILY_DETAIL" and not detailed:
        raise Phase16DKrxError("daily detail requires the official 상세보기 control")
    if mode == "DAILY_SUMMARY" and detailed:
        raise Phase16DKrxError("daily summary cannot request 상세보기")

    inq_type = "1" if mode == "PERIOD_TOTAL" else "2"
    parameters = {
        "locale": "ko_KR",
        "inqTpCd": inq_type,
        "isuCd": standard_code,
        "isuCd2": short_code,
        "param1isuCd_finder_stkisu0": "ALL",
        "strtDd": session.strftime("%Y%m%d"),
        "endDd": session.strftime("%Y%m%d"),
    }
    if mode != "PERIOD_TOTAL":
        parameters.update(
            {
                "trdVolVal": "1" if measure == "volume" else "2",
                "askBid": {"sell": "1", "buy": "2", "net": "3"}[side],
            }
        )
        if detailed:
            parameters["detailView"] = "1"

    return ExactDayRequest(
        source_id=SOURCE_ID,
        screen_id=SCREEN_ID,
        request_version=REQUEST_VERSION,
        request_mode=mode,
        session=session,
        market=market,
        short_code=short_code,
        standard_code=standard_code,
        service_id=SERVICE_BY_MODE[mode],
        parameters=parameters,
    )


def require_automation_authorization(reference: str | None) -> str:
    """Fail closed until a provider authorization reference is supplied."""
    if not isinstance(reference, str) or not reference.strip():
        raise AuthorizationRequired(
            "KRX terms prohibit unauthorized automated collection; no authorization reference supplied"
        )
    return reference.strip()


def extract_output_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read the screen's documented grid binding key without coercing values."""
    rows = payload.get("output")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise Phase16DKrxError("response does not contain an output list of row objects")
    return rows


def _parse_screen_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) != 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def extract_returned_dates(rows: Iterable[Mapping[str, Any]]) -> list[date]:
    """Extract screen date values; period-total rows intentionally have none."""
    dates: list[date] = []
    for row in rows:
        raw = next((row[key] for key in _DATE_KEYS if key in row), None)
        parsed = _parse_screen_date(raw)
        if parsed is not None:
            dates.append(parsed)
    return dates


def verify_exact_session(
    rows: Sequence[Mapping[str, Any]],
    requested_session: date,
    *,
    explicit_documented_empty: bool = False,
) -> dict[str, Any]:
    """Validate that a date-bearing daily response contains only the request."""
    returned = extract_returned_dates(rows)
    unique = sorted(set(returned))
    if not rows and explicit_documented_empty:
        return {
            "status": "PASS_DOCUMENTED_EMPTY",
            "requested_session": requested_session.isoformat(),
            "returned_sessions": [],
            "unexpected_dates": [],
            "row_count": 0,
        }
    if rows and not returned:
        status = "UNVERIFIABLE_NO_DATE_FIELD"
    elif unique == [requested_session]:
        status = "PASS"
    else:
        status = "FAIL" if unique else "UNVERIFIABLE_NO_DATE_FIELD"
    return {
        "status": status,
        "requested_session": requested_session.isoformat(),
        "returned_sessions": [value.isoformat() for value in unique],
        "unexpected_dates": [value.isoformat() for value in unique if value != requested_session],
        "row_count": len(rows),
    }


def schema_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    """Hash only deterministic field names and value types, never field values."""
    field_types = {
        key: sorted({type(row[key]).__name__ for row in rows if key in row})
        for key in {name for row in rows for name in row}
    }
    schema = {
        "row_count": len(rows),
        "row_field_sets": sorted(
            json.dumps(sorted(row), ensure_ascii=True, separators=(",", ":")) for row in rows
        ),
        "field_types": sorted(field_types.items()),
    }
    encoded = json.dumps(schema, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def observe_field(row: Mapping[str, Any], field: str) -> dict[str, Any]:
    """Preserve absent, explicit-null, and zero as distinct observations."""
    if field not in row:
        return {"state": "MISSING_FIELD"}
    if row[field] is None:
        return {"state": "EXPLICIT_NULL"}
    return {"state": "PRESENT", "value": row[field]}


def cohort_coverage(
    expected_symbols: Sequence[str], observed_symbols: Sequence[str]
) -> dict[str, Any]:
    expected = list(expected_symbols)
    observed = list(observed_symbols)
    expected_set = set(expected)
    observed_set = set(observed)
    duplicate_expected = sorted({symbol for symbol in expected if expected.count(symbol) > 1})
    duplicate_observed = sorted({symbol for symbol in observed if observed.count(symbol) > 1})
    return {
        "target": len(expected),
        "matched": len(expected_set & observed_set),
        "missing": sorted(expected_set - observed_set),
        "unexpected": sorted(observed_set - expected_set),
        "duplicate_expected": duplicate_expected,
        "duplicate_observed": duplicate_observed,
        "ambiguous": sorted(set(duplicate_expected) | set(duplicate_observed)),
    }


def compare_repeated_payloads(
    earlier_rows: Sequence[Mapping[str, Any]], later_rows: Sequence[Mapping[str, Any]]
) -> str:
    """Classify repeatability without retaining or economically interpreting values."""
    if schema_fingerprint(earlier_rows) != schema_fingerprint(later_rows):
        return "SCHEMA_CHANGED"
    if not earlier_rows or not later_rows:
        return "UNCOMPARABLE"
    if earlier_rows == later_rows:
        return "IDENTICAL"

    def stable(rows: Sequence[Mapping[str, Any]]) -> list[str]:
        return sorted(
            json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            for row in rows
        )

    if stable(earlier_rows) == stable(later_rows):
        return "SEMANTICALLY_IDENTICAL"
    return "VALUE_REVISED"


def validate_field_map(field_map: Mapping[str, Any]) -> dict[str, Any]:
    """Validate safe field metadata and identify values quarantined by unknown units."""
    entries = field_map.get("fields")
    if not isinstance(entries, list):
        raise Phase16DKrxError("field map must contain a fields list")
    required = {
        "data_service_id",
        "provider_field",
        "official_label_ko",
        "official_label_en",
        "data_type",
        "unit",
        "semantic_status",
        "normalization_status",
        "notes",
    }
    names: list[tuple[str, str]] = []
    unknown_units: list[str] = []
    errors: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not required.issubset(entry):
            errors.append(f"field[{index}] is missing required metadata")
            continue
        name = entry["provider_field"]
        if not isinstance(name, str) or not name:
            errors.append(f"field[{index}] has no provider_field")
            continue
        names.append((entry["data_service_id"], name))
        unit = entry["unit"]
        if not isinstance(unit, str) or unit.strip().upper() in {"", "UNKNOWN", "UNKNOWN_SCALE"}:
            unknown_units.append(name)
    duplicates = sorted({key for key in names if names.count(key) > 1})
    if duplicates:
        errors.append(
            "duplicate provider fields: " + ",".join(f"{bld}:{field}" for bld, field in duplicates)
        )
    return {
        "valid": not errors,
        "field_count": len(entries),
        "unknown_unit_fields": sorted(set(unknown_units)),
        "quarantined_fields": sorted(set(unknown_units)),
        "errors": errors,
    }


def availability_decision(
    session: date,
    observed_at_kst: datetime,
    *,
    safe_time: time = time(20, 30),
) -> dict[str, Any]:
    """Apply the official page's planned 20:00 final-publication time with margin."""
    if observed_at_kst.tzinfo is None:
        raise Phase16DKrxError("observed_at_kst must be timezone-aware")
    if observed_at_kst.utcoffset() != timedelta(hours=9):
        raise Phase16DKrxError("observed_at_kst must use Korea Standard Time")
    allowed = observed_at_kst.date() > session or (
        observed_at_kst.date() == session
        and observed_at_kst.timetz().replace(tzinfo=None) >= safe_time
    )
    return {
        "allowed": allowed,
        "availability_rule": "AFTER_EXPECTED_20KST_FINAL_PUBLICATION_WINDOW",
        "session": session.isoformat(),
        "observed_at_kst": observed_at_kst.isoformat(),
        "decision": "ALLOW" if allowed else "DENY_BEFORE_FINAL_PUBLICATION_WINDOW",
    }


@dataclass
class RequestBudget:
    """Small per-run transport allowance; retries consume the same budget."""

    maximum: int = 30
    used: int = 0

    def consume(self) -> None:
        if self.maximum < 1 or self.used >= self.maximum:
            raise Phase16DKrxError("Phase16D exact-day request budget exhausted")
        self.used += 1


def source_decision(gates: Mapping[str, bool], *, availability_rule: str | None = None) -> str:
    """Keep source qualification fail-closed when any required gate is open."""
    required = {
        "official_source",
        "exact_session",
        "market_coverage",
        "field_semantics",
        "unit_semantics",
        "reproducibility",
        "publication_availability",
        "operational_feasibility",
        "provider_authorization",
    }
    if not required.issubset(gates) or availability_rule is None:
        return "PARTIAL"
    if not all(gates[key] for key in required):
        return "PARTIAL"
    if availability_rule in {"DOCUMENTED_SAME_DAY_TIME", "EMPIRICALLY_OBSERVED_SAME_DAY"}:
        return "QUALIFIED_PROSPECTIVE"
    if availability_rule == "NEXT_BUSINESS_DAY_SAFE":
        return "QUALIFIED_T_PLUS_1"
    return "PARTIAL"

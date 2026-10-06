import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from krx_trader.research import phase16d

FIXTURES = Path(__file__).parent / "fixtures" / "phase16d"
FIELD_MAP = Path(__file__).parents[1] / "docs" / "research" / "krx-per-stock-field-map.json"
ISIN = "KR0000000000"  # Shape-only synthetic KRX standard-code test value.


def _request(**overrides):
    values = {
        "session": date(2024, 6, 3),
        "market": "KOSPI",
        "short_code": "005940",
        "standard_code": ISIN,
        "request_mode": "DAILY_DETAIL",
        "session_is_verified": lambda _session: True,
        "measure": "volume",
        "side": "net",
        "detailed": True,
    }
    values.update(overrides)
    return phase16d.build_exact_day_request(**values)


def test_request_builder_uses_one_security_one_date_and_official_bld_ids():
    daily = _request()
    assert daily.service_id == "dbms/MDC/STAT/standard/MDCSTAT02303"
    assert daily.parameters["strtDd"] == daily.parameters["endDd"] == "20240603"
    assert daily.parameters["inqTpCd"] == "2"
    assert daily.parameters["trdVolVal"] == "1"
    assert daily.parameters["askBid"] == "3"
    assert daily.parameters["detailView"] == "1"
    assert "mktId" not in daily.parameters
    assert daily.market == "KOSPI"

    period = _request(request_mode="PERIOD_TOTAL", detailed=False)
    assert period.service_id == "dbms/MDC/STAT/standard/MDCSTAT02301"
    assert period.parameters["inqTpCd"] == "1"
    assert "askBid" not in period.parameters
    assert "trdVolVal" not in period.parameters


@pytest.mark.parametrize("session", [date(2026, 2, 2), date(2026, 8, 3)])
def test_protected_ranges_are_denied_before_transport(session):
    with pytest.raises(phase16d.ProtectedRangeDenied):
        _request(session=session)


def test_session_must_be_verified_before_request_is_built():
    with pytest.raises(phase16d.UnverifiedSessionDenied):
        _request(session_is_verified=lambda _session: False)


def test_request_date_rejects_datetime_values_with_an_unmodeled_time_component():
    with pytest.raises(phase16d.Phase16DKrxError, match="datetime.date"):
        _request(session=datetime(2024, 6, 3, 9, 30, tzinfo=ZoneInfo("Asia/Seoul")))


@pytest.mark.parametrize(
    ("short_code", "standard_code"),
    [("5940", ISIN), ("005940", "not-an-isin")],
)
def test_invalid_security_identifier_shapes_are_rejected(short_code, standard_code):
    with pytest.raises(phase16d.Phase16DKrxError):
        _request(short_code=short_code, standard_code=standard_code)


def test_kosdaq_requests_use_the_same_screen_with_market_as_local_metadata():
    request = _request(market="KOSDAQ", short_code="064960")
    assert request.market == "KOSDAQ"
    assert request.service_id.endswith("MDCSTAT02303")
    assert not any(key.lower().startswith("market") or key == "mktId" for key in request.parameters)


def test_synthetic_response_fixture_has_an_exact_single_date():
    fixture = json.loads((FIXTURES / "krx-mdcstat023-daily-synthetic.json").read_text())
    assert fixture["fixture_kind"].startswith("SYNTHETIC_")
    rows = phase16d.extract_output_rows(fixture["payload"])
    result = phase16d.verify_exact_session(rows, date(2024, 6, 3))
    assert result["status"] == "PASS"
    assert result["returned_sessions"] == ["2024-06-03"]
    assert result["row_count"] == 1


def test_unexpected_response_date_is_a_contract_failure():
    rows = [
        {"TRD_DD": "2024/06/03", "TRDVAL1": "SYNTHETIC_VALUE"},
        {"TRD_DD": "2024/06/04", "TRDVAL1": "SYNTHETIC_VALUE"},
    ]
    result = phase16d.verify_exact_session(rows, date(2024, 6, 3))
    assert result["status"] == "FAIL"
    assert result["unexpected_dates"] == ["2024-06-04"]


def test_period_summary_without_date_is_not_misreported_as_exact_session_pass():
    rows = [{"INVST_TP_NM": "SYNTHETIC_CATEGORY", "NETBID_TRDVOL": "SYNTHETIC_VALUE"}]
    result = phase16d.verify_exact_session(rows, date(2024, 6, 3))
    assert result["status"] == "UNVERIFIABLE_NO_DATE_FIELD"


def test_only_an_explicitly_documented_empty_result_can_pass_empty():
    assert (
        phase16d.verify_exact_session([], date(2024, 6, 3))["status"]
        == "UNVERIFIABLE_NO_DATE_FIELD"
    )
    assert (
        phase16d.verify_exact_session([], date(2024, 6, 3), explicit_documented_empty=True)[
            "status"
        ]
        == "PASS_DOCUMENTED_EMPTY"
    )


def test_field_map_validates_and_keeps_unknown_units_quarantined():
    field_map = json.loads(FIELD_MAP.read_text())
    check = phase16d.validate_field_map(field_map)
    assert check["valid"] is True
    assert check["field_count"] == len(field_map["fields"])
    assert len(check["unknown_unit_field_entries"]) == 23
    assert "NETBID_TRDVOL" in check["quarantined_fields"]
    assert "TRD_DD" not in check["quarantined_fields"]


def test_field_map_rejects_duplicate_field_within_one_data_service():
    field_map = json.loads(FIELD_MAP.read_text())
    duplicate = dict(field_map["fields"][0])
    duplicate["official_label_ko"] = "중복"
    field_map["fields"].append(duplicate)
    check = phase16d.validate_field_map(field_map)
    assert check["valid"] is False
    assert any("duplicate provider fields" in error for error in check["errors"])


def test_missing_null_and_zero_remain_distinct():
    assert phase16d.observe_field({}, "value") == {"state": "MISSING_FIELD"}
    assert phase16d.observe_field({"value": None}, "value") == {"state": "EXPLICIT_NULL"}
    assert phase16d.observe_field({"value": 0}, "value") == {"state": "PRESENT", "value": 0}


def test_frozen_cohort_coverage_reports_missing_and_duplicates_without_mapping():
    result = phase16d.cohort_coverage(
        ["005940", "064960", "082740"], ["005940", "005940", "111111"]
    )
    assert result["target"] == 3
    assert result["matched"] == 1
    assert result["missing"] == ["064960", "082740"]
    assert result["duplicate_observed"] == ["005940"]
    assert result["ambiguous"] == ["005940"]


def test_repeat_query_comparison_distinguishes_identical_revised_and_schema_changed():
    initial = [{"TRD_DD": "2024/06/03", "TRDVAL1": "A"}]
    assert phase16d.compare_repeated_payloads(initial, initial) == "IDENTICAL"
    assert (
        phase16d.compare_repeated_payloads(
            [{"TRD_DD": "2024/06/03", "TRDVAL1": "A"}, {"TRD_DD": "2024/06/03", "TRDVAL1": "B"}],
            [{"TRDVAL1": "B", "TRD_DD": "2024/06/03"}, {"TRDVAL1": "A", "TRD_DD": "2024/06/03"}],
        )
        == "SEMANTICALLY_IDENTICAL"
    )
    assert (
        phase16d.compare_repeated_payloads(initial, [{"TRD_DD": "2024/06/03", "TRDVAL1": "C"}])
        == "VALUE_REVISED"
    )
    assert (
        phase16d.compare_repeated_payloads(initial, [{"TRD_DD": "2024/06/03", "NEW_FIELD": "A"}])
        == "SCHEMA_CHANGED"
    )


def test_schema_fingerprint_tracks_shape_but_not_field_values():
    first = [{"TRD_DD": "2024/06/03", "TRDVAL1": "A"}]
    second = [{"TRD_DD": "2024/06/04", "TRDVAL1": "B"}]
    changed = [{"TRD_DD": "2024/06/04", "TRDVAL1": "B", "NEW": True}]
    assert phase16d.schema_fingerprint(first) == phase16d.schema_fingerprint(second)
    assert phase16d.schema_fingerprint(first) != phase16d.schema_fingerprint(changed)
    assert phase16d.schema_fingerprint([{"TRD_DD": "2024/06/03"}]) != phase16d.schema_fingerprint(
        [{"TRD_DD": "2024/06/03", "TRDVAL1": None}]
    )


def test_source_identity_isolated_from_other_flow_sources():
    assert phase16d.SOURCE_ID == "krx_exact_day_per_stock_investor"
    assert phase16d.SOURCE_ID != "krx_market_flow_exact_day"
    assert phase16d.SOURCE_ID != "kis_program_flow"


def test_provider_authorization_is_required_before_automated_transport():
    with pytest.raises(phase16d.AuthorizationRequired):
        phase16d.require_automation_authorization(None)
    assert (
        phase16d.require_automation_authorization("KRX written permission ref")
        == "KRX written permission ref"
    )


def test_request_budget_has_no_implicit_retry_allowance():
    budget = phase16d.RequestBudget(maximum=1)
    budget.consume()
    with pytest.raises(phase16d.Phase16DKrxError, match="budget exhausted"):
        budget.consume()


def test_availability_guard_waits_until_after_documented_final_window():
    tz = ZoneInfo("Asia/Seoul")
    session = date(2024, 6, 3)
    before = phase16d.availability_decision(session, datetime(2024, 6, 3, 20, 29, tzinfo=tz))
    after = phase16d.availability_decision(session, datetime(2024, 6, 3, 20, 30, tzinfo=tz))
    next_morning = phase16d.availability_decision(session, datetime(2024, 6, 4, 8, 30, tzinfo=tz))
    assert before["decision"] == "DENY_BEFORE_FINAL_PUBLICATION_WINDOW"
    assert after["allowed"] is True
    assert next_morning["allowed"] is True


def test_unqualified_source_never_enters_phase16_prospective_collection():
    incomplete = {
        "official_source": True,
        "exact_session": False,
        "provider_authorization": False,
    }
    assert phase16d.source_decision(incomplete) == "PARTIAL"
    gates = {
        name: True
        for name in (
            "official_source",
            "exact_session",
            "market_coverage",
            "field_semantics",
            "unit_semantics",
            "reproducibility",
            "publication_availability",
            "operational_feasibility",
            "provider_authorization",
        )
    }
    assert (
        phase16d.source_decision(gates, availability_rule="DOCUMENTED_SAME_DAY_TIME")
        == "QUALIFIED_PROSPECTIVE"
    )
    assert (
        phase16d.source_decision(gates, availability_rule="NEXT_BUSINESS_DAY_SAFE")
        == "QUALIFIED_T_PLUS_1"
    )


def test_public_metadata_and_synthetic_fixture_contain_no_auth_material():
    encoded = FIELD_MAP.read_text() + (FIXTURES / "krx-mdcstat023-daily-synthetic.json").read_text()
    lowered = encoded.lower()
    assert "cookie" not in lowered
    assert "authorization header" not in lowered
    assert "app_secret" not in lowered
    assert "password" not in lowered

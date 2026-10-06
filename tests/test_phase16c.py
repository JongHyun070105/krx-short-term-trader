from __future__ import annotations

from datetime import date, timedelta

from krx_trader.research import phase16b, phase16c

PROTECTED = [
    (date(2026, 1, 5), date(2026, 4, 16), "EXTERNAL_2026"),
    (date(2026, 7, 28), date(2026, 8, 28), "JUL_AUG_BLOCK"),
]


def _calendar(sessions: list[date], start: date, end: date) -> phase16b.KRXSessionCalendar:
    return phase16b.KRXSessionCalendar(
        sessions=tuple(sorted(set(sessions))),
        coverage_start=start,
        coverage_end=end,
        source_urls=("fixture://krx-sessions",),
        calendar_version="phase16c-test-calendar-v1",
    )


def _market_weekdays(start: date, end: date) -> list[date]:
    closures = {date(2025, 5, 1), date(2025, 5, 5), date(2025, 5, 6)}
    sessions = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in closures:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions


def test_incident_reconstruction_marks_stored_dates_and_unknown_fields():
    dates = ["2025-06-02", "2025-05-30", "2025-05-29", "2025-04-17"]
    event = {
        "event_type": "SOURCE_CONTRACT_VIOLATION",
        "source_id": "kis_per_stock_flow",
        "symbol": "005930",
        "anchor": "2025-06-02",
        "payload_persisted": False,
        "response_sha256": "a" * 64,
        "response_check": {
            "request_anchor": "2025-06-02",
            "row_count": 5,
            "returned_sessions": dates,
            "failures": ["DATE_FIELD_INVALID_OR_MISSING", "ROW_OUTSIDE_VERIFIED_RESPONSE_ENVELOPE"],
        },
    }
    result = phase16c.reconstruct_incident(
        event,
        weekday_expected_start=date(2025, 4, 21),
        krx_expected_start_for_recorded_rows=date(2025, 4, 17),
        active_contract={"contract_version": "kis-per-stock-flow-v2", "maximum_rows": 31},
        active_config_sha256="b" * 64,
        audited_collector_git_sha="c" * 40,
        market_context="KOSPI",
    )
    assert result["actual_earliest_returned_session"] == "2025-04-17"
    assert result["actual_latest_returned_session"] == "2025-06-02"
    assert result["dated_session_count_recorded"] == 4
    assert result["undated_or_unparseable_object_count"] == 1
    assert result["validation_failures"] == [
        "DATE_FIELD_INVALID_OR_MISSING",
        "ROW_OUTSIDE_VERIFIED_RESPONSE_ENVELOPE",
    ]
    assert result["request_hash"] == "NOT_RETAINED"
    assert result["incident_contract_version_recorded"] is None


def test_old_weekday_envelope_reproduces_incident_failure_class():
    anchor = date(2025, 6, 2)
    actual_sessions = _market_weekdays(date(2025, 3, 1), anchor)[-30:]
    rows = [{"stck_bsop_date": day.strftime("%Y%m%d")} for day in actual_sessions]
    rows.append({"investor_summary": "undated"})
    payload = {"output2": rows}
    calendar = phase16b.build_weekday_calendar(
        date(2025, 3, 1),
        anchor,
        official_closures=set(),
        source_urls=("fixture://weekday-only",),
        calendar_version="FIXED-HISTORICAL-PROBE-WEEKDAY-ENVELOPE-v1",
    )
    contract = phase16b.SourceResponseContract(
        source_id="kis_per_stock_flow",
        source_name="fixture",
        endpoint_id="fixture",
        request_anchor_semantics="DATE_UPPER_BOUND",
        response_direction="BACKWARD",
        maximum_rows=31,
        date_field="stck_bsop_date",
        anchor_included=True,
        session_only=True,
        ordering="DESCENDING",
        future_rows_allowed=False,
        response_window_verified=True,
        verification_method="fixture",
        verification_observation_count=59,
        earliest_contract_probe="2019-06-03",
        latest_contract_probe="2025-06-02",
        contract_version="kis-per-stock-flow-v2",
        confidence="EMPIRICALLY_VERIFIED",
        notes="The observed 31-object cap is not a provider guarantee.",
    )
    result = phase16b.verify_response_contract(
        payload,
        contract,
        request_anchor=anchor,
        calendar=calendar,
        protected_ranges=PROTECTED,
    )
    assert "DATE_FIELD_INVALID_OR_MISSING" in result["failures"]
    assert "ROW_OUTSIDE_VERIFIED_RESPONSE_ENVELOPE" in result["failures"]


def test_observation_extraction_separates_undated_summary_and_schema():
    result = phase16c.extract_observation_dates(
        {
            "output1": {"summary_code": "x"},
            "output2": [
                {"stck_bsop_date": "20250602", "buy_qty": "1"},
                {"stck_bsop_date": "2025-05-30", "buy_qty": "2"},
            ],
        },
        date_field="stck_bsop_date",
    )
    assert result["object_count"] == 3
    assert result["dated_record_count"] == 2
    assert result["undated_object_count"] == 1
    assert result["undated_by_block"] == {"output1": 1}
    assert result["sessions"] == [date(2025, 6, 2), date(2025, 5, 30)]
    assert len(result["schema_fingerprint"]) == 64


def test_ordering_change_is_reported_without_date_contract_failure():
    anchor = date(2025, 6, 2)
    sessions = [date(2025, 6, 2), date(2025, 5, 30), date(2025, 5, 29)]
    kwargs = {
        "symbol": "005930",
        "market": "KOSPI",
        "anchor": anchor,
        "date_field": "stck_bsop_date",
        "maximum_objects": 31,
        "anchor_expected": True,
        "market_sessions": sessions,
        "market_coverage": (date(2025, 5, 1), anchor),
        "protected_ranges": PROTECTED,
    }
    ascending = phase16c.analyze_probe_response(
        {"output2": [{"stck_bsop_date": day.strftime("%Y%m%d")} for day in reversed(sessions)]},
        **kwargs,
    )
    descending = phase16c.analyze_probe_response(
        {"output2": [{"stck_bsop_date": day.strftime("%Y%m%d")} for day in sessions]},
        **kwargs,
    )
    assert ascending["ordering"] == "ASCENDING"
    assert descending["ordering"] == "DESCENDING"
    assert ascending["contract_result"] == "EMPIRICALLY_MATCHED"
    assert descending["contract_result"] == "EMPIRICALLY_MATCHED"
    assert ascending["calendar_span_days"] == 4


def test_contract_drift_future_date_and_non_session_are_rejected():
    anchor = date(2025, 6, 2)
    market = _market_weekdays(date(2025, 5, 1), date(2025, 6, 6))
    base = {
        "symbol": "005930",
        "market": "KOSPI",
        "anchor": anchor,
        "date_field": "stck_bsop_date",
        "maximum_objects": 31,
        "anchor_expected": True,
        "market_sessions": market,
        "market_coverage": (date(2025, 5, 1), date(2025, 6, 6)),
        "protected_ranges": PROTECTED,
    }
    future = phase16c.analyze_probe_response(
        {"output2": [{"stck_bsop_date": "20250603"}, {"stck_bsop_date": "20250602"}]},
        **base,
    )
    over_limit = phase16c.analyze_probe_response(
        {"output2": [{"stck_bsop_date": "20250602"}] * 32}, **base
    )
    non_session = phase16c.analyze_probe_response(
        {"output2": [{"stck_bsop_date": "20250602"}, {"stck_bsop_date": "20250531"}]},
        **base,
    )
    assert "DATE_NEWER_THAN_ANCHOR" in future["failures"]
    assert "ROW_COUNT_DRIFT" in over_limit["failures"]
    assert "NON_KRX_SESSION_DATE" in non_session["failures"]


def test_protected_response_and_duplicate_dates_are_rejected():
    anchor = date(2026, 4, 20)
    market = _market_weekdays(date(2026, 3, 1), date(2026, 4, 20))
    result = phase16c.analyze_probe_response(
        {
            "output2": [
                {"stck_bsop_date": "20260420"},
                {"stck_bsop_date": "20260416"},
                {"stck_bsop_date": "20260416"},
            ]
        },
        symbol="005930",
        market="KOSPI",
        anchor=anchor,
        date_field="stck_bsop_date",
        maximum_objects=31,
        anchor_expected=True,
        market_sessions=market,
        market_coverage=(date(2026, 3, 1), anchor),
        protected_ranges=PROTECTED,
    )
    assert "PROTECTED_RANGE_BREACH" in result["failures"]
    assert "DUPLICATE_SESSION" in result["failures"]


def test_sparse_symbol_window_exposes_long_and_extreme_gap_spans():
    anchor = date(2025, 6, 2)
    sparse = [anchor - timedelta(days=90 * index) for index in range(31)]
    window = phase16c.observation_window(sparse, anchor=anchor, count=31)
    assert window["observation_count"] == 31
    assert window["calendar_span_days"] == 90 * 30
    assert window["earliest_session"] == (anchor - timedelta(days=2700)).isoformat()
    # A finite symbol-row count does not create a finite KRX-session envelope.
    result = phase16c.finite_pre_network_bound(
        maximum_observations=31,
        maximum_is_provider_documented=True,
        maximum_missing_sessions_between_observations=None,
    )
    assert result["proven"] is False


def test_local_price_observation_candidate_is_only_comparable_when_covered():
    anchor = date(2025, 6, 2)
    sessions = _market_weekdays(date(2025, 4, 1), anchor)
    market_window = sessions[-30:]
    exact_price_window = market_window
    payload = {"output2": [{"stck_bsop_date": day.strftime("%Y%m%d")} for day in reversed(market_window)]}
    metrics = phase16c.analyze_probe_response(
        payload,
        symbol="001440",
        market="KOSPI",
        anchor=anchor,
        date_field="stck_bsop_date",
        maximum_objects=31,
        anchor_expected=True,
        market_sessions=sessions,
        market_coverage=(sessions[0], sessions[-1]),
        symbol_price_sessions=exact_price_window,
        protected_ranges=PROTECTED,
    )
    assert metrics["krx_sessions_spanned"] == 30
    assert metrics["missing_krx_sessions_inside_return_span"] == []
    assert metrics["last_n_krx_sessions_match"] is True
    assert metrics["last_n_symbol_price_rows_match"] is True
    comparison = phase16c.compare_candidate_models([metrics])
    model_a = next(item for item in comparison["models"] if item["model"].startswith("MODEL_A"))
    model_b = next(item for item in comparison["models"] if item["model"].startswith("MODEL_B"))
    assert model_a["explained"] == model_b["explained"] == 1

    uncovered = phase16c.analyze_probe_response(
        payload,
        symbol="001440",
        market="KOSPI",
        anchor=anchor,
        date_field="stck_bsop_date",
        maximum_objects=31,
        anchor_expected=True,
        market_sessions=None,
        market_coverage=None,
        symbol_price_sessions=None,
    )
    assert uncovered["last_n_krx_sessions_match"] is None
    assert uncovered["last_n_symbol_price_rows_match"] is None


def test_finite_bound_needs_documented_row_cap_and_gap_bound():
    no_documented_cap = phase16c.finite_pre_network_bound(
        maximum_observations=31,
        maximum_is_provider_documented=False,
        maximum_missing_sessions_between_observations=0,
    )
    no_gap_bound = phase16c.finite_pre_network_bound(
        maximum_observations=31,
        maximum_is_provider_documented=True,
        maximum_missing_sessions_between_observations=None,
    )
    proven = phase16c.finite_pre_network_bound(
        maximum_observations=31,
        maximum_is_provider_documented=True,
        maximum_missing_sessions_between_observations=2,
    )
    assert no_documented_cap["proven"] is False
    assert no_gap_bound["proven"] is False
    assert proven["proven"] is True
    assert proven["bound_sessions"] == 91


def test_protected_range_and_prospective_guards_fail_closed():
    blocked = phase16c.preflight_old_probe_anchor(
        date(2026, 2, 2),
        protected_ranges=PROTECTED,
        upper_anchor_semantics_previously_observed=True,
    )
    allowed_old = phase16c.preflight_old_probe_anchor(
        date(2025, 6, 2),
        protected_ranges=PROTECTED,
        upper_anchor_semantics_previously_observed=True,
    )
    prospective = phase16c.preflight_prospective_request(
        date(2026, 10, 16), protected_ranges=PROTECTED, finite_bound={"proven": False}
    )
    assert blocked["allowed"] is False
    assert allowed_old["allowed"] is True
    assert prospective["allowed"] is False
    assert prospective["request_decision"] == "DEFERRED_RESPONSE_CONTRACT"
    safe_sessions = _market_weekdays(date(2025, 1, 1), date(2025, 6, 2))
    bounded = phase16c.preflight_prospective_request(
        date(2025, 6, 2),
        protected_ranges=PROTECTED,
        finite_bound={"proven": True, "bound_sessions": 31},
        market_sessions=safe_sessions,
        market_coverage=(safe_sessions[0], safe_sessions[-1]),
    )
    assert bounded["allowed"] is True
    assert bounded["earliest_possible_date"] < bounded["latest_possible_date"]


def test_contract_hold_clearance_and_first_safe_anchor_require_evidence():
    required = {
        "root_cause_identified": True,
        "all_probes_explained": True,
        "no_contradictory_probes": True,
        "finite_pre_network_bound_proven": True,
        "protected_guard_verified": True,
        "original_failure_regression_covered": True,
        "documentation_updated": True,
        "contract_version_updated": True,
    }
    assert phase16c.can_clear_contract_hold(required) is True
    assert phase16c.can_clear_contract_hold({**required, "finite_pre_network_bound_proven": False}) is False
    unknown = phase16c.first_safe_anchor_or_unknown(
        finite_bound={"proven": False}, compute_anchor=lambda: date(2026, 10, 16)
    )
    calculated = phase16c.first_safe_anchor_or_unknown(
        finite_bound={"proven": True}, compute_anchor=lambda: date(2026, 10, 20)
    )
    assert unknown is None
    assert calculated == date(2026, 10, 20)


def test_contract_version_changes_when_response_contract_changes():
    contract = phase16b.SourceResponseContract(
        source_id="kis_per_stock_flow",
        source_name="fixture",
        endpoint_id="fixture",
        request_anchor_semantics="DATE_UPPER_BOUND",
        response_direction="BACKWARD",
        maximum_rows=31,
        date_field="stck_bsop_date",
        anchor_included=True,
        session_only=True,
        ordering="DESCENDING",
        future_rows_allowed=False,
        response_window_verified=True,
        verification_method="fixture",
        verification_observation_count=10,
        earliest_contract_probe="2025-01-01",
        latest_contract_probe="2025-02-01",
        contract_version="kis-per-stock-flow-v2",
        confidence="EMPIRICALLY_VERIFIED",
        notes="fixture only",
    )
    revised = phase16b.versioned_contract_update(
        contract,
        new_version="kis-per-stock-flow-v3",
        changes={"maximum_rows": 30, "confidence": "PARTIAL"},
    )
    assert revised.contract_version == "kis-per-stock-flow-v3"
    assert revised.maximum_rows == 30

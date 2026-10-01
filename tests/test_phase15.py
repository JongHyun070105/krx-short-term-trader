from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from krx_trader.kis.rest import (
    INVESTOR_FLOW_DAILY_PATH,
    KisApiError,
    KisRestClient,
)
from krx_trader.kis.transport import HttpResponse
from krx_trader.research.phase15 import (
    SCORECARD_FIELDS,
    Phase15DateError,
    SymbolLineage,
    append_cache_record,
    assert_phase15_source_date,
    build_artifact_index,
    canonical_response_hash,
    classify_availability,
    classify_delisted_support,
    classify_fresh_period,
    classify_historical_depth,
    classify_reproducibility,
    classify_response_hashes,
    compare_artifact_snapshot,
    derive_qualification,
    estimate_acquisition,
    proposed_factor_registry,
    read_cached_request,
    request_manifest_record,
    validate_date_range,
    validate_pilot_rows,
    validate_scorecard,
    verify_artifact_index,
    write_json,
)


def _scorecard(**updates):
    record = {
        "source_name": "sample",
        "provider": "official",
        "official_or_third_party": "OFFICIAL",
        "access_method": "REST",
        "documentation_url": "https://example.test/docs",
        "data_type": "daily flow",
        "earliest_date_tested": "2019-06-03",
        "latest_date_tested": "2025-06-02",
        "date_range_support": "one date per request",
        "request_granularity": "symbol/session",
        "pagination": "continuation header",
        "rate_limit": "4 seconds empirically shared",
        "auth_required": True,
        "point_in_time_safety": "UNKNOWN",
        "publication_timestamp": None,
        "revision_semantics": "unknown",
        "delisted_support": "unknown",
        "symbol_lineage_support": "not provided",
        "reproducibility": "IDENTICAL",
        "missing_data_behavior": "empty response remains missing",
        "license_or_usage_limitation": "personal use",
        "estimated_full_acquisition_requests": 170_000,
        "estimated_full_acquisition_time": "8 days continuous",
        "qualification": "PARTIAL",
    }
    record.update(updates)
    return record


def _response(payload, *, tr_cont=""):
    headers = {"tr_cont": tr_cont} if tr_cont else {}
    return HttpResponse(200, json.dumps(payload).encode("utf-8"), headers)


class _TokenStub:
    def get_token(self):
        return "transient-token"

    def invalidate(self, _token=None):
        return None


class _TransportStub:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _client(tmp_path, responses, transport=None):
    stub = transport or _TransportStub(responses)
    client = KisRestClient(
        "test-app-key",
        "test-app-secret",
        _TokenStub(),
        stub,
        sleeper=lambda _seconds: None,
        min_request_interval=0,
        rate_limit_path=tmp_path / "limiter.json",
        wall_clock=lambda: 1_000.0,
    )
    return client, stub


def test_source_scorecard_requires_complete_schema_and_allowed_values():
    result = validate_scorecard(_scorecard())
    assert tuple(result) == SCORECARD_FIELDS


@pytest.mark.parametrize(
    "updates",
    [
        {"qualification": "PROMISING"},
        {"point_in_time_safety": "SAME_DAY_UNKNOWN"},
        {"reproducibility": "MAYBE"},
        {"earliest_date_tested": "2019-02-30"},
        {"estimated_full_acquisition_requests": -1},
        {"documentation_url": "http://example.test"},
        {"unexpected": "field"},
    ],
)
def test_source_scorecard_rejects_invalid_values(updates):
    with pytest.raises(ValueError):
        validate_scorecard(_scorecard(**updates))


def test_source_scorecard_requires_every_named_field():
    record = _scorecard()
    del record["publication_timestamp"]
    with pytest.raises(ValueError, match="missing"):
        validate_scorecard(record)


def test_qualification_is_fail_closed_and_prospective_is_distinct():
    inputs = {
        "available": True,
        "historical": True,
        "reproducible": True,
        "semantics_documented": True,
        "sufficient_depth": True,
        "timestamp_safe": True,
        "missing_behavior_known": True,
        "acquisition_practical": True,
        "cacheable": True,
        "use_permitted": True,
    }
    assert derive_qualification(**inputs) == "QUALIFIED"
    assert derive_qualification(**(inputs | {"timestamp_safe": False})) == "PARTIAL"
    assert derive_qualification(**(inputs | {"reproducible": False})) == "REJECTED"
    assert derive_qualification(**(inputs | {"use_permitted": False})) == "REJECTED"
    assert derive_qualification(**(inputs | {"available": False})) == "NOT_AVAILABLE"
    assert derive_qualification(**(inputs | {"historical": False})) == "PROSPECTIVE_ONLY"


def test_publication_time_policy_is_conservative():
    assert classify_availability(known_at="2024-01-02T15:32:00+09:00", session_date=date(2024, 1, 2)) == "SAFE_INTRADAY"
    assert classify_availability(known_at=None, session_date=date(2024, 1, 2)) == "NEXT_SESSION_ONLY"
    assert classify_availability(known_at="2024-01-02T15:32:00", session_date=date(2024, 1, 2)) == "UNKNOWN"
    assert classify_availability(known_at=None, session_date=None) == "UNKNOWN"


def test_reproducibility_ignores_row_and_mapping_order_but_not_content():
    first = {"output": [{"symbol": "000660", "flow": "3"}, {"symbol": "005930", "flow": "-2"}]}
    reordered = {"output": [{"flow": "-2", "symbol": "005930"}, {"symbol": "000660", "flow": "3"}]}
    revised = {"output": [{"symbol": "000660", "flow": "4"}, {"symbol": "005930", "flow": "-2"}]}
    assert classify_reproducibility(first, first) == "IDENTICAL"
    assert classify_reproducibility(first, reordered) == "SEMANTICALLY_IDENTICAL"
    assert classify_reproducibility(first, revised) == "REVISED"
    assert classify_reproducibility(first, None) == "UNAVAILABLE"


def test_canonical_hash_preserves_order_for_non_row_lists():
    assert canonical_response_hash({"rows": [{"b": 2, "a": 1}]}) == canonical_response_hash(
        {"rows": [{"a": 1, "b": 2}]}
    )
    assert canonical_response_hash({"values": [1, 2]}) != canonical_response_hash({"values": [2, 1]})


def test_repeat_hash_classification_distinguishes_semantic_equality():
    assert classify_response_hashes("raw1", "canonical", "raw1", "canonical") == "IDENTICAL"
    assert classify_response_hashes("raw1", "canonical", "raw2", "canonical") == "SEMANTICALLY_IDENTICAL"
    assert classify_response_hashes("raw1", "a", "raw2", "b") == "REVISED"
    assert classify_response_hashes(None, "a", "raw2", "b") == "UNAVAILABLE"


@pytest.mark.parametrize("blocked", ["2026-01-05", "2026-04-16", "2026-07-28", "2026-08-28"])
def test_phase15_source_date_guard_rejects_protected_period_edges(blocked):
    with pytest.raises(Phase15DateError):
        assert_phase15_source_date(blocked)


def test_phase15_source_date_guard_allows_older_history_and_prospective_dates():
    assert_phase15_source_date("2019-06-03")
    assert_phase15_source_date("2026-06-30")


def test_request_manifest_is_stable_redacted_and_date_scoped():
    record = request_manifest_record(
        source="KIS investor flow",
        endpoint="/flow",
        parameters={"date": "2019-06-03", "symbol": "005930"},
        symbol="005930",
        requested_date=date(2019, 6, 3),
    )
    assert record["request_sha256"] == canonical_response_hash(record["parameters"])
    assert "appsecret" not in json.dumps(record).lower()
    with pytest.raises(ValueError, match="credentials"):
        request_manifest_record(
            source="KIS",
            endpoint="/flow",
            parameters={"appsecret": "must-not-appear"},
            symbol="005930",
            requested_date=date(2019, 6, 3),
        )


def test_pilot_data_quality_checks_dates_duplicates_and_numeric_fields():
    report = validate_pilot_rows(
        [
            {"stck_bsop_date": "20190603", "frgn_ntby_qty": "-10"},
            {"stck_bsop_date": "20190603", "frgn_ntby_qty": "-10"},
        ],
        requested_date=date(2019, 6, 3),
        date_field="stck_bsop_date",
    )
    assert report["row_count"] == 2
    assert report["duplicate_rows"] == 1
    assert report["returned_dates"] == ["2019-06-03"]
    assert report["requested_date_matches"] is True
    assert report["invalid_numeric_fields"] == []
    assert report["negative_impossible_fields"] == []
    assert report["missing_is_preserved"] is True


def test_pilot_data_quality_keeps_missing_and_unreported_dates_unknown():
    report = validate_pilot_rows([{"frgn_ntby_qty": None}], requested_date=date(2020, 1, 2), date_field="date")
    assert report["requested_date_matches"] is None
    assert report["missing_is_preserved"] is True
    assert report["returned_dates"] == []


def test_pilot_data_quality_accepts_a_bounded_window_ending_on_requested_date():
    report = validate_pilot_rows(
        [
            {"stck_bsop_date": "20200102", "frgn_ntby_qty": "-4"},
            {"stck_bsop_date": "20200103", "frgn_ntby_qty": "5"},
        ],
        requested_date=date(2020, 1, 3),
        date_field="stck_bsop_date",
        allow_prior_sessions=True,
    )
    assert report["requested_date_matches"] is True
    assert report["returned_dates"] == ["2020-01-02", "2020-01-03"]


def test_pilot_data_quality_rejects_nonfinite_and_impossible_negative_values():
    report = validate_pilot_rows(
        [{"stck_clpr": "-1", "acml_vol": "NaN", "frgn_ntby_qty": "-5"}],
        requested_date=date(2020, 1, 2),
        date_field="date",
    )
    assert report["invalid_numeric_fields"] == ["acml_vol"]
    assert report["negative_impossible_fields"] == ["stck_clpr"]


def test_phase15_date_range_guard_rejects_crossing_protected_periods():
    assert validate_date_range(date(2019, 1, 1), date(2020, 1, 1)) == (
        date(2019, 1, 1),
        date(2020, 1, 1),
    )
    with pytest.raises(Phase15DateError, match="intersects"):
        validate_date_range(date(2025, 12, 31), date(2026, 6, 1))
    with pytest.raises(ValueError, match="start"):
        validate_date_range(date(2021, 1, 1), date(2020, 1, 1))


@pytest.mark.parametrize(
    ("tested", "required", "expected"),
    [([], date(2019, 1, 1), "NOT_AVAILABLE"), ([date(2018, 6, 1)], date(2019, 1, 1), "AVAILABLE"), ([date(2020, 1, 1)], date(2019, 1, 1), "PARTIAL")],
)
def test_historical_depth_classification(tested, required, expected):
    assert classify_historical_depth(tested, required_start=required) == expected


@pytest.mark.parametrize(
    ("metadata", "flow", "price", "expected"),
    [(False, False, False, "NOT_AVAILABLE"), (True, False, False, "PARTIAL"), (True, True, True, "AVAILABLE")],
)
def test_delisted_support_classification(metadata, flow, price, expected):
    assert classify_delisted_support(
        metadata_found=metadata, flow_returned=flow, price_returned=price
    ) == expected


def test_acquisition_estimator_calculates_requests_retries_and_wall_clock():
    estimate = estimate_acquisition(symbols=100, sessions=1_708, retry_rate=0.01, interval_seconds=4)
    assert estimate["base_requests"] == 170_800
    assert estimate["expected_retries"] == 1_708
    assert estimate["total_requests"] == 172_508
    assert estimate["estimated_hours"] == pytest.approx(191.68, abs=0.02)
    assert estimate["feasibility"] == "IMPRACTICAL"
    assert estimate["estimated_artifact_bytes"] == 341_600_000


def test_acquisition_estimator_rejects_negative_assumptions():
    with pytest.raises(ValueError):
        estimate_acquisition(symbols=-1, sessions=1)


def test_kis_flow_adapter_paginates_and_preserves_category_rows(tmp_path):
    client, transport = _client(
        tmp_path,
        [
            _response({"rt_cd": "0", "output1": [{"investor": "foreign"}], "output2": [{"date": "20190603"}]}, tr_cont="M"),
            _response({"rt_cd": "0", "output1": [{"investor": "institution"}], "output2": [{"date": "20190603"}]}, tr_cont="N"),
        ],
    )
    result = client.get_investor_flow_by_date("005930", date(2019, 6, 3))
    assert result["output1"] == [{"investor": "foreign"}, {"investor": "institution"}]
    assert result["output2"] == [{"date": "20190603"}, {"date": "20190603"}]
    assert len(transport.calls) == 2
    assert transport.calls[0][1].endswith(INVESTOR_FLOW_DAILY_PATH)
    assert transport.calls[0][2]["params"]["FID_INPUT_ISCD"] == "005930"
    assert transport.calls[0][2]["params"]["FID_INPUT_DATE_1"] == "20190603"
    assert transport.calls[1][2]["headers"]["tr_cont"] == "N"


def test_kis_flow_adapter_fails_closed_when_page_limit_is_reached(tmp_path):
    client, _ = _client(tmp_path, [_response({"rt_cd": "0", "output": []}, tr_cont="M")])
    with pytest.raises(KisApiError, match="pagination"):
        client.get_investor_flow_by_date("005930", date(2019, 6, 3), max_pages=1)


def test_kis_flow_adapter_rejects_bad_symbol(tmp_path):
    client, _ = _client(tmp_path, [])
    with pytest.raises(ValueError, match="six-digit"):
        client.get_investor_flow_by_date("bad", date(2019, 6, 3))


def test_pilot_cache_is_source_isolated_append_safe_and_resumable(tmp_path):
    flow_path = tmp_path / "phase15" / "kis-flow.jsonl"
    program_path = tmp_path / "phase15" / "kis-program.jsonl"
    first = {"request_sha256": "req-a", "canonical_response_sha256": "resp-a", "query_round": 1}
    changed = first | {"canonical_response_sha256": "resp-b"}
    assert append_cache_record(flow_path, first) is True
    assert append_cache_record(flow_path, first) is False
    assert append_cache_record(flow_path, changed) is True
    updated_policy = changed | {"dq_policy_version": 2}
    assert append_cache_record(flow_path, updated_policy) is True
    assert append_cache_record(program_path, first) is True
    assert read_cached_request(flow_path, "req-a", 1) == updated_policy
    assert read_cached_request(flow_path, "missing", 1) is None
    assert len(flow_path.read_text().splitlines()) == 3
    assert len(program_path.read_text().splitlines()) == 1
    assert "resp-a" in flow_path.read_text()


def test_delisted_symbol_lineage_representation_keeps_identity_and_dates():
    lineage = SymbolLineage(
        symbol="181340",
        valid_from="2017-07-26",
        valid_to="2024-07-10",
        event="delisting",
        successor_symbol=None,
        source_url="https://kind.krx.co.kr/example",
    )
    assert lineage.symbol == "181340"
    assert lineage.successor_symbol is None
    assert lineage.valid_to == "2024-07-10"


def test_fresh_period_detector_separates_outcome_use_from_price_only_warmup():
    reused_outcomes = [{"start": "2023-01-02", "end": "2025-12-30"}]
    touched_price_only = [{"start": "2022-11-01", "end": "2022-12-30"}]
    fresh = classify_fresh_period(
        date(2019, 1, 2),
        date(2020, 12, 30),
        used_outcome_periods=reused_outcomes,
        prior_source_used=False,
        price_only_touched_periods=touched_price_only,
    )
    assert fresh["classification"] == "FRESH"
    confirmation = classify_fresh_period(
        date(2022, 1, 3),
        date(2022, 12, 29),
        used_outcome_periods=reused_outcomes,
        prior_source_used=False,
        price_only_touched_periods=touched_price_only,
    )
    assert confirmation["classification"] == "FRESH_WITH_PRICE_CAVEAT"
    contaminated = classify_fresh_period(
        date(2024, 1, 1),
        date(2024, 12, 31),
        used_outcome_periods=reused_outcomes,
        prior_source_used=False,
    )
    assert contaminated["classification"] == "CONTAMINATED"


def test_fresh_period_detector_rejects_reversed_ranges():
    with pytest.raises(ValueError):
        classify_fresh_period(date(2021, 1, 1), date(2020, 1, 1), used_outcome_periods=[], prior_source_used=False)


def test_proposed_registry_is_inactive_and_requires_availability_time():
    proposal = proposed_factor_registry()
    assert proposal["status"] == "PROPOSED"
    assert proposal["active_factor_registry_modified"] is False
    assert proposal["availability_time_required"] is True
    assert all(item["predictive_result"] is None for item in proposal["entries"])
    assert all("availability_time" in item for item in proposal["entries"])
    assert all(item["status"] == "DEFERRED" for item in proposal["entries"])
    assert all(item["label"] == "POTENTIAL_FEATURE_ONLY" for item in proposal["entries"])


def test_phase_artifact_index_hashes_and_verifies_files(tmp_path):
    write_json(tmp_path / "one.json", {"x": 1})
    index = build_artifact_index(tmp_path)
    assert index["artifact_count"] == 1
    assert verify_artifact_index(tmp_path, index)["status"] == "PASS"
    (tmp_path / "one.json").write_text("changed")
    result = verify_artifact_index(tmp_path, index)
    assert result["status"] == "FAIL"
    assert result["failed"] == ["one.json"]


def test_previous_phase_snapshot_compares_without_rewriting(tmp_path):
    path = tmp_path / "runtime/research/phase14/summary.json"
    path.parent.mkdir(parents=True)
    path.write_text("preserved\n")
    snapshot = {
        "files": [
            {
                "path": "runtime/research/phase14/summary.json",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ]
    }
    result = compare_artifact_snapshot(snapshot, tmp_path)
    assert result["status"] == "PASS"
    path.write_text("modified\n")
    assert compare_artifact_snapshot(snapshot, tmp_path)["status"] == "FAIL"

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from krx_trader.research import phase16b

KST = phase16b.KST
PROTECTED = [
    (date(2026, 1, 5), date(2026, 4, 16), "EXTERNAL_2026"),
    (date(2026, 7, 28), date(2026, 8, 28), "HOLDOUT_2026"),
]


def _calendar() -> phase16b.KRXSessionCalendar:
    return phase16b.build_weekday_calendar(
        date(2026, 8, 31),
        date(2026, 12, 30),
        official_closures={
            date(2026, 9, 24),
            date(2026, 9, 25),
            date(2026, 10, 5),
            date(2026, 10, 9),
            date(2026, 12, 25),
        },
        source_urls=("https://www.krx.co.kr/",),
        calendar_version="fixture-v1",
    )


def _contract(
    *,
    source_id: str = "kis_per_stock_flow",
    direction: str = "BACKWARD",
    maximum_rows: int = 31,
    confidence: str = "EMPIRICALLY_VERIFIED",
    window_verified: bool = True,
    date_field: str = "stck_bsop_date",
) -> phase16b.SourceResponseContract:
    return phase16b.SourceResponseContract(
        source_id=source_id,
        source_name="fixture flow",
        endpoint_id="fixture-endpoint-v1",
        request_anchor_semantics="DATE_UPPER_BOUND" if direction == "BACKWARD" else "EXACT_DATE",
        response_direction=direction,
        maximum_rows=maximum_rows,
        date_field=date_field,
        anchor_included=True,
        session_only=True,
        ordering="ASCENDING",
        future_rows_allowed=False,
        response_window_verified=window_verified,
        verification_method="fixture probe",
        verification_observation_count=3,
        earliest_contract_probe="2025-01-02",
        latest_contract_probe="2025-02-03",
        contract_version="fixture-v1",
        confidence=confidence,
        notes="test contract",
    )


def test_contract_schema_round_trip_and_supported_confidence():
    contract = _contract()
    assert phase16b.SourceResponseContract.from_dict(contract.as_dict()) == contract
    for confidence in ("DOCUMENTED", "EMPIRICALLY_VERIFIED", "PARTIAL", "UNKNOWN"):
        assert phase16b.SourceResponseContract.from_dict({**contract.as_dict(), "confidence": confidence})
    with pytest.raises(ValueError, match="confidence"):
        phase16b.SourceResponseContract.from_dict({**contract.as_dict(), "confidence": "GUESSED"})


def test_contract_behavior_change_requires_version_increment():
    contract = _contract()
    with pytest.raises(ValueError, match="new contract_version"):
        phase16b.versioned_contract_update(contract, new_version="fixture-v1", changes={"maximum_rows": 32})
    revised = phase16b.versioned_contract_update(
        contract, new_version="fixture-v2", changes={"maximum_rows": 32}
    )
    assert revised.contract_version == "fixture-v2"
    assert revised.maximum_rows == 32


def test_session_envelope_counts_trading_sessions_not_calendar_days():
    calendar = _calendar()
    contract = _contract()
    envelope = phase16b.possible_response_session_range(
        contract, date(2026, 10, 16), calendar=calendar
    )
    assert envelope["earliest_possible_session"] == "2026-08-31"
    assert envelope["latest_possible_session"] == "2026-10-16"
    assert envelope["maximum_session_count"] == 31


def test_session_envelope_accounts_for_excluded_anchor_rows():
    contract = phase16b.versioned_contract_update(
        _contract(maximum_rows=3),
        new_version="fixture-v2",
        changes={"anchor_included": False},
    )
    envelope = phase16b.possible_response_session_range(
        contract, date(2026, 10, 16), calendar=_calendar()
    )
    assert envelope["earliest_possible_session"] == "2026-10-13"
    assert envelope["maximum_session_count"] == 3


def test_first_safe_anchor_calculates_per_stock_and_program_offsets():
    calendar = _calendar()
    stock = phase16b.first_safe_anchor_session(
        _contract(), calendar=calendar, protected_ranges=PROTECTED
    )
    program = phase16b.first_safe_anchor_session(
        _contract(maximum_rows=30), calendar=calendar, protected_ranges=PROTECTED
    )
    exact = phase16b.first_safe_anchor_session(
        _contract(source_id="krx", direction="EXACT", maximum_rows=1),
        calendar=calendar,
        protected_ranges=PROTECTED,
    )
    assert stock == date(2026, 10, 16)
    assert program == date(2026, 10, 15)
    assert exact == date(2026, 8, 31)


@pytest.mark.parametrize("session,label", [(date(2026, 2, 2), "EXTERNAL_2026"), (date(2026, 8, 3), "HOLDOUT_2026")])
def test_exact_day_request_denies_each_protected_period_before_transport(session, label):
    calendar = phase16b.build_weekday_calendar(
        date(2026, 1, 1), date(2026, 12, 31), official_closures=set(),
        source_urls=("fixture",), calendar_version="fixture-all-weekdays",
    )
    contract = _contract(source_id="krx", direction="EXACT", maximum_rows=1)
    calls = []
    with pytest.raises(phase16b.ContractError, match="DENY_PROTECTED_RANGE"):
        phase16b.execute_contract_guarded_request(
            contract,
            session,
            calendar=calendar,
            protected_ranges=PROTECTED,
            transport=lambda: calls.append("called"),
        )
    assert calls == []
    assert label in {row[2] for row in PROTECTED}


def test_non_session_anchor_is_denied_without_previous_session_fallback():
    decision = phase16b.request_contract_guard(
        _contract(),
        date(2026, 10, 3),
        calendar=_calendar(),
        protected_ranges=PROTECTED,
    )
    assert decision["allowed"] is False
    assert decision["request_decision"] == "DENY_UNVERIFIED_SESSION"


def test_incomplete_contract_is_deferred_and_transport_is_never_called():
    contract = _contract(confidence="PARTIAL", window_verified=False)
    called = []
    with pytest.raises(phase16b.ContractError, match="DEFERRED_RESPONSE_CONTRACT"):
        phase16b.execute_contract_guarded_request(
            contract,
            date(2026, 10, 16),
            calendar=_calendar(),
            protected_ranges=PROTECTED,
            transport=lambda: called.append(True),
        )
    assert called == []


def test_activation_state_waits_then_transitions_to_active_deterministically():
    contract = _contract()
    safe = date(2026, 10, 16)
    waiting = phase16b.activation_state(
        contract,
        first_safe_anchor=safe,
        latest_completed_session=date(2026, 10, 15),
        credentials_available=True,
        collector_healthy=True,
        auto_activation=True,
    )
    active = phase16b.activation_state(
        contract,
        first_safe_anchor=safe,
        latest_completed_session=safe,
        credentials_available=True,
        collector_healthy=True,
        auto_activation=True,
    )
    assert waiting["state"] == "WAITING_FOR_SAFE_DATE"
    assert active["state"] == "ACTIVE"


def test_frozen_probe_cohort_hash_is_enforced_before_scheduled_collection():
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    cohort = phase16b.frozen_probe_cohort(config)
    assert len(cohort) == 12
    assert sum(item["market"] == "KOSPI" for item in cohort) == 6
    assert sum(item["market"] == "KOSDAQ" for item in cohort) == 6
    changed = json.loads(json.dumps(config))
    changed["phase16b"]["observation_policy"]["fixed_flow_probe_cohort"]["symbols_by_market"]["KOSPI"][0] = "999999"
    with pytest.raises(phase16b.ContractError, match="cohort hash mismatch"):
        phase16b.frozen_probe_cohort(changed)


def test_no_completed_market_session_is_reported_before_first_calendar_session():
    calendar = _calendar()
    result = phase16b.latest_completed_session(
        calendar, datetime(2026, 8, 30, 23, 0, tzinfo=KST)
    )
    assert result is None
    assert phase16b.latest_completed_session(
        calendar, datetime(2027, 1, 4, 20, 0, tzinfo=KST)
    ) is None


def test_scheduled_weekend_slot_returns_without_provider_transport(tmp_path, monkeypatch):
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    monkeypatch.setattr(phase16b, "DEFAULT_PHASE16B_ROOT", tmp_path / "phase16b")
    result = phase16b.collect_active_flow_slot(
        config=config,
        config_sha256=hashlib.sha256(phase16b.phase16._json_bytes(config)).hexdigest(),
        root=tmp_path / "phase16",
        slot="full-evening",
        now=datetime(2026, 10, 3, 20, 30, tzinfo=KST),
        krx_fetch=lambda *_: pytest.fail("non-session must not reach provider transport"),
    )
    assert result["status"] == "NO_COMPLETED_MARKET_SESSION"
    assert result["network_accessed"] is False


def test_per_stock_response_parser_checks_anchor_window_dates_and_order():
    contract = _contract()
    calendar = phase16b.build_weekday_calendar(
        date(2024, 11, 1), date(2026, 12, 31), official_closures=set(),
        source_urls=("fixture",), calendar_version="weekday fixture",
    )
    anchor = date(2025, 1, 31)
    payload = {"output1": [
        {"stck_bsop_date": "20250130", "orgn_ntby_qty": "4"},
        {"stck_bsop_date": "20250131", "orgn_ntby_qty": "5"},
    ]}
    result = phase16b.verify_response_contract(payload, contract, request_anchor=anchor, calendar=calendar, protected_ranges=PROTECTED)
    assert result["valid"] is True
    assert result["row_count"] == 2
    assert result["returned_sessions"][-1] == "2025-01-31"


def test_live_contract_ordering_is_descending_for_each_kis_flow_source():
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    contracts = phase16b.contracts_from_config(config)
    calendar = phase16b._calendar_from_config(config)
    anchor = date(2026, 10, 16)
    for source_id in ("kis_per_stock_flow", "kis_program_flow"):
        contract = contracts[source_id]
        result = phase16b.verify_response_contract(
            {"output": [
                {"stck_bsop_date": "20261016", "provider_field": "newest"},
                {"stck_bsop_date": "20261015", "provider_field": "older"},
            ]},
            contract,
            request_anchor=anchor,
            calendar=calendar,
            protected_ranges=PROTECTED,
        )
        assert contract.ordering == "DESCENDING"
        assert result["valid"] is True


def test_program_parser_rejects_future_rows_and_rows_over_contract_cap():
    calendar = phase16b.build_weekday_calendar(
        date(2025, 1, 1), date(2026, 12, 31), official_closures=set(),
        source_urls=("fixture",), calendar_version="weekday fixture",
    )
    contract = _contract(maximum_rows=1)
    result = phase16b.verify_response_contract(
        {"output": [
            {"stck_bsop_date": "20250131", "v": "1"},
            {"stck_bsop_date": "20250203", "v": "2"},
        ]},
        contract,
        request_anchor=date(2025, 1, 31),
        calendar=calendar,
        protected_ranges=PROTECTED,
    )
    assert result["state"] == "SOURCE_CONTRACT_VIOLATION"
    assert {"MAXIMUM_ROWS_EXCEEDED", "FUTURE_ROW_AFTER_ANCHOR"}.issubset(result["failures"])


def test_parser_rejects_anchor_when_contract_excludes_it():
    calendar = phase16b.build_weekday_calendar(
        date(2025, 1, 1), date(2025, 12, 31), official_closures=set(),
        source_urls=("fixture",), calendar_version="weekday fixture",
    )
    contract = phase16b.versioned_contract_update(
        _contract(source_id="krx", direction="EXACT", maximum_rows=1, date_field="TRD_DD"),
        new_version="exact-v2",
        changes={"anchor_included": False},
    )
    result = phase16b.verify_response_contract(
        {"output": [{"TRD_DD": "2025/06/02"}]},
        contract,
        request_anchor=date(2025, 6, 2),
        calendar=calendar,
        protected_ranges=PROTECTED,
    )
    assert result["valid"] is False
    assert "ANCHOR_ROW_UNEXPECTED" in result["failures"]


def test_exact_day_parser_rejects_rows_outside_the_verified_response_envelope():
    calendar = phase16b.build_weekday_calendar(
        date(2025, 1, 1), date(2025, 12, 31), official_closures=set(),
        source_urls=("fixture",), calendar_version="weekday fixture",
    )
    contract = _contract(source_id="krx", direction="EXACT", maximum_rows=1, date_field="TRD_DD")
    result = phase16b.verify_response_contract(
        {"output": [{"TRD_DD": "2025/01/30"}]},
        contract,
        request_anchor=date(2025, 1, 31),
        calendar=calendar,
        protected_ranges=PROTECTED,
    )
    assert result["valid"] is False
    assert "ROW_OUTSIDE_VERIFIED_RESPONSE_ENVELOPE" in result["failures"]


def test_krx_exact_day_parser_and_adapter_request_are_market_specific():
    contract = _contract(source_id="krx", direction="EXACT", maximum_rows=1, date_field="TRD_DD")
    calendar = phase16b.build_weekday_calendar(
        date(2025, 1, 1), date(2025, 12, 31), official_closures=set(),
        source_urls=("fixture",), calendar_version="weekday fixture",
    )
    path, kospi = phase16b.make_krx_request(date(2025, 6, 2), "STK")
    _, kosdaq = phase16b.make_krx_request(date(2025, 6, 2), "KSQ")
    assert "MDCSTAT02202_OUT" in path
    assert kospi["mktId"] == "STK" and kosdaq["mktId"] == "KSQ"
    assert kospi["strtDd"] == kospi["endDd"] == "20250602"
    result = phase16b.verify_response_contract(
        {"output": [{"TRD_DD": "2025/06/02", "TRDVAL1": "provider-value"}]},
        contract,
        request_anchor=date(2025, 6, 2),
        calendar=calendar,
        protected_ranges=PROTECTED,
    )
    assert result["valid"] is True
    assert result["returned_sessions"] == ["2025-06-02"]


def test_normalized_rows_separate_event_session_from_observation_time_and_classify_history():
    contract = _contract()
    observation = datetime(2026, 10, 20, 20, 35, tzinfo=KST)
    rows = phase16b.normalize_dated_rows(
        {"output": [
            {"stck_bsop_date": "20260915", "orgn_ntby_qty": "10"},
            {"stck_bsop_date": "20261019", "orgn_ntby_qty": "11"},
        ]},
        contract=contract,
        source_name=contract.source_name,
        symbol="005930",
        market="KOSPI",
        response_snapshot_id="snapshot-a",
        request_anchor=date(2026, 10, 20),
        observed_at=observation,
        prospective_start=date(2026, 10, 1),
    )
    assert rows[0]["logical_key"] == "fixture flow:005930:2026-09-15"
    assert rows[0]["request_anchor"] == "2026-10-20"
    assert rows[0]["observed_at_kst"].startswith("2026-10-20T20:35")
    assert rows[0]["evidence_class"] == "HISTORICAL_SESSION_REOBSERVED"
    assert rows[1]["evidence_class"] == "PROSPECTIVE_SESSION_OBSERVED"
    assert all(field["feature_status"] == "QUARANTINED_PROSPECTIVE" for row in rows for field in row["fields"])


def test_row_revision_and_response_hash_change_are_reported_separately():
    old = {"logical_key": "s:005930:2026-10-01", "response_snapshot_id": "a", "observed_at_utc": "t1", "provider_values": {"orgn": "4"}}
    new = {"logical_key": old["logical_key"], "response_snapshot_id": "b", "observed_at_utc": "t2", "provider_values": {"orgn": "5"}}
    result = phase16b.classify_row_revision(old, new, response_hash_changed=True)
    assert result["classification"] == "VALUE_REVISED"
    assert result["response_level_hash_changed"] is True
    assert result["changed_fields"] == ["orgn"]


def test_response_hash_change_can_be_separate_from_canonical_and_row_changes():
    old = {"logical_key": "s:005930:2026-10-01", "response_snapshot_id": "a", "observed_at_utc": "t1", "provider_values": {"orgn": "4"}}
    new = {"logical_key": old["logical_key"], "response_snapshot_id": "b", "observed_at_utc": "t2", "provider_values": {"orgn": "4"}}
    result = phase16b.classify_row_revision(
        old, new, response_hash_changed=True, canonical_response_hash_changed=False
    )
    assert result["classification"] == "SEMANTICALLY_IDENTICAL"
    assert result["response_level_hash_changed"] is True
    assert result["canonical_response_hash_changed"] is False
    assert result["changed_fields"] == []


@pytest.mark.parametrize(
    ("old_present", "new_present", "expected"),
    [(False, True, "MISSING_TO_PRESENT"), (True, False, "PRESENT_TO_MISSING")],
)
def test_row_revision_classifies_missingness_transitions(old_present, new_present, expected):
    old = {"logical_key": "s:005930:2026-10-01", "response_snapshot_id": "a", "observed_at_utc": "t1", "row_present": old_present}
    new = {"logical_key": old["logical_key"], "response_snapshot_id": "b", "observed_at_utc": "t2", "row_present": new_present}
    assert phase16b.classify_row_revision(old, new, response_hash_changed=False)["classification"] == expected


def test_field_revision_profile_keeps_provider_semantics_unresolved():
    records = [
        {"provider_values": {"orgn": "2", "prsn": "4"}},
        {"provider_values": {"orgn": "4", "prsn": "4"}},
    ]
    profile = phase16b.field_revision_profile(records, [])
    orgn = next(row for row in profile if row["field"] == "orgn")
    assert orgn["revision_count"] == 1
    assert orgn["semantic_status"] == "UNKNOWN"
    assert orgn["unit_status"] == "UNKNOWN"


def test_field_revision_profile_counts_non_numeric_field_changes():
    profile = phase16b.field_revision_profile(
        [
            {"provider_values": {"side": "BUY"}},
            {"provider_values": {"side": "SELL"}},
        ],
        [],
    )
    side = next(row for row in profile if row["field"] == "side")
    assert side["comparison_count"] == 1
    assert side["revision_count"] == 1
    assert side["revision_rate"] == 1.0
    assert side["median_abs_change"] is None


def test_config_revision_records_parent_hash_without_mutating_phase16_v1():
    old_path = Path("config/phase16/phase16-collector-config.json")
    new_path = Path("config/phase16/phase16b-collector-config.json")
    old_sha = hashlib.sha256(old_path.read_bytes()).hexdigest()
    config = json.loads(new_path.read_text(encoding="utf-8"))
    new_sha = hashlib.sha256(new_path.read_bytes()).hexdigest()
    assert config["phase16b"]["parent_config_sha256"] == old_sha
    assert new_sha == new_path.with_suffix(".sha256").read_text().split()[0]


def test_all_phase16b_config_revisions_form_a_sha_verified_parent_chain():
    paths = [
        Path("config/phase16/phase16b-collector-config.json"),
        Path("config/phase16/phase16b-collector-config-phase16b-v2.json"),
        Path("config/phase16/phase16b-collector-config-phase16b-v3.json"),
        Path("config/phase16/phase16b-collector-config-phase16b-v4.json"),
    ]
    previous_sha = hashlib.sha256(Path("config/phase16/phase16-collector-config.json").read_bytes()).hexdigest()
    for path in paths:
        raw = path.read_bytes()
        config = json.loads(raw)
        current_sha = hashlib.sha256(raw).hexdigest()
        assert config["parent_config_sha256"] == previous_sha
        assert path.with_suffix(".sha256").read_text().split()[0] == current_sha
        previous_sha = current_sha
    assert hashlib.sha256(paths[-1].read_bytes()).hexdigest() == hashlib.sha256(phase16b.DEFAULT_PHASE16B_CONFIG.read_bytes()).hexdigest()


def test_phase16b_first_safe_anchor_remains_before_or_after_current_date_without_guessing():
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    result = phase16b.flow_preflight(
        config,
        now=datetime(2026, 10, 2, 11, 30, tzinfo=KST),
        credentials_available=False,
        collector_healthy=True,
    )
    by_id = {row["source_id"]: row for row in result["sources"]}
    assert by_id["kis_per_stock_flow"]["first_safe_anchor_session"] == "2026-10-16"
    assert by_id["kis_program_flow"]["first_safe_anchor_session"] == "2026-10-15"
    assert by_id["krx_market_flow_exact_day"]["first_safe_anchor_session"] == "2026-08-31"
    assert by_id["kis_per_stock_flow"]["collection_state"] == "WAITING_FOR_SAFE_DATE"
    assert by_id["kis_program_flow"]["collection_state"] == "WAITING_FOR_SAFE_DATE"
    assert result["latest_completed_krx_session"] == "2026-10-01"


def test_phase17_readiness_stays_no_without_core_per_stock_vintages(tmp_path):
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    result = phase16b.flow_status(
        config,
        tmp_path / "phase16",
        now=datetime(2026, 10, 2, 13, 0, tzinfo=KST),
    )
    assert result["per_stock_prospective_sessions"] == 0
    assert result["per_stock_multi_vintage_sessions"] == 0
    assert result["phase17_source_stability_ready"] == "NO"
    assert result["external_2026"] == "NOT_READ"
    assert result["holdout_2026"] == "NOT_READ"


def test_active_kis_probe_records_response_vintage_and_universe_link(tmp_path, monkeypatch):
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    monkeypatch.setattr(phase16b, "credentials_available", lambda: True)
    monkeypatch.setattr(phase16b, "DEFAULT_PHASE16B_ROOT", tmp_path / "phase16b")
    monkeypatch.setattr(phase16b, "_latest_universe_snapshot_id", lambda _root: "universe-fixture-id")

    class FixtureClient:
        def get_investor_flow_by_date(self, symbol, anchor, *, max_pages):
            assert max_pages == 1
            return {"output": [{"stck_bsop_date": "20261016", "orgn_ntby_qty": symbol, "nullable_field": None}]}

    monkeypatch.setattr(phase16b, "_kis_client", lambda _config: FixtureClient())
    root = tmp_path / "phase16"
    observation = datetime(2026, 10, 16, 20, 20, tzinfo=KST)
    result = phase16b.collect_kis_flow(
        config=config,
        config_sha256=hashlib.sha256(phase16b.phase16._json_bytes(config)).hexdigest(),
        root=root,
        source_id="kis_per_stock_flow",
        symbols=[{"symbol": "005930", "market": "KOSPI"}, {"symbol": "086520", "market": "KOSDAQ"}],
        anchor=date(2026, 10, 16),
        observed_at=observation,
        observation_slot="probe-evening",
    )
    assert result["status"] == "COMPLETE"
    assert result["request_count"] == result["success_count"] == 2
    assert result["response_received_count"] == 2
    assert result["null_field_count"] == 2
    assert result["coverage"] == 1.0
    status = phase16b.flow_status(config, root, now=observation)
    assert status["prospective_flow_sessions"] == 1
    assert status["per_stock_prospective_sessions"] == 1
    assert status["phase17_source_stability_ready"] == "NO"
    revision = phase16b.revision_summary(root)
    assert revision["source_summaries"]["kis_per_stock_flow"]["prospective_event_sessions"] == 1
    manifests = phase16b.phase16.EvidenceStore(root).manifests()
    assert len(manifests) == 2
    for manifest in manifests:
        assert manifest["metadata"]["universe_snapshot_id"] == "universe-fixture-id"
        assert manifest["metadata"]["null_field_count"] == 1
        assert manifest["metadata"]["null_provider_fields"] == ["nullable_field"]
        normalized = json.loads((root / manifest["normalized_payload_path"]).read_text(encoding="utf-8"))
        assert normalized[0]["request_anchor"] == "2026-10-16"
        assert normalized[0]["observed_at_kst"].startswith("2026-10-16T20:20")
        assert normalized[0]["response_snapshot_id"] == manifest["snapshot_id"]


def test_krx_protected_response_breach_is_quarantined_and_stops_batch(tmp_path, monkeypatch):
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    monkeypatch.setattr(phase16b, "DEFAULT_PHASE16B_ROOT", tmp_path / "phase16b")
    calls = []

    def fetch(session, market_code):
        calls.append(market_code)
        payload = {"output": [{"TRD_DD": "2026/08/03", "SECRET_VALUE": "not emitted"}]}
        return payload, json.dumps(payload).encode()

    outcome = phase16b.collect_krx_exact_day(
        config=config,
        root=tmp_path / "phase16",
        session=date(2026, 10, 1),
        observed_at=datetime(2026, 10, 2, 20, 30, tzinfo=KST),
        fetch=fetch,
        observation_slot="full-evening",
    )
    assert outcome["status"] == "PROTECTED_RANGE_CONTRACT_BREACH"
    assert outcome["payload_persisted"] is False
    assert calls == ["STK"]
    assert phase16b.phase16.verify_store(tmp_path / "phase16")["snapshot_count"] == 0
    violation_files = list((tmp_path / "phase16" / "contract-violations").glob("*.json"))
    assert len(violation_files) == 1
    violation = json.loads(violation_files[0].read_text(encoding="utf-8"))
    assert violation["event_type"] == "PROTECTED_RANGE_CONTRACT_BREACH"


def test_duplicate_krx_schedule_slot_does_not_reach_transport(tmp_path, monkeypatch):
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    monkeypatch.setattr(phase16b, "DEFAULT_PHASE16B_ROOT", tmp_path / "phase16b")
    root = tmp_path / "phase16"
    calls = []

    def fetch(session, market_code):
        calls.append(market_code)
        payload = {"output": [{"TRD_DD": "2026/10/01", "provider_field": "structural-fixture"}]}
        return payload, json.dumps(payload).encode()

    first = phase16b.collect_krx_exact_day(
        config=config, root=root, session=date(2026, 10, 1),
        observed_at=datetime(2026, 10, 2, 20, 30, tzinfo=KST),
        fetch=fetch, observation_slot="full-evening",
    )
    second = phase16b.collect_krx_exact_day(
        config=config, root=root, session=date(2026, 10, 1),
        observed_at=datetime(2026, 10, 2, 20, 31, tzinfo=KST),
        fetch=fetch, observation_slot="full-evening",
    )
    assert first["status"] == "COMPLETE"
    assert second["status"] == "ACCIDENTAL_DUPLICATE_JOB"
    assert calls == ["STK", "KSQ"]
    manifests = phase16b.phase16.EvidenceStore(root).manifests()
    for manifest in manifests:
        normalized = json.loads((root / manifest["normalized_payload_path"]).read_text(encoding="utf-8"))
        assert all(row["response_snapshot_id"] == manifest["snapshot_id"] for row in normalized)


def test_kis_pagination_contract_violation_disables_future_requests(tmp_path, monkeypatch):
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    monkeypatch.setattr(phase16b, "credentials_available", lambda: True)
    calls = []

    class PaginationDriftClient:
        def get_investor_flow_by_date(self, symbol, anchor, *, max_pages):
            calls.append((symbol, anchor, max_pages))
            raise phase16b.KisApiError("KIS dated-flow pagination exceeded its safe page limit")

    monkeypatch.setattr(phase16b, "_kis_client", lambda _config: PaginationDriftClient())
    root = tmp_path / "phase16"
    kwargs = {
        "config": config,
        "config_sha256": hashlib.sha256(phase16b.phase16._json_bytes(config)).hexdigest(),
        "root": root,
        "source_id": "kis_per_stock_flow",
        "symbols": [{"symbol": "005930", "market": "KOSPI"}, {"symbol": "000660", "market": "KOSPI"}],
        "anchor": date(2026, 10, 16),
        "observed_at": datetime(2026, 10, 16, 16, 20, tzinfo=KST),
        "observation_slot": "probe-close",
    }
    first = phase16b.collect_kis_flow(**kwargs)
    second = phase16b.collect_kis_flow(**{**kwargs, "observation_slot": "probe-evening"})
    assert first["status"] == "PARTIAL"
    assert second["status"] == "ERROR"
    assert len(calls) == 1
    violation = next((root / "contract-violations").glob("*.json"))
    assert json.loads(violation.read_text(encoding="utf-8"))["event_type"] == "SOURCE_CONTRACT_VIOLATION"


def test_historical_probe_is_blocked_after_source_contract_violation(tmp_path, monkeypatch):
    config = json.loads(phase16b.DEFAULT_PHASE16B_CONFIG.read_text(encoding="utf-8"))
    monkeypatch.setattr(phase16b, "DEFAULT_PHASE16B_ROOT", tmp_path / "phase16b")
    monkeypatch.setattr(phase16b, "credentials_available", lambda: True)
    root = tmp_path / "phase16"
    violations = root / "contract-violations"
    violations.mkdir(parents=True)
    (violations / "violation.json").write_text(
        json.dumps({
            "event_type": "SOURCE_CONTRACT_VIOLATION",
            "source_id": "kis_per_stock_flow",
            "payload_persisted": False,
        }),
        encoding="utf-8",
    )
    client_requests = []

    def unexpected_kis_client(_config):
        client_requests.append("created")
        raise AssertionError("contract-violated source must not reach transport setup")

    monkeypatch.setattr(phase16b, "_kis_client", unexpected_kis_client)
    result = phase16b.collect_active_flow_slot(
        config=config,
        config_sha256=hashlib.sha256(phase16b.phase16._json_bytes(config)).hexdigest(),
        root=root,
        slot="historical-weekly",
        now=datetime(2026, 10, 16, 20, 45, tzinfo=KST),
    )

    assert result["status"] == "CONTRACT_REVIEW_REQUIRED"
    assert result["request_count"] == 0
    assert result["network_accessed"] is False
    assert client_requests == []


def test_flow_launchd_templates_include_guarded_daily_slots_and_weekly_probe(tmp_path):
    plists = phase16b.build_flow_launchd_plists(root=tmp_path)
    by_label = {row["Label"]: row for row in plists}
    assert len(plists) == 5
    assert by_label["com.krxtrader.phase16.flow.full-evening"]["StartCalendarInterval"] == {"Hour": 20, "Minute": 30}
    weekly = by_label["com.krxtrader.phase16.flow.historical-weekly"]
    assert weekly["StartCalendarInterval"] == {"Hour": 20, "Minute": 45, "Weekday": 5}
    assert weekly["ProgramArguments"][-2:] == ["--slot", "historical-weekly"]

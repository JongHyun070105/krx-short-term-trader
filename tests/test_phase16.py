from __future__ import annotations

import copy
import hashlib
import io
import json
import multiprocessing
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from krx_trader.research import phase16, phase16b
from krx_trader.research.phase16 import (
    AppendOnlyError,
    EvidenceStore,
    ProtectedRangeError,
    build_launchd_plist,
    build_revision_records,
    canonical_payload_sha256,
    classify_observation_delay,
    classify_revision,
    collect_flow_preflight,
    collect_universe,
    deterministic_archive,
    execute_guarded_request,
    export_evidence,
    health_report,
    load_config,
    make_snapshot_id,
    make_status,
    normalize_flow_payload,
    raw_payload_sha256,
    request_range_guard,
    sanitize_payload,
    storage_usage,
    timezone_pair,
    verify_previous_phase_snapshot,
    verify_store,
)

KST = phase16.KST
OBSERVED = datetime(2026, 10, 1, 20, 31, 4, tzinfo=KST)


def _manifest(snapshot_id: str, *, raw: str, canonical: str, schema: str = "schema") -> dict:
    return {
        "snapshot_id": snapshot_id,
        "logical_key": "source:005930:2026-10-01",
        "raw_payload_sha256": raw,
        "canonical_payload_sha256": canonical,
        "schema_fingerprint": schema,
        "observed_at_utc": "2026-10-01T11:31:04+00:00",
        "requested_session": "2026-10-01",
        "provider_session": "2026-10-01",
    }


def _create_snapshot(store: EvidenceStore, payload, *, observed_at=OBSERVED, logical_key="flow:005930:2026-10-01", evidence_class="PROSPECTIVE_OBSERVED", source_name="flow"):
    return store.create_snapshot(
        evidence_class=evidence_class,
        source_name=source_name,
        provider="fixture provider",
        endpoint_id="fixture-endpoint",
        request_schema_version="v1",
        logical_key=logical_key,
        payload=payload,
        normalized_payload=payload,
        observed_at=observed_at,
        requested_session=date(2026, 10, 1),
        provider_session=date(2026, 10, 1),
        config_sha256="c" * 64,
        collector_git_sha="g" * 40,
    )


def _hold_file_lock(path: str, ready, release) -> None:
    with phase16._file_lock(Path(path)) as locked:
        if locked:
            ready.set()
            release.wait(5)


def _master_archive(market: str, symbols: list[str]) -> bytes:
    width = 227 if market == "KOSPI" else 221
    name_width = 28 if market == "KOSPI" else 35
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        lines = []
        for symbol in symbols:
            tail = list(" " * width)
            tail[0:2] = "ST"
            lines.append(f"{symbol:<9}{'KR7000000000':<12}{('Test ' + symbol):<{name_width}}" + "".join(tail))
        archive.writestr(f"{market.lower()}_code.mst", "\n".join(lines) + "\n")
    return stream.getvalue()


def test_snapshot_id_is_stable_and_contains_logical_identity_and_payload_hash():
    args = {
        "source_name": "KIS per-stock investor flow",
        "logical_key": "flow:005930:2026-10-01",
        "symbol": "005930",
        "requested_session": date(2026, 10, 1),
        "observed_at": OBSERVED,
        "canonical_sha256": "a" * 64,
        "raw_sha256": "c" * 64,
    }
    first = make_snapshot_id(**args)
    assert first == make_snapshot_id(**args)
    assert "005930" in first
    assert "2026-10-01" in first
    assert "a" * 64 in first
    assert first != make_snapshot_id(**{**args, "canonical_sha256": "b" * 64})
    assert first != make_snapshot_id(**{**args, "raw_sha256": "d" * 64})


def test_evidence_timestamps_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        timezone_pair(datetime(2026, 10, 1, 20, 0))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone offset"):
        phase16.parse_aware_datetime("2026-10-01T20:00:00")
    utc, kst = timezone_pair(OBSERVED)
    assert utc.endswith("+00:00")
    assert kst.endswith("+09:00")


def test_raw_hash_preserves_received_order_while_canonical_hash_ignores_object_order():
    left = {"output": [{"date": "20261001", "value": "10"}, {"date": "20260930", "value": "9"}]}
    right = {"output": [{"value": "9", "date": "20260930"}, {"value": "10", "date": "20261001"}]}
    assert raw_payload_sha256(left) != raw_payload_sha256(right)
    assert canonical_payload_sha256(left) == canonical_payload_sha256(right)
    assert raw_payload_sha256(b'{ "x": 1 }') != raw_payload_sha256(b'{"x":1}')
    assert canonical_payload_sha256({"output": [], "_phase15_pages": 1}) == canonical_payload_sha256({"output": [], "_phase15_pages": 2})


def test_credential_fields_are_redacted_before_hashing_and_persistence():
    value, flags = sanitize_payload({"output": [{"appsecret": "not-a-real-secret", "orgn_ntby_qty": "14"}]})
    assert value["output"][0]["appsecret"] == "[REDACTED]"
    assert value["output"][0]["orgn_ntby_qty"] == "14"
    assert flags == ["CREDENTIAL_FIELD_REDACTED"]


def test_raw_json_bytes_are_redacted_before_persistent_retention(tmp_path):
    store = EvidenceStore(tmp_path / "store")
    manifest = store.create_snapshot(
        evidence_class="PROSPECTIVE_OBSERVED",
        source_name="fixture",
        provider="fixture",
        endpoint_id="fixture",
        request_schema_version="v1",
        logical_key="fixture:2026-10-01",
        payload={"output": [{"value": "4"}]},
        normalized_payload={"output": [{"value": "4"}]},
        raw_payload=b'{"access_token":"secret-token","value":"4"}',
        persist_raw=True,
        observed_at=OBSERVED,
        config_sha256="c" * 64,
        collector_git_sha="g" * 40,
    )
    raw_path = tmp_path / "store" / manifest["raw_payload_path"]
    stored = raw_path.read_bytes()
    assert b"secret-token" not in stored
    assert json.loads(stored)["access_token"] == "[REDACTED]"
    assert "CREDENTIAL_FIELD_REDACTED" in manifest["quality_flags"]


def test_protected_flow_windows_are_denied_before_transport():
    config, _, _ = load_config()
    for source_id in ("kis_per_stock_flow", "kis_market_flow", "kis_program_flow"):
        decision = request_range_guard(source_id, date(2026, 10, 1), config=config)
        assert decision["allowed"] is False
        assert decision["request_guard"] == "DENY"
        assert "HOLDOUT_2026" in decision["protected_overlaps"]
    called = []
    with pytest.raises(ProtectedRangeError, match="before network access"):
        execute_guarded_request(
            "kis_per_stock_flow",
            date(2026, 10, 1),
            config=config,
            transport=lambda: called.append("called"),
        )
    assert called == []


@pytest.mark.parametrize("session", [date(2026, 2, 2), date(2026, 8, 3)])
def test_exact_date_guard_rejects_both_protected_periods(session):
    config, _, _ = load_config()
    custom = copy.deepcopy(config)
    custom["sources"]["exact-test"] = {
        "request_semantics": "EXACT_DATE",
        "range_contract_proven": True,
        "exact_date_semantics_proven": True,
    }
    assert request_range_guard("exact-test", session, config=custom)["request_guard"] == "DENY"


def test_exact_date_current_session_is_allowed_and_transport_runs():
    config, _, _ = load_config()
    custom = copy.deepcopy(config)
    custom["sources"]["exact-test"] = {
        "request_semantics": "EXACT_DATE",
        "range_contract_proven": True,
        "exact_date_semantics_proven": True,
    }
    assert request_range_guard("exact-test", date(2026, 10, 1), config=custom)["request_guard"] == "ALLOW"
    assert execute_guarded_request("exact-test", date(2026, 10, 1), config=custom, transport=lambda: "ok") == "ok"


def test_current_only_listing_archive_passes_guard_but_is_not_a_flow_source():
    config, _, _ = load_config()
    assert request_range_guard("kis_current_listings", date(2026, 10, 1), config=config)["allowed"] is True
    assert collect_flow_preflight("kis_per_stock_flow", ["005930"], config=config, session=date(2026, 10, 1))["network_accessed"] is False


def test_flow_preflight_cli_honors_persisted_contract_violation(tmp_path, monkeypatch, capsys):
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
    monkeypatch.setattr(phase16b, "credentials_available", lambda: False)
    monkeypatch.setattr(phase16b, "DEFAULT_PHASE16B_ROOT", tmp_path / "phase16b")

    assert phase16.main(["--root", str(root), "flow-preflight"]) == 0
    report = json.loads(capsys.readouterr().out)
    per_stock = next(row for row in report["sources"] if row["source_id"] == "kis_per_stock_flow")

    assert per_stock["collection_state"] == "ERROR"
    assert per_stock["reason"] == "CONTRACT_REVIEW_REQUIRED"
    assert per_stock["request_decision"] == "DENY_PROTECTED_RANGE"
    assert report["network_accessed"] is False


def test_append_only_store_rejects_snapshot_id_reuse(tmp_path):
    store = EvidenceStore(tmp_path / "phase16")
    payload = {"output": [{"value": "10"}]}
    first = _create_snapshot(store, payload)
    with pytest.raises(AppendOnlyError, match="already exists"):
        _create_snapshot(store, payload)
    assert verify_store(store.root)["valid"] is True
    assert verify_store(store.root)["snapshot_count"] == 1
    assert first["previous_vintage_id"] is None


def test_same_source_symbol_session_later_observation_creates_new_vintage(tmp_path):
    store = EvidenceStore(tmp_path / "phase16")
    first = _create_snapshot(store, {"output": [{"value": "10"}]})
    second = _create_snapshot(
        store,
        {"output": [{"value": "11"}]},
        observed_at=OBSERVED + timedelta(hours=12),
    )
    assert first["snapshot_id"] != second["snapshot_id"]
    assert second["previous_vintage_id"] == first["snapshot_id"]
    assert verify_store(store.root)["snapshot_count"] == 2


def test_manifest_chain_detects_payload_mutation(tmp_path):
    store = EvidenceStore(tmp_path / "phase16")
    manifest = _create_snapshot(store, {"value": "before"})
    assert verify_store(store.root)["valid"] is True
    payload_path = store.root / manifest["normalized_payload_path"]
    payload_path.write_text('{"value":"after"}', encoding="utf-8")
    report = verify_store(store.root)
    assert report["valid"] is False
    assert any("normalized_payload_path hash mismatch" in item for item in report["errors"])


def test_failed_atomic_payload_write_never_appears_as_a_valid_snapshot(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path / "phase16")
    original_link = phase16.os.link

    def fail_normalized(source, destination):
        if "normalized" in str(destination):
            raise OSError("simulated failure before publication")
        original_link(source, destination)

    monkeypatch.setattr(phase16.os, "link", fail_normalized)
    with pytest.raises(OSError, match="simulated failure"):
        _create_snapshot(store, {"value": "10"})
    assert list((store.root / "prospective/manifests/snapshots").glob("*.json")) == []
    assert verify_store(store.root)["valid"] is False


def test_historical_and_prospective_evidence_use_separate_trees(tmp_path):
    store = EvidenceStore(tmp_path / "phase16")
    prospective = _create_snapshot(store, {"value": "new"})
    historical = _create_snapshot(
        store,
        {"value": "old"},
        observed_at=OBSERVED + timedelta(seconds=1),
        logical_key="history:005930:2020-01-02",
        evidence_class="HISTORICAL_REVISION_PROBE",
        source_name="history probe",
    )
    assert (store.root / "prospective/manifests/snapshots" / f"{prospective['snapshot_id']}.json").exists()
    assert (store.root / "revision_probes/manifests/snapshots" / f"{historical['snapshot_id']}.json").exists()
    assert verify_store(store.root)["snapshot_count"] == 2


def test_flow_normalization_preserves_ambiguous_fields_missingness_and_unknown_units():
    normalized = normalize_flow_payload(
        {"output1": [{"stck_bsop_date": "20261001", "orgn_ntby_qty": "14", "prsn_ntby_qty": None}]},
        symbol="005930",
        market="KOSPI",
        observed_at=OBSERVED,
        source="KIS per-stock investor daily flow",
        snapshot_id="fixture-snapshot",
    )
    orgn = next(row for row in normalized if row["provider_field_name"] == "orgn_ntby_qty")
    missing = next(row for row in normalized if row["provider_field_name"] == "prsn_ntby_qty")
    assert orgn["raw_numeric_value"] == "14"
    assert orgn["normalized_numeric_value"] is None
    assert orgn["unit_status"] == "UNKNOWN"
    assert orgn["semantic_status"] == "UNRESOLVED"
    assert orgn["provider_field_group"] == "orgn_UNRESOLVED"
    assert missing["raw_value"] is None
    assert missing["raw_numeric_value"] is None
    assert {row["provider_field_name"] for row in normalized}.isdisjoint({"institution_net_flow"})


def test_revision_classifications_cover_identical_and_semantic_equality():
    old = _manifest("old", raw="a", canonical="same")
    same = _manifest("same", raw="a", canonical="same")
    formatted = _manifest("formatted", raw="b", canonical="same")
    assert classify_revision(old, same, {"x": 1}, {"x": 1})["classification"] == "IDENTICAL"
    assert classify_revision(old, formatted, {"x": 1}, {"x": 1})["classification"] == "SEMANTICALLY_IDENTICAL"


def test_revision_value_diff_has_field_level_absolute_relative_values_and_age():
    old = _manifest("old", raw="a", canonical="old")
    new = _manifest("new", raw="b", canonical="new")
    new["observed_at_utc"] = "2026-10-02T11:31:04+00:00"
    result = classify_revision(old, new, {"orgn_ntby_qty": "100"}, {"orgn_ntby_qty": "125"})
    assert result["classification"] == "VALUE_REVISED"
    assert result["field_changes"][0]["field"] == "orgn_ntby_qty"
    assert result["field_changes"][0]["absolute_difference"] == "25"
    assert result["field_changes"][0]["relative_difference"] == "0.25"
    assert result["age_at_later_observation_seconds"] == 86400
    assert result["business_sessions_since_event"] is None


@pytest.mark.parametrize(
    ("old_payload", "new_payload", "expected"),
    [
        ({"x": None}, {"x": "1"}, "MISSING_TO_PRESENT"),
        ({"x": "1"}, {"x": None}, "PRESENT_TO_MISSING"),
        ({"x": "1"}, {"x": "1", "y": "2"}, "FIELD_ADDED"),
        ({"x": "1", "y": "2"}, {"x": "1"}, "FIELD_REMOVED"),
    ],
)
def test_revision_classifies_missing_and_field_changes(old_payload, new_payload, expected):
    old = _manifest("old", raw="a", canonical="old")
    new = _manifest("new", raw="b", canonical="new")
    result = classify_revision(old, new, old_payload, new_payload)
    assert result["classification"] == expected
    assert result["field_changes"]


def test_revision_detects_schema_change_and_uncomparable_payload():
    old = _manifest("old", raw="a", canonical="old", schema="object-string")
    new = _manifest("new", raw="b", canonical="new", schema="object-int")
    assert classify_revision(old, new, {"x": "1"}, {"x": 1})["classification"] == "SCHEMA_CHANGED"
    assert classify_revision(old, new, None, {"x": 1})["classification"] == "UNCOMPARABLE"


def test_revision_index_creates_explicit_schema_drift_event(tmp_path):
    store = EvidenceStore(tmp_path / "phase16")
    first = _create_snapshot(store, {"x": "1"}, logical_key="listing:day1", source_name="listings")
    second = _create_snapshot(store, {"x": "1", "new_field": "2"}, observed_at=OBSERVED + timedelta(days=1), logical_key="listing:day2", source_name="listings")
    records = build_revision_records(store.root)
    assert records == []
    schema_events = list((store.root / "revision/schema-events").glob("*.json"))
    assert len(schema_events) == 1
    assert json.loads(schema_events[0].read_text())["event_type"] == "SCHEMA_CHANGED"
    assert first["snapshot_id"] != second["snapshot_id"]


def test_collection_lock_reports_competing_process(tmp_path):
    lock = tmp_path / "collector.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_file_lock, args=(str(lock), ready, release))
    process.start()
    try:
        assert ready.wait(3)
        with phase16._file_lock(lock, blocking=False) as acquired:
            assert acquired is False
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0


def test_phase16_uses_repository_persistent_limiter_across_instances(tmp_path):
    state = tmp_path / "shared-limiter.json"
    now = [100.0]
    waits = []
    first = phase16._PersistentRateLimiter(state, 4.0, waits.append, lambda: now[0])
    second = phase16._PersistentRateLimiter(state, 4.0, waits.append, lambda: now[0])
    first.wait()
    second.wait()
    assert waits == [4.0]
    assert json.loads(state.read_text())["last_request_at"] == 104.0


def test_transient_archive_error_uses_bounded_retry_and_permanent_error_stops(monkeypatch):
    class Limiter:
        def __init__(self):
            self.waits = 0

        def wait(self):
            self.waits += 1

        def defer(self, *_args, **_kwargs):
            pass

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"archive"

    calls = []

    def opener(_request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise phase16.urllib.error.HTTPError("url", 503, "retry", None, None)
        return Response()

    monkeypatch.setattr(phase16.urllib.request, "urlopen", opener)
    limiter = Limiter()
    sleeps = []
    body, retries = phase16._fetch_public_archive("https://example.invalid/master.zip", limiter=limiter, max_retries=1, sleeper=sleeps.append)
    assert body == b"archive"
    assert retries == 1
    assert len(calls) == limiter.waits == 2
    assert sleeps == [0.5]


def test_batch_coverage_and_status_use_a_mocked_current_listing_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(phase16, "verify_previous_phase_snapshot", lambda _root: {"status": "PASS", "verified_count": 850, "artifact_count": 850})
    archives = {
        "KOSPI": _master_archive("KOSPI", ["001440"]),
        "KOSDAQ": _master_archive("KOSDAQ", ["086520"]),
    }

    def fetch(url, _limiter, _retries):
        market = "KOSPI" if "kospi" in url else "KOSDAQ"
        return archives[market], 0

    fixed = lambda: OBSERVED
    result = collect_universe(root=tmp_path / "store", observed_at=OBSERVED, fetch_archive=fetch, clock=fixed)
    assert result["snapshot_count"] == 2
    assert result["manifest_chain_valid"] is True
    assert result["status"] == "PARTIAL"
    assert result["target_symbols"] == 100
    assert result["successful_symbols"] == 1
    assert result["coverage"] == 0.01
    assert result["current_listing_coverage"] == 1.0
    manifests = EvidenceStore(tmp_path / "store").manifests()
    scopes = {m["metadata"]["universe_scope"] for m in manifests}
    assert scopes == {"FULL_CURRENT_LISTINGS", "FROZEN_RESEARCH_COHORT"}
    assert verify_store(tmp_path / "store")["valid"] is True
    summary = json.loads((tmp_path / "store/reports/phase16-summary.json").read_text())
    assert summary["manifest_chain"] == "PASS"
    assert summary["per_stock_flow_prospective"] == "DEFERRED_SAFETY_GUARD"
    assert summary["prospective_sessions"] == 0
    assert summary["first_observation_at_utc"] == "2026-10-01T11:31:04.000000+00:00"


def test_failed_current_listing_batch_records_failure_without_payload_or_fake_success(tmp_path, monkeypatch):
    monkeypatch.setattr(phase16, "verify_previous_phase_snapshot", lambda _root: {"status": "PASS", "verified_count": 850, "artifact_count": 850})

    def fail_before_response(_url, _limiter, _retries):
        raise phase16.CollectionFailure("TRANSPORT_ERROR", "current listing transport failed (TimeoutError)", request_count=3, retry_count=2)

    root = tmp_path / "failed-store"
    result = collect_universe(root=root, observed_at=OBSERVED, fetch_archive=fail_before_response, clock=lambda: OBSERVED)
    assert result["status"] == "FAILED"
    assert result["target_symbols"] == 100
    assert result["successful_symbols"] == 0
    assert result["request_count"] == 3
    assert verify_store(root)["snapshot_count"] == 0
    batch_files = list((root / "prospective/manifests/batches").glob("*.json"))
    assert len(batch_files) == 1
    batch = json.loads(batch_files[0].read_text())
    assert batch["transport_errors"] == 1
    assert batch["coverage"] == 0.0


def test_missed_observation_is_recorded_without_backfill_or_timestamp_reuse(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    phase16._atomic_replace(root / "health/scheduler-state.json", phase16._json_bytes({"last_observed_date": "2026-10-01"}, pretty=True))
    observed = datetime(2026, 10, 4, 20, 20, tzinfo=KST)
    missed = phase16._append_missed_events(root, observed)
    assert [item["scheduled_date"] for item in missed] == ["2026-10-02", "2026-10-03"]
    assert all(item["status"] == "MISSED_OBSERVATION" and item["backfilled"] is False for item in missed)
    assert classify_observation_delay(datetime(2026, 10, 2, 20, 15, tzinfo=KST), observed) == "LATE_CATCHUP"
    assert observed.isoformat() == "2026-10-04T20:20:00+09:00"


def test_launchd_schedule_is_deterministic_and_contains_only_safe_listing_command(tmp_path):
    plist = build_launchd_plist(tmp_path / "phase16", Path("/tmp/python"))
    assert plist["StartCalendarInterval"] == {"Hour": 20, "Minute": 15}
    args = plist["ProgramArguments"]
    assert "collect-universe" in args
    assert "collect-full" not in args
    assert "POST_20KST" in args
    assert plist["RunAtLoad"] is False


def test_status_and_health_report_data_readiness_without_claiming_source_stability(tmp_path, monkeypatch):
    root = tmp_path / "empty-store"
    monkeypatch.setattr(phase16, "verify_previous_phase_snapshot", lambda _root: {"status": "PASS", "verified_count": 850, "artifact_count": 850})
    status = make_status(root)
    assert status["prospective_sessions"] == 0
    assert status["multi_vintage_sessions"] == 0
    assert status["source_stability"] == "INSUFFICIENT_OBSERVATIONS"
    assert status["phase17_source_stability_ready"] == "NO"
    health = health_report(root)
    assert health["status"] == "PASS_WITH_WARNINGS"
    assert "SOURCE_STABILITY_INSUFFICIENT" in health["warnings"]
    assert storage_usage(root)["total_bytes"] > 0
    assert (root / "reports/phase16-summary.json").is_file()


def test_evidence_export_is_deterministic_content_addressed_and_redacted(tmp_path):
    root = tmp_path / "phase16"
    store = EvidenceStore(root)
    _create_snapshot(store, {"output": [{"authorization": "Bearer private-token-value", "value": "12"}]})
    first = deterministic_archive(root)
    second = deterministic_archive(root)
    assert first == second
    assert b"private-token-value" not in first
    out = tmp_path / "exports"
    result = export_evidence(root, out)
    archive_path = out / result["archive"]
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == result["archive_sha256"]
    assert result["archive_bytes"] == len(first)


def test_previous_phase_artifact_snapshot_detects_mutation_without_reading_other_paths(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    artifact = repo / "runtime/research/phase14/summary.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"status":"PASS"}', encoding="utf-8")
    phase_root = repo / "runtime/research/phase16"
    phase_root.mkdir(parents=True)
    item_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    snapshot = {"artifact_count": 1, "artifacts": [{"path": "runtime/research/phase14/summary.json", "sha256": item_hash}]}
    (phase_root / "previous-phase-artifact-snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(phase16, "REPO_ROOT", repo)
    assert verify_previous_phase_snapshot(phase_root)["status"] == "PASS"
    artifact.write_text('{"status":"CHANGED"}', encoding="utf-8")
    report = verify_previous_phase_snapshot(phase_root)
    assert report["status"] == "FAIL"
    assert report["changed"] == ["runtime/research/phase14/summary.json"]


def test_export_never_includes_env_files_or_limiter_credentials(tmp_path):
    root = tmp_path / "phase16"
    EvidenceStore(root)
    (root / ".env").write_text("APP_SECRET=never-export-this", encoding="utf-8")
    archive = deterministic_archive(root)
    assert b"never-export-this" not in archive
    assert b".env" not in archive

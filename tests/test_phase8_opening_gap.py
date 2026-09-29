from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from krx_trader.data.cache import ParquetBarCache
from krx_trader.models import Bar
from krx_trader.research import phase8_opening_gap as phase8
from krx_trader.research.phase5_backfill import backfill_symbol_sessions
from krx_trader.research.phase8_opening_gap import (
    _clean_confidence_events,
    _daily_return_horizons,
    _gap_fraction,
    _gap_state,
    _phase6_reconciliation,
    _range_metrics,
    _window_bars,
    assert_phase8_development_session,
    assert_phase8_external_allowed,
    assert_phase8_secondary_allowed,
    build_opening_gap_events,
    gap_bucket,
    load_phase8_development_dataset,
    next_executable_open,
    opening_gap_pct,
    previous_valid_close,
    run_phase8_acquisition,
    session_open_bar,
)

KST = ZoneInfo("Asia/Seoul")
DAY = date(2026, 4, 20)


def _bar(day: date, minute: int, price: float, *, high: float | None = None,
         low: float | None = None, open_: float | None = None, volume: int = 100) -> Bar:
    timestamp = datetime.combine(day, time(9, 0), KST) + timedelta(minutes=minute)
    opening = price if open_ is None else open_
    return Bar(timestamp, opening, high if high is not None else max(opening, price),
               low if low is not None else min(opening, price), price, volume)


def _session(day: date = DAY, *, open_price: float = 102.0, later_price: float = 103.0) -> tuple[Bar, ...]:
    bars = []
    for minute in range(380):
        price = open_price if minute < 30 else later_price
        bars.append(_bar(day, minute, price))
    return tuple(bars)


def _daily(day: date, close: float) -> Bar:
    return Bar(datetime.combine(day, time.min, KST), close, close, close, close, 10_000)


def test_previous_valid_close_uses_latest_prior_safe_session_not_calendar_yesterday():
    rows = [_daily(date(2026, 4, 16), 80), _daily(date(2026, 4, 17), 100), _daily(DAY, 110)]
    assert previous_valid_close(rows, DAY) == (date(2026, 4, 17), 100)
    assert previous_valid_close([rows[1], rows[2]], date(2026, 4, 17)) is None


def test_session_open_lookup_requires_exact_0900_bar():
    assert session_open_bar((_bar(DAY, 0, 101, open_=100),), DAY).open == 100
    assert session_open_bar((_bar(DAY, 1, 101),), DAY) is None


def test_gap_calculation_and_bucket_boundaries_are_fixed():
    assert opening_gap_pct(103, 100) == pytest.approx(3.0)
    assert [gap_bucket(value) for value in (-5, -3, -1, -0.1, 0, 1, 3, 5)] == [
        "<= -5%", "-5% to -3%", "-3% to -1%", "-1% to 0%",
        "0 to +1%", "+1% to +3%", "+3% to +5%", ">= +5%",
    ]


def test_opening_range_requires_all_fixed_minutes_and_reports_width():
    bars = tuple(_bar(DAY, minute, 100 + minute / 10, high=102, low=99) for minute in range(15))
    result = _range_metrics(bars, DAY, 15)
    assert result["complete"] is True
    assert result["high"] == 102
    assert result["low"] == 99
    assert result["width_pct"] == pytest.approx(3.0)
    assert not _range_metrics(bars[:-1], DAY, 15)["complete"]


def test_gap_retained_fraction_and_gap_fill_touch_direction():
    assert _gap_fraction(102.4, 100, 103) == pytest.approx(0.8)
    assert _gap_fraction(97.6, 100, 97) == pytest.approx(0.8)
    assert _gap_fraction(100, 100, 103) == pytest.approx(0.0)
    assert _gap_state(tuple(_bar(DAY, i, 103, high=103, low=100 if i == 9 else 102) for i in range(60)),
                      100, 103, 3) == "GAP_FULLY_FILLED"


@pytest.mark.parametrize(
    ("close", "high", "low", "expected"),
    [
        (103, 103, 102, "GAP_HELD"),
        (103, 104, 102, "GAP_EXTENDED"),
        (101, 102, 100.5, "GAP_PARTIALLY_FILLED"),
        (100.5, 102, 99, "GAP_FULLY_FILLED"),
        (99, 102, 98, "GAP_REVERSED"),
    ],
)
def test_first_hour_gap_states_use_completed_window_only(close, high, low, expected):
    bars = tuple(_bar(DAY, minute, close, high=high, low=low) for minute in range(60))
    assert _gap_state(bars, 100, 103, 3) == expected


def test_down_gap_recovery_state_is_descriptive_and_directional():
    bars = tuple(_bar(DAY, minute, 98, high=98, low=97) for minute in range(60))
    assert _gap_state(bars, 100, 97, -3) == "GAP_PARTIALLY_FILLED"


def test_next_executable_open_uses_next_bar_not_signal_bar_close():
    bars = (_bar(DAY, 14, 110, open_=109), _bar(DAY, 15, 120, open_=111))
    next_bar = next_executable_open(bars, datetime.combine(DAY, time(9, 15), KST))
    assert next_bar is bars[1]
    assert next_bar.open == 111


def test_future_bars_cannot_change_first_half_hour_observation_or_entry():
    baseline = list(_session())
    changed_future = list(baseline)
    changed_future[45] = _bar(DAY, 45, 1000, high=1001, low=999)
    assert _range_metrics(baseline, DAY, 30) == _range_metrics(changed_future, DAY, 30)
    boundary = datetime.combine(DAY, time(9, 30), KST)
    assert next_executable_open(baseline, boundary).open == next_executable_open(changed_future, boundary).open
    assert _window_bars(changed_future, DAY, 60)[45].high == 1001


def test_gap_event_records_checkpoints_excursions_fill_and_suspicious_action_flag():
    bars = list(_session(open_price=125, later_price=127))
    bars[20] = _bar(DAY, 20, 124, high=126, low=99)
    daily = {"000001": (_daily(date(2026, 4, 17), 100),)}
    events, excluded = build_opening_gap_events(
        minutes_by_symbol_session={("000001", DAY): tuple(bars)},
        daily_by_symbol=daily, market_by_symbol={"000001": "KOSDAQ"},
    )
    assert excluded == {}
    event = events[0]
    assert event["gap_pct"] == pytest.approx(25.0)
    assert event["suspicious_extreme_gap"] is True
    assert event["corporate_action_status"] == "UNVERIFIED_SUSPICIOUS_PRICE_JUMP"
    assert event["gap_fill"]["30M"] is True
    assert event["open_to_checkpoint_return_pct"]["09:15"] is not None
    assert event["open_to_checkpoint_return_pct"]["09:30"] is not None
    assert event["open_to_checkpoint_return_pct"]["10:00"] is not None
    assert event["open_to_checkpoint_return_pct"]["CONTINUOUS_FINAL"] is not None
    assert event["excursions_from_open"]["mfe_pct"] > 0
    assert event["excursions_from_open"]["mae_pct"] < 0
    assert event["price_limit_context"] == "NOT_NEAR_APPROX_30_PERCENT_BAND"
    assert event["prior_close_source"] == "KIS_DAILY_CLOSE_RAW"


def test_missing_daily_reference_uses_labeled_prior_safe_minute_close_proxy():
    prior_day = date(2026, 4, 17)
    minutes = {
        ("000001", prior_day): _session(prior_day, open_price=100, later_price=101),
        ("000001", DAY): _session(DAY, open_price=103, later_price=104),
    }
    events, excluded = build_opening_gap_events(
        minutes_by_symbol_session=minutes, daily_by_symbol={"000001": ()},
        market_by_symbol={"000001": "KOSDAQ"},
    )
    event = next(item for item in events if item["session"] == DAY.isoformat())
    assert event["previous_session"] == prior_day.isoformat()
    assert event["previous_close"] == 101
    assert event["prior_close_source"] == "KIS_PREVIOUS_CONTINUOUS_SESSION_15_19_CLOSE_PROXY"
    assert _clean_confidence_events([event]) == []
    assert excluded == {"NO_SAFE_PREVIOUS_SESSION_CLOSE": 1}


def test_raw_daily_jump_during_swing_horizon_is_excluded():
    event = {
        "symbol": "000001", "session": DAY.isoformat(), "current_open": 100,
        "previous_close": 100,
        "longer_horizon_daily_close_return_pct": {},
    }
    daily = {
        "000001": (
            _daily(date(2026, 4, 17), 100),
            _daily(DAY, 100),
            _daily(date(2026, 4, 21), 10),
            _daily(date(2026, 4, 22), 10),
            _daily(date(2026, 4, 23), 10),
        )
    }
    _daily_return_horizons(
        [event], daily,
        [DAY, date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 23)],
    )
    assert event["longer_horizon_daily_close_return_pct"]["NEXT_SESSION_CLOSE"] is None
    assert event["longer_horizon_status"]["NEXT_SESSION_CLOSE"] == "SUSPICIOUS_RAW_PRICE_JUMP_DURING_HORIZON"


def test_suspicious_opening_gap_is_excluded_from_swing_outcomes():
    event = {
        "symbol": "000001", "session": DAY.isoformat(), "current_open": 100,
        "previous_close": 100,
        "suspicious_extreme_gap": True,
        "longer_horizon_daily_close_return_pct": {},
    }
    daily = {
        "000001": (
            _daily(date(2026, 4, 17), 100),
            _daily(DAY, 100),
            _daily(date(2026, 4, 21), 101),
            _daily(date(2026, 4, 22), 102),
            _daily(date(2026, 4, 23), 103),
        )
    }
    _daily_return_horizons(
        [event], daily,
        [DAY, date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 23)],
    )
    assert event["longer_horizon_daily_close_return_pct"]["NEXT_SESSION_CLOSE"] is None
    assert event["longer_horizon_status"]["NEXT_SESSION_CLOSE"] == "SUSPICIOUS_OPENING_GAP_OR_CORPORATE_ACTION"


def test_daily_horizons_do_not_skip_missing_trading_session_rows():
    event = {
        "symbol": "000001", "session": DAY.isoformat(), "current_open": 100,
        "previous_close": 100, "suspicious_extreme_gap": False,
        "longer_horizon_daily_close_return_pct": {},
    }
    daily = {"000001": (
        _daily(DAY, 100),
        _daily(date(2026, 4, 22), 102),
        _daily(date(2026, 4, 24), 104),
        _daily(date(2026, 4, 27), 105),
    )}
    calendar = [DAY, date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 23),
                date(2026, 4, 24), date(2026, 4, 27)]

    _daily_return_horizons([event], daily, calendar)

    assert event["longer_horizon_daily_close_return_pct"]["NEXT_SESSION_CLOSE"] is None
    assert event["longer_horizon_status"]["NEXT_SESSION_CLOSE"] == "SAFE_DAILY_HORIZON_UNAVAILABLE"
    assert event["longer_horizon_daily_close_return_pct"]["THREE_SESSION_CLOSE"] is None
    assert event["longer_horizon_status"]["THREE_SESSION_CLOSE"] == "SAFE_DAILY_HORIZON_UNAVAILABLE"


def test_daily_horizons_use_exact_next_and_third_development_sessions():
    event = {
        "symbol": "000001", "session": DAY.isoformat(), "current_open": 100,
        "previous_close": 100, "suspicious_extreme_gap": False,
        "longer_horizon_daily_close_return_pct": {},
    }
    sessions = [DAY, date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 23)]
    daily = {"000001": tuple(_daily(day, 100 + index) for index, day in enumerate(sessions))}

    _daily_return_horizons([event], daily, sessions)

    assert event["longer_horizon_daily_close_return_pct"]["NEXT_SESSION_CLOSE"] == pytest.approx(1.0)
    assert event["longer_horizon_daily_close_return_pct"]["THREE_SESSION_CLOSE"] == pytest.approx(3.0)


def test_phase6_reconciliation_records_unavailable_original_and_indexed_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    phase6_dir = tmp_path / "runtime" / "research" / "phase6"
    phase6_dir.mkdir(parents=True)
    indexed_manifest = b"synthetic historical manifest bytes unavailable in this fixture\n"
    current_manifest = b"current overwritten manifest\n"
    summary_bytes = json.dumps({"verdicts": {"ALPHA": "UNPROVEN"}}).encode()
    (phase6_dir / "phase6-acquisition-manifest.json").write_bytes(current_manifest)
    (phase6_dir / "phase6-summary.json").write_bytes(summary_bytes)
    index = {
        "files": [
            {"file": "phase6-acquisition-manifest.json",
             "sha256": hashlib.sha256(indexed_manifest).hexdigest(), "bytes": len(indexed_manifest)},
            {"file": "phase6-summary.json",
             "sha256": hashlib.sha256(summary_bytes).hexdigest(), "bytes": len(summary_bytes)},
        ]
    }
    index_path = phase6_dir / "phase6-artifact-index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    index_hash = hashlib.sha256(index_path.read_bytes()).hexdigest()
    summary_hash = hashlib.sha256(summary_bytes).hexdigest()

    record = _phase6_reconciliation(
        phase6_dir=phase6_dir,
        output_path=tmp_path / "runtime" / "research" / "phase8" / "phase6-integrity-reconciliation.json",
    )

    assert record["indexed_manifest"]["bytes"] == len(indexed_manifest)
    assert record["current_manifest"]["bytes"] == len(current_manifest)
    assert record["original_manifest_bytes_available"] is False
    assert record["exact_restoration_claimed"] is False
    assert record["phase6_summary"]["matches_artifact_index"] is True
    assert record["phase6_summary"]["summary_and_verdict_unchanged"] is True
    assert hashlib.sha256(index_path.read_bytes()).hexdigest() == index_hash
    assert hashlib.sha256((phase6_dir / "phase6-summary.json").read_bytes()).hexdigest() == summary_hash


def test_missing_safe_previous_close_excludes_first_development_session():
    events, excluded = build_opening_gap_events(
        minutes_by_symbol_session={("000001", date(2026, 4, 17)): _session(date(2026, 4, 17))},
        daily_by_symbol={"000001": (_daily(date(2026, 4, 17), 100),)},
        market_by_symbol={"000001": "KOSDAQ"},
    )
    assert events == []
    assert excluded == {"NO_SAFE_PREVIOUS_SESSION_CLOSE": 1}


def test_session_and_holdout_guards_and_external_freeze_guard():
    assert_phase8_development_session(date(2026, 4, 17))
    with pytest.raises(ValueError):
        assert_phase8_development_session(date(2026, 7, 1))
    with pytest.raises(ValueError):
        assert_phase8_development_session(date(2026, 7, 28))
    with pytest.raises(ValueError):
        assert_phase8_secondary_allowed(
            development_candidate=False, exact_rule_frozen=False, sessions=[date(2026, 7, 1)]
        )
    with pytest.raises(ValueError):
        assert_phase8_external_allowed(
            candidate_preregistered=False, strategy_freeze_commit=None, sessions=[date(2026, 1, 5)]
        )
    with pytest.raises(ValueError):
        assert_phase8_external_allowed(
            candidate_preregistered=True, strategy_freeze_commit="UNAVAILABLE", sessions=[date(2026, 1, 5)]
        )


def test_phase8_acquisition_regression_keeps_phase5_6_7_manifests_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = tmp_path / "runtime" / "research"
    preserved = {
        runtime / "phase5" / "data-acquisition-manifest.json": b'{"owner":"phase5"}\n',
        runtime / "phase6" / "phase6-acquisition-manifest.json": b'{"owner":"phase6"}\n',
        runtime / "phase7" / "phase7-acquisition-manifest.json": b'{"owner":"phase7"}\n',
    }
    for path, content in preserved.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    hashes_before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in preserved}

    symbols = [f"{number:06d}" for number in range(1, 61)]
    cohort_path = runtime / "phase4" / "cohort-manifest.json"
    cohort_path.parent.mkdir(parents=True)
    cohort_path.write_text(json.dumps({"cohort_60": {"symbols": symbols}}), encoding="utf-8")
    split_path = runtime / "phase25-diagnostic-dataset-manifest-v1.json"
    split_path.write_text(json.dumps({"splits": {
        "development": {"sessions": ["2026-04-17"]},
        "validation": {"sessions": ["2026-07-01"]},
    }}), encoding="utf-8")

    class FakeClient:
        def get_daily_bars(self, symbol, start, end):
            raise AssertionError("Phase 8 acquisition must not read or fetch daily cache data")

        def get_minute_bars(self, symbol, session_date):
            return [_bar(session_date, 0, 100)]

    phase8_path = runtime / "phase8" / "phase8-acquisition-manifest.json"
    state = run_phase8_acquisition(
        cohort_manifest_path=cohort_path, split_manifest_path=split_path,
        cache_root=tmp_path / "data", status_output_path=phase8_path,
        max_new_requests=3, min_request_interval=4.0, client=FakeClient(),
    )
    assert phase8_path.is_file()
    assert state["artifact"] == "phase8-acquisition-manifest"
    assert state["new_requests_attempted"] == 3
    assert set(state["symbol_results"]) == set(symbols)
    assert state["runtime_write_scope"].endswith("daily cache is neither read nor written")
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes_before[path] for path in preserved)
    assert not (runtime / "phase6" / "phase6-acquisition-manifest.json.tmp").exists()


def test_safe_phase8_backfill_skips_a_shared_daily_cache_partition(tmp_path):
    class DailyReadGuardCache(ParquetBarCache):
        def load(self, kind, symbol, interval, session_date=None):
            if kind == "daily":
                raise AssertionError("daily partition was opened")
            return super().load(kind, symbol, interval, session_date)

    class FakeClient:
        daily_requests = 0

        def get_daily_bars(self, symbol, start, end):
            self.daily_requests += 1
            raise AssertionError("Phase 8 must not fetch or overwrite daily cache data")

        def get_minute_bars(self, symbol, session_date):
            return [_bar(session_date, 0, 100)]

    cache = DailyReadGuardCache(tmp_path / "data")
    daily_path = cache.partition_path("daily", "000001", "1d")
    daily_path.parent.mkdir(parents=True)
    daily_path.write_bytes(b"synthetic sealed shared daily cache")
    daily_sidecar = daily_path.with_suffix(".metadata.json")
    daily_sidecar.write_text('{"last_timestamp":"2026-08-28T00:00:00+09:00"}')
    before = (daily_path.read_bytes(), daily_sidecar.read_bytes())
    client = FakeClient()

    result = backfill_symbol_sessions(
        client, "000001", [DAY], cache,
        max_new_requests=1, ensure_daily=False,
    )

    assert result["status"] == "SUCCESS"
    assert result["new_requests_attempted"] == 1
    assert client.daily_requests == 0
    assert (daily_path.read_bytes(), daily_sidecar.read_bytes()) == before


def test_phase8_acquisition_uses_secure_loader_and_ephemeral_token_cache(tmp_path, monkeypatch):
    captured = {}

    class FakeSettings:
        trading_mode = "shadow"
        live_trading_enabled = False
        kis_app_key = "test-key"
        kis_app_secret = "test-secret"

        @classmethod
        def from_env(cls):
            return cls()

    class FakeTokenManager:
        def __init__(self, app_key, app_secret, transport, *, cache_path):
            captured["cache_path"] = cache_path
            captured["key_seen_in_memory"] = app_key
            captured["secret_seen_in_memory"] = app_secret

    class FakeClient:
        pass

    def fake_backfill(**kwargs):
        captured["status_output_path"] = kwargs["status_output_path"]
        captured["ensure_daily"] = kwargs["ensure_daily"]
        return {"artifact": "phase8-acquisition-manifest", "total_sessions": 67,
                "symbol_results": {}, "status": "PARTIAL"}

    monkeypatch.setattr(phase8, "Settings", FakeSettings)
    monkeypatch.setattr(phase8, "TokenManager", FakeTokenManager)
    monkeypatch.setattr(phase8, "UrllibTransport", lambda: object())
    monkeypatch.setattr(phase8, "KisRestClient", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(phase8, "run_phase5_backfill", fake_backfill)
    output = tmp_path / "runtime" / "research" / "phase8" / "phase8-acquisition-manifest.json"

    phase8.run_phase8_acquisition(status_output_path=output, max_new_requests=0)

    assert captured["cache_path"] is None
    assert captured["status_output_path"] == output
    assert captured["ensure_daily"] is False
    assert not (tmp_path / "runtime" / "kis_token.json").exists()
    persisted = output.read_text(encoding="utf-8")
    assert "test-key" not in persisted
    assert "test-secret" not in persisted


def test_development_dataset_loader_never_reads_secondary_or_holdout_partitions(tmp_path):
    # Invalid dates in the Development split fail before any cache partition can be opened.
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(json.dumps({"cohort_60": {"symbols": [f"{number:06d}" for number in range(60)]}}))
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps({"splits": {"development": {"sessions": ["2026-07-28"]}}}))
    with pytest.raises(ValueError):
        load_phase8_development_dataset(
            cohort_manifest_path=cohort_path, split_manifest_path=split_path,
            acquisition_manifest_path=tmp_path / "missing.json", cache_root=tmp_path / "data",
        )


def test_secondary_dataset_loader_accepts_only_safe_secondary_sessions(tmp_path):
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(json.dumps({"cohort_60": {
        "symbols": [f"{number:06d}" for number in range(60)],
    }}))
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps({"splits": {
        "development": {"sessions": ["2026-06-30"]},
        "validation": {"sessions": ["2026-07-01"]},
    }}))

    dataset, _ = phase8.load_phase8_period_dataset(
        period_name="secondary", cohort_manifest_path=cohort_path,
        split_manifest_path=split_path,
        acquisition_manifest_path=tmp_path / "missing.json", cache_root=tmp_path / "data",
    )

    assert dataset.period_name == "secondary"
    assert dataset.period_sessions == (date(2026, 7, 1),)
    assert dataset.dq["analysis_secondary_partitions_opened"] == 0

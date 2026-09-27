from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from krx_trader.config import Settings
from krx_trader.execution.realtime_shadow import RealtimeShadowRunner, scheduled_market_window
from krx_trader.kis.rest import KisApiError
from krx_trader.market.regime import Regime
from krx_trader.models import Bar

KST = ZoneInfo("Asia/Seoul")


class FakeClient:
    def __init__(self, bars: list[Bar] | None = None) -> None:
        self.bars = bars if bars is not None else [minute(value) for value in range(5)]
        self.index_calls = 0
        self.fail_first_index_call = False

    def get_minute_bars(self, _symbol, _session):
        return self.bars

    def get_index_bars(self, _index_code, _start, _end):
        self.index_calls += 1
        if self.fail_first_index_call and self.index_calls == 1:
            raise KisApiError("temporary index API failure")
        return [Bar(datetime(2026, 9, 25, 9, tzinfo=KST), 100, 102, 99, 101, 1_000)]


def make_runner(tmp_path, settings: Settings | None = None) -> RealtimeShadowRunner:
    start = datetime(2026, 9, 28, 9, tzinfo=KST)
    return RealtimeShadowRunner(
        settings or Settings(),
        FakeClient(),  # type: ignore[arg-type]
        run_id="unit-shadow",
        scheduled_start=start,
        stop_at=datetime(2026, 9, 28, 13, tzinfo=KST),
        root=tmp_path,
        scanner=lambda *_args, **_kwargs: ([], 0),
        index_bars={"kospi": [], "kosdaq": []},
        clock=lambda: datetime(2026, 9, 28, 9, 5, tzinfo=KST),
    )


def minute(minute: int, *, open_: float = 1_000, high: float = 1_001, low: float = 1_000, close: float = 1_000) -> Bar:
    return Bar(datetime(2026, 9, 28, 9, minute, tzinfo=KST), open_, high, low, close, 1_000)


def test_realtime_shadow_fails_closed_outside_shadow_mode(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires TRADING_MODE=shadow"):
        make_runner(tmp_path, Settings(trading_mode="live"))
    with pytest.raises(ValueError, match="requires TRADING_MODE=shadow"):
        make_runner(tmp_path, Settings(live_trading_enabled=True))


def test_pending_entry_waits_for_minute_after_decision_observation(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._state["pending"].append({
        "signal_id": "signal-1",
        "symbol": "005930",
        "strategy": "breakout",
        "interval": 15,
        "signal_time": "2026-09-28T09:00:00+09:00",
        "earliest_fill_time": "2026-09-28T09:03:00+09:00",
        "signal_high": 1_005,
        "stop_price": 999,
        "max_holding_bars": 10,
        "scanner_rank": 1,
        "regime": "UP",
        "snapshot_id": "snapshot-1",
    })

    runner._resolve_pending({"005930": [minute(2)]}, observed_at=datetime(2026, 9, 28, 9, 5, tzinfo=KST))
    assert len(runner._state["pending"]) == 1
    assert not runner._state["profiles"]["CURRENT_LIVE_LIKE"]["positions"]

    runner._resolve_pending(
        {"005930": [minute(2), minute(3, high=1_003)]},
        observed_at=datetime(2026, 9, 28, 9, 5, tzinfo=KST),
    )
    profile = runner._state["profiles"]["CURRENT_LIVE_LIKE"]
    assert not runner._state["pending"]
    assert len(profile["positions"]) == 1
    assert profile["positions"][0]["entry_time"] == "2026-09-28T09:03:00+09:00"
    event = json.loads(runner.events_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event_type"] == "SIMULATED_ENTRY_FILL"
    assert event["status"] == "SIMULATED"
    assert event["order_api_calls"] == 0


def test_pending_entry_after_shadow_cutoff_is_not_filled(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._state["pending"].append({
        "signal_id": "after-cutoff",
        "symbol": "005930",
        "strategy": "breakout",
        "interval": 15,
        "signal_time": "2026-09-28T12:45:00+09:00",
        "earliest_fill_time": "2026-09-28T13:00:00+09:00",
        "signal_high": 1_005,
        "stop_price": 999,
        "max_holding_bars": 10,
        "scanner_rank": 1,
        "regime": "UP",
        "snapshot_id": "after-cutoff",
    })

    runner._resolve_pending(
        {"005930": [
            Bar(datetime(2026, 9, 28, 13, 0, tzinfo=KST), 1_000, 1_005, 999, 1_002, 100),
        ]},
        observed_at=datetime(2026, 9, 28, 13, 1, tzinfo=KST),
    )

    assert runner._state["pending"]
    assert not runner._state["profiles"]["CURRENT_LIVE_LIKE"]["positions"]
    runner._finish("STOP_TIME_REACHED")
    event = next(
        json.loads(line) for line in runner.events_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event_type"] == "SIMULATED_ORDER_EXPIRED"
    )
    assert event["event_type"] == "SIMULATED_ORDER_EXPIRED"
    assert event["reason"] == "SHADOW_WINDOW_STOP"


def test_strategy_decisions_observed_after_cutoff_create_no_intent(tmp_path) -> None:
    runner = make_runner(tmp_path)
    start = datetime(2026, 9, 28, 14, 15, tzinfo=KST)
    completed = [
        Bar(start + timedelta(minutes=index * 15), 1_000, 1_005, 995, 1_000, 100)
        for index in range(20)
    ]
    completed.append(Bar(datetime(2026, 9, 28, 14, 15, tzinfo=KST), 1_000, 1_012, 1_008, 1_010, 1_000))

    runner._decision(
        "005930", 15, "breakout", completed, Regime.UP,
        datetime(2026, 9, 28, 13, 1, tzinfo=KST),
    )

    events = [json.loads(line) for line in runner.events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events] == ["DECISION_SKIPPED_AFTER_STOP"]


def test_delayed_market_poll_never_collects_bars_after_cutoff(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner.client.bars = [
        Bar(datetime(2026, 9, 28, 9, tzinfo=KST) + timedelta(minutes=index), 1_000, 1_001, 999, 1_000, 100)
        for index in range(245)
    ]  # type: ignore[attr-defined]
    runner.clock = lambda: datetime(2026, 9, 28, 13, 5, tzinfo=KST)

    runner.cycle(datetime(2026, 9, 28, 12, 59, tzinfo=KST))

    stored = [json.loads(line) for line in runner.bars_path.read_text(encoding="utf-8").splitlines()]
    assert len(stored) == 240
    assert stored[-1]["timestamp"] == "2026-09-28T12:59:00+09:00"
    assert all(datetime.fromisoformat(row["timestamp"]) < datetime(2026, 9, 28, 13, tzinfo=KST) for row in stored)


def test_max_holding_exit_uses_next_observed_minute_open(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._state["pending"].append({
        "signal_id": "time-exit-entry",
        "symbol": "005930",
        "strategy": "breakout",
        "interval": 15,
        "signal_time": "2026-09-28T09:00:00+09:00",
        "earliest_fill_time": "2026-09-28T09:03:00+09:00",
        "signal_high": 1_005,
        "stop_price": 900,
        "max_holding_bars": 1,
        "scanner_rank": 1,
        "regime": "UP",
        "snapshot_id": "time-exit",
    })
    runner._resolve_pending(
        {"005930": [Bar(datetime(2026, 9, 28, 9, 3, tzinfo=KST), 1_000, 1_005, 999, 1_002, 100)]},
        observed_at=datetime(2026, 9, 28, 9, 5, tzinfo=KST),
    )
    position = runner._state["profiles"]["CURRENT_LIVE_LIKE"]["positions"][0]
    strategy_bar = Bar(datetime(2026, 9, 28, 9, 15, tzinfo=KST), 1_000, 1_010, 995, 1_005, 10_000)

    runner._manage_strategy_bar(
        "005930", 15, "breakout", strategy_bar, datetime(2026, 9, 28, 9, 16, tzinfo=KST)
    )
    assert position["exit_pending_at"] == "2026-09-28T09:17:00+09:00"
    runner._manage_minute("005930", Bar(datetime(2026, 9, 28, 9, 16, tzinfo=KST), 1_006, 1_008, 1_004, 1_007, 100))
    assert runner._state["profiles"]["CURRENT_LIVE_LIKE"]["positions"]
    runner._manage_minute("005930", Bar(datetime(2026, 9, 28, 9, 17, tzinfo=KST), 1_007, 1_010, 1_006, 1_009, 100))
    assert not runner._state["profiles"]["CURRENT_LIVE_LIKE"]["positions"]
    exit_event = next(
        json.loads(line) for line in runner.events_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event_type"] == "SIMULATED_EXIT_FILL"
    )
    assert exit_event["exit_reason"] == "MAX_HOLD_TIME_NEXT_OPEN"
    assert exit_event["reference_price"] == 1_007
    assert exit_event["order_api_calls"] == 0


def test_late_next_bar_after_process_downtime_expires_without_fill(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._state["pending"].append({
        "signal_id": "missed-next-minute",
        "symbol": "005930",
        "strategy": "breakout",
        "interval": 15,
        "signal_time": "2026-09-28T09:00:00+09:00",
        "decision_observed_at": "2026-09-28T09:02:00+09:00",
        "earliest_fill_time": "2026-09-28T09:03:00+09:00",
        "signal_high": 1_005,
        "stop_price": 999,
        "max_holding_bars": 10,
        "scanner_rank": 1,
        "regime": "UP",
        "snapshot_id": "snapshot-late",
    })

    runner._resolve_pending(
        {"005930": [minute(3, high=1_003)]},
        observed_at=datetime(2026, 9, 28, 9, 10, tzinfo=KST),
    )

    assert not runner._state["pending"]
    assert not runner._state["profiles"]["CURRENT_LIVE_LIKE"]["positions"]
    event = json.loads(runner.events_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event_type"] == "SIMULATED_ORDER_EXPIRED"
    assert event["reason"] == "NEXT_BAR_DATA_OBSERVED_TOO_LATE"


def test_duplicate_pending_signal_id_creates_one_simulated_fill(tmp_path) -> None:
    runner = make_runner(tmp_path)
    intent = {
        "signal_id": "duplicate-signal",
        "symbol": "005930",
        "strategy": "breakout",
        "interval": 15,
        "signal_time": "2026-09-28T09:00:00+09:00",
        "decision_observed_at": "2026-09-28T09:02:00+09:00",
        "earliest_fill_time": "2026-09-28T09:03:00+09:00",
        "signal_high": 1_005,
        "stop_price": 999,
        "max_holding_bars": 10,
        "scanner_rank": 1,
        "regime": "UP",
        "snapshot_id": "snapshot-duplicate",
    }
    runner._state["pending"].extend([dict(intent), dict(intent)])

    runner._resolve_pending(
        {"005930": [minute(3, high=1_003)]},
        observed_at=datetime(2026, 9, 28, 9, 5, tzinfo=KST),
    )

    profile = runner._state["profiles"]["CURRENT_LIVE_LIKE"]
    assert len(profile["positions"]) == 1
    events = [json.loads(line) for line in runner.events_path.read_text(encoding="utf-8").splitlines()]
    assert sum(event["event_type"] == "SIMULATED_ENTRY_FILL" for event in events) == 1


def test_entry_slippage_above_next_bar_high_does_not_fill(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._state["pending"].append({
        "signal_id": "impossible-fill",
        "symbol": "005930",
        "strategy": "breakout",
        "interval": 15,
        "signal_time": "2026-09-28T09:00:00+09:00",
        "decision_observed_at": "2026-09-28T09:02:00+09:00",
        "earliest_fill_time": "2026-09-28T09:03:00+09:00",
        "signal_high": 1_005,
        "stop_price": 999,
        "max_holding_bars": 10,
        "scanner_rank": 1,
        "regime": "UP",
        "snapshot_id": "snapshot-impossible",
    })

    runner._resolve_pending(
        {"005930": [minute(3)]},
        observed_at=datetime(2026, 9, 28, 9, 5, tzinfo=KST),
    )

    assert not runner._state["profiles"]["CURRENT_LIVE_LIKE"]["positions"]
    event = json.loads(runner.events_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event_type"] == "SIMULATED_ORDER_NOT_FILLED"
    assert event["reason"] == "ENTRY_SLIPPAGE_ABOVE_NEXT_BAR_HIGH"


def test_market_like_next_open_fill_allows_gap_above_signal_bar_high(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._state["pending"].append({
        "signal_id": "gap-open-fill",
        "symbol": "005930",
        "strategy": "breakout",
        "interval": 15,
        "signal_time": "2026-09-28T09:00:00+09:00",
        "decision_observed_at": "2026-09-28T09:02:00+09:00",
        "earliest_fill_time": "2026-09-28T09:03:00+09:00",
        "signal_high": 1_005,
        "stop_price": 999,
        "max_holding_bars": 10,
        "scanner_rank": 1,
        "regime": "UP",
        "snapshot_id": "gap-open",
    })

    runner._resolve_pending(
        {"005930": [minute(3, open_=1_006, high=1_010, low=1_005, close=1_008)]},
        observed_at=datetime(2026, 9, 28, 9, 5, tzinfo=KST),
    )

    profile = runner._state["profiles"]["CURRENT_LIVE_LIKE"]
    assert len(profile["positions"]) == 1
    events = [json.loads(line) for line in runner.events_path.read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "SIMULATED_ENTRY_FILL" for event in events)


def test_replay_inputs_are_persisted_and_hashed(tmp_path) -> None:
    runner = make_runner(tmp_path)
    assert len(runner._manifest["config_sha256"]) == 64
    assert runner._manifest["strategy_config"]["breakout"]
    assert runner._manifest["scanner_config"]["candidate_top_n"] == 20
    assert runner._manifest["scanner_config"]["deep_monitor_top_n"] == 5
    assert runner._manifest["scanner_config"]["effective_max_price_krw"] == Settings().effective_max_price_krw
    index = Bar(datetime(2026, 9, 25, 9, tzinfo=KST), 100, 102, 99, 101, 1_000)
    warmup = Bar(datetime(2026, 9, 25, 9, tzinfo=KST), 1_000, 1_002, 999, 1_001, 2_000)
    runner.index_bars = {"kospi": [index], "kosdaq": [index]}
    runner._persist_index_snapshot()
    runner._persist_warmup_bar("005930", warmup)
    runner._index_latest_hash()

    assert json.loads(runner.index_path.read_text(encoding="utf-8"))["kospi"][0]["close"] == 101
    row = json.loads(runner.warmup_bars_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["data_type"] == "historical_warmup"
    assert row["timestamp"] == warmup.time.isoformat()
    assert runner._manifest["warmup_bars_count"] == 1
    assert runner._manifest["warmup_bars_sha256"]
    assert runner._manifest["index_snapshot_sha256"]


def test_scanner_failure_clears_previous_candidate_membership(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._monitor_symbols = ["001440"]
    runner._candidate_rank = {"001440": 1}

    def fail_scanner(*_args, **_kwargs):
        raise KisApiError("read-only market rank failed")

    runner.scanner = fail_scanner
    runner._scan(datetime(2026, 9, 28, 9, 30, tzinfo=KST))

    assert runner._monitor_symbols == []
    assert runner._candidate_rank == {}
    event = json.loads(runner.events_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event_type"] == "SCANNER_FAILURE"


def test_scanner_response_after_cutoff_is_not_accepted(tmp_path) -> None:
    runner = make_runner(tmp_path)
    clock = [datetime(2026, 9, 28, 12, 59, tzinfo=KST)]
    runner.clock = lambda: clock[0]
    runner._monitor_symbols = ["001440"]
    runner._candidate_rank = {"001440": 1}

    def late_scan(*_args, **_kwargs):
        clock[0] = datetime(2026, 9, 28, 13, 1, tzinfo=KST)
        return ([object()], 0)

    runner.scanner = late_scan
    runner._scan(datetime(2026, 9, 28, 12, 59, tzinfo=KST))

    assert runner._monitor_symbols == []
    assert runner._candidate_rank == {}
    event = json.loads(runner.events_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event_type"] == "SCANNER_SKIPPED_AFTER_STOP"
    assert event["observed_at"] == "2026-09-28T13:01:00+09:00"


def test_index_refresh_retries_transient_failure_after_cooldown(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner.index_bars = {}
    runner._index_refresh_done = False
    runner.client.fail_first_index_call = True  # type: ignore[attr-defined]
    clock = [datetime(2026, 9, 28, 9, 5, tzinfo=KST)]
    runner.clock = lambda: clock[0]

    runner._refresh_indexes(clock[0].date())
    assert runner.client.index_calls == 1  # type: ignore[attr-defined]
    assert not runner._index_refresh_done

    clock[0] += timedelta(minutes=4)
    runner._refresh_indexes(clock[0].date())
    assert runner.client.index_calls == 1  # type: ignore[attr-defined]

    clock[0] += timedelta(minutes=1)
    runner._refresh_indexes(clock[0].date())
    assert runner.client.index_calls == 3  # type: ignore[attr-defined]
    assert runner._index_refresh_done
    assert runner.index_bars["kospi"] and runner.index_bars["kosdaq"]
    assert runner._manifest["index_refresh_failures"] == 1


def test_stop_expires_pending_intents_without_filling(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._state["pending"].append({
        "signal_id": "after-stop",
        "symbol": "005930",
        "strategy": "breakout",
        "interval": 15,
        "signal_time": "2026-09-28T12:45:00+09:00",
        "earliest_fill_time": "2026-09-28T13:00:00+09:00",
    })

    runner._finish("STOP_TIME_REACHED")

    assert runner._state["pending"] == []
    event = json.loads(runner.events_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "SIMULATED_ORDER_EXPIRED"
    assert event["reason"] == "SHADOW_WINDOW_STOP"


def test_empty_scanner_does_not_fallback_into_strategy_monitoring(tmp_path) -> None:
    runner = make_runner(tmp_path)

    runner.cycle(datetime(2026, 9, 28, 9, 5, tzinfo=KST))

    assert runner._monitor_symbols == []
    assert runner._candidate_rank == {}
    events = [json.loads(line) for line in runner.events_path.read_text(encoding="utf-8").splitlines()]
    assert not [event for event in events if event["event_type"] == "DECISION"]


def test_missing_regular_minutes_prevent_full_pass(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner.client.bars = [minute(0), minute(2), minute(4)]  # type: ignore[attr-defined]

    runner.cycle(datetime(2026, 9, 28, 9, 5, tzinfo=KST))
    runner._finish("STOP_TIME_REACHED")

    assert runner._manifest["missing_minute_count"] == 2
    assert runner._manifest["status"] == "PARTIAL"


def test_shared_breakout_evaluator_creates_only_a_simulated_pending_entry(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner.stop_at = datetime(2026, 9, 28, 16, tzinfo=KST)
    runner._candidate_rank["005930"] = 1
    start = datetime(2026, 9, 28, 9, 15, tzinfo=KST)
    completed = [
        Bar(start + timedelta(minutes=index * 15), 1_000, 1_005, 995, 1_000, 100)
        for index in range(20)
    ]
    completed.append(Bar(datetime(2026, 9, 28, 14, 15, tzinfo=KST), 1_000, 1_012, 1_008, 1_010, 1_000))

    runner._decision(
        "005930",
        15,
        "breakout",
        completed,
        Regime.UP,
        datetime(2026, 9, 28, 14, 16, tzinfo=KST),
    )

    events = [json.loads(line) for line in runner.events_path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["event_type"] == "DECISION"
    assert events[0]["decision"] == "ENTER"
    assert events[0]["scanner_rank"] == 1
    assert events[1]["event_type"] == "SIMULATED_ORDER_INTENT"
    assert events[1]["status"] == "SIMULATED_PENDING_NEXT_EXECUTABLE_BAR"
    assert runner._state["pending"][0]["earliest_fill_time"] == "2026-09-28T14:17:00+09:00"
    assert not runner._state["profiles"]["CURRENT_LIVE_LIKE"]["positions"]


def test_shadow_event_append_is_idempotent(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._event("CHECKPOINT", "same-key", {"safe": True})
    runner._event("CHECKPOINT", "same-key", {"safe": True})
    assert len(runner.events_path.read_text(encoding="utf-8").splitlines()) == 1


def test_shadow_resume_refuses_a_malformed_audit_tail(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner._event("CHECKPOINT", "valid", {"safe": True})
    with runner.events_path.open("a", encoding="utf-8") as stream:
        stream.write("{\"truncated\":")
    with pytest.raises(ValueError, match="malformed line"):
        make_runner(tmp_path)


def test_scheduled_window_is_kst_and_ordered() -> None:
    start, stop = scheduled_market_window(datetime(2026, 9, 28, tzinfo=KST).date())
    assert start.isoformat() == "2026-09-28T09:00:00+09:00"
    assert stop.isoformat() == "2026-09-28T13:00:00+09:00"
    with pytest.raises(ValueError, match="ordered"):
        scheduled_market_window(start.date(), "13:00", "09:00")

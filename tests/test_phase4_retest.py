import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.engine import run_backtest
from krx_trader.models import Bar, Decision
from krx_trader.research.phase4_study import (
    _near_gap,
    _one_share_execution,
    canonical_sha256,
    inspect_frozen_block_cache,
    validate_external_dates,
    write_external_validation_unavailable,
    write_preregistration_manifest,
)
from krx_trader.strategies.retest import (
    BreakoutRetestConfig,
    BreakoutRetestMachine,
    RetestVariant,
    SetupState,
    evaluate_breakout_retest,
)

KST = ZoneInfo("Asia/Seoul")


def _retest_bars(*rows: tuple[float, float, float, float, int]) -> list[Bar]:
    start = datetime(2026, 6, 30, 9, 0, tzinfo=KST)
    return [
        Bar(start + timedelta(minutes=15 * index), open_, high, low, close, volume)
        for index, (open_, high, low, close, volume) in enumerate(rows)
    ]


def _small_retest_config() -> BreakoutRetestConfig:
    return BreakoutRetestConfig(
        lookback=2, volume_lookback=2, volume_multiple=1.5,
        max_retest_bars=3, retest_tolerance_pct=0.003,
        stop_buffer_pct=0.003, max_holding_bars=10,
    )


def test_breakout_setup_is_created_on_close_without_same_bar_entry():
    bars = _retest_bars(
        (100, 102, 99, 101, 100),
        (101, 103, 100, 102, 100),
        (103, 108, 102, 107, 200),
    )
    machine = BreakoutRetestMachine(RetestVariant.RETEST_A, _small_retest_config())
    signal = machine.update(bars[:1], "005930", "15m")
    signal = machine.update(bars[:2], "005930", "15m")
    signal = machine.update(bars, "005930", "15m")
    assert signal.decision == Decision.HOLD
    assert machine.state == SetupState.WAITING_RETEST
    assert machine.setups[0].breakout_level == 103
    assert machine.setups[0].transitions == ["BREAKOUT_DETECTED", "WAITING_RETEST"]


def test_retest_a_accepts_held_level_and_fills_only_on_next_bar_open():
    bars = _retest_bars(
        (100, 102, 99, 101, 100),
        (101, 103, 100, 102, 100),
        (103, 108, 102, 107, 200),
        (104, 105, 102.8, 104, 150),
        (104, 106, 99, 100, 180),
    )
    config = _small_retest_config()
    signal = evaluate_breakout_retest(bars[:4], "005930", "15m", RetestVariant.RETEST_A, config=config)
    assert signal.decision == Decision.ENTER
    assert signal.timestamp == bars[3].time
    assert signal.stop_price < min(103, 102.8)

    result = run_backtest(
        bars,
        lambda history: evaluate_breakout_retest(history, "005930", "15m", RetestVariant.RETEST_A,
                                                 config=config),
        symbol="005930", starting_cash_krw=100_000, capital_cap_krw=100_000,
        order_cap_krw=20_000, risk_per_trade_pct=0.25, min_price_krw=1,
        max_price_krw=50_000, cost_model=CostModel(0.00015, 0.002, 15),
    )
    assert result.trades
    assert result.trades[0].entry_time == bars[4].time
    assert result.trades[0].entry_signal_time == bars[3].time
    assert result.trades[0].entry_reference_price == bars[4].open
    assert result.trades[0].exit_reason == "STOP"


def test_retest_b_waits_for_later_reexpansion_close_above_retest_high():
    bars = _retest_bars(
        (100, 102, 99, 101, 100),
        (101, 103, 100, 102, 100),
        (103, 108, 102, 107, 200),
        (104, 105, 102.8, 104, 150),
        (104, 106, 103.2, 105.2, 160),
    )
    config = _small_retest_config()
    machine = BreakoutRetestMachine(RetestVariant.RETEST_B, config)
    for index in range(4):
        signal = machine.update(bars[:index + 1], "005930", "15m")
    assert signal.decision == Decision.HOLD
    assert machine.state == SetupState.RETEST_OBSERVED
    signal = machine.update(bars, "005930", "15m")
    assert signal.decision == Decision.ENTER
    assert signal.timestamp == bars[4].time
    assert machine.state == SetupState.ENTER_NEXT_BAR
    assert machine.setups[0].transitions[-3:] == [
        "RETEST_OBSERVED", "ACCEPTANCE_CONFIRMED", "ENTER_NEXT_BAR",
    ]


def test_retest_invalidates_close_below_level_and_expires_after_three_bars():
    invalid_bars = _retest_bars(
        (100, 102, 99, 101, 100),
        (101, 103, 100, 102, 100),
        (103, 108, 102, 107, 200),
        (102, 103, 100, 102, 140),
    )
    machine = BreakoutRetestMachine(RetestVariant.RETEST_A, _small_retest_config())
    for index in range(len(invalid_bars)):
        signal = machine.update(invalid_bars[:index + 1], "005930", "15m")
    assert signal.decision == Decision.HOLD
    assert machine.setups[0].state == SetupState.INVALIDATED
    assert machine.setups[0].terminal_reason == "CLOSE_AT_OR_BELOW_BREAKOUT_LEVEL"

    expiry_bars = _retest_bars(
        (100, 102, 99, 101, 100),
        (101, 103, 100, 102, 100),
        (103, 108, 102, 107, 200),
        (104, 106, 104, 105, 130),
        (105, 107, 104, 106, 130),
        (106, 107, 104, 105, 130),
        (105, 107, 104, 106, 130),
    )
    machine = BreakoutRetestMachine(RetestVariant.RETEST_A, _small_retest_config())
    for index in range(len(expiry_bars)):
        signal = machine.update(expiry_bars[:index + 1], "005930", "15m")
    assert signal.decision == Decision.HOLD
    assert machine.setups[0].state == SetupState.EXPIRED
    assert machine.setups[0].terminal_reason == "RETEST_TIMEOUT"


def test_deep_retest_penetration_invalidates_and_duplicate_updates_do_not_reenter():
    bars = _retest_bars(
        (100, 102, 99, 101, 100),
        (101, 103, 100, 102, 100),
        (103, 108, 102, 107, 200),
        (104, 105, 102, 104, 150),
    )
    machine = BreakoutRetestMachine(RetestVariant.RETEST_A, _small_retest_config())
    for index in range(len(bars)):
        signal = machine.update(bars[:index + 1], "005930", "15m")
    assert signal.decision == Decision.HOLD
    assert machine.setups[0].state == SetupState.INVALIDATED
    assert machine.setups[0].terminal_reason == "RETEST_PENETRATION_TOO_DEEP"
    assert machine.setups[0].retest_attempt_time == bars[-1].time.isoformat()
    assert machine.update(bars, "005930", "15m").decision == Decision.HOLD
    assert len(machine.setups) == 1


def test_randomized_future_bars_cannot_change_earlier_retest_decisions():
    rng = random.Random(81)
    prefix = _retest_bars(
        (100, 102, 99, 101, 100),
        (101, 103, 100, 102, 100),
        (103, 108, 102, 107, 200),
        (104, 105, 102.8, 104, 150),
    )
    changed = list(prefix)
    price = prefix[-1].close
    for index in range(12):
        open_ = price
        close = open_ * (1 + rng.uniform(-0.01, 0.01))
        high = max(open_, close) * (1 + rng.uniform(0, 0.01))
        low = min(open_, close) * (1 - rng.uniform(0, 0.01))
        future = Bar(prefix[-1].time + timedelta(minutes=15 * (index + 1)), open_, high, low, close, 100)
        changed.append(future)
        price = close
    for variant in RetestVariant:
        for end in range(1, len(prefix) + 1):
            baseline = evaluate_breakout_retest(prefix[:end], "005930", "15m", variant,
                                                config=_small_retest_config())
            with_future = evaluate_breakout_retest(changed[:end], "005930", "15m", variant,
                                                   config=_small_retest_config())
            assert baseline == with_future


def test_event_execution_uses_next_bar_and_reports_post_entry_excursions():
    bars = _retest_bars(
        (10_000, 10_200, 9_900, 10_100, 100),
        (10_100, 10_300, 10_000, 10_200, 100),
        (10_300, 10_800, 10_200, 10_700, 200),
        (10_400, 10_500, 10_280, 10_400, 150),
        (10_500, 10_700, 10_400, 10_600, 160),
        (10_600, 10_800, 10_400, 10_700, 160),
        (10_600, 10_600, 10_100, 10_200, 170),
    )
    result = _one_share_execution(
        bars, 4, signal_close=10_600, stop_price=10_220, max_holding_bars=10,
    )
    assert result["entry_time"] == bars[5].time.isoformat()
    assert result["entry_reference"] == bars[5].open
    assert result["exit_reason"] == "STOP"
    assert result["time_to_failure_bars"] == 1
    assert result["time_to_mfe_bars"] == 0
    assert result["mfe_pct"] > 0
    assert result["mae_pct"] < 0
    assert result["gross_pnl_krw"] < 0
    assert result["net_pnl_krw"] < result["gross_pnl_krw"]


def test_event_execution_respects_slippage_limit_even_when_entry_bar_touches_stop():
    bars = _retest_bars(
        (10_000, 10_100, 9_900, 10_050, 100),
        (10_000, 10_010, 9_950, 9_980, 100),
        (9_980, 10_000, 9_900, 9_950, 100),
    )
    unfilled = _one_share_execution(
        bars, 0, signal_close=10_050, stop_price=9_900, max_holding_bars=10,
    )
    assert unfilled["status"] == "UNFILLED_SLIPPAGE_ABOVE_NEXT_BAR_HIGH"

    stopped = _one_share_execution(
        bars, 0, signal_close=10_050, stop_price=9_975, max_holding_bars=10,
    )
    assert stopped["status"] == "UNFILLED_SLIPPAGE_ABOVE_NEXT_BAR_HIGH"

    stop_invalid = _one_share_execution(
        bars, 0, signal_close=10_050, stop_price=10_020, max_holding_bars=10,
    )
    assert stop_invalid["status"] == "UNFILLED_ENTRY_NOT_ABOVE_STOP"


def test_gap_sensitivity_marks_interval_holes_and_external_dates_guard_holdout():
    bars = _retest_bars(
        (100, 102, 99, 101, 100),
        (101, 103, 100, 102, 100),
        (103, 108, 102, 107, 200),
        (104, 105, 102.8, 104, 150),
    )
    assert not _near_gap(bars, 0, 3, 15, set(), "005930")
    bars[3] = Bar(bars[3].time + timedelta(minutes=15), 104, 105, 102.8, 104, 150)
    assert _near_gap(bars, 0, 3, 15, set(), "005930")
    validate_external_dates(date(2026, 1, 5), date(2026, 4, 16))
    with pytest.raises(ValueError, match="locked Fresh Holdout"):
        validate_external_dates(date(2026, 7, 27), date(2026, 7, 29))


def test_preregistration_hash_is_canonical_and_sensitive_to_rules():
    original = {"variant": "RETEST-A", "tolerance_pct": 0.003, "expiry": 3}
    assert canonical_sha256(original) == canonical_sha256({"expiry": 3, "tolerance_pct": .003,
                                                          "variant": "RETEST-A"})
    assert canonical_sha256(original) != canonical_sha256({**original, "expiry": 2})


def test_preregistration_precedes_external_cache_inspection_and_unavailable_is_fail_closed(tmp_path: Path):
    symbols = [f"{index:06d}" for index in range(100)]
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(json.dumps({
        "cohort_100": {"symbols": symbols},
        "survivorship_bias": "CURRENT-LISTING COHORT",
    }), encoding="utf-8")
    phase3_path = tmp_path / "phase3.json"
    phase3_path.write_text(json.dumps({
        "splits": {
            "development": {"sessions": ["2026-04-17", "2026-06-30"]},
            "validation": {"sessions": ["2026-07-01", "2026-07-27"]},
        },
    }), encoding="utf-8")
    prereg_path = tmp_path / "retest-preregistration.json"
    manifest = write_preregistration_manifest(
        freeze_commit_sha="a" * 40, cohort_manifest_path=cohort_path,
        phase3_manifest_path=phase3_path, output_path=prereg_path,
    )
    assert manifest["status"] == "FROZEN_BEFORE_NEW_EVIDENCE"
    assert manifest["used_design_data"]["fresh_holdout"] == "LOCKED_NOT_EVALUATED"
    assert manifest["new_external_block"]["start_date_inclusive"] == "2026-01-05"

    cache_root = tmp_path / "cache"
    cached = cache_root / "minute" / symbols[0] / "2026-01-05.parquet"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"partition bytes are not opened")
    cache_audit = inspect_frozen_block_cache(
        cache_root=cache_root, symbols=symbols, start=date(2026, 1, 5), end=date(2026, 4, 16),
    )
    assert cache_audit["existing_partition_count"] == 1
    assert cache_audit["files_read"] == 0
    assert cache_audit["holdout_paths_checked"] == 0

    external_path = tmp_path / "external-validation.json"
    report = write_external_validation_unavailable(
        preregistration_path=prereg_path, output_path=external_path,
        cache_audit=cache_audit, credentials_available=False,
    )
    assert report["status"] == "NOT_AVAILABLE"
    assert report["verdict"] == "NEED_PROSPECTIVE_EVIDENCE"
    assert report["external_fetch_attempted"] is False
    assert report["dotenv_read"] is False
    assert report["fresh_holdout"]["partition_files_opened"] == 0
    assert "1 candidate partition files" in report["reason"]

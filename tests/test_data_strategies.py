from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import make_bar

from krx_trader.data.quality import DataQualityError, inspect_bars, is_stale, require_healthy_bars
from krx_trader.data.resample import resample_session_minutes
from krx_trader.market.regime import Regime, classify_regime
from krx_trader.models import Decision
from krx_trader.strategies.breakout import BreakoutConfig, evaluate_breakout
from krx_trader.strategies.pullback import PullbackConfig, evaluate_pullback


def test_data_quality_rejects_duplicates_and_out_of_order():
    first = make_bar(0)
    second = make_bar(1)
    issues = inspect_bars([first, second, first])
    assert {issue.code for issue in issues} == {"DUPLICATE_TIMESTAMP", "OUT_OF_ORDER"}
    with pytest.raises(DataQualityError):
        require_healthy_bars([first, second, first])


def test_data_quality_flags_impossible_ohlc():
    invalid = make_bar(0, low=110, high=105, close=111)
    assert {issue.code for issue in inspect_bars([invalid])} >= {"HIGH_BELOW_LOW", "CLOSE_OUTSIDE_RANGE"}


def test_stale_feed_uses_timezone_aware_market_time():
    now = make_bar(2).time
    assert is_stale(now - timedelta(seconds=31), now=now)
    assert is_stale(now.replace(tzinfo=None), now=now)


def test_resample_uses_session_open_and_drops_incomplete_bucket():
    bars = [make_bar(index, open_=100 + index) for index in range(15)]
    bars += [make_bar(index + 15, open_=200 + index) for index in range(14)]
    result = resample_session_minutes(bars, 15)
    assert len(result) == 1
    assert result[0].time.hour == 9 and result[0].time.minute == 15
    assert result[0].open == 100
    assert result[0].close == 115
    assert result[0].high == 116
    assert result[0].volume == 1500
    utc_bars = [replace(bar, time=bar.time.astimezone(UTC)) for bar in bars[:15]]
    assert resample_session_minutes(utc_bars, 15) == result


def test_breakout_uses_only_previous_completed_range_and_volume():
    bars = [
        make_bar(0, open_=100, high=102, low=99, close=101, volume=100),
        make_bar(1, open_=101, high=103, low=100, close=102, volume=100),
        make_bar(2, open_=103, high=108, low=102, close=107, volume=200),
    ]
    signal = evaluate_breakout(
        bars, "005930", regime=Regime.NEUTRAL,
        config=BreakoutConfig(lookback=2, volume_lookback=2, volume_multiple=1.5),
    )
    assert signal.decision == Decision.ENTER
    assert signal.reference_price == 103
    assert signal.stop_price < signal.reference_price


def test_future_extreme_change_does_not_rewrite_prior_breakout_decision():
    prefix = [
        make_bar(0, open_=100, high=102, low=99, close=101, volume=100),
        make_bar(1, open_=101, high=103, low=100, close=102, volume=100),
        make_bar(2, open_=103, high=108, low=102, close=107, volume=200),
    ]
    original_future = make_bar(3, open_=108, high=109, low=107, close=108, volume=100)
    modified_future = make_bar(3, open_=1, high=1_000_000, low=1, close=900_000, volume=10_000_000)
    config = BreakoutConfig(lookback=2, volume_lookback=2, volume_multiple=1.5)
    earlier = evaluate_breakout(prefix, "005930", regime=Regime.NEUTRAL, config=config)
    assert evaluate_breakout(prefix, "005930", regime=Regime.NEUTRAL, config=config) == earlier
    assert evaluate_breakout(prefix + [original_future], "005930", regime=Regime.NEUTRAL, config=config) != earlier
    assert modified_future.time > prefix[-1].time


def test_pullback_requires_impulse_structure_hold_and_rebreak():
    bars = [
        make_bar(0, open_=98, high=100, low=97, close=99, volume=100),
        make_bar(1, open_=99, high=100, low=98, close=99, volume=100),
        make_bar(2, open_=99, high=100, low=98, close=99, volume=100),
        make_bar(3, open_=101, high=112, low=100, close=110, volume=200),
        make_bar(4, open_=109, high=110, low=104, close=108, volume=100),
        make_bar(5, open_=110, high=113, low=109, close=112, volume=130),
    ]
    signal = evaluate_pullback(bars, "005930", regime=Regime.NEUTRAL, config=PullbackConfig(impulse_lookback=3))
    assert signal.decision == Decision.ENTER
    assert signal.reason_codes == ("IMPULSE_CONFIRMED", "STRUCTURE_HELD", "LOCAL_REBREAK")


def test_risk_regime_blocks_long_entries():
    bars = [make_bar(i, open_=100 + i) for i in range(4)]
    signal = evaluate_breakout(bars, "005930", regime=Regime.DOWN)
    assert signal.decision == Decision.HOLD
    assert signal.reason_codes == ("REGIME_BLOCK",)


def test_regime_is_unavailable_without_enough_completed_index_history():
    bars = [make_bar(i, open_=100 + i) for i in range(4)]
    assert classify_regime(bars) is None
    signal = evaluate_breakout(bars, "005930")
    assert signal.decision == Decision.HOLD
    assert signal.reason_codes == ("REGIME_UNAVAILABLE",)


def test_regime_classification_uses_only_previous_index_sessions():
    from krx_trader.cli import _resolve_regime
    from krx_trader.config import Settings
    from krx_trader.models import Bar

    kst = ZoneInfo("Asia/Seoul")
    index = [
        Bar(datetime(2026, 1, 1, tzinfo=kst) + timedelta(days=i), 100 + i, 102 + i, 99 + i, 101 + i, 0)
        for i in range(22)
    ]
    stock_bar = make_bar(0, day=23)
    future_extreme = Bar(datetime(2026, 1, 23, tzinfo=kst), 1000, 2000, 900, 1900, 0)
    baseline = _resolve_regime([stock_bar], index, index, Settings(), "on")
    changed_future = _resolve_regime([stock_bar], index + [future_extreme], index + [future_extreme], Settings(), "on")
    assert baseline == Regime.UP
    assert changed_future == baseline

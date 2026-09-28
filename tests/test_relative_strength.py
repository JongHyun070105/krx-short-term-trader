from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from krx_trader.models import Bar, Decision
from krx_trader.research.phase5_backfill import assert_safe_research_date
from krx_trader.strategies.relative_strength import (
    CrossSectionalEngine,
    RelativeStrengthConfig,
    RelativeStrengthVariant,
    evaluate_relative_strength,
)

KST = ZoneInfo("Asia/Seoul")


def _make_bar(dt: datetime, price: float, volume: int = 1000) -> Bar:
    return Bar(time=dt, open=price, high=price * 1.01, low=price * 0.99, close=price, volume=volume)


def test_holdout_guard_fails_closed():
    # Locked holdout starts 2026-07-28
    assert_safe_research_date(date(2026, 7, 27))
    with pytest.raises(ValueError, match="LOCKED HOLDOUT GUARD TRIGGERED"):
        assert_safe_research_date(date(2026, 7, 28))
    with pytest.raises(ValueError, match="LOCKED HOLDOUT GUARD TRIGGERED"):
        assert_safe_research_date(date(2026, 8, 15))


def test_cross_sectional_percentile_and_tie_breaking():
    config = RelativeStrengthConfig(
        short_lookback_bars=2,
        med_lookback_bars=4,
        min_cross_section_size=3,
    )
    engine = CrossSectionalEngine(config)

    t0 = datetime(2026, 5, 4, 9, 0, tzinfo=KST)
    histories: dict[str, list[Bar]] = {}

    # 3 symbols with distinct performance: A < B < C
    # Needed bars: med_lookback_bars + 1 = 5 bars
    for i, sym in enumerate(["001440", "005940", "035720"]):
        # Base prices at t0..t4
        # sym 001440: return 0%
        # sym 005940: return 10%
        # sym 035720: return 20%
        mult = 1.0 + i * 0.1
        bars = [
            _make_bar(t0 + timedelta(minutes=15 * k), 10_000 * (1.0 if k < 4 else mult))
            for k in range(5)
        ]
        histories[sym] = bars

    t_eval = t0 + timedelta(minutes=15 * 4)
    obs = engine.evaluate_timestamp(t_eval, histories)

    assert len(obs) == 3
    assert obs["001440"].percentile_short == pytest.approx(100.0 / 3.0)
    assert obs["005940"].percentile_short == pytest.approx(200.0 / 3.0)
    assert obs["035720"].percentile_short == pytest.approx(100.0)


def test_symbol_order_determinism():
    config = RelativeStrengthConfig(
        short_lookback_bars=2,
        med_lookback_bars=4,
        min_cross_section_size=3,
    )
    t0 = datetime(2026, 5, 4, 9, 0, tzinfo=KST)

    def create_histories():
        h = {}
        for i, sym in enumerate(["001440", "005940", "035720", "068270"]):
            mult = 1.0 + (i % 2) * 0.05
            h[sym] = [
                _make_bar(t0 + timedelta(minutes=15 * k), 10_000 * (1.0 if k < 4 else mult))
                for k in range(5)
            ]
        return h

    h1 = create_histories()
    engine1 = CrossSectionalEngine(config)
    t_eval = t0 + timedelta(minutes=15 * 4)
    obs1 = engine1.evaluate_timestamp(t_eval, h1)

    # Shuffle symbol dictionary insertion order
    items = list(h1.items())
    random.seed(42)
    random.shuffle(items)
    h2 = dict(items)

    engine2 = CrossSectionalEngine(config)
    obs2 = engine2.evaluate_timestamp(t_eval, h2)

    assert set(obs1.keys()) == set(obs2.keys())
    for sym in obs1:
        assert obs1[sym].percentile_short == obs2[sym].percentile_short
        assert obs1[sym].percentile_med == obs2[sym].percentile_med
        assert obs1[sym].ret_short == obs2[sym].ret_short


def test_lookahead_isolation_future_bars_do_not_alter_past_rank():
    config = RelativeStrengthConfig(
        short_lookback_bars=2,
        med_lookback_bars=4,
        min_cross_section_size=3,
    )
    t0 = datetime(2026, 5, 4, 9, 0, tzinfo=KST)
    symbols = ["001440", "005940", "035720"]

    h_past = {
        sym: [_make_bar(t0 + timedelta(minutes=15 * k), 10_000 * (1.0 + (i + 1) * 0.02 * k)) for k in range(5)]
        for i, sym in enumerate(symbols)
    }

    t_eval = t0 + timedelta(minutes=15 * 4)
    engine_past = CrossSectionalEngine(config)
    obs_past = engine_past.evaluate_timestamp(t_eval, h_past)

    # Now add future bars with wild swings in the future (k = 5, 6, 7)
    h_future = {
        sym: list(bars) for sym, bars in h_past.items()
    }
    for sym in symbols:
        for k in range(5, 8):
            h_future[sym].append(_make_bar(t0 + timedelta(minutes=15 * k), 50_000))

    # Evaluate at the same past timestamp t_eval
    engine_future = CrossSectionalEngine(config)
    obs_evaluated_at_t_eval = engine_future.evaluate_timestamp(t_eval, h_future)

    for sym in symbols:
        assert obs_past[sym].percentile_short == obs_evaluated_at_t_eval[sym].percentile_short
        assert obs_past[sym].ret_short == obs_evaluated_at_t_eval[sym].ret_short
        assert obs_past[sym].ret_med == obs_evaluated_at_t_eval[sym].ret_med


def test_cross_symbol_future_isolation():
    """Future bars of symbol B do not alter symbol A's evaluation at past timestamp T."""
    config = RelativeStrengthConfig(
        short_lookback_bars=2,
        med_lookback_bars=4,
        min_cross_section_size=3,
    )
    t0 = datetime(2026, 5, 4, 9, 0, tzinfo=KST)
    symbols = ["001440", "005940", "035720"]

    h = {
        sym: [_make_bar(t0 + timedelta(minutes=15 * k), 10_000) for k in range(5)]
        for sym in symbols
    }
    t_eval = t0 + timedelta(minutes=15 * 4)

    engine1 = CrossSectionalEngine(config)
    obs1 = engine1.evaluate_timestamp(t_eval, h)

    # Mutate ONLY symbol "035720" in future timestamps (append bars for t5, t6)
    h["035720"].append(_make_bar(t0 + timedelta(minutes=15 * 5), 999_999))
    h["035720"].append(_make_bar(t0 + timedelta(minutes=15 * 6), 999_999))

    engine2 = CrossSectionalEngine(config)
    obs2 = engine2.evaluate_timestamp(t_eval, h)

    assert obs1["001440"].percentile_short == obs2["001440"].percentile_short
    assert obs1["005940"].percentile_short == obs2["005940"].percentile_short


def test_minimum_cross_section_guard():
    config = RelativeStrengthConfig(
        short_lookback_bars=2,
        med_lookback_bars=4,
        min_cross_section_size=5,  # Requires at least 5 symbols
    )
    t0 = datetime(2026, 5, 4, 9, 0, tzinfo=KST)
    h = {
        sym: [_make_bar(t0 + timedelta(minutes=15 * k), 10_000) for k in range(5)]
        for sym in ["001440", "005940", "035720"]  # Only 3 symbols
    }
    engine = CrossSectionalEngine(config)
    obs = engine.evaluate_timestamp(t0 + timedelta(minutes=15 * 4), h)
    assert len(obs) == 0  # Guarded: empty observation due to insufficient cross-section

    sig = evaluate_relative_strength(None, h["001440"], "001440", config=config)
    assert sig.decision == Decision.HOLD
    assert "INSUFFICIENT_CROSS_SECTION" in sig.reason_codes


def test_rank_persistence_tracking():
    config = RelativeStrengthConfig(
        short_lookback_bars=1,
        med_lookback_bars=2,
        rank_threshold=70.0,
        persistence_lookback=3,
        min_persistence_count=2,
        min_cross_section_size=3,
    )
    engine = CrossSectionalEngine(config)
    t0 = datetime(2026, 5, 4, 9, 0, tzinfo=KST)

    # Symbol 035720 is always top performer across 5 consecutive timestamps
    h = {
        "001440": [_make_bar(t0 + timedelta(minutes=15 * k), 10_000) for k in range(6)],
        "005940": [_make_bar(t0 + timedelta(minutes=15 * k), 10_000 * (1.0 + 0.01 * k)) for k in range(6)],
        "035720": [_make_bar(t0 + timedelta(minutes=15 * k), 10_000 * (1.0 + 0.05 * k)) for k in range(6)],
    }

    # Step through timestamps 2, 3, 4, 5
    for k in range(2, 6):
        t_k = t0 + timedelta(minutes=15 * k)
        sub_h = {s: bars[:k + 1] for s, bars in h.items()}
        obs = engine.evaluate_timestamp(t_k, sub_h)
        assert obs["035720"].percentile_short == 100.0

    # At timestamp 5 (k=5), 035720 has been top in timestamps 3, 4, 5 -> persistence should be 3
    final_obs = engine.evaluate_timestamp(t0 + timedelta(minutes=15 * 5), h)
    assert final_obs["035720"].persistence_count == 3


def test_strategy_rs_a_and_rs_b_decisions():
    config = RelativeStrengthConfig(
        short_lookback_bars=2,
        med_lookback_bars=4,
        rank_threshold=70.0,
        persistence_lookback=3,
        min_persistence_count=2,
        min_cross_section_size=3,
        max_single_bar_return_pct=0.04,
    )
    t0 = datetime(2026, 5, 4, 9, 0, tzinfo=KST)
    engine = CrossSectionalEngine(config)

    p_leader = [10_000, 10_000, 10_000, 10_050, 10_100, 10_200, 10_350]
    h = {
        "001440": [_make_bar(t0 + timedelta(minutes=15 * k), 10_000) for k in range(7)],
        "005940": [_make_bar(t0 + timedelta(minutes=15 * k), 10_000 * (1.0 + 0.005 * k)) for k in range(7)],
        "035720": [_make_bar(t0 + timedelta(minutes=15 * k), p_leader[k]) for k in range(7)],
    }

    # Step through to build persistence
    for k in range(4, 7):
        sub_h = {s: bars[:k + 1] for s, bars in h.items()}
        engine.evaluate_timestamp(t0 + timedelta(minutes=15 * k), sub_h)

    t_eval = t0 + timedelta(minutes=15 * 6)
    obs = engine.evaluate_timestamp(t_eval, h)
    leader_obs = obs["035720"]

    # RS-A evaluation
    sig_a = evaluate_relative_strength(
        leader_obs,
        h["035720"],
        "035720",
        variant=RelativeStrengthVariant.RS_A,
        config=config,
    )
    assert sig_a.decision == Decision.ENTER
    assert sig_a.stop_price is not None
    assert sig_a.stop_price < leader_obs.close_price

    # RS-B evaluation (anti-climax confirmed, momentum accelerating)
    sig_b = evaluate_relative_strength(
        leader_obs,
        h["035720"],
        "035720",
        variant=RelativeStrengthVariant.RS_B,
        config=config,
    )
    assert sig_b.decision == Decision.ENTER
    assert sig_b.stop_price is not None

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from krx_trader.models import Bar
from krx_trader.research.phase10_daily_opportunity import (
    assert_development_date,
    build_daily_observations,
    return_3d_bucket,
    return_5d_bucket,
)

KST = ZoneInfo("Asia/Seoul")


def _bars(sessions: list[date], *, base: float = 100.0, step: float = 1.0) -> list[Bar]:
    output = []
    for index, session in enumerate(sessions):
        opening = base + index * step
        output.append(Bar(
            datetime.combine(session, time.min, KST),
            opening,
            opening + 4,
            opening - 2,
            opening + 2,
            1000 + index * 10,
        ))
    return output


def _row(rows: list[dict], symbol: str, session: date) -> dict:
    return next(item for item in rows if item["symbol"] == symbol and item["date"] == session.isoformat())


def test_coarse_trailing_return_buckets_have_fixed_boundaries() -> None:
    assert return_3d_bucket(-8.0) == "<= -8%"
    assert return_3d_bucket(-7.99) == "-8% to -4%"
    assert return_3d_bucket(0.0) == "0 to +2%"
    assert return_3d_bucket(8.0) == ">= +8%"
    assert return_5d_bucket(-10.0) == "<= -10%"
    assert return_5d_bucket(10.0) == ">= +10%"
    assert return_3d_bucket(None) is None


def test_protected_and_external_outcomes_are_rejected() -> None:
    assert_development_date(date(2026, 4, 17))
    with pytest.raises(ValueError, match="Development dates"):
        assert_development_date(date(2026, 7, 28))
    with pytest.raises(ValueError, match="Development dates"):
        assert_development_date(date(2026, 4, 16))


def test_features_for_completed_day_do_not_change_when_future_daily_bar_changes() -> None:
    sessions = [date(2026, 5, 4) + timedelta(days=index) for index in range(7)]
    bars = _bars(sessions)
    features_before, _ = build_daily_observations(
        bars_by_symbol={"000001": bars},
        symbols=["000001"],
        market_by_symbol={"000001": "KOSPI"},
        sessions=sessions,
    )
    changed = bars.copy()
    future = changed[5]
    changed[5] = Bar(future.time, future.open, future.open * 8, future.low, future.open * 7, future.volume)
    features_after, _ = build_daily_observations(
        bars_by_symbol={"000001": changed},
        symbols=["000001"],
        market_by_symbol={"000001": "KOSPI"},
        sessions=sessions,
    )
    target = sessions[3]
    assert _row(features_before, "000001", target) == _row(features_after, "000001", target)


def test_cross_sectional_ranks_use_only_the_same_completed_session() -> None:
    sessions = [date(2026, 5, 4) + timedelta(days=index) for index in range(7)]
    symbols = ["000001", "000002", "000003"]
    input_bars = {
        symbol: _bars(sessions, base=100 + offset * 5, step=offset + 1)
        for offset, symbol in enumerate(symbols)
    }
    before, _ = build_daily_observations(
        bars_by_symbol=input_bars, symbols=symbols,
        market_by_symbol={symbol: "KOSPI" for symbol in symbols}, sessions=sessions,
    )
    changed = {symbol: bars.copy() for symbol, bars in input_bars.items()}
    future = changed["000003"][-1]
    changed["000003"][-1] = Bar(
        future.time, future.open, future.open * 4, future.low, future.open * 3, future.volume
    )
    after, _ = build_daily_observations(
        bars_by_symbol=changed, symbols=symbols,
        market_by_symbol={symbol: "KOSPI" for symbol in symbols}, sessions=sessions,
    )
    target = sessions[3]
    assert _row(before, "000001", target)["cross_sectional_return_3d_percentile"] == \
        _row(after, "000001", target)["cross_sectional_return_3d_percentile"]
    assert _row(before, "000001", target)["cross_sectional_volatility_5d_percentile"] == \
        _row(after, "000001", target)["cross_sectional_volatility_5d_percentile"]


def test_holding_horizon_enters_next_open_and_uses_exact_exit_day() -> None:
    sessions = [
        date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6),
        date(2026, 5, 7), date(2026, 5, 8), date(2026, 5, 11),
    ]
    bars = []
    for index, session in enumerate(sessions):
        opening = 100 + index * 10
        bars.append(Bar(
            datetime.combine(session, time.min, KST),
            opening, opening + 12, opening - 2, opening + 5, 1000,
        ))
    features, outcomes = build_daily_observations(
        bars_by_symbol={"000001": bars},
        symbols=["000001"],
        market_by_symbol={"000001": "KOSDAQ"},
        sessions=sessions,
    )
    first_signal = sessions[0].isoformat()
    two_day = next(
        row for row in outcomes
        if row["signal_date"] == first_signal and row["horizon_days"] == 2
    )
    assert two_day["entry_date"] == sessions[1].isoformat()
    assert two_day["exit_date"] == sessions[2].isoformat()
    assert two_day["entry_open"] == 110
    assert two_day["exit_close"] == 125
    assert two_day["gross_return_pct"] == pytest.approx((125 / 110 - 1) * 100)
    assert two_day["mfe_pct"] == pytest.approx((132 / 110 - 1) * 100)
    assert two_day["mae_pct"] == pytest.approx((108 / 110 - 1) * 100)
    assert len(features) == len(sessions)
    first_feature = _row(features, "000001", sessions[0])
    assert first_feature["price_bucket"] == "LT_10K"
    assert first_feature["liquidity_bucket_adjusted_close_x_volume"] == "LOW_LT_50M"

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from krx_trader.backtest.engine import Trade
from krx_trader.backtest.validation import chronological_split
from krx_trader.models import Bar
from krx_trader.research.runner import _bars_for_segment, _scanner_membership, _trade_concentration

KST = ZoneInfo("Asia/Seoul")


def test_research_split_uses_whole_sessions_in_chronological_order():
    days = [
        Bar(datetime(2026, 9, day, 0, tzinfo=KST), 1, 1, 1, 1, 0)
        for day in range(21, 26)
    ]
    split = chronological_split(days)
    assert [bar.time.date().isoformat() for bar in split.development] == ["2026-09-21", "2026-09-22"]
    assert [bar.time.date().isoformat() for bar in split.validation] == ["2026-09-23"]
    assert [bar.time.date().isoformat() for bar in split.final_test] == ["2026-09-24", "2026-09-25"]
    assert not split.final_test_touched


def test_historical_scanner_rank_is_point_in_time_and_tie_breaks_by_symbol():
    times = [datetime(2026, 9, 21, 9, index, tzinfo=KST) for index in range(2)]
    bars = {
        "000002": [Bar(time, 100, 101, 99, 100, 100) for time in times],
        "000001": [Bar(time, 100, 101, 99, 100, 100) for time in times],
    }
    membership = _scanner_membership(bars, 1)
    assert membership[times[0]] == {"000001"}
    assert membership[times[1]] == {"000001"}


def test_research_segment_keeps_warmup_bars_and_excludes_adjacent_partitions():
    bars = [
        Bar(datetime(2026, 9, day, 9, 0, tzinfo=KST), 100, 101, 99, 100, 1)
        for day in (17, 18, 21, 22, 23)
    ]
    segment = _bars_for_segment({"000001": bars}, date(2026, 9, 18), date(2026, 9, 22))
    assert [bar.time.day for bar in segment["000001"]] == [18, 21]


def test_trade_concentration_reports_trade_symbol_day_month_and_buckets():
    def trade(symbol: str, day: int, pnl: float, price: float) -> Trade:
        time = datetime(2026, 9, day, 10, 0, tzinfo=KST)
        return Trade(
            symbol, "test", time, time, price, price, time + timedelta(minutes=15),
            price + pnl, price + pnl, 1, price - 10, pnl, 0, 0, 0, max(0, pnl), min(0, pnl),
            pnl, 1, "TIME_EXIT",
        )

    trades = (trade("000001", 23, 100, 5_000), trade("000002", 24, 50, 20_000))
    bars = {
        symbol: [Bar(datetime(2026, 9, 23, 9, 45, tzinfo=KST), 1000, 1000, 1000, 1000, 100_000)]
        for symbol in ("000001", "000002")
    }
    summary = _trade_concentration(trades, bars, {"000001": "KOSPI", "000002": "KOSDAQ"})
    assert summary["top_trade_pct"] == 66.67
    assert summary["top5_trade_pct"] == 100.0
    assert summary["top_symbol"]["key"] == "000001"
    assert summary["markets"] == {"KOSDAQ": 50, "KOSPI": 100}
    assert summary["price_buckets"]["1k_10k"]["trades"] == 1
    assert summary["price_buckets"]["10k_30k"]["trades"] == 1

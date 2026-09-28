import io
import zipfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.research_set import build_research_set
from krx_trader.models import Bar
from krx_trader.universe.filters import check_eligibility, deduplicate_activities
from krx_trader.universe.master import parse_master_archive
from krx_trader.universe.models import CandidateContext, MarketActivity, StockMaster
from krx_trader.universe.rankers import rank_breakout, rank_pullback
from krx_trader.universe.scanner import scan_market

KST = ZoneInfo("Asia/Seoul")


def stock(symbol: str = "005930", **updates) -> StockMaster:
    values = {
        "symbol": symbol, "name": "Test Corp", "market": "KOSPI", "instrument_type": "COMMON",
        "listing_status": "CURRENT_MASTER", "reference_price": 10_000, "market_cap_raw": None,
        "halted": False, "management": False, "warning_status": None,
        "is_preferred": False, "is_etp": False, "is_spac": False,
    }
    values.update(updates)
    return StockMaster(**values)


def activity(symbol: str = "005930", **updates) -> MarketActivity:
    values = {
        "symbol": symbol, "price": 10_000, "volume": 1_000_000,
        "turnover_krw": 10_000_000_000, "source": "test", "source_rank": 1,
    }
    values.update(updates)
    return MarketActivity(**values)


def test_universe_eligibility_uses_price_security_status_and_turnover():
    settings = Settings.from_env({}, env_file=None)
    result = check_eligibility(stock(), activity(), settings, min_turnover_krw=50_000_000)
    assert result.eligible
    assert result.reason_codes == ("ELIGIBLE",)

    excluded = check_eligibility(
        stock(instrument_type="PREFERRED", halted=True, management=True, warning_status="1"),
        activity(price=900, turnover_krw=100),
        settings,
        min_turnover_krw=50_000_000,
    )
    assert not excluded.eligible
    assert excluded.reason_codes == (
        "SECURITY_TYPE_EXCLUDED", "TRADING_HALTED", "MANAGEMENT_SECURITY", "MARKET_WARNING",
        "PRICE_TOO_LOW", "LIQUIDITY_LOW",
    )


def test_universe_excludes_price_above_one_share_order_cap():
    settings = Settings.from_env({}, env_file=None)
    result = check_eligibility(
        stock(), activity(price=20_000), settings, min_turnover_krw=50_000_000
    )
    assert not result.eligible
    assert result.reason_codes == ("UNAFFORDABLE_ONE_SHARE",)


def test_scanner_returns_empty_when_order_cap_cannot_afford_minimum_price(tmp_path):
    settings = Settings.from_env({"MAX_ORDER_NOTIONAL_KRW": "500"}, env_file=None)

    class UnusedClient:
        def get_market_activity_rank(self, **_kwargs):
            raise AssertionError("scanner must stop before an invalid provider price range")

    assert scan_market(UnusedClient(), settings, master_path=tmp_path / "missing.parquet") == ([], 0)


def test_market_rank_source_union_deduplicates_by_symbol_deterministically():
    rows = deduplicate_activities(
        [activity("005930", volume=10, turnover_krw=100, source="volume")],
        [activity("005930", volume=20, turnover_krw=200, source="turnover"), activity("000660")],
    )
    assert [row.symbol for row in rows] == ["000660", "005930"]
    samsung = rows[1]
    assert samsung.source == "volume"
    assert samsung.volume == 20
    assert samsung.turnover_krw == 200


def test_kis_master_parser_keeps_common_stock_flags_and_reference_price():
    tail = list(" " * 227)
    fields = {
        "security_group": (0, 2, "ST"),
        "etp": (22, 23, " "), "spac": (29, 30, " "), "reference_price": (41, 50, "000010000"),
        "halted": (60, 61, " "), "management": (62, 63, " "), "market_warning": (63, 65, "00"),
        "warning_alert": (65, 66, " "), "preferred": (158, 159, " "), "market_cap": (212, 221, "000100000"),
    }
    for start, end, value in fields.values():
        tail[start:end] = value
    record = f"{'005930':<9}{'KR7005930003':<12}{'Samsung Test':<28}" + "".join(tail)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("kospi_code.mst", record + "\n")
    parsed = parse_master_archive(stream.getvalue(), "KOSPI")
    assert len(parsed) == 1
    assert parsed[0].symbol == "005930"
    assert parsed[0].name == "Samsung Test"
    assert parsed[0].instrument_type == "COMMON"
    assert parsed[0].reference_price == 10_000
    assert parsed[0].market_cap_raw == 100_000


def test_kis_master_parser_excludes_official_etp_marker_2():
    tail = list(" " * 227)
    tail[0:2] = "ST"
    tail[22] = "2"
    record = f"{'069500':<9}{'KR7069500007':<12}{'KODEX 200':<28}" + "".join(tail)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("kospi_code.mst", record + "\n")
    parsed = parse_master_archive(stream.getvalue(), "KOSPI")
    assert parsed[0].instrument_type == "ETF_ETN"
    assert parsed[0].is_etp


def test_kis_master_parser_excludes_etn_product_codes():
    tail = list(" " * 227)
    tail[0:2] = "ST"
    tail[22] = "3"
    record = f"{'123456':<9}{'KR7123450000':<12}{'Test ETN':<28}" + "".join(tail)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("kospi_code.mst", record + "\n")
    parsed = parse_master_archive(stream.getvalue(), "KOSPI")
    assert parsed[0].instrument_type == "ETF_ETN"
    assert parsed[0].is_etp


def test_kis_kosdaq_master_uses_its_own_suffix_and_field_offsets():
    tail = list(" " * 221)
    fields = {
        "security_group": (0, 2, "ST"),
        "etp": (18, 19, "0"),
        "spac": (24, 25, "N"),
        "reference_price": (36, 45, "000001657"),
        "halted": (55, 56, "N"),
        "management": (57, 58, "N"),
        "market_warning": (58, 60, "00"),
        "warning_alert": (60, 61, "N"),
        "preferred": (153, 154, "N"),
        "market_cap": (206, 215, "000038869"),
    }
    for start, end, value in fields.values():
        tail[start:end] = value
    record = f"{'000250':<9}{'KR7000250004':<12}{'KOSDAQ Test':<35}" + "".join(tail)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("kosdaq_code.mst", record + "\n")
    parsed = parse_master_archive(stream.getvalue(), "KOSDAQ")
    assert len(parsed) == 1
    assert parsed[0].symbol == "000250"
    assert parsed[0].name == "KOSDAQ Test"
    assert parsed[0].instrument_type == "COMMON"
    assert parsed[0].reference_price == 1_657
    assert parsed[0].market_cap_raw == 38_869
    assert not parsed[0].halted
    assert not parsed[0].management
    assert parsed[0].warning_status is None


def test_breakout_and_pullback_rankers_are_separate_and_tie_break_by_symbol():
    start = datetime(2026, 1, 1, 9, 0, tzinfo=KST)
    bars = tuple(
        Bar(start + timedelta(days=index), 100 + index, 102 + index, 99 + index, 101 + index, 1_000 + index)
        for index in range(10)
    )
    contexts = [
        CandidateContext(stock(symbol="000660"), activity(symbol="000660"), bars),
        CandidateContext(stock(symbol="005930"), activity(symbol="005930"), bars),
    ]
    breakout = rank_breakout(contexts, top_n=2)
    pullback = rank_pullback(contexts, top_n=2)
    assert [row.stock.symbol for row in breakout] == ["000660", "005930"]
    assert [row.stock.symbol for row in pullback] == ["000660", "005930"]
    assert all(row.strategy == "breakout" for row in breakout)
    assert all(row.strategy == "pullback" for row in pullback)
    assert all(0 <= row.score <= 1 for row in breakout + pullback)


def test_parquet_cache_round_trip_is_resumable_and_hash_checked(tmp_path):
    cache = ParquetBarCache(tmp_path / "data")
    bars = [
        Bar(datetime(2026, 1, 1, 9, 0, tzinfo=KST), 100, 102, 99, 101, 10),
        Bar(datetime(2026, 1, 1, 9, 1, tzinfo=KST), 101, 103, 100, 102, 20),
    ]
    path, metadata = cache.save(
        bars, kind="minute", symbol="005930", interval="1m", market="KOSPI",
        source="unit fixture", session_date=date(2026, 1, 1),
    )
    assert cache.contains("minute", "005930", "1m", date(2026, 1, 1))
    assert cache.load("minute", "005930", "1m", date(2026, 1, 1)) == bars
    assert metadata["rows"] == 2
    assert metadata["first_timestamp"].endswith("+09:00")

    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash"):
        cache.load("minute", "005930", "1m", date(2026, 1, 1))


def test_research_set_resumes_daily_and_session_partitions_without_refetch(tmp_path):
    session = date(2026, 1, 2)
    daily = [Bar(datetime(2026, 1, 2, 0, tzinfo=KST), 100, 102, 99, 101, 1000)]
    minute = [Bar(datetime(2026, 1, 2, 9, 0, tzinfo=KST), 100, 102, 99, 101, 100)]

    class FakeClient:
        daily_calls = 0
        minute_calls = 0

        def get_daily_bars(self, symbol, start, end):
            self.daily_calls += 1
            return daily

        def get_minute_bars(self, symbol, requested_session):
            assert requested_session == session
            self.minute_calls += 1
            return minute

    client = FakeClient()
    cache = ParquetBarCache(tmp_path / "data")
    kwargs = {
        "symbols": ["005930"], "start": session, "end": session, "max_sessions": 1,
        "cache": cache, "report_path": tmp_path / "run.json",
    }
    first = build_research_set(client, **kwargs)
    assert first["symbols_succeeded"] == 1
    assert first["minute_rows"] == 1
    assert (client.daily_calls, client.minute_calls) == (1, 1)

    resumed = build_research_set(client, **kwargs)
    assert resumed["symbols_succeeded"] == 1
    assert (client.daily_calls, client.minute_calls) == (1, 1)

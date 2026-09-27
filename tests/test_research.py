import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from krx_trader.backtest.engine import Trade
from krx_trader.backtest.validation import chronological_split
from krx_trader.data.cache import ParquetBarCache
from krx_trader.models import Bar
from krx_trader.research.runner import (
    _bars_for_segment,
    _load_research_bars,
    _scanner_membership,
    _segment_dates,
    _trade_concentration,
    _verify_frozen_dataset_digest,
    _with_reference_dataset_hashes,
)
from krx_trader.research.scenario import ResearchScenario

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


def test_research_scenario_is_separate_and_remains_under_live_safety_caps():
    scenario = ResearchScenario("diagnostic", 100_000, 50_000, 1.0, "off", 2)
    assert scenario.capital_krw == 100_000
    assert scenario.regime_mode == "off"


def test_research_loader_honors_a_separate_frozen_dataset_manifest(tmp_path: Path):
    cache = ParquetBarCache(tmp_path / "data")
    sessions = (date(2026, 8, 27), date(2026, 8, 28), date(2026, 9, 23))
    for symbol in ("000001", "000002"):
        for day in sessions:
            cache.save(
                [Bar(datetime.combine(day, datetime.min.time(), KST).replace(hour=9), 100, 101, 99, 100, 10)],
                kind="minute",
                symbol=symbol,
                interval="1m",
                market="KRX",
                source="test KIS session",
                session_date=day,
            )
    _, saved_metadata = cache.save(
        [Bar(datetime(2026, 8, 27, 9, tzinfo=KST), 100, 101, 99, 100, 10)],
        kind="minute",
        symbol="000001",
        interval="1m",
        market="KRX",
        source="test KIS session",
        session_date=date(2026, 8, 27),
    )
    manifest = tmp_path / "runtime" / "clean-manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({
        "succeeded_symbols": ["000001"],
        "date_range": {"start": "2026-08-27", "end": "2026-08-28"},
        "sessions": ["2026-08-27"],
        "partition_hashes": [f"000001:2026-08-27:{saved_metadata['sha256']}"],
    }))

    bars, dataset_hash, partitions = _load_research_bars(tmp_path / "data", dataset_manifest=manifest)

    assert list(bars) == ["000001"]
    assert [bar.time.date() for bar in bars["000001"]] == [date(2026, 8, 27)]
    assert len(dataset_hash) == 64
    assert all("2026-09-23" not in partition for partition in partitions)
    cache.save(
        [Bar(datetime(2026, 8, 27, 9, tzinfo=KST), 100, 102, 99, 101, 10)],
        kind="minute",
        symbol="000001",
        interval="1m",
        market="KRX",
        source="test mutated KIS session",
        session_date=date(2026, 8, 27),
    )
    with pytest.raises(ValueError, match="frozen dataset manifest"):
        _load_research_bars(tmp_path / "data", dataset_manifest=manifest)


def test_clean_research_hash_excludes_index_rows_after_frozen_dataset_end(tmp_path: Path):
    cache = ParquetBarCache(tmp_path / "data")
    index_rows = [
        Bar(datetime(2026, 8, 28, tzinfo=KST), 100, 101, 99, 100, 10),
        Bar(datetime(2026, 9, 23, tzinfo=KST), 100, 102, 99, 101, 10),
    ]
    for market in ("kospi", "kosdaq"):
        cache.save(
            index_rows[:1],
            kind="indexes",
            symbol=market,
            interval="1d",
            market=market.upper(),
            source="test KIS index rows",
        )
    universe_metadata = tmp_path / "data" / "universe" / "stocks.metadata.json"
    universe_metadata.parent.mkdir(parents=True)
    universe_metadata.write_text(json.dumps({"parquet_sha256": "current-universe-hash"}))
    first_hash, _ = _with_reference_dataset_hashes(
        tmp_path / "data", [], through_date=date(2026, 8, 28)
    )
    for market in ("kospi", "kosdaq"):
        cache.save(
            index_rows,
            kind="indexes",
            symbol=market,
            interval="1d",
            market=market.upper(),
            source="test KIS index rows",
        )
    second_hash, records = _with_reference_dataset_hashes(
        tmp_path / "data", [], through_date=date(2026, 8, 28)
    )

    assert first_hash == second_hash
    assert all("2026-08-28" in row for row in records if row.startswith("index:"))


def test_partial_fresh_research_cohort_still_enforces_frozen_digest():
    manifest = {
        "evidence_class": "FRESH_RESEARCH_DIAGNOSTIC_WITH_GAPS",
        "dataset_sha256": "frozen-digest",
    }
    _verify_frozen_dataset_digest(manifest, "frozen-digest")
    with pytest.raises(ValueError, match="frozen cohort manifest"):
        _verify_frozen_dataset_digest(manifest, "changed-digest")


def test_frozen_split_does_not_shift_when_sessions_are_missing():
    development = ["2026-04-17", "2026-04-20", "2026-04-21"]
    manifest = {
        "splits": {
            "development": {"start": development[0], "end": development[-1], "sessions": development},
            "validation": {
                "start": "2026-04-22",
                "end": "2026-04-24",
                "sessions": ["2026-04-22", "2026-04-23", "2026-04-24"],
            },
            "fresh_holdout": {
                "start": "2026-04-27",
                "end": "2026-04-30",
                "sessions": ["2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30"],
            },
        }
    }
    complete = [date.fromisoformat(value) for value in development]
    assert _segment_dates(complete, manifest, "development") == complete
    with pytest.raises(ValueError, match="boundary shifts are prohibited"):
        _segment_dates(complete[:-1], manifest, "development")

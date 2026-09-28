import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from krx_trader.data.cache import ParquetBarCache
from krx_trader.models import Bar
from krx_trader.research.phase4 import (
    audit_minute_gaps,
    load_used_development_validation,
    select_expanded_cohorts,
)
from krx_trader.universe.models import StockMaster

KST = ZoneInfo("Asia/Seoul")


def _stock(symbol: int, market: str, price: int, market_cap: int) -> StockMaster:
    return StockMaster(
        symbol=f"{symbol:06d}", name=f"S{symbol}", market=market,
        instrument_type="COMMON", listing_status="CURRENT_MASTER",
        reference_price=price, market_cap_raw=market_cap,
        halted=False, management=False, warning_status=None,
        is_preferred=False, is_etp=False, is_spac=False,
    )


def test_phase4_loader_reads_only_used_sessions_and_keeps_holdout_locked(tmp_path: Path):
    cache_root = tmp_path / "data"
    cache = ParquetBarCache(cache_root)
    development = date(2026, 6, 30)
    validation = date(2026, 7, 1)
    safe_hashes = []
    for day in (development, validation):
        _, metadata = cache.save(
            [Bar(datetime.combine(day, datetime.min.time(), KST).replace(hour=9), 100, 101, 99, 100, 1)],
            kind="minute", symbol="000001", interval="1m", market="KOSPI",
            source="test KIS partition", session_date=day,
        )
        safe_hashes.append(f"000001:{day.isoformat()}:{metadata['sha256']}")
    safe_hashes.append(f"000001:2026-07-28:{'a' * 64}")
    manifest = tmp_path / "phase3-manifest.json"
    manifest.write_text(json.dumps({
        "fresh_holdout_state": "LOCKED_NOT_EVALUATED",
        "dataset_sha256": "b" * 64,
        "succeeded_symbols": ["000001"],
        "splits": {
            "development": {"sessions": [development.isoformat()]},
            "validation": {"sessions": [validation.isoformat()]},
            "fresh_holdout": {"sessions": ["2026-07-28"]},
        },
        "partition_hashes": safe_hashes,
        "quality": {"expected_minutes_per_session_adjusted_for_documented_kospi_halts": {
            development.isoformat(): 380, validation.isoformat(): 380, "2026-07-28": 380,
        }},
    }), encoding="utf-8")

    bars, by_session, hashes, _, splits = load_used_development_validation(cache_root, manifest)
    assert list(bars) == ["000001"]
    assert set(by_session["000001"]) == {development, validation}
    assert set(hashes) == {f"000001:{development.isoformat()}", f"000001:{validation.isoformat()}"}
    assert all(day < date(2026, 7, 28) for days in splits.values() for day in days)
    assert not (cache_root / "minute" / "000001" / "2026-07-28.parquet").exists()


def test_phase4_minute_gap_audit_keeps_missing_slots_unknown_and_never_fills():
    day = date(2026, 6, 30)
    start = datetime.combine(day, datetime.min.time(), KST).replace(hour=9)
    bars = [
        Bar(start + timedelta(minutes=index), 100, 101, 99, 100, 1)
        for index in range(380) if index not in {4, 5, 30}
    ]
    result = audit_minute_gaps(
        {"000001": {day: bars}},
        {f"000001:{day.isoformat()}": hashlib.sha256(b"partition").hexdigest()},
        {"development": [day], "validation": [date(2026, 7, 1)]},
        git_sha="c" * 40,
    )
    assert result["expected_minute_slots"] == 380
    assert result["observed_minute_slots"] == 377
    assert result["missing_minute_slots"] == 3
    assert result["classifications"]["UNKNOWN"] == 3
    assert result["classifications"]["RETRIEVAL_GAP_CONFIRMED"] == 0
    assert result["partial_partitions_detail"][0]["missing_ranges"] == [
        {"start": "09:04", "end": "09:05", "minutes": 2},
        {"start": "09:30", "end": "09:30", "minutes": 1},
    ]
    assert result["partial_partitions_detail"][0]["synthetic_bars_added"] == 0
    assert result["holdout_integrity"]["holdout_partition_files_opened"] == 0


def test_phase4_dq_audit_rejects_any_split_crossing_locked_boundary():
    with pytest.raises(ValueError, match="locked Holdout"):
        audit_minute_gaps({}, {}, {"development": [date(2026, 7, 28)]}, git_sha="c" * 40)


def test_expanded_cohort_selection_is_deterministic_and_balanced(tmp_path: Path):
    stocks = []
    baseline = []
    for index in range(1, 31):
        stock = _stock(index, "KOSPI", 5_000 + (index % 3) * 10_000, index * 1_000_000)
        stocks.append(stock)
        baseline.append(stock.symbol)
    for market, start in (("KOSPI", 100), ("KOSDAQ", 1_000)):
        for index in range(90):
            price = (5_000, 20_000, 40_000)[index % 3]
            stocks.append(_stock(start + index, market, price, (index + 1) * 2_000_000))
    master = tmp_path / "stocks.parquet"
    master.write_bytes(b"frozen test master")
    first = select_expanded_cohorts(stocks, baseline, master_path=master, git_sha="d" * 40)
    second = select_expanded_cohorts(stocks, baseline, master_path=master, git_sha="d" * 40)
    assert first == second
    assert first["cohort_60"]["size"] == 60
    assert first["cohort_60"]["market_counts"] == {"KOSDAQ": 30, "KOSPI": 30}
    assert first["cohort_100"]["size"] == 100
    assert first["cohort_100"]["market_counts"] == {"KOSDAQ": 50, "KOSPI": 50}
    assert set(baseline) <= set(first["cohort_60"]["symbols"])
    assert set(first["cohort_60"]["symbols"]) <= set(first["cohort_100"]["symbols"])
    assert first["cohort_100"]["turnover_liquidity_measure"] == "NOT_AVAILABLE_FROM_CURRENT_STOCK_MASTER"
    assert first["selection_frozen_before_strategy_evaluation"] is True


def test_expanded_cohort_does_not_silently_filter_original_symbols():
    baseline = [_stock(index, "KOSPI", 5_000, 100_000) for index in range(1, 31)]
    candidates = baseline + [_stock(1000 + index, "KOSDAQ", 10_000, 1_000_000) for index in range(40)]
    report = select_expanded_cohorts(
        candidates, [stock.symbol for stock in baseline], master_path=Path(__file__), git_sha="e" * 40,
    )
    assert report["cohort_60"]["baseline_symbols_retained"] == 30
    assert report["cohort_100"]["baseline_symbols_retained"] == 30


def test_expanded_cohort_does_not_hide_market_shortfall_with_single_market_fallback():
    baseline = [_stock(index, "KOSPI", 5_000, 100_000) for index in range(1, 31)]
    candidates = baseline + [_stock(1000 + index, "KOSDAQ", 10_000, 1_000_000) for index in range(5)]
    report = select_expanded_cohorts(
        candidates, [stock.symbol for stock in baseline], master_path=Path(__file__), git_sha="f" * 40,
    )
    assert report["cohort_60"]["size"] == 35
    assert report["cohort_60"]["selection_shortfall"] == 25
    assert report["cohort_60"]["market_counts"] == {"KOSDAQ": 5, "KOSPI": 30}
    assert report["cohort_100"]["size"] == 35

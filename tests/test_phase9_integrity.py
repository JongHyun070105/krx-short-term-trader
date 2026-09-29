from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.resample import resample_session_minutes
from krx_trader.models import Bar
from krx_trader.research.phase9_integrity import (
    DAILY_SOURCE,
    MINUTE_SOURCE,
    artifact_index,
    assert_development_date,
    assert_phase9_output_path,
    audit_development_minute_cache,
    build_alignment_report,
    classify_suspicious_gap,
    compare_manifest_hashes,
    drift_value,
    market_by_cohort_order,
    validate_development_sessions,
    verify_artifact_index,
)

KST = ZoneInfo("Asia/Seoul")
DEV_DAY = date(2026, 5, 4)


def _session_bars(session: date = DEV_DAY, *, open_price: float = 100.0) -> list[Bar]:
    start = datetime.combine(session, time(9, 0), KST)
    return [
        Bar(start + timedelta(minutes=index), open_price, open_price + 1, open_price - 1,
            open_price + (0.25 if index < 60 else 0.5), 10)
        for index in range(380)
    ]


def _daily(session: date, *, open_price: float = 100, close: float = 100.5) -> Bar:
    return Bar(datetime.combine(session, time(0, 0), KST), open_price, 102, 98, close, 1000)


def test_source_inventory_pins_daily_raw_and_leaves_minute_adjustment_unknown() -> None:
    assert DAILY_SOURCE["endpoint"].endswith("inquire-daily-itemchartprice")
    assert DAILY_SOURCE["adjustment_parameter"] == "FID_ORG_ADJ_PRC: 0=adjusted, 1=raw/original"
    assert DAILY_SOURCE["adjustment_status"] == "CONFIGURABLE"
    assert DAILY_SOURCE["phase9_comparison_convention"].startswith("adjusted (0)")
    assert MINUTE_SOURCE["endpoint"].endswith("inquire-time-dailychartprice")
    assert MINUTE_SOURCE["adjustment_parameter"] is None
    assert MINUTE_SOURCE["adjustment_status"] == "UNKNOWN"


def test_development_guard_rejects_external_and_protected_dates() -> None:
    assert_development_date(DEV_DAY)
    with pytest.raises(ValueError, match="Development dates"):
        assert_development_date(date(2026, 4, 16))
    with pytest.raises(ValueError, match="Development dates"):
        assert_development_date(date(2026, 7, 28))
    with pytest.raises(ValueError, match="Development dates"):
        validate_development_sessions([DEV_DAY, date(2026, 8, 1)])


def test_phase9_artifact_output_is_isolated_from_previous_phases() -> None:
    assert_phase9_output_path(Path("runtime/research/phase9"))
    assert_phase9_output_path(Path("runtime/research/phase9/reconciliation"))
    with pytest.raises(ValueError, match="runtime/research/phase9"):
        assert_phase9_output_path(Path("runtime/research/phase8"))


def test_frozen_cohort_market_split_is_exactly_thirty_each() -> None:
    symbols = [f"{index:06d}" for index in range(60)]
    market = market_by_cohort_order(symbols)
    assert sum(value == "KOSPI" for value in market.values()) == 30
    assert sum(value == "KOSDAQ" for value in market.values()) == 30
    with pytest.raises(ValueError, match="frozen 60-symbol"):
        market_by_cohort_order(symbols[:-1])


def test_alignment_reports_open_close_and_prior_close_by_market() -> None:
    symbols = [f"{index:06d}" for index in range(60)]
    markets = market_by_cohort_order(symbols)
    prior_session = date(2026, 4, 30)
    current = _session_bars()
    prior = _session_bars(prior_session, open_price=99)
    daily = {
        symbols[0]: [_daily(prior_session, open_price=99, close=100), _daily(DEV_DAY)],
    }
    minutes = {
        (symbols[0], prior_session): prior,
        (symbols[0], DEV_DAY): current,
    }
    report, _ = build_alignment_report(
        daily_by_symbol=daily, raw_daily_by_symbol=daily,
        minute_by_symbol_session=minutes, market_by_symbol=markets,
        development_sessions=[prior_session, DEV_DAY],
    )
    assert report["daily_open_vs_09_00_minute_open_pct"]["KOSPI"]["n"] == 2
    assert report["daily_open_vs_09_00_minute_open_pct"]["KOSPI"]["exact_match_pct"] == 100
    assert report["daily_close_vs_15_19_minute_close_pct"]["KOSPI"]["n"] == 2
    assert report["daily_previous_close_vs_prior_15_19_continuous_close_pct"]["KOSPI"]["n"] == 1
    assert report["daily_close_vs_15_19_threshold_counts"]["KOSPI"]["gt_0.5_pct"] == 1


def test_raw_daily_gap_is_classified_as_adjustment_mismatch_when_adjusted_source_aligns() -> None:
    symbols = [f"{index:06d}" for index in range(60)]
    symbol = symbols[0]
    markets = market_by_cohort_order(symbols)
    prior_session = date(2026, 4, 30)
    current = _session_bars(open_price=100)
    prior = _session_bars(prior_session, open_price=100)
    adjusted = {symbol: [_daily(prior_session, open_price=100, close=100), _daily(DEV_DAY)]}
    raw = {symbol: [_daily(prior_session, open_price=1000, close=1000),
                    _daily(DEV_DAY, open_price=1000, close=1000)]}
    report, events = build_alignment_report(
        daily_by_symbol=adjusted, raw_daily_by_symbol=raw,
        minute_by_symbol_session={(symbol, prior_session): prior, (symbol, DEV_DAY): current},
        market_by_symbol=markets, development_sessions=[prior_session, DEV_DAY],
    )
    assert report["suspicious_gap_event_count"] == 1
    assert events[0]["daily_based_gap_pct"] == pytest.approx(-90)
    assert events[0]["adjusted_daily_based_gap_pct"] == pytest.approx(0)
    assert events[0]["classification"] == "ADJUSTMENT_MISMATCH"
    assert events[0]["ratio_cluster"] == pytest.approx(10)


def test_suspicious_gap_classification_keeps_split_scale_as_likely_corporate_action() -> None:
    classification, ratio = classify_suspicious_gap(
        daily_open=10, daily_prior_close=100, minute_open=10, minute_prior_close=100,
        daily_gap_pct=-90, continuous_gap_pct=-90, complete_session=True,
    )
    assert classification == "LIKELY_CORPORATE_ACTION"
    assert ratio == pytest.approx(0.1)


def test_suspicious_gap_classification_distinguishes_mismatch_and_unknown() -> None:
    mismatch, _ = classify_suspicious_gap(
        daily_open=105, daily_prior_close=100, minute_open=100, minute_prior_close=100,
        daily_gap_pct=5, continuous_gap_pct=0, complete_session=True,
    )
    missing, _ = classify_suspicious_gap(
        daily_open=100, daily_prior_close=100, minute_open=100, minute_prior_close=None,
        daily_gap_pct=0, continuous_gap_pct=None, complete_session=True,
    )
    assert mismatch == "DAILY_MINUTE_SOURCE_MISMATCH"
    assert missing == "MISSING_PRIOR_SESSION"


def test_timestamp_and_resampling_semantics_use_start_labeled_bars() -> None:
    source = _session_bars()
    result = resample_session_minutes(source[:15], 15)
    assert len(result) == 1
    assert result[0].time == datetime(2026, 5, 4, 9, 15, tzinfo=KST)
    assert result[0].open == source[0].open
    assert result[0].close == source[14].close


def test_checkpoint_drift_excludes_future_bar_after_checkpoint() -> None:
    source = _session_bars()
    changed_future = source.copy()
    changed_future[15] = Bar(changed_future[15].time, 500, 501, 499, 500, 1)
    assert drift_value(source, DEV_DAY, "09:15") == drift_value(changed_future, DEV_DAY, "09:15")


def test_cache_audit_only_loads_explicit_development_partitions(tmp_path: Path) -> None:
    cache = ParquetBarCache(tmp_path)
    bars = _session_bars()
    cache.save(bars, kind="minute", symbol="000001", interval="1m", market="KOSPI",
               source="test", session_date=DEV_DAY)
    locked_day = date(2026, 8, 3)
    locked_path = cache.partition_path("minute", "000001", "1m", locked_day)
    locked_path.parent.mkdir(parents=True, exist_ok=True)
    locked_path.write_bytes(b"protected fixture must not be touched")
    protected_sidecar = locked_path.with_suffix(".metadata.json")
    protected_sidecar.write_text("{this protected metadata must not be parsed", encoding="utf-8")
    loaded, report = audit_development_minute_cache(
        symbols=["000001"], sessions=[DEV_DAY], market_by_symbol={"000001": "KOSPI"},
        cache_root=tmp_path,
    )
    assert len(loaded) == 1
    assert report["partition_counts"]["valid_full_session"] == 1
    assert report["partition_counts"]["expected"] == 1
    assert report["partition_counts"]["invalid"] == 0


def test_cache_audit_keeps_sparse_valid_bars_as_partial_provider_shape(tmp_path: Path) -> None:
    cache = ParquetBarCache(tmp_path)
    start = datetime.combine(DEV_DAY, time(9, 0), KST)
    bars = [
        Bar(start, 100, 101, 99, 100, 1),
        Bar(start + timedelta(minutes=7), 100, 101, 99, 100, 1),
        Bar(start + timedelta(minutes=379), 100, 101, 99, 100, 1),
    ]
    cache.save(bars, kind="minute", symbol="000001", interval="1m", market="KOSPI",
               source="test", session_date=DEV_DAY)
    loaded, report = audit_development_minute_cache(
        symbols=["000001"], sessions=[DEV_DAY], market_by_symbol={"000001": "KOSPI"},
        cache_root=tmp_path,
    )
    assert len(loaded) == 1
    assert report["status"] == "PARTIAL"
    assert report["partition_counts"]["valid_partial_session"] == 1
    assert report["partition_counts"]["invalid"] == 0
    assert report["partition_counts"]["missing_expected_rows"] == 377


def test_cache_audit_fails_closed_on_sidecar_row_count_mismatch(tmp_path: Path) -> None:
    import json

    cache = ParquetBarCache(tmp_path)
    cache.save(_session_bars(), kind="minute", symbol="000001", interval="1m", market="KOSPI",
               source="test", session_date=DEV_DAY)
    sidecar = cache.partition_path("minute", "000001", "1m", DEV_DAY).with_suffix(".metadata.json")
    metadata = json.loads(sidecar.read_text())
    metadata["rows"] = 379
    sidecar.write_text(json.dumps(metadata))
    loaded, report = audit_development_minute_cache(
        symbols=["000001"], sessions=[DEV_DAY], market_by_symbol={"000001": "KOSPI"},
        cache_root=tmp_path,
    )
    assert not loaded
    assert report["status"] == "FAIL"
    assert report["invalid_partitions"][0]["reason"] == "ValueError"


def test_manifest_immutability_comparison_fails_closed() -> None:
    assert compare_manifest_hashes({"p5": "a", "p8": "b"}, {"p5": "a", "p8": "b"})["status"] == "PASS"
    changed = compare_manifest_hashes({"p5": "a", "p8": "b"}, {"p5": "a", "p8": "c"})
    assert changed["status"] == "FAIL"
    assert changed["changed"] == ["p8"]


def test_phase9_artifact_index_checks_exist_size_and_sha(tmp_path: Path) -> None:
    (tmp_path / "report.json").write_text('{"ok": true}\n')
    index = artifact_index(tmp_path, ["report.json"])
    assert verify_artifact_index(tmp_path, index)["status"] == "PASS"
    (tmp_path / "report.json").write_text('{"ok": false}\n')
    assert verify_artifact_index(tmp_path, index)["failures"][0]["reason"] == "SIZE_MISMATCH"

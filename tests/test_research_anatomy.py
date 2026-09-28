import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_trader.backtest.costs import CostModel
from krx_trader.data.cache import ParquetBarCache
from krx_trader.models import Bar
from krx_trader.research.anatomy import (
    _cost_stress,
    _event_row,
    _feature_comparison,
    _gap_near_event,
    _load_frozen_partitions,
    _one_share_order_cap_eligible,
    _one_share_risk_krw,
    _overextension_filter,
    _thresholds_from_dev,
    _trade_metrics,
    _variant_metrics,
    candle_features,
    extract_context_at_index,
    run_breakout_v2_comparison,
    time_bucket,
)

KST = ZoneInfo("Asia/Seoul")


def test_time_bucket_boundaries_match_phase3_spec():
    cases = {
        (9, 0): "09:00-09:30",
        (9, 29): "09:00-09:30",
        (9, 30): "09:30-10:00",
        (10, 0): "10:00-11:00",
        (11, 0): "11:00-12:00",
        (12, 0): "12:00-13:00",
        (13, 0): "13:00-14:00",
        (14, 0): "14:00-15:00",
        (15, 0): "15:00+",
    }
    for (hour, minute), expected in cases.items():
        assert time_bucket(datetime(2026, 7, 1, hour, minute, tzinfo=KST)) == expected


def test_candle_metrics_handle_flat_range_and_long_upper_wick():
    flat = Bar(datetime(2026, 7, 1, 9, 15, tzinfo=KST), 100, 100, 100, 100, 1)
    assert candle_features(flat)["close_location_value"] is None

    wick = Bar(datetime(2026, 7, 1, 9, 30, tzinfo=KST), 100, 110, 90, 108, 1)
    result = candle_features(wick)
    assert result["body_ratio"] == pytest.approx(0.4)
    assert result["upper_wick_ratio"] == pytest.approx(0.1)
    assert result["lower_wick_ratio"] == pytest.approx(0.5)
    assert result["close_location_value"] == pytest.approx(0.9)


def test_future_bar_mutation_cannot_change_signal_context():
    base = datetime(2026, 7, 1, 9, 0, tzinfo=KST)
    bars = [
        Bar(base + timedelta(minutes=15 * index), 100 + index, 102 + index, 99 + index,
            101 + index, 100 + index)
        for index in range(4)
    ]
    baseline = extract_context_at_index(bars, 1, previous_session_close=95, session_open=100)
    changed = list(bars)
    changed[2] = Bar(changed[2].time, 100, 1_000_000, 1, 900_000, 9_000_000)
    changed[3] = Bar(changed[3].time, 100, 2_000_000, 1, 1_900_000, 10_000_000)
    assert extract_context_at_index(changed, 1, previous_session_close=95, session_open=100) == baseline


def test_relative_volume_and_turnover_are_measured_against_prior_bars():
    start = datetime(2026, 6, 30, 9, 0, tzinfo=KST)
    bars = [
        Bar(start, 100, 101, 99, 100, 100),
        Bar(start + timedelta(minutes=15), 100, 102, 99, 101, 200),
        Bar(start + timedelta(minutes=30), 101, 104, 100, 103, 450),
    ]
    context = extract_context_at_index(bars, 2, session_open=100)
    assert context["relative_volume"] == pytest.approx(3.0)
    assert context["volume_vs_previous_bar"] == pytest.approx(2.25)
    assert context["relative_turnover"] == pytest.approx(46_350 / 15_100)


def test_near_data_gap_flags_missing_completed_interval_bars():
    start = datetime(2026, 6, 30, 9, 0, tzinfo=KST)
    complete = [Bar(start + timedelta(minutes=15 * index), 100, 101, 99, 100, 1) for index in range(3)]
    partial = [complete[0], complete[1], Bar(start + timedelta(minutes=45), 100, 101, 99, 100, 1)]
    assert not _gap_near_event(complete, 1, complete[-1].time, 15)
    assert _gap_near_event(partial, 1, partial[-1].time, 15)


def test_event_label_distinguishes_unfilled_stop_and_time_exit():
    signal = {
        "symbol": "000001", "signal_time": "2026-07-01T09:15:00+09:00", "signal_close": 100,
        "stop_price": 98, "execution": {"status": "CLOSED", "net_pnl_krw": -2,
            "exit_reason": "STOP", "exit_time": "2026-07-01T09:30:00+09:00",
            "entry_time": "2026-07-01T09:30:00+09:00"},
    }
    context = {"signal_timestamp": signal["signal_time"], "market": "KOSPI"}
    row = _event_row(signal, context, "development", "15m")
    assert row["outcome"] == "LOSS"
    assert row["exit_label"] == "STOPPED"

    signal["signal_path_excursion"] = {"mfe_pct": .6, "mae_pct": -1.5}
    row = _event_row(signal, context, "development", "15m")
    assert row["signal_mfe_gt_1pct"] is False
    assert row["signal_mae_lt_minus_1pct"] is True

    signal["execution"] = {"status": "UNFILLED", "reason": "NO_NEXT_BAR"}
    assert _event_row(signal, context, "development", "15m")["outcome"] == "UNFILLED"


def test_frozen_loader_reads_only_development_and_validation_partitions(tmp_path: Path):
    cache_root = tmp_path / "data"
    cache = ParquetBarCache(cache_root)
    selected_days = [date(2026, 6, 30), date(2026, 7, 1)]
    hashes: list[str] = []
    for day in selected_days:
        _, metadata = cache.save(
            [Bar(datetime.combine(day, datetime.min.time(), KST).replace(hour=9),
                 100, 101, 99, 100, 10)],
            kind="minute", symbol="000001", interval="1m", market="KOSPI",
            source="test KIS partition", session_date=day,
        )
        hashes.append(f"000001:{day.isoformat()}:{metadata['sha256']}")

    # The holdout hash is manifest metadata only; no holdout Parquet is created or read.
    hashes.append(f"000001:2026-07-28:{'a' * 64}")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "fresh_holdout_state": "LOCKED_NOT_EVALUATED",
        "succeeded_symbols": ["000001"],
        "splits": {
            "development": {"sessions": ["2026-06-30"], "start": "2026-06-30", "end": "2026-06-30"},
            "validation": {"sessions": ["2026-07-01"], "start": "2026-07-01", "end": "2026-07-01"},
            "fresh_holdout": {"sessions": ["2026-07-28"], "start": "2026-07-28", "end": "2026-07-28"},
        },
        "partition_hashes": hashes,
        "quality": {
            "expected_minutes_per_session_adjusted_for_documented_kospi_halts": {
                "2026-06-30": 380, "2026-07-01": 380, "2026-07-28": 380,
            },
        },
    }))

    bars, sessions, loaded, incomplete = _load_frozen_partitions(cache_root, manifest)
    assert list(bars) == ["000001"]
    assert set(sessions["000001"]) == set(selected_days)
    assert len(loaded) == 2
    assert "000001:2026-07-28" not in loaded
    assert incomplete == {("000001", date(2026, 6, 30)), ("000001", date(2026, 7, 1))}

    payload = json.loads(manifest.read_text())
    payload["fresh_holdout_state"] = "EVALUATED"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="LOCKED_NOT_EVALUATED"):
        _load_frozen_partitions(cache_root, manifest)


def test_frozen_loader_rejects_validation_manifest_that_crosses_holdout_boundary(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "fresh_holdout_state": "LOCKED_NOT_EVALUATED",
        "succeeded_symbols": ["000001"],
        "splits": {
            "development": {"sessions": ["2026-06-30"]},
            "validation": {"sessions": ["2026-07-28"]},
            "fresh_holdout": {"sessions": ["2026-07-29"]},
        },
        "partition_hashes": [],
    }))
    with pytest.raises(ValueError, match="validation partitions exceed"):
        _load_frozen_partitions(tmp_path / "data", manifest)


def test_false_breakout_rate_and_overextension_filter_use_only_signal_context():
    records = [{
        "execution_status": "CLOSED", "outcome": "LOSS", "net_pnl_krw": -10,
        "gross_pnl_krw": -5, "fee_krw": 1, "tax_krw": 1, "slippage_krw": 3,
        "false_breakout": True, "exit_reason": "STOP",
    }]
    metrics = _trade_metrics(records)
    assert metrics["false_breakout_rate_pct"] == 100
    row = {"intraday_return_from_open_pct": 2.0, "future_close": 1_000_000}
    assert _overextension_filter(row, 2.0)
    row["future_close"] = -1_000_000
    assert _overextension_filter(row, 2.0)


def test_cost_stress_scales_only_fee_tax_and_slippage():
    metrics = _cost_stress([{
        "execution_status": "CLOSED", "gross_pnl_krw": 100,
        "fee_krw": 5, "tax_krw": 10, "slippage_krw": 5,
    }])
    assert metrics["1.0x"]["net_pnl_krw"] == 80
    assert metrics["1.5x"]["net_pnl_krw"] == 70
    assert metrics["2.0x"]["net_pnl_krw"] == 60


def test_one_share_feasibility_uses_the_project_cost_model():
    cost = CostModel(.00015, .002, 15)
    entry = cost.buy_fill_price(1_000)
    stop_fill = cost.sell_fill_price(980)
    expected_risk = entry - stop_fill + cost.buy_cost(entry) + cost.sell_cost(stop_fill)
    assert _one_share_risk_krw(1_000, 980, cost) == pytest.approx(expected_risk)
    assert _one_share_order_cap_eligible(19_950, 20_000, cost)
    assert not _one_share_order_cap_eligible(19_990, 20_000, cost)
    assert _one_share_risk_krw(1_000, 1_010, cost) == float("inf")


def test_trade_metrics_reports_median_timing_and_overnight_exits():
    metrics = _trade_metrics([{
        "execution_status": "CLOSED", "outcome": "WIN", "net_pnl_krw": 20,
        "gross_pnl_krw": 25, "fee_krw": 1, "tax_krw": 2, "slippage_krw": 2,
        "mfe_pct": 1.5, "mae_pct": -.5, "signal_mfe_pct": 1.5,
        "false_breakout": False, "exit_reason": "TIME_EXIT",
        "signal_timestamp": "2026-07-01T14:45:00+09:00",
        "exit_time": "2026-07-02T09:15:00+09:00",
        "time_to_mfe_bars": 2, "time_to_failure_bars": None,
    }])
    assert metrics["median_bars_to_mfe"] == 2
    assert metrics["median_bars_to_failure"] is None
    assert metrics["overnight_exit_count"] == 1
    assert metrics["overnight_exit_rate_pct"] == 100


def test_false_breakout_thresholds_are_derived_from_development_distribution():
    thresholds = _thresholds_from_dev([
        {"signal_mfe_pct": 1, "signal_mae_pct": -4},
        {"signal_mfe_pct": 2, "signal_mae_pct": -3},
        {"signal_mfe_pct": 3, "signal_mae_pct": -2},
        {"signal_mfe_pct": 4, "signal_mae_pct": -1},
    ])
    assert thresholds["signal_mfe_p25_pct"] == pytest.approx(1.75)
    assert thresholds["signal_mae_p50_pct"] == pytest.approx(-2.5)


def test_winner_loser_feature_summary_reports_quantiles_and_effect_size():
    comparison = _feature_comparison([
        {"outcome": "WIN", "relative_volume": 3.0},
        {"outcome": "WIN", "relative_volume": 5.0},
        {"outcome": "LOSS", "relative_volume": 1.0},
        {"outcome": "LOSS", "relative_volume": 2.0},
    ])
    assert comparison["relative_volume"]["winner"]["p50"] == 4.0
    assert comparison["relative_volume"]["loser"]["p50"] == 1.5
    assert comparison["relative_volume"]["cliffs_delta"] == 1.0


def test_variant_metrics_reports_filter_retention_and_cost_stress():
    records = [
        {"execution_status": "CLOSED", "outcome": "WIN", "net_pnl_krw": 10,
         "gross_pnl_krw": 12, "fee_krw": 1, "tax_krw": 0, "slippage_krw": 1,
         "false_breakout": False, "exit_reason": "TIME_EXIT"},
        {"execution_status": "CLOSED", "outcome": "LOSS", "net_pnl_krw": -20,
         "gross_pnl_krw": -18, "fee_krw": 1, "tax_krw": 0, "slippage_krw": 1,
         "false_breakout": True, "exit_reason": "STOP"},
    ]
    metrics = _variant_metrics(records, baseline_count=4, sessions=2)
    assert metrics["trade_retention_pct"] == 50
    assert metrics["trades_per_session"] == 1
    assert metrics["cost_stress"]["1.5x"]["net_pnl_krw"] == pytest.approx(-12)


def test_v2_comparison_attributes_removed_trades_and_preserves_holdout_lock(tmp_path: Path):
    (tmp_path / "breakout-anatomy-15m.json").write_text(json.dumps({
        "schema_version": "phase3-breakout-context-v2",
        "dataset_sha256": "a" * 64,
        "selected_partition_hash_sha256": "b" * 64,
        "holdout_integrity": {"state": "LOCKED_NOT_EVALUATED"},
        "dev_derived_thresholds": {"late_return_p75_pct": 5.0},
        "split_sessions": {"development": 2, "validation": 2},
        "winner_loser_feature_comparison": {
            "development": {"intraday_return_from_open_pct": {"winner": {}, "loser": {}}},
            "validation": {"intraday_return_from_open_pct": {"winner": {}, "loser": {}}},
        },
    }))
    def row(split, pnl, intraday_return, false_breakout):
        return {
            "split": split, "execution_status": "CLOSED",
            "outcome": "WIN" if pnl > 0 else "LOSS", "net_pnl_krw": pnl,
            "gross_pnl_krw": pnl + 5, "fee_krw": 1, "tax_krw": 2,
            "slippage_krw": 2, "false_breakout": false_breakout,
            "exit_reason": "TIME_EXIT", "intraday_return_from_open_pct": intraday_return,
            "future_close": 1_000,
        }
    events = [
        row("development", 10, 1, False), row("development", -40, 10, True),
        row("validation", 10, 2, False), row("validation", -20, 8, True),
    ]
    pq.write_table(pa.Table.from_pylist(events), tmp_path / "breakout-signals-15m.parquet")

    result = run_breakout_v2_comparison(interval="15m", report_root=tmp_path)

    dev = result["development_validation"]["development"]
    assert dev["v2_a_not_overextended"]["signals"] == 1
    assert dev["v2_a_not_overextended"]["trade_retention_pct"] == 50
    assert dev["filter_attribution"]["removed_signals"] == 1
    assert dev["filter_attribution"]["removed_closed_trade_metrics"]["net_pnl_krw"] == -40
    assert result["holdout_integrity"]["state"] == "LOCKED_NOT_EVALUATED"


def test_v2_comparison_rejects_event_dataset_containing_holdout_rows(tmp_path: Path):
    (tmp_path / "breakout-anatomy-15m.json").write_text(json.dumps({
        "schema_version": "phase3-breakout-context-v2",
        "dev_derived_thresholds": {"late_return_p75_pct": 5.0},
    }))
    pq.write_table(pa.Table.from_pylist([{"split": "fresh_holdout"}]), tmp_path / "breakout-signals-15m.parquet")
    with pytest.raises(ValueError, match="non-permitted split"):
        run_breakout_v2_comparison(interval="15m", report_root=tmp_path)

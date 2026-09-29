from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from krx_trader.config import Settings
from krx_trader.models import Bar
from krx_trader.research import phase5_backfill
from krx_trader.research.phase6 import (
    EXTERNAL_END,
    EXTERNAL_START,
    ResampledResearchBar,
    _portfolio_replay,
    _rank_percentiles,
    assert_external_evidence_allowed,
    attach_forward_outcomes,
    build_point_in_time_snapshots,
    promotion_gate,
    resample_with_quality,
    safe_research_sessions,
    simulate_variant,
)
from krx_trader.strategies.mean_reversion import (
    MeanReversionVariant,
    ReversalObservation,
    evaluate_mean_reversion,
    preregistration_mr_config,
)

KST = ZoneInfo("Asia/Seoul")


def _minute(day: date, minute: int, price: float, *, volume: int = 10) -> Bar:
    timestamp = datetime.combine(day, time(9, 0), KST) + timedelta(minutes=minute)
    return Bar(timestamp, price, price + 1, price - 1, price + 0.2, volume)


def _research_bar(timestamp: datetime, close: float, *, freshness: float = 0,
                  completeness: float = 1.0, volume: int = 100) -> ResampledResearchBar:
    bar = Bar(timestamp, close - 0.1, close + 1, close - 1, close, volume)
    return ResampledResearchBar(bar, round(completeness * 15), 15, timestamp - timedelta(minutes=15),
                                timestamp - timedelta(minutes=1 + freshness), freshness,
                                completeness, close * volume)


def _series(symbol: str, start: datetime, *, drift: float, count: int = 10) -> list[ResampledResearchBar]:
    return [_research_bar(start + timedelta(minutes=15 * index), 100 + index * drift)
            for index in range(count)]


def _reversal_observation(*, stabilized: bool = False, **changes) -> ReversalObservation:
    values = {
        "timestamp": datetime(2026, 6, 1, 10, 0, tzinfo=KST),
        "symbol": "000001",
        "close_price": 98.0,
        "stop_price": 95.0,
        "prior_percentile": 10.0,
        "prior_short_return": -0.04,
        "rank_delta_points": 15.0,
        "current_bar_return": 0.01,
        "close_location": 0.8,
        "turnover_percentile": 50.0,
        "freshness_minutes": 0.0,
        "completeness": 1.0,
        "stopped_making_local_low": stabilized,
    }
    values.update(changes)
    return ReversalObservation(**values)


def test_laggard_percentiles_use_rank_order_and_shared_midrank_for_ties():
    result = _rank_percentiles({"000003": -0.1, "000001": 0.0, "000002": 0.0})
    assert result["000003"] == pytest.approx(100 / 3)
    assert result["000001"] == result["000002"] == pytest.approx(250 / 3)
    reversed_result = _rank_percentiles({"000002": 0.0, "000001": 0.0, "000003": -0.1})
    assert result == reversed_result


def test_resample_tracks_missing_minutes_freshness_completeness_and_turnover():
    day = date(2026, 6, 1)
    source = [_minute(day, minute, 100 + minute) for minute in range(14)]
    value = resample_with_quality(source, 15)[0]
    assert value.bar.time == datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    assert value.observed_minute_count == 14
    assert value.expected_minute_count == 15
    assert value.first_observed_minute.minute == 0
    assert value.last_observed_minute.minute == 13
    assert value.freshness_minutes == pytest.approx(1)
    assert value.completeness == pytest.approx(14 / 15)
    assert value.turnover_krw == pytest.approx(sum(bar.close * bar.volume for bar in source))


def test_rank_population_excludes_stale_and_missing_current_interval_symbols():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    values = {symbol: _series(symbol, start, drift=drift)
              for symbol, drift in (("000001", 1), ("000002", 0), ("000003", -1))}
    values["000003"][-1] = _research_bar(start + timedelta(minutes=15 * 9), 91, freshness=2)
    values.pop("000002")
    snapshots, participants = build_point_in_time_snapshots(
        values, interval_minutes=15, market_by_symbol={s: "KOSPI" for s in values}, minimum_participants=2,
    )
    final_time = (start + timedelta(minutes=15 * 9)).isoformat()
    assert participants[final_time] == 1
    assert not any(row["timestamp"] == final_time for row in snapshots)


def test_clean_sensitivity_excludes_low_completeness_observations():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    values = {symbol: _series(symbol, start, drift=drift)
              for symbol, drift in (("000001", 1), ("000002", 0), ("000003", -1))}
    values["000003"][-1] = _research_bar(start + timedelta(minutes=15 * 9), 91, completeness=0.5)
    snapshots, _ = build_point_in_time_snapshots(
        values, interval_minutes=15, market_by_symbol={s: "KOSPI" for s in values}, minimum_participants=2,
        clean_only=True,
    )
    target = (start + timedelta(minutes=15 * 9)).isoformat()
    assert not any(row["symbol"] == "000003" and row["timestamp"] == target for row in snapshots)


def test_missing_symbol_is_not_forward_filled_and_participant_count_is_exact():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    values = {symbol: _series(symbol, start, drift=drift)
              for symbol, drift in (("000001", 1), ("000002", 0), ("000003", -1))}
    values["000003"] = values["000003"][:-1]
    _, participants = build_point_in_time_snapshots(
        values, interval_minutes=15, market_by_symbol={s: "KOSPI" for s in values}, minimum_participants=2,
    )
    assert participants[(start + timedelta(minutes=15 * 9)).isoformat()] == 2


def test_input_symbol_order_does_not_change_ranks_or_signals():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    values = {symbol: _series(symbol, start, drift=drift)
              for symbol, drift in (("000001", 1), ("000002", 0), ("000003", -1))}
    markets = {symbol: "KOSDAQ" for symbol in values}
    normal, _ = build_point_in_time_snapshots(values, interval_minutes=15, market_by_symbol=markets,
                                               minimum_participants=3)
    reversed_rows, _ = build_point_in_time_snapshots(dict(reversed(list(values.items())),), interval_minutes=15,
                                                      market_by_symbol=markets, minimum_participants=3)
    project = lambda rows: [(row["symbol"], row["timestamp"], row["percentile_short"], row["rank_delta_points"])
                            for row in rows]
    assert project(normal) == project(reversed_rows)


def test_rank_recovery_is_measured_against_the_previous_completed_rank():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    symbols = [f"{index:06d}" for index in range(20)]
    values = {}
    for index, symbol in enumerate(symbols):
        drift = (index - 1) / 10
        closes = [100 + bar_index * drift for bar_index in range(10)]
        if index == 0:
            closes[-1] = closes[-2] + 5
        values[symbol] = [_research_bar(start + timedelta(minutes=15 * bar_index), close)
                          for bar_index, close in enumerate(closes)]
    rows, _ = build_point_in_time_snapshots(
        values, interval_minutes=15, market_by_symbol={symbol: "KOSPI" for symbol in symbols},
        minimum_participants=15,
    )
    current_time = (start + timedelta(minutes=15 * 9)).isoformat()
    recovering = next(row for row in rows if row["symbol"] == symbols[0] and row["timestamp"] == current_time)
    assert recovering["prior_percentile_short"] <= 20
    assert recovering["rank_delta_points"] >= 10
    assert recovering["current_bar_return"] > 0


def test_cross_section_skips_timestamps_below_participant_minimum():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    values = {symbol: _series(symbol, start, drift=drift)
              for symbol, drift in (("000001", -1), ("000002", 0), ("000003", 1))}
    rows, participant_counts = build_point_in_time_snapshots(
        values, interval_minutes=15, market_by_symbol={symbol: "KOSPI" for symbol in values},
        minimum_participants=4,
    )
    assert rows == []
    assert participant_counts
    assert max(participant_counts.values()) == 3


def test_future_bar_mutation_cannot_change_current_features():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    values = {symbol: _series(symbol, start, drift=drift, count=12)
              for symbol, drift in (("000001", 1), ("000002", 0), ("000003", -1))}
    baseline, _ = build_point_in_time_snapshots(values, interval_minutes=15,
        market_by_symbol={s: "KOSPI" for s in values}, minimum_participants=3)
    mutated = {symbol: list(series) for symbol, series in values.items()}
    future = mutated["000001"][-1]
    mutated["000001"][-1] = _research_bar(future.bar.time, 999999)
    changed, _ = build_point_in_time_snapshots(mutated, interval_minutes=15,
        market_by_symbol={s: "KOSPI" for s in values}, minimum_participants=3)
    target = (start + timedelta(minutes=15 * 8)).isoformat()
    select = lambda rows: [(r["symbol"], r["percentile_short"], r["current_bar_return"])
                           for r in rows if r["timestamp"] == target]
    assert select(baseline) == select(changed)


def test_other_symbol_future_mutation_cannot_change_current_cross_sectional_rank():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    values = {symbol: _series(symbol, start, drift=drift, count=12)
              for symbol, drift in (("000001", 1), ("000002", 0), ("000003", -1))}
    base, _ = build_point_in_time_snapshots(values, interval_minutes=15,
        market_by_symbol={s: "KOSPI" for s in values}, minimum_participants=3)
    mutated = {symbol: list(series) for symbol, series in values.items()}
    last = mutated["000003"][-1]
    mutated["000003"][-1] = _research_bar(last.bar.time, 1_000_000)
    changed, _ = build_point_in_time_snapshots(mutated, interval_minutes=15,
        market_by_symbol={s: "KOSPI" for s in values}, minimum_participants=3)
    target = (start + timedelta(minutes=15 * 8)).isoformat()
    base_rank = next(row["percentile_short"] for row in base
                     if row["symbol"] == "000001" and row["timestamp"] == target)
    changed_rank = next(row["percentile_short"] for row in changed
                        if row["symbol"] == "000001" and row["timestamp"] == target)
    assert base_rank == changed_rank


def test_stabilization_features_are_independent_and_recorded():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    values = {symbol: _series(symbol, start, drift=drift, count=10)
              for symbol, drift in (("000001", -1), ("000002", 0), ("000003", 1))}
    current = values["000001"][-1]
    # The latest close rises off a prior local low and closes in the bar's upper half.
    values["000001"][-1] = _research_bar(current.bar.time, current.bar.close + 2)
    rows, _ = build_point_in_time_snapshots(values, interval_minutes=15,
        market_by_symbol={s: "KOSPI" for s in values}, minimum_participants=3)
    final = next(row for row in rows if row["symbol"] == "000001" and row["timestamp"] == current.bar.time.isoformat())
    assert isinstance(final["stabilization"]["new_local_low"], bool)
    assert isinstance(final["stabilization"]["rank_deterioration_stopped"], bool)
    assert isinstance(final["stabilization"]["session_low_distance_recovery"], bool)


def test_mr_a_requires_laggard_recovery_and_price_reversal():
    assert evaluate_mean_reversion(_reversal_observation(), variant=MeanReversionVariant.MR_A)
    assert evaluate_mean_reversion(_reversal_observation(prior_percentile=30), variant=MeanReversionVariant.MR_A) is None
    assert evaluate_mean_reversion(_reversal_observation(current_bar_return=-0.01), variant=MeanReversionVariant.MR_A) is None
    assert evaluate_mean_reversion(_reversal_observation(rank_delta_points=5), variant=MeanReversionVariant.MR_A) is None


def test_mr_b_adds_local_low_stabilization_to_mr_a():
    assert evaluate_mean_reversion(_reversal_observation(stabilized=True), variant=MeanReversionVariant.MR_B)
    assert evaluate_mean_reversion(_reversal_observation(stabilized=False), variant=MeanReversionVariant.MR_B) is None
    assert evaluate_mean_reversion(_reversal_observation(stabilized=False), variant=MeanReversionVariant.MR_A)


def test_preregistration_records_the_fixed_structural_stop_rule():
    config = preregistration_mr_config(MeanReversionVariant.MR_A)
    assert "stop_lookback_bars" not in config["config"]
    assert config["structural_stop_rule"] == "MIN_LOW_OF_SIGNAL_BAR_AND_PRIOR_2_VALID_SAME_SESSION_BARS"


def test_freshness_and_liquidity_guards_exclude_stale_or_illiquid_signals():
    assert evaluate_mean_reversion(_reversal_observation(freshness_minutes=2), variant=MeanReversionVariant.MR_A) is None
    assert evaluate_mean_reversion(_reversal_observation(turnover_percentile=10), variant=MeanReversionVariant.MR_A) is None


def test_next_bar_entry_and_forward_labels_use_future_bar_open():
    start = datetime(2026, 6, 1, 10, 0, tzinfo=KST)
    bars = [_research_bar(start + timedelta(minutes=15 * index), 100 + index)
            for index in range(3)]
    current = {"symbol": "000001", "timestamp": start.isoformat(), "percentile_short": 10.0}
    rows = attach_forward_outcomes([current], {"000001": bars}, interval_minutes=15)
    assert rows[0]["ret_fwd_1"] == pytest.approx((bars[1].bar.close - bars[1].bar.open) / bars[1].bar.open)
    assert rows[0]["entry_delay_intervals"] == pytest.approx(1.0)


def test_signal_simulation_enters_at_next_executable_open_and_cost_stress_worsens_net():
    start = datetime(2026, 6, 1, 9, 15, tzinfo=KST)
    series = [_research_bar(start + timedelta(minutes=15 * index), close)
              for index, close in enumerate((100, 99, 98, 97, 96, 95, 96, 98, 99, 100, 101, 102))]
    signal_time = series[6].bar.time
    rows = [{
        "timestamp": signal_time.isoformat(), "session_date": signal_time.date().isoformat(),
        "symbol": "000001", "close": 96.0, "stop_price": 94.0,
        "prior_percentile_short": 10.0, "prior_short_return": -0.04,
        "rank_delta_points": 15.0, "current_bar_return": 0.02,
        "close_location": 0.8, "turnover_percentile": 50.0,
        "freshness_minutes": 0.0, "completeness": 1.0,
        "stabilization": {"lower_low_stopped": True}, "time_of_day": "10:00-11:00",
    }]
    bars = {"000001": series}
    base = simulate_variant(rows, bars, variant=MeanReversionVariant.MR_A)
    middle_stress = simulate_variant(rows, bars, variant=MeanReversionVariant.MR_A, cost_multiplier=1.5)
    stress = simulate_variant(rows, bars, variant=MeanReversionVariant.MR_A, cost_multiplier=2.0)
    assert base["closed_trades"] == 1
    assert base["trades_detail"][0]["entry_time"] == series[7].bar.time.isoformat()
    assert base["net_expectancy_pct"] > middle_stress["net_expectancy_pct"] > stress["net_expectancy_pct"]


def test_external_and_protected_period_guards_fail_closed(tmp_path: Path):
    manifest = tmp_path / "splits.json"
    manifest.write_text(json.dumps({"splits": {
        "development": {"sessions": ["2026-06-30"]},
        "validation": {"sessions": ["2026-07-28"]},
    }}))
    with pytest.raises(ValueError, match="HOLDOUT"):
        safe_research_sessions(manifest)
    allowed_dates = [EXTERNAL_START, EXTERNAL_END]
    assert_external_evidence_allowed(candidate_preregistered=True,
                                     strategy_freeze_commit="abc123", sessions=allowed_dates)
    with pytest.raises(ValueError, match="preregistered"):
        assert_external_evidence_allowed(candidate_preregistered=False,
                                         strategy_freeze_commit="abc123", sessions=allowed_dates)
    with pytest.raises(ValueError, match="freeze commit"):
        assert_external_evidence_allowed(candidate_preregistered=True,
                                         strategy_freeze_commit=None, sessions=allowed_dates)
    with pytest.raises(ValueError, match="protected"):
        assert_external_evidence_allowed(candidate_preregistered=True,
                                         strategy_freeze_commit="abc123", sessions=[date(2026, 7, 28)])


def test_promotion_gate_requires_positive_gross_net_sample_and_clean_sensitivity():
    primary = {"closed_trades": 40, "gross_expectancy_pct": 0.1, "net_expectancy_pct": 0.02,
               "profit_factor_net": 1.2, "top_symbol_positive_pnl_share": 0.2,
               "top_day_positive_pnl_share": 0.2, "top_time_bucket_positive_pnl_share": 0.4,
               "distinct_time_buckets": 4}
    clean = {"gross_expectancy_pct": 0.05, "net_expectancy_pct": 0.01}
    assert promotion_gate(primary, clean)["status"] == "PASS"
    clean["net_expectancy_pct"] = -0.01
    assert promotion_gate(primary, clean)["status"] == "REJECTED"


def test_portfolio_replay_uses_existing_whole_share_bounded_framework():
    start = datetime(2026, 6, 1, 10, 0, tzinfo=KST)
    values = [_research_bar(start + timedelta(minutes=15 * index), close)
              for index, close in enumerate((100, 103, 104))]
    row = {
        "timestamp": start.isoformat(), "symbol": "000001", "close": 100.0,
        "stop_price": 90.0, "prior_percentile_short": 10.0,
        "prior_short_return": -0.04, "rank_delta_points": 15.0,
        "current_bar_return": 0.02, "close_location": 0.8,
        "turnover_percentile": 50.0, "freshness_minutes": 0.0,
        "completeness": 1.0, "stabilization": {"lower_low_stopped": True},
    }
    result = _portfolio_replay([row], {"000001": values}, variant=MeanReversionVariant.MR_A)
    assert result["status"] == "RUN"
    assert result["whole_shares"] is True
    assert result["orders_api_calls"] == 0
    assert result["paper_or_live_trading"] is False


def test_backfill_daily_failure_manifest_path_does_not_crash_on_missing_skip_count(tmp_path: Path,
                                                                                   monkeypatch, capsys):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"cohort_60": {"symbols": ["000001"]}}))
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"splits": {
        "development": {"sessions": ["2026-06-30"]},
        "validation": {"sessions": []},
    }}))
    monkeypatch.setattr(phase5_backfill.Settings, "from_env", classmethod(lambda cls: Settings()))
    monkeypatch.setattr(phase5_backfill, "TokenManager", lambda *args, **kwargs: object())
    monkeypatch.setattr(phase5_backfill, "KisRestClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(phase5_backfill, "backfill_symbol_sessions", lambda *args, **kwargs: {
        "symbol": "000001", "status": "DAILY_FETCH_FAILED", "error": "ValueError",
        "succeeded_sessions": 0, "failed_sessions": 1,
    })
    output = tmp_path / "phase6-acquisition-manifest.json"
    result = phase5_backfill.run_phase5_backfill(
        cohort_manifest_path=cohort,
        split_manifest_path=split,
        cache_root=tmp_path / "data",
        status_output_path=output,
    )
    assert result["status"] == "PARTIAL"
    assert result["symbol_results"]["000001"]["status"] == "DAILY_FETCH_FAILED"
    assert "skipped 0" in capsys.readouterr().out
    assert json.loads(output.read_text())["holdout_integrity"]["holdout_dates_requested"] == 0

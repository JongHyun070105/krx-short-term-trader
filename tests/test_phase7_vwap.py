from __future__ import annotations

import gzip
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from krx_trader.models import Bar
from krx_trader.research.phase5_backfill import assert_safe_research_date
from krx_trader.research.phase6 import assert_external_evidence_allowed
from krx_trader.research.phase7_vwap import (
    VWAP_FORMULA,
    _answer_questions,
    _build_formula_audit,
    _matched_baseline,
    _session_classification,
    _strategy_gate,
    _write_jsonl_gz,
    attach_forward_outcomes,
    build_interval_observations,
    canonical_sha256,
    compute_intraday_vwap,
    compute_session_vwap,
)

KST = ZoneInfo("Asia/Seoul")
DAY = date(2026, 6, 1)


def _minute(
    minute: int,
    price: float,
    *,
    volume: int = 10,
    low: float | None = None,
    high: float | None = None,
    day: date = DAY,
) -> Bar:
    timestamp = datetime.combine(day, time(9, 0), KST) + timedelta(minutes=minute)
    return Bar(timestamp, price, high if high is not None else price,
               low if low is not None else price, price, volume)


def _interval_minutes(prices: list[float], *, day: date = DAY) -> list[Bar]:
    bars = []
    for interval, price in enumerate(prices):
        for offset in range(15):
            bars.append(_minute(interval * 15 + offset, price, day=day))
    return bars


def _outcome_row(index: int, *, close: float, open_: float | None = None, vwap: float = 100.0) -> dict:
    return {
        "symbol": "000001",
        "session": DAY.isoformat(),
        "timestamp": (datetime.combine(DAY, time(9, 15), KST) + timedelta(minutes=15 * index)).isoformat(),
        "open": close if open_ is None else open_,
        "high": max(close, open_ or close) + 1,
        "low": min(close, open_ or close) - 1,
        "close": close,
        "session_vwap_proxy": vwap,
        "up_reclaim": index == 0,
        "acceptance_next_close_above_vwap": None,
        "acceptance_next_low_touch_hold": None,
        "acceptance_next_close_above_reclaim": None,
        "acceptance_two_consecutive_closes_above_vwap": None,
    }


def test_hand_computable_typical_price_volume_vwap_proxy():
    bars = [
        Bar(datetime(2026, 6, 1, 9, 0, tzinfo=KST), 100, 101, 99, 100, 10),
        Bar(datetime(2026, 6, 1, 9, 1, tzinfo=KST), 101, 102, 100, 101, 10),
    ]
    points = compute_session_vwap(bars)
    assert points[0].cumulative_proxy_turnover == pytest.approx(1_000)
    assert points[0].vwap == pytest.approx(100)
    assert points[1].cumulative_proxy_turnover == pytest.approx(2_010)
    assert points[1].cumulative_volume == 20
    assert points[1].vwap == pytest.approx(100.5)


def test_formula_audit_labels_ohlcv_calculation_as_proxy():
    result = _build_formula_audit(
        source_git_sha="a" * 40,
        code_sha256="b" * 64,
        sample_schema=["timestamp", "open", "high", "low", "close", "volume"],
    )
    assert result["exact_or_proxy"] == "PROXY"
    assert result["vwap_source"] == "OHLCV_PROXY"
    assert result["vwap_formula"] == VWAP_FORMULA
    assert result["observed_turnover_fields"] == []
    assert "not transaction-level" in result["accuracy_limitation"]


def test_session_state_resets_for_each_krx_session():
    bars = [
        _minute(0, 100, volume=10, day=date(2026, 6, 1)),
        _minute(0, 200, volume=10, day=date(2026, 6, 2)),
    ]
    results = compute_intraday_vwap(bars)
    assert results[date(2026, 6, 1)][0].vwap == pytest.approx(100)
    assert results[date(2026, 6, 2)][0].vwap == pytest.approx(200)
    assert results[date(2026, 6, 2)][0].cumulative_volume == 10


def test_zero_volume_has_no_division_and_no_valid_vwap_signal():
    points = compute_session_vwap([_minute(0, 100, volume=0), _minute(1, 101, volume=0)])
    assert [point.vwap for point in points] == [None, None]
    later = compute_session_vwap([_minute(0, 100, volume=0), _minute(1, 102, volume=5)])
    assert later[0].vwap is None
    assert later[1].vwap == pytest.approx(102)


def test_missing_minute_is_not_synthesized_or_forward_filled():
    bars = [_minute(0, 100), _minute(2, 102)]
    points = compute_session_vwap(bars)
    assert len(points) == 2
    assert [point.timestamp.minute for point in points] == [0, 2]
    assert [point.cumulative_volume for point in points] == [10, 20]
    observations = build_interval_observations(
        {"000001": bars}, market_by_symbol={"000001": "KOSPI"}, interval_minutes=15
    )
    assert observations == []


def test_30m_emits_only_a_complete_thirty_minute_bar():
    full = _interval_minutes([100, 101])
    observations = build_interval_observations(
        {"000001": full}, market_by_symbol={"000001": "KOSPI"}, interval_minutes=30
    )
    assert len(observations) == 1
    assert observations[0]["timestamp"].endswith("09:30:00+09:00")
    missing = [bar for bar in full if bar.time.minute != 20]
    assert build_interval_observations(
        {"000001": missing}, market_by_symbol={"000001": "KOSPI"}, interval_minutes=30
    ) == []


def test_vwap_is_point_in_time_to_future_price_and_future_volume_mutations():
    original = [_minute(0, 100, volume=10), _minute(1, 102, volume=20)]
    baseline = compute_session_vwap(original)
    changed_price = [original[0], _minute(1, 999, volume=20)]
    changed_volume = [original[0], _minute(1, 102, volume=2_000_000)]
    assert compute_session_vwap(changed_price)[0] == baseline[0]
    assert compute_session_vwap(changed_volume)[0] == baseline[0]
    assert compute_session_vwap(changed_price)[1].vwap != baseline[1].vwap
    assert compute_session_vwap(changed_volume)[1].vwap != baseline[1].vwap


def test_other_symbol_future_bars_cannot_change_current_symbol_vwap():
    first = _interval_minutes([100, 90, 100, 100, 100])
    other_before = _interval_minutes([100, 110, 120, 130, 140])
    other_after = _interval_minutes([100, 110, 120, 130, 9_999_999])
    baseline = build_interval_observations(
        {"000001": first, "000002": other_before},
        market_by_symbol={"000001": "KOSPI", "000002": "KOSPI"}, interval_minutes=15,
    )
    changed = build_interval_observations(
        {"000001": first, "000002": other_after},
        market_by_symbol={"000001": "KOSPI", "000002": "KOSPI"}, interval_minutes=15,
    )
    select = lambda rows: [row for row in rows if row["symbol"] == "000001"]
    assert select(baseline) == select(changed)


def test_reclaim_loss_and_first_second_third_sequence_are_deterministic():
    prices = [100, 90, 100, 90, 100, 90, 100]
    rows = build_interval_observations(
        {"000001": _interval_minutes(prices)},
        market_by_symbol={"000001": "KOSPI"}, interval_minutes=15,
    )
    reclaims = [row for row in rows if row["up_reclaim"]]
    losses = [row for row in rows if row["down_cross"]]
    assert len(reclaims) == 3
    assert [row["reclaim_type"] for row in reclaims] == [
        "FIRST_RECLAIM", "SECOND_RECLAIM", "THIRD_PLUS_RECLAIM"
    ]
    assert [row["reclaim_sequence"] for row in reclaims] == [1, 2, 3]
    assert [row["reclaim_event_id"] for row in reclaims] == [
        f"000001:{DAY}:1", f"000001:{DAY}:2", f"000001:{DAY}:3"
    ]
    assert len(losses) >= 2
    assert rows[0]["vwap_age_minutes"] == 15


def test_zero_volume_bar_does_not_create_a_reclaim_signal():
    bars = _interval_minutes([100, 90])
    bars.extend(_minute(30 + offset, 100, volume=0) for offset in range(15))
    rows = build_interval_observations(
        {"000001": bars}, market_by_symbol={"000001": "KOSPI"}, interval_minutes=15
    )
    assert rows[1]["close_to_vwap_pct"] < 0
    assert rows[2]["volume"] == 0
    assert rows[2]["up_reclaim"] is False
    assert rows[2]["reclaim_sequence"] is None


def test_acceptance_failure_and_next_executable_bar_return_are_separate_from_signal_bar():
    values = [
        _outcome_row(0, close=100),
        _outcome_row(1, open_=120, close=121, vwap=100),
        _outcome_row(2, close=99, vwap=100),
        _outcome_row(3, close=98, vwap=100),
        _outcome_row(4, close=97, vwap=100),
        _outcome_row(5, close=96, vwap=100),
        _outcome_row(6, close=95, vwap=100),
        _outcome_row(7, close=94, vwap=100),
        _outcome_row(8, close=93, vwap=100),
    ]
    values[1]["low"] = 99
    result = attach_forward_outcomes(values)
    first = result[0]
    assert first["forward_1bar_gross_return_pct"] == pytest.approx((121 / 120 - 1) * 100)
    assert first["forward_1bar_gross_return_pct"] != pytest.approx((121 / 100 - 1) * 100)
    assert first["acceptance_next_close_above_vwap"] is True
    assert first["acceptance_next_low_touch_hold"] is True
    assert first["acceptance_next_close_above_reclaim"] is True
    assert first["vwap_recross_within_1bar"] is False
    assert first["vwap_recross_within_2bar"] is True
    assert first["vwap_recross_after_acceptance_a_within_1bar"] is True
    assert first["acceptance_a_entry_1bar_gross_return_pct"] == pytest.approx(0.0)


def test_two_bar_acceptance_and_post_acceptance_failure_use_the_next_bar():
    values = [
        _outcome_row(0, close=100),
        _outcome_row(1, close=101, vwap=100),
        _outcome_row(2, close=102, vwap=100),
        _outcome_row(3, open_=103, close=99, vwap=100),
        _outcome_row(4, close=98, vwap=100),
        _outcome_row(5, close=97, vwap=100),
        _outcome_row(6, close=96, vwap=100),
        _outcome_row(7, close=95, vwap=100),
    ]
    first = attach_forward_outcomes(values)[0]
    assert first["acceptance_two_consecutive_closes_above_vwap"] is True
    assert first["vwap_recross_after_acceptance_d_within_1bar"] is True
    assert first["acceptance_d_entry_1bar_gross_return_pct"] == pytest.approx((99 / 103 - 1) * 100)


def test_session_confidence_thresholds_are_fixed_at_380_and_361_slots():
    def session_rows(count: int) -> list[Bar]:
        return [_minute(index, 100, volume=1) for index in range(count)]

    assert _session_classification(session_rows(380))[2] == "HIGH_CONFIDENCE"
    assert _session_classification(session_rows(361))[2] == "PARTIAL"
    assert _session_classification(session_rows(360))[2] == "UNRELIABLE"


def test_development_gate_has_pass_weak_fail_and_insufficient_states():
    def anatomy(*, n: int, gross: float, baseline: float, high: float, accepted_failure: float):
        return {
            "event_count_with_4bar_outcome": n,
            "reclaim_summary": {"forward_4bar": {"mean_pct": gross}},
            "matched_baseline": {"mean_event_minus_matched_baseline_pct": baseline},
            "high_confidence_reclaim_summary": {"count": 10, "forward_4bar": {"mean_pct": high}},
            "failure_rates": {"within_4bar": 0.60},
            "acceptance": {"A_next_close_above_vwap": {"post_confirmation_4bar_failure_rate": accepted_failure}},
        }

    assert _strategy_gate(anatomy(n=100, gross=0.1, baseline=0.1, high=0.1, accepted_failure=0.4))["status"] == "PASS"
    assert _strategy_gate(anatomy(n=100, gross=0.1, baseline=-0.1, high=-0.1, accepted_failure=0.4))["status"] == "WEAK"
    assert _strategy_gate(anatomy(n=100, gross=-0.1, baseline=-0.1, high=-0.1, accepted_failure=0.4))["status"] == "FAIL"
    assert _strategy_gate(anatomy(n=99, gross=0.1, baseline=0.1, high=0.1, accepted_failure=0.4))["status"] == "INSUFFICIENT"


def test_acceptance_gross_question_compares_confirmed_entry_to_unrestricted_reclaims():
    anatomy = {
        "event_count_with_4bar_outcome": 100,
        "reclaim_summary": {"forward_4bar": {"mean_pct": -0.047}},
        "matched_baseline": {"mean_event_minus_matched_baseline_pct": -0.03},
        "first_vs_repeated_reclaim": {"first_mean_minus_repeated_mean_4bar_pct": None},
        "time_below_vwap": {},
        "vwap_slope": {},
        "acceptance": {"A_next_close_above_vwap": {
            "events": 60,
            "post_confirmation_4bar_failure_rate": 0.476,
            "next_executable_bar_after_confirmation_4bar_mean_gross_pct": 0.036,
            "confirmation_entry_mean_delta_vs_immediate_pct": -0.241,
        }},
        "failure_rates": {"within_4bar": 0.622},
        "high_confidence_reclaim_count": 20,
        "high_confidence_reclaim_summary": {"forward_4bar": {"mean_pct": -0.067}},
        "interval_cost_hurdle": {"multipliers": {"1.0": {"mean_net_pct": -0.576}}},
    }
    result = _answer_questions(anatomy, "FAIL")
    assert result["Q5_acceptance_improves_gross_expectancy"] == "YES"
    assert result["Q8_vwap_a_or_b_positive_after_costs"] == "NO"
    assert result["acceptance_a_post_confirmation_delta_vs_all_reclaims_pct"] == pytest.approx(0.083)
    assert result["acceptance_a_paired_delay_delta_vs_immediate_within_accepted_events_pct"] == pytest.approx(-0.241)


def test_matched_baseline_excludes_same_symbol_session_and_uses_fixed_group():
    event = {
        "up_reclaim": True, "symbol": "000001", "session": DAY.isoformat(),
        "market": "KOSPI", "time_of_day_bucket": "10:00-11:00", "liquidity_bucket": "GE_200M_KRW",
        "forward_4bar_gross_return_pct": -0.1,
    }
    controls = [
        {**event, "up_reclaim": False, "symbol": "000001", "forward_4bar_gross_return_pct": 100.0},
        {**event, "up_reclaim": False, "symbol": "000002", "forward_4bar_gross_return_pct": 0.1},
        {**event, "up_reclaim": False, "symbol": "000003", "forward_4bar_gross_return_pct": 0.3},
    ]
    result = _matched_baseline([event, *controls])
    assert result["event_count_with_control"] == 1
    assert result["matched_baseline_mean_4bar_gross_pct"] == pytest.approx(0.2)
    assert result["mean_event_minus_matched_baseline_pct"] == pytest.approx(-0.3)


def test_vwap_age_and_missing_bar_do_not_create_a_completed_observation():
    full = _interval_minutes([100])
    missing = [bar for bar in full if bar.time.minute != 7]
    observations = build_interval_observations(
        {"000001": missing}, market_by_symbol={"000001": "KOSPI"}, interval_minutes=15
    )
    assert observations == []


def test_future_mutation_cannot_change_earlier_completed_features():
    bars = _interval_minutes([100, 90, 100, 90])
    baseline = build_interval_observations(
        {"000001": bars}, market_by_symbol={"000001": "KOSPI"}, interval_minutes=15
    )
    future = list(bars)
    future[-1] = _minute(59, 1_000_000, volume=999_999)
    changed = build_interval_observations(
        {"000001": future}, market_by_symbol={"000001": "KOSPI"}, interval_minutes=15
    )
    assert baseline[0] == changed[0]
    assert baseline[1] == changed[1]


def test_fresh_holdout_guard_rejects_protected_date():
    with pytest.raises(ValueError):
        assert_safe_research_date(date(2026, 7, 28))


def test_external_evidence_requires_preregistered_frozen_strategy():
    with pytest.raises(ValueError, match="preregistered"):
        assert_external_evidence_allowed(
            candidate_preregistered=False,
            strategy_freeze_commit=None,
            sessions=[date(2026, 1, 5)],
        )


def test_artifact_hash_and_jsonl_gzip_are_reproducible(tmp_path: Path):
    payload = {"b": [1, 2], "a": {"price": 10.0}}
    assert canonical_sha256(payload) == canonical_sha256({"a": {"price": 10.0}, "b": [1, 2]})
    rows = [{"timestamp": "2026-06-01T09:15:00+09:00", "close": 100.0}]
    first = tmp_path / "first.jsonl.gz"
    second = tmp_path / "second.jsonl.gz"
    _write_jsonl_gz(first, rows)
    _write_jsonl_gz(second, rows)
    assert first.read_bytes() == second.read_bytes()
    assert gzip.decompress(first.read_bytes()).decode().strip() == '{"close":100.0,"timestamp":"2026-06-01T09:15:00+09:00"}'

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_trader.models import Bar
from krx_trader.research import phase12 as phase12_module
from krx_trader.research.phase12 import (
    ConfirmationAccessError,
    _bounded_bars_sha256,
    _build_artifact_index,
    _is_prior_manifest_or_index,
    _verify_phase12_index,
    authorize_touched_window,
    beta_adjusted_residual,
    build_breadth_observation,
    build_market_state_rows,
    decompose_returns,
    group_stress_episodes,
    guarded_confirmation_read,
    rolling_beta,
)


def test_touched_window_guard_rejects_confirmation_before_reading_data() -> None:
    with pytest.raises(ConfirmationAccessError):
        authorize_touched_window(date(2025, 7, 1), date(2025, 12, 30))


def test_confirmation_partition_is_not_opened_before_exact_freeze(tmp_path: Path) -> None:
    payload = tmp_path / "protected-confirmation.json"
    registration = tmp_path / "phase12-preregistration.json"
    with pytest.raises(ConfirmationAccessError):
        guarded_confirmation_read(
            payload,
            "005930",
            "KOSPI",
            registration,
            current_git_sha="abc123",
            freeze_git_sha="different-sha",
        )
    assert not payload.exists()
    assert not registration.exists()


def test_confirmation_reader_materializes_only_frozen_period_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_sha = "frozen-commit-sha"
    monkeypatch.setattr(phase12_module, "PHASE11_ROOT", tmp_path / "phase11")
    monkeypatch.setattr(phase12_module, "_remote_main_sha", lambda: freeze_sha)
    cache_root = tmp_path / "phase11" / "daily_cache"
    partition = cache_root / "daily" / "005930-1d-adjusted.parquet"
    partition.parent.mkdir(parents=True)
    dates = [date(2025, 6, 30), date(2025, 7, 1), date(2025, 12, 30), date(2026, 7, 28)]
    timestamps = [
        datetime.combine(day, datetime.min.time(), ZoneInfo("Asia/Seoul")).isoformat()
        for day in dates
    ]
    table = pa.table({
        "timestamp": timestamps,
        "open": [100, 100, 100, 100],
        "high": [101, 101, 101, 101],
        "low": [99, 99, 99, 99],
        "close": [100, 100, 100, 100],
        "volume": [100, 100, 100, 100],
        "turnover_krw": [10_000, 10_000, 10_000, 10_000],
    })
    pq.write_table(table, partition)
    partition.with_suffix(".metadata.json").write_text(json.dumps({
        "symbol": "005930",
        "market": "KOSPI",
        "interval": "1d-adjusted",
    }))
    preregistration = tmp_path / "phase12-preregistration.json"
    preregistration.write_text(json.dumps({
        "candidate_status": "RESEARCH_CANDIDATE",
        "pre_confirmation_freeze_sha": freeze_sha,
        "confirmation_period": ["2025-07-01", "2025-12-30"],
        "candidate_rule": {"name": "frozen-fixture"},
        "anatomy_artifact_hashes": {"phase12-hypothesis.json": "fixture-hash"},
    }))

    bars = guarded_confirmation_read(
        partition,
        "005930",
        "KOSPI",
        preregistration,
        current_git_sha=freeze_sha,
        freeze_git_sha=freeze_sha,
    )

    assert bars is not None
    assert [bar.time.date() for bar in bars] == [date(2025, 7, 1), date(2025, 12, 30)]


def test_touched_window_guard_accepts_development_and_validation_only() -> None:
    authorize_touched_window(date(2023, 1, 2), date(2024, 6, 28))
    authorize_touched_window(date(2024, 7, 1), date(2025, 6, 30))


def test_confirmation_rows_do_not_change_touched_dataset_hash() -> None:
    touched = [
        Bar(
            time=datetime(2025, 6, 30, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            open=100,
            high=105,
            low=95,
            close=102,
            volume=10,
            turnover_krw=1_000,
        )
    ]
    changed_confirmation = Bar(
        time=datetime(2025, 7, 1, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        open=102,
        high=10_000,
        low=1,
        close=9_999,
        volume=999_999,
        turnover_krw=99_999_999,
    )
    start, end = date(2022, 11, 1), date(2025, 6, 30)

    assert _bounded_bars_sha256(touched, start=start, end=end) == _bounded_bars_sha256(
        [*touched, changed_confirmation], start=start, end=end,
    )


def test_previous_phase_immutability_scope_is_manifest_and_index_only() -> None:
    assert _is_prior_manifest_or_index(Path("phase11-dataset-manifest.json"))
    assert _is_prior_manifest_or_index(Path("phase11-artifact-index.json"))
    assert not _is_prior_manifest_or_index(Path("symbol-1d-adjusted.parquet"))
    assert not _is_prior_manifest_or_index(Path("holdout-metadata.json"))


def test_breadth_excludes_missing_symbols_and_records_coverage() -> None:
    result = build_breadth_observation(
        market="KOSPI",
        session=date(2025, 1, 2),
        eligible_count=4,
        returns_by_window={
            1: {"000001": 1.0, "000002": -1.0},
            5: {"000001": 2.0, "000002": -6.0},
            20: {"000001": None, "000002": -11.0},
        },
        minimum_coverage=0.5,
    )

    assert result["observed_count"] == 2
    assert result["coverage_ratio"] == pytest.approx(0.5)
    assert result["data_quality"] == "PASS"
    assert result["fraction_positive_today"] == pytest.approx(0.5)
    assert result["fraction_5d_return_le_minus_5pct"] == pytest.approx(0.5)
    assert result["fraction_20d_return_le_minus_10pct"] == pytest.approx(1.0)
    assert result["return_observed_counts"]["20d"] == 1


def test_breadth_is_low_quality_below_completeness_threshold() -> None:
    result = build_breadth_observation(
        market="KOSDAQ",
        session=date(2025, 1, 2),
        eligible_count=10,
        returns_by_window={1: {"000001": -1.0}, 5: {}, 20: {}},
        minimum_coverage=0.8,
    )
    assert result["observed_count"] == 1
    assert result["coverage_ratio"] == pytest.approx(0.1)
    assert result["data_quality"] == "LOW_COVERAGE"
    assert result["fraction_negative_today"] == pytest.approx(1.0)
    assert result["fraction_5d_return_positive"] is None


def test_episode_groups_adjacent_and_near_adjacent_signal_dates() -> None:
    sessions = [date.fromisoformat(value) for value in (
        "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07",
        "2025-01-08", "2025-01-09", "2025-01-10", "2025-01-13",
    )]
    episodes = group_stress_episodes(
        market="KOSPI",
        signal_dates=[sessions[0], sessions[2], sessions[6], sessions[7]],
        sessions=sessions,
        max_non_signal_sessions=2,
    )
    assert [episode["signal_dates"] for episode in episodes] == [
        ["2025-01-02", "2025-01-06"],
        ["2025-01-10", "2025-01-13"],
    ]
    assert [episode["duration_sessions"] for episode in episodes] == [3, 2]


def test_episode_grouping_depends_only_on_signal_and_session_dates() -> None:
    sessions = [date.fromisoformat(value) for value in (
        "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07",
    )]
    first = group_stress_episodes("KOSPI", [sessions[0], sessions[2]], sessions)
    second = group_stress_episodes("KOSPI", [sessions[0], sessions[2]], sessions)
    assert first == second


def test_market_return_decomposition_and_beta_residual() -> None:
    result = decompose_returns(stock_return_pct=2.0, market_return_pct=1.0)
    assert result["excess_return_pct"] == pytest.approx(1.0)
    assert beta_adjusted_residual(2.0, 1.0, 1.5) == pytest.approx(0.5)


def test_rolling_beta_uses_only_paired_completed_returns() -> None:
    market = [float(i) for i in range(1, 8)]
    stock = [2 * value + 3 for value in market]
    assert rolling_beta(stock, market, minimum_observations=5) == pytest.approx(2.0)
    assert rolling_beta(stock[:4], market[:4], minimum_observations=5) is None
    assert rolling_beta([1.0, None, 3.0], [1.0, 2.0, 3.0], minimum_observations=2) == pytest.approx(1.0)


def _synthetic_bars(days: list[date], closes: list[float]) -> list[Bar]:
    return [
        Bar(
            time=datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Asia/Seoul")),
            open=close - 0.1,
            high=close + 0.2,
            low=close - 0.2,
            close=close,
            volume=1_000,
            turnover_krw=100_000,
        )
        for day, close in zip(days, closes, strict=True)
    ]


def test_signal_market_state_at_t_is_unchanged_by_t_plus_1_edits() -> None:
    days = [date(2022, 11, 1) + timedelta(days=offset) for offset in range(100)]
    closes = [100.0 + offset * 0.2 for offset in range(len(days))]
    index_bars = _synthetic_bars(days, closes)
    stock_bars = _synthetic_bars(days, [value * 1.1 for value in closes])
    signal_day = date(2023, 1, 3)
    signal_index = days.index(signal_day)
    altered_closes = closes[:signal_index + 1] + [10_000.0] * (len(closes) - signal_index - 1)
    altered_stock_closes = [value * 1.1 for value in closes[:signal_index + 1]] + [
        20_000.0
    ] * (len(closes) - signal_index - 1)
    altered_indexes = _synthetic_bars(days, altered_closes)
    altered_stocks = _synthetic_bars(days, altered_stock_closes)

    baseline, _, _ = build_market_state_rows(
        prices_by_symbol={"S1": stock_bars},
        indexes={"KOSPI": index_bars, "KOSDAQ": []},
        market_by_symbol={"S1": "KOSPI"},
    )
    changed_future, _, _ = build_market_state_rows(
        prices_by_symbol={"S1": altered_stocks},
        indexes={"KOSPI": altered_indexes, "KOSDAQ": []},
        market_by_symbol={"S1": "KOSPI"},
    )
    before = next(row for row in baseline if row["date"] == signal_day.isoformat())
    after = next(row for row in changed_future if row["date"] == signal_day.isoformat())
    assert before == after


def test_market_state_aggregate_is_deterministic_under_symbol_input_reordering() -> None:
    days = [date(2022, 11, 1) + timedelta(days=offset) for offset in range(80)]
    first = _synthetic_bars(days, [100.0 + i for i in range(len(days))])
    second = _synthetic_bars(days, [200.0 + i * 2 for i in range(len(days))])
    common = {"indexes": {"KOSPI": _synthetic_bars(days, [300.0 + i for i in range(len(days))]), "KOSDAQ": []},
              "market_by_symbol": {"S1": "KOSPI", "S2": "KOSPI"}}
    first_order = build_market_state_rows(prices_by_symbol={"S1": first, "S2": second}, **common)
    reverse_order = build_market_state_rows(prices_by_symbol={"S2": second, "S1": first}, **common)
    assert first_order[0] == reverse_order[0]
    assert first_order[1] == reverse_order[1]


def test_market_breadth_is_grouped_by_market() -> None:
    kospi = build_breadth_observation(
        market="KOSPI", session=date(2025, 1, 2), eligible_count=2,
        returns_by_window={1: {"A": 1.0, "B": -1.0}}, minimum_coverage=1.0,
    )
    kosdaq = build_breadth_observation(
        market="KOSDAQ", session=date(2025, 1, 2), eligible_count=2,
        returns_by_window={1: {"C": -1.0, "D": -1.0}}, minimum_coverage=1.0,
    )
    assert kospi["market"] == "KOSPI" and kospi["fraction_positive_today"] == pytest.approx(0.5)
    assert kosdaq["market"] == "KOSDAQ" and kosdaq["fraction_positive_today"] == pytest.approx(0.0)


def test_artifact_index_detects_changes_after_indexing(tmp_path: Path) -> None:
    artifact = tmp_path / "result.json"
    artifact.write_text('{"value": 1}\n', encoding="utf-8")
    index = _build_artifact_index(tmp_path, {"source_git_sha": "fixture"})
    (tmp_path / "phase12-artifact-index.json").write_text("{}\n", encoding="utf-8")
    assert _verify_phase12_index(tmp_path, index)["status"] == "PASS"
    artifact.write_text('{"value": 2}\n', encoding="utf-8")
    assert _verify_phase12_index(tmp_path, index)["status"] == "FAIL"

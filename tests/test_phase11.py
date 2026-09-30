from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from krx_trader.models import Bar
from krx_trader.research.phase11 import (
    CONFIRMATION,
    POOL_END,
    POOL_START,
    VALIDATION,
    WARMUP_START,
    _cluster_bootstrap,
    _metrics,
    acquire_phase11_data,
    assert_phase11_pool_date,
    build_daily_features,
    build_factor_maps,
    build_outcomes,
    compare_hash_snapshots,
    load_phase11_cache,
    non_overlapping_events,
    normalize_flow,
    run_phase11,
    snapshot_previous_phase_manifests,
)

KST = ZoneInfo("Asia/Seoul")


def _bar(session: date, open_price: float, close: float, *, volume: int = 1000, turnover: int | None = 100_000) -> Bar:
    return Bar(
        datetime.combine(session, time.min, KST),
        open_price, max(open_price, close) + 1, min(open_price, close) - 1,
        close, volume, turnover,
    )


def _sessions(count: int) -> list[date]:
    return [date(2023, 1, 2) + timedelta(days=index) for index in range(count)]


def _series(sessions: list[date], *, step: float = 1.0) -> list[Bar]:
    return [
        _bar(session, 100 + index * step, 100 + index * step + 0.5,
             volume=1000 + index * 10, turnover=100_000 + index * 1_000)
        for index, session in enumerate(sessions)
    ]


def _feature(rows: list[dict], session: date, symbol: str = "000001") -> dict:
    return next(row for row in rows if row["date"] == session.isoformat() and row["symbol"] == symbol)


def test_phase11_date_guard_allows_pool_and_warmup_but_blocks_2026_evidence() -> None:
    assert_phase11_pool_date(WARMUP_START)
    assert_phase11_pool_date(POOL_START)
    assert_phase11_pool_date(POOL_END)
    with pytest.raises(ValueError, match="2023-2025 pool"):
        assert_phase11_pool_date(date(2026, 1, 5))
    with pytest.raises(ValueError, match="2023-2025 pool"):
        assert_phase11_pool_date(date(2026, 7, 28))


def test_flow_timestamp_alignment_normalization_and_missing_values() -> None:
    signal = date(2024, 5, 6)
    assert normalize_flow(2_000_000, 100_000_000, flow_session=signal, signal_session=signal) == 2.0
    assert normalize_flow(0, 100_000_000, flow_session=signal, signal_session=signal) == 0.0
    assert normalize_flow(None, 100_000_000, flow_session=signal, signal_session=signal) is None
    assert normalize_flow(2_000_000, None, flow_session=signal, signal_session=signal) is None
    assert normalize_flow(2_000_000, 0, flow_session=signal, signal_session=signal) is None
    assert normalize_flow(2_000_000, 100_000_000, flow_session=None, signal_session=signal) is None
    with pytest.raises(ValueError, match="later than"):
        normalize_flow(2_000_000, 100_000_000, flow_session=signal + timedelta(days=1), signal_session=signal)


def test_completed_day_features_do_not_change_when_later_rows_change() -> None:
    sessions = _sessions(30)
    stock = _series(sessions)
    index = _series(sessions, step=0.25)
    before = build_daily_features(
        prices_by_symbol={"000001": stock}, indexes={"KOSPI": index, "KOSDAQ": index},
        symbols=["000001"], market_by_symbol={"000001": "KOSPI"}, sessions=sessions,
    )
    changed = list(stock)
    future = changed[25]
    changed[25] = _bar(future.time.date(), future.open * 5, future.close * 8, volume=9_999_999, turnover=9_999_999_999)
    changed_indexes = list(index)
    future_index = changed_indexes[25]
    changed_indexes[25] = _bar(future_index.time.date(), 50, 900)
    after = build_daily_features(
        prices_by_symbol={"000001": changed},
        indexes={"KOSPI": changed_indexes, "KOSDAQ": changed_indexes},
        symbols=["000001"], market_by_symbol={"000001": "KOSPI"}, sessions=sessions,
    )
    assert _feature(before, sessions[20]) == _feature(after, sessions[20])


def test_price_features_include_1_3_5_10_20_day_returns_and_5_20_day_windows() -> None:
    sessions = _sessions(25)
    stock = _series(sessions, step=1.0)
    index = _series(sessions, step=0.25)
    features = build_daily_features(
        prices_by_symbol={"000001": stock}, indexes={"KOSPI": index, "KOSDAQ": index},
        symbols=["000001"], market_by_symbol={"000001": "KOSPI"}, sessions=sessions,
    )
    row = _feature(features, sessions[24])
    for horizon in (1, 3, 5, 10, 20):
        assert row[f"return_{horizon}d_pct"] is not None
    for horizon in (5, 20):
        assert row[f"volatility_{horizon}d_pct"] is not None
        assert row[f"range_{horizon}d_pct"] is not None
    assert row["distance_20d_high_pct"] is not None
    assert row["distance_20d_low_pct"] is not None
    assert row["overnight_gap_pct"] is not None
    assert row["intraday_return_pct"] is not None
    assert row["volume_ratio_5d"] is not None
    assert row["volume_ratio_20d"] is not None
    assert row["turnover_ratio_5d"] is not None
    assert row["turnover_ratio_20d"] is not None


def test_missing_turnover_is_not_forward_filled_or_replaced_with_zero() -> None:
    sessions = _sessions(22)
    stock = _series(sessions)
    stock[20] = _bar(sessions[20], stock[20].open, stock[20].close, turnover=None)
    index = _series(sessions)
    features = build_daily_features(
        prices_by_symbol={"000001": stock}, indexes={"KOSPI": index, "KOSDAQ": index},
        symbols=["000001"], market_by_symbol={"000001": "KOSPI"}, sessions=sessions,
    )
    row = _feature(features, sessions[20])
    assert row["turnover_krw"] is None
    assert row["turnover_ratio_5d"] is None
    assert row["turnover_ratio_20d"] is None


def test_market_relative_return_uses_matching_index_and_completed_session() -> None:
    sessions = _sessions(7)
    stock = [_bar(day, 100, 100) for day in sessions]
    index = [_bar(day, 100, 100) for day in sessions]
    stock[-1] = _bar(sessions[-1], 119, 120)
    index[-1] = _bar(sessions[-1], 109, 110)
    features = build_daily_features(
        prices_by_symbol={"000001": stock}, indexes={"KOSPI": index, "KOSDAQ": index},
        symbols=["000001"], market_by_symbol={"000001": "KOSPI"}, sessions=sessions,
    )
    row = _feature(features, sessions[-1])
    assert row["return_1d_pct"] == pytest.approx(20.0)
    assert row["market_return_1d_pct"] == pytest.approx(10.0)
    assert row["symbol_minus_market_1d_pct"] == pytest.approx(10.0)


def test_next_session_entry_and_exact_2_3_5_10_day_exit_dates() -> None:
    sessions = _sessions(12)
    stock = [
        _bar(day, 100 + index * 10, 101 + index * 10)
        for index, day in enumerate(sessions)
    ]
    feature = {"symbol": "000001", "market": "KOSPI", "date": sessions[0].isoformat()}
    outcomes = build_outcomes(
        features=[feature], prices_by_symbol={"000001": stock}, sessions=sessions,
        period=(sessions[0], sessions[-1]), horizons=(2, 3, 5, 10),
    )
    by_horizon = {row["horizon_days"]: row for row in outcomes}
    assert len(by_horizon) == 4
    for horizon in (2, 3, 5, 10):
        assert by_horizon[horizon]["entry_date"] == sessions[1].isoformat()
        assert by_horizon[horizon]["exit_date"] == sessions[horizon].isoformat()
        assert by_horizon[horizon]["entry_open"] == stock[1].open
        assert by_horizon[horizon]["exit_close"] == stock[horizon].close


def test_non_overlapping_events_are_symbol_scoped_and_date_counts_are_separate() -> None:
    events = [
        {"symbol": "000001", "signal_date": "2023-01-02", "entry_date": "2023-01-03",
         "exit_date": "2023-01-05", "gross_return_pct": 1.0},
        {"symbol": "000001", "signal_date": "2023-01-03", "entry_date": "2023-01-04",
         "exit_date": "2023-01-06", "gross_return_pct": 2.0},
        {"symbol": "000001", "signal_date": "2023-01-05", "entry_date": "2023-01-06",
         "exit_date": "2023-01-09", "gross_return_pct": 3.0},
        {"symbol": "000002", "signal_date": "2023-01-02", "entry_date": "2023-01-03",
         "exit_date": "2023-01-05", "gross_return_pct": 4.0},
    ]
    selected = non_overlapping_events(events)
    assert [row["gross_return_pct"] for row in selected] == [1.0, 4.0, 3.0]
    metrics = _metrics(events)
    assert metrics["raw_event_count"] == 4
    assert metrics["non_overlapping_event_count"] == 3
    assert metrics["unique_signal_session_count"] == 3
    assert metrics["unique_symbol_count"] == 2


def test_session_cluster_bootstrap_resamples_dates_deterministically() -> None:
    events = [
        {"signal_date": "2023-01-02", "gross_return_pct": -1.0},
        {"signal_date": "2023-01-02", "gross_return_pct": 1.0},
        {"signal_date": "2023-01-03", "gross_return_pct": 2.0},
        {"signal_date": "2023-01-04", "gross_return_pct": 3.0},
    ]
    first = _cluster_bootstrap(events, iterations=100)
    second = _cluster_bootstrap(events, iterations=100)
    assert first == second
    assert first is not None
    assert first["unique_signal_sessions"] == 3
    assert first["iterations"] == 100


def test_market_context_primary_factor_maps_include_session_cluster_diagnostics() -> None:
    dates = [date(2023, 1, 2), date(2023, 1, 16)]
    features = [
        {"symbol": "000001", "market": "KOSPI", "date": day.isoformat(),
         "market_return_1d_pct": -2.0, "market_return_5d_pct": -3.0,
         "market_return_20d_pct": -5.0}
        for day in dates
    ]
    outcomes = [
        {"symbol": "000001", "market": "KOSPI", "signal_date": day.isoformat(),
         "entry_date": (day + timedelta(days=1)).isoformat(),
         "exit_date": (day + timedelta(days=horizon)).isoformat(),
         "horizon_days": horizon, "gross_return_pct": 1.0}
        for day in dates for horizon in (2, 3, 5, 10)
    ]
    maps = build_factor_maps(features, outcomes)
    context = maps["market_relative"]["market_return_20d_pct"]["development"]
    state_3d = next(row for row in context["3"] if row["state"] == "DOWN")
    state_2d = next(row for row in context["2"] if row["state"] == "DOWN")
    assert state_3d["session_cluster_bootstrap"]["unique_signal_sessions"] == 2
    assert "session_cluster_bootstrap" not in state_2d


class _TinyKis:
    def __init__(self) -> None:
        self.daily_calls: list[tuple[str, date, date, bool]] = []
        self.index_calls: list[tuple[str, date, date]] = []

    def get_daily_bars(self, symbol: str, start: date, end: date, *, adjusted: bool) -> list[Bar]:
        self.daily_calls.append((symbol, start, end, adjusted))
        days = [WARMUP_START, POOL_START, VALIDATION[0], CONFIRMATION[0], POOL_END]
        return [_bar(day, 100, 101) for day in days if start <= day <= end]

    def get_index_bars(self, index_code: str, start: date, end: date) -> list[Bar]:
        self.index_calls.append((index_code, start, end))
        days = [WARMUP_START, POOL_START, date(2024, 1, 2), date(2025, 1, 2), POOL_END]
        return [_bar(day, 100, 101) for day in days if start <= day <= end]


def _write_cohort(path: Path) -> list[str]:
    symbols = [f"{index:06d}" for index in range(1, 61)]
    payload = {
        "cohort_60": {
            "size": 60, "symbols": symbols,
            "market_counts": {"KOSDAQ": 30, "KOSPI": 30},
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return symbols


def test_phase11_acquisition_is_adjusted_atomic_resumable_and_confined(tmp_path: Path) -> None:
    cohort_path = tmp_path / "runtime/research/phase4/cohort-manifest.json"
    symbols = _write_cohort(cohort_path)
    output_root = tmp_path / "runtime/research/phase11"
    fake = _TinyKis()
    manifest = acquire_phase11_data(fake, output_root=output_root, cohort_path=cohort_path)
    assert manifest["status"] == "COMPLETE"
    assert len(fake.daily_calls) == 60
    assert all(start == WARMUP_START and end == POOL_END and adjusted for _, start, end, adjusted in fake.daily_calls)
    assert all(end <= POOL_END for _, _, end in fake.index_calls)
    metadata_path = output_root / "daily_cache/daily/000001-1d-adjusted.metadata.json"
    metadata = json.loads(metadata_path.read_text())
    assert metadata["adjustment_convention"] == "FID_ORG_ADJ_PRC=0 (KIS adjusted daily OHLCV)"
    assert metadata["sha256"] == hashlib.sha256(
        metadata_path.with_name("000001-1d-adjusted.parquet").read_bytes()
    ).hexdigest()
    prices, indexes = load_phase11_cache(output_root, symbols)
    assert prices["000001"][1].turnover_krw == 100_000
    assert indexes["KOSPI"]
    class NoRefetch:
        def get_daily_bars(self, *args, **kwargs):
            raise AssertionError("verified cache should resume without a provider call")

        def get_index_bars(self, *args, **kwargs):
            raise AssertionError("verified index cache should resume without a provider call")

    resumed = acquire_phase11_data(NoRefetch(), output_root=output_root, cohort_path=cohort_path)
    assert resumed["complete_symbols"] == len(symbols)
    assert len(fake.daily_calls) == 60


def test_phase11_resume_retries_failed_symbol_and_preserves_attempt_history(tmp_path: Path) -> None:
    cohort_path = tmp_path / "runtime/research/phase4/cohort-manifest.json"
    symbols = _write_cohort(cohort_path)
    output_root = tmp_path / "runtime/research/phase11"

    class FailsFirstSymbol(_TinyKis):
        def get_daily_bars(self, symbol: str, start: date, end: date, *, adjusted: bool) -> list[Bar]:
            if symbol == symbols[0]:
                return []
            return super().get_daily_bars(symbol, start, end, adjusted=adjusted)

    first = acquire_phase11_data(FailsFirstSymbol(), output_root=output_root, cohort_path=cohort_path)
    assert first["status"] == "PARTIAL"
    assert first["errors"] == [{
        "symbol": symbols[0], "error_type": "KisApiError",
        "reason": "KIS returned no daily rows",
    }]

    retry_client = _TinyKis()
    resumed = acquire_phase11_data(retry_client, output_root=output_root, cohort_path=cohort_path)
    assert resumed["status"] == "COMPLETE"
    assert resumed["errors"] == []
    assert len(resumed["attempt_history"]) == 2
    assert resumed["attempt_history"][0]["errors"] == first["errors"]
    assert resumed["attempt_history"][1]["errors"] == []
    assert [call[0] for call in retry_client.daily_calls] == [symbols[0]]


def test_phase11_analysis_preserves_previous_manifests_and_writes_integrity_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    cohort_path = Path("runtime/research/phase4/cohort-manifest.json")
    _write_cohort(cohort_path)
    for phase in range(5, 11):
        manifest = Path(f"runtime/research/phase{phase}/phase{phase}-artifact-index.json")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"phase": phase}), encoding="utf-8")
    before = snapshot_previous_phase_manifests()
    summary = run_phase11(
        output_root=Path("runtime/research/phase11"), cohort_path=cohort_path,
        client=_TinyKis(), acquire=True,
    )
    after = snapshot_previous_phase_manifests()
    assert compare_hash_snapshots(before, after)["status"] == "PASS"
    assert summary["previous_phase_manifest_integrity"]["status"] == "PASS"
    integrity = json.loads(Path("runtime/research/phase11/artifact-integrity.json").read_text())
    assert integrity["status"] == "PASS"
    assert summary["external_2026_status"] == "NOT_READ"
    assert summary["holdout_2026_status"] == "NOT_READ"
    assert summary["flow_data"] == "NOT_AVAILABLE"

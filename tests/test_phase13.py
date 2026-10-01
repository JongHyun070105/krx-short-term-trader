from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_trader.models import Bar
from krx_trader.research.phase13 import (
    ProtectedPeriodError,
    _assign_cross_sectional_ranks,
    _cache_source_sha,
    _candidate_gate,
    _candidate_name,
    _chronological_stability,
    _concentration,
    _emit_panel,
    _factor_groups,
    _factor_map_payload,
    _forward_outcomes,
    _interaction_candidate_candidates,
    _interaction_event_groups,
    _load_safe_parquet,
    _mean_date_cluster_bootstrap,
    _metrics,
    _period_idio_cuts,
    _residual_drawdown,
    _state,
    _turnover_ratio,
    assert_phase13_data_date,
    build_phase13_features,
    non_overlapping_events,
    read_confirmation_outcomes,
    read_external_2026,
    read_holdout_2026,
    rolling_beta,
)

KST = ZoneInfo("Asia/Seoul")


def _bar(
    day: date, open_price: float, close: float, *, volume: int = 1000, turnover: int | None = 100
) -> Bar:
    return Bar(
        datetime.combine(day, time.min, KST),
        open_price,
        max(open_price, close) + 1,
        min(open_price, close) - 1,
        close,
        volume,
        turnover,
    )


def _days(count: int, start: date = date(2023, 1, 2)) -> list[date]:
    return [start + timedelta(days=i) for i in range(count)]


def _feature_for(rows: list[dict], day: date, symbol: str = "000001") -> dict:
    return next(row for row in rows if row["date"] == day.isoformat() and row["symbol"] == symbol)


def test_phase13_date_guard_allows_safe_periods_and_blocks_protected_periods() -> None:
    assert_phase13_data_date(date(2022, 11, 1))
    assert_phase13_data_date(date(2023, 1, 2))
    assert_phase13_data_date(date(2025, 6, 30))
    for day in (date(2025, 7, 1), date(2025, 12, 30), date(2026, 1, 5), date(2026, 7, 28)):
        with pytest.raises(ProtectedPeriodError):
            assert_phase13_data_date(day)


def test_protected_outcome_readers_fail_closed() -> None:
    with pytest.raises(ProtectedPeriodError, match="freeze SHA"):
        read_confirmation_outcomes()
    with pytest.raises(ProtectedPeriodError, match="later phase"):
        read_external_2026()
    with pytest.raises(ProtectedPeriodError, match="must remain unread"):
        read_holdout_2026()


def test_confirmation_freeze_mismatch_fails_before_outcome_access(tmp_path: Path) -> None:
    marker = tmp_path / "phase13-confirmation.json"
    marker.write_text('{"confirmation":"NOT_RUN"}', encoding="utf-8")
    with pytest.raises(ProtectedPeriodError, match="clean local and remote main"):
        read_confirmation_outcomes(expected_freeze_sha="0" * 40, output_root=tmp_path)
    assert marker.read_text(encoding="utf-8") == '{"confirmation":"NOT_RUN"}'


def test_confirmation_marker_missing_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "krx_trader.research.phase13._verify_confirmation_freeze",
        lambda *_args, **_kwargs: {},
    )
    with pytest.raises(ProtectedPeriodError, match="Confirmation marker is missing"):
        read_confirmation_outcomes(expected_freeze_sha="a" * 40, output_root=tmp_path)


def test_safe_parquet_loader_returns_only_rows_through_touched_period(tmp_path: Path) -> None:
    days = [date(2025, 6, 30), date(2025, 7, 1)]
    table = pa.table(
        {
            "timestamp": [f"{day.isoformat()}T00:00:00+09:00" for day in days],
            "open": [100.0, 10_000.0],
            "high": [101.0, 10_001.0],
            "low": [99.0, 9_999.0],
            "close": [100.0, 10_000.0],
            "volume": [1, 1],
            "turnover_krw": [100, 100],
        }
    )
    path = tmp_path / "prices.parquet"
    pq.write_table(table, path)
    bars = _load_safe_parquet(path)
    assert [bar.time.astimezone(KST).date() for bar in bars] == [date(2025, 6, 30)]
    assert [bar.close for bar in bars] == [100.0]


def test_cache_sha_is_recomputed_and_mismatch_fails_closed(tmp_path: Path) -> None:
    expected = hashlib.sha256(b"cache content").hexdigest()
    assert _cache_source_sha(tmp_path / "daily.parquet", {"sha256": expected}) == expected
    with pytest.raises(ValueError, match="valid SHA-256"):
        _cache_source_sha(tmp_path / "daily.parquet", {"sha256": "not-a-digest"})


def test_confirmation_cache_row_bound_requires_explicit_authorization(tmp_path: Path) -> None:
    with pytest.raises(ProtectedPeriodError, match="without a verified Confirmation read"):
        _load_safe_parquet(tmp_path / "missing.parquet", end_date=date(2025, 12, 30))


def test_rolling_beta_known_beta_and_insufficient_pairs() -> None:
    market = [((i % 17) - 8) / 1000 for i in range(120)]
    stock = [0.0003 + 1.5 * value for value in market]
    beta, count, r_squared = rolling_beta(stock, market)
    assert beta == pytest.approx(1.5)
    assert count == 120
    assert r_squared == pytest.approx(1.0)
    missing, count, fit = rolling_beta(stock[:59], market[:59])
    assert missing is None
    assert count == 59
    assert fit is None


def test_rolling_beta_uses_only_returns_through_signal_date() -> None:
    sessions = _days(130)
    stock = [_bar(day, 100 + i, 101 + i) for i, day in enumerate(sessions)]
    index = [_bar(day, 100 + i / 2, 100.5 + i / 2) for i, day in enumerate(sessions)]
    args = {
        "prices_by_symbol": {"000001": stock}, "indexes": {"KOSPI": index, "KOSDAQ": index},
        "symbols": ["000001"], "market_by_symbol": {"000001": "KOSPI"}, "sessions": sessions,
    }
    before = build_phase13_features(**args)
    mutated = list(stock)
    mutated[-1] = _bar(sessions[-1], 10_000, 25_000)
    after = build_phase13_features(**{**args, "prices_by_symbol": {"000001": mutated}})
    signal = sessions[-2]
    assert _feature_for(before, signal)["beta_120d"] == pytest.approx(_feature_for(after, signal)["beta_120d"])


def test_excess_return_is_stock_return_minus_matched_market_return() -> None:
    sessions = _days(2)
    stock = [_bar(sessions[0], 100, 100), _bar(sessions[1], 100, 103)]
    kospi = [_bar(sessions[0], 100, 100), _bar(sessions[1], 100, 101)]
    kosdaq = [_bar(sessions[0], 100, 100), _bar(sessions[1], 100, 105)]
    features = build_phase13_features(
        prices_by_symbol={"000001": stock}, indexes={"KOSPI": kospi, "KOSDAQ": kosdaq},
        symbols=["000001"], market_by_symbol={"000001": "KOSPI"}, sessions=sessions,
    )
    assert _feature_for(features, sessions[1])["excess_return_1d_pct"] == pytest.approx(2.0)
    assert _feature_for(features, sessions[1])["market_breadth_positive_pct"] == 100.0
    assert _feature_for(features, sessions[1])["market_breadth_participants"] == 1


def test_symbol_market_mapping_selects_its_frozen_market_index() -> None:
    sessions = _days(2)
    stocks = {
        "000001": [_bar(day, 100, 100 + i * 3) for i, day in enumerate(sessions)],
        "000002": [_bar(day, 100, 100 + i * 3) for i, day in enumerate(sessions)],
    }
    kospi = [_bar(sessions[0], 100, 100), _bar(sessions[1], 100, 101)]
    kosdaq = [_bar(sessions[0], 100, 100), _bar(sessions[1], 100, 102)]
    features = build_phase13_features(
        prices_by_symbol=stocks, indexes={"KOSPI": kospi, "KOSDAQ": kosdaq},
        symbols=["000001", "000002"], market_by_symbol={"000001": "KOSPI", "000002": "KOSDAQ"},
        sessions=sessions,
    )
    assert _feature_for(features, sessions[1], "000001")["excess_return_1d_pct"] == pytest.approx(2.0)
    assert _feature_for(features, sessions[1], "000002")["excess_return_1d_pct"] == pytest.approx(1.0)


def test_forward_outcome_uses_t_plus_1_open_and_beta_residual_formula() -> None:
    sessions = _days(4)
    stock = [
        _bar(sessions[0], 90, 90),
        _bar(sessions[1], 100, 101),
        _bar(sessions[2], 101, 102),
        _bar(sessions[3], 102, 104),
    ]
    market = [
        _bar(sessions[0], 90, 100),
        _bar(sessions[1], 100, 101),
        _bar(sessions[2], 101, 101),
        _bar(sessions[3], 101, 102),
    ]
    feature = {"symbol": "000001", "market": "KOSPI", "date": sessions[0].isoformat(), "beta_120d": 1.5}
    outcomes = _forward_outcomes(
        [feature], {"000001": stock}, {"KOSPI": market, "KOSDAQ": market}, sessions
    )
    row = next(item for item in outcomes if item["horizon_sessions"] == 3)
    assert row["entry_date"] == sessions[1].isoformat()
    assert row["entry_open"] == 100
    assert row["stock_gross_return_pct"] == pytest.approx(4.0)
    assert row["matched_market_return_pct"] == pytest.approx(2.0)
    assert row["beta_residual_return_pct"] == pytest.approx(1.0)
    assert row["mfe_pct"] > 4.0
    assert row["mae_pct"] < 0


def test_features_do_not_change_when_future_stock_or_index_values_change() -> None:
    sessions = _days(35)
    stock = [_bar(day, 100 + i, 101 + i) for i, day in enumerate(sessions)]
    index = [_bar(day, 100 + i / 2, 100.5 + i / 2) for i, day in enumerate(sessions)]
    args = {
        "prices_by_symbol": {"000001": stock}, "indexes": {"KOSPI": index, "KOSDAQ": index},
        "symbols": ["000001"], "market_by_symbol": {"000001": "KOSPI"}, "sessions": sessions,
    }
    baseline = build_phase13_features(**args)
    changed_stock, changed_index = list(stock), list(index)
    changed_stock[30] = _bar(sessions[30], 1_000, 2_000, turnover=99_000)
    changed_index[31] = _bar(sessions[31], 1_000, 2_000)
    changed = build_phase13_features(**{
        **args, "prices_by_symbol": {"000001": changed_stock},
        "indexes": {"KOSPI": changed_index, "KOSDAQ": changed_index},
    })
    assert _feature_for(baseline, sessions[25]) == _feature_for(changed, sessions[25])


def test_residual_drawdown_tracks_relative_cumulative_path() -> None:
    sessions = _days(3)
    stock = {
        sessions[0]: _bar(sessions[0], 100, 100),
        sessions[1]: _bar(sessions[1], 100, 110),
        sessions[2]: _bar(sessions[2], 110, 99),
    }
    market = {day: _bar(day, 100, 100) for day in sessions}
    drawdown, distance = _residual_drawdown(stock, market, sessions, 2, 2)
    assert drawdown == pytest.approx(-10.0)
    assert distance == pytest.approx(-10.0)


def test_turnover_ratio_uses_five_completed_sessions_over_prior_median() -> None:
    sessions = _days(26)
    stock = {day: _bar(day, 100, 100, turnover=100 if i < 21 else 200) for i, day in enumerate(sessions)}
    assert _turnover_ratio(stock, sessions, 25) == pytest.approx(2.0)
    stock[sessions[24]] = _bar(sessions[24], 100, 100, turnover=None)
    assert _turnover_ratio(stock, sessions, 25) is None


def test_cross_sectional_ranks_are_market_date_scoped_and_missing_excluded() -> None:
    rows = [
        {"symbol": "000001", "market": "KOSPI", "date": "2023-01-02", "excess_return_1d_pct": 1.0, "excess_return_5d_pct": 1.0, "excess_return_20d_pct": 1.0, "residual_return_5d_pct": 1.0, "residual_return_20d_pct": 1.0, "idio_volatility_20d_pct": 1.0, "turnover_ratio_5d_vs_prior20_median": 1.0},
        {"symbol": "000002", "market": "KOSPI", "date": "2023-01-02", "excess_return_1d_pct": 1.0, "excess_return_5d_pct": 1.0, "excess_return_20d_pct": 1.0, "residual_return_5d_pct": 1.0, "residual_return_20d_pct": 1.0, "idio_volatility_20d_pct": 1.0, "turnover_ratio_5d_vs_prior20_median": 1.0},
        {"symbol": "000003", "market": "KOSPI", "date": "2023-01-02", "excess_return_1d_pct": 2.0, "excess_return_5d_pct": 2.0, "excess_return_20d_pct": 2.0, "residual_return_5d_pct": 2.0, "residual_return_20d_pct": 2.0, "idio_volatility_20d_pct": 2.0, "turnover_ratio_5d_vs_prior20_median": 2.0},
        {"symbol": "000004", "market": "KOSPI", "date": "2023-01-02", "excess_return_1d_pct": None, "excess_return_5d_pct": None, "excess_return_20d_pct": None, "residual_return_5d_pct": None, "residual_return_20d_pct": None, "idio_volatility_20d_pct": None, "turnover_ratio_5d_vs_prior20_median": None},
        {"symbol": "000005", "market": "KOSDAQ", "date": "2023-01-02", "excess_return_1d_pct": 99.0, "excess_return_5d_pct": 99.0, "excess_return_20d_pct": 99.0, "residual_return_5d_pct": 99.0, "residual_return_20d_pct": 99.0, "idio_volatility_20d_pct": 99.0, "turnover_ratio_5d_vs_prior20_median": 99.0},
    ]
    _assign_cross_sectional_ranks(rows)
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["000001"]["excess_rank_1d_pct"] == pytest.approx(25.0)
    assert by_symbol["000002"]["excess_rank_1d_pct"] == pytest.approx(25.0)
    assert by_symbol["000003"]["excess_rank_1d_pct"] == pytest.approx(100.0)
    assert by_symbol["000001"]["excess_rank_1d_pct_participants"] == 3
    assert by_symbol["000004"]["excess_rank_1d_pct"] is None
    assert by_symbol["000004"]["excess_rank_1d_pct_participants"] == 3
    assert by_symbol["000005"]["excess_rank_1d_pct"] == 50.0
    assert _state("LOWER", "residual_rank_20d_state") == "LOWER"


def test_symbol_input_order_does_not_change_ranked_factor_maps() -> None:
    sessions = _days(12)
    prices = {
        symbol: [_bar(day, 100 + i + offset, 100.5 + i + offset) for i, day in enumerate(sessions)]
        for symbol, offset in (("000001", 0), ("000002", 4), ("000003", 9))
    }
    index = [_bar(day, 100 + i / 2, 100.25 + i / 2) for i, day in enumerate(sessions)]
    kwargs = {"indexes": {"KOSPI": index, "KOSDAQ": index},
              "market_by_symbol": {s: "KOSPI" for s in prices}, "sessions": sessions}
    one = build_phase13_features(prices_by_symbol=prices, symbols=list(prices), **kwargs)
    two = build_phase13_features(prices_by_symbol=prices, symbols=list(reversed(prices)), **kwargs)
    def project(rows: list[dict]) -> dict:
        return {
            (row["symbol"], row["date"]): (
                row.get("residual_rank_20d_pct"), row.get("excess_rank_5d_pct")
            )
            for row in rows
        }
    assert project(one) == project(two)


def test_non_overlapping_execution_filter_is_symbol_scoped() -> None:
    events = [
        {"symbol": "000001", "signal_date": "2023-01-02", "entry_date": "2023-01-03", "exit_date": "2023-01-05", "stock_gross_return_pct": 1.0},
        {"symbol": "000001", "signal_date": "2023-01-03", "entry_date": "2023-01-04", "exit_date": "2023-01-06", "stock_gross_return_pct": 2.0},
        {"symbol": "000001", "signal_date": "2023-01-05", "entry_date": "2023-01-06", "exit_date": "2023-01-09", "stock_gross_return_pct": 3.0},
        {"symbol": "000002", "signal_date": "2023-01-02", "entry_date": "2023-01-03", "exit_date": "2023-01-05", "stock_gross_return_pct": 4.0},
    ]
    chosen = non_overlapping_events(events)
    assert [row["stock_gross_return_pct"] for row in chosen] == [1.0, 3.0, 4.0]


def test_date_cluster_bootstrap_is_deterministic_and_clusters_shared_dates() -> None:
    events = [
        {"signal_date": "2023-01-02", "stock_gross_return_pct": -1.0, "excess_return_pct": -0.5, "beta_residual_return_pct": -0.4},
        {"signal_date": "2023-01-02", "stock_gross_return_pct": 1.0, "excess_return_pct": 0.5, "beta_residual_return_pct": 0.4},
        {"signal_date": "2023-01-03", "stock_gross_return_pct": 2.0, "excess_return_pct": 1.0, "beta_residual_return_pct": 0.8},
    ]
    first = _mean_date_cluster_bootstrap(events, iterations=100, seed=13)
    assert first == _mean_date_cluster_bootstrap(events, iterations=100, seed=13)
    assert first["date_clusters"] == 2
    assert first["cluster_unit"].startswith("signal_date")
    assert first["interval_pct_5_95"]["beta_adjusted_residual_return_pct"] != [None, None]


def test_missing_features_are_excluded_not_zero_filled_and_costs_are_explicit() -> None:
    row = {
        "symbol": "000001", "market": "KOSPI", "signal_date": "2023-01-02",
        "entry_date": "2023-01-03", "exit_date": "2023-01-05",
        "stock_gross_return_pct": 3.0, "matched_market_return_pct": 1.0,
        "excess_return_pct": 2.0, "beta_residual_return_pct": 1.5,
    }
    metrics = _metrics([row])
    assert metrics["net_return_pct"]["1x"] == pytest.approx(2.47)
    assert metrics["net_return_pct"]["1.5x"] == pytest.approx(2.205)
    assert metrics["net_return_pct"]["2x"] == pytest.approx(1.94)
    assert metrics["market_component_share_pct"] == pytest.approx(100 / 3)


def test_factor_maps_are_deterministic_and_report_all_three_outcome_measures() -> None:
    features = [
        {"symbol": "000001", "date": "2023-01-02", "period": "DISCOVERY", "excess_return_5d_pct": -3.0, "idio_volatility_20d_pct": 1.0, "turnover_ratio_5d_vs_prior20_median": 0.5, "range_position_20d": 0.1, "residual_rank_5d_state": "LOWER", "residual_rank_20d_state": "LOWER"},
        {"symbol": "000002", "date": "2023-01-02", "period": "DISCOVERY", "excess_return_5d_pct": -1.0, "idio_volatility_20d_pct": 2.0, "turnover_ratio_5d_vs_prior20_median": 1.0, "range_position_20d": 0.5, "residual_rank_5d_state": "MIDDLE", "residual_rank_20d_state": "MIDDLE"},
        {"symbol": "000003", "date": "2023-01-02", "period": "DISCOVERY", "excess_return_5d_pct": 3.0, "idio_volatility_20d_pct": 3.0, "turnover_ratio_5d_vs_prior20_median": 2.0, "range_position_20d": 0.9, "residual_rank_5d_state": "UPPER", "residual_rank_20d_state": "UPPER"},
    ]
    outcomes = [
        {"symbol": symbol, "market": "KOSPI", "signal_date": "2023-01-02", "period": "DISCOVERY", "entry_date": "2023-01-03", "exit_date": "2023-01-05", "horizon_sessions": 3, "stock_gross_return_pct": gross, "matched_market_return_pct": 1.0, "excess_return_pct": gross - 1.0, "beta_120d": 1.0, "beta_residual_return_pct": gross - 1.0}
        for symbol, gross in (("000001", 2.0), ("000002", 3.0), ("000003", 4.0))
    ]
    groups = _factor_groups(features, outcomes, idio_cuts=(1.5, 2.5))
    excess_keys = [key for key in groups if key.startswith("EXCESS_RETURN_") and "excess_return_5d_pct" in key]
    assert {key.split("|", 1)[0] for key in excess_keys} == {"EXCESS_RETURN_MOMENTUM", "EXCESS_RETURN_REVERSAL"}
    assert all("|−" in key for key in excess_keys if key.startswith("EXCESS_RETURN_REVERSAL|"))
    assert all("|−" not in key for key in excess_keys if key.startswith("EXCESS_RETURN_MOMENTUM|"))
    assert any(key.startswith("IDIOSYNCRATIC_VOLATILITY|idio_volatility_20d_pct|LOW|") for key in groups)
    assert any(key.startswith("ABNORMAL_TURNOVER|turnover_ratio_5d_vs_prior20_median|CONTRACTED|") for key in groups)
    assert any(key.startswith("RESIDUAL_RANK|residual_rank_5d_state|LOWER|") for key in groups)
    assert any(key.startswith("RANGE_POSITION|range_position_20d|LOWER_QUARTER|") for key in groups)
    first = _factor_map_payload(groups)
    second = _factor_map_payload(groups)
    assert first == second
    metrics = first["maps"][0]["metrics"]
    assert metrics["stock_gross_return_pct"] is not None
    assert metrics["simple_excess_return_pct"] is not None
    assert metrics["beta_adjusted_residual_return_pct"] is not None


def test_two_factor_interactions_keep_market_cells_separate() -> None:
    features = [
        {"symbol": "000001", "date": "2023-01-02", "excess_return_5d_pct": -5.0, "turnover_ratio_5d_vs_prior20_median": 2.0, "residual_drawdown_20d_pct": -9.0, "idio_volatility_20d_pct": 2.0, "residual_rank_20d_state": "LOWER"},
        {"symbol": "000002", "date": "2023-01-02", "excess_return_5d_pct": -5.0, "turnover_ratio_5d_vs_prior20_median": 2.0, "residual_drawdown_20d_pct": -9.0, "idio_volatility_20d_pct": 2.0, "residual_rank_20d_state": "LOWER"},
    ]
    outcomes = [
        {"symbol": symbol, "market": market, "signal_date": "2023-01-02", "period": "DISCOVERY", "entry_date": "2023-01-03", "exit_date": "2023-01-09", "horizon_sessions": 5, "stock_gross_return_pct": 1.0, "matched_market_return_pct": 0.2, "excess_return_pct": 0.8, "beta_120d": 1.0, "beta_residual_return_pct": 0.8}
        for symbol, market in (("000001", "KOSPI"), ("000002", "KOSDAQ"))
    ]
    grouped = _interaction_event_groups(features, outcomes, idio_cuts=(1.0, 3.0))
    assert ("EXCESS_5D_X_TURNOVER", "DISCOVERY", "KOSPI", "LE_−4", "EXPANDED", 5) in grouped
    assert ("EXCESS_5D_X_TURNOVER", "DISCOVERY", "KOSDAQ", "LE_−4", "EXPANDED", 5) in grouped
    assert len(grouped[("EXCESS_5D_X_TURNOVER", "DISCOVERY", "KOSPI", "LE_−4", "EXPANDED", 5)]) == 1


def test_interaction_candidate_gate_requires_both_period_economics_and_chronology() -> None:
    def events(start: date, prefix: str) -> list[dict]:
        blocks = [start, date(2023, 7, 1), date(2024, 1, 2)] if prefix == "D" else [start, date(2025, 1, 2), date(2025, 3, 1)]
        rows = []
        for index in range(60):
            signal = blocks[index // 20] + timedelta(days=index % 20)
            gross = -0.5 if index % 12 == 0 else 1.5
            rows.append({
                "symbol": f"{prefix}{index:03d}", "market": "KOSPI", "signal_date": signal.isoformat(),
                "entry_date": (signal + timedelta(days=1)).isoformat(), "exit_date": (signal + timedelta(days=5)).isoformat(),
                "period": "DISCOVERY" if prefix == "D" else "TOUCHED_REPLICATION", "horizon_sessions": 5,
                "stock_gross_return_pct": gross, "matched_market_return_pct": 0.2,
                "excess_return_pct": gross - 0.2, "beta_120d": 1.0,
                "beta_residual_return_pct": gross - 0.2, "mfe_pct": max(gross, 1.0), "mae_pct": min(gross, -0.1),
            })
        return rows

    groups = {
        ("EXCESS_5D_X_TURNOVER", "DISCOVERY", "KOSPI", "LE_−4", "EXPANDED", 5): events(date(2023, 1, 2), "D"),
        ("EXCESS_5D_X_TURNOVER", "TOUCHED_REPLICATION", "KOSPI", "LE_−4", "EXPANDED", 5): events(date(2024, 7, 1), "R"),
    }
    result = _interaction_candidate_candidates(groups)[0]
    assert result["gate"]["pass"] is True
    assert result["positive_chronological_blocks"] == 5
    groups[("EXCESS_5D_X_TURNOVER", "TOUCHED_REPLICATION", "KOSPI", "LE_−4", "EXPANDED", 5)] = [
        {**row, "stock_gross_return_pct": -1.0, "excess_return_pct": -1.2, "beta_residual_return_pct": -1.2}
        for row in groups[("EXCESS_5D_X_TURNOVER", "TOUCHED_REPLICATION", "KOSPI", "LE_−4", "EXPANDED", 5)]
    ]
    rejected = _interaction_candidate_candidates(groups)[0]
    assert rejected["gate"]["pass"] is False
    assert "positive_absolute_net_1x_both_periods" in rejected["gate"]["failed_checks"]


def test_discovery_idio_cutoffs_are_frozen_from_discovery_rows_only() -> None:
    rows = [
        {"period": "DISCOVERY", "idio_volatility_20d_pct": float(value)} for value in range(1, 7)
    ] + [{"period": "TOUCHED_REPLICATION", "idio_volatility_20d_pct": 1_000_000.0}]
    cuts = _period_idio_cuts(rows)
    assert cuts is not None
    assert cuts[1] < 1_000_000


def test_chronological_blocks_include_touched_replication_and_keep_period_boundary() -> None:
    leader = {"family": "BETA_RESIDUAL_REVERSAL", "factor": "residual_return_20d_pct", "state": "LOW", "events": []}
    discovery_dates = ["2023-03-01", "2023-09-01", "2024-03-01"]
    touched_dates = ["2024-09-01", "2025-03-01"]
    def rows(dates: list[str], prefix: str) -> list[dict]:
        return [
            {"symbol": f"{prefix}{i}", "market": "KOSPI", "signal_date": day,
             "entry_date": day, "exit_date": day, "stock_gross_return_pct": 1.0,
             "matched_market_return_pct": 0.1, "excess_return_pct": 0.9,
             "beta_residual_return_pct": 0.8}
            for i, day in enumerate(dates)
        ]
    groups = {
        "BETA_RESIDUAL_REVERSAL|residual_return_20d_pct|LOW|DISCOVERY|5": rows(discovery_dates, "D"),
        "BETA_RESIDUAL_REVERSAL|residual_return_20d_pct|LOW|TOUCHED_REPLICATION|5": rows(touched_dates, "R"),
    }
    result = _chronological_stability(leader, groups)
    assert [row["metrics"]["non_overlapping_executions"] for row in result["blocks"]] == [1, 1, 1, 1, 1]


def test_concentration_reports_contribution_shares_not_only_raw_sums() -> None:
    events = [
        {"symbol": f"00000{i}", "market": "KOSPI", "signal_date": f"2023-01-0{i}",
         "entry_date": f"2023-01-0{i+1}", "exit_date": f"2023-01-0{i+2}",
         "stock_gross_return_pct": float(i)}
        for i in range(1, 6)
    ]
    result = _concentration(events)
    assert result["top_trade_contribution"]["gross_pct"] == 5.0
    assert result["top_trade_contribution"]["share_of_positive_contribution_pct"] > 0
    assert result["top_5_signal_date_share_pct"] == pytest.approx(100.0)
    assert result["top_3_symbol_share_pct"] > 0


def test_candidate_gate_rejects_positive_residual_when_absolute_net_is_negative() -> None:
    metrics = {
        "non_overlapping_executions": 100, "unique_signal_dates": 60,
        "simple_excess_return_pct": 0.2, "beta_adjusted_residual_return_pct": 0.3,
        "stock_gross_return_pct": 0.4, "net_return_pct": {"1x": -0.13}, "payoff_ratio": 1.2,
    }
    leader = {"metrics": metrics}
    chronology = {"blocks": [
        {"metrics": {"stock_gross_return_pct": 0.1, "beta_adjusted_residual_return_pct": 0.1}}
        for _ in range(4)
    ]}
    concentration = {"top_3_symbol_share_pct": 20.0, "top_5_signal_date_share_pct": 30.0}
    result = _candidate_gate(leader, metrics, chronology, concentration)
    assert result["pass"] is False
    assert "positive_absolute_net_1x_both_periods" in result["failed_checks"]


def test_candidate_name_reflects_selected_interaction_states() -> None:
    assert _candidate_name(
        "KOSPI", "excess_return_5d_pct", "LE_−4",
        "turnover_ratio_5d_vs_prior20_median", "EXPANDED",
    ) == "KOSPI_EXCESS_REVERSAL_WITH_EXPANDED_TURNOVER"
    assert _candidate_name(
        "KOSDAQ", "residual_drawdown_20d_pct", "DEEP_LE_−8",
        "idio_volatility_20d_pct", "HIGH",
    ) == "KOSDAQ_RESIDUAL_DRAWDOWN_RECOVERY_WITH_IDIOSYNCRATIC_VOLATILITY_HIGH"


def test_safe_factor_panel_gzip_is_byte_deterministic(tmp_path: Path) -> None:
    features = [{"symbol": "000001", "date": "2023-01-02", "period": "DISCOVERY", "beta_120d": None}]
    outcomes = [{
        "symbol": "000001", "market": "KOSPI", "signal_date": "2023-01-02", "entry_date": "2023-01-03",
        "exit_date": "2023-01-05", "horizon_sessions": 3, "stock_gross_return_pct": 1.0,
    }]
    first, second = tmp_path / "first.jsonl.gz", tmp_path / "second.jsonl.gz"
    _emit_panel(first, features, outcomes)
    _emit_panel(second, features, outcomes)
    assert first.read_bytes() == second.read_bytes()

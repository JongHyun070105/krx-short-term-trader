from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.metrics import Metrics, calculate_metrics
from krx_trader.backtest.portfolio import run_portfolio_backtest
from krx_trader.backtest.validation import (
    StrategyGateState,
    chronological_split,
    evaluate_strategy_gate,
)
from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.resample import resample_session_minutes
from krx_trader.market.regime import Regime, classify_regime
from krx_trader.models import Bar, Decision, Signal
from krx_trader.strategies.breakout import BreakoutConfig, evaluate_breakout
from krx_trader.strategies.pullback import PullbackConfig, evaluate_pullback
from krx_trader.universe.master import load_stock_master

KST = ZoneInfo("Asia/Seoul")


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "INF" if value > 0 else "-INF"
    return value


def _metrics_dict(metrics: Metrics) -> dict[str, Any]:
    return {name: _finite(getattr(metrics, name)) for name in metrics.__dataclass_fields__}


def _market_regime(
    market_date: date,
    kospi: list[Bar],
    kosdaq: list[Bar],
    settings: Settings,
) -> Regime | None:
    def classify(bars: list[Bar]) -> Regime | None:
        prior = [bar for bar in bars if bar.time.astimezone(KST).date() < market_date]
        return classify_regime(
            prior,
            trend_lookback=settings.regime_trend_lookback,
            volatility_lookback=settings.regime_volatility_lookback,
            high_vol_threshold=settings.regime_high_vol_threshold,
        )

    kospi_regime = classify(kospi)
    kosdaq_regime = classify(kosdaq)
    if kospi_regime is None or kosdaq_regime is None:
        return None
    if Regime.HIGH_VOL in {kospi_regime, kosdaq_regime}:
        return Regime.HIGH_VOL
    if Regime.DOWN in {kospi_regime, kosdaq_regime}:
        return Regime.DOWN
    if kospi_regime == kosdaq_regime == Regime.UP:
        return Regime.UP
    return Regime.NEUTRAL


def _scanner_membership(
    bars_by_symbol: dict[str, list[Bar]], top_n: int
) -> dict[datetime, set[str]]:
    scores: dict[datetime, dict[str, float]] = {}
    for symbol, bars in bars_by_symbol.items():
        for index, bar in enumerate(bars):
            history = bars[max(0, index - 19):index + 1]
            turnover = sum(item.close * item.volume for item in history) / len(history)
            scores.setdefault(bar.time, {})[symbol] = turnover
    return {
        timestamp: {
            symbol for symbol, _ in sorted(rows.items(), key=lambda item: (-item[1], item[0]))[:top_n]
        }
        for timestamp, rows in scores.items()
    }


def _signal_function(
    strategy: str,
    settings: Settings,
    regime_filter: str,
    kospi: list[Bar],
    kosdaq: list[Bar],
    *,
    config: BreakoutConfig | PullbackConfig,
    start_date: date,
    end_date: date,
    warmup_date: date | None = None,
    scanner_membership: dict[datetime, set[str]] | None,
):
    def evaluate(symbol: str, bars: list[Bar]) -> Signal:
        last = bars[-1]
        signal_date = last.time.astimezone(KST).date()
        if not (start_date <= signal_date < end_date):
            return Signal(last.time, symbol, strategy, Decision.HOLD, ("OUTSIDE_SEGMENT",))
        if scanner_membership is not None and symbol not in scanner_membership.get(last.time, set()):
            return Signal(last.time, symbol, strategy, Decision.HOLD, ("SCANNER_EXCLUDED",))
        regime = Regime.NEUTRAL if regime_filter == "off" else _market_regime(
            last.time.astimezone(KST).date(), kospi, kosdaq, settings
        )
        if strategy == "breakout":
            return evaluate_breakout(bars, symbol, regime=regime, config=config)
        return evaluate_pullback(bars, symbol, regime=regime, config=config)

    return evaluate


def _portfolio_metrics(
    bars_by_symbol: dict[str, list[Bar]],
    strategy: str,
    settings: Settings,
    kospi: list[Bar],
    kosdaq: list[Bar],
    *,
    config: BreakoutConfig | PullbackConfig,
    start_date: date,
    end_date: date,
    scanner: str,
    cost_multiplier: float,
    top_n: int = 10,
    warmup_date: date | None = None,
) -> tuple[Metrics, Any, dict[datetime, set[str]] | None]:
    history_start = warmup_date or start_date
    segment_bars = _bars_for_segment(bars_by_symbol, history_start, end_date)
    membership = _scanner_membership(segment_bars, top_n) if scanner == "top10" else None
    evaluate = _signal_function(
        strategy, settings, "on", kospi, kosdaq, config=config,
        start_date=start_date, end_date=end_date, warmup_date=warmup_date,
        scanner_membership=membership,
    )
    result = run_portfolio_backtest(
        segment_bars,
        evaluate,
        starting_cash_krw=100_000,
        capital_cap_krw=100_000,
        order_cap_krw=settings.max_order_notional_krw,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        max_concurrent_positions=settings.max_concurrent_positions,
        cost_model=CostModel(settings.broker_fee_rate, settings.sell_tax_rate, settings.slippage_bps),
        stress_multiplier=cost_multiplier,
    )
    return calculate_metrics(result), result, membership


def _bars_for_segment(
    bars_by_symbol: dict[str, list[Bar]], history_start: date, end_date: date
) -> dict[str, list[Bar]]:
    """Include prior-session history while keeping fills inside this segment."""
    return {
        symbol: selected
        for symbol, bars in bars_by_symbol.items()
        if (selected := [
            bar for bar in bars
            if history_start <= bar.time.astimezone(KST).date() < end_date
        ])
    }


def _prior_invalid_final_runs(report_root: Path) -> list[str]:
    contaminated: list[str] = []
    for path in report_root.glob("phase2-*.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        split = report.get("split", {})
        if split.get("common_sessions") == 1 and "005930" in report.get("symbols", []):
            contaminated.append(report.get("run_id", path.stem))
    return sorted(contaminated)


def _trade_concentration(
    trades: tuple,
    bars_by_symbol: dict[str, list[Bar]],
    markets: dict[str, str],
) -> dict[str, Any]:
    if not trades:
        return {"trade_count": 0, "top_trade_pct": None, "top5_trade_pct": None,
                "top_symbol": None, "top_day": None, "top_month": None,
                "markets": {}, "price_buckets": {}, "liquidity_buckets": {}}
    total = sum(trade.net_pnl_krw for trade in trades)

    def top_group(key_fn):
        groups: dict[str, float] = {}
        for trade in trades:
            key = key_fn(trade)
            groups[key] = groups.get(key, 0.0) + trade.net_pnl_krw
        key, pnl = max(groups.items(), key=lambda item: (item[1], item[0]))
        return {"key": key, "net_pnl_krw": round(pnl, 2),
                "contribution_pct": round(pnl / total * 100, 2) if total > 0 else None}

    positive = sorted((max(0.0, trade.net_pnl_krw) for trade in trades), reverse=True)
    sum_positive = sum(positive)
    market_groups: dict[str, float] = {}
    price_groups: dict[str, dict[str, float]] = {}
    liquidity_groups: dict[str, dict[str, float]] = {}
    by_symbol = {symbol: sorted(bars, key=lambda bar: bar.time) for symbol, bars in bars_by_symbol.items()}
    for trade in trades:
        market = markets.get(trade.symbol, "UNKNOWN")
        market_groups[market] = market_groups.get(market, 0.0) + trade.net_pnl_krw
        price = trade.entry_reference_price
        bucket = "1k_10k" if price < 10_000 else "10k_30k" if price < 30_000 else "30k_50k"
        item = price_groups.setdefault(bucket, {"trades": 0.0, "net_pnl_krw": 0.0})
        item["trades"] += 1
        item["net_pnl_krw"] += trade.net_pnl_krw
        prior = [bar for bar in by_symbol.get(trade.symbol, []) if bar.time < trade.entry_time][-20:]
        average_turnover = sum(bar.close * bar.volume for bar in prior) / len(prior) if prior else 0.0
        liquidity = "LOW" if average_turnover < 50_000_000 else "MEDIUM" if average_turnover < 250_000_000 else "HIGH"
        liquidity_item = liquidity_groups.setdefault(liquidity, {"trades": 0.0, "net_pnl_krw": 0.0})
        liquidity_item["trades"] += 1
        liquidity_item["net_pnl_krw"] += trade.net_pnl_krw
    return {
        "trade_count": len(trades),
        "top_trade_pct": round(positive[0] / sum_positive * 100, 2) if sum_positive else None,
        "top5_trade_pct": round(sum(positive[:5]) / sum_positive * 100, 2) if sum_positive else None,
        "top_symbol": top_group(lambda trade: trade.symbol),
        "top_day": top_group(lambda trade: trade.exit_time.astimezone(KST).date().isoformat()),
        "top_month": top_group(lambda trade: trade.exit_time.astimezone(KST).strftime("%Y-%m")),
        "markets": {key: round(value, 2) for key, value in sorted(market_groups.items())},
        "price_buckets": price_groups,
        "liquidity_buckets": liquidity_groups,
    }


def _load_research_bars(cache_root: Path, symbol_filter: list[str] | None = None):
    minute_root = cache_root / "minute"
    dataset_report = Path("runtime/research/latest_dataset.json")
    report = json.loads(dataset_report.read_text(encoding="utf-8")) if dataset_report.is_file() else {}
    if symbol_filter is None:
        succeeded = report.get("succeeded_symbols", [])
        symbol_filter = succeeded if succeeded else None
    symbols = symbol_filter or sorted(path.name for path in minute_root.iterdir() if path.is_dir())
    range_start = date.fromisoformat(report["date_range"]["start"]) if report.get("date_range") else None
    range_end = date.fromisoformat(report["date_range"]["end"]) if report.get("date_range") else None
    session_limit = report.get("session_limit_per_symbol")
    bars_by_symbol: dict[str, list[Bar]] = {}
    hashes: list[str] = []
    cache = ParquetBarCache(cache_root)
    for symbol in symbols:
        symbol_path = minute_root / symbol
        dates = sorted(path.stem for path in symbol_path.glob("*.parquet")) if symbol_path.is_dir() else []
        if range_start is not None and range_end is not None:
            dates = [
                date_text for date_text in dates
                if range_start <= date.fromisoformat(date_text) <= range_end
            ]
        if isinstance(session_limit, int) and session_limit > 0:
            dates = dates[-session_limit:]
        bars: list[Bar] = []
        for date_text in dates:
            session = date.fromisoformat(date_text)
            bars.extend(cache.load("minute", symbol, "1m", session))
            metadata = json.loads((symbol_path / f"{date_text}.metadata.json").read_text(encoding="utf-8"))
            hashes.append(f"{symbol}:{date_text}:{metadata['sha256']}")
        bars.sort(key=lambda bar: bar.time)
        if bars:
            bars_by_symbol[symbol] = bars
    return bars_by_symbol, hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest(), hashes


def _with_reference_dataset_hashes(cache_root: Path, hashes: list[str]) -> tuple[str, list[str]]:
    records = list(hashes)
    for symbol in ("kospi", "kosdaq"):
        path = cache_root / "indexes" / f"{symbol}-1d.metadata.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        records.append(f"index:{symbol}:{metadata['sha256']}")
    master_metadata = json.loads(
        (cache_root / "universe" / "stocks.metadata.json").read_text(encoding="utf-8")
    )
    records.append(f"universe:current-master:{master_metadata['parquet_sha256']}")
    return hashlib.sha256("\n".join(sorted(records)).encode()).hexdigest(), sorted(records)


def _json_metrics(metrics: Metrics, result, concentration: dict[str, Any]) -> dict[str, Any]:
    return {
        "metrics": _metrics_dict(metrics),
        "net_pnl_krw": round(metrics.total_return_pct * 100_000 / 100, 2),
        "trades": len(result.trades),
        "decision_counts": {
            "total": len(result.decisions),
            "enter": sum(signal.decision == Decision.ENTER for signal in result.decisions),
            "hold": sum(signal.decision == Decision.HOLD for signal in result.decisions),
            "reason_codes": dict(sorted(Counter(
                reason for signal in result.decisions for reason in signal.reason_codes
            ).items())),
        },
        "closed_trade_net_pnl_krw": round(sum(trade.net_pnl_krw for trade in result.trades), 2),
        "open_position": result.open_position,
        "concentration": concentration,
    }


def run_validation(
    settings: Settings,
    *,
    cache_root: Path = Path("data"),
    report_root: Path = Path("runtime/research"),
    top_n: int = 10,
) -> dict[str, Any]:
    """Run frozen 55/20/25 chronology, baseline, fixed neighbors, stress and A/Bs offline."""
    bars_1m, dataset_hash, partition_hashes = _load_research_bars(cache_root)
    if not bars_1m:
        raise ValueError("no cached real minute dataset; run data build-research-set first")
    common_dates = sorted(set.intersection(*[
        {bar.time.astimezone(KST).date() for bar in bars} for bars in bars_1m.values()
    ]))
    date_splits = chronological_split(
        [Bar(datetime.combine(day, datetime.min.time(), KST), 1, 1, 1, 1, 0) for day in common_dates]
    ) if len(common_dates) >= 2 else None
    if date_splits:
        development_dates = [bar.time.date() for bar in date_splits.development]
        validation_dates = [bar.time.date() for bar in date_splits.validation]
        final_dates = [bar.time.date() for bar in date_splits.final_test]
    else:
        development_dates, validation_dates, final_dates = [], [], []
    if not final_dates and common_dates:
        final_dates = common_dates[-1:]
    split_meta = {
        "development": [min(development_dates).isoformat(), max(development_dates).isoformat()] if development_dates else None,
        "validation": [min(validation_dates).isoformat(), max(validation_dates).isoformat()] if validation_dates else None,
        "final_partition": [min(final_dates).isoformat(), max(final_dates).isoformat()] if final_dates else None,
        "common_sessions": len(common_dates),
        "final_test_touched": True,
    }
    prior_invalid_final_runs = _prior_invalid_final_runs(report_root)
    dates_for_run = set(common_dates)
    bars_by_symbol: dict[str, dict[str, list[Bar]]] = {}
    for symbol, bars in bars_1m.items():
        grouped: dict[date, list[Bar]] = {}
        for bar in bars:
            day = bar.time.astimezone(KST).date()
            if day in dates_for_run:
                grouped.setdefault(day, []).append(bar)
        bars_by_symbol[symbol] = {
            interval: [sample for day in common_dates for sample in resample_session_minutes(grouped.get(day, []), size)]
            for interval, size in (("15m", 15), ("30m", 30))
        }
    index_cache = ParquetBarCache(cache_root)
    kospi = index_cache.load("indexes", "kospi", "1d")
    kosdaq = index_cache.load("indexes", "kosdaq", "1d")
    dataset_hash, partition_hashes = _with_reference_dataset_hashes(cache_root, partition_hashes)
    stock_master = {row.symbol: row for row in load_stock_master(cache_root / "universe" / "stocks.parquet")}
    report: dict[str, Any] = {
        "run_id": datetime.now(KST).strftime("phase2-%Y%m%dT%H%M%S%z"),
        "git_sha": subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip(),
        "working_tree_dirty": bool(subprocess.run(["git", "status", "--porcelain"], check=True, capture_output=True, text=True).stdout.strip()),
        "dataset_sha256": dataset_hash,
        "dataset_partitions": partition_hashes,
        "symbols": sorted(bars_by_symbol),
        "universe_selection": "current KIS common-stock master, eligible flags and 1k-50k reference price, top 30 by market-cap field",
        "survivorship_bias": "current listed securities define the historical research cohort",
        "market_indexes": {
            "KOSPI": {"rows": len(kospi), "first": kospi[0].time.isoformat() if kospi else None,
                      "last": kospi[-1].time.isoformat() if kospi else None},
            "KOSDAQ": {"rows": len(kosdaq), "first": kosdaq[0].time.isoformat() if kosdaq else None,
                       "last": kosdaq[-1].time.isoformat() if kosdaq else None},
        },
        "period": [min(common_dates).isoformat(), max(common_dates).isoformat()] if common_dates else None,
        "split": split_meta,
        "holdout_integrity": {
            "state": "CONTAMINATED_PRIOR_INVALID_COHORT_RUN" if prior_invalid_final_runs else "UNTOUCHED",
            "prior_invalid_run_ids": prior_invalid_final_runs,
        },
        "capital_krw": 100_000,
        "position_cap": settings.max_concurrent_positions,
        "order_cap_krw": settings.max_order_notional_krw,
        "cost_assumptions": {
            "broker_fee_rate": settings.broker_fee_rate,
            "sell_tax_rate": settings.sell_tax_rate,
            "slippage_bps": settings.slippage_bps,
            "status": "ASSUMED",
        },
        "interval_results": {},
    }
    neighbor_results: dict[str, list[Metrics]] = {}
    for interval in ("15m", "30m"):
        interval_series = {symbol: rows[interval] for symbol, rows in bars_by_symbol.items() if rows[interval]}
        for strategy in ("breakout", "pullback"):
            key = f"{strategy}_{interval}"
            baseline = BreakoutConfig() if strategy == "breakout" else PullbackConfig()
            neighbors = (
                [BreakoutConfig(lookback=15, volume_lookback=15, volume_multiple=1.2),
                 BreakoutConfig(lookback=25, volume_lookback=25, volume_multiple=1.8)]
                if strategy == "breakout"
                else [PullbackConfig(impulse_lookback=4, impulse_volume_multiple=1.1),
                      PullbackConfig(impulse_lookback=6, impulse_volume_multiple=1.3)]
            )
            result_data: dict[str, Any] = {"baseline_parameters": asdict(baseline), "neighbors": []}
            if not final_dates or not interval_series:
                report["interval_results"][key] = {**result_data, "verdict": "INSUFFICIENT_SAMPLE", "reason": "NO_OOS_SESSION"}
                continue
            development_start = min(development_dates) if development_dates else min(common_dates)
            development_end = max(development_dates) if development_dates else development_start
            development_end_exclusive = date.fromordinal(development_end.toordinal() + 1)
            development_metrics, development_result, _ = _portfolio_metrics(
                interval_series, strategy, settings, kospi, kosdaq, config=baseline,
                start_date=development_start, end_date=development_end_exclusive,
                scanner="top10", cost_multiplier=1.0, top_n=top_n,
                warmup_date=max((day for day in common_dates if day < development_start), default=development_start),
            )
            result_data["development_baseline"] = {
                "parameters": asdict(baseline),
                "metrics": _metrics_dict(development_metrics),
                "trades": len(development_result.trades),
                "source_partition": "development_only",
            }
            validation_end = max(validation_dates) if validation_dates else development_end
            validation_start = min(validation_dates) if validation_dates else validation_end
            validation_end_exclusive = date.fromordinal(validation_end.toordinal() + 1)
            neighbor_metrics = []
            for config in neighbors:
                metrics, _result, _ = _portfolio_metrics(
                    interval_series, strategy, settings, kospi, kosdaq, config=config,
                    start_date=validation_start, end_date=validation_end_exclusive,
                    scanner="top10", cost_multiplier=1.0, top_n=top_n,
                    warmup_date=max((day for day in common_dates if day < validation_start), default=validation_start),
                )
                neighbor_metrics.append(metrics)
                result_data["neighbors"].append({
                    "parameters": asdict(config), "metrics": _metrics_dict(metrics), "trades": metrics.trade_count,
                    "source_partition": "validation_only",
                })
            final_start, final_end = min(final_dates), max(final_dates)
            final_end_exclusive = date.fromordinal(final_end.toordinal() + 1)
            final_warmup = max((day for day in common_dates if day < final_start), default=final_start)
            oos_metrics, oos_result, _ = _portfolio_metrics(
                interval_series, strategy, settings, kospi, kosdaq, config=baseline,
                start_date=final_start, end_date=final_end_exclusive, scanner="top10", cost_multiplier=1.0,
                top_n=top_n, warmup_date=final_warmup,
            )
            stress: dict[str, Any] = {}
            for multiplier in (1.5, 2.0):
                stress_metrics, stress_result, _ = _portfolio_metrics(
                    interval_series, strategy, settings, kospi, kosdaq, config=baseline,
                    start_date=final_start, end_date=final_end_exclusive, scanner="top10", cost_multiplier=multiplier,
                    top_n=top_n, warmup_date=final_warmup,
                )
                stress[f"{multiplier:.1f}x"] = _json_metrics(
                    stress_metrics, stress_result,
                    _trade_concentration(stress_result.trades, interval_series,
                                         {symbol: stock_master[symbol].market for symbol in interval_series if symbol in stock_master}),
                )
            gate = evaluate_strategy_gate(
                oos_metrics,
                _metrics_from_json(stress["1.5x"]["metrics"], stress["1.5x"]["trades"]),
                _metrics_from_json(stress["2.0x"]["metrics"], stress["2.0x"]["trades"]),
                neighbor_metrics,
            )
            if prior_invalid_final_runs:
                gate = type(gate)(
                    StrategyGateState.REJECTED,
                    (*gate.reasons, "FINAL_PARTITION_PREVIOUSLY_TOUCHED"),
                )
            ab: dict[str, Any] = {}
            for label, scanner, regime in (("all_regime_on", "all", "on"),
                                           ("top10_regime_on", "top10", "on"),
                                           ("top10_regime_off", "top10", "off")):
                membership = _scanner_membership(interval_series, top_n) if scanner == "top10" else None
                evaluate = _signal_function(
                    strategy, settings, regime, kospi, kosdaq, config=baseline,
                    start_date=final_start, end_date=final_end_exclusive, warmup_date=final_warmup,
                    scanner_membership=membership,
                )
                ab_result = run_portfolio_backtest(
                    interval_series, evaluate, starting_cash_krw=100_000, capital_cap_krw=100_000,
                    order_cap_krw=settings.max_order_notional_krw,
                    risk_per_trade_pct=settings.risk_per_trade_pct,
                    max_concurrent_positions=settings.max_concurrent_positions,
                    cost_model=CostModel(settings.broker_fee_rate, settings.sell_tax_rate, settings.slippage_bps),
                )
                ab[label] = _json_metrics(
                    calculate_metrics(ab_result), ab_result,
                    _trade_concentration(ab_result.trades, interval_series,
                                         {symbol: stock_master[symbol].market for symbol in interval_series if symbol in stock_master}),
                )
            concentration = _trade_concentration(
                oos_result.trades, interval_series,
                {symbol: stock_master[symbol].market for symbol in interval_series if symbol in stock_master},
            )
            result_data.update({
                "oos": _json_metrics(oos_metrics, oos_result, concentration),
                "cost_stress": stress,
                "scanner_regime_ab": ab,
                "gate": {"state": str(gate.state), "reasons": list(gate.reasons)},
                "verdict": "INSUFFICIENT_SAMPLE" if "TOO_FEW_OOS_TRADES" in gate.reasons
                else "PASS" if gate.state == StrategyGateState.ACCEPTED_FOR_SHADOW else "FAIL",
                "insufficient_sample": oos_metrics.trade_count < 30,
                "reasons": list(gate.reasons),
                "final_test_metrics_used_only_for_verdict": True,
            })
            neighbor_results[key] = neighbor_metrics
            report["interval_results"][key] = result_data
    accepted = [key for key, item in report["interval_results"].items() if item.get("verdict") == "PASS"]
    report["alpha"] = "PROVISIONAL" if accepted else "UNPROVEN"
    report["shadow"] = "READY" if accepted else "NOT_PROMOTED"
    report["live"] = "DISABLED"
    report["decision"] = {"shadow_candidate_exists": bool(accepted), "accepted_strategies": accepted}
    report_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = report_root / f"{report['run_id']}.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    report["report_path"] = str(target)
    return report


def run_baseline(
    settings: Settings,
    *,
    strategy: str,
    interval: str,
    cache_root: Path = Path("data"),
    report_root: Path = Path("runtime/research"),
) -> dict[str, Any]:
    if strategy not in {"breakout", "pullback"} or interval not in {"15m", "30m"}:
        raise ValueError("strategy must be breakout/pullback and interval must be 15m/30m")
    one_minute, dataset_hash, hashes = _load_research_bars(cache_root)
    if not one_minute:
        raise ValueError("no cached real minute dataset; run data build-research-set first")
    common_dates = sorted(set.intersection(*[
        {bar.time.astimezone(KST).date() for bar in bars} for bars in one_minute.values()
    ]))
    if not common_dates:
        raise ValueError("no common KRX sessions across the cached symbols")
    split = chronological_split([
        Bar(datetime.combine(day, datetime.min.time(), KST), 1, 1, 1, 1, 0) for day in common_dates
    ]) if len(common_dates) >= 2 else None
    development_dates = [bar.time.date() for bar in split.development] if split else common_dates[:1]
    if not development_dates:
        raise ValueError("not enough sessions for a development-only baseline")
    size = int(interval[:-1])
    series = {
        symbol: [
            resampled
            for session in development_dates
            for resampled in resample_session_minutes(
                [bar for bar in bars if bar.time.astimezone(KST).date() == session], size
            )
        ]
        for symbol, bars in one_minute.items()
    }
    cache = ParquetBarCache(cache_root)
    kospi = cache.load("indexes", "kospi", "1d")
    kosdaq = cache.load("indexes", "kosdaq", "1d")
    dataset_hash, hashes = _with_reference_dataset_hashes(cache_root, hashes)
    config = BreakoutConfig() if strategy == "breakout" else PullbackConfig()
    end_exclusive = date.fromordinal(development_dates[-1].toordinal() + 1)
    results = {}
    for multiplier in (1.0, 1.5, 2.0):
        metrics, portfolio, _ = _portfolio_metrics(
            series, strategy, settings, kospi, kosdaq, config=config,
            start_date=development_dates[0], end_date=end_exclusive,
            scanner="top10", cost_multiplier=multiplier, warmup_date=development_dates[0],
        )
        results[f"{multiplier:.1f}x"] = {
            "metrics": _metrics_dict(metrics), "trades": len(portfolio.trades),
            "net_pnl_krw": round(metrics.total_return_pct * 100_000 / 100, 2),
            "closed_trade_net_pnl_krw": round(sum(trade.net_pnl_krw for trade in portfolio.trades), 2),
        }
    report = {
        "run_id": datetime.now(KST).strftime("baseline-%Y%m%dT%H%M%S%z"),
        "scope": "DEVELOPMENT_BASELINE_ONLY; FINAL_NOT_TOUCHED",
        "git_sha": subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip(),
        "dataset_sha256": dataset_hash,
        "dataset_partitions": hashes,
        "strategy": strategy,
        "interval": interval,
        "symbols": sorted(series),
        "period": [development_dates[0].isoformat(), development_dates[-1].isoformat()],
        "parameters": asdict(config),
        "capital_krw": 100_000,
        "cost_status": "ASSUMED",
        "results": results,
    }
    report_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = report_root / f"{report['run_id']}.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    report["report_path"] = str(target)
    return report


def _metrics_from_json(values: dict[str, Any], trade_count: int) -> Metrics:
    fields = Metrics.__dataclass_fields__
    defaults: dict[str, Any] = {name: 0.0 for name in fields}
    defaults.update(values)
    defaults["trade_count"] = trade_count
    pf = defaults.get("profit_factor")
    defaults["profit_factor"] = float("inf") if pf == "INF" else pf
    return Metrics(**defaults)

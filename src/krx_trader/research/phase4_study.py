from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.metrics import calculate_metrics
from krx_trader.backtest.portfolio import run_portfolio_backtest
from krx_trader.data.resample import resample_session_minutes
from krx_trader.models import Bar, Decision, Signal
from krx_trader.research.anatomy import INTERVAL_MINUTES
from krx_trader.research.phase4 import (
    LOCKED_HOLDOUT_START,
    load_used_development_validation,
)
from krx_trader.strategies.retest import (
    DEFAULT_RETEST_CONFIG,
    BreakoutRetestConfig,
    BreakoutRetestMachine,
    RetestVariant,
    preregistration_config,
)

BASE_COST = CostModel(broker_fee_rate=0.00015, sell_tax_rate=0.002, slippage_bps=15.0)
STRESS_MULTIPLIERS = (1.0, 1.5, 2.0)
PORTFOLIO_SCENARIOS = ((20_000, 0.25), (30_000, 0.50), (50_000, 0.50))
PRICE_BUCKETS = ((1_000, 10_000, "1k_10k"), (10_000, 30_000, "10k_30k"),
                 (30_000, 50_000, "30k_50k"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_external_dates(start: date, end: date) -> None:
    if start > end:
        raise ValueError("external validation start must not follow its end")
    if start <= LOCKED_HOLDOUT_START <= end:
        raise ValueError("external validation block overlaps the locked Fresh Holdout")


def write_preregistration_manifest(
    *, freeze_commit_sha: str, cohort_manifest_path: Path, phase3_manifest_path: Path,
    output_path: Path, config: BreakoutRetestConfig = DEFAULT_RETEST_CONFIG,
    block_start: date = date(2026, 1, 5), block_end: date = date(2026, 4, 16),
) -> dict[str, Any]:
    """Freeze implementation, costs, broad cohort and unseen block before reading it."""
    if len(freeze_commit_sha) != 40 or any(char not in "0123456789abcdef" for char in freeze_commit_sha):
        raise ValueError("preregistration requires an exact lowercase Git commit SHA")
    validate_external_dates(block_start, block_end)
    cohort = json.loads(cohort_manifest_path.read_text(encoding="utf-8"))
    phase3 = json.loads(phase3_manifest_path.read_text(encoding="utf-8"))
    symbols = list(cohort["cohort_100"]["symbols"])
    if len(symbols) != len(set(symbols)) or len(symbols) != 100:
        raise ValueError("the frozen 100-symbol historical validation cohort is invalid")
    variant_rules = {variant.value: preregistration_config(variant, config) for variant in RetestVariant}
    strategy_source = Path(__file__).resolve().parents[2] / "krx_trader/strategies/retest.py"
    manifest = {
        "schema_version": 1,
        "artifact": "phase4-breakout-retest-preregistration",
        "status": "FROZEN_BEFORE_NEW_EVIDENCE",
        "freeze_commit_sha": freeze_commit_sha,
        "strategy_source_sha256": _sha256(strategy_source),
        "analysis_runner_sha256": _sha256(Path(__file__)),
        "variant_rules": variant_rules,
        "strategy_config_sha256": canonical_sha256(variant_rules),
        "cost_assumptions": {
            "broker_fee_rate": BASE_COST.broker_fee_rate,
            "sell_tax_rate": BASE_COST.sell_tax_rate,
            "slippage_bps": BASE_COST.slippage_bps,
            "classification": "ASSUMED_BASELINE",
            "stress_multipliers": list(STRESS_MULTIPLIERS),
        },
        "used_design_data": {
            "development": phase3["splits"]["development"]["sessions"][0] + ".." +
                phase3["splits"]["development"]["sessions"][-1],
            "validation_role": "SECONDARY_DIAGNOSTIC_ONLY",
            "validation": phase3["splits"]["validation"]["sessions"][0] + ".." +
                phase3["splits"]["validation"]["sessions"][-1],
            "fresh_holdout": "LOCKED_NOT_EVALUATED",
        },
        "new_external_block": {
            "start_date_inclusive": block_start.isoformat(),
            "end_date_inclusive": block_end.isoformat(),
            "symbols": symbols,
            "symbol_count": len(symbols),
            "cohort_rule": "cohort_100 from frozen CURRENT-LISTING cohort manifest",
            "cohort_manifest_sha256": _sha256(cohort_manifest_path),
            "survivorship_bias": cohort["survivorship_bias"],
            "intervals": ["15m", "30m"],
            "market_context_gate": "OFF",
            "config_changes_after_first_read": "PROHIBITED",
        },
        "portfolio_diagnostics": {
            "capital_krw": 100_000,
            "live_like_order_cap_krw": 20_000,
            "live_like_risk_pct": 0.25,
            "research_scenarios": [{"order_cap_krw": cap, "risk_pct": risk}
                                   for cap, risk in PORTFOLIO_SCENARIOS[1:]],
            "live_status": "DISABLED",
        },
        "holdout_access": {
            "boundary": LOCKED_HOLDOUT_START.isoformat(),
            "state": "LOCKED_NOT_EVALUATED",
            "allowed_for_external_block": False,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False,
                                      allow_nan=False) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(output_path)
    manifest["manifest_sha256"] = _sha256(output_path)
    return manifest


def inspect_frozen_block_cache(
    *, cache_root: Path, symbols: list[str], start: date, end: date,
) -> dict[str, Any]:
    """Check only explicit frozen pre-Development partition paths, after preregistration."""
    validate_external_dates(start, end)
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    present: list[str] = []
    for symbol in sorted(set(symbols)):
        for day in days:
            path = cache_root / "minute" / symbol / f"{day.isoformat()}.parquet"
            if path.is_file():
                present.append(f"{symbol}:{day.isoformat()}")
    return {
        "checked_period": [start.isoformat(), end.isoformat()],
        "symbols": len(set(symbols)),
        "calendar_days_checked": len(days),
        "existing_partition_count": len(present),
        "existing_partitions": present,
        "files_read": 0,
        "holdout_paths_checked": 0,
    }


def write_external_validation_unavailable(
    *, preregistration_path: Path, output_path: Path, cache_audit: dict[str, Any],
    credentials_available: bool,
) -> dict[str, Any]:
    """Record unavailable validation without touching .env or expanding data access."""
    preregistration = json.loads(preregistration_path.read_text(encoding="utf-8"))
    if preregistration.get("status") != "FROZEN_BEFORE_NEW_EVIDENCE":
        raise ValueError("external validation plan cannot run before strategy freeze")
    period = preregistration["new_external_block"]
    credential_state = "available" if credentials_available else "absent"
    existing_paths = cache_audit["existing_partition_count"]
    report = {
        "schema_version": 1,
        "artifact": "phase4-external-validation",
        "status": "NOT_AVAILABLE",
        "preregistration_commit_sha": preregistration["freeze_commit_sha"],
        "preregistration_manifest_sha256": _sha256(preregistration_path),
        "strategy_config_sha256": preregistration["strategy_config_sha256"],
        "strategy_source_sha256": preregistration["strategy_source_sha256"],
        "period": [period["start_date_inclusive"], period["end_date_inclusive"]],
        "symbols_sha256": canonical_sha256(period["symbols"]),
        "symbols": len(period["symbols"]),
        "cohort_manifest_sha256": period["cohort_manifest_sha256"],
        "cache_audit": cache_audit,
        "credentials_available_in_process_environment": credentials_available,
        "dotenv_read": False,
        "minute_partition_files_read": 0,
        "external_fetch_attempted": False,
        "reason": (
            "Complete frozen-block symbol/session coverage was not verified; "
            f"path-only cache inspection found {existing_paths} candidate partition files. "
            f"KIS minute-history credentials were {credential_state}; no external fetch was attempted; "
            ".env was not read."
        ),
        "fresh_holdout": {
            "state": "LOCKED_NOT_EVALUATED",
            "partition_files_opened": 0,
            "features_signals_pnl_or_index_context_read": False,
        },
        "verdict": "NEED_PROSPECTIVE_EVIDENCE",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                      allow_nan=False) + "\n", encoding="utf-8")
    return report


def _quantile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _price_bucket(price: float | None) -> str:
    if price is None:
        return "UNKNOWN"
    for lower, upper, label in PRICE_BUCKETS:
        if lower <= price < upper or (label == PRICE_BUCKETS[-1][2] and price == upper):
            return label
    return "OUT_OF_RANGE"


def _turnover_before(bars: list[Bar], index: int, window: int = 20) -> float | None:
    prior = bars[max(0, index - window):index]
    return statistics.fmean(bar.close * bar.volume for bar in prior) if prior else None


def _near_gap(
    bars: list[Bar], start_index: int, end_index: int, interval_minutes: int,
    incomplete_partitions: set[tuple[str, date]], symbol: str,
) -> bool:
    relevant = bars[max(0, start_index - 20):min(len(bars), end_index + 1)]
    dates = {bar.time.date() for bar in relevant}
    if any((symbol, day) in incomplete_partitions for day in dates):
        return True
    limit_seconds = interval_minutes * 60
    for left, right in pairwise(relevant):
        if left.time.date() == right.time.date() and (right.time - left.time).total_seconds() > limit_seconds:
            return True
    return False


def _one_share_execution(
    bars: list[Bar], signal_index: int, *, signal_close: float, stop_price: float,
    max_holding_bars: int, cost: CostModel = BASE_COST, stress: float = 1.0,
) -> dict[str, Any]:
    entry_index = signal_index + 1
    if entry_index >= len(bars):
        return {"status": "NO_NEXT_BAR", "entry_index": None}
    entry_bar = bars[entry_index]
    entry_reference = entry_bar.open
    if entry_reference <= 0 or stop_price <= 0:
        return {"status": "INVALID_ENTRY_STOP", "entry_index": entry_index,
                "entry_time": entry_bar.time.isoformat(), "entry_reference": entry_reference}

    entry_fill = cost.buy_fill_price(entry_reference, stress)
    if entry_fill <= stop_price:
        return {"status": "UNFILLED_ENTRY_NOT_ABOVE_STOP", "entry_index": entry_index,
                "entry_time": entry_bar.time.isoformat(), "entry_reference": entry_reference,
                "entry_fill": entry_fill, "stop_price": stop_price}
    if entry_fill > entry_bar.high:
        return {"status": "UNFILLED_SLIPPAGE_ABOVE_NEXT_BAR_HIGH", "entry_index": entry_index,
                "entry_time": entry_bar.time.isoformat(), "entry_reference": entry_reference,
                "entry_fill": entry_fill, "stop_price": stop_price}
    entry_fee = cost.buy_cost(entry_fill, stress)
    best_high = entry_bar.high
    worst_low = entry_bar.low
    mfe_index = entry_index
    bars_held = 0
    exit_next_open = False
    exit_bar: Bar | None = None
    exit_reference: float | None = None
    exit_reason: str | None = None
    exit_index: int | None = None

    for index in range(entry_index, len(bars)):
        bar = bars[index]
        if index > entry_index:
            if exit_next_open:
                exit_bar, exit_index, exit_reference, exit_reason = bar, index, bar.open, "TIME_EXIT"
                break
            if bar.high > best_high:
                best_high = bar.high
                mfe_index = index
            worst_low = min(worst_low, bar.low)
            if bar.low <= stop_price:
                exit_bar, exit_index = bar, index
                exit_reference, exit_reason = min(bar.open, stop_price), "STOP"
                break
            bars_held += 1
            if bars_held >= max_holding_bars:
                exit_next_open = True
        else:
            if bar.low <= stop_price:
                exit_bar, exit_index = bar, index
                exit_reference, exit_reason = min(bar.open, stop_price), "STOP"
                break
            bars_held = 0

    signal_path_bars = bars[entry_index:(exit_index + 1 if exit_index is not None else len(bars))]
    signal_mfe = max((bar.high for bar in signal_path_bars), default=signal_close)
    signal_mae = min((bar.low for bar in signal_path_bars), default=signal_close)
    mfe_pct = (best_high / entry_fill - 1) * 100 if entry_fill else None
    mae_pct = (worst_low / entry_fill - 1) * 100 if entry_fill else None
    base = {
        "status": "OPEN_AT_SEGMENT_END" if exit_bar is None else "CLOSED",
        "entry_index": entry_index,
        "entry_time": entry_bar.time.isoformat(),
        "entry_reference": entry_reference,
        "entry_fill": entry_fill,
        "stop_price": stop_price,
        "signal_mfe_pct": (signal_mfe / signal_close - 1) * 100 if signal_close else None,
        "signal_mae_pct": (signal_mae / signal_close - 1) * 100 if signal_close else None,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "time_to_mfe_bars": max(0, mfe_index - entry_index),
        "time_to_failure_bars": (max(1, exit_index - entry_index)
                                  if exit_reason == "STOP" and exit_index is not None else None),
        "unrealized_gross_pnl_krw": (bars[-1].close - entry_reference) if exit_bar is None else None,
    }
    if exit_bar is None:
        return base

    assert exit_reference is not None and exit_reason is not None
    exit_fill = cost.sell_fill_price(exit_reference, stress)
    exit_fee = cost.buy_cost(exit_fill, stress)
    tax = exit_fill * cost.sell_tax_rate * stress
    slippage = (entry_fill - entry_reference) + (exit_reference - exit_fill)
    gross = exit_reference - entry_reference
    net = gross - entry_fee - exit_fee - tax - slippage
    base.update({
        "exit_time": exit_bar.time.isoformat(),
        "exit_reference": exit_reference,
        "exit_fill": exit_fill,
        "exit_reason": exit_reason,
        "gross_pnl_krw": gross,
        "fee_krw": entry_fee + exit_fee,
        "tax_krw": tax,
        "slippage_krw": slippage,
        "cost_krw": entry_fee + exit_fee + tax + slippage,
        "net_pnl_krw": net,
        "holding_bars": max(1, (exit_index - entry_index + 1) if exit_index is not None else 1),
    })
    return base


def _event_metrics(events: list[dict[str, Any]], sessions: int, no_follow_threshold: float | None) -> dict[str, Any]:
    closed = [event for event in events if event.get("execution_status") == "CLOSED"]
    net = [float(event["net_pnl_krw"]) for event in closed]
    gross = [float(event["gross_pnl_krw"]) for event in closed]
    winners = [value for value in net if value > 0]
    losers = [value for value in net if value < 0]
    gross_winners = sum(value for value in gross if value > 0)
    gross_losers = abs(sum(value for value in gross if value < 0))
    total_net = sum(net)
    immediate = [
        event for event in closed
        if event.get("exit_reason") == "STOP" and
        event.get("time_to_failure_bars") is not None and int(event["time_to_failure_bars"]) <= 2
    ]
    follow_rows = [event for event in closed if event.get("signal_mfe_pct") is not None]
    no_follow = ([event for event in follow_rows if float(event["signal_mfe_pct"]) < no_follow_threshold]
                 if no_follow_threshold is not None else [])
    by_symbol: dict[str, float] = defaultdict(float)
    by_day: dict[str, float] = defaultdict(float)
    by_market: dict[str, float] = defaultdict(float)
    for event in closed:
        pnl = float(event["net_pnl_krw"])
        by_symbol[str(event["symbol"])] += pnl
        by_day[str(event["entry_time"])[:10]] += pnl
        by_market[str(event.get("market", "UNKNOWN"))] += pnl
    profitable = sum(winners)
    top_five = sum(sorted(winners, reverse=True)[:5])
    pf = sum(winners) / abs(sum(losers)) if losers else ("INF" if winners else None)
    return {
        "signals": len(events),
        "closed_trades": len(closed),
        "open_at_segment_end": sum(event.get("execution_status") == "OPEN_AT_SEGMENT_END" for event in events),
        "unexecutable": sum(event.get("execution_status") not in {"CLOSED", "OPEN_AT_SEGMENT_END"} for event in events),
        "wins": len(winners),
        "losses": len(losers),
        "gross_pnl_krw": sum(gross),
        "gross_expectancy_krw": statistics.fmean(gross) if gross else None,
        "net_pnl_krw": total_net,
        "expectancy_krw": statistics.fmean(net) if net else None,
        "profit_factor": pf,
        "cost_drag_krw": sum(float(event.get("cost_krw", 0)) for event in closed),
        "mfe_pct_p50": _quantile([float(event["mfe_pct"]) for event in closed if event.get("mfe_pct") is not None], .5),
        "mae_pct_p50": _quantile([float(event["mae_pct"]) for event in closed if event.get("mae_pct") is not None], .5),
        "time_to_mfe_bars_p50": _quantile([float(event["time_to_mfe_bars"]) for event in closed
                                             if event.get("time_to_mfe_bars") is not None], .5),
        "time_to_stop_bars_p50": _quantile([float(event["time_to_failure_bars"]) for event in closed
                                              if event.get("time_to_failure_bars") is not None], .5),
        "immediate_rejection_count": len(immediate),
        "immediate_rejection_pct_of_closed": len(immediate) / len(closed) * 100 if closed else None,
        "no_follow_through_threshold_signal_mfe_pct": no_follow_threshold,
        "no_follow_through_count": len(no_follow),
        "no_follow_through_pct_of_closed": len(no_follow) / len(follow_rows) * 100 if follow_rows else None,
        "signals_per_session": len(events) / sessions if sessions else None,
        "closed_trades_per_session": len(closed) / sessions if sessions else None,
        "closed_trades_per_five_session_week": len(closed) / sessions * 5 if sessions else None,
        "concentration": {
            "top_trade_share_of_positive_pnl_pct": max(winners) / profitable * 100 if profitable else None,
            "top_five_trades_share_of_positive_pnl_pct": top_five / profitable * 100 if profitable else None,
            "top_symbol": max(by_symbol, key=by_symbol.get) if by_symbol else None,
            "top_symbol_net_pnl_krw": max(by_symbol.values()) if by_symbol else None,
            "top_day": max(by_day, key=by_day.get) if by_day else None,
            "top_day_net_pnl_krw": max(by_day.values()) if by_day else None,
            "market_net_pnl_krw": dict(sorted(by_market.items())),
        },
        "win_rate_pct": len(winners) / len(net) * 100 if net else None,
        "gross_profit_factor": gross_winners / gross_losers if gross_losers else ("INF" if gross_winners else None),
        "mdd_pct": None,
        "mdd_scope": "NOT_MEANINGFUL_FOR_OVERLAPPING_INDEPENDENT_ONE_SHARE_EVENT_DIAGNOSTICS",
    }


def _stress_metrics(
    events: list[dict[str, Any]], sessions: int, no_follow_threshold: float | None,
    multiplier: float,
) -> dict[str, Any]:
    key = f"{multiplier:.1f}x"
    adjusted: list[dict[str, Any]] = []
    for event in events:
        stress = event.get("stress_execution", {}).get(key)
        if not isinstance(stress, dict):
            continue
        normalized = dict(event)
        status = str(stress.get("status", "UNFILLED"))
        normalized.update({
            "execution_status": status if status in {"CLOSED", "OPEN_AT_SEGMENT_END"} else "UNFILLED",
            "entry_time": stress.get("entry_time") or event.get("acceptance_time"),
            "exit_time": stress.get("exit_time"),
            "exit_reason": stress.get("exit_reason"),
            "gross_pnl_krw": stress.get("gross_pnl_krw"),
            "cost_krw": stress.get("cost_krw", 0.0),
            "net_pnl_krw": stress.get("net_pnl_krw"),
            "mfe_pct": stress.get("mfe_pct"),
            "mae_pct": stress.get("mae_pct"),
            "signal_mfe_pct": stress.get("signal_mfe_pct"),
            "signal_mae_pct": stress.get("signal_mae_pct"),
            "time_to_mfe_bars": stress.get("time_to_mfe_bars"),
            "time_to_failure_bars": stress.get("time_to_failure_bars"),
        })
        adjusted.append(normalized)
    return _event_metrics(adjusted, sessions, no_follow_threshold)


def _simulate_event(
    bars: list[Bar], signal_index: int, *, signal_time: str, signal_close: float,
    stop_price: float, symbol: str, market: str, interval: str, split: str,
    setup_id: str | None, breakout_time: str | None, breakout_level: float | None,
    breakout_close: float | None, breakout_relative_volume: float | None,
    bars_since_breakout: int | None, bars_to_retest: int | None, retest_time: str | None,
    retest_low: float | None, retest_close: float | None, retest_depth_pct: float | None,
    level_penetration_pct: float | None, interval_minutes: int,
    incomplete_partitions: set[tuple[str, date]], max_holding_bars: int,
) -> dict[str, Any]:
    executions = {
        f"{multiplier:.1f}x": _one_share_execution(
            bars, signal_index, signal_close=signal_close, stop_price=stop_price,
            max_holding_bars=max_holding_bars, stress=multiplier,
        ) for multiplier in STRESS_MULTIPLIERS
    }
    execution = executions["1.0x"]
    entry_reference = execution.get("entry_reference")
    entry_fill = execution.get("entry_fill")
    if entry_reference is not None and entry_fill is not None:
        risk = (
            float(entry_fill) - BASE_COST.sell_fill_price(stop_price)
            + BASE_COST.buy_cost(float(entry_fill))
            + BASE_COST.sell_cost(BASE_COST.sell_fill_price(stop_price))
        )
    else:
        risk = None
    next_index = signal_index + 1
    end_index = (next_index if execution.get("entry_index") is not None else signal_index)
    if execution.get("exit_time"):
        exit_time = datetime.fromisoformat(execution["exit_time"])
        end_index = next((i for i, bar in enumerate(bars) if bar.time == exit_time), end_index)
    near_gap = _near_gap(bars, signal_index, end_index, interval_minutes, incomplete_partitions, symbol)
    event = {
        "setup_id": setup_id,
        "symbol": symbol,
        "market": market,
        "interval": interval,
        "split": split,
        "breakout_time": breakout_time,
        "breakout_level": breakout_level,
        "breakout_close": breakout_close,
        "breakout_relative_volume": breakout_relative_volume,
        "bars_since_breakout": bars_since_breakout,
        "bars_to_retest": bars_to_retest,
        "retest_time": retest_time,
        "retest_low": retest_low,
        "retest_close": retest_close,
        "retest_depth_pct": retest_depth_pct,
        "level_penetration_pct": level_penetration_pct,
        "acceptance_time": signal_time,
        "acceptance_close": signal_close,
        "entry_time": execution.get("entry_time"),
        "entry_reference": entry_reference,
        "entry_fill": entry_fill,
        "stop_price": stop_price,
        "exit_time": execution.get("exit_time"),
        "exit_reference": execution.get("exit_reference"),
        "exit_fill": execution.get("exit_fill"),
        "exit_reason": execution.get("exit_reason"),
        "gross_pnl_krw": execution.get("gross_pnl_krw"),
        "fee_krw": execution.get("fee_krw"),
        "tax_krw": execution.get("tax_krw"),
        "slippage_krw": execution.get("slippage_krw"),
        "cost_krw": execution.get("cost_krw"),
        "net_pnl_krw": execution.get("net_pnl_krw"),
        "mfe_pct": execution.get("mfe_pct"),
        "mae_pct": execution.get("mae_pct"),
        "signal_mfe_pct": execution.get("signal_mfe_pct"),
        "signal_mae_pct": execution.get("signal_mae_pct"),
        "time_to_mfe_bars": execution.get("time_to_mfe_bars"),
        "time_to_failure_bars": execution.get("time_to_failure_bars"),
        "execution_status": execution["status"] if execution["status"] in {
            "CLOSED", "OPEN_AT_SEGMENT_END",
        } else "UNFILLED",
        "execution_detail_status": execution["status"],
        "near_data_gap": near_gap,
        "entry_price_bucket": _price_bucket(float(entry_reference)) if entry_reference is not None else "UNKNOWN",
        "one_share_affordable_20k": (
            execution["status"] in {"CLOSED", "OPEN_AT_SEGMENT_END"} and
            entry_reference is not None and entry_fill is not None and
            float(entry_fill) + BASE_COST.buy_cost(float(entry_fill)) <= 20_000
        ),
        "one_share_risk_eligible_250krw": (
            execution["status"] in {"CLOSED", "OPEN_AT_SEGMENT_END"} and risk is not None and risk <= 250
        ),
        "stress_execution": executions,
    }
    return event


def _segment_histories(
    by_session: dict[str, dict[date, list[Bar]]], split_dates: dict[str, list[date]], split: str,
) -> tuple[dict[str, list[Bar]], dict[str, list[Bar]]]:
    dates = sorted(split_dates[split])
    start = dates[0]
    selected: dict[str, list[Bar]] = {}
    warmup: dict[str, list[Bar]] = {}
    for symbol, sessions in by_session.items():
        selected[symbol] = [bar for day in dates for bar in sessions.get(day, [])]
        prior_dates = [day for day in sessions if day < start]
        warmup[symbol] = [bar for day in sorted(prior_dates) for bar in sessions[day]]
    return selected, warmup


def _scan_retest_setups(
    bars_by_symbol: dict[str, list[Bar]], warmup_by_symbol: dict[str, list[Bar]], *,
    variant: RetestVariant, interval: str, split: str, market_by_symbol: dict[str, str],
    incomplete_partitions: set[tuple[str, date]], config: BreakoutRetestConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    interval_minutes = INTERVAL_MINUTES[interval]
    setups: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    for symbol, bars in sorted(bars_by_symbol.items()):
        if not bars:
            continue
        warmup = warmup_by_symbol.get(symbol, [])
        history = [*warmup, *bars]
        first_current = len(warmup)
        machine = BreakoutRetestMachine(variant, config)
        for index in range(first_current, len(history)):
            machine.update(history[:index + 1], symbol, interval)
        indices = {bar.time: index for index, bar in enumerate(history)}
        for setup in machine.setups:
            row = setup.as_dict()
            row.update({"symbol": symbol, "market": market_by_symbol.get(symbol, "UNKNOWN"),
                        "interval": interval, "split": split})
            setups.append(row)
            if not setup.acceptance_time:
                continue
            signal_time = datetime.fromisoformat(setup.acceptance_time)
            signal_index = indices.get(signal_time)
            if signal_index is None or signal_index < first_current:
                raise ValueError("accepted setup does not map to a current split bar")
            turnover = _turnover_before(history, setup.breakout_index)
            event = _simulate_event(
                history, signal_index,
                signal_time=signal_time.isoformat(), signal_close=float(setup.acceptance_close),
                stop_price=float(setup.stop_price), symbol=symbol,
                market=market_by_symbol.get(symbol, "UNKNOWN"), interval=interval, split=split,
                setup_id=setup.setup_id, breakout_time=setup.breakout_time,
                breakout_level=setup.breakout_level, breakout_close=setup.breakout_close,
                breakout_relative_volume=setup.breakout_relative_volume,
                bars_since_breakout=setup.bars_since_breakout,
                bars_to_retest=setup.bars_to_retest or
                    (indices.get(datetime.fromisoformat(setup.retest_time), signal_index) - setup.breakout_index
                     if setup.retest_time else None),
                retest_time=setup.retest_time, retest_low=setup.retest_low,
                retest_close=setup.retest_close, retest_depth_pct=setup.retest_depth_pct,
                level_penetration_pct=setup.level_penetration_pct,
                interval_minutes=interval_minutes, incomplete_partitions=incomplete_partitions,
                max_holding_bars=config.max_holding_bars,
            )
            event["breakout_turnover_20bar_mean_krw"] = turnover
            event["breakout_signal_key"] = f"{symbol}|{setup.breakout_time}"
            accepted.append(event)
    return setups, accepted


def _v1_events(
    rows: list[dict[str, Any]], bars_by_symbol: dict[str, list[Bar]], warmup_by_symbol: dict[str, list[Bar]],
    *, interval: str, split: str, market_by_symbol: dict[str, str],
    incomplete_partitions: set[tuple[str, date]], config: BreakoutRetestConfig,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row["symbol"])
        history = [*warmup_by_symbol.get(symbol, []), *bars_by_symbol.get(symbol, [])]
        timestamp = datetime.fromisoformat(str(row["signal_timestamp"]))
        indices = {bar.time: index for index, bar in enumerate(history)}
        index = indices.get(timestamp)
        if index is None:
            continue
        event = _simulate_event(
            history, index, signal_time=timestamp.isoformat(), signal_close=float(row["signal_price"]),
            stop_price=float(row["stop_price"]), symbol=symbol,
            market=market_by_symbol.get(symbol, str(row.get("market", "UNKNOWN"))),
            interval=interval, split=split, setup_id=str(row["signal_id"]),
            breakout_time=timestamp.isoformat(), breakout_level=None,
            breakout_close=float(row["signal_price"]), breakout_relative_volume=row.get("relative_volume"),
            bars_since_breakout=0, bars_to_retest=None, retest_time=None, retest_low=None,
            retest_close=None, retest_depth_pct=None, level_penetration_pct=None,
            interval_minutes=INTERVAL_MINUTES[interval], incomplete_partitions=incomplete_partitions,
            max_holding_bars=config.max_holding_bars,
        )
        event["breakout_turnover_20bar_mean_krw"] = row.get("turnover_krw")
        event["breakout_signal_key"] = f"{symbol}|{timestamp.isoformat()}"
        event["source_phase3_net_pnl_krw"] = row.get("net_pnl_krw")
        event["source_phase3_execution_status"] = row.get("execution_status")
        event["replay_execution_status"] = event["execution_status"]
        event["replay_exit_time"] = event.get("exit_time")
        event["replay_net_pnl_krw"] = event.get("net_pnl_krw")
        event["execution_status"] = row.get("execution_status")
        event["exit_time"] = row.get("exit_time")
        event["exit_reason"] = row.get("exit_reason")
        event["gross_pnl_krw"] = row.get("gross_pnl_krw")
        event["fee_krw"] = row.get("fee_krw")
        event["tax_krw"] = row.get("tax_krw")
        event["slippage_krw"] = row.get("slippage_krw")
        event["cost_krw"] = sum(float(row.get(name) or 0) for name in ("fee_krw", "tax_krw", "slippage_krw"))
        event["net_pnl_krw"] = row.get("net_pnl_krw")
        event["mfe_pct"] = row.get("mfe_pct")
        event["mae_pct"] = row.get("mae_pct")
        event["signal_mfe_pct"] = row.get("signal_mfe_pct")
        event["signal_mae_pct"] = row.get("signal_mae_pct")
        event["time_to_mfe_bars"] = row.get("time_to_mfe_bars")
        event["time_to_failure_bars"] = row.get("time_to_failure_bars")
        event["entry_time"] = event.get("entry_time") or timestamp.isoformat()
        result.append(event)
    return result


def _dev_liquidity_edges(events: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    values = [float(event["breakout_turnover_20bar_mean_krw"]) for event in events
              if event["split"] == "development" and event.get("breakout_turnover_20bar_mean_krw") is not None]
    return _quantile(values, 1 / 3), _quantile(values, 2 / 3)


def _annotate_buckets(events: list[dict[str, Any]], edges: tuple[float | None, float | None]) -> None:
    low, high = edges
    for event in events:
        value = event.get("breakout_turnover_20bar_mean_krw")
        if value is None or low is None or high is None:
            event["liquidity_bucket"] = "UNKNOWN"
        elif float(value) <= low:
            event["liquidity_bucket"] = "LOW_TURNOVER"
        elif float(value) <= high:
            event["liquidity_bucket"] = "MID_TURNOVER"
        else:
            event["liquidity_bucket"] = "HIGH_TURNOVER"


def _bucket_metrics(events: list[dict[str, Any]], sessions: int, threshold: float | None,
                    key: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(event.get(key, "UNKNOWN"))].append(event)
    return {label: _event_metrics(rows, sessions, threshold) for label, rows in sorted(grouped.items())}


def _portfolio_results(
    bars_by_symbol: dict[str, list[Bar]], events: list[dict[str, Any]], *,
    cost: CostModel = BASE_COST, clean_only: bool = False,
) -> dict[str, Any]:
    selected = [event for event in events if not clean_only or not event["near_data_gap"]]
    signal_map = {
        (event["symbol"], datetime.fromisoformat(event["acceptance_time"])): event
        for event in selected if event.get("execution_status") != "NO_NEXT_BAR"
    }
    sessions = len({bar.time.date() for values in bars_by_symbol.values() for bar in values})
    timeline = sorted({bar.time for values in bars_by_symbol.values() for bar in values})
    report: dict[str, Any] = {"selected_signals": len(selected), "clean_window_only": clean_only,
                              "scenarios": {}}
    for order_cap, risk_pct in PORTFOLIO_SCENARIOS:
        def signal_fn(symbol: str, history: list[Bar]) -> Signal:
            bar = history[-1]
            event = signal_map.get((symbol, bar.time))
            if event is None:
                return Signal(bar.time, symbol, "phase4_retest", Decision.HOLD, ("NO_FROZEN_ENTRY",))
            return Signal(
                bar.time, symbol, "phase4_retest", Decision.ENTER,
                ("BREAKOUT_RETEST_ACCEPTED",), reference_price=event["breakout_level"],
                stop_price=event["stop_price"], max_holding_bars=10,
            )

        scenario: dict[str, Any] = {}
        for multiplier in STRESS_MULTIPLIERS:
            result = run_portfolio_backtest(
                bars_by_symbol, signal_fn, starting_cash_krw=100_000,
                capital_cap_krw=100_000, order_cap_krw=order_cap,
                risk_per_trade_pct=risk_pct, min_price_krw=1_000,
                max_price_krw=50_000, max_concurrent_positions=2,
                cost_model=cost, stress_multiplier=multiplier,
            )
            metrics = calculate_metrics(result)
            invested_ticks = sum(
                trade.entry_price * trade.quantity
                for trade in result.trades for timestamp in timeline
                if trade.entry_time <= timestamp <= trade.exit_time
            )
            average_invested = invested_ticks / len(timeline) if timeline else 0.0
            key = f"{order_cap}_krw_{risk_pct:.2f}pct_{multiplier:.1f}x"
            scenario[key] = {
                "starting_capital_krw": 100_000,
                "accepted_signals": len(selected),
                "executable_next_bar_signals": sum(event.get("entry_time") is not None for event in selected),
                "portfolio_entry_decisions": sum(signal.decision == Decision.ENTER for signal in result.decisions),
                "fills": len(result.trades),
                "execution_rate_pct": len(result.trades) / len(selected) * 100 if selected else None,
                "ending_equity_krw": round(result.equity_curve[-1], 2) if result.equity_curve else result.ending_cash_krw,
                "net_pnl_krw": round((result.equity_curve[-1] if result.equity_curve else result.ending_cash_krw)
                                     - result.starting_cash_krw, 2),
                "profit_factor": metrics.profit_factor if metrics.profit_factor is None or
                    math.isfinite(metrics.profit_factor) else "INF",
                "expectancy_krw": metrics.expectancy_krw,
                "mdd_pct": metrics.max_drawdown_pct,
                "average_invested_capital_krw": round(average_invested, 2),
                "average_idle_cash_krw": round(100_000 - average_invested, 2),
                "capital_utilization_pct": round(average_invested / 100_000 * 100, 2),
                "closed_trade_count": metrics.trade_count,
            }
        report["scenarios"][f"order_cap_{order_cap}_risk_{risk_pct:.2f}pct"] = scenario
    report["sessions"] = sessions
    return report


def _removal_attribution(
    v1: list[dict[str, Any]], accepted: list[dict[str, Any]], sessions: int,
) -> dict[str, Any]:
    kept_keys = {event["breakout_signal_key"] for event in accepted}
    kept = [event for event in v1 if event["breakout_signal_key"] in kept_keys]
    removed = [event for event in v1 if event["breakout_signal_key"] not in kept_keys]
    threshold = None
    kept_metrics = _event_metrics(kept, sessions, threshold)
    removed_metrics = _event_metrics(removed, sessions, threshold)
    return {
        "kept_v1_immediate_entry": kept_metrics,
        "removed_v1_immediate_entry": removed_metrics,
        "removed_v1_signals": len(removed),
        "kept_v1_signals": len(kept),
        "removed_worse_net_expectancy": (
            removed_metrics["expectancy_krw"] is not None and
            kept_metrics["expectancy_krw"] is not None and
            removed_metrics["expectancy_krw"] < kept_metrics["expectancy_krw"]
        ),
    }


def run_retest_study(
    *, cache_root: Path, phase3_manifest_path: Path, phase3_events_root: Path,
    cohort_manifest_path: Path, output_dir: Path, git_sha: str,
    config: BreakoutRetestConfig = DEFAULT_RETEST_CONFIG,
) -> dict[str, Any]:
    """Run only used Development and secondary Validation evidence for the fixed 30-symbol cohort."""
    _, minute_by_session, partition_hashes, incomplete, splits = load_used_development_validation(
        cache_root, phase3_manifest_path,
    )
    manifest = json.loads(phase3_manifest_path.read_text(encoding="utf-8"))
    allowed_dates = set(splits["development"] + splits["validation"])
    if any(day >= LOCKED_HOLDOUT_START for daily in minute_by_session.values() for day in daily):
        raise ValueError("Phase 4 research loader exposed a locked Holdout date")
    selected_hash = hashlib.sha256(
        "\n".join(f"{key}:{value}" for key, value in sorted(partition_hashes.items())).encode()
    ).hexdigest()
    cohort_manifest = json.loads(cohort_manifest_path.read_text(encoding="utf-8"))
    baseline_symbols = list(manifest["succeeded_symbols"])
    if not set(baseline_symbols).issubset(set(cohort_manifest["cohort_60"]["symbols"])):
        raise ValueError("frozen expanded cohort does not preserve the Phase 2.5 baseline")
    market_by_symbol = {symbol: "KOSPI" for symbol in baseline_symbols}
    for symbol in baseline_symbols:
        if symbol not in minute_by_session:
            raise ValueError(f"frozen baseline symbol missing from loaded sessions: {symbol}")

    output_dir.mkdir(parents=True, exist_ok=True)
    frozen_variant_configs = {
        variant.value: preregistration_config(variant, config) for variant in RetestVariant
    }
    frozen_config_hash = canonical_sha256(frozen_variant_configs)
    strategy_source_path = Path(__file__).resolve().parents[2] / "krx_trader/strategies/retest.py"
    outputs: dict[str, Any] = {}
    for interval, minutes in INTERVAL_MINUTES.items():
        all_interval: dict[str, list[Bar]] = {
            symbol: resample_session_minutes(
                [bar for day in sorted(daily) if day in allowed_dates for bar in daily[day]], minutes,
            ) for symbol, daily in minute_by_session.items()
        }
        event_path = phase3_events_root / f"breakout-signals-{interval}.parquet"
        anatomy_path = phase3_events_root / f"breakout-anatomy-{interval}.json"
        if not event_path.is_file() or not anatomy_path.is_file():
            raise ValueError(f"Phase 3 {interval} signal/anatomy artifacts are needed as the V1 comparator")
        phase3_rows = pq.read_table(event_path).to_pylist()
        if any(row.get("split") not in {"development", "validation"} for row in phase3_rows):
            raise ValueError("V1 comparator contains a row outside permitted used evidence")
        anatomy = json.loads(anatomy_path.read_text(encoding="utf-8"))
        if anatomy.get("holdout_integrity", {}).get("state") != "LOCKED_NOT_EVALUATED":
            raise ValueError("Phase 3 V1 artifact has unexpected Holdout state")
        interval_report: dict[str, Any] = {
            "schema_version": 1,
            "artifact": f"phase4-retest-anatomy-{interval}",
            "git_sha": git_sha,
            "source_file_sha256": {
                "retest_strategy": _sha256(strategy_source_path),
                "phase4_study": _sha256(Path(__file__)),
            },
            "dataset_scope": "ORIGINAL_30_KOSPI_USED_DEVELOPMENT_AND_SECONDARY_VALIDATION",
            "source_dataset_sha256": manifest["dataset_sha256"],
            "selected_partition_sha256": selected_hash,
            "cohort_manifest_sha256": _sha256(cohort_manifest_path),
            "cohort_status": "BREADTH_DIAGNOSTIC_METADATA_ONLY; no expanded-cohort minute bars available",
            "phase3_v1_source_sha256": _sha256(event_path),
            "phase3_v1_anatomy_sha256": _sha256(anatomy_path),
            "strategy_config": {
                "variant_rules": frozen_variant_configs,
                "strategy_config_sha256": frozen_config_hash,
            },
            "cost_assumptions": {"broker_fee_rate": BASE_COST.broker_fee_rate,
                                 "sell_tax_rate": BASE_COST.sell_tax_rate,
                                 "slippage_bps": BASE_COST.slippage_bps,
                                 "source": "ASSUMED_PHASE3_BASELINE", "stress": list(STRESS_MULTIPLIERS)},
            "regime_gate": "OFF",
            "raw_regime_context": "NOT_AVAILABLE; Phase 3 index context was not read because its row group overlaps the locked Holdout.",
            "split_periods": {name: [days[0].isoformat(), days[-1].isoformat()] for name, days in splits.items()},
            "holdout_integrity": {
                "state": "LOCKED_NOT_EVALUATED", "boundary": LOCKED_HOLDOUT_START.isoformat(),
                "holdout_partition_files_opened": 0, "holdout_features_signals_or_pnl_read": False,
            },
            "splits": {},
        }

        all_variant_events: dict[str, list[dict[str, Any]]] = {variant.value: [] for variant in RetestVariant}
        all_variant_setups: dict[str, list[dict[str, Any]]] = {variant.value: [] for variant in RetestVariant}
        v1_all: list[dict[str, Any]] = []
        split_bars: dict[str, dict[str, list[Bar]]] = {}
        for split in ("development", "validation"):
            segment_bars = {symbol: [bar for bar in all_interval[symbol]
                                     if bar.time.date() in set(splits[split])]
                            for symbol in all_interval}
            warmup_bars = {symbol: [bar for bar in all_interval[symbol]
                                    if bar.time.date() < splits[split][0]]
                           for symbol in all_interval}
            split_bars[split] = {symbol: values for symbol, values in segment_bars.items() if values}
            if any(any(bar.time.date() >= LOCKED_HOLDOUT_START for bar in bars)
                   for bars in split_bars[split].values()):
                raise ValueError("Phase 4 interval bars cross the locked Holdout boundary")
            v1_rows = [row for row in phase3_rows if row["split"] == split]
            v1_events = _v1_events(
                v1_rows, split_bars[split], warmup_bars, interval=interval, split=split,
                market_by_symbol=market_by_symbol, incomplete_partitions=incomplete, config=config,
            )
            v1_all.extend(v1_events)
            variant_data: dict[str, Any] = {}
            for variant in RetestVariant:
                setup_rows, events = _scan_retest_setups(
                    split_bars[split], warmup_bars, variant=variant, interval=interval,
                    split=split, market_by_symbol=market_by_symbol,
                    incomplete_partitions=incomplete, config=config,
                )
                all_variant_events[variant.value].extend(events)
                all_variant_setups[variant.value].extend(setup_rows)
                event_by_setup = {event["setup_id"]: event for event in events}
                for setup_row in setup_rows:
                    setup_row["event_record"] = event_by_setup.get(setup_row["setup_id"])
                funnel = {
                    "breakout_detected": len(setup_rows),
                    "retest_occurred": sum(row.get("retest_attempt_time") is not None for row in setup_rows),
                    "retest_held_level": sum(row.get("retest_time") is not None for row in setup_rows),
                    "acceptance_confirmed": len(events),
                    "next_bar_executable": sum(event.get("execution_status") in {
                        "CLOSED", "OPEN_AT_SEGMENT_END",
                    } for event in events),
                    "one_share_affordable": sum(event.get("one_share_affordable_20k") is True for event in events),
                    "risk_eligible": sum(event.get("one_share_risk_eligible_250krw") is True for event in events),
                    "portfolio_fill": None,
                    "closed_trade": sum(event.get("execution_status") == "CLOSED" for event in events),
                }
                variant_data[variant.value] = {
                    "setup_funnel": funnel,
                    "setup_state_counts": dict(sorted({
                        state: sum(row.get("state") == state for row in setup_rows)
                        for state in {str(row.get("state")) for row in setup_rows}
                    }.items())),
                    "setup_records": setup_rows,
                    "event_metrics": {
                        "all": _event_metrics(events, len(splits[split]), None),
                        "clean_window_only": _event_metrics([e for e in events if not e["near_data_gap"]],
                                                             len(splits[split]), None),
                        "near_gap_events": sum(event["near_data_gap"] for event in events),
                    },
                    "removed_v1_signal_attribution": _removal_attribution(
                        v1_events, events, len(splits[split]),
                    ),
                }
                variant_data[variant.value]["portfolio_all"] = _portfolio_results(
                    split_bars[split], events,
                )
                variant_data[variant.value]["portfolio_clean_window_only"] = _portfolio_results(
                    split_bars[split], events, clean_only=True,
                )
                variant_data[variant.value]["setup_funnel"]["portfolio_fill"] = (
                    variant_data[variant.value]["portfolio_all"]["scenarios"]
                    ["order_cap_20000_risk_0.25pct"]["20000_krw_0.25pct_1.0x"]["fills"]
                )
                liquidity_edges = _dev_liquidity_edges(all_variant_events[variant.value])
                _annotate_buckets(all_variant_events[variant.value], liquidity_edges)
                variant_data[variant.value]["dev_liquidity_edges_krw"] = liquidity_edges
                variant_data[variant.value]["price_bucket_metrics"] = _bucket_metrics(
                    events, len(splits[split]), None, "entry_price_bucket",
                )
                variant_data[variant.value]["liquidity_bucket_metrics"] = _bucket_metrics(
                    events, len(splits[split]), None, "liquidity_bucket",
                )

            interval_report["splits"][split] = {
                "sessions": len(splits[split]),
                "v1_immediate_entry": {
                    "metrics": _event_metrics(v1_events, len(splits[split]), None),
                    "replay_parity_vs_phase3": {
                        "rows": len(v1_events),
                        "closed_source_rows": sum(row.get("source_phase3_execution_status") == "CLOSED" for row in v1_events),
                        "execution_status_mismatches": sum(
                            ("UNFILLED" if row["replay_execution_status"] not in {
                                "CLOSED", "OPEN_AT_SEGMENT_END",
                            } else row["replay_execution_status"]) != row["source_phase3_execution_status"]
                            for row in v1_events
                        ),
                        "exit_time_mismatches": sum(
                            row.get("replay_exit_time") is not None and row.get("exit_time") is not None and
                            row["replay_exit_time"] != row["exit_time"]
                            for row in v1_events
                        ),
                        "net_pnl_abs_difference_gt_0_02_krw": sum(
                            event.get("replay_net_pnl_krw") is not None and event.get("source_phase3_net_pnl_krw") is not None
                            and abs(float(event["replay_net_pnl_krw"]) - float(event["source_phase3_net_pnl_krw"])) > .02
                            for event in v1_events
                        ),
                    },
                },
                "variants": variant_data,
            }

        v1_dev_mfe = [float(event["signal_mfe_pct"]) for event in v1_all
                      if event["split"] == "development" and event.get("signal_mfe_pct") is not None]
        no_follow_threshold = _quantile(v1_dev_mfe, .25)
        for variant in RetestVariant:
            events = all_variant_events[variant.value]
            for split in ("development", "validation"):
                split_events = [event for event in events if event["split"] == split]
                v1_split_events = [event for event in v1_all if event["split"] == split]
                split_report = interval_report["splits"][split]["variants"][variant.value]
                split_report["event_metrics"]["all"] = _event_metrics(
                    split_events, len(splits[split]), no_follow_threshold,
                )
                split_report["event_metrics"]["clean_window_only"] = _event_metrics(
                    [event for event in split_events if not event["near_data_gap"]],
                    len(splits[split]), no_follow_threshold,
                )
                split_report["event_metrics"]["cost_stress"] = {
                    f"{multiplier:.1f}x": _stress_metrics(
                        split_events, len(splits[split]), no_follow_threshold, multiplier,
                    ) for multiplier in STRESS_MULTIPLIERS
                }
                v1_metrics = interval_report["splits"][split]["v1_immediate_entry"]["metrics"]
                v1_metrics["cost_stress"] = {
                    f"{multiplier:.1f}x": _stress_metrics(
                        v1_split_events, len(splits[split]), no_follow_threshold, multiplier,
                    ) for multiplier in STRESS_MULTIPLIERS
                }
                split_report["depth_metrics"] = _bucket_metrics(
                    split_events, len(splits[split]), no_follow_threshold, "retest_depth_bucket",
                ) if split_events and "retest_depth_bucket" in split_events[0] else {}
                for event in split_events:
                    penetration = float(event.get("level_penetration_pct") or 0)
                    event["retest_depth_bucket"] = (
                        "AT_OR_ABOVE_LEVEL" if penetration <= 0 else
                        "SHALLOW_0_0.1PCT" if penetration <= .1 else "SHALLOW_0.1_0.3PCT"
                    )
                    event["retest_timing_bucket"] = (
                        f"{event['bars_to_retest']}_BAR" if event.get("bars_to_retest") in {1, 2, 3}
                        else "OUTSIDE_WINDOW"
                    )
                split_report["depth_metrics"] = _bucket_metrics(
                    split_events, len(splits[split]), no_follow_threshold, "retest_depth_bucket",
                )
                split_report["time_to_retest_metrics"] = _bucket_metrics(
                    split_events, len(splits[split]), no_follow_threshold, "retest_timing_bucket",
                )
            comparison = {}
            for split in ("development", "validation"):
                v1_metrics = _event_metrics([event for event in v1_all if event["split"] == split],
                                            len(splits[split]), no_follow_threshold)
                retest_metrics = _event_metrics([event for event in events if event["split"] == split],
                                                len(splits[split]), no_follow_threshold)
                comparison[split] = {
                    "v1_immediate_rejection_pct": v1_metrics["immediate_rejection_pct_of_closed"],
                    "retest_immediate_rejection_pct": retest_metrics["immediate_rejection_pct_of_closed"],
                    "immediate_rejection_reduction_percentage_points": (
                        v1_metrics["immediate_rejection_pct_of_closed"] - retest_metrics["immediate_rejection_pct_of_closed"]
                        if v1_metrics["immediate_rejection_pct_of_closed"] is not None and
                        retest_metrics["immediate_rejection_pct_of_closed"] is not None else None
                    ),
                    "v1_no_follow_through_pct": v1_metrics["no_follow_through_pct_of_closed"],
                    "retest_no_follow_through_pct": retest_metrics["no_follow_through_pct_of_closed"],
                    "no_follow_through_reduction_percentage_points": (
                        v1_metrics["no_follow_through_pct_of_closed"] - retest_metrics["no_follow_through_pct_of_closed"]
                        if v1_metrics["no_follow_through_pct_of_closed"] is not None and
                        retest_metrics["no_follow_through_pct_of_closed"] is not None else None
                    ),
                }
            interval_report["no_follow_through_threshold_frozen_from_v1_dev_p25_pct"] = no_follow_threshold
            interval_report.setdefault("false_breakout_comparison_by_variant", {})[variant.value] = comparison
            event_target = output_dir / f"retest-events-{variant.value.lower().replace('-', '')}-{interval}.json"
            event_target.write_text(json.dumps({
                "schema_version": 1, "git_sha": git_sha, "interval": interval,
                "variant": variant.value, "selected_partition_sha256": selected_hash,
                "holdout_integrity": interval_report["holdout_integrity"],
                "setups": all_variant_setups[variant.value],
                "accepted_events": events,
            }, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
            interval_report.setdefault("event_file_sha256", {})[variant.value] = _sha256(event_target)

        # Keep the broad universe distinct from the 30-symbol historical strategy cohort.
        interval_report["market_split"] = {
            "KOSPI": "METRICS_ABOVE_USE_ORIGINAL_30_ONLY",
            "KOSDAQ": "NOT_AVAILABLE_NO_EXPANDED_COHORT_MINUTE_HISTORY",
        }
        interval_target = output_dir / f"retest-anatomy-{interval}.json"
        interval_report["report_path"] = str(interval_target)
        interval_target.write_text(json.dumps(interval_report, indent=2, ensure_ascii=False,
                                             allow_nan=False) + "\n", encoding="utf-8")
        outputs[interval] = interval_report
    return outputs


def run_development_neighborhood(
    *, cache_root: Path, phase3_manifest_path: Path, phase3_events_root: Path,
    output_dir: Path, git_sha: str, base_config: BreakoutRetestConfig = DEFAULT_RETEST_CONFIG,
) -> dict[str, Any]:
    """Check only the immediate tolerance/expiry neighborhood in used Development."""
    _, minute_by_session, partition_hashes, incomplete, splits = load_used_development_validation(
        cache_root, phase3_manifest_path,
    )
    days = set(splits["development"])
    v1_mfe: dict[str, list[float]] = {interval: [] for interval in INTERVAL_MINUTES}
    for interval in INTERVAL_MINUTES:
        path = phase3_events_root / f"breakout-signals-{interval}.parquet"
        for row in pq.read_table(path).to_pylist():
            if row.get("split") == "development" and row.get("signal_mfe_pct") is not None:
                v1_mfe[interval].append(float(row["signal_mfe_pct"]))
    market_by_symbol = {symbol: "KOSPI" for symbol in minute_by_session}
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "schema_version": 1,
        "artifact": "phase4-development-parameter-neighborhood",
        "git_sha": git_sha,
        "dataset_scope": "USED_DEVELOPMENT_ONLY",
        "selected_partition_sha256": hashlib.sha256(
            "\n".join(f"{key}:{value}" for key, value in sorted(partition_hashes.items())).encode()
        ).hexdigest(),
        "allowed_dates": [splits["development"][0].isoformat(), splits["development"][-1].isoformat()],
        "holdout_integrity": {"state": "LOCKED_NOT_EVALUATED", "holdout_partition_files_opened": 0},
        "neighbor_policy": "one parameter at a time; no selection or promotion from these diagnostic neighbors",
        "results": {},
    }
    for interval, minutes in INTERVAL_MINUTES.items():
        bars_by_symbol = {
            symbol: resample_session_minutes(
                [bar for day in sorted(daily) if day in days for bar in daily[day]], minutes,
            ) for symbol, daily in minute_by_session.items()
        }
        segment = {symbol: [bar for bar in bars if bar.time.date() in days]
                   for symbol, bars in bars_by_symbol.items()}
        warmup = {symbol: [] for symbol in segment}
        threshold = _quantile(v1_mfe[interval], .25)
        interval_result: dict[str, Any] = {}
        neighbors = (
            ("tolerance_0.20pct", {"retest_tolerance_pct": .002}),
            ("tolerance_0.40pct", {"retest_tolerance_pct": .004}),
            ("expiry_2_bars", {"max_retest_bars": 2}),
        )
        for variant in RetestVariant:
            arms: dict[str, Any] = {}
            for label, changes in (("registered", {}), *neighbors):
                values = {
                    "lookback": base_config.lookback,
                    "volume_lookback": base_config.volume_lookback,
                    "volume_multiple": base_config.volume_multiple,
                    "max_retest_bars": base_config.max_retest_bars,
                    "retest_tolerance_pct": base_config.retest_tolerance_pct,
                    "stop_buffer_pct": base_config.stop_buffer_pct,
                    "max_holding_bars": base_config.max_holding_bars,
                }
                values.update(changes)
                config = BreakoutRetestConfig(**values)
                _, events = _scan_retest_setups(
                    segment, warmup, variant=variant, interval=interval, split="development",
                    market_by_symbol=market_by_symbol, incomplete_partitions=incomplete, config=config,
                )
                arms[label] = {
                    "parameter_values": values,
                    "metrics": _event_metrics(events, len(splits["development"]), threshold),
                    "near_gap_events": sum(event["near_data_gap"] for event in events),
                }
            interval_result[variant.value] = arms
        result["results"][interval] = interval_result
    target = output_dir / "retest-neighborhood.json"
    result["report_path"] = str(target)
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return result


def write_100k_feasibility(
    *, anatomy_paths: dict[str, Path], preregistration_path: Path, output_path: Path,
) -> dict[str, Any]:
    """Extract fixed-capital execution diagnostics from the frozen research reports."""
    preregistration = json.loads(preregistration_path.read_text(encoding="utf-8"))
    if preregistration.get("status") != "FROZEN_BEFORE_NEW_EVIDENCE":
        raise ValueError("100K diagnostics require a frozen strategy registration")
    source_reports = {interval: json.loads(path.read_text(encoding="utf-8"))
                      for interval, path in sorted(anatomy_paths.items())}
    portfolio: dict[str, Any] = {}
    for interval, report in source_reports.items():
        portfolio[interval] = {}
        for split, split_report in report["splits"].items():
            portfolio[interval][split] = {
                variant: details["portfolio_all"]["scenarios"]
                for variant, details in split_report["variants"].items()
            }
    result = {
        "schema_version": 1,
        "artifact": "phase4-100k-feasibility",
        "status": "DIAGNOSTIC_ONLY",
        "git_sha": preregistration["freeze_commit_sha"],
        "preregistration_manifest_sha256": _sha256(preregistration_path),
        "strategy_config_sha256": preregistration["strategy_config_sha256"],
        "source_anatomy_sha256": {interval: _sha256(path) for interval, path in sorted(anatomy_paths.items())},
        "portfolio_assumptions": preregistration["portfolio_diagnostics"],
        "scope": "ORIGINAL_30_KOSPI_USED_DEVELOPMENT_AND_SECONDARY_VALIDATION_ONLY",
        "holdout_integrity": {"state": "LOCKED_NOT_EVALUATED", "partition_files_opened": 0},
        "scenarios_by_interval_split_variant": portfolio,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result["report_path"] = str(output_path)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False,
                                      allow_nan=False) + "\n", encoding="utf-8")
    result["report_sha256"] = _sha256(output_path)
    return result

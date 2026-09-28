from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from krx_trader.models import Bar
from krx_trader.research.anatomy import _load_frozen_partitions
from krx_trader.universe.models import StockMaster

KST = ZoneInfo("Asia/Seoul")
LOCKED_HOLDOUT_START = date(2026, 7, 28)
LAST_ALLOWED_USED_DATE = date(2026, 7, 27)
REGULAR_SESSION_OPEN = time(9, 0)
REGULAR_SESSION_MINUTES = 380  # 09:00 through 15:19, start-labelled; auction excluded.
PRICE_BUCKETS = ((1_000, 10_000, "1k_10k"), (10_000, 30_000, "10k_30k"),
                 (30_000, 50_000, "30k_50k"))
COHORT_SEED = "phase4-current-listing-cohort-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_phase3_splits(manifest_path: Path) -> tuple[dict[str, list[date]], dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("fresh_holdout_state") != "LOCKED_NOT_EVALUATED":
        raise ValueError("Phase 4 refuses to run unless the Fresh Holdout remains LOCKED_NOT_EVALUATED")
    splits = manifest.get("splits")
    if not isinstance(splits, dict):
        raise TypeError("frozen Phase 3 manifest has no split map")
    allowed: dict[str, list[date]] = {}
    for name in ("development", "validation"):
        details = splits.get(name)
        sessions = details.get("sessions") if isinstance(details, dict) else None
        if not isinstance(sessions, list) or not sessions:
            raise ValueError(f"frozen Phase 3 manifest is missing {name} sessions")
        days = [date.fromisoformat(str(value)) for value in sessions]
        if days != sorted(set(days)) or days[-1] >= LOCKED_HOLDOUT_START:
            raise ValueError(f"Phase 4 {name} dates are invalid or cross the locked boundary")
        allowed[name] = days
    if set(allowed["development"]) & set(allowed["validation"]):
        raise ValueError("Phase 4 Development and Validation dates overlap")
    return allowed, manifest


def load_used_development_validation(
    cache_root: Path,
    manifest_path: Path,
) -> tuple[dict[str, list[Bar]], dict[str, dict[date, list[Bar]]], dict[str, str], set[tuple[str, date]], dict]:
    """Load only frozen Development and Validation partitions; never enumerate the Holdout."""
    splits, _ = _safe_phase3_splits(manifest_path)
    # The existing loader checks every selected partition hash and constructs paths only
    # from Development/Validation dates. It does not glob or read Holdout Parquet files.
    bars, by_session, hashes, incomplete = _load_frozen_partitions(cache_root, manifest_path)
    allowed_dates = set(splits["development"] + splits["validation"])
    if any(day >= LOCKED_HOLDOUT_START for _, day in incomplete):
        raise ValueError("Phase 4 data loader returned a partition on or after the Holdout boundary")
    if len(hashes) != sum(len(values) for values in by_session.values()):
        raise ValueError("Phase 4 loader returned an unexpected selected-partition count")
    if any(day not in allowed_dates for daily in by_session.values() for day in daily):
        raise ValueError("Phase 4 loader returned a date outside frozen Development/Validation")
    return bars, by_session, hashes, incomplete, splits


def _missing_ranges(missing: list[datetime]) -> list[dict[str, str | int]]:
    if not missing:
        return []
    values = sorted(missing)
    ranges: list[dict[str, str | int]] = []
    start = previous = values[0]
    count = 1
    for current in values[1:]:
        if current - previous == timedelta(minutes=1):
            previous = current
            count += 1
            continue
        ranges.append({"start": start.strftime("%H:%M"), "end": previous.strftime("%H:%M"), "minutes": count})
        start = previous = current
        count = 1
    ranges.append({"start": start.strftime("%H:%M"), "end": previous.strftime("%H:%M"), "minutes": count})
    return ranges


def audit_minute_gaps(
    bars_by_session: dict[str, dict[date, list[Bar]]],
    partition_hashes: dict[str, str],
    splits: dict[str, list[date]],
    *,
    git_sha: str,
    expected_minutes_by_session: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Describe gaps only in Phase 3 Development/Validation, without imputing bars."""
    if any(day >= LOCKED_HOLDOUT_START for days in splits.values() for day in days):
        raise ValueError("Phase 4 DQ audit cannot include a locked Holdout session")
    allowed = set(splits["development"] + splits["validation"])
    selected_hashes = {
        key: value for key, value in partition_hashes.items()
        if date.fromisoformat(key.split(":", 1)[1]) in allowed
    }
    if len(selected_hashes) != len(partition_hashes):
        raise ValueError("partition hash set contains dates outside the permitted used windows")
    gaps: list[dict[str, Any]] = []
    expected_total = actual_total = 0
    expected_minutes_by_session = expected_minutes_by_session or {}
    for symbol, daily in sorted(bars_by_session.items()):
        for day, bars in sorted(daily.items()):
            if day not in allowed:
                raise ValueError("Phase 4 DQ audit encountered a date outside the allowed windows")
            session_start = datetime.combine(day, REGULAR_SESSION_OPEN, KST)
            expected = [session_start + timedelta(minutes=index) for index in range(REGULAR_SESSION_MINUTES)]
            observed = {
                bar.time.astimezone(KST).replace(second=0, microsecond=0)
                for bar in bars
                if bar.time.astimezone(KST).date() == day
                and session_start <= bar.time.astimezone(KST) < session_start + timedelta(minutes=REGULAR_SESSION_MINUTES)
            }
            expected_for_day = int(expected_minutes_by_session.get(day.isoformat(), REGULAR_SESSION_MINUTES))
            if not 0 <= expected_for_day <= REGULAR_SESSION_MINUTES:
                raise ValueError("expected minute count is outside the regular KRX session")
            absent_from_full_session = [slot for slot in expected if slot not in observed]
            missing_count = max(0, expected_for_day - len(observed))
            expected_total += expected_for_day
            actual_total += min(len(observed), expected_for_day)
            if missing_count:
                gaps.append({
                    "symbol": symbol,
                    "session": day.isoformat(),
                    "expected_slots": expected_for_day,
                    "observed_slots": min(len(observed), expected_for_day),
                    "missing_slots": missing_count,
                    "missing_ranges": _missing_ranges(absent_from_full_session) if expected_for_day == REGULAR_SESSION_MINUTES else [],
                    "session_adjusted_expected_count": expected_for_day != REGULAR_SESSION_MINUTES,
                    "classification": "UNKNOWN",
                    "classification_reason": "No per-minute trade/status evidence establishes no-trade, halt, or retrieval loss; session-adjusted slots have no exact schedule in the source manifest.",
                    "synthetic_bars_added": 0,
                })
    chosen_hash = hashlib.sha256(
        "\n".join(f"{key}:{value}" for key, value in sorted(selected_hashes.items())).encode()
    ).hexdigest()
    missing_count = expected_total - actual_total
    by_split = {}
    for name, dates in splits.items():
        days = set(dates)
        selected = [row for row in gaps if date.fromisoformat(row["session"]) in days]
        by_split[name] = {
            "sessions": len(dates),
            "partitions": sum(day in days for daily in bars_by_session.values() for day in daily),
            "partial_partitions": len(selected),
            "missing_minute_slots": sum(int(row["missing_slots"]) for row in selected),
        }
    return {
        "schema_version": 1,
        "artifact": "phase4-data-quality-audit",
        "git_sha": git_sha,
        "dataset_scope": "USED_DEVELOPMENT_AND_SECONDARY_VALIDATION_ONLY",
        "split_periods": {
            name: [dates[0].isoformat(), dates[-1].isoformat()] for name, dates in splits.items()
        },
        "holdout_integrity": {
            "state": "LOCKED_NOT_EVALUATED",
            "holdout_boundary": LOCKED_HOLDOUT_START.isoformat(),
            "holdout_partition_files_opened": 0,
            "holdout_features_signals_pnl_or_index_context_read": False,
        },
        "selected_partition_count": len(selected_hashes),
        "selected_partition_sha256": chosen_hash,
        "expected_slots_per_partition": REGULAR_SESSION_MINUTES,
        "expected_minute_slots": expected_total,
        "observed_minute_slots": actual_total,
        "missing_minute_slots": missing_count,
        "partial_partitions": len(gaps),
        "classifications": {
            "RETRIEVAL_GAP_CONFIRMED": 0,
            "NO_TRADE_MINUTE_CONFIRMED": 0,
            "PROVIDER_OMITTED_NO_TRADE_CONFIRMED": 0,
            "SESSION_SEMANTIC": 0,
            "HALT_OR_SPECIAL_STATUS_CONFIRMED": 0,
            "UNKNOWN": missing_count,
        },
        "split_summary": by_split,
        "partial_partitions_detail": gaps,
        "interpretation": "Missing minutes are unresolved, not automatically data corruption. Existing bounded KIS rereads found persistent gaps in two pre-Holdout examples, but neither distinguishes no-trade minutes from provider omission. No bars were synthesized.",
        "historical_rechecks": [
            {
                "symbol": "001440", "session": "2026-06-08", "source_artifact": "phase25-missing-minute-001440-20260608.json",
                "observed": "09:04-09:33 absent in both FID_PW_DATA_INCU_YN variants",
                "classification": "UNKNOWN", "synthetic_bars_added": 0,
            },
            {
                "symbol": "034220", "session": "2026-05-13", "source_artifact": "phase25-missing-minute-034220-20260513.json",
                "observed": "10:22 absent in bounded retries, parameter variants, and direct anchor queries",
                "classification": "UNKNOWN", "synthetic_bars_added": 0,
            },
        ],
        "fresh_recheck": {
            "status": "NOT_PERFORMED",
            "reason": "KIS app credentials were absent from the process environment; .env was not read.",
        },
    }


def _price_bucket(price: int | None) -> str:
    if price is None:
        return "UNKNOWN"
    for lower, upper, name in PRICE_BUCKETS:
        if lower <= price < upper or (name == PRICE_BUCKETS[-1][2] and price == upper):
            return name
    return "OUT_OF_RANGE"


def _eligible_stock(stock: StockMaster) -> bool:
    return (
        stock.market in {"KOSPI", "KOSDAQ"}
        and stock.instrument_type == "COMMON"
        and not stock.halted
        and not stock.management
        and stock.warning_status is None
        and stock.reference_price is not None
        and 1_000 <= stock.reference_price <= 50_000
    )


def _stable_rank(symbol: str) -> str:
    return hashlib.sha256(f"{COHORT_SEED}|{symbol}".encode()).hexdigest()


def _round_robin_by_price_and_cap(candidates: list[StockMaster], count: int) -> list[StockMaster]:
    if count <= 0:
        return []
    market_caps = sorted(stock.market_cap_raw for stock in candidates if stock.market_cap_raw is not None)
    if market_caps:
        low_edge = market_caps[len(market_caps) // 3]
        high_edge = market_caps[(2 * len(market_caps)) // 3]
    else:
        low_edge = high_edge = None

    groups: dict[tuple[str, str], list[StockMaster]] = {}
    for stock in candidates:
        cap = stock.market_cap_raw
        cap_bucket = "UNKNOWN" if cap is None else (
            "LOW_CAP_PROXY" if low_edge is not None and cap <= low_edge else
            "MID_CAP_PROXY" if high_edge is not None and cap <= high_edge else "HIGH_CAP_PROXY"
        )
        groups.setdefault((_price_bucket(stock.reference_price), cap_bucket), []).append(stock)
    for values in groups.values():
        values.sort(key=lambda stock: _stable_rank(stock.symbol))
    keys = sorted(groups)
    selected: list[StockMaster] = []
    while len(selected) < count:
        advanced = False
        for key in keys:
            values = groups[key]
            if values:
                selected.append(values.pop(0))
                advanced = True
                if len(selected) == count:
                    break
        if not advanced:
            break
    return selected


def _cohort_summary(stocks: list[StockMaster], *, target: int, base: set[str], order_cap_krw: int = 20_000,
                    fee_rate: float = 0.00015, slippage_bps: float = 15.0) -> dict[str, Any]:
    price_limit = order_cap_krw / ((1 + slippage_bps / 10_000) * (1 + fee_rate))
    return {
        "size": len(stocks),
        "target_size": target,
        "selection_shortfall": max(0, target - len(stocks)),
        "symbols": [stock.symbol for stock in stocks],
        "market_counts": dict(sorted(Counter(stock.market for stock in stocks).items())),
        "price_bucket_counts": dict(sorted(Counter(_price_bucket(stock.reference_price) for stock in stocks).items())),
        "market_cap_proxy_counts": {
            market: len([stock for stock in stocks if stock.market == market and stock.market_cap_raw is not None])
            for market in ("KOSPI", "KOSDAQ")
        },
        "one_share_affordable_at_20k_order_cap": sum(
            stock.reference_price is not None and stock.reference_price <= price_limit for stock in stocks
        ),
        "not_one_share_affordable_at_20k_order_cap": sum(
            stock.reference_price is None or stock.reference_price > price_limit for stock in stocks
        ),
        "baseline_symbols_retained": sum(stock.symbol in base for stock in stocks),
        "turnover_liquidity_measure": "NOT_AVAILABLE_FROM_CURRENT_STOCK_MASTER",
        "market_cap_is_liquidity_proxy": True,
    }


def select_expanded_cohorts(
    stocks: list[StockMaster],
    baseline_symbols: list[str],
    *,
    master_path: Path,
    master_metadata_path: Path | None = None,
    git_sha: str,
    order_cap_krw: int = 20_000,
    fee_rate: float = 0.00015,
    slippage_bps: float = 15.0,
) -> dict[str, Any]:
    """Freeze current-listing additions before any retest evaluation."""
    if len(set(baseline_symbols)) != len(baseline_symbols) or not baseline_symbols:
        raise ValueError("baseline cohort must contain unique symbols")
    base = set(baseline_symbols)
    by_symbol = {stock.symbol: stock for stock in stocks}
    if len(by_symbol) != len(stocks):
        raise ValueError("current KIS master contains duplicate symbols")
    eligible = [stock for stock in stocks if _eligible_stock(stock)]
    market_eligible = {market: [stock for stock in eligible if stock.market == market] for market in ("KOSPI", "KOSDAQ")}
    base_current = [by_symbol[symbol] for symbol in baseline_symbols if symbol in by_symbol]
    if len(base_current) != len(baseline_symbols):
        raise ValueError("at least one original Phase 2.5 symbol is missing from current KIS master")
    if any(stock.market != "KOSPI" for stock in base_current):
        raise ValueError("original Phase 2.5 baseline is not the frozen 30-symbol KOSPI cohort")

    selected60 = list(base_current)
    additions60 = _round_robin_by_price_and_cap(
        [stock for stock in market_eligible["KOSDAQ"] if stock.symbol not in base], 30
    )
    selected60.extend(additions60)

    selected100 = list(selected60)
    selected_ids = {stock.symbol for stock in selected100}
    for market, wanted in (("KOSPI", 20), ("KOSDAQ", 20)):
        candidates = [stock for stock in market_eligible[market] if stock.symbol not in selected_ids]
        additions = _round_robin_by_price_and_cap(candidates, wanted)
        selected100.extend(additions)
        selected_ids.update(stock.symbol for stock in additions)

    metadata: dict[str, Any] = {}
    if master_metadata_path and master_metadata_path.is_file():
        metadata = json.loads(master_metadata_path.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "artifact": "phase4-expanded-cohort-manifest",
        "git_sha": git_sha,
        "selection_frozen_before_strategy_evaluation": True,
        "selection_seed": COHORT_SEED,
        "source": metadata.get("source", "KIS stock master"),
        "source_requested_at": metadata.get("requested_at"),
        "source_master_sha256": _sha256(master_path),
        "source_master_metadata_sha256": _sha256(master_metadata_path) if master_metadata_path and master_metadata_path.is_file() else None,
        "source_markets": metadata.get("markets", ["KOSDAQ", "KOSPI"]),
        "survivorship_bias": "CURRENT-LISTING COHORT; historical point-in-time membership is not reconstructed.",
        "selection_rule": {
            "include_original_30": baseline_symbols,
            "instrument_filter": "current KOSPI/KOSDAQ COMMON; exclude current halt, management, warning, ETF/ETN, SPAC, preferred, missing/out-of-range reference price",
            "target60": "original 30 KOSPI plus up to 30 KOSDAQ; report a shortfall instead of silently changing market mix",
            "target100": "target60 plus up to 20 KOSPI and 20 KOSDAQ; report a shortfall instead of silently changing market mix",
            "price_buckets_krw": ["1k_10k", "10k_30k", "30k_50k"],
            "liquidity_selection": "Turnover was unavailable in the current stock master. Market-cap terciles are used only as a labeled proxy for deterministic selection; actual historical turnover buckets remain a separate diagnostic.",
            "within_stratum": "SHA-256 rank of fixed seed and symbol; no strategy result enters selection.",
            "affordability": "record one-share affordability under ₩20,000 cap with baseline fee and slippage assumptions; do not filter the cohort by affordability.",
        },
        "available_current_common_stocks": len(eligible),
        "cohort_60": _cohort_summary(selected60, target=60, base=base, order_cap_krw=order_cap_krw,
                                     fee_rate=fee_rate, slippage_bps=slippage_bps),
        "cohort_100": _cohort_summary(selected100, target=100, base=base, order_cap_krw=order_cap_krw,
                                       fee_rate=fee_rate, slippage_bps=slippage_bps),
        "breadth_data_status": "METADATA_ONLY_PENDING_HISTORICAL_MINUTE_ACQUISITION",
        "external_market_data_retrieval": "NOT_PERFORMED; KIS app credentials absent from process environment; .env was not read.",
    }


def run_data_quality_and_cohort_prep(
    *,
    cache_root: Path,
    phase3_manifest: Path,
    stock_master_path: Path,
    stock_master_metadata_path: Path,
    output_dir: Path,
    baseline_symbols: list[str],
    git_sha: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _, by_session, hashes, _, splits = load_used_development_validation(cache_root, phase3_manifest)
    manifest = json.loads(phase3_manifest.read_text(encoding="utf-8"))
    used_dates = {day for values in splits.values() for day in values}
    expected_map = manifest.get("quality", {}).get(
        "expected_minutes_per_session_adjusted_for_documented_kospi_halts", {}
    )
    safe_expected_map = {
        day: int(minutes) for day, minutes in expected_map.items()
        if date.fromisoformat(day) in used_dates
    }
    data_quality = audit_minute_gaps(
        by_session, hashes, splits, git_sha=git_sha,
        expected_minutes_by_session=safe_expected_map,
    )
    table = pq.read_table(stock_master_path)
    stocks = [StockMaster(**row) for row in table.to_pylist()]
    cohort = select_expanded_cohorts(
        stocks, baseline_symbols, master_path=stock_master_path,
        master_metadata_path=stock_master_metadata_path, git_sha=git_sha,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "data-quality-audit.json").write_text(
        json.dumps(data_quality, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    (output_dir / "cohort-manifest.json").write_text(
        json.dumps(cohort, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    return data_quality, cohort

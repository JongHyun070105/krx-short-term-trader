from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.quality import require_healthy_bars
from krx_trader.kis.auth import KisAuthError, TokenManager
from krx_trader.kis.rest import KisApiError, KisRestClient
from krx_trader.kis.transport import UrllibTransport
from krx_trader.models import Bar

KST = ZoneInfo("Asia/Seoul")
POOL_START = date(2023, 1, 2)
POOL_END = date(2025, 12, 30)
WARMUP_START = date(2022, 11, 1)
DEVELOPMENT = (date(2023, 1, 2), date(2024, 6, 28))
VALIDATION = (date(2024, 7, 1), date(2025, 6, 30))
CONFIRMATION = (date(2025, 7, 1), date(2025, 12, 30))
OUTPUT_ROOT = Path("runtime/research/phase11")
COHORT_PATH = Path("runtime/research/phase4/cohort-manifest.json")
DAILY_INTERVAL = "1d-adjusted"
INDEX_INTERVAL = "1d-kis-index"
DAILY_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
INDEX_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
ADJUSTMENT = "FID_ORG_ADJ_PRC=0 (KIS adjusted daily OHLCV)"
BASE_COST_PCT = 0.53
COST_MULTIPLIERS = (1.0, 1.5, 2.0)
OUTCOME_HORIZONS = (2, 3, 5, 10)
PRIMARY_HORIZONS = (3, 5, 10)
BOOTSTRAP_ITERATIONS = 400
MIN_RAW_EVENTS = 100
MIN_NON_OVERLAPPING = 50
MIN_UNIQUE_SESSIONS = 30
PROMOTION_GROSS_MIN_PCT = 1.0

SOURCE_AUDIT: dict[str, Any] = {
    "as_of": "2026-09-30",
    "adjusted_daily_price": {
        "status": "REPRODUCIBLE_HISTORICAL_SOURCE",
        "endpoint": DAILY_ENDPOINT,
        "adapter": "KisRestClient.get_daily_bars(adjusted=True)",
        "tr_id": "FHKST03010100",
        "date_parameters": ["FID_INPUT_DATE_1", "FID_INPUT_DATE_2"],
        "adjustment_convention": ADJUSTMENT,
        "timestamp_semantics": "KIS trading-session date represented at 00:00 Asia/Seoul",
        "point_in_time_safety": "Session-final values are used only after the signal close for next-session entry; historical adjusted series has no vintage selector and is not a strict as-was snapshot.",
        "revision_risk": "KIS does not expose a point-in-time vintage selector; later corporate-action adjustments can revise historical rows.",
        "reproducible": True,
        "observed_probe": {
            "symbol": "005930",
            "requested_start": POOL_START.isoformat(),
            "requested_end": POOL_END.isoformat(),
            "rows": 731,
            "returned_start": POOL_START.isoformat(),
            "returned_end": POOL_END.isoformat(),
        },
        "official_reference": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_examples.py",
    },
    "daily_traded_value": {
        "status": "AVAILABLE_WITH_PRICE_ENDPOINT",
        "endpoint": DAILY_ENDPOINT,
        "field": "acml_tr_pbmn",
        "semantics": "daily accumulated trading value in KRW; missing source field remains null",
        "point_in_time_safety": "Same completed-session aggregate; no intraday publication time or historical correction vintage is exposed.",
        "revision_risk": "KIS does not document point-in-time revision history in the inspected adapter/sample.",
        "reproducible": True,
        "official_reference": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py",
    },
    "stock_investor_flow_latest": {
        "status": "NOT_SUITABLE_FOR_MULTIYEAR_BACKFILL",
        "endpoint": "/uapi/domestic-stock/v1/quotations/inquire-investor",
        "adapter": "not implemented in this repository",
        "tr_id": "FHKST01010900",
        "date_parameters": [],
        "history": "Official legacy helper returns 30 rows; current sample accepts only market and symbol, with no historical date selector.",
        "timestamp_semantics": "session date; source does not expose an intraday availability timestamp",
        "point_in_time_safety": "NOT_ESTABLISHED; no publication timestamp or historical vintage selector is exposed.",
        "revision_risk": "correction/vintage policy and exact post-close publication time are undocumented in inspected sources",
        "rate_limit": "not pinned per endpoint; local REST transport retries KIS EGW00201 with a 61-second defer and shared adaptive cooldown",
        "reproducible": "only the latest rolling sample; not a reproducible 2023-2025 panel",
        "official_reference": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_investor/inquire_investor.py",
        "official_legacy_reference": "https://github.com/koreainvestment/open-trading-api/blob/main/legacy/rest/kis_api.py",
    },
    "stock_investor_flow_by_date": {
        "status": "DATE_QUERY_EXISTS_BULK_PANEL_NOT_PRACTICAL",
        "endpoint": "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
        "adapter": "not implemented in this repository",
        "tr_id": "FHPTJ04160001",
        "date_parameters": ["FID_INPUT_DATE_1"],
        "history": "one input session date per symbol request; continuation pages are for that response, not a multi-year date range",
        "timestamp_semantics": "daily session date; next-session use is conceptually possible after final data, but exact publication timestamp is not documented",
        "point_in_time_safety": "DATE_ALIGNED_ONLY; exact release time and point-in-time revision history are not established.",
        "revision_risk": "latest response has no documented historical vintage or correction timestamp",
        "rate_limit": "not pinned per endpoint; local shared cooldown handles EGW00201",
        "reproducible": "technically queryable by repeated date calls, but 100 symbols x about 731 sessions is about 73,100 requests before retries; excluded from this Phase",
        "official_reference": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/investor_trade_by_stock_daily/investor_trade_by_stock_daily.py",
    },
    "market_investor_flow_by_date": {
        "status": "DAILY_MARKET_QUERY_EXISTS_NOT_ACQUIRED",
        "endpoint": "/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market",
        "adapter": "not implemented in this repository",
        "tr_id": "FHPTJ04040000",
        "date_parameters": ["FID_INPUT_DATE_1", "FID_INPUT_DATE_2"],
        "history": "official example supplies the same date in both fields; per-session calls would be needed to build a historical series",
        "timestamp_semantics": "market-level session date; exact publication timestamp and correction policy are undocumented",
        "point_in_time_safety": "DATE_ALIGNED_ONLY; exact release time and point-in-time revision history are not established.",
        "revision_risk": "no point-in-time vintage selector documented",
        "rate_limit": "not pinned per endpoint; local shared cooldown handles EGW00201",
        "reproducible": "possible by dated requests, but not used as a substitute for per-symbol investor flow",
        "official_reference": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_investor_daily_by_market/inquire_investor_daily_by_market.py",
    },
    "market_indices": {
        "status": "REPRODUCIBLE_HISTORICAL_SOURCE",
        "endpoint": INDEX_ENDPOINT,
        "adapter": "KisRestClient.get_index_bars(index_code, start, end)",
        "tr_id": "FHKUP03500100",
        "range_limit": "adapter enforces at most 365 calendar days per call and pages in 35-day chunks",
        "timestamp_semantics": "KIS trading-session date at 00:00 Asia/Seoul",
        "point_in_time_safety": "Session-final index close is used as a next-session context; no historical vintage selector is documented.",
        "revision_risk": "KIS does not expose a point-in-time vintage selector",
        "reproducible": True,
        "official_reference": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_examples.py",
    },
    "market_cap_valuation_fundamentals": {
        "status": "EXCLUDED_POINT_IN_TIME_UNVERIFIED",
        "adapter": "current master/current quote only; no audited historical as-of market-cap panel in this repository",
        "fields_excluded": ["historical market capitalization", "EPS", "PER", "PBR", "financial statements"],
        "reason": "no publication-time or historical as-of semantics were established; current values would leak future information",
        "point_in_time_safety": "UNSAFE_FOR_HISTORICAL_PREDICTION; excluded",
        "reproducible": False,
    },
    "program_trading_activity": {
        "status": "NOT_SUITABLE_FOR_MULTIYEAR_PANEL",
        "endpoint": "/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily",
        "adapter": "not implemented in this repository",
        "tr_id": "not pinned in inspected sample metadata",
        "date_parameters": [],
        "history": "official sample accepts market and symbol but no explicit historical date range",
        "timestamp_semantics": "daily label/availability not fully established for historical reconstruction",
        "revision_risk": "no point-in-time vintage selector documented",
        "point_in_time_safety": "NOT_ESTABLISHED",
        "rate_limit": "not pinned per endpoint; local shared cooldown handles EGW00201",
        "reproducible": "latest response only from the inspected example; excluded",
        "official_reference": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/program_trade_by_stock_daily/program_trade_by_stock_daily.py",
    },
    "sector_context": {
        "status": "NOT_ACQUIRED",
        "adapter": "current listing metadata only; no historical point-in-time sector membership adapter found",
        "reason": "historical sector classification and revision semantics were not established",
        "point_in_time_safety": "NOT_ESTABLISHED; excluded",
        "reproducible": False,
    },
    "rate_limit_policy": {
        "adapter": "KisRestClient shared persistent limiter",
        "retry_behavior": "EGW00201 waits at least 61 seconds, increases the persistent minimum interval up to 5 seconds, and all requests remain serialized",
        "phase_history": "Phase 6 recorded a 4-second shared interval after KIS rate limiting",
        "official_per_endpoint_quota": "not verified from the inspected public sample code",
    },
    "flow_decision": "No multi-year per-symbol investor flow series is acquired or imputed. Missing flow remains null; no zero fill or synthetic flow factor is used.",
}

FACTOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "price": (
        "return_1d_pct", "return_3d_pct", "return_5d_pct", "return_10d_pct",
        "return_20d_pct", "volatility_5d_pct", "volatility_20d_pct",
        "range_5d_pct", "range_20d_pct", "distance_20d_high_pct",
        "distance_20d_low_pct", "overnight_gap_pct", "intraday_return_pct",
    ),
    "liquidity": (
        "volume_ratio_5d", "volume_ratio_20d", "turnover_ratio_5d",
        "turnover_ratio_20d",
    ),
    "market_relative": (
        "symbol_minus_market_1d_pct", "symbol_minus_market_5d_pct",
        "symbol_minus_market_20d_pct", "market_return_1d_pct",
        "market_return_5d_pct", "market_return_20d_pct",
        "market_volatility_20d_pct", "market_breadth_advancing_pct",
    ),
}

STATE_RULES: dict[str, tuple[tuple[float, float, str], ...]] = {
    "return_1d_pct": ((-math.inf, -1.0, "DOWN"), (-1.0, 1.0, "FLAT"), (1.0, math.inf, "UP")),
    "return_3d_pct": ((-math.inf, -2.0, "DOWN"), (-2.0, 2.0, "FLAT"), (2.0, math.inf, "UP")),
    "return_5d_pct": ((-math.inf, -2.0, "DOWN"), (-2.0, 2.0, "FLAT"), (2.0, math.inf, "UP")),
    "return_10d_pct": ((-math.inf, -2.0, "DOWN"), (-2.0, 2.0, "FLAT"), (2.0, math.inf, "UP")),
    "return_20d_pct": ((-math.inf, -4.0, "DOWN"), (-4.0, 4.0, "FLAT"), (4.0, math.inf, "UP")),
    "overnight_gap_pct": ((-math.inf, -1.0, "GAP_DOWN"), (-1.0, 1.0, "GAP_FLAT"), (1.0, math.inf, "GAP_UP")),
    "intraday_return_pct": ((-math.inf, -1.0, "DOWN"), (-1.0, 1.0, "FLAT"), (1.0, math.inf, "UP")),
    "volatility_5d_pct": ((-math.inf, 1.0, "LOW"), (1.0, 3.0, "MID"), (3.0, math.inf, "HIGH")),
    "volatility_20d_pct": ((-math.inf, 1.0, "LOW"), (1.0, 3.0, "MID"), (3.0, math.inf, "HIGH")),
    "range_5d_pct": ((-math.inf, 4.0, "TIGHT"), (4.0, 10.0, "MID"), (10.0, math.inf, "WIDE")),
    "range_20d_pct": ((-math.inf, 8.0, "TIGHT"), (8.0, 20.0, "MID"), (20.0, math.inf, "WIDE")),
    "distance_20d_high_pct": ((-math.inf, -10.0, "FAR_FROM_HIGH"), (-10.0, -2.0, "MID"), (-2.0, math.inf, "NEAR_HIGH")),
    "distance_20d_low_pct": ((-math.inf, 2.0, "NEAR_LOW"), (2.0, 10.0, "MID"), (10.0, math.inf, "FAR_FROM_LOW")),
    "volume_ratio_5d": ((-math.inf, 0.75, "LOW"), (0.75, 1.5, "NORMAL"), (1.5, math.inf, "EXPANSION")),
    "volume_ratio_20d": ((-math.inf, 0.75, "LOW"), (0.75, 1.5, "NORMAL"), (1.5, math.inf, "EXPANSION")),
    "turnover_ratio_5d": ((-math.inf, 0.75, "LOW"), (0.75, 1.5, "NORMAL"), (1.5, math.inf, "EXPANSION")),
    "turnover_ratio_20d": ((-math.inf, 0.75, "LOW"), (0.75, 1.5, "NORMAL"), (1.5, math.inf, "EXPANSION")),
    "symbol_minus_market_1d_pct": ((-math.inf, -1.0, "RELATIVE_WEAK"), (-1.0, 1.0, "RELATIVE_NEUTRAL"), (1.0, math.inf, "RELATIVE_STRONG")),
    "symbol_minus_market_5d_pct": ((-math.inf, -2.0, "RELATIVE_WEAK"), (-2.0, 2.0, "RELATIVE_NEUTRAL"), (2.0, math.inf, "RELATIVE_STRONG")),
    "symbol_minus_market_20d_pct": ((-math.inf, -4.0, "RELATIVE_WEAK"), (-4.0, 4.0, "RELATIVE_NEUTRAL"), (4.0, math.inf, "RELATIVE_STRONG")),
    "market_return_1d_pct": ((-math.inf, -1.0, "DOWN"), (-1.0, 1.0, "FLAT"), (1.0, math.inf, "UP")),
    "market_return_5d_pct": ((-math.inf, -2.0, "DOWN"), (-2.0, 2.0, "FLAT"), (2.0, math.inf, "UP")),
    "market_return_20d_pct": ((-math.inf, -4.0, "DOWN"), (-4.0, 4.0, "FLAT"), (4.0, math.inf, "UP")),
    "market_volatility_20d_pct": ((-math.inf, 1.0, "LOW"), (1.0, 2.5, "MID"), (2.5, math.inf, "HIGH")),
    "market_breadth_advancing_pct": ((-math.inf, 35.0, "NARROW"), (35.0, 65.0, "MIXED"), (65.0, math.inf, "BROAD")),
}

HYPOTHESES: dict[str, Any] = {
    "artifact": "phase11-preregistered-factor-map",
    "pool": {"start": POOL_START.isoformat(), "end": POOL_END.isoformat()},
    "warmup_only": {"start": WARMUP_START.isoformat(), "end": (POOL_START - timedelta(days=1)).isoformat()},
    "split": {
        "development": [item.isoformat() for item in DEVELOPMENT],
        "validation": [item.isoformat() for item in VALIDATION],
        "confirmation": [item.isoformat() for item in CONFIRMATION],
    },
    "outcomes": {
        "horizons_sessions": list(OUTCOME_HORIZONS),
        "primary_horizons_sessions": list(PRIMARY_HORIZONS),
        "entry": "next trading session open after completed signal day",
        "exit": "close of the horizon-th session starting with the entry session",
        "secondary_2d": "context only; never used to select a candidate",
        "cost_pct_round_trip": BASE_COST_PCT,
    },
    "states": {
        factor: [
            {"lower_inclusive": None if math.isinf(low) else low,
             "upper_exclusive": None if math.isinf(high) else high, "label": label}
            for low, high, label in ranges
        ]
        for factor, ranges in STATE_RULES.items()
    },
    "candidate_eligible_rules": [
        {
            "family": "MARKET_RELATIVE_CONTINUATION",
            "feature": "symbol_minus_market_5d_pct",
            "state": "RELATIVE_STRONG",
            "threshold": ">= 2.0 percentage points",
            "horizon_sessions": 5,
            "economic_explanation": "fixed-cohort stock 5-session return exceeds its matched KOSPI/KOSDAQ index by at least 2 percentage points",
        },
        {
            "family": "LIQUIDITY_EXPANSION",
            "feature": "turnover_ratio_20d",
            "state": "EXPANSION",
            "threshold": "current KIS daily traded value / prior 20-session median >= 1.5",
            "horizon_sessions": 5,
            "economic_explanation": "completed-day traded value expands versus its own prior 20-session baseline",
        },
    ],
    "candidate_gate": {
        "minimum_raw_events": MIN_RAW_EVENTS,
        "minimum_non_overlapping_per_symbol": MIN_NON_OVERLAPPING,
        "minimum_unique_signal_dates": MIN_UNIQUE_SESSIONS,
        "gross_mean_strictly_greater_than_pct": PROMOTION_GROSS_MIN_PCT,
        "net_mean_positive_at_cost_multipliers": [1.0, 1.5],
        "payoff_ratio_strictly_greater_than": 1.0,
        "positive_quarters_minimum": 2,
        "positive_half_years_minimum": 2,
        "max_symbol_or_signal_date_share": 0.20,
        "session_cluster_bootstrap": "400 deterministic session-cluster resamples; 10th percentile must be above -1x assumed cost",
        "market_split": "when each market has at least 20 non-overlapping events, neither market mean may be negative",
        "candidate_horizon": "5 sessions only; 2d is context and 3d/10d do not select candidate",
        "maximum_candidate_families": 1,
    },
    "not_used": ["accounting fundamentals", "EPS", "PER", "PBR", "ML", "weighted scores", "parameter search", "2026 external", "2026 holdout"],
}

MAP_DERIVED_CANDIDATE_RULE: dict[str, Any] = {
    "family": "MARKET_CONTEXT_MEAN_REVERSION",
    "feature": "market_return_20d_pct",
    "state": "DOWN",
    "threshold": "matched KOSPI or KOSDAQ index 20-session return <= -4.0%",
    "horizon_sessions": 3,
    "origin": "Development coarse factor map after factor maps were complete",
    "horizon_selection": (
        "3 sessions is the shortest primary horizon; it retains at least 30 distinct signal sessions "
        "after per-symbol non-overlap filtering (3d=35, 5d=28, 10d=20 in Development). "
        "The horizon was not selected by maximizing gross return."
    ),
    "initial_candidate_eligibility_note": (
        "This market-context family was not one of the two initial 5-session candidate rules. "
        "It is a transparent Development-map extension using a state boundary frozen in phase11-hypotheses.json; "
        "it remains exploratory and must pass the untouched one-shot Validation."
    ),
    "entry": "next trading session open after completed signal day",
    "exit": "close of the third session starting with the entry session",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
        temp_path = Path(stream.name)
        json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)


def _atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fieldnames = sorted({key for row in rows for key in row})
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
        temp_path = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)


def snapshot_previous_phase_manifests(repository_root: Path = Path(".")) -> dict[str, str]:
    output: dict[str, str] = {}
    names = ("*manifest*.json", "*artifact-index*.json", "*integrity*.json")
    for phase in range(5, 11):
        root = repository_root / "runtime" / "research" / f"phase{phase}"
        if not root.is_dir():
            continue
        for name in names:
            for path in sorted(root.rglob(name)):
                if path.is_file():
                    key = path.relative_to(repository_root).as_posix()
                    output[key] = _sha256(path)
    return output


def compare_hash_snapshots(before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    changed = sorted(path for path, digest in before.items() if after.get(path) != digest)
    missing = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    return {
        "status": "PASS" if not changed and not missing and not added else "FAIL",
        "baseline_count": len(before), "after_count": len(after),
        "changed": changed, "missing": missing, "added": added,
    }


def assert_phase11_pool_date(session: date) -> None:
    if not WARMUP_START <= session <= POOL_END:
        raise ValueError("Phase 11 data access is limited to warmup and the 2023-2025 pool")


def normalize_flow(
    net_buy_krw: float | None,
    traded_value_krw: float | None,
    *,
    flow_session: date | None,
    signal_session: date,
) -> float | None:
    """Normalize flow to same-day traded value; missing is null and future flow is rejected."""
    if flow_session is None:
        return None
    if flow_session > signal_session:
        raise ValueError("flow session is later than the completed signal session")
    if net_buy_krw is None or traded_value_krw is None or traded_value_krw <= 0:
        return None
    return net_buy_krw / traded_value_krw * 100.0


def _load_frozen_cohort(path: Path) -> tuple[str, list[str], dict[str, str], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    selected: tuple[str, dict[str, Any]] | None = None
    for name in ("cohort_100", "cohort_60"):
        candidate = payload.get(name)
        if isinstance(candidate, dict) and len(candidate.get("symbols", [])) >= 60:
            selected = name, candidate
            break
    if selected is None:
        raise ValueError("no reproducibly frozen Phase 4 cohort of at least 60 symbols exists")
    name, cohort = selected
    symbols = cohort.get("symbols")
    if not isinstance(symbols, list) or len(symbols) != cohort.get("size") or len(set(symbols)) != len(symbols):
        raise ValueError("frozen cohort symbol list is invalid")
    counts = cohort.get("market_counts", {})
    kospi_count = int(counts.get("KOSPI", 0))
    kosdaq_count = int(counts.get("KOSDAQ", 0))
    if name == "cohort_100":
        first_kospi = min(30, kospi_count)
        first_kosdaq = min(30, kosdaq_count)
        extra_kospi = kospi_count - first_kospi
        markets = (
            ["KOSPI"] * first_kospi + ["KOSDAQ"] * first_kosdaq
            + ["KOSPI"] * extra_kospi + ["KOSDAQ"] * (kosdaq_count - first_kosdaq)
        )
    else:
        markets = ["KOSPI"] * kospi_count + ["KOSDAQ"] * kosdaq_count
    if len(markets) != len(symbols) or len(markets) < 60:
        raise ValueError("frozen cohort ordering does not match its market counts")
    market_by_symbol = dict(zip(symbols, markets, strict=True))
    actual = {market: markets.count(market) for market in ("KOSPI", "KOSDAQ")}
    if actual != {"KOSPI": kospi_count, "KOSDAQ": kosdaq_count}:
        raise ValueError("frozen cohort market ordering failed its manifest counts")
    digest = hashlib.sha256(json.dumps(symbols, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return name, symbols, market_by_symbol, digest


def _write_adjusted_cache_metadata(
    cache: ParquetBarCache,
    *, kind: str,
    symbol: str,
    interval: str,
    market: str,
    start: date,
    end: date,
    adjustment: str,
) -> dict[str, Any]:
    path = cache.partition_path(kind, symbol, interval)
    metadata_path = path.with_suffix(".metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update({
        "requested_start": start.isoformat(), "requested_end": end.isoformat(),
        "adjustment_convention": adjustment, "provider": "KIS Open API",
        "timestamp_semantics": "KRX trading-session date at 00:00 Asia/Seoul",
        "hash_algorithm": "SHA-256", "market": market,
    })
    _atomic_json(metadata_path, metadata)
    return metadata


def _load_verified_cache(
    cache: ParquetBarCache,
    *, kind: str,
    symbol: str,
    interval: str,
    start: date,
    end: date,
    adjustment: str,
    expected_market: str | None = None,
) -> tuple[list[Bar], dict[str, Any]] | None:
    path = cache.partition_path(kind, symbol, interval)
    metadata_path = path.with_suffix(".metadata.json")
    if not path.is_file() or not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("requested_start") != start.isoformat()
            or metadata.get("requested_end") != end.isoformat()
            or metadata.get("adjustment_convention") != adjustment
            or metadata.get("provider") != "KIS Open API"
            or metadata.get("symbol") != symbol
            or metadata.get("interval") != interval
            or (expected_market is not None and metadata.get("market") != expected_market)
            or metadata.get("sha256") != _sha256(path)
        ):
            return None
        bars = cache.load(kind, symbol, interval)
        if any(not start <= bar.time.astimezone(KST).date() <= end for bar in bars):
            return None
        return bars, metadata
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def acquire_phase11_data(
    client: Any,
    *,
    output_root: Path = OUTPUT_ROOT,
    cohort_path: Path = COHORT_PATH,
) -> dict[str, Any]:
    """Resumable single-writer acquisition of adjusted daily stocks and daily indices."""
    cohort_name, symbols, market_by_symbol, cohort_sha = _load_frozen_cohort(cohort_path)
    cache = ParquetBarCache(output_root / "daily_cache")
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_path = output_root / "phase11-acquisition-manifest.json"
    prior = None
    if manifest_path.is_file():
        try:
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = None
    attempt_history = list(prior.get("attempt_history", [])) if isinstance(prior, dict) else []
    if isinstance(prior, dict) and prior.get("finished_at"):
        previous_attempt = {
            "finished_at": prior["finished_at"],
            "status": prior.get("status"),
            "errors": list(prior.get("errors", [])),
        }
        if not attempt_history or attempt_history[-1] != previous_attempt:
            attempt_history.append(previous_attempt)
    manifest: dict[str, Any] = {
        "artifact": "phase11-resumable-acquisition",
        "status": "RUNNING",
        "provider": "KIS Open API",
        "cohort": cohort_name,
        "cohort_size": len(symbols),
        "cohort_order_sha256": cohort_sha,
        "requested_start": WARMUP_START.isoformat(),
        "primary_pool_start": POOL_START.isoformat(),
        "requested_end": POOL_END.isoformat(),
        "price_adjustment": ADJUSTMENT,
        "single_writer": True,
        "atomic_partition_and_sidecar": True,
        "resumable": True,
        "symbols": {},
        "indexes": {},
        "errors": [],
        "attempt_history": attempt_history,
        "external_2026_read": False,
        "holdout_2026_read": False,
    }
    if isinstance(prior, dict) and prior.get("cohort_order_sha256") == cohort_sha:
        manifest["symbols"] = dict(prior.get("symbols", {}))
        manifest["indexes"] = dict(prior.get("indexes", {}))
    _atomic_json(manifest_path, manifest)

    for symbol in symbols:
        cached = _load_verified_cache(
            cache, kind="daily", symbol=symbol, interval=DAILY_INTERVAL,
            start=WARMUP_START, end=POOL_END, adjustment=ADJUSTMENT,
            expected_market=market_by_symbol[symbol],
        )
        if cached is not None:
            bars, metadata = cached
            manifest["symbols"][symbol] = {
                "status": "COMPLETE_CACHED", "rows": len(bars),
                "first_date": bars[0].time.astimezone(KST).date().isoformat() if bars else None,
                "last_date": bars[-1].time.astimezone(KST).date().isoformat() if bars else None,
                "provider": "KIS Open API", "adjustment_convention": ADJUSTMENT,
                "sha256": metadata["sha256"],
            }
            _atomic_json(manifest_path, manifest)
            continue
        manifest["symbols"][symbol] = {"status": "ACQUIRING"}
        _atomic_json(manifest_path, manifest)
        try:
            bars = client.get_daily_bars(symbol, WARMUP_START, POOL_END, adjusted=True)
            if not bars:
                raise KisApiError("KIS returned no daily rows")
            if len({bar.time.astimezone(KST).date() for bar in bars}) != len(bars):
                raise KisApiError("KIS returned duplicate daily dates")
            if any(not WARMUP_START <= bar.time.astimezone(KST).date() <= POOL_END for bar in bars):
                raise KisApiError("KIS returned daily rows outside the requested window")
            require_healthy_bars(bars)
            path, metadata = cache.save(
                bars, kind="daily", symbol=symbol, interval=DAILY_INTERVAL,
                market=market_by_symbol[symbol],
                source=f"KIS {DAILY_ENDPOINT}; FID_ORG_ADJ_PRC=0",
                requested_at=datetime.now(KST), requested_start=WARMUP_START,
                requested_end=POOL_END,
            )
            metadata = _write_adjusted_cache_metadata(
                cache, kind="daily", symbol=symbol, interval=DAILY_INTERVAL,
                market=market_by_symbol[symbol], start=WARMUP_START, end=POOL_END,
                adjustment=ADJUSTMENT,
            )
            manifest["symbols"][symbol] = {
                "status": "COMPLETE", "rows": len(bars),
                "first_date": bars[0].time.astimezone(KST).date().isoformat(),
                "last_date": bars[-1].time.astimezone(KST).date().isoformat(),
                "provider": "KIS Open API", "adjustment_convention": ADJUSTMENT,
                "sha256": metadata["sha256"], "cache_path": path.relative_to(output_root).as_posix(),
            }
        except (KisApiError, KisAuthError, OSError, RuntimeError, ValueError) as exc:
            failure = {"symbol": symbol, "error_type": type(exc).__name__}
            if isinstance(exc, KisApiError) and str(exc) == "KIS returned no daily rows":
                failure["reason"] = "KIS returned no daily rows"
            manifest["symbols"][symbol] = {"status": "FAILED", **failure}
            manifest["errors"].append(failure)
            _atomic_json(manifest_path, manifest)
            if isinstance(exc, KisAuthError):
                break
        _atomic_json(manifest_path, manifest)

    for name, code in (("KOSPI", "0001"), ("KOSDAQ", "1001")):
        cached = _load_verified_cache(
            cache, kind="indexes", symbol=name, interval=INDEX_INTERVAL,
            start=WARMUP_START, end=POOL_END, adjustment="N/A_INDEX_NOT_ADJUSTED",
            expected_market=name,
        )
        if cached is not None:
            bars, metadata = cached
            manifest["indexes"][name] = {
                "status": "COMPLETE_CACHED", "index_code": code, "rows": len(bars),
                "first_date": bars[0].time.astimezone(KST).date().isoformat() if bars else None,
                "last_date": bars[-1].time.astimezone(KST).date().isoformat() if bars else None,
                "provider": "KIS Open API", "sha256": metadata["sha256"],
            }
            _atomic_json(manifest_path, manifest)
            continue
        try:
            bars: list[Bar] = []
            year_start = WARMUP_START
            for year_end in (date(2022, 12, 31), date(2023, 12, 31), date(2024, 12, 31), POOL_END):
                chunk_start = max(year_start, WARMUP_START)
                if chunk_start <= year_end:
                    bars.extend(client.get_index_bars(code, chunk_start, year_end))
                year_start = year_end + timedelta(days=1)
            by_date = {bar.time.astimezone(KST).date(): bar for bar in bars}
            bars = [by_date[session] for session in sorted(by_date)]
            if not bars:
                raise KisApiError("KIS returned no index rows")
            require_healthy_bars(bars)
            path, _ = cache.save(
                bars, kind="indexes", symbol=name, interval=INDEX_INTERVAL,
                market=name, source=f"KIS {INDEX_ENDPOINT}; daily index OHLC",
                requested_at=datetime.now(KST), requested_start=WARMUP_START,
                requested_end=POOL_END,
            )
            metadata = _write_adjusted_cache_metadata(
                cache, kind="indexes", symbol=name, interval=INDEX_INTERVAL,
                market=name, start=WARMUP_START, end=POOL_END,
                adjustment="N/A_INDEX_NOT_ADJUSTED",
            )
            manifest["indexes"][name] = {
                "status": "COMPLETE", "index_code": code, "rows": len(bars),
                "first_date": bars[0].time.astimezone(KST).date().isoformat(),
                "last_date": bars[-1].time.astimezone(KST).date().isoformat(),
                "provider": "KIS Open API", "sha256": metadata["sha256"],
                "cache_path": path.relative_to(output_root).as_posix(),
            }
        except (KisApiError, KisAuthError, OSError, RuntimeError, ValueError) as exc:
            failure = {"index": name, "error_type": type(exc).__name__}
            manifest["indexes"][name] = {"status": "FAILED", **failure}
            manifest["errors"].append(failure)
            if isinstance(exc, KisAuthError):
                break
        _atomic_json(manifest_path, manifest)
    complete_symbols = sum(
        item.get("status") in {"COMPLETE", "COMPLETE_CACHED"}
        for item in manifest["symbols"].values()
    )
    complete_indexes = sum(
        item.get("status") in {"COMPLETE", "COMPLETE_CACHED"}
        for item in manifest["indexes"].values()
    )
    manifest["complete_symbols"] = complete_symbols
    manifest["complete_indexes"] = complete_indexes
    manifest["status"] = "COMPLETE" if complete_symbols == len(symbols) and complete_indexes == 2 else "PARTIAL"
    manifest["finished_at"] = datetime.now(KST).isoformat()
    manifest["attempt_history"].append({
        "finished_at": manifest["finished_at"],
        "status": manifest["status"],
        "errors": list(manifest["errors"]),
    })
    _atomic_json(manifest_path, manifest)
    return manifest


def load_phase11_cache(
    output_root: Path, symbols: list[str], market_by_symbol: dict[str, str] | None = None
) -> tuple[dict[str, list[Bar]], dict[str, list[Bar]]]:
    cache = ParquetBarCache(output_root / "daily_cache")
    prices: dict[str, list[Bar]] = {}
    for symbol in symbols:
        verified = _load_verified_cache(
            cache, kind="daily", symbol=symbol, interval=DAILY_INTERVAL,
            start=WARMUP_START, end=POOL_END, adjustment=ADJUSTMENT,
            expected_market=market_by_symbol.get(symbol) if market_by_symbol else None,
        )
        if verified is not None:
            prices[symbol] = verified[0]
    indexes: dict[str, list[Bar]] = {}
    for name in ("KOSPI", "KOSDAQ"):
        verified = _load_verified_cache(
            cache, kind="indexes", symbol=name, interval=INDEX_INTERVAL,
            start=WARMUP_START, end=POOL_END, adjustment="N/A_INDEX_NOT_ADJUSTED",
            expected_market=name,
        )
        if verified is not None:
            indexes[name] = verified[0]
    return prices, indexes


def _bars_by_date(bars: list[Bar]) -> dict[date, Bar]:
    output: dict[date, Bar] = {}
    for bar in bars:
        session = bar.time.astimezone(KST).date()
        if session in output:
            raise ValueError(f"duplicate daily bar date {session}")
        output[session] = bar
    return output


def _return_pct(bars: dict[date, Bar], sessions: list[date], index: int, days: int) -> float | None:
    if index < days:
        return None
    window = sessions[index - days:index + 1]
    if any(session not in bars for session in window):
        return None
    before, after = bars[window[0]].close, bars[window[-1]].close
    return (after / before - 1.0) * 100.0 if before > 0 else None


def _daily_return_values(
    bars: dict[date, Bar], sessions: list[date], index: int, count: int
) -> list[float] | None:
    if index < count:
        return None
    values: list[float] = []
    for current_index in range(index - count + 1, index + 1):
        prior_date, current_date = sessions[current_index - 1], sessions[current_index]
        if prior_date not in bars or current_date not in bars or bars[prior_date].close <= 0:
            return None
        values.append(bars[current_date].close / bars[prior_date].close - 1.0)
    return values


def _range_pct(
    bars: dict[date, Bar], sessions: list[date], index: int, count: int
) -> float | None:
    if index < count - 1:
        return None
    window = sessions[index - count + 1:index + 1]
    if any(session not in bars for session in window):
        return None
    high = max(bars[session].high for session in window)
    low = min(bars[session].low for session in window)
    close = bars[window[-1]].close
    return (high - low) / close * 100.0 if close > 0 else None


def _ratio(current: float | None, prior: list[float | int | None]) -> float | None:
    if current is None or not prior or any(value is None for value in prior):
        return None
    median = statistics.median(value for value in prior if value is not None)
    return float(current) / median if median > 0 else None


def build_daily_features(
    *,
    prices_by_symbol: dict[str, list[Bar]],
    indexes: dict[str, list[Bar]],
    symbols: list[str],
    market_by_symbol: dict[str, str],
    sessions: list[date],
) -> list[dict[str, Any]]:
    """Build only completed-day features; no fill or future-row access is used."""
    price_dates = {symbol: _bars_by_date(prices_by_symbol.get(symbol, [])) for symbol in symbols}
    index_dates = {name: _bars_by_date(indexes.get(name, [])) for name in ("KOSPI", "KOSDAQ")}
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        market = market_by_symbol[symbol]
        stock = price_dates[symbol]
        market_bars = index_dates[market]
        for index, session in enumerate(sessions):
            if session < POOL_START or session > POOL_END:
                continue
            current = stock.get(session)
            if current is None:
                continue
            row: dict[str, Any] = {
                "symbol": symbol, "market": market, "date": session.isoformat(),
                "quarter": f"{session.year}-Q{(session.month - 1) // 3 + 1}",
                "half_year": f"{session.year}-H{1 if session.month <= 6 else 2}",
                "close_adjusted": current.close, "volume": current.volume,
                "turnover_krw": current.turnover_krw,
            }
            for days in (1, 3, 5, 10, 20):
                row[f"return_{days}d_pct"] = _return_pct(stock, sessions, index, days)
            for days in (5, 20):
                daily = _daily_return_values(stock, sessions, index, days)
                row[f"volatility_{days}d_pct"] = (
                    statistics.stdev(daily) * 100.0 if daily is not None and len(daily) >= 2 else None
                )
                row[f"range_{days}d_pct"] = _range_pct(stock, sessions, index, days)
            if index >= 19 and all(day in stock for day in sessions[index - 19:index + 1]):
                trailing = [stock[day] for day in sessions[index - 19:index + 1]]
                high = max(bar.high for bar in trailing)
                low = min(bar.low for bar in trailing)
                row["distance_20d_high_pct"] = (current.close / high - 1.0) * 100.0 if high > 0 else None
                row["distance_20d_low_pct"] = (current.close / low - 1.0) * 100.0 if low > 0 else None
            else:
                row["distance_20d_high_pct"] = None
                row["distance_20d_low_pct"] = None
            prior = stock.get(sessions[index - 1]) if index >= 1 else None
            row["overnight_gap_pct"] = (
                (current.open / prior.close - 1.0) * 100.0
                if prior is not None and prior.close > 0 else None
            )
            row["intraday_return_pct"] = (
                (current.close / current.open - 1.0) * 100.0 if current.open > 0 else None
            )
            for days in (5, 20):
                if index >= days:
                    prior_bars = [stock.get(day) for day in sessions[index - days:index]]
                    row[f"volume_ratio_{days}d"] = _ratio(
                        current.volume, [bar.volume if bar else None for bar in prior_bars]
                    )
                    row[f"turnover_ratio_{days}d"] = _ratio(
                        current.turnover_krw,
                        [bar.turnover_krw if bar else None for bar in prior_bars],
                    )
                else:
                    row[f"volume_ratio_{days}d"] = None
                    row[f"turnover_ratio_{days}d"] = None
            for days in (1, 5, 20):
                row[f"market_return_{days}d_pct"] = _return_pct(market_bars, sessions, index, days)
                symbol_return = row[f"return_{days}d_pct"]
                market_return = row[f"market_return_{days}d_pct"]
                row[f"symbol_minus_market_{days}d_pct"] = (
                    symbol_return - market_return
                    if symbol_return is not None and market_return is not None else None
                )
            market_daily = _daily_return_values(market_bars, sessions, index, 20)
            row["market_volatility_20d_pct"] = (
                statistics.stdev(market_daily) * 100.0
                if market_daily is not None and len(market_daily) >= 2 else None
            )
            row["market_breadth_advancing_pct"] = None
            row["market_breadth_available_symbols"] = 0
            rows.append(row)
    breadth: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        value = row["return_1d_pct"]
        if value is not None:
            breadth[(row["market"], row["date"])].append(value)
    for row in rows:
        values = breadth[(row["market"], row["date"])]
        row["market_breadth_available_symbols"] = len(values)
        row["market_breadth_advancing_pct"] = (
            sum(value > 0 for value in values) / len(values) * 100.0 if values else None
        )
    return rows


def classify_state(factor: str, value: float | None) -> str | None:
    if value is None or factor not in STATE_RULES:
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    for index, (lower, upper, label) in enumerate(STATE_RULES[factor]):
        if (index == 0 and number <= upper) or (index == len(STATE_RULES[factor]) - 1 and number >= lower):
            return label
        if lower <= number < upper:
            return label
    return None


def build_outcomes(
    *,
    features: list[dict[str, Any]],
    prices_by_symbol: dict[str, list[Bar]],
    sessions: list[date],
    period: tuple[date, date],
    horizons: tuple[int, ...] = OUTCOME_HORIZONS,
) -> list[dict[str, Any]]:
    start, end = period
    index_by_session = {session: index for index, session in enumerate(sessions)}
    by_symbol = {symbol: _bars_by_date(bars) for symbol, bars in prices_by_symbol.items()}
    output: list[dict[str, Any]] = []
    for feature in features:
        signal_date = date.fromisoformat(feature["date"])
        if not start <= signal_date <= end:
            continue
        index = index_by_session[signal_date]
        stock = by_symbol[feature["symbol"]]
        for horizon in horizons:
            exit_index = index + horizon
            if exit_index >= len(sessions) or sessions[exit_index] > end:
                continue
            path = sessions[index + 1:exit_index + 1]
            if len(path) != horizon or any(session not in stock for session in path):
                continue
            entry = stock[path[0]].open
            exit_close = stock[path[-1]].close
            if entry <= 0 or exit_close <= 0:
                continue
            output.append({
                "symbol": feature["symbol"], "market": feature["market"],
                "signal_date": signal_date.isoformat(), "entry_date": path[0].isoformat(),
                "exit_date": path[-1].isoformat(), "horizon_days": horizon,
                "entry_open": entry, "exit_close": exit_close,
                "gross_return_pct": (exit_close / entry - 1.0) * 100.0,
            })
    return output


def non_overlapping_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    last_exit: dict[str, date] = {}
    for event in sorted(events, key=lambda row: (row["signal_date"], row["symbol"])):
        entry = date.fromisoformat(event["entry_date"])
        exit_date = date.fromisoformat(event["exit_date"])
        if entry <= last_exit.get(event["symbol"], date.min):
            continue
        selected.append(event)
        last_exit[event["symbol"]] = exit_date
    return selected


def _payoff_ratio(values: list[float]) -> float | None:
    gains = [value for value in values if value > 0]
    losses = [-value for value in values if value < 0]
    if not losses:
        return None
    if not gains:
        return 0.0
    return statistics.fmean(gains) / statistics.fmean(losses)


def _cluster_bootstrap(events: list[dict[str, Any]], iterations: int = BOOTSTRAP_ITERATIONS) -> dict[str, float] | None:
    by_session: dict[str, list[float]] = defaultdict(list)
    for event in events:
        by_session[event["signal_date"]].append(float(event["gross_return_pct"]))
    dates = sorted(by_session)
    if len(dates) < 2:
        return None
    rng = random.Random(1103)
    samples: list[float] = []
    for _ in range(iterations):
        values: list[float] = []
        for _ in dates:
            values.extend(by_session[rng.choice(dates)])
        if values:
            samples.append(statistics.fmean(values))
    samples.sort()
    return {
        "iterations": iterations,
        "lower_90_pct": samples[int(0.05 * (len(samples) - 1))],
        "median_pct": samples[int(0.50 * (len(samples) - 1))],
        "upper_90_pct": samples[int(0.95 * (len(samples) - 1))],
        "unique_signal_sessions": len(dates),
    }


def _metrics(events: list[dict[str, Any]], *, bootstrap: bool = False) -> dict[str, Any]:
    raw = list(events)
    selected = non_overlapping_events(raw)
    values = [float(event["gross_return_pct"]) for event in selected]
    raw_values = [float(event["gross_return_pct"]) for event in raw]
    gross = statistics.fmean(values) if values else None
    raw_gross = statistics.fmean(raw_values) if raw_values else None
    unique_dates = len({event["signal_date"] for event in raw})
    unique_symbols = len({event["symbol"] for event in raw})
    symbol_counts: dict[str, int] = defaultdict(int)
    date_counts: dict[str, int] = defaultdict(int)
    for event in raw:
        symbol_counts[event["symbol"]] += 1
        date_counts[event["signal_date"]] += 1
    stats: dict[str, Any] = {
        "raw_event_count": len(raw), "non_overlapping_event_count": len(selected),
        "unique_signal_session_count": unique_dates, "unique_symbol_count": unique_symbols,
        "gross_mean_pct": gross, "gross_median_pct": statistics.median(values) if values else None,
        "raw_gross_mean_pct": raw_gross,
        "win_rate_pct": sum(value > 0 for value in values) / len(values) * 100.0 if values else None,
        "payoff_ratio": _payoff_ratio(values), "break_even_friction_pct": gross,
        "net_mean_pct_by_cost": {
            str(multiplier): gross - BASE_COST_PCT * multiplier if gross is not None else None
            for multiplier in COST_MULTIPLIERS
        },
        "max_symbol_event_share": max(symbol_counts.values(), default=0) / len(raw) if raw else None,
        "max_signal_session_event_share": max(date_counts.values(), default=0) / len(raw) if raw else None,
        "sample_status": (
            "BROAD_STATE" if len(raw) >= MIN_RAW_EVENTS
            and len(selected) >= MIN_NON_OVERLAPPING and unique_dates >= MIN_UNIQUE_SESSIONS
            else "DESCRIPTIVE_ONLY"
        ),
        "non_overlapping_events": selected,
    }
    if bootstrap:
        stats["session_cluster_bootstrap"] = _cluster_bootstrap(selected)
    return stats


def _period_metrics(events: list[dict[str, Any]], *, key: str, label_fn: Callable[[date], str]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        groups[label_fn(date.fromisoformat(event["signal_date"]))].append(event)
    return {
        label: {
            "raw_event_count": len(group),
            "unique_signal_session_count": len({event["signal_date"] for event in group}),
            "non_overlapping_gross_mean_pct": _metrics(group)["gross_mean_pct"],
        }
        for label, group in sorted(groups.items())
    }


def _state_analysis(
    features: list[dict[str, Any]], outcomes: list[dict[str, Any]], *, factor: str,
    horizon: int, session_cluster: bool = False,
) -> list[dict[str, Any]]:
    outcome_by_key = {
        (row["symbol"], row["signal_date"], row["horizon_days"]): row
        for row in outcomes if row["horizon_days"] == horizon
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for feature in features:
        state = classify_state(factor, feature.get(factor))
        if state is None:
            continue
        outcome = outcome_by_key.get((feature["symbol"], feature["date"], horizon))
        if outcome is not None:
            groups[state].append(outcome)
    output: list[dict[str, Any]] = []
    for state, events in sorted(groups.items()):
        metrics = _metrics(events)
        nonoverlap = metrics.pop("non_overlapping_events")
        if session_cluster:
            metrics["session_cluster_bootstrap"] = _cluster_bootstrap(nonoverlap)
        quarterly = _period_metrics(nonoverlap, key="quarter", label_fn=lambda day: f"{day.year}-Q{(day.month - 1) // 3 + 1}")
        halfyear = _period_metrics(nonoverlap, key="half_year", label_fn=lambda day: f"{day.year}-H{1 if day.month <= 6 else 2}")
        market_split: dict[str, Any] = {}
        for market in ("KOSPI", "KOSDAQ"):
            market_rows = [event for event in events if event["market"] == market]
            market_metrics = _metrics(market_rows)
            market_metrics.pop("non_overlapping_events")
            market_split[market] = market_metrics
        record = {
            "factor": factor, "state": state, "horizon_days": horizon,
            **metrics, "quarterly_stability": quarterly, "half_year_stability": halfyear,
            "market_split": market_split,
        }
        output.append(record)
    return output


def build_factor_maps(features: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    dev_features = [row for row in features if DEVELOPMENT[0].isoformat() <= row["date"] <= DEVELOPMENT[1].isoformat()]
    maps: dict[str, Any] = {}
    for family, factors in FACTOR_FAMILIES.items():
        factor_map: dict[str, Any] = {}
        for factor in factors:
            by_horizon = {
                str(horizon): _state_analysis(
                    dev_features, outcomes, factor=factor, horizon=horizon,
                    session_cluster=(
                        factor in {"market_return_1d_pct", "market_return_5d_pct", "market_return_20d_pct"}
                        and horizon in PRIMARY_HORIZONS
                    ),
                )
                for horizon in OUTCOME_HORIZONS
            }
            factor_map[factor] = {
                "source": "KIS adjusted daily OHLCV" if factor in FACTOR_FAMILIES["price"] + FACTOR_FAMILIES["liquidity"] else "KIS daily indexes + adjusted daily OHLCV",
                "states": [{"state": label} for _, _, label in STATE_RULES[factor]],
                "development": by_horizon,
            }
        maps[family] = factor_map
    flow_map = {
        "status": "NOT_AVAILABLE_FOR_MULTIYEAR_PER_SYMBOL_PANEL",
        "source_audit": "phase11-source-audit.json",
        "factors": {
            f"{investor}_{horizon}d": {"status": "MISSING_SOURCE", "values": None}
            for investor in ("foreign", "institution", "individual")
            for horizon in (1, 3, 5, 10)
        },
        "missing_is_not_zero": True,
        "no_forward_fill": True,
    }
    maps["flow"] = flow_map
    return maps


def _interaction_map(features: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    dev = [row for row in features if DEVELOPMENT[0].isoformat() <= row["date"] <= DEVELOPMENT[1].isoformat()]
    output: dict[str, Any] = {
        "price_x_flow": {
            "status": "NOT_AVAILABLE",
            "reason": "No multi-year point-in-time per-symbol investor-flow panel is reproducibly available from the inspected KIS endpoints.",
            "cells": [],
        },
        "price_x_liquidity": {"status": "DESCRIPTIVE", "horizons": {}},
    }
    for horizon in OUTCOME_HORIZONS:
        outcome_by_key = {
            (row["symbol"], row["signal_date"], horizon): row
            for row in outcomes if row["horizon_days"] == horizon
        }
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for feature in dev:
            price = feature.get("return_5d_pct")
            turnover = feature.get("turnover_ratio_20d")
            if price is None or turnover is None:
                continue
            price_state = "PRICE_DOWN" if price < 0 else "PRICE_FLAT_OR_UP"
            liquidity_state = "TURNOVER_EXPANSION" if turnover >= 1.5 else "NO_EXPANSION"
            event = outcome_by_key.get((feature["symbol"], feature["date"], horizon))
            if event is not None:
                groups[f"{price_state}__{liquidity_state}"].append(event)
        output["price_x_liquidity"]["horizons"][str(horizon)] = {
            state: {key: value for key, value in _metrics(events).items() if key != "non_overlapping_events"}
            for state, events in sorted(groups.items())
        }
    return output


def _gate(metrics: dict[str, Any]) -> dict[str, Any]:
    quarterly = metrics.get("quarterly_stability", {})
    halfyear = metrics.get("half_year_stability", {})
    positive_quarters = sum(
        item.get("non_overlapping_gross_mean_pct") is not None
        and item["non_overlapping_gross_mean_pct"] > 0
        and item.get("raw_event_count", 0) >= 10
        for item in quarterly.values()
    )
    positive_halves = sum(
        item.get("non_overlapping_gross_mean_pct") is not None
        and item["non_overlapping_gross_mean_pct"] > 0
        and item.get("raw_event_count", 0) >= 10
        for item in halfyear.values()
    )
    stress = metrics.get("net_mean_pct_by_cost", {})
    bootstrap = metrics.get("session_cluster_bootstrap")
    market = metrics.get("market_split", {})
    market_ok = all(
        item.get("non_overlapping_event_count", 0) < 20 or item.get("gross_mean_pct") is None
        or item["gross_mean_pct"] >= 0
        for item in market.values()
    )
    checks = {
        "sample": metrics.get("raw_event_count", 0) >= MIN_RAW_EVENTS
        and metrics.get("non_overlapping_event_count", 0) >= MIN_NON_OVERLAPPING
        and metrics.get("unique_signal_session_count", 0) >= MIN_UNIQUE_SESSIONS,
        "gross_cost_sized": (metrics.get("gross_mean_pct") or -math.inf) > PROMOTION_GROSS_MIN_PCT,
        "net_positive_1x": (stress.get("1.0") or -math.inf) > 0,
        "net_positive_1_5x": (stress.get("1.5") or -math.inf) > 0,
        "payoff_gt_1": (metrics.get("payoff_ratio") or -math.inf) > 1.0,
        "multiple_positive_quarters": positive_quarters >= 2,
        "multiple_positive_half_years": positive_halves >= 2,
        "not_symbol_or_date_concentrated": (metrics.get("max_symbol_event_share") or 1.0) <= 0.20
        and (metrics.get("max_signal_session_event_share") or 1.0) <= 0.20,
        "session_cluster_not_fragile": bootstrap is not None
        and bootstrap["lower_90_pct"] > -BASE_COST_PCT,
        "enough_non_overlapping_signal_sessions": bootstrap is not None
        and bootstrap["unique_signal_sessions"] >= MIN_UNIQUE_SESSIONS,
        "market_split_not_opposite": market_ok,
    }
    return {
        "checks": checks,
        "positive_quarters": positive_quarters,
        "positive_half_years": positive_halves,
        "pass": all(checks.values()),
    }


def _candidate_family_evaluation(
    family: str,
    feature: str,
    state: str,
    features: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    *,
    horizon: int = 5,
    rule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feature_lookup = {(row["symbol"], row["date"]): row for row in features}
    selected = []
    for outcome in outcomes:
        if outcome["horizon_days"] != horizon:
            continue
        row = feature_lookup.get((outcome["symbol"], outcome["signal_date"]))
        if row is not None and classify_state(feature, row.get(feature)) == state:
            selected.append(outcome)
    metrics = _metrics(selected)
    nonoverlap = metrics["non_overlapping_events"]
    metrics["quarterly_stability"] = _period_metrics(
        nonoverlap, key="quarter", label_fn=lambda day: f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    )
    metrics["half_year_stability"] = _period_metrics(
        nonoverlap, key="half_year", label_fn=lambda day: f"{day.year}-H{1 if day.month <= 6 else 2}"
    )
    metrics["market_split"] = {}
    for market in ("KOSPI", "KOSDAQ"):
        part = _metrics([event for event in selected if event["market"] == market])
        part.pop("non_overlapping_events")
        metrics["market_split"][market] = part
    if len(selected) >= MIN_RAW_EVENTS and len(nonoverlap) >= MIN_NON_OVERLAPPING:
        metrics["session_cluster_bootstrap"] = _cluster_bootstrap(nonoverlap)
    else:
        metrics["session_cluster_bootstrap"] = None
    gate = _gate(metrics)
    return {
        "family": family, "feature": feature, "state": state, "horizon_days": horizon,
        "rule": rule or next(item for item in HYPOTHESES["candidate_eligible_rules"] if item["family"] == family),
        "metrics": {key: value for key, value in metrics.items() if key != "non_overlapping_events"},
        "gate": gate,
        "_events": nonoverlap,
    }


def _evaluate_frozen_candidate(
    candidate: dict[str, Any],
    *,
    features: list[dict[str, Any]],
    prices: dict[str, list[Bar]],
    sessions: list[date],
    period: tuple[date, date],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    outcomes = build_outcomes(
        features=features, prices_by_symbol=prices, sessions=sessions,
        period=period, horizons=(int(candidate["horizon_days"]),),
    )
    feature_lookup = {(row["symbol"], row["date"]): row for row in features}
    events = [
        event for event in outcomes
        if (row := feature_lookup.get((event["symbol"], event["signal_date"]))) is not None
        and classify_state(candidate["feature"], row.get(candidate["feature"])) == candidate["state"]
    ]
    metrics = _metrics(events)
    nonoverlap = metrics["non_overlapping_events"]
    metrics["quarterly_stability"] = _period_metrics(
        nonoverlap, key="quarter", label_fn=lambda day: f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    )
    metrics["half_year_stability"] = _period_metrics(
        nonoverlap, key="half_year", label_fn=lambda day: f"{day.year}-H{1 if day.month <= 6 else 2}"
    )
    metrics["market_split"] = {}
    for market in ("KOSPI", "KOSDAQ"):
        part = _metrics([event for event in events if event["market"] == market])
        part.pop("non_overlapping_events")
        metrics["market_split"][market] = part
    metrics["session_cluster_bootstrap"] = (
        _cluster_bootstrap(nonoverlap)
        if len(events) >= MIN_RAW_EVENTS and len(nonoverlap) >= MIN_NON_OVERLAPPING else None
    )
    return nonoverlap, {key: value for key, value in metrics.items() if key != "non_overlapping_events"}


def _date_sessions(indexes: dict[str, list[Bar]]) -> list[date]:
    kospi = set(_bars_by_date(indexes.get("KOSPI", [])).keys())
    kosdaq = set(_bars_by_date(indexes.get("KOSDAQ", [])).keys())
    sessions = sorted(kospi & kosdaq)
    return [session for session in sessions if WARMUP_START <= session <= POOL_END]


def _dataset_quality(
    prices: dict[str, list[Bar]], indexes: dict[str, list[Bar]],
    symbols: list[str], market_by_symbol: dict[str, str], sessions: list[date],
) -> dict[str, Any]:
    primary_sessions = [session for session in sessions if POOL_START <= session <= POOL_END]
    by_symbol: dict[str, Any] = {}
    multi_year: list[str] = []
    dense_multi_year: list[str] = []
    total_rows = 0
    for symbol in symbols:
        bars = sorted(prices.get(symbol, []), key=lambda item: item.time)
        dates = [bar.time.astimezone(KST).date() for bar in bars]
        date_set = set(dates)
        in_pool = [session for session in dates if POOL_START <= session <= POOL_END]
        expected = [session for session in primary_sessions if session in date_set or (
            in_pool and in_pool[0] <= session <= in_pool[-1]
        )]
        missing_inside = [session for session in expected if session not in date_set]
        total_rows += len(in_pool)
        coverage = {
            "first_date": dates[0].isoformat() if dates else None,
            "last_date": dates[-1].isoformat() if dates else None,
            "pool_first_date": in_pool[0].isoformat() if in_pool else None,
            "pool_last_date": in_pool[-1].isoformat() if in_pool else None,
            "pool_rows": len(in_pool),
            "warmup_rows": sum(session < POOL_START for session in dates),
            "expected_index_sessions_within_observed_span": len(expected),
            "missing_sessions_inside_observed_span": len(missing_inside),
            "observed_span_coverage_pct": (
                (len(expected) - len(missing_inside)) / len(expected) * 100.0 if expected else None
            ),
            "continuity_status": "COMPLETE" if not missing_inside else "GAPS_PRESERVED_NO_FILL",
            "provider": "KIS Open API",
            "adjustment_convention": ADJUSTMENT,
            "turnover_rows": sum(bar.turnover_krw is not None for bar in bars if POOL_START <= bar.time.astimezone(KST).date() <= POOL_END),
            "sha256": None,
        }
        by_symbol[symbol] = coverage
        if (
            len(in_pool) >= 500 and in_pool[0] <= POOL_START + timedelta(days=90)
            and in_pool[-1] >= POOL_END - timedelta(days=30)
        ):
            multi_year.append(symbol)
            if not missing_inside:
                dense_multi_year.append(symbol)
    indexes_ok = all(name in indexes and indexes[name] for name in ("KOSPI", "KOSDAQ"))
    status = "COMPLETE" if len(dense_multi_year) >= 60 and indexes_ok and len(primary_sessions) >= 700 else "PARTIAL"
    return {
        "status": status,
        "primary_pool": {"start": POOL_START.isoformat(), "end": POOL_END.isoformat(),
                         "common_index_sessions": len(primary_sessions)},
        "warmup_only": {"start": WARMUP_START.isoformat(), "end": (POOL_START - timedelta(days=1)).isoformat()},
        "cohort_symbol_count": len(symbols), "symbols_with_500_plus_rows_and_multi_year_span": len(multi_year),
        "symbols_with_dense_multi_year_span": len(dense_multi_year),
        "multi_year_symbols": multi_year, "daily_rows_in_primary_pool": total_rows,
        "index_rows": {name: len(indexes.get(name, [])) for name in ("KOSPI", "KOSDAQ")},
        "index_sources_available": indexes_ok,
        "per_symbol_coverage": by_symbol,
        "current_listing_survivorship_bias": True,
        "survivorship_limitation": "CURRENT-LISTING SURVIVORSHIP BIAS: current KIS cohort membership is used for all historical dates; historical point-in-time membership is not reconstructed.",
        "missing_price_rows_are_not_filled": True,
        "synthetic_bars": False,
    }


def _candidate_decision(
    dev_features: list[dict[str, Any]], dev_outcomes: list[dict[str, Any]],
    dataset_quality: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    evaluations = [
        _candidate_family_evaluation(
            "MARKET_RELATIVE_CONTINUATION", "symbol_minus_market_5d_pct", "RELATIVE_STRONG",
            dev_features, dev_outcomes,
        ),
        _candidate_family_evaluation(
            "LIQUIDITY_EXPANSION", "turnover_ratio_20d", "EXPANSION",
            dev_features, dev_outcomes,
        ),
    ]
    context_evaluation = _candidate_family_evaluation(
        MAP_DERIVED_CANDIDATE_RULE["family"],
        MAP_DERIVED_CANDIDATE_RULE["feature"],
        MAP_DERIVED_CANDIDATE_RULE["state"],
        dev_features,
        dev_outcomes,
        horizon=MAP_DERIVED_CANDIDATE_RULE["horizon_sessions"],
        rule=MAP_DERIVED_CANDIDATE_RULE,
    )
    context_evaluation["origin"] = "MAP_DERIVED_EXPLORATORY_EXTENSION"
    evaluations.append(context_evaluation)
    for evaluation in evaluations:
        evaluation["gate"]["checks"]["minimum_multi_year_dataset"] = dataset_quality["status"] == "COMPLETE"
        evaluation["gate"]["pass"] = all(evaluation["gate"]["checks"].values())
    predeclared_passing = [
        item for item in evaluations[:2] if item["gate"]["pass"]
    ]
    selected = max(
        predeclared_passing,
        key=lambda item: item["metrics"].get("gross_mean_pct") or -math.inf,
        default=None,
    )
    if selected is None and context_evaluation["gate"]["pass"]:
        selected = context_evaluation
    candidate = None
    if selected is not None:
        candidate = {
            "family": selected["family"], "feature": selected["feature"],
            "state": selected["state"], "horizon_days": selected["horizon_days"],
            "exact_rule": selected["rule"],
            "entry_exit": (
                selected["rule"].get("entry", HYPOTHESES["outcomes"]["entry"])
                + "; " + selected["rule"].get("exit", HYPOTHESES["outcomes"]["exit"])
            ),
            "frozen_from": "Development only",
            "origin": selected.get("origin", "PREDECLARED_CANDIDATE_RULE"),
        }
    report = {
        "artifact": "phase11-development-candidate-gate",
        "period": {"start": DEVELOPMENT[0].isoformat(), "end": DEVELOPMENT[1].isoformat()},
        "candidate_status": "RESEARCH_CANDIDATE" if candidate else "NOT_CREATED",
        "candidate": candidate,
        "evaluated_development_families": [
            {key: value for key, value in item.items() if key != "_events"}
            for item in evaluations
        ],
        "map_derived_extension": MAP_DERIVED_CANDIDATE_RULE,
        "maximum_candidate_families": 1,
        "secondary_read": False,
        "validation_read_before_freeze": False,
        "confirmation_read_before_validation_pass": False,
    }
    return report, selected


def _factor_map_best(maps: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for family in ("price", "liquidity", "market_relative"):
        candidates: list[dict[str, Any]] = []
        for payload in maps.get(family, {}).values():
            candidates.extend(payload.get("development", {}).get("5", []))
        adequate = [item for item in candidates if item["sample_status"] == "BROAD_STATE"]
        pool = adequate or candidates
        best = max(
            pool,
            key=lambda item: (
                item.get("gross_mean_pct") if item.get("gross_mean_pct") is not None else -math.inf,
                item.get("raw_event_count", 0),
            ),
            default=None,
        )
        results[family] = best
    results["flow"] = None
    results["price_x_flow"] = None
    return results


def _index_artifacts(output_root: Path) -> dict[str, Any]:
    entries = []
    for path in sorted(output_root.rglob("*")):
        if not path.is_file() or path.name == "phase11-artifact-index.json" or path.name.endswith(".tmp"):
            continue
        entries.append({
            "path": path.relative_to(output_root).as_posix(),
            "size_bytes": path.stat().st_size, "sha256": _sha256(path),
        })
    return {"artifact": "phase11-artifact-index", "algorithm": "SHA-256", "artifacts": entries}


def verify_artifact_index(output_root: Path, index: dict[str, Any]) -> dict[str, Any]:
    failures = []
    for artifact in index.get("artifacts", []):
        path = output_root / artifact["path"]
        if not path.is_file() or path.stat().st_size != artifact["size_bytes"] or _sha256(path) != artifact["sha256"]:
            failures.append(artifact["path"])
    return {"status": "PASS" if not failures else "FAIL", "verified": len(index.get("artifacts", [])), "failures": failures}


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNAVAILABLE"


def _make_kis_client(settings: Settings) -> KisRestClient:
    if settings.trading_mode != "shadow" or settings.live_trading_enabled:
        raise ValueError("Phase 11 data acquisition requires the existing read-only shadow configuration")
    transport = UrllibTransport()
    tokens = TokenManager(settings.kis_app_key, settings.kis_app_secret, transport)
    return KisRestClient(settings.kis_app_key, settings.kis_app_secret, tokens, transport)


def run_phase11(
    *, output_root: Path = OUTPUT_ROOT, cohort_path: Path = COHORT_PATH,
    client: Any | None = None, acquire: bool = True,
) -> dict[str, Any]:
    """Acquire and map Phase 11 data; candidate selection uses Development only."""
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    baseline_hashes = snapshot_previous_phase_manifests()
    _atomic_json(output_root / "phase11-source-audit.json", SOURCE_AUDIT)
    _atomic_json(output_root / "phase11-hypotheses.json", HYPOTHESES)
    _atomic_json(output_root / "phase11-prior-phase-manifest-hashes.json", baseline_hashes)
    cohort_name, symbols, market_by_symbol, cohort_sha = _load_frozen_cohort(cohort_path)
    if acquire:
        active_client = client or _make_kis_client(Settings.from_env())
        acquisition = acquire_phase11_data(active_client, output_root=output_root, cohort_path=cohort_path)
    else:
        acquisition_path = output_root / "phase11-acquisition-manifest.json"
        acquisition = json.loads(acquisition_path.read_text(encoding="utf-8")) if acquisition_path.is_file() else {"status": "NOT_RUN"}
    prices, indexes = load_phase11_cache(output_root, symbols, market_by_symbol)
    sessions = _date_sessions(indexes)
    features = build_daily_features(
        prices_by_symbol=prices, indexes=indexes, symbols=symbols,
        market_by_symbol=market_by_symbol, sessions=sessions,
    )
    quality = _dataset_quality(prices, indexes, symbols, market_by_symbol, sessions)
    quality["cohort_name"] = cohort_name
    quality["cohort_order_sha256"] = cohort_sha
    quality["acquisition_status"] = acquisition.get("status", "NOT_RUN")
    quality["source_adjustment"] = ADJUSTMENT
    quality["daily_cache_root"] = "daily_cache/"
    quality["development_only_candidate_discovery"] = True
    quality["secondary_outcomes_computed_before_candidate_freeze"] = False
    quality["external_2026_read"] = False
    quality["holdout_2026_read"] = False
    for symbol in prices:
        path = ParquetBarCache(output_root / "daily_cache").partition_path("daily", symbol, DAILY_INTERVAL)
        if symbol in quality["per_symbol_coverage"] and path.is_file():
            quality["per_symbol_coverage"][symbol]["sha256"] = _sha256(path)
    _atomic_json(output_root / "phase11-daily-dataset.json", quality)
    _atomic_json(output_root / "phase11-factor-availability.json", {
        "daily_price": {"status": "AVAILABLE" if prices else "NOT_AVAILABLE", "adjustment": ADJUSTMENT},
        "daily_turnover": {
            "status": "AVAILABLE" if any(bar.turnover_krw is not None for bars in prices.values() for bar in bars) else "NOT_AVAILABLE",
            "source_field": "acml_tr_pbmn", "missing_values_remain_null": True,
        },
        "volume": {"status": "AVAILABLE" if prices else "NOT_AVAILABLE", "source_field": "acml_vol"},
        "stock_investor_flow": {"status": "NOT_AVAILABLE", "reason": SOURCE_AUDIT["flow_decision"]},
        "market_indexes": {"status": "AVAILABLE" if len(indexes) == 2 else "PARTIAL", "names": sorted(indexes)},
        "historical_market_cap": "EXCLUDED_POINT_IN_TIME_UNVERIFIED",
        "fundamentals": "EXCLUDED_POINT_IN_TIME_UNVERIFIED",
        "sector_history": "NOT_AVAILABLE",
    })
    if sessions:
        features = [row for row in features if POOL_START.isoformat() <= row["date"] <= POOL_END.isoformat()]
    _atomic_csv(output_root / "daily-features.csv", features)
    dev_outcomes = build_outcomes(
        features=features, prices_by_symbol=prices, sessions=sessions,
        period=DEVELOPMENT, horizons=OUTCOME_HORIZONS,
    )
    _atomic_csv(output_root / "development-outcomes.csv", dev_outcomes)
    maps = build_factor_maps(features, dev_outcomes)
    _atomic_json(output_root / "price-factor-map.json", maps["price"])
    _atomic_json(output_root / "flow-factor-map.json", maps["flow"])
    _atomic_json(output_root / "liquidity-factor-map.json", maps["liquidity"])
    _atomic_json(output_root / "market-context-map.json", maps["market_relative"])
    _atomic_json(output_root / "candidate-map-extension.json", MAP_DERIVED_CANDIDATE_RULE)
    interactions = _interaction_map(features, dev_outcomes)
    _atomic_json(output_root / "factor-interactions.json", interactions)
    development_features = [row for row in features if DEVELOPMENT[0].isoformat() <= row["date"] <= DEVELOPMENT[1].isoformat()]
    candidate_dev, selected = _candidate_decision(development_features, dev_outcomes, quality)
    _atomic_json(output_root / "candidate-development.json", candidate_dev)
    candidate = candidate_dev["candidate"]
    validation_report: dict[str, Any] = {"status": "NOT_RUN", "reason": "No Development candidate passed the predeclared gate."}
    confirmation_report: dict[str, Any] = {"status": "NOT_RUN", "reason": "No Validation-passing frozen candidate exists."}
    if candidate is not None:
        frozen = {
            **candidate,
            "development_metrics": selected["metrics"],
            "development_gate": selected["gate"],
            "dataset_cohort_sha256": cohort_sha,
            "feature_definition_sha256": hashlib.sha256(json.dumps(HYPOTHESES, sort_keys=True).encode()).hexdigest(),
            "source_git_sha": _git_sha(),
            "frozen_at": datetime.now(KST).isoformat(),
        }
        _atomic_json(output_root / "candidate-preregistration.json", frozen)
        validation_events, validation_metrics = _evaluate_frozen_candidate(
            frozen, features=features, prices=prices, sessions=sessions, period=VALIDATION,
        )
        validation_gate = _gate(validation_metrics)
        validation_report = {
            "status": "PASS" if validation_gate["pass"] else "FAIL",
            "period": [item.isoformat() for item in VALIDATION],
            "frozen_candidate_sha256": hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
            "metrics": validation_metrics, "gate": validation_gate,
        }
        if validation_gate["pass"]:
            _, confirmation_metrics = _evaluate_frozen_candidate(
                frozen, features=features, prices=prices, sessions=sessions, period=CONFIRMATION,
            )
            confirmation_gate = _gate(confirmation_metrics)
            confirmation_report = {
                "status": "PASS" if confirmation_gate["pass"] else "FAIL",
                "period": [item.isoformat() for item in CONFIRMATION],
                "frozen_candidate_sha256": validation_report["frozen_candidate_sha256"],
                "metrics": confirmation_metrics, "gate": confirmation_gate,
                "one_shot": True,
            }
        else:
            confirmation_report["reason"] = "Validation did not pass; confirmation outcomes were not computed."
        _atomic_csv(output_root / "candidate-validation-events.csv", validation_events)
    _atomic_json(output_root / "validation.json", validation_report)
    _atomic_json(output_root / "confirmation.json", confirmation_report)
    _atomic_json(output_root / "cost-stress.json", {
        "assumed_round_trip_cost_pct": BASE_COST_PCT,
        "multipliers": list(COST_MULTIPLIERS),
        "candidate_cost_results": (
            {"development": selected["metrics"].get("net_mean_pct_by_cost"),
             "validation": validation_report.get("metrics", {}).get("net_mean_pct_by_cost"),
             "confirmation": confirmation_report.get("metrics", {}).get("net_mean_pct_by_cost")}
            if selected is not None else "NO_CANDIDATE; development factor maps retain descriptive cost columns"
        ),
        "break_even_friction_pct": selected["metrics"].get("gross_mean_pct") if selected else None,
    })
    best = _factor_map_best(maps)
    candidate_status = candidate_dev["candidate_status"]
    if candidate is not None:
        candidate_status = "REJECTED" if validation_report["status"] == "FAIL" or confirmation_report["status"] == "FAIL" else "VALIDATION_CANDIDATE"
    map_status = "PASS" if prices and len(indexes) == 2 else "PARTIAL"
    if not prices or not indexes:
        map_status = "FAIL"
    elif SOURCE_AUDIT["flow_decision"].startswith("No multi-year"):
        map_status = "PARTIAL"
    summary = {
        "artifact": "phase11-multi-year-daily-evidence",
        "source_git_sha": _git_sha(),
        "acquisition_status": acquisition.get("status", "NOT_RUN"),
        "acquisition_errors": acquisition.get("errors", []),
        "cohort": {"name": cohort_name, "symbols": len(symbols), "market_counts": {m: list(market_by_symbol.values()).count(m) for m in ("KOSPI", "KOSDAQ")}, "order_sha256": cohort_sha},
        "multi_year_data": quality["status"], "flow_data": "NOT_AVAILABLE",
        "phase11_factor_map": map_status, "phase11_candidate": candidate_status,
        "validation": validation_report["status"], "confirmation": confirmation_report["status"],
        "alpha": "UNPROVEN", "live": "DISABLED",
        "date_coverage": quality["primary_pool"],
        "current_listing_survivorship_bias": quality["survivorship_limitation"],
        "daily_adjustment_semantics": ADJUSTMENT,
        "turnover_semantics": "KIS acml_tr_pbmn; missing remains null",
        "source_audit": "phase11-source-audit.json",
        "daily_rows": quality["daily_rows_in_primary_pool"],
        "feature_rows": len(features), "development_outcome_rows": len(dev_outcomes),
        "event_and_session_counts_reported_separately": True,
        "best_development_states_5d": best,
        "best_price_only_state": best["price"], "best_flow_state": None,
        "best_price_x_flow_interaction": None,
        "price_x_flow_status": "NOT_AVAILABLE",
        "candidate": candidate,
        "validation_status": validation_report["status"],
        "confirmation_status": confirmation_report["status"],
        "external_2026_status": "NOT_READ",
        "holdout_2026_status": "NOT_READ",
        "previous_phase_manifest_integrity": compare_hash_snapshots(
            baseline_hashes, snapshot_previous_phase_manifests()
        ),
        "artifact_integrity": "SEE_SEPARATE_artifact-integrity.json",
        "report_execution_window_bug": {
            "status": "NOT_REPRODUCED_IN_REPOSITORY",
            "finding": "No execution-window field, overnight final-report generator, or the cited timestamps occur in repository source/tests/docs or the supplied STATUS/RESULTS snapshots.",
            "cause": None,
        },
        "limitations": [
            quality["survivorship_limitation"],
            "KIS adjusted history has no point-in-time vintage selector; the acquired rows are the current KIS adjusted series.",
            "No multi-year per-symbol investor-flow panel is reproducibly available from the inspected KIS endpoints; no flow values are fabricated.",
            "Phase 10 was a 49-session study, not universal evidence against daily strategies.",
            "Only Development outcomes select a candidate; Validation is one-shot and Confirmation is one-shot only after Validation passes.",
        ],
    }
    _atomic_json(output_root / "phase11-summary.json", summary)
    previous_integrity = summary["previous_phase_manifest_integrity"]
    _atomic_json(output_root / "previous-phase-manifest-integrity.json", previous_integrity)
    report = [
        "# Phase 11: Multi-year Daily Evidence and Factor Map", "",
        f"- Multi-year data: **{quality['status']}**; acquisition: **{acquisition.get('status', 'NOT_RUN')}** ({len(acquisition.get('errors', []))} unresolved errors); flow data: **NOT_AVAILABLE**; factor map: **{map_status}**.",
        f"- Pool: {POOL_START}–{POOL_END}; warmup-only data: {WARMUP_START}–{POOL_START - timedelta(days=1)}.",
        f"- Cohort: {len(symbols)} frozen current listings; KOSPI {summary['cohort']['market_counts']['KOSPI']}, KOSDAQ {summary['cohort']['market_counts']['KOSDAQ']}; current-listing survivorship bias applies.",
        f"- Primary pool rows: {quality['daily_rows_in_primary_pool']}; feature rows: {len(features)}; Development outcomes: {len(dev_outcomes)}.",
        f"- Adjustment: {ADJUSTMENT}; KIS daily traded value field: `acml_tr_pbmn`.",
        f"- Candidate: **{candidate_status}**; Validation: **{validation_report['status']}**; Confirmation: **{confirmation_report['status']}**.",
        "- External 2026: **NOT_READ**; protected Holdout 2026: **NOT_READ**; LIVE: **DISABLED**; ALPHA: **UNPROVEN**.",
        "- Phase 10 was a 49-session study; its result is not universal evidence against daily strategies.",
        "- Investor flow was excluded because a reproducible multi-year per-symbol panel and point-in-time revision semantics were not established.",
        "- Phase 5–10 manifest integrity: **" + previous_integrity["status"] + "**.",
        "",
        "The detailed per-factor state metrics, unique signal sessions, non-overlapping executions, quarterly/half-year stability, and KOSPI/KOSDAQ splits are in the machine-readable factor maps.",
    ]
    (output_root / "phase11-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    index = _index_artifacts(output_root)
    _atomic_json(output_root / "phase11-artifact-index.json", index)
    integrity = verify_artifact_index(output_root, index)
    _atomic_json(output_root / "artifact-integrity.json", integrity)
    return summary


if __name__ == "__main__":
    result = run_phase11()
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))

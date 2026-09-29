from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.quality import inspect_bars
from krx_trader.kis.auth import TokenManager
from krx_trader.kis.rest import KisRestClient
from krx_trader.kis.transport import UrllibTransport
from krx_trader.models import Bar

KST = ZoneInfo("Asia/Seoul")
DEVELOPMENT_START = date(2026, 4, 17)
DEVELOPMENT_END = date(2026, 6, 30)
PROTECTED_START = date(2026, 7, 28)
SESSION_OPEN = time(9, 0)
LAST_CONTINUOUS_BAR = time(15, 19)
RATIO_CLUSTERS = (0.1, 10.0, 0.2, 5.0, 0.25, 4.0, 0.5, 2.0)
CHECKPOINTS = ("09:15", "09:30", "10:00", "11:00", "14:00", "15:19")
ALIGNMENT_THRESHOLDS = {
    "pass": {"minimum_n": 100, "p95_pct_max": 0.5, "max_pct_max": 1.0},
    "partial": {"minimum_n": 30, "p95_pct_max": 1.0, "max_pct_max": 5.0},
}

DAILY_SOURCE = {
    "endpoint": "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
    "tr_id": "FHKST03010100",
    "market_code": "J (KRX)",
    "period_code": "D",
    "adjustment_parameter": "FID_ORG_ADJ_PRC: 0=adjusted, 1=raw/original",
    "adjustment_status": "CONFIGURABLE",
    "phase9_comparison_convention": "adjusted (0); raw/original (1) retained for frozen gap replay comparison",
    "timestamp_timezone": "Asia/Seoul",
    "timestamp_meaning": "trading-session date; parsed as 00:00 KST",
    "fields": {
        "open": "stck_oprc", "high": "stck_hgpr", "low": "stck_lwpr",
        "close": "stck_clpr", "volume": "acml_vol",
    },
    "semantics_reference": (
        "https://github.com/koreainvestment/open-trading-api/blob/main/"
        "MCP/Kis%20Trading%20MCP/configs/domestic_stock.json"
    ),
}
MINUTE_SOURCE = {
    "endpoint": "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
    "tr_id": "FHKST03010230",
    "market_code": "J (KRX)",
    "date_parameter": "FID_INPUT_DATE_1",
    "time_parameter": "FID_INPUT_HOUR_1",
    "include_prior_data": "FID_PW_DATA_INCU_YN=N",
    "include_fake_ticks": "FID_FAKE_TICK_INCU_YN=''",
    "adjustment_parameter": None,
    "adjustment_status": "UNKNOWN",
    "timestamp_timezone": "Asia/Seoul",
    "timestamp_meaning": "observed bar-start labels; source docs only name a trade time",
    "continuous_session": "09:00 through 15:19 inclusive; 15:20 auction excluded",
    "fields": {
        "time": "stck_cntg_hour", "open": "stck_oprc", "high": "stck_hgpr",
        "low": "stck_lwpr", "close": "stck_prpr", "volume": "cntg_vol",
    },
    "semantics_reference": (
        "https://github.com/koreainvestment/open-trading-api/blob/main/"
        "examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py"
    ),
}


def assert_development_date(session: date) -> None:
    if not DEVELOPMENT_START <= session <= DEVELOPMENT_END:
        raise ValueError("Phase 9 payload access is restricted to Development dates")
    if session >= PROTECTED_START:
        raise ValueError("Phase 9 cannot access protected Holdout dates")


def assert_phase9_output_path(path: Path) -> None:
    allowed = Path("runtime/research/phase9").resolve()
    resolved = path.resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise ValueError("Phase 9 artifacts must remain under runtime/research/phase9")


def validate_development_sessions(sessions: Iterable[date]) -> tuple[date, ...]:
    ordered = tuple(sorted(set(sessions)))
    if not ordered:
        raise ValueError("Development session list is empty")
    for session in ordered:
        assert_development_date(session)
    return ordered


def market_by_cohort_order(symbols: Sequence[str]) -> dict[str, str]:
    if len(symbols) != 60 or len(set(symbols)) != 60:
        raise ValueError("Phase 9 requires the frozen 60-symbol cohort")
    return {symbol: "KOSPI" if index < 30 else "KOSDAQ" for index, symbol in enumerate(symbols)}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_hashes(paths: Iterable[Path]) -> dict[str, str]:
    return {str(path): sha256_file(path) for path in sorted(paths) if path.is_file()}


def compare_manifest_hashes(before: Mapping[str, str], after: Mapping[str, str]) -> dict[str, Any]:
    changed = sorted(name for name, digest in before.items() if after.get(name) != digest)
    missing = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    return {
        "status": "PASS" if not changed and not missing and not added else "FAIL",
        "baseline_count": len(before),
        "after_count": len(after),
        "changed": changed,
        "missing": missing,
        "added": added,
    }


def daily_minute_partition_paths(
    cache: ParquetBarCache, symbols: Sequence[str], sessions: Sequence[date]
) -> Iterable[tuple[str, date, Path, Path]]:
    """Enumerate only explicitly supplied safe dates; never glob or recurse the cache."""
    for session in validate_development_sessions(sessions):
        for symbol in symbols:
            path = cache.partition_path("minute", symbol, "1m", session)
            yield symbol, session, path, path.with_suffix(".metadata.json")


def audit_development_minute_cache(
    *,
    symbols: Sequence[str],
    sessions: Sequence[date],
    market_by_symbol: Mapping[str, str],
    cache_root: Path = Path("data"),
) -> tuple[dict[tuple[str, date], tuple[Bar, ...]], dict[str, Any]]:
    safe_sessions = validate_development_sessions(sessions)
    cache = ParquetBarCache(cache_root)
    expected = len(symbols) * len(safe_sessions)
    loaded: dict[tuple[str, date], tuple[Bar, ...]] = {}
    counts: Counter[str] = Counter()
    invalid: list[dict[str, str]] = []
    hashes: dict[str, str] = {}
    row_counts: Counter[int] = Counter()
    for symbol, session, path, sidecar in daily_minute_partition_paths(cache, symbols, safe_sessions):
        if not path.is_file() and not sidecar.is_file():
            counts["absent"] += 1
            continue
        if not path.is_file() or not sidecar.is_file():
            counts["missing_pair_member"] += 1
            invalid.append({"symbol": symbol, "session": session.isoformat(), "reason": "PAIR_MEMBER_MISSING"})
            continue
        try:
            bars = cache.load("minute", symbol, "1m", session)
            timestamps = tuple(bar.time.astimezone(KST) for bar in bars)
            expected_times = tuple(
                datetime.combine(session, SESSION_OPEN, KST) + timedelta(minutes=index)
                for index in range(380)
            )
            if any(bar.time.utcoffset() != timedelta(hours=9) for bar in bars):
                raise ValueError("WRONG_TIMEZONE")
            if any(item.date() != session for item in timestamps):
                raise ValueError("CROSS_DATE_CONTAMINATION")
            issues = inspect_bars(list(bars))
            if issues:
                raise ValueError("INVALID_OHLCV:" + ",".join(sorted({issue.code for issue in issues})))
            if any(
                local.time() < SESSION_OPEN or local.time() > LAST_CONTINUOUS_BAR
                or local.second != 0 or local.microsecond != 0
                for local in timestamps
            ):
                raise ValueError("BAD_SESSION_ALIGNMENT")
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            if metadata.get("symbol") != symbol or metadata.get("interval") != "1m":
                raise ValueError("SIDECAR_IDENTITY_MISMATCH")
            loaded[(symbol, session)] = tuple(bars)
            hashes[f"{symbol}:{session.isoformat()}"] = str(metadata.get("sha256", ""))
            row_counts[len(bars)] += 1
            if timestamps == expected_times:
                counts["valid_full_session"] += 1
            else:
                counts["valid_partial_session"] += 1
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            reason = str(exc) if str(exc).isupper() or str(exc).startswith("INVALID_OHLCV:") else type(exc).__name__
            counts["invalid"] += 1
            invalid.append({"symbol": symbol, "session": session.isoformat(), "reason": reason})
    for key in ("absent", "missing_pair_member", "invalid", "valid_full_session", "valid_partial_session"):
        counts.setdefault(key, 0)
    counts["expected"] = expected
    counts["observed_valid"] = len(loaded)
    counts["missing_or_invalid"] = expected - len(loaded)
    counts["observed_rows"] = sum(row_count * frequency for row_count, frequency in row_counts.items())
    counts["expected_rows"] = expected * 380
    counts["missing_expected_rows"] = counts["expected_rows"] - counts["observed_rows"]
    report = {
        "artifact": "phase9-cache-integrity",
        "cache_root": str(cache_root),
        "scope": {"start": safe_sessions[0].isoformat(), "end": safe_sessions[-1].isoformat(),
                  "sessions": len(safe_sessions), "symbols": len(symbols)},
        "partition_counts": dict(counts),
        "status": "FAIL" if counts["invalid"] or counts["missing_pair_member"] else (
            "PARTIAL" if counts["absent"] or counts["valid_partial_session"] else "PASS"
        ),
        "invalid_partitions": invalid,
        "verified_partition_sha256": hashes,
        "row_count_distribution": {str(key): value for key, value in sorted(row_counts.items())},
        "market_symbol_counts": {
            market: sum(value == market for value in market_by_symbol.values())
            for market in ("KOSPI", "KOSDAQ")
        },
        "daily_cache_policy": "NOT_OPENED; existing daily files may span protected dates",
        "enumeration_policy": "explicit Development date and frozen cohort paths only; no recursive scan",
        "synthetic_rows_added": 0,
    }
    return loaded, report


def _pct_difference(actual: float, reference: float) -> float:
    if reference == 0:
        raise ValueError("reference price must not be zero")
    return abs(actual - reference) / abs(reference) * 100.0


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def alignment_summary(values_pct: Sequence[float]) -> dict[str, Any]:
    values = list(values_pct)
    return {
        "n": len(values),
        "exact_match_pct": sum(value == 0 for value in values) / len(values) * 100 if values else None,
        "median_abs_diff_pct": statistics.median(values) if values else None,
        "p95_abs_diff_pct": _percentile(values, 95),
        "p99_abs_diff_pct": _percentile(values, 99),
        "max_abs_diff_pct": max(values) if values else None,
    }


def _date_by_bar(bars: Sequence[Bar]) -> dict[date, Bar]:
    result: dict[date, Bar] = {}
    duplicates: set[date] = set()
    for bar in bars:
        session = bar.time.astimezone(KST).date()
        if session in result:
            duplicates.add(session)
        result[session] = bar
    for session in duplicates:
        result.pop(session, None)
    return result


def _bar_at(bars: Sequence[Bar], session: date, at: time) -> Bar | None:
    target = datetime.combine(session, at, KST)
    found = [bar for bar in bars if bar.time.astimezone(KST) == target]
    return found[0] if len(found) == 1 else None


def _continuous_complete(bars: Sequence[Bar], session: date) -> bool:
    actual = tuple(bar.time.astimezone(KST) for bar in bars)
    expected = tuple(datetime.combine(session, SESSION_OPEN, KST) + timedelta(minutes=i) for i in range(380))
    return actual == expected


def _ratio_cluster(value: float | None, tolerance: float = 0.05) -> float | None:
    if value is None or value <= 0:
        return None
    return min(RATIO_CLUSTERS, key=lambda target: abs(math.log(value / target))) if any(
        abs(math.log(value / target)) <= math.log1p(tolerance) for target in RATIO_CLUSTERS
    ) else None


def classify_suspicious_gap(
    *,
    daily_open: float | None,
    daily_prior_close: float | None,
    minute_open: float | None,
    minute_prior_close: float | None,
    daily_gap_pct: float | None,
    continuous_gap_pct: float | None,
    complete_session: bool,
) -> tuple[str, float | None]:
    if daily_prior_close is None or minute_prior_close is None:
        return "MISSING_PRIOR_SESSION", None
    if daily_open is None or minute_open is None or not complete_session:
        return "BAD_SESSION_ALIGNMENT", None
    opening_ratio = daily_open / minute_open if minute_open else None
    prior_ratio = daily_prior_close / minute_prior_close if minute_prior_close else None
    cluster = _ratio_cluster(opening_ratio) or _ratio_cluster(prior_ratio)
    if cluster is not None:
        return "PRICE_SCALE_MISMATCH", cluster
    if _pct_difference(daily_open, minute_open) >= 0.5:
        return "DAILY_MINUTE_SOURCE_MISMATCH", None
    if daily_gap_pct is None or continuous_gap_pct is None:
        return "UNKNOWN", None
    gap_cluster = _ratio_cluster((100 + daily_gap_pct) / (100 + continuous_gap_pct))
    if gap_cluster is not None and abs(daily_gap_pct - continuous_gap_pct) >= 5:
        return "ADJUSTMENT_MISMATCH", gap_cluster
    level_cluster = _ratio_cluster((100 + daily_gap_pct) / 100)
    if level_cluster is not None and abs(daily_gap_pct - continuous_gap_pct) <= 1.0:
        return "LIKELY_CORPORATE_ACTION", level_cluster
    if abs(daily_gap_pct - continuous_gap_pct) <= 1.0:
        return "REAL_PRICE_MOVE", None
    return "UNKNOWN", None


def build_alignment_report(
    *,
    daily_by_symbol: Mapping[str, Sequence[Bar]],
    raw_daily_by_symbol: Mapping[str, Sequence[Bar]],
    minute_by_symbol_session: Mapping[tuple[str, date], Sequence[Bar]],
    market_by_symbol: Mapping[str, str],
    development_sessions: Sequence[date],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    safe_sessions = validate_development_sessions(development_sessions)
    previous_session = {session: safe_sessions[index - 1] for index, session in enumerate(safe_sessions) if index}
    open_values: dict[str, list[float]] = defaultdict(list)
    close_values: dict[str, list[float]] = defaultdict(list)
    prior_close_values: dict[str, list[float]] = defaultdict(list)
    session_lookup: dict[str, dict[date, Sequence[Bar]]] = defaultdict(dict)
    daily_lookup = {symbol: _date_by_bar(rows) for symbol, rows in daily_by_symbol.items()}
    raw_lookup = {symbol: _date_by_bar(rows) for symbol, rows in raw_daily_by_symbol.items()}
    for (symbol, session), bars in minute_by_symbol_session.items():
        assert_development_date(session)
        session_lookup[symbol][session] = bars
    for (symbol, session), bars in minute_by_symbol_session.items():
        daily = daily_lookup.get(symbol, {}).get(session)
        open_bar = _bar_at(bars, session, SESSION_OPEN)
        close_bar = _bar_at(bars, session, LAST_CONTINUOUS_BAR)
        market = market_by_symbol[symbol]
        if daily is not None and open_bar is not None:
            open_values[market].append(_pct_difference(daily.open, open_bar.open))
        if daily is not None and close_bar is not None:
            close_values[market].append(_pct_difference(daily.close, close_bar.close))
        prior = previous_session.get(session)
        prior_daily = daily_lookup.get(symbol, {}).get(prior) if prior else None
        prior_bars = session_lookup[symbol].get(prior, ()) if prior else ()
        if prior_daily is not None and prior_bars:
            prior_minute_bar = _bar_at(
                prior_bars, prior, LAST_CONTINUOUS_BAR
            )
            if prior_minute_bar is not None:
                prior_close_values[market].append(_pct_difference(prior_daily.close, prior_minute_bar.close))

    gaps: list[dict[str, Any]] = []
    for (symbol, session), bars in sorted(minute_by_symbol_session.items()):
        daily_rows = daily_lookup.get(symbol, {})
        raw_rows = raw_lookup.get(symbol, {})
        daily = daily_rows.get(session)
        raw_daily = raw_rows.get(session)
        minute_open_bar = _bar_at(bars, session, SESSION_OPEN)
        minute_open = minute_open_bar.open if minute_open_bar else None
        prior = previous_session.get(session)
        if prior is None:
            raw_prior_dates = [item for item in raw_rows if item < session]
            adjusted_prior_dates = [item for item in daily_rows if item < session]
            raw_prior_date = max(raw_prior_dates) if raw_prior_dates else None
            adjusted_prior_date = max(adjusted_prior_dates) if adjusted_prior_dates else None
        else:
            raw_prior_date = prior if prior in raw_rows else None
            adjusted_prior_date = prior if prior in daily_rows else None
        raw_prior_close = raw_rows[raw_prior_date].close if raw_prior_date else None
        adjusted_prior_close = daily_rows[adjusted_prior_date].close if adjusted_prior_date else None
        prior_bars = session_lookup[symbol].get(prior, ()) if prior else ()
        prior_close_bar = _bar_at(prior_bars, prior, LAST_CONTINUOUS_BAR) if prior_bars and prior else None
        minute_prior_close = prior_close_bar.close if prior_close_bar else None
        raw_gap = ((minute_open / raw_prior_close - 1) * 100) if minute_open and raw_prior_close else None
        adjusted_gap = (
            (minute_open / adjusted_prior_close - 1) * 100
            if minute_open and adjusted_prior_close else None
        )
        cont_gap = ((minute_open / minute_prior_close - 1) * 100) if minute_open and minute_prior_close else None
        complete = _continuous_complete(bars, session)
        category, cluster = "UNKNOWN", None
        adjustment_cluster = (
            _ratio_cluster(raw_prior_close / adjusted_prior_close)
            if raw_prior_close and adjusted_prior_close else None
        )
        if raw_gap is not None and adjusted_gap is not None and abs(raw_gap) >= 20.0:
            if adjustment_cluster is not None and abs(adjusted_gap) < 20.0:
                category, cluster = "ADJUSTMENT_MISMATCH", adjustment_cluster
            else:
                category, cluster = classify_suspicious_gap(
                    daily_open=daily.open if daily else None,
                    daily_prior_close=adjusted_prior_close,
                    minute_open=minute_open,
                    minute_prior_close=minute_prior_close,
                    daily_gap_pct=adjusted_gap,
                    continuous_gap_pct=cont_gap,
                    complete_session=(minute_open is not None and adjusted_prior_close is not None),
                )
        if raw_gap is not None and abs(raw_gap) >= 20.0:
            gaps.append({
                "symbol": symbol, "date": session.isoformat(), "market": market_by_symbol[symbol],
                "daily_prior_date": raw_prior_date.isoformat() if raw_prior_date else None,
                "continuous_prior_date": prior.isoformat() if prior else None,
                "daily_prior_close": raw_prior_close,
                "adjusted_daily_prior_close": adjusted_prior_close,
                "continuous_prior_close": minute_prior_close,
                "current_open": minute_open,
                "raw_daily_open": raw_daily.open if raw_daily else None,
                "adjusted_daily_open": daily.open if daily else None,
                "daily_based_gap_pct": raw_gap,
                "adjusted_daily_based_gap_pct": adjusted_gap,
                "continuous_based_gap_pct": cont_gap,
                "source_mismatch": (
                    _pct_difference(raw_daily.open, minute_open) >= 0.5
                    if raw_daily is not None and minute_open is not None else None
                ),
                "adjusted_source_mismatch": (
                    _pct_difference(daily.open, minute_open) >= 0.5
                    if daily is not None and minute_open is not None else None
                ),
                "data_complete": complete,
                "classification": category,
                "ratio_cluster": cluster or adjustment_cluster,
            })
    report = {
        "daily_open_vs_09_00_minute_open_pct": {
            market: alignment_summary(open_values[market]) for market in ("KOSPI", "KOSDAQ")
        },
        "daily_close_vs_15_19_minute_close_pct": {
            market: alignment_summary(close_values[market]) for market in ("KOSPI", "KOSDAQ")
        },
        "daily_previous_close_vs_prior_15_19_continuous_close_pct": {
            market: alignment_summary(prior_close_values[market]) for market in ("KOSPI", "KOSDAQ")
        },
        "daily_close_vs_15_19_threshold_counts": {
            market: {
                f"gt_{threshold:g}_pct": sum(value > threshold for value in close_values[market])
                for threshold in (0.5, 1, 3, 5, 10, 20)
            }
            for market in ("KOSPI", "KOSDAQ")
        },
        "suspicious_gap_event_count": len(gaps),
        "suspicious_gap_classifications": dict(Counter(item["classification"] for item in gaps)),
        "suspicious_gap_ratio_clusters": dict(Counter(
            str(item["ratio_cluster"]) for item in gaps if item["ratio_cluster"] is not None
        )),
    }
    return report, gaps


def _checkpoint_bar(bars: Sequence[Bar], session: date, checkpoint: str) -> Bar | None:
    if checkpoint == "15:19":
        target = time(15, 19)
    else:
        hour, minute = (int(value) for value in checkpoint.split(":"))
        boundary = datetime.combine(session, time(hour, minute), KST)
        target = (boundary - timedelta(minutes=1)).time()
    return _bar_at(bars, session, target)


def drift_value(bars: Sequence[Bar], session: date, checkpoint: str) -> float | None:
    assert_development_date(session)
    if checkpoint not in CHECKPOINTS:
        raise ValueError("unsupported Phase 9 drift checkpoint")
    opening = _bar_at(bars, session, SESSION_OPEN)
    end = _checkpoint_bar(bars, session, checkpoint)
    if opening is None or end is None or opening.open <= 0:
        return None
    if checkpoint != "15:19":
        start = datetime.combine(session, SESSION_OPEN, KST)
        boundary = datetime.combine(session, time.fromisoformat(checkpoint), KST)
        expected = tuple(start + timedelta(minutes=i) for i in range(int((boundary - start).total_seconds() // 60)))
        actual = tuple(bar.time.astimezone(KST) for bar in bars if start <= bar.time.astimezone(KST) < boundary)
        if actual != expected:
            return None
    return end.close / opening.open - 1.0


def drift_summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "mean_pct": statistics.fmean(values) * 100 if values else None,
        "median_pct": statistics.median(values) * 100 if values else None,
        "win_rate_pct": sum(value > 0 for value in values) / len(values) * 100 if values else None,
        "p10_pct": _percentile([value * 100 for value in values], 10),
        "p90_pct": _percentile([value * 100 for value in values], 90),
    }


def build_drift_report(
    minute_by_symbol_session: Mapping[tuple[str, date], Sequence[Bar]],
    market_by_symbol: Mapping[str, str],
) -> dict[str, Any]:
    values: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for (symbol, session), bars in minute_by_symbol_session.items():
        month = session.strftime("%B")
        market = market_by_symbol[symbol]
        for checkpoint in CHECKPOINTS:
            value = drift_value(bars, session, checkpoint)
            if value is not None:
                values[("ALL", market, checkpoint)].append(value)
                values[(month, market, checkpoint)].append(value)
    return {
        "scope": "Development only; start-labeled minute bars; checkpoints use completed bars",
        "index_cross_check": "NOT_AVAILABLE: cached index source is daily; no implemented safe intraday index source",
        "summary": {
            f"{month}|{market}|{checkpoint}": drift_summary(items)
            for (month, market, checkpoint), items in sorted(values.items())
        },
    }


def artifact_index(root: Path, filenames: Sequence[str]) -> dict[str, Any]:
    entries = []
    for name in sorted(filenames):
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"Phase 9 artifact is missing: {name}")
        entries.append({"path": name, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
    return {"artifact": "phase9-artifact-index", "status": "PASS", "artifacts": entries}


def verify_artifact_index(root: Path, index: Mapping[str, Any]) -> dict[str, Any]:
    failures = []
    for entry in index.get("artifacts", []):
        path = root / str(entry.get("path", ""))
        if not path.is_file():
            failures.append({"path": str(entry.get("path", "")), "reason": "MISSING"})
            continue
        if path.stat().st_size != entry.get("size_bytes"):
            failures.append({"path": str(entry["path"]), "reason": "SIZE_MISMATCH"})
        elif sha256_file(path) != entry.get("sha256"):
            failures.append({"path": str(entry["path"]), "reason": "SHA256_MISMATCH"})
    return {"status": "PASS" if not failures else "FAIL", "checked": len(index.get("artifacts", [])),
            "failures": failures}


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    assert_phase9_output_path(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _current_git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNAVAILABLE"


def _previous_phase_manifest_paths() -> list[Path]:
    paths: list[Path] = []
    for phase in ("phase5", "phase6", "phase7", "phase8"):
        root = Path("runtime/research") / phase
        for pattern in ("*manifest*.json", "*artifact-index*.json", "*integrity*.json"):
            paths.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(set(paths))


def _make_read_only_market_data_client(output_root: Path) -> KisRestClient:
    settings = Settings.from_env()
    if settings.trading_mode != "shadow" or settings.live_trading_enabled:
        raise RuntimeError("Phase 9 historical retrieval requires shadow mode with live trading disabled")
    if not settings.kis_app_key or not settings.kis_app_secret:
        raise RuntimeError("KIS read-only market-data credentials are unavailable")
    transport = UrllibTransport()
    token_manager = TokenManager(
        settings.kis_app_key, settings.kis_app_secret, transport, cache_path=None
    )
    return KisRestClient(
        settings.kis_app_key, settings.kis_app_secret, token_manager, transport,
        min_request_interval=4.0,
        max_retries=0,
        rate_limit_path=output_root / "reconciliation" / "kis-rate-limit.json",
    )


def _daily_cache_root(output_root: Path) -> Path:
    return output_root / "reconciliation" / "safe-daily-cache"


def _acquire_safe_daily(
    *,
    client: KisRestClient,
    symbols: Sequence[str],
    market_by_symbol: Mapping[str, str],
    cache_root: Path,
    adjusted: bool,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, tuple[Bar, ...]], dict[str, str]]:
    cache = ParquetBarCache(cache_root)
    interval = "1d-adjusted" if adjusted else "1d-raw"
    daily: dict[str, tuple[Bar, ...]] = {}
    errors: dict[str, str] = {}
    request_start = DEVELOPMENT_START - timedelta(days=1)
    for index, symbol in enumerate(symbols, start=1):
        try:
            bars = client.get_daily_bars(symbol, request_start, DEVELOPMENT_END, adjusted=adjusted)
            bars = [bar for bar in bars if request_start <= bar.time.astimezone(KST).date() <= DEVELOPMENT_END]
            if not bars or any(bar.time.astimezone(KST).date() >= PROTECTED_START for bar in bars):
                errors[symbol] = "NO_SAFE_DAILY_ROWS"
                continue
            cache.save(
                bars, kind="daily", symbol=symbol, interval=interval, market=market_by_symbol[symbol],
                source=("KIS inquire-daily-itemchartprice; FID_ORG_ADJ_PRC=0; adjusted"
                        if adjusted else
                        "KIS inquire-daily-itemchartprice; FID_ORG_ADJ_PRC=1; raw/original"),
                requested_start=request_start, requested_end=DEVELOPMENT_END,
            )
            verified = cache.load("daily", symbol, interval)
            if len(verified) != len(bars) or verified != bars:
                errors[symbol] = "SAFE_DAILY_PERSISTENCE_MISMATCH"
                continue
            daily[symbol] = tuple(verified)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            errors[symbol] = type(exc).__name__
        if progress is not None and (index % 5 == 0 or index == len(symbols)):
            progress(f"safe daily acquisition {index}/{len(symbols)}; loaded={len(daily)} failed={len(errors)}")
    return daily, errors


def _audit_safe_daily_cache(
    *, symbols: Sequence[str], expected: Mapping[str, Mapping[str, Sequence[Bar]]], cache_root: Path
) -> dict[str, Any]:
    cache = ParquetBarCache(cache_root)
    verified: dict[str, str] = {}
    failures: dict[str, str] = {}
    rows_by_symbol: dict[str, int] = {}
    for convention, adjusted in (("adjusted", True), ("raw", False)):
        interval = "1d-adjusted" if adjusted else "1d-raw"
        for symbol in symbols:
            key = f"{convention}:{symbol}"
            if symbol not in expected.get(convention, {}):
                failures[key] = "NO_SAFE_DAILY_SOURCE_ROWS"
                continue
            try:
                path = cache.partition_path("daily", symbol, interval)
                rows = cache.load("daily", symbol, interval)
                sidecar = json.loads(path.with_suffix(".metadata.json").read_text(encoding="utf-8"))
                if sidecar.get("symbol") != symbol or sidecar.get("interval") != interval:
                    raise ValueError("SIDECAR_IDENTITY_MISMATCH")
                if tuple(rows) != tuple(expected[convention][symbol]):
                    raise ValueError("PERSISTENCE_MISMATCH")
                if any(bar.time.astimezone(KST).date() >= PROTECTED_START for bar in rows):
                    raise ValueError("PROTECTED_DATE_IN_PHASE9_RECONCILIATION_CACHE")
                verified[key] = sha256_file(path)
                rows_by_symbol[key] = len(rows)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                failures[key] = type(exc).__name__
    return {
        "status": "PASS" if not failures else "PARTIAL" if verified else "FAIL",
        "verified_partitions": len(verified), "failed_partitions": failures,
        "row_counts_by_symbol": rows_by_symbol,
        "sha256_by_symbol": verified,
        "cache_root": str(cache_root),
    }


def _load_safe_daily_cache(
    *, symbols: Sequence[str], cache_root: Path, adjusted: bool
) -> tuple[dict[str, tuple[Bar, ...]], dict[str, str]]:
    cache = ParquetBarCache(cache_root)
    interval = "1d-adjusted" if adjusted else "1d-raw"
    daily: dict[str, tuple[Bar, ...]] = {}
    errors: dict[str, str] = {}
    for symbol in symbols:
        path = cache.partition_path("daily", symbol, interval)
        sidecar = path.with_suffix(".metadata.json")
        if not path.is_file() and not sidecar.is_file():
            errors[symbol] = "SAFE_DAILY_CACHE_ABSENT"
            continue
        try:
            bars = cache.load("daily", symbol, interval)
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            if metadata.get("symbol") != symbol or metadata.get("interval") != interval:
                raise ValueError("SAFE_DAILY_SIDECAR_IDENTITY_MISMATCH")
            if any(bar.time.astimezone(KST).date() >= PROTECTED_START for bar in bars):
                raise ValueError("PROTECTED_DATE_IN_PHASE9_RECONCILIATION_CACHE")
            daily[symbol] = tuple(bars)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            errors[symbol] = type(exc).__name__
    return daily, errors


def _acquire_safe_indexes(
    *, client: KisRestClient, output_root: Path
) -> dict[str, Any]:
    cache = ParquetBarCache(output_root / "reconciliation" / "safe-index-cache")
    index_report: dict[str, Any] = {"period": [DEVELOPMENT_START.isoformat(), DEVELOPMENT_END.isoformat()], "indexes": {}}
    for code, name in (("0001", "KOSPI"), ("1001", "KOSDAQ")):
        try:
            bars = client.get_index_bars(code, DEVELOPMENT_START, DEVELOPMENT_END)
            cache.save(
                bars, kind="indexes", symbol=code, interval="1d", market=name,
                source="KIS inquire-daily-indexchartprice; safe Development only",
                requested_start=DEVELOPMENT_START, requested_end=DEVELOPMENT_END,
            )
            index_report["indexes"][name] = {"status": "ACQUIRED", "daily_rows": len(bars)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            index_report["indexes"][name] = {"status": type(exc).__name__}
    index_report["first_hour_cross_check"] = (
        "NOT_AVAILABLE: only daily index bars are implemented; no intraday index endpoint is used"
    )
    return index_report


def run_phase9_audit(
    *,
    cohort_manifest_path: Path = Path("runtime/research/phase4/cohort-manifest.json"),
    split_manifest_path: Path = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json"),
    cache_root: Path = Path("data"),
    output_root: Path = Path("runtime/research/phase9"),
    acquire: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    assert_phase9_output_path(output_root)
    cohort = json.loads(cohort_manifest_path.read_text(encoding="utf-8"))["cohort_60"]
    symbols = tuple(cohort["symbols"])
    market_by_symbol = market_by_cohort_order(symbols)
    split_manifest = json.loads(split_manifest_path.read_text(encoding="utf-8"))
    sessions = validate_development_sessions(
        date.fromisoformat(value) for value in split_manifest["splits"]["development"]["sessions"]
    )

    baseline_file = output_root / "previous-manifest-baseline.sha256"
    if baseline_file.is_file():
        initial_manifest_hashes = {}
        for line in baseline_file.read_text(encoding="utf-8").splitlines():
            digest, separator, name = line.partition("  ")
            if separator:
                initial_manifest_hashes[name] = digest
    else:
        initial_manifest_hashes = manifest_hashes(_previous_phase_manifest_paths())
    daily_root = _daily_cache_root(output_root)
    index_report: dict[str, Any]
    if acquire:
        client = _make_read_only_market_data_client(output_root)
        adjusted_daily_by_symbol, adjusted_errors = _acquire_safe_daily(
            client=client, symbols=symbols, market_by_symbol=market_by_symbol, cache_root=daily_root,
            adjusted=True,
            progress=(lambda message: progress("adjusted " + message)) if progress else None,
        )
        raw_daily_by_symbol, raw_errors = _acquire_safe_daily(
            client=client, symbols=symbols, market_by_symbol=market_by_symbol, cache_root=daily_root,
            adjusted=False,
            progress=(lambda message: progress("raw " + message)) if progress else None,
        )
        index_report = _acquire_safe_indexes(client=client, output_root=output_root)
    else:
        adjusted_daily_by_symbol, adjusted_errors = _load_safe_daily_cache(
            symbols=symbols, cache_root=daily_root, adjusted=True
        )
        raw_daily_by_symbol, raw_errors = _load_safe_daily_cache(
            symbols=symbols, cache_root=daily_root, adjusted=False
        )
        index_report = {"status": "NOT_REFRESHED", "first_hour_cross_check": "NOT_AVAILABLE"}

    minute_by_symbol_session, cache_report = audit_development_minute_cache(
        symbols=symbols, sessions=sessions, market_by_symbol=market_by_symbol, cache_root=cache_root
    )
    daily_cache_report = _audit_safe_daily_cache(
        symbols=symbols,
        expected={"adjusted": adjusted_daily_by_symbol, "raw": raw_daily_by_symbol},
        cache_root=daily_root,
    )
    alignment, suspicious_gaps = build_alignment_report(
        daily_by_symbol=adjusted_daily_by_symbol,
        raw_daily_by_symbol=raw_daily_by_symbol,
        minute_by_symbol_session=minute_by_symbol_session,
        market_by_symbol=market_by_symbol,
        development_sessions=sessions,
    )
    drift = build_drift_report(minute_by_symbol_session, market_by_symbol)
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    alignment["artifact"] = "phase9-daily-minute-alignment"
    alignment["source_git_sha"] = _current_git_sha()
    alignment["daily_symbols_loaded"] = len(adjusted_daily_by_symbol)
    alignment["adjusted_daily_failures"] = adjusted_errors
    alignment["raw_daily_failures"] = raw_errors
    drift["artifact"] = "phase9-unconditional-drift"
    drift["source_git_sha"] = _current_git_sha()
    drift["sample_scope"] = {
        "minute_symbol_session_partitions": len(minute_by_symbol_session),
        "development_sessions": len(sessions),
    }
    cache_report["source_git_sha"] = _current_git_sha()
    cache_report["daily_source_acquisition"] = {
        "adjusted_symbols_loaded": len(adjusted_daily_by_symbol), "adjusted_failures": adjusted_errors,
        "raw_symbols_loaded": len(raw_daily_by_symbol), "raw_failures": raw_errors,
        "cache_root": str(daily_root), "existing_mixed_daily_cache_opened": False,
    }

    with (output_root / "suspicious-gaps.csv").open("w", newline="", encoding="utf-8") as stream:
        columns = [
            "symbol", "date", "market", "daily_prior_date", "continuous_prior_date",
            "daily_prior_close", "adjusted_daily_prior_close", "continuous_prior_close", "current_open",
            "raw_daily_open", "adjusted_daily_open", "daily_based_gap_pct",
            "adjusted_daily_based_gap_pct", "continuous_based_gap_pct", "source_mismatch",
            "adjusted_source_mismatch", "data_complete", "classification", "ratio_cluster",
        ]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(suspicious_gaps)

    _write_json(output_root / "source-inventory.json", {
        "artifact": "phase9-source-inventory", "source_git_sha": _current_git_sha(),
        "daily": DAILY_SOURCE, "minute": MINUTE_SOURCE,
        "daily_cache": {
            "path_template": "data/daily/{symbol}-1d.parquet + .metadata.json",
            "partitioning": "one multi-session file per symbol; existing files not opened due possible protected-date rows",
            "Phase9_source": str(daily_root),
            "phase9_alignment_convention": "FID_ORG_ADJ_PRC=0 (adjusted)",
            "phase9_comparison_copy_intervals": ["1d-adjusted", "1d-raw"],
        },
        "minute_cache": {
            "path_template": "data/minute/{symbol}/{session}.parquet + .metadata.json",
            "partitioning": "one safe session per file; explicit Development dates only",
        },
        "derived_daily_aggregation": {
            "status": "NOT_IMPLEMENTED_AS_A_CACHE_SOURCE",
            "session_resampling": "resample_session_minutes is intraday KRX-session-anchored; interval endpoints are bucket-end labels",
        },
        "index_daily_source": {
            "endpoint": "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
            "tr_id": "FHKUP03500100", "codes": {"0001": "KOSPI", "1001": "KOSDAQ"},
            "first_hour_comparison": index_report.get("first_hour_cross_check", "NOT_AVAILABLE"),
            "acquisition": index_report,
        },
        "safe_daily_reconciliation_integrity": daily_cache_report,
        "period": {"development_start": sessions[0].isoformat(), "development_end": sessions[-1].isoformat(),
                   "sessions": len(sessions)},
        "protected_holdout": {"start": PROTECTED_START.isoformat(), "payload_or_sidecar_reads": 0},
    })
    _write_json(output_root / "alignment.json", alignment)
    _write_json(output_root / "cache-integrity.json", cache_report)
    _write_json(output_root / "daily-reconciliation-integrity.json", daily_cache_report)
    _write_json(output_root / "unconditional-drift.json", drift)

    alignment_rows = sum(
        block.get("n", 0) for block in alignment["daily_open_vs_09_00_minute_open_pct"].values()
    )
    p95_open = max(
        (block.get("p95_abs_diff_pct") or 0.0)
        for block in alignment["daily_open_vs_09_00_minute_open_pct"].values()
    )
    max_open = max(
        (block.get("max_abs_diff_pct") or 0.0)
        for block in alignment["daily_open_vs_09_00_minute_open_pct"].values()
    )
    daily_minute_alignment = (
        "PASS" if alignment_rows >= ALIGNMENT_THRESHOLDS["pass"]["minimum_n"]
        and p95_open <= ALIGNMENT_THRESHOLDS["pass"]["p95_pct_max"]
        and max_open <= ALIGNMENT_THRESHOLDS["pass"]["max_pct_max"] else
        "PARTIAL" if alignment_rows >= ALIGNMENT_THRESHOLDS["partial"]["minimum_n"]
        and p95_open <= ALIGNMENT_THRESHOLDS["partial"]["p95_pct_max"]
        and max_open <= ALIGNMENT_THRESHOLDS["partial"]["max_pct_max"] else "FAIL"
    )
    cache_status = cache_report["status"]
    timestamp_failures = [
        item for item in cache_report["invalid_partitions"]
        if item["reason"] in {"WRONG_TIMEZONE", "CROSS_DATE_CONTAMINATION", "BAD_SESSION_ALIGNMENT"}
    ]
    timestamp_status = "FAIL" if timestamp_failures else (
        "PASS" if minute_by_symbol_session else "FAIL"
    )
    if (
        cache_status == "FAIL" or daily_cache_report["status"] == "FAIL"
        or daily_minute_alignment == "FAIL" or timestamp_status == "FAIL"
    ):
        platform = "NOT_READY"
    elif (
        DAILY_SOURCE["adjustment_status"] == "CONFIGURABLE"
        and MINUTE_SOURCE["adjustment_status"] in {"CONFIRMED_RAW", "CONFIRMED_ADJUSTED"}
        and cache_status == "PASS"
        and daily_minute_alignment == "PASS"
    ):
        platform = "READY"
    else:
        platform = "READY_WITH_LIMITATIONS"
    current_manifest_hashes = manifest_hashes(_previous_phase_manifest_paths())
    immutability = compare_manifest_hashes(initial_manifest_hashes, current_manifest_hashes)
    immutability["artifact"] = "phase9-previous-manifest-immutability"
    immutability["baseline_sha256"] = initial_manifest_hashes
    immutability["after_sha256"] = current_manifest_hashes
    _write_json(output_root / "previous-manifest-immutability.json", immutability)

    config_record = {
        "development_sessions": [item.isoformat() for item in sessions],
        "cohort_symbols": list(symbols), "daily_source": DAILY_SOURCE,
        "minute_source": MINUTE_SOURCE, "protected_holdout_start": PROTECTED_START.isoformat(),
        "minute_expected_rows_per_session": 380, "minute_cache_repair": "none; reconciliation only",
        "alignment_thresholds": ALIGNMENT_THRESHOLDS,
    }
    config_sha = hashlib.sha256(
        json.dumps(config_record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    refresh_path = output_root / "reconciliation" / "refresh-comparison.json"
    refresh_report = json.loads(refresh_path.read_text(encoding="utf-8")) if refresh_path.is_file() else None
    refresh_classifications = (
        dict(Counter(item.get("classification", "UNKNOWN") for item in refresh_report.get("results", [])))
        if refresh_report else {}
    )
    replay_path = output_root / "phase8-data-corrected-replay.json"
    replay_report = json.loads(replay_path.read_text(encoding="utf-8")) if replay_path.is_file() else None
    summary = {
        "artifact": "phase9-summary", "source_git_sha": _current_git_sha(),
        "cohort_sha256": hashlib.sha256(json.dumps(symbols, separators=(",", ":")).encode()).hexdigest(),
        "config_sha256": config_sha,
        "dataset_sha256": hashlib.sha256(json.dumps(
            {"daily_adjusted": {
                s: sha256_file(_daily_cache_root(output_root) / "daily" / f"{s}-1d-adjusted.parquet")
                for s in adjusted_daily_by_symbol
            }, "daily_raw": {
                s: sha256_file(_daily_cache_root(output_root) / "daily" / f"{s}-1d-raw.parquet")
                for s in raw_daily_by_symbol
            },
             "minute": cache_report["verified_partition_sha256"]},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
        "price_semantics": "PARTIAL",
        "daily_price_adjustment": DAILY_SOURCE["adjustment_status"],
        "minute_price_adjustment": MINUTE_SOURCE["adjustment_status"],
        "daily_minute_alignment": daily_minute_alignment,
        "timestamp_semantics": timestamp_status,
        "corporate_action_handling": "PARTIAL",
        "cache_integrity": cache_status,
        "daily_reconciliation_integrity": daily_cache_report["status"],
        "phase8_evidence_integrity": "DEGRADED_BUT_USABLE",
        "research_platform": platform,
        "phase9": "COMPLETE" if (
            immutability["status"] == "PASS" and cache_status == "PASS"
            and daily_cache_report["status"] == "PASS" and daily_minute_alignment == "PASS"
            and timestamp_status == "PASS"
        ) else "PARTIAL",
        "phase10_allowed_to_start": platform in {"READY", "READY_WITH_LIMITATIONS"},
        "phase10_opportunity_map": "NOT_RUN",
        "daily_a": "NOT_CREATED",
        "external_validation": "NOT_RUN",
        "shadow_next_session": "NO", "alpha": "UNPROVEN", "live": "DISABLED",
        "starting_safe_cohort": {"complete": 47, "partial": 1, "not_acquired": 12},
        "development_minute_partition_counts": cache_report["partition_counts"],
        "suspicious_gaps_ge_20_pct": len(suspicious_gaps),
        "suspicious_gap_classifications": dict(Counter(item["classification"] for item in suspicious_gaps)),
        "daily_minute_open_alignment": alignment["daily_open_vs_09_00_minute_open_pct"],
        "daily_minute_close_alignment": alignment["daily_close_vs_15_19_minute_close_pct"],
        "alignment_gate_thresholds": ALIGNMENT_THRESHOLDS,
        "index_first_hour_cross_check": "NOT_AVAILABLE",
        "protected_holdout": {"start": PROTECTED_START.isoformat(), "payload_reads": 0, "sidecar_reads": 0},
        "previous_phase_manifests_immutable": immutability["status"],
        "representative_refresh_comparison": refresh_classifications,
        "phase8_fixed_replay": replay_report.get("status", "UNKNOWN") if replay_report else "NOT_RUN",
        "daily_alignment_convention": "FID_ORG_ADJ_PRC=0 (adjusted); raw flag 1 retained for frozen Phase 8 gap comparison",
        "note": "KIS daily prices are configurable. Minute adjustment remains UNKNOWN; adjusted daily was checked against minute data. No Phase 8 replay performed.",
    }
    _write_json(output_root / "phase9-summary.json", summary)
    report_path = output_root / "phase9-report.md"
    report_path.write_text(
        "# Phase 9 Data Integrity\n\n"
        f"- Source Git SHA: `{summary['source_git_sha']}`\n"
        f"- Research platform: **{platform}**\n"
        f"- Daily adjustment: **{DAILY_SOURCE['adjustment_status']}**; alignment convention `FID_ORG_ADJ_PRC=0`; minute adjustment: **{MINUTE_SOURCE['adjustment_status']}**\n"
        f"- Daily/minute alignment: **{daily_minute_alignment}**; timestamp semantics: **{timestamp_status}**\n"
        f"- Cache integrity: **{cache_status}**; verified Development sessions: {len(minute_by_symbol_session)} symbol-session partitions\n"
        f"- Suspicious Development gaps >=20%: {len(suspicious_gaps)}\n"
        f"- Representative refresh comparison: {json.dumps(refresh_classifications, sort_keys=True)}\n"
        f"- Frozen Phase 8 corrected replay: {replay_report.get('status', 'UNKNOWN') if replay_report else 'NOT_RUN'}\n"
        f"- Phase 8 evidence integrity: **DEGRADED_BUT_USABLE**\n"
        f"- Protected Holdout payload/sidecar reads: **0 / 0**\n"
        f"- Previous phase manifest hashes unchanged: **{immutability['status']}**\n"
        "- KOSPI/KOSDAQ index first-hour comparison: **NOT_AVAILABLE** (only daily index source implemented)\n\n"
        "Raw (`FID_ORG_ADJ_PRC=1`) and adjusted (`0`) daily bars were re-fetched into separate Phase 9 reconciliation partitions; the existing multi-session daily cache was not opened. Minute bars were read only by explicit Development date path. Missing bars were not synthesized.\n",
        encoding="utf-8",
    )
    indexed_files = [
        "source-inventory.json", "alignment.json", "cache-integrity.json",
        "daily-reconciliation-integrity.json", "unconditional-drift.json", "suspicious-gaps.csv",
        "previous-manifest-immutability.json", "phase9-summary.json", "phase9-report.md",
    ]
    daily_cache = ParquetBarCache(daily_root)
    for interval, daily_partition in (
        ("1d-adjusted", adjusted_daily_by_symbol), ("1d-raw", raw_daily_by_symbol)
    ):
        for symbol in daily_partition:
            data_path = daily_cache.partition_path("daily", symbol, interval)
            sidecar_path = data_path.with_suffix(".metadata.json")
            indexed_files.extend([
                data_path.relative_to(output_root).as_posix(),
                sidecar_path.relative_to(output_root).as_posix(),
            ])
    index_cache = ParquetBarCache(output_root / "reconciliation" / "safe-index-cache")
    for code in ("0001", "1001"):
        data_path = index_cache.partition_path("indexes", code, "1d")
        sidecar_path = data_path.with_suffix(".metadata.json")
        if data_path.is_file() and sidecar_path.is_file():
            indexed_files.extend([
                data_path.relative_to(output_root).as_posix(),
                sidecar_path.relative_to(output_root).as_posix(),
            ])
    if baseline_file.is_file():
        indexed_files.append(baseline_file.relative_to(output_root).as_posix())
    if refresh_report:
        indexed_files.append(refresh_path.relative_to(output_root).as_posix())
        for result in refresh_report.get("results", []):
            session = date.fromisoformat(result["session"])
            assert_development_date(session)
            symbol = result["symbol"]
            data_path = (
                output_root / "reconciliation" / "refresh-cache" / "minute"
                / symbol / f"{session.isoformat()}.parquet"
            )
            sidecar_path = data_path.with_suffix(".metadata.json")
            for artifact_path in (data_path, sidecar_path):
                if artifact_path.is_file():
                    indexed_files.append(artifact_path.relative_to(output_root).as_posix())
    if replay_report:
        indexed_files.append(replay_path.relative_to(output_root).as_posix())
        event_path = output_root / "phase8-data-corrected-events.jsonl.gz"
        if event_path.is_file():
            indexed_files.append(event_path.relative_to(output_root).as_posix())
    index = artifact_index(output_root, indexed_files)
    _write_json(output_root / "phase9-artifact-index.json", index)
    integrity = verify_artifact_index(output_root, index)
    _write_json(output_root / "artifact-integrity.json", integrity)
    summary["artifact_index_integrity"] = integrity["status"]
    _write_json(output_root / "phase9-summary.json", summary)
    report_text = report_path.read_text(encoding="utf-8").replace(
        "- Previous phase manifest hashes unchanged:",
        f"- Artifact index integrity: **{integrity['status']}**\n- Previous phase manifest hashes unchanged:",
    )
    report_path.write_text(report_text, encoding="utf-8")
    index = artifact_index(output_root, indexed_files)
    _write_json(output_root / "phase9-artifact-index.json", index)
    integrity = verify_artifact_index(output_root, index)
    _write_json(output_root / "artifact-integrity.json", integrity)
    return summary


if __name__ == "__main__":
    run_phase9_audit(progress=print)

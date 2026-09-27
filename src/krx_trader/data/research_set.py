from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.quality import inspect_bars
from krx_trader.kis.rest import KisRestClient

KST = ZoneInfo("Asia/Seoul")

def build_research_set(
    client: KisRestClient,
    *,
    symbols: list[str],
    start: date,
    end: date,
    max_sessions: int,
    cache: ParquetBarCache | None = None,
    report_path: Path = Path("runtime/research/latest_dataset.json"),
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Download actual KIS OHLCV into resumable, per-symbol/session Parquet partitions."""
    if start > end or not 1 <= max_sessions <= 30:
        raise ValueError("invalid date range or max_sessions (1..30)")
    unique_symbols = list(dict.fromkeys(symbols))
    if not unique_symbols or any(len(symbol) != 6 or not symbol.isdigit() for symbol in unique_symbols):
        raise ValueError("symbols must be a non-empty list of six-digit KRX codes")
    cache = cache or ParquetBarCache()
    succeeded: set[str] = set()
    failed: dict[str, str] = {}
    rows = 0
    duplicates = 0
    dq_errors = 0
    first: str | None = None
    last: str | None = None
    completed_sessions: dict[str, int] = {}

    for symbol in unique_symbols:
        try:
            daily_path = cache.partition_path("daily", symbol, "1d")
            daily_meta_path = daily_path.with_suffix(".metadata.json")
            daily_bars: list
            if daily_path.is_file() and daily_meta_path.is_file():
                metadata = json.loads(daily_meta_path.read_text(encoding="utf-8"))
                requested_range_matches = (
                    metadata.get("requested_start") == start.isoformat()
                    and metadata.get("requested_end") == end.isoformat()
                )
                daily_bars = cache.load("daily", symbol, "1d") if requested_range_matches else []
            else:
                daily_bars = []
            if not daily_bars:
                daily_bars = client.get_daily_bars(symbol, start, end)
                if not daily_bars:
                    failed[symbol] = "NO_DAILY_BARS_IN_RANGE"
                    continue
                cache.save(
                    daily_bars,
                    kind="daily",
                    symbol=symbol,
                    interval="1d",
                    market="KRX",
                    source="KIS daily OHLCV",
                    requested_start=start,
                    requested_end=end,
                )
            sessions = sorted({bar.time.date() for bar in daily_bars})[-max_sessions:]
            symbol_rows = 0
            symbol_errors = 0
            symbol_completed_sessions = 0
            symbol_failed_sessions = 0
            for session in sessions:
                if cache.contains("minute", symbol, "1m", session):
                    try:
                        bars = cache.load("minute", symbol, "1m", session)
                    except (OSError, ValueError, json.JSONDecodeError):
                        failed[f"{symbol}:{session.isoformat()}"] = "INVALID_CACHED_PARTITION"
                        symbol_failed_sessions += 1
                        continue
                else:
                    try:
                        bars = client.get_minute_bars(symbol, session)
                        if not bars:
                            raise ValueError("empty completed-bar response")
                        cache.save(
                            bars,
                            kind="minute",
                            symbol=symbol,
                            interval="1m",
                            market="KRX",
                            source="KIS regular-session minute OHLCV; timestamp=bar_start; auction excluded",
                            session_date=session,
                        )
                    except (OSError, ValueError, RuntimeError) as exc:
                        failed[f"{symbol}:{session.isoformat()}"] = type(exc).__name__
                        symbol_failed_sessions += 1
                        continue
                issues = inspect_bars(bars)
                if issues:
                    dq_errors += len(issues)
                    symbol_errors += len(issues)
                    symbol_failed_sessions += 1
                    failed[f"{symbol}:{session.isoformat()}"] = "DATA_QUALITY_FAILURE"
                    continue
                duplicates += len(bars) - len({bar.time for bar in bars})
                rows += len(bars)
                symbol_rows += len(bars)
                symbol_completed_sessions += 1
                if bars:
                    first = min(first, bars[0].time.isoformat()) if first else bars[0].time.isoformat()
                    last = max(last, bars[-1].time.isoformat()) if last else bars[-1].time.isoformat()
            completed_sessions[symbol] = symbol_completed_sessions
            if symbol_completed_sessions == len(sessions) and not symbol_errors and not symbol_failed_sessions:
                succeeded.add(symbol)
            elif symbol not in failed:
                failed[symbol] = "NO_VALID_MINUTE_BARS"
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            failed[symbol] = type(exc).__name__
        if progress is not None:
            progress(f"{symbol}: {completed_sessions.get(symbol, 0)} sessions cached; {rows} minute rows total")

    summary: dict[str, object] = {
        "source": "KIS official read-only REST",
        "requested_at": datetime.now(KST).isoformat(),
        "symbols_requested": len(unique_symbols),
        "symbols_succeeded": len(succeeded),
        "symbols_failed": len(unique_symbols) - len(succeeded),
        "succeeded_symbols": sorted(succeeded),
        "failed": failed,
        "session_limit_per_symbol": max_sessions,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "completed_sessions_by_symbol": completed_sessions,
        "minute_rows": rows,
        "first_timestamp": first,
        "last_timestamp": last,
        "duplicate_rows": duplicates,
        "data_quality_errors": dq_errors,
        "cache_root": str(cache.root),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary

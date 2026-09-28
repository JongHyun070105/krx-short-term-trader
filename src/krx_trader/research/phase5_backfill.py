from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.quality import require_healthy_bars
from krx_trader.kis.auth import TokenManager
from krx_trader.kis.rest import KisRestClient
from krx_trader.kis.transport import UrllibTransport

KST = ZoneInfo("Asia/Seoul")
LOCKED_HOLDOUT_START = date(2026, 7, 28)
DEVELOPMENT_START = date(2026, 4, 17)
DEVELOPMENT_END = date(2026, 6, 30)
VALIDATION_START = date(2026, 7, 1)
VALIDATION_END = date(2026, 7, 27)


def assert_safe_research_date(session_date: date) -> None:
    if session_date >= LOCKED_HOLDOUT_START:
        raise ValueError(
            f"LOCKED HOLDOUT GUARD TRIGGERED: Refusing access to session {session_date.isoformat()} "
            f"(Holdout boundary is {LOCKED_HOLDOUT_START.isoformat()})"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_used_period_sessions(manifest_path: Path) -> list[date]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    splits = manifest.get("splits", {})
    dev_sessions = [date.fromisoformat(d) for d in splits.get("development", {}).get("sessions", [])]
    val_sessions = [date.fromisoformat(d) for d in splits.get("validation", {}).get("sessions", [])]
    combined = sorted(set(dev_sessions + val_sessions))
    for d in combined:
        assert_safe_research_date(d)
    return combined


def backfill_symbol_sessions(
    client: KisRestClient,
    symbol: str,
    sessions: list[date],
    cache: ParquetBarCache,
    *,
    market: str = "KOSDAQ",
    on_progress: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    for session in sessions:
        assert_safe_research_date(session)

    # 1. Ensure daily bars are cached for the entire period
    start_date = sessions[0]
    end_date = sessions[-1]
    daily_path = cache.partition_path("daily", symbol, "1d")
    daily_meta_path = daily_path.with_suffix(".metadata.json")
    daily_bars = []
    if daily_path.is_file() and daily_meta_path.is_file():
        try:
            daily_bars = cache.load("daily", symbol, "1d")
        except (OSError, ValueError, json.JSONDecodeError):
            daily_bars = []

    if not daily_bars:
        try:
            daily_bars = client.get_daily_bars(symbol, start_date, end_date)
            if daily_bars:
                cache.save(
                    daily_bars,
                    kind="daily",
                    symbol=symbol,
                    interval="1d",
                    market=market,
                    source="KIS daily OHLCV",
                    requested_start=start_date,
                    requested_end=end_date,
                )
        except (OSError, ValueError, RuntimeError) as exc:
            return {
                "symbol": symbol,
                "status": "DAILY_FETCH_FAILED",
                "error": type(exc).__name__,
                "succeeded_sessions": 0,
                "failed_sessions": len(sessions),
            }

    # 2. Iterate minute sessions
    succeeded = 0
    skipped_existing = 0
    failed: dict[str, str] = {}
    total_minute_rows = 0

    for session in sessions:
        assert_safe_research_date(session)
        day_str = session.isoformat()
        if cache.contains("minute", symbol, "1m", session):
            try:
                cached_bars = cache.load("minute", symbol, "1m", session)
                skipped_existing += 1
                succeeded += 1
                total_minute_rows += len(cached_bars)
                continue
            except (OSError, ValueError, json.JSONDecodeError):
                cached_bars = []

        try:
            bars = client.get_minute_bars(symbol, session)
            if not bars:
                failed[day_str] = "EMPTY_MINUTE_RESPONSE"
                continue
            require_healthy_bars(bars)
            cache.save(
                bars,
                kind="minute",
                symbol=symbol,
                interval="1m",
                market=market,
                source="KIS regular-session minute OHLCV; timestamp=bar_start; auction excluded",
                session_date=session,
            )
            succeeded += 1
            total_minute_rows += len(bars)
        except (OSError, ValueError, RuntimeError) as exc:
            failed[day_str] = f"{type(exc).__name__}: {exc}"

        if on_progress:
            on_progress({
                "symbol": symbol,
                "session": day_str,
                "succeeded": succeeded,
                "failed": len(failed),
                "skipped_existing": skipped_existing,
            })

    return {
        "symbol": symbol,
        "status": "SUCCESS" if not failed else "PARTIAL",
        "succeeded_sessions": succeeded,
        "failed_sessions": len(failed),
        "skipped_existing": skipped_existing,
        "failures": failed,
        "total_minute_rows": total_minute_rows,
    }


def run_phase5_backfill(
    *,
    cohort_manifest_path: Path = Path("runtime/research/phase4/cohort-manifest.json"),
    split_manifest_path: Path = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json"),
    cache_root: Path = Path("data"),
    status_output_path: Path = Path("runtime/research/phase5/data-acquisition-manifest.json"),
    target_cohort: str = "cohort_60",
    min_request_interval: float = 0.4,
) -> dict[str, object]:
    cohort = json.loads(cohort_manifest_path.read_text(encoding="utf-8"))
    symbols = list(cohort[target_cohort]["symbols"])
    sessions = get_used_period_sessions(split_manifest_path)

    for session in sessions:
        assert_safe_research_date(session)

    cache = ParquetBarCache(cache_root)
    settings = Settings.from_env()
    transport = UrllibTransport()
    manager = TokenManager(settings.kis_app_key, settings.kis_app_secret, transport)
    client = KisRestClient(
        settings.kis_app_key,
        settings.kis_app_secret,
        manager,
        transport,
        min_request_interval=min_request_interval,
    )

    status_output_path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {
        "schema_version": 1,
        "artifact": "phase5-data-acquisition-manifest",
        "created_at": datetime.now(KST).isoformat(),
        "target_cohort": target_cohort,
        "total_symbols": len(symbols),
        "total_sessions": len(sessions),
        "total_target_partitions": len(symbols) * len(sessions),
        "holdout_integrity": {
            "locked_holdout_start": LOCKED_HOLDOUT_START.isoformat(),
            "state": "LOCKED_NOT_EVALUATED",
            "holdout_dates_requested": 0,
        },
        "period": [sessions[0].isoformat(), sessions[-1].isoformat()],
        "symbol_results": {},
        "status": "RUNNING",
    }

    print(
        f"Starting Phase 5 Backfill: {target_cohort} ({len(symbols)} symbols) x {len(sessions)} sessions "
        f"({len(symbols) * len(sessions)} partitions)",
        flush=True,
    )

    completed_partitions = 0
    start_time = time.time()

    for idx, symbol in enumerate(symbols, 1):
        market = "KOSPI" if idx <= 30 else "KOSDAQ"
        res = backfill_symbol_sessions(
            client,
            symbol,
            sessions,
            cache,
            market=market,
        )
        state["symbol_results"][symbol] = res
        completed_partitions += int(res["succeeded_sessions"])
        elapsed = time.time() - start_time
        rate = completed_partitions / max(0.1, elapsed)

        print(
            f"[{idx}/{len(symbols)}] {symbol} ({market}): {res['succeeded_sessions']}/{len(sessions)} sessions "
            f"(skipped {res['skipped_existing']}), rate={rate:.1f} part/s",
            flush=True,
        )

        tmp_status = status_output_path.with_suffix(".tmp")
        tmp_status.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp_status, status_output_path)

    state["finished_at"] = datetime.now(KST).isoformat()
    all_succeeded = all(
        res.get("status") == "SUCCESS" for res in state["symbol_results"].values()
    )
    state["status"] = "COMPLETE" if all_succeeded else "PARTIAL"
    tmp_status = status_output_path.with_suffix(".tmp")
    tmp_status.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_status, status_output_path)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 5 Data Acquisition Backfill")
    parser.add_argument("--cohort", default="cohort_60", choices=["cohort_60", "cohort_100"])
    parser.add_argument("--interval", type=float, default=0.4)
    args = parser.parse_args()
    res = run_phase5_backfill(target_cohort=args.cohort, min_request_interval=args.interval)
    print(f"Backfill finished with status: {res.get('status')}")

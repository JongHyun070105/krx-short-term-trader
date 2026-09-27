from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from krx_trader.data.quality import KST, require_healthy_bars
from krx_trader.models import Bar


def resample_session_minutes(bars: list[Bar], interval_minutes: int) -> list[Bar]:
    """Resample complete 1-minute bars into KRX-session anchored bars.

    Input timestamps are candle start times. Output timestamps are bucket end times,
    e.g. the 09:00-09:14 bucket is timestamped 09:15 Asia/Seoul.
    Incomplete buckets are omitted so the open candle cannot produce a signal.
    """
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    require_healthy_bars(bars)
    buckets: dict[tuple[object, int], list[Bar]] = defaultdict(list)
    for bar in bars:
        local = bar.time.astimezone(KST)
        if local.hour < 9 or local.hour > 15 or (local.hour == 15 and local.minute >= 30):
            continue
        session_open = local.replace(hour=9, minute=0, second=0, microsecond=0)
        offset = int((local - session_open).total_seconds() // 60)
        if offset < 0:
            continue
        buckets[(session_open.date(), offset // interval_minutes)].append(
            Bar(local, bar.open, bar.high, bar.low, bar.close, bar.volume)
        )

    result: list[Bar] = []
    for (session_date, bucket_index), values in sorted(buckets.items()):
        values.sort(key=lambda item: item.time)
        expected_start = values[0].time.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(
            minutes=bucket_index * interval_minutes
        )
        if len(values) != interval_minutes:
            continue
        expected_times = [expected_start + timedelta(minutes=index) for index in range(interval_minutes)]
        if [bar.time for bar in values] != expected_times:
            continue
        result.append(
            Bar(
                time=expected_start + timedelta(minutes=interval_minutes),
                open=values[0].open,
                high=max(bar.high for bar in values),
                low=min(bar.low for bar in values),
                close=values[-1].close,
                volume=sum(bar.volume for bar in values),
            )
        )
    return result

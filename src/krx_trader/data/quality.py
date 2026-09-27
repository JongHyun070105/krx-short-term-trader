from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from krx_trader.models import Bar

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: str
    index: int | None


class DataQualityError(ValueError):
    def __init__(self, issues: list[QualityIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("critical market-data quality checks failed: " + ", ".join(i.code for i in issues))


def inspect_bars(bars: list[Bar]) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    seen: set[datetime] = set()
    previous: datetime | None = None
    for index, bar in enumerate(bars):
        if bar.time.tzinfo is None or bar.time.utcoffset() is None:
            issues.append(QualityIssue("NAIVE_TIMESTAMP", index))
        if bar.time in seen:
            issues.append(QualityIssue("DUPLICATE_TIMESTAMP", index))
        seen.add(bar.time)
        if previous is not None and bar.time < previous:
            issues.append(QualityIssue("OUT_OF_ORDER", index))
        previous = bar.time
        values = (bar.open, bar.high, bar.low, bar.close)
        if any(value is None for value in values):
            issues.append(QualityIssue("NULL_OHLC", index))
            continue
        if any(value <= 0 for value in values):
            issues.append(QualityIssue("NON_POSITIVE_PRICE", index))
        if bar.volume < 0:
            issues.append(QualityIssue("NEGATIVE_VOLUME", index))
        if bar.high < bar.low:
            issues.append(QualityIssue("HIGH_BELOW_LOW", index))
        if bar.open < bar.low or bar.open > bar.high:
            issues.append(QualityIssue("OPEN_OUTSIDE_RANGE", index))
        if bar.close < bar.low or bar.close > bar.high:
            issues.append(QualityIssue("CLOSE_OUTSIDE_RANGE", index))
    return issues


def require_healthy_bars(bars: list[Bar]) -> None:
    issues = inspect_bars(bars)
    if issues:
        raise DataQualityError(issues)


def is_stale(timestamp: datetime, *, now: datetime | None = None, max_age: timedelta = timedelta(seconds=30)) -> bool:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return True
    reference = (now or datetime.now(KST)).astimezone(KST)
    observed = timestamp.astimezone(KST)
    return observed > reference + timedelta(seconds=5) or reference - observed > max_age

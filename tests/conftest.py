from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from krx_trader.models import Bar

KST = ZoneInfo("Asia/Seoul")


def make_bar(minute: int, *, day: int = 2, hour: int = 9, open_: float = 100, high: float | None = None,
             low: float | None = None, close: float | None = None, volume: int = 100) -> Bar:
    return Bar(
        datetime(2026, 1, day, hour, 0, tzinfo=KST) + timedelta(minutes=minute),
        open_, high if high is not None else open_ + 2, low if low is not None else open_ - 2,
        close if close is not None else open_ + 1, volume,
    )

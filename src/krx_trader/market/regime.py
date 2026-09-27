from __future__ import annotations

from enum import StrEnum
from math import log, sqrt

from krx_trader.data.quality import require_healthy_bars
from krx_trader.models import Bar


class Regime(StrEnum):
    UP = "UP"
    NEUTRAL = "NEUTRAL"
    DOWN = "DOWN"
    HIGH_VOL = "HIGH_VOL"


def classify_regime(
    index_bars: list[Bar], *, trend_lookback: int = 20, volatility_lookback: int = 20, high_vol_threshold: float = 0.025
) -> Regime | None:
    """Classify an index series as a conservative long-entry risk filter."""
    if trend_lookback < 2 or volatility_lookback < 2 or high_vol_threshold <= 0:
        raise ValueError("regime parameters must be positive and lookbacks >= 2")
    require_healthy_bars(index_bars)
    closes = [bar.close for bar in index_bars]
    needed = max(trend_lookback, volatility_lookback + 1)
    if len(closes) < needed or any(value <= 0 for value in closes[-needed:]):
        return None
    returns = [log(closes[index] / closes[index - 1]) for index in range(len(closes) - volatility_lookback, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)
    annualized_window_vol = sqrt(variance) * sqrt(volatility_lookback)
    if annualized_window_vol >= high_vol_threshold:
        return Regime.HIGH_VOL
    if closes[-1] > closes[-trend_lookback]:
        return Regime.UP
    if closes[-1] < closes[-trend_lookback]:
        return Regime.DOWN
    return Regime.NEUTRAL

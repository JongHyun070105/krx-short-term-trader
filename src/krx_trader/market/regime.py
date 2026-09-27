from __future__ import annotations

from enum import StrEnum
from math import isfinite, log, sqrt

from krx_trader.data.quality import require_healthy_bars
from krx_trader.models import Bar


class Regime(StrEnum):
    UP = "UP"
    NEUTRAL = "NEUTRAL"
    DOWN = "DOWN"
    HIGH_VOL = "HIGH_VOL"


def aggregate_window_volatility(daily_log_returns: list[float]) -> float:
    """Return sample daily log-return volatility scaled to this N-session window."""
    if len(daily_log_returns) < 2 or any(not isfinite(value) for value in daily_log_returns):
        raise ValueError("window volatility requires at least two finite daily returns")
    mean = sum(daily_log_returns) / len(daily_log_returns)
    variance = sum((value - mean) ** 2 for value in daily_log_returns) / (len(daily_log_returns) - 1)
    return sqrt(variance) * sqrt(len(daily_log_returns))


def classify_regime(
    index_bars: list[Bar], *, trend_lookback: int = 20, volatility_lookback: int = 20, high_vol_threshold: float = 0.025
) -> Regime | None:
    """Classify an index using prior sessions and N-session aggregate volatility.

    ``high_vol_threshold`` is a decimal fraction of the N-session window volatility,
    not an annualized volatility threshold.
    """
    if trend_lookback < 2 or volatility_lookback < 2 or high_vol_threshold <= 0:
        raise ValueError("regime parameters must be positive and lookbacks >= 2")
    require_healthy_bars(index_bars)
    closes = [bar.close for bar in index_bars]
    needed = max(trend_lookback, volatility_lookback + 1)
    if len(closes) < needed or any(value <= 0 for value in closes[-needed:]):
        return None
    returns = [log(closes[index] / closes[index - 1]) for index in range(len(closes) - volatility_lookback, len(closes))]
    window_volatility = aggregate_window_volatility(returns)
    if window_volatility >= high_vol_threshold:
        return Regime.HIGH_VOL
    if closes[-1] > closes[-trend_lookback]:
        return Regime.UP
    if closes[-1] < closes[-trend_lookback]:
        return Regime.DOWN
    return Regime.NEUTRAL

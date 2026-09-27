from __future__ import annotations

from dataclasses import dataclass

from krx_trader.market.regime import Regime
from krx_trader.models import Bar, Decision, Signal


@dataclass(frozen=True, slots=True)
class PullbackConfig:
    impulse_lookback: int = 5
    impulse_volume_multiple: float = 1.2
    max_holding_bars: int = 10

    def __post_init__(self) -> None:
        if self.impulse_lookback < 2 or self.impulse_volume_multiple <= 0 or self.max_holding_bars < 1:
            raise ValueError("pullback configuration must be positive")


DEFAULT_PULLBACK_CONFIG = PullbackConfig()


def evaluate_pullback(
    bars: list[Bar], symbol: str, *, regime: Regime | None = None, config: PullbackConfig = DEFAULT_PULLBACK_CONFIG
) -> Signal:
    if not bars:
        raise ValueError("at least one completed bar is required")
    last = bars[-1]
    base = {"timestamp": last.time, "symbol": symbol, "strategy_id": "pullback_rebreak"}
    if regime is None:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("REGIME_UNAVAILABLE",))
    if regime in {Regime.DOWN, Regime.HIGH_VOL}:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("REGIME_BLOCK",))
    needed = config.impulse_lookback + 3
    if len(bars) < needed:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("INSUFFICIENT_COMPLETED_BARS",))
    prior = bars[-needed:-3]
    impulse, pullback, rebreak = bars[-3:]
    level = max(bar.high for bar in prior)
    average_volume = sum(bar.volume for bar in prior) / len(prior)
    if impulse.close <= level or impulse.volume < average_volume * config.impulse_volume_multiple:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("IMPULSE_NOT_CONFIRMED",), reference_price=level)
    if pullback.close >= impulse.close or pullback.low <= level:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("PULLBACK_STRUCTURE_FAILED",), reference_price=level)
    if pullback.volume > impulse.volume:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("PULLBACK_VOLUME_NOT_NORMALIZED",), reference_price=level)
    if rebreak.close <= pullback.high or rebreak.volume <= pullback.volume:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("REBREAK_NOT_CONFIRMED",), reference_price=pullback.high)
    stop = max(level, pullback.low)
    if stop >= rebreak.close:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("INVALID_STOP_DISTANCE",), reference_price=level)
    return Signal(
        **base,
        decision=Decision.ENTER,
        reason_codes=("IMPULSE_CONFIRMED", "STRUCTURE_HELD", "LOCAL_REBREAK"),
        reference_price=pullback.high,
        stop_price=stop,
        max_holding_bars=config.max_holding_bars,
    )

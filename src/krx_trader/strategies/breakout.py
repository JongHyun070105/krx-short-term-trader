from __future__ import annotations

from dataclasses import dataclass

from krx_trader.market.regime import Regime
from krx_trader.models import Bar, Decision, Signal


@dataclass(frozen=True, slots=True)
class BreakoutConfig:
    lookback: int = 20
    volume_lookback: int = 20
    volume_multiple: float = 1.5
    max_holding_bars: int = 10

    def __post_init__(self) -> None:
        if min(self.lookback, self.volume_lookback, self.max_holding_bars) < 1 or self.volume_multiple <= 0:
            raise ValueError("breakout configuration must be positive")


DEFAULT_BREAKOUT_CONFIG = BreakoutConfig()


def evaluate_breakout(
    bars: list[Bar], symbol: str, *, regime: Regime | None = None, config: BreakoutConfig = DEFAULT_BREAKOUT_CONFIG
) -> Signal:
    if not bars:
        raise ValueError("at least one completed bar is required")
    last = bars[-1]
    base = {"timestamp": last.time, "symbol": symbol, "strategy_id": "breakout_volume"}
    if regime is None:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("REGIME_UNAVAILABLE",))
    if regime in {Regime.DOWN, Regime.HIGH_VOL}:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("REGIME_BLOCK",))
    needed = max(config.lookback, config.volume_lookback)
    if len(bars) < needed + 1:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("INSUFFICIENT_COMPLETED_BARS",))
    previous = bars[-needed - 1 : -1]
    reference = max(bar.high for bar in previous[-config.lookback :])
    volume_base = previous[-config.volume_lookback :]
    mean_volume = sum(bar.volume for bar in volume_base) / len(volume_base)
    if last.close <= reference:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("BREAKOUT_NOT_CONFIRMED",), reference_price=reference)
    if mean_volume <= 0 or last.volume < mean_volume * config.volume_multiple:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("VOLUME_TOO_LOW",), reference_price=reference)
    stop = min(last.low, reference)
    if stop <= 0 or stop >= last.close:
        return Signal(**base, decision=Decision.HOLD, reason_codes=("INVALID_STOP_DISTANCE",), reference_price=reference)
    return Signal(
        **base,
        decision=Decision.ENTER,
        reason_codes=("PREVIOUS_RANGE_BREAK", "VOLUME_CONFIRMED"),
        reference_price=reference,
        stop_price=stop,
        max_holding_bars=config.max_holding_bars,
    )

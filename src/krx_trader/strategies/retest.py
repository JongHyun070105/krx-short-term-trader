from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from math import isclose
from zoneinfo import ZoneInfo

from krx_trader.models import Bar, Decision, Signal

KST = ZoneInfo("Asia/Seoul")


class RetestVariant(StrEnum):
    RETEST_A = "RETEST-A"
    RETEST_B = "RETEST-B"


class SetupState(StrEnum):
    IDLE = "IDLE"
    BREAKOUT_DETECTED = "BREAKOUT_DETECTED"
    WAITING_RETEST = "WAITING_RETEST"
    RETEST_OBSERVED = "RETEST_OBSERVED"
    ACCEPTANCE_CONFIRMED = "ACCEPTANCE_CONFIRMED"
    ENTER_NEXT_BAR = "ENTER_NEXT_BAR"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class BreakoutRetestConfig:
    """Frozen, small-scope retest rules; breakout inputs match Breakout v1."""

    lookback: int = 20
    volume_lookback: int = 20
    volume_multiple: float = 1.5
    max_retest_bars: int = 3
    retest_tolerance_pct: float = 0.003
    stop_buffer_pct: float = 0.003
    max_holding_bars: int = 10

    def __post_init__(self) -> None:
        if min(self.lookback, self.volume_lookback, self.max_retest_bars, self.max_holding_bars) < 1:
            raise ValueError("retest lookbacks, expiry, and holding limit must be positive")
        if self.volume_multiple <= 0 or not 0 < self.retest_tolerance_pct < 0.05:
            raise ValueError("retest volume and tolerance settings are invalid")
        if not 0 < self.stop_buffer_pct < 0.05:
            raise ValueError("retest stop buffer must be a small positive percentage")


DEFAULT_RETEST_CONFIG = BreakoutRetestConfig()


def preregistration_config(
    variant: RetestVariant,
    config: BreakoutRetestConfig = DEFAULT_RETEST_CONFIG,
) -> dict[str, object]:
    return {
        "variant": variant.value,
        "config": asdict(config),
        "rules": {
            "breakout": "completed close above the previous 20 completed-bar highs with volume >= 1.5x the prior 20-bar mean; same as Breakout v1",
            "retest_window": "the next 1 to 3 completed bars; setup expires after bar 3",
            "retest": "bar low reaches within 0.30% of the fixed breakout level, does not penetrate 0.30% below it, and closes strictly above the level",
            "acceptance_a": "RETEST-A accepts on the retest bar close above the breakout level",
            "acceptance_b": "RETEST-B additionally requires a later completed bar to close above the retest bar high, within the same 3-bar window",
            "invalidation": "close at or below the breakout level, low below the permitted 0.30% penetration, or expiry",
            "entry": "next available completed interval bar open after acceptance; acceptance-bar OHLC is never a fill",
            "stop": "the lower of breakout level and retest low, minus a 0.30% structural buffer",
            "exit": "existing 10 completed-bar maximum-holding rule, stop first; no exit optimization",
            "regime": "OFF for primary strategy evaluation; raw regime is context only",
        },
    }


@dataclass(slots=True)
class _Setup:
    setup_id: str
    symbol: str
    interval: str
    breakout_time: str
    breakout_level: float
    breakout_close: float
    breakout_relative_volume: float
    breakout_index: int
    bars_since_breakout: int = 0
    retest_attempt_time: str | None = None
    retest_attempt_low: float | None = None
    retest_attempt_close: float | None = None
    bars_to_retest: int | None = None
    retest_time: str | None = None
    retest_low: float | None = None
    retest_close: float | None = None
    retest_high: float | None = None
    retest_depth_pct: float | None = None
    level_penetration_pct: float | None = None
    acceptance_time: str | None = None
    acceptance_close: float | None = None
    stop_price: float | None = None
    state: SetupState = SetupState.WAITING_RETEST
    terminal_reason: str | None = None
    transitions: list[str] | None = None

    def as_dict(self) -> dict[str, object]:
        values = asdict(self)
        values["state"] = self.state.value
        values["transitions"] = list(self.transitions or [])
        return values


class BreakoutRetestMachine:
    """Single-symbol completed-bar state machine with one setup per resistance level."""

    def __init__(
        self,
        variant: RetestVariant,
        config: BreakoutRetestConfig = DEFAULT_RETEST_CONFIG,
    ) -> None:
        self.variant = RetestVariant(variant)
        self.config = config
        self.state = SetupState.IDLE
        self.setup: _Setup | None = None
        self.setups: list[_Setup] = []
        self._last_time = None
        self._session_date: date | None = None
        self._seen_levels: list[float] = []

    def _transition(self, state: SetupState, reason: str | None = None) -> None:
        self.state = state
        if self.setup is not None:
            self.setup.state = state
            if self.setup.transitions is None:
                self.setup.transitions = []
            self.setup.transitions.append(state.value)
            if reason:
                self.setup.terminal_reason = reason

    def _hold(self, bar: Bar, symbol: str, reason: str | None = None) -> Signal:
        return Signal(
            timestamp=bar.time,
            symbol=symbol,
            strategy_id=self.variant.value.lower().replace("-", "_"),
            decision=Decision.HOLD,
            reason_codes=(reason or self.state.value,),
            reference_price=self.setup.breakout_level if self.setup else None,
        )

    def _finish(self, state: SetupState, reason: str) -> None:
        if self.setup is not None:
            self._transition(state, reason)
        self.setup = None

    def _new_breakout(self, history: list[Bar], symbol: str, interval: str) -> bool:
        needed = max(self.config.lookback, self.config.volume_lookback)
        if len(history) < needed + 1:
            return False
        current = history[-1]
        previous = history[-needed - 1:-1]
        level = max(bar.high for bar in previous[-self.config.lookback:])
        volume_history = previous[-self.config.volume_lookback:]
        mean_volume = sum(bar.volume for bar in volume_history) / len(volume_history)
        if current.close <= level or mean_volume <= 0 or current.volume < mean_volume * self.config.volume_multiple:
            return False
        if any(isclose(level, seen, rel_tol=self.config.retest_tolerance_pct, abs_tol=0.0)
               for seen in self._seen_levels):
            return False
        self._seen_levels.append(level)
        setup_id = hashlib.sha256(
            f"{symbol}|{interval}|{current.time.isoformat()}|{level:.8f}".encode()
        ).hexdigest()[:20]
        self.setup = _Setup(
            setup_id=setup_id,
            symbol=symbol,
            interval=interval,
            breakout_time=current.time.isoformat(),
            breakout_level=level,
            breakout_close=current.close,
            breakout_relative_volume=current.volume / mean_volume,
            breakout_index=len(history) - 1,
            transitions=[SetupState.BREAKOUT_DETECTED.value, SetupState.WAITING_RETEST.value],
        )
        self.setups.append(self.setup)
        self.state = SetupState.WAITING_RETEST
        return True

    def _accept(self, bar: Bar, symbol: str) -> Signal:
        assert self.setup is not None and self.setup.retest_low is not None
        level = self.setup.breakout_level
        self.setup.acceptance_time = bar.time.isoformat()
        self.setup.acceptance_close = bar.close
        self.setup.stop_price = min(level, self.setup.retest_low) * (1 - self.config.stop_buffer_pct)
        self._transition(SetupState.ACCEPTANCE_CONFIRMED)
        signal = Signal(
            timestamp=bar.time,
            symbol=symbol,
            strategy_id=self.variant.value.lower().replace("-", "_"),
            decision=Decision.ENTER,
            reason_codes=("BREAKOUT_LEVEL_RETESTED", "ACCEPTANCE_CONFIRMED"),
            reference_price=level,
            stop_price=self.setup.stop_price,
            max_holding_bars=self.config.max_holding_bars,
        )
        self._transition(SetupState.ENTER_NEXT_BAR, "NEXT_BAR_ENTRY_SIGNAL")
        return signal

    def update(self, history: list[Bar], symbol: str, interval: str) -> Signal:
        if not history:
            raise ValueError("at least one completed bar is required")
        bar = history[-1]
        if self._last_time == bar.time:
            return self._hold(bar, symbol)
        if self._last_time is not None and bar.time < self._last_time:
            raise ValueError("completed bars must be supplied in chronological order")
        local_day = bar.time.astimezone(KST).date()
        if self._session_date is not None and local_day != self._session_date:
            if self.setup is not None:
                self._finish(SetupState.EXPIRED, "SESSION_BOUNDARY")
            self._seen_levels.clear()
            self.state = SetupState.IDLE
        self._session_date = local_day
        self._last_time = bar.time

        if self.state == SetupState.ENTER_NEXT_BAR:
            self.setup = None
            self.state = SetupState.IDLE

        if self.setup is None:
            self._new_breakout(history, symbol, interval)
            return self._hold(bar, symbol)

        setup = self.setup
        setup.bars_since_breakout += 1
        if setup.bars_since_breakout > self.config.max_retest_bars:
            self._finish(SetupState.EXPIRED, "RETEST_TIMEOUT")
            return self._hold(bar, symbol, "SETUP_EXPIRED")

        level = setup.breakout_level
        lower_bound = level * (1 - self.config.retest_tolerance_pct)
        upper_bound = level * (1 + self.config.retest_tolerance_pct)
        if bar.low <= upper_bound and setup.retest_attempt_time is None:
            setup.retest_attempt_time = bar.time.isoformat()
            setup.retest_attempt_low = bar.low
            setup.retest_attempt_close = bar.close
            setup.bars_to_retest = setup.bars_since_breakout
        if bar.close <= level:
            self._finish(SetupState.INVALIDATED, "CLOSE_AT_OR_BELOW_BREAKOUT_LEVEL")
            return self._hold(bar, symbol, "SETUP_INVALIDATED")
        if bar.low < lower_bound:
            self._finish(SetupState.INVALIDATED, "RETEST_PENETRATION_TOO_DEEP")
            return self._hold(bar, symbol, "SETUP_INVALIDATED")

        if self.state == SetupState.RETEST_OBSERVED:
            assert setup.retest_high is not None
            if self.variant == RetestVariant.RETEST_B and bar.close > setup.retest_high:
                return self._accept(bar, symbol)
            return self._hold(bar, symbol)

        if bar.low <= upper_bound:
            setup.retest_time = bar.time.isoformat()
            setup.retest_low = bar.low
            setup.retest_close = bar.close
            setup.retest_high = bar.high
            penetration = max(0.0, (level - bar.low) / level * 100)
            setup.retest_depth_pct = penetration
            setup.level_penetration_pct = penetration
            self._transition(SetupState.RETEST_OBSERVED)
            if self.variant == RetestVariant.RETEST_A:
                return self._accept(bar, symbol)
        return self._hold(bar, symbol)


def evaluate_breakout_retest(
    bars: list[Bar],
    symbol: str,
    interval: str,
    variant: RetestVariant,
    *,
    config: BreakoutRetestConfig = DEFAULT_RETEST_CONFIG,
) -> Signal:
    """Replay completed history and return only a signal from its final completed bar."""
    if not bars:
        raise ValueError("at least one completed bar is required")
    machine = BreakoutRetestMachine(variant, config)
    signal = machine._hold(bars[0], symbol, SetupState.IDLE.value)
    for index in range(len(bars)):
        signal = machine.update(bars[:index + 1], symbol, interval)
    return signal

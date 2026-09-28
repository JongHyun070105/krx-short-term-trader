from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from krx_trader.models import Bar, Decision, Signal


class RelativeStrengthVariant(StrEnum):
    RS_A = "RS-A"  # Persistent Leader
    RS_B = "RS-B"  # Persistent Leader + Reacceleration


@dataclass(frozen=True, slots=True)
class RelativeStrengthConfig:
    short_lookback_bars: int = 3
    med_lookback_bars: int = 6
    rank_threshold: float = 80.0
    persistence_lookback: int = 3
    min_persistence_count: int = 2
    min_cross_section_size: int = 15
    max_single_bar_return_pct: float = 0.04
    max_holding_bars: int = 10

    def __post_init__(self) -> None:
        if min(self.short_lookback_bars, self.med_lookback_bars, self.persistence_lookback, self.max_holding_bars) < 1:
            raise ValueError("lookback and holding parameters must be positive integers")
        if self.short_lookback_bars >= self.med_lookback_bars:
            raise ValueError("short lookback must be strictly less than medium lookback")
        if not 0.0 < self.rank_threshold < 100.0:
            raise ValueError("rank threshold must be between 0 and 100")
        if not 1 <= self.min_persistence_count <= self.persistence_lookback:
            raise ValueError("min persistence count must be between 1 and persistence lookback")
        if self.min_cross_section_size < 2:
            raise ValueError("min cross section size must be at least 2")
        if self.max_single_bar_return_pct <= 0.0:
            raise ValueError("max single bar return pct must be positive")


DEFAULT_RS_CONFIG = RelativeStrengthConfig()


@dataclass(frozen=True, slots=True)
class CrossSectionalObservation:
    timestamp: datetime
    symbol: str
    close_price: float
    ret_short: float
    ret_med: float
    ret_1bar: float
    percentile_short: float
    percentile_med: float
    persistence_count: int
    cohort_size: int
    median_ret_med: float


class CrossSectionalEngine:
    """Computes point-in-time cross-sectional ranks and persistence without lookahead."""

    def __init__(self, config: RelativeStrengthConfig = DEFAULT_RS_CONFIG) -> None:
        self.config = config
        self._history_by_timestamp: dict[datetime, dict[str, CrossSectionalObservation]] = {}
        self._timeline: list[datetime] = []

    def evaluate_timestamp(
        self,
        timestamp: datetime,
        histories_by_symbol: dict[str, list[Bar]],
    ) -> dict[str, CrossSectionalObservation]:
        """Compute cross-sectional ranks strictly at the given timestamp across symbols."""
        if timestamp in self._history_by_timestamp:
            return self._history_by_timestamp[timestamp]

        needed_bars = self.config.med_lookback_bars + 1
        active_returns: list[tuple[str, float, float, float, float]] = []

        for symbol in sorted(histories_by_symbol.keys()):
            raw_bars = histories_by_symbol[symbol]
            if not raw_bars:
                continue
            # Isolate strictly bars <= timestamp
            bars = [b for b in raw_bars if b.time <= timestamp]
            if not bars or bars[-1].time != timestamp or len(bars) < needed_bars:
                continue
            curr_bar = bars[-1]
            short_ref = bars[-1 - self.config.short_lookback_bars]
            med_ref = bars[-1 - self.config.med_lookback_bars]
            prev_ref = bars[-2]

            if short_ref.close <= 0 or med_ref.close <= 0 or prev_ref.close <= 0 or curr_bar.close <= 0:
                continue

            ret_short = (curr_bar.close - short_ref.close) / short_ref.close
            ret_med = (curr_bar.close - med_ref.close) / med_ref.close
            ret_1bar = (curr_bar.close - prev_ref.close) / prev_ref.close
            active_returns.append((symbol, curr_bar.close, ret_short, ret_med, ret_1bar))

        cohort_size = len(active_returns)
        if cohort_size < self.config.min_cross_section_size:
            # Insufficient cross section
            self._history_by_timestamp[timestamp] = {}
            self._timeline.append(timestamp)
            return {}

        # Deterministic sorting for percentile ranks: tie-break by symbol code ascending
        sorted_by_short = sorted(active_returns, key=lambda item: (item[2], item[0]))
        sorted_by_med = sorted(active_returns, key=lambda item: (item[3], item[0]))

        ranks_short = {item[0]: (rank + 1) / cohort_size * 100.0 for rank, item in enumerate(sorted_by_short)}
        ranks_med = {item[0]: (rank + 1) / cohort_size * 100.0 for rank, item in enumerate(sorted_by_med)}

        med_values = [item[3] for item in sorted_by_med]
        median_ret_med = med_values[cohort_size // 2]

        # Persistence: inspect up to (persistence_lookback - 1) prior timestamps
        prior_timestamps = self._timeline[-(self.config.persistence_lookback - 1):] if self.config.persistence_lookback > 1 else []

        observations: dict[str, CrossSectionalObservation] = {}
        for symbol, close_price, ret_short, ret_med, ret_1bar in active_returns:
            pct_short = ranks_short[symbol]
            pct_med = ranks_med[symbol]

            # Count high-rank occurrences in prior timestamps + current timestamp
            high_count = 1 if pct_short >= self.config.rank_threshold else 0
            for pt in prior_timestamps:
                prior_obs = self._history_by_timestamp.get(pt, {}).get(symbol)
                if prior_obs and prior_obs.percentile_short >= self.config.rank_threshold:
                    high_count += 1

            obs = CrossSectionalObservation(
                timestamp=timestamp,
                symbol=symbol,
                close_price=close_price,
                ret_short=ret_short,
                ret_med=ret_med,
                ret_1bar=ret_1bar,
                percentile_short=pct_short,
                percentile_med=pct_med,
                persistence_count=high_count,
                cohort_size=cohort_size,
                median_ret_med=median_ret_med,
            )
            observations[symbol] = obs

        self._history_by_timestamp[timestamp] = observations
        self._timeline.append(timestamp)
        return observations


def evaluate_relative_strength(
    obs: CrossSectionalObservation | None,
    bars: list[Bar],
    symbol: str,
    *,
    variant: RelativeStrengthVariant = RelativeStrengthVariant.RS_A,
    config: RelativeStrengthConfig = DEFAULT_RS_CONFIG,
) -> Signal:
    """Evaluate RS-A or RS-B candidate rules strictly on point-in-time inputs."""
    if not bars:
        raise ValueError("bars cannot be empty")
    last_bar = bars[-1]
    base = {
        "timestamp": last_bar.time,
        "symbol": symbol,
        "strategy_id": f"relative_strength_{variant.value.lower().replace('-', '_')}",
    }

    if obs is None or obs.cohort_size < config.min_cross_section_size:
        return Signal(
            **base,
            decision=Decision.HOLD,
            reason_codes=("INSUFFICIENT_CROSS_SECTION",),
            reference_price=last_bar.close,
        )

    # Common structural stop: lowest low of recent short lookback bars
    if len(bars) < config.short_lookback_bars:
        return Signal(
            **base,
            decision=Decision.HOLD,
            reason_codes=("INSUFFICIENT_BARS",),
            reference_price=last_bar.close,
        )

    recent_bars = bars[-config.short_lookback_bars:]
    stop = min(bar.low for bar in recent_bars)
    if stop <= 0 or stop >= last_bar.close:
        return Signal(
            **base,
            decision=Decision.HOLD,
            reason_codes=("INVALID_STOP_DISTANCE",),
            reference_price=last_bar.close,
        )

    if variant == RelativeStrengthVariant.RS_A:
        # RS-A: Persistent Leader
        # 1. High short-term relative rank
        # 2. Positive medium-term relative rank (outperforming median)
        # 3. Rank persistence >= threshold
        # 4. Absolute short return > 0
        if obs.percentile_short < config.rank_threshold:
            return Signal(
                **base,
                decision=Decision.HOLD,
                reason_codes=("RANK_BELOW_THRESHOLD",),
                reference_price=last_bar.close,
            )
        if obs.ret_short <= 0:
            return Signal(
                **base,
                decision=Decision.HOLD,
                reason_codes=("ABSOLUTE_SHORT_RETURN_NON_POSITIVE",),
                reference_price=last_bar.close,
            )
        if obs.percentile_med < 50.0 or obs.ret_med <= 0:
            return Signal(
                **base,
                decision=Decision.HOLD,
                reason_codes=("MEDIUM_RELATIVE_WEAKNESS",),
                reference_price=last_bar.close,
            )
        if obs.persistence_count < config.min_persistence_count:
            return Signal(
                **base,
                decision=Decision.HOLD,
                reason_codes=("INSUFFICIENT_RANK_PERSISTENCE",),
                reference_price=last_bar.close,
            )

        return Signal(
            **base,
            decision=Decision.ENTER,
            reason_codes=("PERSISTENT_LEADER_QUALIFIED", "SHORT_RS_HIGH", "MED_RS_POSITIVE"),
            reference_price=last_bar.close,
            stop_price=stop,
            max_holding_bars=config.max_holding_bars,
        )

    elif variant == RelativeStrengthVariant.RS_B:
        # RS-B: Persistent Leader + Reacceleration
        # 1. Persistent leader (persistence >= threshold and med percentile >= 50)
        # 2. Reacceleration confirmation: latest 1-bar return > 0
        # 3. Anti-climax guard: 0 < latest 1-bar return <= max_single_bar_return_pct
        # 4. Short return accelerating over medium return: ret_short > 0.5 * ret_med and ret_short > 0
        if obs.persistence_count < config.min_persistence_count or obs.percentile_med < 50.0:
            return Signal(
                **base,
                decision=Decision.HOLD,
                reason_codes=("NOT_A_PERSISTENT_LEADER",),
                reference_price=last_bar.close,
            )
        if obs.ret_1bar <= 0:
            return Signal(
                **base,
                decision=Decision.HOLD,
                reason_codes=("NO_REACCELERATION_CONFIRMATION",),
                reference_price=last_bar.close,
            )
        if obs.ret_1bar > config.max_single_bar_return_pct:
            return Signal(
                **base,
                decision=Decision.HOLD,
                reason_codes=("SINGLE_BAR_OVEREXTENDED_CLIMAX",),
                reference_price=last_bar.close,
            )
        if obs.ret_short <= 0 or obs.ret_short <= 0.5 * obs.ret_med:
            return Signal(
                **base,
                decision=Decision.HOLD,
                reason_codes=("MOMENTUM_NOT_ACCELERATING",),
                reference_price=last_bar.close,
            )

        return Signal(
            **base,
            decision=Decision.ENTER,
            reason_codes=("PERSISTENT_LEADER_REACCELERATING", "ANTI_CLIMAX_CONFIRMED"),
            reference_price=last_bar.close,
            stop_price=stop,
            max_holding_bars=config.max_holding_bars,
        )

    raise ValueError(f"unknown RelativeStrengthVariant: {variant}")


def preregistration_rs_config(
    variant: RelativeStrengthVariant,
    config: RelativeStrengthConfig = DEFAULT_RS_CONFIG,
) -> dict[str, Any]:
    return {
        "variant": variant.value,
        "config": asdict(config),
        "entry_rule": "NEXT_BAR_OPEN",
        "stop_rule": "RECENT_3_BAR_LOWEST_LOW",
        "exit_rule": "BOUNDED_10_BAR_HOLDING_OR_STOP",
        "lookahead_prevention": "strictly completed bars; cross-section evaluated on synchronous history",
    }

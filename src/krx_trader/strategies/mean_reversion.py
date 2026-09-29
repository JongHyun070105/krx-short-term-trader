from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum

from krx_trader.models import Decision, Signal


class MeanReversionVariant(StrEnum):
    MR_A = "MR-A"
    MR_B = "MR-B"


@dataclass(frozen=True, slots=True)
class MeanReversionConfig:
    laggard_percentile: float = 20.0
    minimum_rank_recovery_points: float = 10.0
    minimum_turnover_percentile: float = 20.0
    maximum_freshness_minutes: float = 1.0
    minimum_clean_completeness: float = 0.8
    stop_lookback_bars: int = 3
    max_holding_bars: int = 10

    def __post_init__(self) -> None:
        if not 0 < self.laggard_percentile < 50:
            raise ValueError("laggard percentile must be between 0 and 50")
        if self.minimum_rank_recovery_points <= 0:
            raise ValueError("rank recovery must be positive")
        if not 0 <= self.minimum_turnover_percentile <= 100:
            raise ValueError("turnover percentile must be between 0 and 100")
        if self.maximum_freshness_minutes < 0:
            raise ValueError("maximum freshness cannot be negative")
        if not 0 < self.minimum_clean_completeness <= 1:
            raise ValueError("clean completeness must be in (0, 1]")
        if self.stop_lookback_bars < 1 or self.max_holding_bars < 1:
            raise ValueError("stop lookback and holding period must be positive")


DEFAULT_MR_CONFIG = MeanReversionConfig()


@dataclass(frozen=True, slots=True)
class ReversalObservation:
    timestamp: datetime
    symbol: str
    close_price: float
    stop_price: float
    prior_percentile: float
    prior_short_return: float
    rank_delta_points: float
    current_bar_return: float
    close_location: float | None
    turnover_percentile: float
    freshness_minutes: float
    completeness: float
    stopped_making_local_low: bool


def evaluate_mean_reversion(
    observation: ReversalObservation | None,
    *,
    variant: MeanReversionVariant,
    config: MeanReversionConfig = DEFAULT_MR_CONFIG,
) -> Signal | None:
    """Return an ENTER signal from completed-bar features, filled only on a later bar."""
    if observation is None:
        return None
    if observation.freshness_minutes > config.maximum_freshness_minutes:
        return None
    if observation.completeness <= 0 or observation.turnover_percentile < config.minimum_turnover_percentile:
        return None
    if observation.prior_percentile > config.laggard_percentile:
        return None
    if observation.prior_short_return >= 0:
        return None
    if observation.rank_delta_points < config.minimum_rank_recovery_points:
        return None
    if observation.current_bar_return <= 0:
        return None
    if observation.stop_price <= 0 or observation.stop_price >= observation.close_price:
        return None
    if variant is MeanReversionVariant.MR_B and not observation.stopped_making_local_low:
        return None

    return Signal(
        timestamp=observation.timestamp,
        symbol=observation.symbol,
        strategy_id=f"mean_reversion_{variant.value.lower().replace('-', '_')}",
        decision=Decision.ENTER,
        reason_codes=(
            "LAGGARD_PRIOR_RANK",
            "ABSOLUTE_RETURN_NEGATIVE",
            "RELATIVE_RANK_RECOVERY",
            "ABSOLUTE_PRICE_REVERSAL",
            "FRESH_LIQUID_OBSERVATION",
            *(("LOCAL_LOW_STABILIZED",) if variant is MeanReversionVariant.MR_B else ()),
        ),
        reference_price=observation.close_price,
        stop_price=observation.stop_price,
        max_holding_bars=config.max_holding_bars,
    )


def preregistration_mr_config(
    variant: MeanReversionVariant,
    config: MeanReversionConfig = DEFAULT_MR_CONFIG,
) -> dict[str, object]:
    return {
        "variant": variant.value,
        "config": asdict(config),
        "entry_rule": "SIGNAL_AFTER_COMPLETED_BAR; FILL_AT_NEXT_EXECUTABLE_BAR_OPEN",
        "exit_rule": "STRUCTURAL_REVERSAL_LOW_OR_MAX_10_BARS",
        "mr_b_addition": "CURRENT_LOW_DID_NOT_BREAK_PRIOR_3_BAR_LOCAL_LOW",
        "lookahead_prevention": "point_in_time_cross_section; future bars excluded from feature construction",
    }

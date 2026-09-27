from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from krx_trader.backtest.metrics import Metrics
from krx_trader.data.quality import require_healthy_bars
from krx_trader.models import Bar


@dataclass(frozen=True, slots=True)
class ChronologicalSplit:
    development: tuple[Bar, ...]
    validation: tuple[Bar, ...]
    final_test: tuple[Bar, ...]
    final_test_touched: bool = False


class StrategyGateState(StrEnum):
    CANDIDATE = "CANDIDATE"
    ACCEPTED_FOR_SHADOW = "ACCEPTED_FOR_SHADOW"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class StrategyGateThresholds:
    minimum_oos_trades: int = 30
    minimum_profit_factor: float = 1.0
    maximum_oos_drawdown_pct: float = 3.0
    minimum_stress_net_return_pct: float = 0.0


DEFAULT_STRATEGY_GATE_THRESHOLDS = StrategyGateThresholds()


@dataclass(frozen=True, slots=True)
class StrategyGateResult:
    state: StrategyGateState
    reasons: tuple[str, ...]


def evaluate_strategy_gate(
    oos: Metrics,
    stress_1_5x: Metrics,
    stress_2_0x: Metrics,
    neighborhood: Sequence[Metrics],
    thresholds: StrategyGateThresholds = DEFAULT_STRATEGY_GATE_THRESHOLDS,
) -> StrategyGateResult:
    """Promote only on fixed OOS, cost stress, and nearby-parameter evidence."""
    reasons: list[str] = []
    if oos.trade_count < thresholds.minimum_oos_trades:
        reasons.append("TOO_FEW_OOS_TRADES")
    if oos.expectancy_krw <= 0:
        reasons.append("OOS_EXPECTANCY_NOT_POSITIVE")
    if oos.profit_factor is None or oos.profit_factor <= thresholds.minimum_profit_factor:
        reasons.append("OOS_PROFIT_FACTOR_NOT_ABOVE_THRESHOLD")
    if oos.max_drawdown_pct > thresholds.maximum_oos_drawdown_pct:
        reasons.append("OOS_DRAWDOWN_OVER_LIMIT")
    for label, metrics in (("1_5X", stress_1_5x), ("2_0X", stress_2_0x)):
        if metrics.total_return_pct <= thresholds.minimum_stress_net_return_pct:
            reasons.append(f"COST_STRESS_{label}_FAILED")
    if not neighborhood:
        reasons.append("PARAMETER_NEIGHBORHOOD_MISSING")
    elif any(
        item.expectancy_krw <= 0 or item.profit_factor is None or item.profit_factor <= thresholds.minimum_profit_factor
        for item in neighborhood
    ):
        reasons.append("PARAMETER_NEIGHBORHOOD_UNSTABLE")
    return StrategyGateResult(
        StrategyGateState.ACCEPTED_FOR_SHADOW if not reasons else StrategyGateState.REJECTED,
        tuple(reasons),
    )


def chronological_split(
    bars: list[Bar], *, development_fraction: float = 0.55, validation_fraction: float = 0.20
) -> ChronologicalSplit:
    require_healthy_bars(bars)
    if not 0 < development_fraction < 1 or not 0 < validation_fraction < 1:
        raise ValueError("split fractions must be between zero and one")
    if development_fraction + validation_fraction >= 1:
        raise ValueError("development plus validation fractions must be less than one")
    first = int(len(bars) * development_fraction)
    second = int(len(bars) * (development_fraction + validation_fraction))
    return ChronologicalSplit(tuple(bars[:first]), tuple(bars[first:second]), tuple(bars[second:]))

from __future__ import annotations

from collections.abc import Callable
from math import log1p

from krx_trader.universe.models import CandidateContext, RankedCandidate


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _liquidity(context: CandidateContext) -> float:
    return _clamp(log1p(context.activity.turnover_krw) / log1p(10_000_000_000))


def _breakout_features(context: CandidateContext) -> tuple[float, tuple[str, ...]]:
    bars = context.completed_daily_bars
    if len(bars) < 6:
        return 0.0, ("INSUFFICIENT_HISTORY",)
    last = bars[-1]
    prior = bars[-21:-1]
    if not prior:
        return 0.0, ("INSUFFICIENT_HISTORY",)
    recent_high = max(bar.high for bar in prior)
    distance = max(0.0, (recent_high - last.close) / recent_high) if recent_high > 0 else 1.0
    near_high = _clamp(1.0 - distance / 0.05)
    mean_volume = sum(bar.volume for bar in prior[-20:]) / min(20, len(prior))
    relative_volume = _clamp(last.volume / mean_volume / 3.0) if mean_volume > 0 else 0.0
    mean_range = sum(bar.high - bar.low for bar in prior[-10:]) / min(10, len(prior))
    range_expansion = _clamp((last.high - last.low) / mean_range / 2.0) if mean_range > 0 else 0.0
    liquidity = _liquidity(context)
    score = 0.30 * liquidity + 0.30 * near_high + 0.25 * relative_volume + 0.15 * range_expansion
    reasons = []
    if liquidity >= 0.5:
        reasons.append("HIGH_LIQUIDITY")
    if near_high >= 0.6:
        reasons.append("NEAR_RECENT_HIGH")
    if relative_volume >= 0.5:
        reasons.append("HIGH_RELATIVE_VOLUME")
    if range_expansion >= 0.6:
        reasons.append("RANGE_EXPANSION")
    return score, tuple(reasons or ["LOW_BREAKOUT_PRIORITY"])


def _pullback_features(context: CandidateContext) -> tuple[float, tuple[str, ...]]:
    bars = context.completed_daily_bars
    if len(bars) < 8:
        return 0.0, ("INSUFFICIENT_HISTORY",)
    impulse = bars[-7:-2]
    last = bars[-1]
    impulse_floor = min(bar.low for bar in impulse)
    impulse_high = max(bar.high for bar in impulse)
    if impulse_floor <= 0:
        return 0.0, ("INVALID_PRICE_RANGE",)
    impulse_strength = _clamp((impulse_high / impulse_floor - 1.0) / 0.15)
    pullback_depth = max(0.0, (impulse_high - last.close) / impulse_high) if impulse_high > 0 else 1.0
    depth_score = _clamp(1.0 - abs(pullback_depth - 0.035) / 0.065)
    mean_impulse_volume = sum(bar.volume for bar in impulse) / len(impulse)
    contraction = _clamp(1.0 - last.volume / mean_impulse_volume) if mean_impulse_volume > 0 else 0.0
    trend_intact = 1.0 if last.close >= impulse_floor else 0.0
    liquidity = _liquidity(context)
    score = (
        0.30 * impulse_strength + 0.25 * depth_score + 0.20 * contraction
        + 0.15 * trend_intact + 0.10 * liquidity
    )
    reasons = []
    if impulse_strength >= 0.4:
        reasons.append("PRIOR_IMPULSE")
    if 0.005 <= pullback_depth <= 0.10:
        reasons.append("CONTROLLED_PULLBACK_DEPTH")
    if contraction >= 0.4:
        reasons.append("PULLBACK_VOLUME_CONTRACTION")
    if trend_intact:
        reasons.append("TREND_STRUCTURE_INTACT")
    if liquidity >= 0.5:
        reasons.append("HIGH_LIQUIDITY")
    return score, tuple(reasons or ["LOW_PULLBACK_PRIORITY"])


def _rank(
    strategy: str,
    contexts: list[CandidateContext],
    scorer: Callable[[CandidateContext], tuple[float, tuple[str, ...]]],
    top_n: int,
) -> list[RankedCandidate]:
    if top_n < 1:
        raise ValueError("top_n must be positive")
    scored = [(context, *scorer(context)) for context in contexts]
    scored.sort(key=lambda item: (-item[1], -item[0].activity.turnover_krw, item[0].stock.symbol))
    return [
        RankedCandidate(index, strategy, context.stock, context.activity, round(score, 6), reasons)
        for index, (context, score, reasons) in enumerate(scored[:top_n], start=1)
    ]


def rank_breakout(contexts: list[CandidateContext], *, top_n: int = 20) -> list[RankedCandidate]:
    """Rank deep-scan priority; never emits or implies an entry signal."""
    return _rank("breakout", contexts, _breakout_features, top_n)


def rank_pullback(contexts: list[CandidateContext], *, top_n: int = 20) -> list[RankedCandidate]:
    """Rank pullback deep-scan priority; never emits or implies an entry signal."""
    return _rank("pullback", contexts, _pullback_features, top_n)

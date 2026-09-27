from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True, slots=True)
class SizingResult:
    quantity: int
    risk_budget_krw: float
    expected_risk_krw: float
    notional_krw: float
    reason: str


def size_long_position(
    *,
    entry_price: float,
    stop_price: float,
    bot_cash_krw: float,
    current_exposure_krw: float,
    capital_cap_krw: float = 100_000,
    order_cap_krw: float = 20_000,
    risk_per_trade_pct: float = 0.25,
    max_price_krw: float = 50_000,
    min_price_krw: float = 1_000,
    fee_rate: float = 0.00015,
    sell_tax_rate: float = 0.002,
    slippage_bps: float = 15,
) -> SizingResult:
    """Size whole shares; ``entry_price`` is the assumed buy fill price including slippage."""
    budget = max(0.0, capital_cap_krw * risk_per_trade_pct / 100)
    if entry_price < min_price_krw or entry_price > max_price_krw:
        return SizingResult(0, budget, 0.0, 0.0, "PRICE_OUT_OF_RANGE")
    if stop_price <= 0 or stop_price >= entry_price:
        return SizingResult(0, budget, 0.0, 0.0, "INVALID_STOP")
    if min(bot_cash_krw, current_exposure_krw, capital_cap_krw, order_cap_krw) < 0:
        return SizingResult(0, budget, 0.0, 0.0, "INVALID_CAPITAL_STATE")
    remaining_cap = capital_cap_krw - current_exposure_krw
    max_notional = min(bot_cash_krw, remaining_cap, order_cap_krw)
    one_share_purchase_cost = entry_price * (1 + fee_rate)
    if max_notional < one_share_purchase_cost:
        return SizingResult(0, budget, 0.0, 0.0, "UNAFFORDABLE_ONE_SHARE")
    stop_fill = stop_price * (1 - slippage_bps / 10_000)
    per_share_risk = entry_price - stop_fill
    per_share_risk += (entry_price * fee_rate) + (stop_fill * (fee_rate + sell_tax_rate))
    if per_share_risk <= 0:
        return SizingResult(0, budget, 0.0, 0.0, "INSUFFICIENT_CASH_OR_CAP")
    by_risk = floor(budget / per_share_risk)
    by_notional = floor(max_notional / (entry_price * (1 + fee_rate)))
    quantity = max(0, min(by_risk, by_notional))
    notional = quantity * entry_price
    expected_risk = quantity * per_share_risk
    reason = "SIZED" if quantity else "RISK_BUDGET_BELOW_ONE_SHARE"
    return SizingResult(quantity, budget, expected_risk, notional, reason)

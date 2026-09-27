from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CostModel:
    broker_fee_rate: float
    sell_tax_rate: float
    slippage_bps: float

    def __post_init__(self) -> None:
        if min(self.broker_fee_rate, self.sell_tax_rate, self.slippage_bps) < 0:
            raise ValueError("cost assumptions cannot be negative")
        if self.broker_fee_rate + self.sell_tax_rate >= 1 or self.slippage_bps >= 10_000:
            raise ValueError("cost assumptions exceed valid execution bounds")

    def buy_fill_price(self, reference_price: float, multiplier: float = 1.0) -> float:
        return reference_price * (1 + self.slippage_bps * multiplier / 10_000)

    def sell_fill_price(self, reference_price: float, multiplier: float = 1.0) -> float:
        return reference_price * (1 - self.slippage_bps * multiplier / 10_000)

    def buy_cost(self, notional: float, multiplier: float = 1.0) -> float:
        return notional * self.broker_fee_rate * multiplier

    def sell_cost(self, notional: float, multiplier: float = 1.0) -> float:
        return notional * (self.broker_fee_rate + self.sell_tax_rate) * multiplier

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ResearchScenario:
    """Research-only sizing inputs; constructing one never changes live Settings."""

    name: str
    capital_krw: int
    order_cap_krw: int
    risk_per_trade_pct: float
    regime_mode: Literal["on", "off"]
    max_positions: int

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("research scenario name is required")
        if not 0 < self.capital_krw <= 100_000:
            raise ValueError("research scenario capital must be between 1 and 100000 KRW")
        if not 0 < self.order_cap_krw <= min(self.capital_krw, 50_000):
            raise ValueError("research scenario order cap must fit capital and the 50000 KRW limit")
        if not 0 < self.risk_per_trade_pct <= 1:
            raise ValueError("research scenario risk must be in (0, 1] percent")
        if self.regime_mode not in {"on", "off"} or self.max_positions < 1:
            raise ValueError("research scenario requires on/off regime and a positive position count")

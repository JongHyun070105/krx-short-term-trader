from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Decision(StrEnum):
    ENTER = "ENTER"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover_krw: int | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    observed_at: datetime
    price: int
    open: int | None = None
    high: int | None = None
    low: int | None = None
    volume: int | None = None
    turnover_krw: int | None = None


@dataclass(frozen=True, slots=True)
class Signal:
    timestamp: datetime
    symbol: str
    strategy_id: str
    decision: Decision
    reason_codes: tuple[str, ...]
    reference_price: float | None = None
    stop_price: float | None = None
    max_holding_bars: int | None = None

from __future__ import annotations

from dataclasses import dataclass

from krx_trader.models import Bar


@dataclass(frozen=True, slots=True)
class StockMaster:
    symbol: str
    name: str
    market: str
    instrument_type: str
    listing_status: str
    reference_price: int | None
    market_cap_raw: int | None
    halted: bool
    management: bool
    warning_status: str | None
    is_preferred: bool
    is_etp: bool
    is_spac: bool


@dataclass(frozen=True, slots=True)
class MarketActivity:
    symbol: str
    price: int
    volume: int
    turnover_krw: int
    source: str
    source_rank: int


@dataclass(frozen=True, slots=True)
class Eligibility:
    stock: StockMaster
    activity: MarketActivity
    eligible: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    rank: int
    strategy: str
    stock: StockMaster
    activity: MarketActivity
    score: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateContext:
    stock: StockMaster
    activity: MarketActivity
    completed_daily_bars: tuple[Bar, ...]

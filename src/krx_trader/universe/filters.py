from __future__ import annotations

from krx_trader.config import Settings
from krx_trader.universe.models import Eligibility, MarketActivity, StockMaster


def check_eligibility(
    stock: StockMaster,
    activity: MarketActivity,
    settings: Settings,
    *,
    min_turnover_krw: int,
) -> Eligibility:
    reasons: list[str] = []
    if stock.market not in {"KOSPI", "KOSDAQ"}:
        reasons.append("MARKET_UNSUPPORTED")
    if stock.instrument_type != "COMMON":
        reasons.append("SECURITY_TYPE_EXCLUDED")
    if stock.halted:
        reasons.append("TRADING_HALTED")
    if stock.management:
        reasons.append("MANAGEMENT_SECURITY")
    if stock.warning_status:
        reasons.append("MARKET_WARNING")
    if activity.price <= 0:
        reasons.append("PRICE_UNAVAILABLE")
    elif activity.price < settings.min_price_krw:
        reasons.append("PRICE_TOO_LOW")
    elif activity.price > settings.max_price_krw:
        reasons.append("PRICE_TOO_HIGH")
    if activity.turnover_krw < min_turnover_krw:
        reasons.append("LIQUIDITY_LOW")
    return Eligibility(stock, activity, not reasons, tuple(reasons or ["ELIGIBLE"]))


def deduplicate_activities(
    *sources: list[MarketActivity],
) -> list[MarketActivity]:
    """Union rank sources with deterministic source order and symbol de-duplication."""
    by_symbol: dict[str, MarketActivity] = {}
    for source_rows in sources:
        for row in source_rows:
            if row.symbol not in by_symbol:
                by_symbol[row.symbol] = row
                continue
            current = by_symbol[row.symbol]
            by_symbol[row.symbol] = MarketActivity(
                symbol=row.symbol,
                price=current.price or row.price,
                volume=max(current.volume, row.volume),
                turnover_krw=max(current.turnover_krw, row.turnover_krw),
                source=current.source,
                source_rank=current.source_rank,
            )
    return sorted(by_symbol.values(), key=lambda item: item.symbol)


def eligible_activities(
    stocks: dict[str, StockMaster],
    activities: list[MarketActivity],
    settings: Settings,
    *,
    min_turnover_krw: int,
) -> tuple[list[Eligibility], list[Eligibility]]:
    included: list[Eligibility] = []
    excluded: list[Eligibility] = []
    for activity in activities:
        stock = stocks.get(activity.symbol)
        if stock is None:
            continue
        result = check_eligibility(stock, activity, settings, min_turnover_krw=min_turnover_krw)
        (included if result.eligible else excluded).append(result)
    included.sort(key=lambda item: (-item.activity.turnover_krw, item.stock.symbol))
    excluded.sort(key=lambda item: item.stock.symbol)
    return included, excluded

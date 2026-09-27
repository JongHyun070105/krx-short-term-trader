from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.kis.rest import KisRestClient
from krx_trader.universe.filters import deduplicate_activities, eligible_activities
from krx_trader.universe.master import load_stock_master
from krx_trader.universe.models import CandidateContext, RankedCandidate
from krx_trader.universe.rankers import rank_breakout, rank_pullback

KST = ZoneInfo("Asia/Seoul")


def scan_market(
    client: KisRestClient,
    settings: Settings,
    *,
    top_n: int = 50,
    master_path: Path = Path("data/universe/stocks.parquet"),
) -> tuple[list[CandidateContext], int]:
    if not 1 <= top_n <= 100:
        raise ValueError("top_n must be between 1 and 100")
    stocks = {row.symbol: row for row in load_stock_master(master_path)}
    volume = client.get_market_activity_rank(
        sort_by="volume", limit=top_n, min_price=settings.min_price_krw, max_price=settings.max_price_krw
    )
    turnover = client.get_market_activity_rank(
        sort_by="turnover", limit=top_n, min_price=settings.min_price_krw, max_price=settings.max_price_krw
    )
    activities = deduplicate_activities(volume, turnover)
    included, excluded = eligible_activities(
        stocks, activities, settings, min_turnover_krw=settings.min_turnover_krw
    )
    contexts = [CandidateContext(item.stock, item.activity, ()) for item in included[:top_n]]
    return contexts, len(excluded)


def rank_market_candidates(
    client: KisRestClient,
    settings: Settings,
    *,
    strategy: str,
    top_n: int = 20,
    market_limit: int = 50,
    master_path: Path = Path("data/universe/stocks.parquet"),
    cache: ParquetBarCache | None = None,
) -> tuple[list[RankedCandidate], dict[str, str], int]:
    if strategy not in {"breakout", "pullback"}:
        raise ValueError("strategy must be breakout or pullback")
    contexts, excluded_count = scan_market(client, settings, top_n=market_limit, master_path=master_path)
    end = datetime.now(KST).date() - timedelta(days=1)
    start = end - timedelta(days=60)
    failures: dict[str, str] = {}
    enriched: list[CandidateContext] = []
    for context in contexts:
        try:
            bars = client.get_daily_bars(context.stock.symbol, start, end)
            if cache is not None and bars:
                cache.save(
                    bars,
                    kind="daily",
                    symbol=context.stock.symbol,
                    interval="1d",
                    market=context.stock.market,
                    source="KIS daily OHLCV",
                )
            enriched.append(CandidateContext(context.stock, context.activity, tuple(bars)))
        except (OSError, ValueError, RuntimeError) as exc:
            failures[context.stock.symbol] = type(exc).__name__
    ranker = rank_breakout if strategy == "breakout" else rank_pullback
    return ranker(enriched, top_n=top_n), failures, excluded_count

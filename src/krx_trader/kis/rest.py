from __future__ import annotations

import fcntl
import json
import os
import random
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from krx_trader.kis.auth import PRODUCTION_BASE_URL, TokenManager
from krx_trader.kis.transport import JsonTransport, TransportError
from krx_trader.models import Bar, Quote
from krx_trader.universe.models import MarketActivity

KST = ZoneInfo("Asia/Seoul")
MINUTE_TIMESTAMP_CONVENTION = "bar_start"
DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
INDEX_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
VOLUME_RANK_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"


class KisApiError(RuntimeError):
    pass


class _PersistentRateLimiter:
    """Serialize REST calls across CLI processes and persist provider cooldowns."""

    def __init__(self, state_path: Path, interval: float, sleeper: Callable[[float], None], wall_clock: Callable[[], float]):
        self._path = state_path
        self._interval = max(0.0, interval)
        self._sleep = sleeper
        self._wall_clock = wall_clock

    def _locked_state(self, update: Callable[[dict[str, float]], None]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "r+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0)
                try:
                    raw = json.load(stream)
                except (json.JSONDecodeError, OSError):
                    raw = {}
                state = {
                    key: value for key, value in raw.items()
                    if key in {"last_request_at", "blocked_until", "minimum_interval"}
                    and isinstance(value, (int, float))
                } if isinstance(raw, dict) else {}
                update(state)
                stream.seek(0)
                stream.truncate()
                json.dump(state, stream, sort_keys=True)
                stream.flush()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def wait(self) -> None:
        def apply(state: dict[str, float]) -> None:
            now = self._wall_clock()
            last = state.get("last_request_at")
            interval = max(self._interval, state.get("minimum_interval", 0.0))
            target = max(
                state.get("blocked_until", 0.0),
                last + interval if last is not None else 0.0,
            )
            delay = target - now
            if delay > 0:
                self._sleep(delay)
                now = max(self._wall_clock(), target)
            state["last_request_at"] = now

        self._locked_state(apply)

    def defer(self, seconds: float, *, rate_limited: bool = False) -> None:
        def apply(state: dict[str, float]) -> None:
            state["blocked_until"] = max(state.get("blocked_until", 0.0), self._wall_clock() + seconds)
            if rate_limited:
                prior = max(self._interval, state.get("minimum_interval", 0.0))
                state["minimum_interval"] = min(5.0, max(1.0, prior * 2.0))

        self._locked_state(apply)


class KisRestClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        token_manager: TokenManager,
        transport: JsonTransport,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        monotonic: Callable[[], float] = time.monotonic,
        min_request_interval: float = 1.0,
        max_retries: int = 3,
        rate_limit_path: Path = Path("runtime/kis_rate_limit.json"),
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._tokens = token_manager
        self._transport = transport
        self._sleep = sleeper
        self._jitter = jitter
        self._monotonic = monotonic
        self._min_request_interval = max(0.0, min_request_interval)
        self._rate_lock = threading.Lock()
        self._last_request_at: float | None = None
        self._shared_limiter = _PersistentRateLimiter(
            rate_limit_path, self._min_request_interval, sleeper, wall_clock
        )
        self._confirmed_daily_sessions: set[tuple[str, date]] = set()
        self._max_retries = max(0, max_retries)

    def _wait_for_request_slot(self) -> None:
        with self._rate_lock:
            self._shared_limiter.wait()
            now = self._monotonic()
            if self._last_request_at is not None:
                delay = self._min_request_interval - (now - self._last_request_at)
                if delay > 0:
                    self._sleep(delay)
                    now = self._monotonic()
            self._last_request_at = now

    def _get(self, path: str, tr_id: str, params: dict[str, str]) -> dict[str, Any]:
        refreshed = False
        attempt = 0
        while True:
            token = self._tokens.get_token()
            self._wait_for_request_slot()
            try:
                response = self._transport.request(
                    "GET",
                    f"{PRODUCTION_BASE_URL}{path}",
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "authorization": f"Bearer {token}",
                        "appkey": self._app_key,
                        "appsecret": self._app_secret,
                        "tr_id": tr_id,
                        "custtype": "P",
                    },
                    params=params,
                )
                if response.status_code == 401 and not refreshed:
                    self._tokens.invalidate(token)
                    refreshed = True
                    continue
                payload = response.json()
            except (TransportError, ValueError, OSError) as exc:
                if attempt < self._max_retries:
                    self._sleep(self._jitter(0, 0.25) + 0.25 * (2**attempt))
                    attempt += 1
                    continue
                raise KisApiError(f"KIS read request failed ({type(exc).__name__})") from None

            if payload.get("msg_cd") == "EGW00123" and not refreshed:
                self._tokens.invalidate(token)
                refreshed = True
                continue
            if payload.get("msg_cd") == "EGW00201" and attempt < self._max_retries:
                self._shared_limiter.defer(61.0 + self._jitter(0.0, 0.25), rate_limited=True)
                attempt += 1
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                self._sleep(self._jitter(0, 0.25) + 0.25 * (2**attempt))
                attempt += 1
                continue
            if response.status_code != 200 or payload.get("rt_cd") not in {None, "0", 0}:
                code = payload.get("msg_cd")
                suffix = f" code={code}" if isinstance(code, str) else ""
                raise KisApiError(f"KIS read request failed (HTTP {response.status_code}){suffix}")
            return payload

    def get_quote(self, symbol: str) -> Quote:
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("symbol must be a six-digit KRX code")
        payload = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        output = payload.get("output")
        try:
            price = int(output["stck_prpr"])
        except (TypeError, KeyError, ValueError):
            raise KisApiError("KIS quote response was invalid") from None
        if price <= 0:
            raise KisApiError("KIS quote response contained a non-positive price")

        def optional_int(key: str) -> int | None:
            value = output.get(key) if isinstance(output, dict) else None
            if value in (None, ""):
                return None
            try:
                number = int(value)
            except (TypeError, ValueError):
                return None
            return number if number >= 0 else None

        quote = Quote(
            symbol=symbol,
            observed_at=datetime.now(KST),
            price=price,
            open=optional_int("stck_oprc"),
            high=optional_int("stck_hgpr"),
            low=optional_int("stck_lwpr"),
            volume=optional_int("acml_vol"),
            turnover_krw=optional_int("acml_tr_pbmn"),
        )
        if quote.low is not None and quote.low > min(quote.price, quote.open or quote.price):
            raise KisApiError("KIS quote response contained inconsistent low price")
        if quote.high is not None and quote.high < max(quote.price, quote.open or quote.price):
            raise KisApiError("KIS quote response contained inconsistent high price")
        return quote

    def get_current_price(self, symbol: str) -> int:
        return self.get_quote(symbol).price

    def get_market_activity_rank(
        self,
        *,
        sort_by: Literal["volume", "turnover"],
        limit: int = 50,
        min_price: int = 1_000,
        max_price: int = 50_000,
    ) -> list[MarketActivity]:
        """Return KIS rank candidates; ranks only prioritize deep scans, never create entries."""
        if sort_by not in {"volume", "turnover"}:
            raise ValueError("sort_by must be volume or turnover")
        if not 1 <= limit <= 100 or min_price <= 0 or max_price < min_price:
            raise ValueError("invalid rank limit or price range")
        payload = self._get(
            VOLUME_RANK_PATH,
            "FHPST01710000",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "1",
                "FID_BLNG_CLS_CODE": "0" if sort_by == "volume" else "3",
                "FID_TRGT_CLS_CODE": "000000000",
                "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                "FID_INPUT_PRICE_1": str(min_price),
                "FID_INPUT_PRICE_2": str(max_price),
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": "",
            },
        )
        rows = payload.get("output", [])
        if not isinstance(rows, list):
            raise KisApiError("KIS market-rank response was invalid")

        def integer(row: dict[str, Any], *keys: str) -> int | None:
            for key in keys:
                value = row.get(key)
                if value in (None, ""):
                    continue
                try:
                    parsed = int(str(value).replace(",", ""))
                except ValueError:
                    continue
                if parsed >= 0:
                    return parsed
            return None

        result: list[MarketActivity] = []
        source_name = f"KIS_{sort_by}_rank"
        for rank, row in enumerate(rows[:limit], start=1):
            if not isinstance(row, dict):
                continue
            symbol = ""
            for key in ("mksc_shrn_iscd", "stck_shrn_iscd", "pdno"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    symbol = value.strip()
                    break
            price = integer(row, "stck_prpr", "prpr")
            volume = integer(row, "acml_vol", "cntg_vol", "vol")
            turnover = integer(row, "acml_tr_pbmn", "tr_pbmn")
            if len(symbol) != 6 or not symbol.isdigit() or not price or volume is None or turnover is None:
                continue
            result.append(MarketActivity(symbol, price, volume, turnover, source_name, rank))
        if rows and not result:
            raise KisApiError("KIS market-rank response lacked required symbol, price, volume, or turnover fields")
        return result

    @staticmethod
    def _daily_bar(row: dict[str, Any]) -> Bar:
        return Bar(
            time=datetime.strptime(row["stck_bsop_date"], "%Y%m%d").replace(tzinfo=KST),
            open=float(row["stck_oprc"]),
            high=float(row["stck_hgpr"]),
            low=float(row["stck_lwpr"]),
            close=float(row["stck_clpr"]),
            volume=int(row["acml_vol"]),
        )

    def get_daily_bars(
        self, symbol: str, start: date, end: date, *, adjusted: bool = False
    ) -> list[Bar]:
        """Fetch daily OHLCV; KIS flag 0 requests adjusted and 1 requests raw/original prices."""
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("symbol must be a six-digit KRX code")
        if start > end:
            raise ValueError("start date must not be after end date")
        all_rows: dict[str, dict[str, Any]] = {}
        current_end = end
        for _ in range(20):
            payload = self._get(
                DAILY_PATH,
                "FHKST03010100",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": current_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
                },
            )
            rows = payload.get("output2", [])
            if not isinstance(rows, list) or not rows:
                break
            valid_rows = [r for r in rows if isinstance(r, dict) and r.get("stck_bsop_date")]
            for row in valid_rows:
                all_rows[row["stck_bsop_date"]] = row
            oldest = min(row["stck_bsop_date"] for row in valid_rows) if valid_rows else ""
            if not oldest or oldest <= start.strftime("%Y%m%d") or len(valid_rows) < 100:
                break
            current_end = date.fromisoformat(oldest[:4] + "-" + oldest[4:6] + "-" + oldest[6:]) - timedelta(days=1)
        bars = []
        for key, row in all_rows.items():
            if start.strftime("%Y%m%d") <= key <= end.strftime("%Y%m%d"):
                try:
                    bars.append(self._daily_bar(row))
                except (KeyError, TypeError, ValueError):
                    raise KisApiError("KIS daily-bar response contained invalid OHLCV") from None
        ordered = sorted(bars, key=lambda bar: bar.time)
        self._confirmed_daily_sessions.update((symbol, bar.time.date()) for bar in ordered)
        return ordered

    def get_index_bars(self, index_code: str, start: date, end: date) -> list[Bar]:
        if index_code not in {"0001", "1001"}:
            raise ValueError("index_code must be 0001 (KOSPI) or 1001 (KOSDAQ)")
        if start > end:
            raise ValueError("start date must not be after end date")
        if (end - start).days > 365:
            raise ValueError("index history is limited to a one-year range")
        all_rows: dict[str, dict[str, Any]] = {}
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=34))
            payload = self._get(
                INDEX_DAILY_PATH,
                "FHKUP03500100",
                {
                    "FID_COND_MRKT_DIV_CODE": "U",
                    "FID_INPUT_ISCD": index_code,
                    "FID_INPUT_DATE_1": chunk_start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": chunk_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                },
            )
            rows = payload.get("output2", [])
            if not isinstance(rows, list):
                raise KisApiError("KIS index-bar response was invalid")
            valid_rows = [row for row in rows if isinstance(row, dict) and row.get("stck_bsop_date")]
            for row in valid_rows:
                session_text = row["stck_bsop_date"]
                if chunk_start.strftime("%Y%m%d") <= session_text <= chunk_end.strftime("%Y%m%d"):
                    all_rows[session_text] = row
            chunk_start = chunk_end + timedelta(days=1)
        bars = []
        for key, row in all_rows.items():
            if start.strftime("%Y%m%d") <= key <= end.strftime("%Y%m%d"):
                try:
                    bars.append(
                        Bar(
                            time=datetime.strptime(row["stck_bsop_date"], "%Y%m%d").replace(tzinfo=KST),
                            open=float(row["bstp_nmix_oprc"]),
                            high=float(row["bstp_nmix_hgpr"]),
                            low=float(row["bstp_nmix_lwpr"]),
                            close=float(row["bstp_nmix_prpr"]),
                            volume=int(row.get("acml_vol", 0) or 0),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    raise KisApiError("KIS index-bar response contained invalid OHLCV") from None
        return sorted(bars, key=lambda bar: bar.time)

    def get_minute_bars(
        self,
        symbol: str,
        session_date: date,
        *,
        max_pages: int = 10,
    ) -> list[Bar]:
        """Fetch verified KRX regular-session minute bars using KIS start-labeled timestamps."""
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("symbol must be a six-digit KRX code")
        if MINUTE_TIMESTAMP_CONVENTION != "bar_start":
            raise KisApiError("KIS minute timestamp convention is not confirmed")
        # KIS may return the latest available session even when the requested day is a holiday.
        # Its minute output has no date field, so confirm the exact date against the daily series.
        if (symbol, session_date) not in self._confirmed_daily_sessions and not any(
            bar.time.date() == session_date
            for bar in self.get_daily_bars(symbol, session_date, session_date)
        ):
            raise KisApiError("KIS has no daily bar for the requested minute session; refusing ambiguous minute data")
        date_text = session_date.strftime("%Y%m%d")
        all_rows: dict[str, dict[str, Any]] = {}
        cursor = "153000"
        for _ in range(max(1, min(max_pages, 10))):
            payload = self._get(
                MINUTE_PATH,
                "FHKST03010230",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_HOUR_1": cursor,
                    "FID_INPUT_DATE_1": date_text,
                    "FID_PW_DATA_INCU_YN": "N",
                    "FID_FAKE_TICK_INCU_YN": "",
                },
            )
            rows = payload.get("output2", [])
            if not isinstance(rows, list) or not rows:
                break
            valid_rows = [
                row for row in rows
                if isinstance(row, dict) and row.get("stck_cntg_hour") and row["stck_cntg_hour"] <= cursor
            ]
            for row in valid_rows:
                all_rows[row["stck_cntg_hour"]] = row
            oldest = min((row["stck_cntg_hour"] for row in valid_rows), default="")
            if not oldest or oldest <= "090000" or len(valid_rows) < 120:
                break
            if oldest >= cursor:
                break
            cursor = oldest
        bars = []
        for time_text, row in all_rows.items():
            try:
                timestamp = datetime.strptime(date_text + time_text, "%Y%m%d%H%M%S").replace(tzinfo=KST)
                # Observed KIS rows are labeled 09:00, 09:01, ...; 09:00 matches
                # the regular-session open. KRX continuous trading ends at 15:20; keep
                # start-labeled bars only before that auction boundary.
                if not ("090000" <= time_text < "152000"):
                    continue
                bars.append(
                    Bar(
                        time=timestamp,
                        open=float(row["stck_oprc"]),
                        high=float(row["stck_hgpr"]),
                        low=float(row["stck_lwpr"]),
                        close=float(row["stck_prpr"]),
                        volume=int(row["cntg_vol"]),
                    )
                )
            except (KeyError, ValueError, TypeError):
                raise KisApiError("KIS minute-bar response contained invalid OHLCV") from None
        return sorted(bars, key=lambda bar: bar.time)

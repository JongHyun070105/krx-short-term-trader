from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from krx_trader.kis.auth import PRODUCTION_BASE_URL, TokenManager
from krx_trader.kis.transport import JsonTransport, TransportError
from krx_trader.models import Bar

KST = ZoneInfo("Asia/Seoul")
DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
INDEX_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"


class KisApiError(RuntimeError):
    pass


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
        max_retries: int = 3,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._tokens = token_manager
        self._transport = transport
        self._sleep = sleeper
        self._jitter = jitter
        self._max_retries = max(0, max_retries)

    def _get(self, path: str, tr_id: str, params: dict[str, str]) -> dict[str, Any]:
        refreshed = False
        attempt = 0
        while True:
            token = self._tokens.get_token()
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
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                self._sleep(self._jitter(0, 0.25) + 0.25 * (2**attempt))
                attempt += 1
                continue
            if response.status_code != 200 or payload.get("rt_cd") not in {None, "0", 0}:
                code = payload.get("msg_cd")
                suffix = f" code={code}" if isinstance(code, str) else ""
                raise KisApiError(f"KIS read request failed (HTTP {response.status_code}){suffix}")
            return payload

    def get_current_price(self, symbol: str) -> int:
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
            raise KisApiError("KIS current-price response was invalid") from None
        if price <= 0:
            raise KisApiError("KIS current-price response contained a non-positive price")
        return price

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

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
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
                    "FID_ORG_ADJ_PRC": "1",
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
        return sorted(bars, key=lambda bar: bar.time)

    def get_index_bars(self, index_code: str, start: date, end: date) -> list[Bar]:
        if index_code not in {"0001", "1001"}:
            raise ValueError("index_code must be 0001 (KOSPI) or 1001 (KOSDAQ)")
        if start > end:
            raise ValueError("start date must not be after end date")
        all_rows: dict[str, dict[str, Any]] = {}
        current_end = end
        for _ in range(10):
            payload = self._get(
                INDEX_DAILY_PATH,
                "FHKUP03500100",
                {
                    "FID_COND_MRKT_DIV_CODE": "U",
                    "FID_INPUT_ISCD": index_code,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": current_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                },
            )
            rows = payload.get("output2", [])
            if not isinstance(rows, list) or not rows:
                break
            valid_rows = [row for row in rows if isinstance(row, dict) and row.get("stck_bsop_date")]
            for row in valid_rows:
                all_rows[row["stck_bsop_date"]] = row
            oldest = min(row["stck_bsop_date"] for row in valid_rows) if valid_rows else ""
            if not oldest or oldest <= start.strftime("%Y%m%d") or len(valid_rows) < 100:
                break
            current_end = date.fromisoformat(f"{oldest[:4]}-{oldest[4:6]}-{oldest[6:8]}") - timedelta(days=1)
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
        timestamp_convention: Literal["bar_start", "bar_end"] | None = None,
        max_pages: int = 10,
    ) -> list[Bar]:
        """Fetch minute bars; caller must confirm the provider timestamp convention first."""
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("symbol must be a six-digit KRX code")
        if timestamp_convention not in {"bar_start", "bar_end"}:
            raise ValueError("timestamp_convention must be explicitly confirmed as bar_start or bar_end")
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
                    "FID_PW_DATA_INCU_YN": "Y",
                    "FID_FAKE_TICK_INCU_YN": "",
                },
            )
            rows = payload.get("output2", [])
            if not isinstance(rows, list) or not rows:
                break
            valid_rows = [row for row in rows if isinstance(row, dict) and row.get("stck_cntg_hour")]
            for row in valid_rows:
                all_rows[row["stck_cntg_hour"]] = row
            oldest = min((row["stck_cntg_hour"] for row in valid_rows), default="")
            if not oldest or oldest <= "090000" or len(valid_rows) < 120:
                break
            cursor = oldest
        bars = []
        for time_text, row in all_rows.items():
            try:
                timestamp = datetime.strptime(date_text + time_text, "%Y%m%d%H%M%S").replace(tzinfo=KST)
                if timestamp_convention == "bar_end":
                    timestamp -= timedelta(minutes=1)
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

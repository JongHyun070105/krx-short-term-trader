from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.exceptions import WebSocketException

from krx_trader.kis.auth import KisAuthError, TokenManager

PRODUCTION_WS_URL = "ws://ops.koreainvestment.com:21000"
REALTIME_TRADE_TR_ID = "H0STCNT0"


def subscription_message(approval_key: str, symbol: str, *, subscribe: bool = True) -> dict[str, Any]:
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("symbol must be a six-digit KRX code")
    return {
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1" if subscribe else "2",
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": REALTIME_TRADE_TR_ID, "tr_key": symbol}},
    }


class KisWebSocketClient:
    """Production quote stream with reconnect, heartbeat, stale detection, and shutdown."""

    def __init__(
        self,
        token_manager: TokenManager,
        *,
        endpoint: str = PRODUCTION_WS_URL,
        connect_factory: Callable[..., Any] | None = None,
        stale_after_seconds: float = 30.0,
        max_backoff_seconds: float = 30.0,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._tokens = token_manager
        self._endpoint = endpoint
        self._connect_factory = connect_factory
        self._stale_after = stale_after_seconds
        self._max_backoff = max_backoff_seconds
        self._jitter = jitter
        self._stop = asyncio.Event()
        self._subscriptions: set[str] = set()

    async def subscribe(self, websocket: Any, symbol: str, approval_key: str) -> None:
        await websocket.send(json.dumps(subscription_message(approval_key, symbol, subscribe=True)))
        self._subscriptions.add(symbol)

    async def unsubscribe(self, websocket: Any, symbol: str, approval_key: str) -> None:
        if symbol not in self._subscriptions:
            return
        await websocket.send(json.dumps(subscription_message(approval_key, symbol, subscribe=False)))
        self._subscriptions.discard(symbol)

    async def close(self) -> None:
        self._stop.set()

    async def run(
        self,
        symbols: list[str],
        on_message: Callable[[str], Awaitable[None]],
    ) -> None:
        if not symbols:
            raise ValueError("at least one symbol is required")
        for symbol in symbols:
            if len(symbol) != 6 or not symbol.isdigit():
                raise ValueError("symbol must be a six-digit KRX code")
        if self._connect_factory is None:
            from websockets.asyncio.client import connect

            connector = connect
        else:
            connector = self._connect_factory
        attempt = 0
        while not self._stop.is_set():
            try:
                approval = await asyncio.to_thread(self._tokens.get_approval_key)
                async with connector(self._endpoint, ping_interval=20, ping_timeout=10) as websocket:
                    self._subscriptions.clear()
                    for symbol in symbols:
                        await self.subscribe(websocket, symbol, approval)
                    attempt = 0
                    loop = asyncio.get_running_loop()
                    last_message = loop.time()
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=min(5.0, self._stale_after))
                            last_message = loop.time()
                            if isinstance(raw, str):
                                try:
                                    control = json.loads(raw)
                                except json.JSONDecodeError:
                                    control = {}
                                if control.get("header", {}).get("tr_id") == "PINGPONG":
                                    await websocket.pong(raw.encode("utf-8"))
                                    continue
                                await on_message(raw)
                        except TimeoutError:
                            if loop.time() - last_message >= self._stale_after:
                                raise ConnectionError("KIS quote stream is stale")
            except asyncio.CancelledError:
                raise
            except KisAuthError:
                self._subscriptions.clear()
                raise
            except (OSError, TimeoutError, WebSocketException):
                if self._stop.is_set():
                    break
                delay = min(self._max_backoff, 0.5 * (2**min(attempt, 8)))
                await asyncio.sleep(delay + self._jitter(0, min(0.5, delay / 4)))
                attempt += 1
        self._subscriptions.clear()

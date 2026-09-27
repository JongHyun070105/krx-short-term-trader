from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TransportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes

    def json(self) -> dict[str, Any]:
        value = json.loads(self.body.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("API response was not a JSON object")
        return value


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> HttpResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        if params:
            url = f"{url}?{urlencode(params)}"
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request = Request(url, data=body, headers=headers or {}, method=method.upper())
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read())
        except HTTPError as exc:
            return HttpResponse(exc.code, exc.read())
        except URLError as exc:
            raise TransportError(f"network request failed ({type(exc.reason).__name__})") from None

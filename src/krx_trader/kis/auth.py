from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from krx_trader.kis.transport import JsonTransport, TransportError

KST = ZoneInfo("Asia/Seoul")
PRODUCTION_BASE_URL = "https://openapi.koreainvestment.com:9443"


class KisAuthError(RuntimeError):
    pass


class TokenManager:
    """Caches a production REST token and refreshes it shortly before expiration."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        transport: JsonTransport,
        cache_path: Path = Path("runtime/kis_token.json"),
        *,
        now=None,
        refresh_before: timedelta = timedelta(minutes=5),
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._transport = transport
        self._cache_path = cache_path
        self._now = now or (lambda: datetime.now(KST))
        self._refresh_before = refresh_before
        self._fingerprint = hashlib.sha256(app_key.encode()).hexdigest()

    def _read_cache(self) -> tuple[str, datetime] | None:
        try:
            record = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if record.get("app_fingerprint") != self._fingerprint:
                return None
            token = record["access_token"]
            expires_at = datetime.fromisoformat(record["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=KST)
            if not isinstance(token, str) or not token:
                return None
            return token, expires_at.astimezone(KST)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_cache(self, token: str, expires_at: datetime) -> None:
        parent = self._cache_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
        payload = json.dumps(
            {
                "app_fingerprint": self._fingerprint,
                "access_token": token,
                "expires_at": expires_at.astimezone(KST).isoformat(),
            }
        )
        fd = os.open(self._cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        try:
            os.chmod(self._cache_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _expiration(payload: dict[str, object], now: datetime) -> datetime:
        raw = payload.get("access_token_token_expired")
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw)
                return (parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed).astimezone(KST)
            except ValueError:
                pass
        ttl = payload.get("expires_in", 86_400)
        try:
            seconds = max(60, int(ttl))
        except (ValueError, TypeError):
            seconds = 86_400
        return now + timedelta(seconds=seconds)

    def get_token(self) -> str:
        now = self._now().astimezone(KST)
        cached = self._read_cache()
        if cached and cached[1] > now + self._refresh_before:
            return cached[0]
        if not self._app_key or not self._app_secret:
            raise KisAuthError("KIS credentials are not configured")
        try:
            response = self._transport.request(
                "POST",
                f"{PRODUCTION_BASE_URL}/oauth2/tokenP",
                headers={"content-type": "application/json"},
                json_body={
                    "grant_type": "client_credentials",
                    "appkey": self._app_key,
                    "appsecret": self._app_secret,
                },
            )
            payload = response.json()
        except (TransportError, ValueError, OSError):
            raise KisAuthError("KIS token request failed; details redacted") from None
        if response.status_code != 200:
            raise KisAuthError(f"KIS token request failed (HTTP {response.status_code})")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            code = payload.get("msg_cd")
            suffix = f" code={code}" if isinstance(code, str) else ""
            raise KisAuthError(f"KIS token response was invalid{suffix}")
        expires_at = self._expiration(payload, now)
        self._write_cache(token, expires_at)
        return token

    def invalidate(self, token: str | None = None) -> None:
        cached = self._read_cache()
        if cached and (token is None or cached[0] == token):
            try:
                self._cache_path.unlink()
            except FileNotFoundError:
                pass

    def get_approval_key(self) -> str:
        if not self._app_key or not self._app_secret:
            raise KisAuthError("KIS credentials are not configured")
        try:
            response = self._transport.request(
                "POST",
                f"{PRODUCTION_BASE_URL}/oauth2/Approval",
                headers={"content-type": "application/json"},
                json_body={
                    "grant_type": "client_credentials",
                    "appkey": self._app_key,
                    "secretkey": self._app_secret,
                },
            )
            payload = response.json()
        except (TransportError, ValueError, OSError):
            raise KisAuthError("KIS websocket approval request failed; details redacted") from None
        key = payload.get("approval_key")
        if response.status_code != 200 or not isinstance(key, str) or not key:
            raise KisAuthError(f"KIS websocket approval failed (HTTP {response.status_code})")
        return key

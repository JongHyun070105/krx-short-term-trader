import json
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

from krx_trader.kis.auth import TokenManager
from krx_trader.kis.rest import KisRestClient
from krx_trader.kis.transport import HttpResponse

KST = ZoneInfo("Asia/Seoul")


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def response(payload, status=200):
    return HttpResponse(status, json.dumps(payload).encode())


def test_token_cached_until_near_expiration_and_file_is_private(tmp_path):
    transport = FakeTransport([response({"access_token": "cached-token", "access_token_token_expired": "2026-01-02 00:00:00"})])
    now = datetime(2026, 1, 1, 12, 0, tzinfo=KST)
    manager = TokenManager("app", "app-secret-value", transport, tmp_path / "runtime" / "token.json", now=lambda: now)
    assert manager.get_token() == "cached-token"
    assert manager.get_token() == "cached-token"
    assert len(transport.calls) == 1
    assert stat.S_IMODE((tmp_path / "runtime" / "token.json").stat().st_mode) == 0o600
    body = transport.calls[0][2]["json_body"]
    assert body["appsecret"] == "app-secret-value"
    assert "app-secret-value" not in (tmp_path / "runtime" / "token.json").read_text()


def test_expiring_token_is_refreshed_once(tmp_path):
    transport = FakeTransport([response({"access_token": "new", "access_token_token_expired": "2026-01-02 12:00:00"})])
    path = tmp_path / "runtime" / "token.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "app_fingerprint": __import__("hashlib").sha256(b"app").hexdigest(),
        "access_token": "old",
        "expires_at": "2026-01-01T12:03:00+09:00",
    }))
    now = datetime(2026, 1, 1, 12, 0, tzinfo=KST)
    manager = TokenManager("app", "secret", transport, path, now=lambda: now)
    assert manager.get_token() == "new"
    assert len(transport.calls) == 1


def test_current_price_uses_official_read_endpoint_and_redacts_errors(tmp_path):
    transport = FakeTransport([
        response({"access_token": "tok", "expires_in": 3600}),
        response({"rt_cd": "0", "output": {"stck_prpr": "12345"}}),
    ])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient("app", "secret", manager, transport, sleeper=lambda _: None, jitter=lambda a, b: 0)
    assert client.get_current_price("005930") == 12345
    method, url, kwargs = transport.calls[1]
    assert method == "GET"
    assert url.endswith("/uapi/domestic-stock/v1/quotations/inquire-price")
    assert kwargs["headers"]["tr_id"] == "FHKST01010100"
    assert kwargs["headers"]["appsecret"] == "secret"


def test_invalid_symbol_fails_before_network(tmp_path):
    transport = FakeTransport([])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient("app", "secret", manager, transport)
    try:
        client.get_current_price("bad")
    except ValueError as error:
        assert "six-digit" in str(error)
    else:
        raise AssertionError("invalid symbol accepted")
    assert transport.calls == []


def test_minute_bar_timestamp_convention_must_be_explicit_before_network(tmp_path):
    transport = FakeTransport([])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient("app", "secret", manager, transport)
    try:
        client.get_minute_bars("005930", datetime(2026, 1, 1, tzinfo=KST).date())
    except ValueError as error:
        assert "explicitly confirmed" in str(error)
    else:
        raise AssertionError("unconfirmed minute timestamp convention accepted")
    assert transport.calls == []

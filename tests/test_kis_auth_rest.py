import json
import stat
from datetime import date, datetime, timedelta
from http.client import IncompleteRead
from zoneinfo import ZoneInfo

import pytest

from krx_trader.kis.auth import TokenManager
from krx_trader.kis.rest import KisApiError, KisRestClient
from krx_trader.kis.transport import HttpResponse, TransportError, UrllibTransport

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


def test_token_can_be_cached_in_memory_without_persisting_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    transport = FakeTransport([response({"access_token": "ephemeral-token", "expires_in": 3600})])
    now = datetime(2026, 1, 1, 12, 0, tzinfo=KST)
    manager = TokenManager("app", "secret", transport, cache_path=None, now=lambda: now)

    assert manager.get_token() == "ephemeral-token"
    assert manager.get_token() == "ephemeral-token"
    assert len(transport.calls) == 1
    assert not (tmp_path / "runtime" / "kis_token.json").exists()


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
    client = KisRestClient("app", "secret", manager, transport, sleeper=lambda _: None, jitter=lambda a, b: 0,
                           min_request_interval=0, rate_limit_path=tmp_path / "rate.json")
    assert client.get_current_price("005930") == 12345
    method, url, kwargs = transport.calls[1]
    assert method == "GET"
    assert url.endswith("/uapi/domestic-stock/v1/quotations/inquire-price")
    assert kwargs["headers"]["tr_id"] == "FHKST01010100"
    assert kwargs["headers"]["appsecret"] == "secret"


def test_quote_parses_only_market_fields_and_marks_local_observation_time(tmp_path):
    transport = FakeTransport([
        response({"access_token": "tok", "expires_in": 3600}),
        response({"rt_cd": "0", "output": {
            "stck_prpr": "12345", "stck_oprc": "12000", "stck_hgpr": "12400",
            "stck_lwpr": "11900", "acml_vol": "9876", "acml_tr_pbmn": "120000000",
            "unused_private_field": "ignored",
        }}),
    ])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient("app", "secret", manager, transport, min_request_interval=0,
                           rate_limit_path=tmp_path / "rate.json")
    quote = client.get_quote("005930")
    assert quote.symbol == "005930"
    assert quote.observed_at.tzinfo == KST
    assert (quote.price, quote.open, quote.high, quote.low) == (12345, 12000, 12400, 11900)
    assert (quote.volume, quote.turnover_krw) == (9876, 120000000)


def test_index_history_is_fetched_in_bounded_date_chunks(tmp_path):
    def row(session: str, close: str) -> dict[str, str]:
        return {
            "stck_bsop_date": session,
            "bstp_nmix_oprc": close,
            "bstp_nmix_hgpr": close,
            "bstp_nmix_lwpr": close,
            "bstp_nmix_prpr": close,
            "acml_vol": "0",
        }

    transport = FakeTransport([
        response({"access_token": "tok", "expires_in": 3600}),
        response({"rt_cd": "0", "output2": [row("20260402", "100"), row("20260506", "101")]}),
        response({"rt_cd": "0", "output2": [row("20260506", "101")]}),
    ])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient("app", "secret", manager, transport, min_request_interval=0,
                           rate_limit_path=tmp_path / "rate.json")

    bars = client.get_index_bars("0001", date(2026, 4, 1), date(2026, 5, 10))

    index_calls = [call for call in transport.calls if call[1].endswith("inquire-daily-indexchartprice")]
    assert len(index_calls) == 2
    assert [call[2]["params"]["FID_INPUT_DATE_1"] for call in index_calls] == ["20260401", "20260506"]
    assert [call[2]["params"]["FID_INPUT_DATE_2"] for call in index_calls] == ["20260505", "20260510"]
    assert [bar.time.date().isoformat() for bar in bars] == ["2026-04-02", "2026-05-06"]


def test_invalid_symbol_fails_before_network(tmp_path):
    transport = FakeTransport([])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient("app", "secret", manager, transport, rate_limit_path=tmp_path / "rate.json")
    try:
        client.get_current_price("bad")
    except ValueError as error:
        assert "six-digit" in str(error)
    else:
        raise AssertionError("invalid symbol accepted")
    assert transport.calls == []


def test_minute_bars_reject_requested_day_without_daily_session(tmp_path):
    transport = FakeTransport([
        response({"access_token": "tok", "expires_in": 3600}),
        response({"rt_cd": "0", "output2": []}),
    ])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient("app", "secret", manager, transport, min_request_interval=0,
                           rate_limit_path=tmp_path / "rate.json")
    try:
        client.get_minute_bars("005930", date(2026, 9, 25))
    except KisApiError as error:
        assert "no daily bar" in str(error)
    else:
        raise AssertionError("ambiguous minute data accepted for a non-session date")
    assert len(transport.calls) == 2  # token plus exact-date daily confirmation


def _minute_row(time_text: str) -> dict[str, str]:
    return {
        "stck_cntg_hour": time_text,
        "stck_oprc": "100", "stck_hgpr": "101", "stck_lwpr": "99",
        "stck_prpr": "100", "cntg_vol": "1",
    }


def _minutes(start: str, end: str) -> list[str]:
    current = datetime.strptime(start, "%H%M%S").replace(tzinfo=KST)
    finish = datetime.strptime(end, "%H%M%S").replace(tzinfo=KST)
    values = []
    while current <= finish:
        values.append(current.strftime("%H%M%S"))
        current += timedelta(minutes=1)
    return values


def test_minute_bars_use_start_labels_and_ignore_wrapped_pages(tmp_path):
    day = "20260923"
    daily = {"rt_cd": "0", "output2": [{
        "stck_bsop_date": day, "stck_oprc": "100", "stck_hgpr": "101",
        "stck_lwpr": "99", "stck_clpr": "100", "acml_vol": "390",
    }]}
    page_times = [
        _minutes("133100", "153000"),
        _minutes("113200", "133100"),
        _minutes("093300", "113200"),
        _minutes("090000", "093300") + _minutes("153100", "165600"),
    ]
    pages = [
        response({"rt_cd": "0", "output2": [_minute_row(value) for value in reversed(times)]})
        for times in page_times
    ]
    transport = FakeTransport([response({"access_token": "tok", "expires_in": 3600}), response(daily), *pages])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient("app", "secret", manager, transport, min_request_interval=0,
                           rate_limit_path=tmp_path / "rate.json")

    bars = client.get_minute_bars("005930", date(2026, 9, 23))

    assert len(bars) == 380
    assert bars[0].time == datetime(2026, 9, 23, 9, 0, tzinfo=KST)
    assert bars[-1].time == datetime(2026, 9, 23, 15, 19, tzinfo=KST)
    assert all(bar.time.date() == date(2026, 9, 23) for bar in bars)
    minute_calls = [call for call in transport.calls if call[1].endswith("inquire-time-dailychartprice")]
    assert len(minute_calls) == 4
    assert all(call[2]["params"]["FID_PW_DATA_INCU_YN"] == "N" for call in minute_calls)


def test_request_limiter_spaces_successful_gets(tmp_path):
    sleeps: list[float] = []
    clock = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    transport = FakeTransport([
        response({"access_token": "tok", "expires_in": 3600}),
        response({"rt_cd": "0", "output": {"stck_prpr": "12345"}}),
        response({"rt_cd": "0", "output": {"stck_prpr": "12345"}}),
    ])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient(
        "app", "secret", manager, transport, sleeper=sleep, monotonic=lambda: clock[0],
        min_request_interval=0.25, rate_limit_path=tmp_path / "rate.json", wall_clock=lambda: clock[0],
    )
    assert client.get_current_price("005930") == 12345
    assert client.get_current_price("005930") == 12345
    assert sleeps == [0.25]


def test_per_second_rate_limit_error_gets_bounded_retry(tmp_path):
    sleeps: list[float] = []
    transport = FakeTransport([
        response({"access_token": "tok", "expires_in": 3600}),
        response({"rt_cd": "1", "msg_cd": "EGW00201"}),
        response({"rt_cd": "0", "output": {"stck_prpr": "12345"}}),
    ])
    manager = TokenManager("app", "secret", transport, tmp_path / "token.json")
    client = KisRestClient(
        "app", "secret", manager, transport, sleeper=sleeps.append, jitter=lambda _a, _b: 0,
        min_request_interval=0, max_retries=1, rate_limit_path=tmp_path / "rate.json",
        wall_clock=lambda: 0.0,
    )
    assert client.get_current_price("005930") == 12345
    assert sleeps == [61.0]
    assert len(transport.calls) == 3


def test_incomplete_http_body_becomes_redacted_retryable_transport_error(monkeypatch):
    class IncompleteResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            raise IncompleteRead(b"private-response-fragment", 100)

    monkeypatch.setattr("krx_trader.kis.transport.urlopen", lambda *_args, **_kwargs: IncompleteResponse())

    with pytest.raises(TransportError, match=r"network response failed \(IncompleteRead\)") as error:
        UrllibTransport().request("GET", "https://example.invalid")

    assert "private-response-fragment" not in str(error.value)

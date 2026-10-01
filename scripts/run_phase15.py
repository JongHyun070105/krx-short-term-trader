from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from krx_trader.config import Settings
from krx_trader.kis.auth import TokenManager
from krx_trader.kis.rest import (
    DAILY_PATH,
    INVESTOR_FLOW_DAILY_PATH,
    MARKET_INVESTOR_FLOW_DAILY_PATH,
    PROGRAM_FLOW_DAILY_PATH,
    KisRestClient,
)
from krx_trader.kis.transport import UrllibTransport
from krx_trader.research.phase15 import (
    append_cache_record,
    assert_phase15_source_date,
    build_artifact_index,
    canonical_response_hash,
    classify_delisted_support,
    classify_historical_depth,
    classify_response_hashes,
    compare_artifact_snapshot,
    estimate_acquisition,
    proposed_factor_registry,
    read_cached_request,
    request_manifest_record,
    validate_pilot_rows,
    validate_scorecard,
    verify_artifact_index,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runtime/research/phase15"
CACHE_DIR = OUT / "pilot-cache"
PILOT_DATES = {
    "2019": date(2019, 6, 3),
    "2020": date(2020, 6, 2),
    "2021": date(2021, 6, 1),
    "2022": date(2022, 6, 2),
    "2023": date(2023, 6, 1),
    "2024": date(2024, 6, 3),
    "2025": date(2025, 6, 2),
}
SYMBOLS = {
    "KOSPI": ["005930", "000660", "005380"],
    "KOSDAQ": ["086520", "028300", "293490"],
}
DELISTED = [
    {"symbol": "003560", "name": "I-HQ", "market": "KOSPI", "delisted": "2024-12-20"},
    {"symbol": "181340", "name": "IzMedia", "market": "KOSDAQ", "delisted": "2024-07-10"},
]
DOCS = {
    "kis_catalog": "https://apiportal.koreainvestment.com/apiservice-category",
    "kis_usage": "https://apiportal.koreainvestment.com/about-open-api",
    "kis_flow": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/investor_trade_by_stock_daily/investor_trade_by_stock_daily.py",
    "kis_market_flow": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_investor_daily_by_market/inquire_investor_daily_by_market.py",
    "kis_program": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/program_trade_by_stock_daily/program_trade_by_stock_daily.py",
    "krx_market_flow": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT022",
    "krx_home": "https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd?locale=ko",
    "krx_usage": "https://data.krx.co.kr/inc/datasale/Market%20Data%20Usage%20Polices_ko.pdf",
    "opendart_guide": "https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001",
    "kind_ihq_delisting": "https://kind.krx.co.kr/external/2024/12/05/000515/20241205001470/68051.htm",
    "kind_izmedia_delisting": "https://kind.krx.co.kr/external/2024/06/27/000865/20240627002013/70769.htm",
}


def git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def output_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("output1", "output2", "output"):
        value = payload.get(key)
        if isinstance(value, dict):
            rows.append(value)
        elif isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    return rows


def date_fields(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    fields = sorted({key for row in rows for key in row if "date" in key.lower() or "bsop" in key.lower()})
    values: set[str] = set()
    for row in rows:
        for field in fields:
            raw = row.get(field)
            if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
                values.add(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
    return fields, sorted(values)


def make_client() -> KisRestClient | None:
    settings = Settings.from_env()
    status = settings.credential_status()
    if not status["KIS_APP_KEY"] or not status["KIS_APP_SECRET"]:
        print("KIS pilot unavailable: app credentials not configured (values not read or printed).")
        return None
    transport = UrllibTransport()
    # Keep the access token in process memory only; no token cache file is created.
    token_manager = TokenManager(settings.kis_app_key, settings.kis_app_secret, transport, cache_path=None)
    return KisRestClient(
        settings.kis_app_key,
        settings.kis_app_secret,
        token_manager,
        transport,
        min_request_interval=4.0,
        rate_limit_path=ROOT / "runtime/kis_rate_limit.json",
    )


def cache_path(source: str) -> Path:
    return CACHE_DIR / f"{source}.jsonl"


def phase15_request_manifest(**kwargs) -> dict[str, Any]:
    return {
        **request_manifest_record(**kwargs),
        "provider": "Korea Investment & Securities",
        "source_git_sha": git_sha(),
        "qualification_logic_version": 1,
    }


def request_record(
    *,
    source: str,
    endpoint: str,
    params: dict[str, str],
    symbol: str | None,
    day: date,
    round_number: int,
    call,
) -> dict[str, Any]:
    assert_phase15_source_date(day)
    manifest = phase15_request_manifest(
        source=source, endpoint=endpoint, parameters=params, symbol=symbol, requested_date=day
    )
    source_cache = cache_path(source)
    cached = read_cached_request(source_cache, manifest["request_sha256"], round_number)
    if cached is not None and cached.get("status") == "OK":
        cached_days = cached.get("returned_dates", [])
        cached_dq = cached.setdefault("dq", {})
        cached_dq["requested_date_matches"] = (
            max(cached_days) == day.isoformat() and all(item <= day.isoformat() for item in cached_days)
        ) if cached_days else None
        if cached_days:
            cached_dq["returned_window_start"] = min(cached_days)
            cached_dq["returned_window_end"] = max(cached_days)
        cached.update(manifest)
        cached["dq_policy_version"] = 3
        append_cache_record(source_cache, cached)
        return cached

    try:
        payload = call()
        rows = output_rows(payload)
        fields, returned_dates = date_fields(rows)
        selected_date_field = next(
            (field for field in fields if field in {"stck_bsop_date", "bsop_date", "date"}), None
        )
        dq = (
            validate_pilot_rows(
                rows,
                requested_date=day,
                date_field=selected_date_field,
                allow_prior_sessions=True,
            )
            if selected_date_field
            else {
                "row_count": len(rows),
                "duplicate_rows": 0,
                "returned_dates": returned_dates,
                "requested_date_matches": None,
                "invalid_numeric_fields": [],
                "negative_impossible_fields": [],
                "missing_is_preserved": True,
            }
        )
        if returned_dates:
            dq["returned_window_start"] = min(returned_dates)
            dq["returned_window_end"] = max(returned_dates)
        safe_payload = {key: value for key, value in payload.items() if key != "_phase15_pages"}
        record = {
            **manifest,
            "query_round": round_number,
            "dq_policy_version": 3,
            "status": "OK",
            "http_endpoint": endpoint,
            "source_date_fields": fields,
            "response_field_names": sorted({key for row in rows for key in row}),
            "returned_dates": returned_dates,
            "category_field_names": sorted(
                {
                    key
                    for row in rows
                    for key in row
                    if any(token in key.lower() for token in ("frgn", "fore", "orgn", "inst", "prsn", "program", "arbitr"))
                }
            ),
            "response_row_count": len(rows),
            "page_count": payload.get("_phase15_pages", 1),
            "dq": dq,
            "response_sha256": hashlib.sha256(
                json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest(),
            "response_sha256_basis": "compact JSON reserialization of decoded response; exact wire bytes are unavailable in the current transport interface",
            "canonical_response_sha256": canonical_response_hash(safe_payload),
            "generated_at_utc": datetime.now(UTC).isoformat(),
        }
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        # Provider messages may echo request context, so persist only the exception type.
        record = {
            **manifest,
            "query_round": round_number,
            "dq_policy_version": 3,
            "status": "UNAVAILABLE",
            "error_type": type(exc).__name__,
            "generated_at_utc": datetime.now(UTC).isoformat(),
        }
    append_cache_record(source_cache, record)
    return record


def run_pilot() -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = make_client()
    records: list[dict[str, Any]] = []

    def invoke(source, endpoint, params, symbol, day, round_number, fn):
        if client is None:
            manifest = phase15_request_manifest(
                source=source, endpoint=endpoint, parameters=params, symbol=symbol, requested_date=day
            )
            record = {**manifest, "query_round": round_number, "status": "UNAVAILABLE", "error_type": "CredentialsNotConfigured"}
        else:
            record = request_record(
                source=source,
                endpoint=endpoint,
                params=params,
                symbol=symbol,
                day=day,
                round_number=round_number,
                call=fn,
            )
        records.append(record)

    flow_endpoint = INVESTOR_FLOW_DAILY_PATH
    for market_symbols in SYMBOLS.values():
        for symbol in market_symbols:
            for day in PILOT_DATES.values():
                invoke(
                    "kis_per_stock_investor_flow",
                    flow_endpoint,
                    {
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": symbol,
                        "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
                        "FID_ORG_ADJ_PRC": "",
                        "FID_ETC_CLS_CODE": "",
                    },
                    symbol,
                    day,
                    1,
                    lambda symbol=symbol, day=day: client.get_investor_flow_by_date(symbol, day),
                )

    # A single oldest-date probe and a repeated query in every sampled year.
    invoke(
        "kis_per_stock_investor_flow",
        flow_endpoint,
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": "005930",
            "FID_INPUT_DATE_1": "20180601",
            "FID_ORG_ADJ_PRC": "",
            "FID_ETC_CLS_CODE": "",
        },
        "005930",
        date(2018, 6, 1),
        1,
        lambda: client.get_investor_flow_by_date("005930", date(2018, 6, 1)),
    )
    for index, day in enumerate(PILOT_DATES.values()):
        symbol = SYMBOLS["KOSPI" if index % 2 == 0 else "KOSDAQ"][index % 3]
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
            "FID_ORG_ADJ_PRC": "",
            "FID_ETC_CLS_CODE": "",
        }
        invoke(
            "kis_per_stock_investor_flow",
            flow_endpoint,
            params,
            symbol,
            day,
            2,
            lambda symbol=symbol, day=day: client.get_investor_flow_by_date(symbol, day),
        )

    for market_code, day in (("KSP", PILOT_DATES["2020"]), ("KSQ", PILOT_DATES["2024"])):
        index_code = "0001" if market_code == "KSP" else "1001"
        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": "0001",
            "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
            "FID_INPUT_ISCD_1": market_code,
            "FID_INPUT_DATE_2": day.strftime("%Y%m%d"),
            "FID_INPUT_ISCD_2": index_code,
        }
        invoke(
            "kis_market_investor_flow",
            MARKET_INVESTOR_FLOW_DAILY_PATH,
            params,
            None,
            day,
            1,
            lambda market_code=market_code, day=day: client.get_market_investor_flow_by_date(market_code, day),
        )

    for symbol, day in (("005930", PILOT_DATES["2021"]), ("000660", PILOT_DATES["2024"])):
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
        }
        invoke(
            "kis_program_trading",
            PROGRAM_FLOW_DAILY_PATH,
            params,
            symbol,
            day,
            1,
            lambda symbol=symbol, day=day: client.get_program_flow_by_date(symbol, day),
        )

    market_repeat_day = PILOT_DATES["2020"]
    market_repeat_params = {
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": "0001",
        "FID_INPUT_DATE_1": market_repeat_day.strftime("%Y%m%d"),
        "FID_INPUT_ISCD_1": "KSP",
        "FID_INPUT_DATE_2": market_repeat_day.strftime("%Y%m%d"),
        "FID_INPUT_ISCD_2": "0001",
    }
    invoke(
        "kis_market_investor_flow",
        MARKET_INVESTOR_FLOW_DAILY_PATH,
        market_repeat_params,
        None,
        market_repeat_day,
        2,
        lambda: client.get_market_investor_flow_by_date("KSP", market_repeat_day),
    )
    program_repeat_day = PILOT_DATES["2021"]
    program_repeat_params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": "005930",
        "FID_INPUT_DATE_1": program_repeat_day.strftime("%Y%m%d"),
    }
    invoke(
        "kis_program_trading",
        PROGRAM_FLOW_DAILY_PATH,
        program_repeat_params,
        "005930",
        program_repeat_day,
        2,
        lambda: client.get_program_flow_by_date("005930", program_repeat_day),
    )

    for item, query_day in zip(DELISTED, (date(2023, 11, 1), date(2023, 6, 1)), strict=True):
        symbol = item["symbol"]
        flow_params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": query_day.strftime("%Y%m%d"),
            "FID_ORG_ADJ_PRC": "",
            "FID_ETC_CLS_CODE": "",
        }
        invoke(
            "kis_delisted_symbol_flow",
            flow_endpoint,
            flow_params,
            symbol,
            query_day,
            1,
            lambda symbol=symbol, query_day=query_day: client.get_investor_flow_by_date(symbol, query_day),
        )
        price_params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": query_day.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": query_day.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1",
        }
        price_source = "kis_delisted_symbol_price_history"
        price_manifest = phase15_request_manifest(
            source=price_source,
            endpoint=DAILY_PATH,
            parameters=price_params,
            symbol=symbol,
            requested_date=query_day,
        )
        price_cache = cache_path(price_source)
        price_payload = read_cached_request(price_cache, price_manifest["request_sha256"], 1)
        if price_payload is None:
            if client is None:
                price_payload = {
                    **price_manifest,
                    "query_round": 1,
                    "dq_policy_version": 3,
                    "status": "UNAVAILABLE",
                    "error_type": "CredentialsNotConfigured",
                }
            else:
                try:
                    bars = client.get_daily_bars(symbol, query_day, query_day, adjusted=False)
                    bar_summary = [
                        {
                            "date": bar.time.date().isoformat(),
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                        }
                        for bar in bars
                    ]
                    response_hash = canonical_response_hash(bar_summary)
                    price_payload = {
                        **price_manifest,
                        "query_round": 1,
                        "dq_policy_version": 3,
                        "status": "OK",
                        "row_count": len(bars),
                        "returned_dates": [bar.time.date().isoformat() for bar in bars],
                        "response_sha256": response_hash,
                        "canonical_response_sha256": response_hash,
                    }
                except (OSError, ValueError, TypeError, RuntimeError) as exc:
                    price_payload = {
                        **price_manifest,
                        "query_round": 1,
                        "dq_policy_version": 3,
                        "status": "UNAVAILABLE",
                        "error_type": type(exc).__name__,
                    }
            append_cache_record(price_cache, price_payload)
        elif (
            price_payload.get("dq_policy_version") != 3
            or price_payload.get("source_git_sha") != price_manifest.get("source_git_sha")
        ):
            price_payload.update(price_manifest)
            price_payload["dq_policy_version"] = 3
            append_cache_record(price_cache, price_payload)
        records.append(
            {
                "source": price_source,
                "endpoint": DAILY_PATH,
                "parameters": price_params,
                "symbol": symbol,
                "requested_date": query_day.isoformat(),
                **price_payload,
            }
        )

    record_index = {}
    for record in records:
        key = (record.get("source"), record.get("symbol"), record.get("requested_date"), record.get("query_round"))
        record_index[key] = record
    repeats = []
    for record in records:
        if record.get("source") in {
            "kis_per_stock_investor_flow",
            "kis_market_investor_flow",
            "kis_program_trading",
        } and record.get("query_round") == 1:
            later = record_index.get(
                (record["source"], record.get("symbol"), record["requested_date"], 2)
            )
            if later is not None:
                repeats.append(
                    {
                        "source": record.get("source"),
                        "symbol": record.get("symbol"),
                        "requested_date": record.get("requested_date"),
                        "classification": classify_response_hashes(
                            record.get("response_sha256"),
                            record.get("canonical_response_sha256"),
                            later.get("response_sha256"),
                            later.get("canonical_response_sha256"),
                        ),
                        "first_status": record.get("status"),
                        "second_status": later.get("status"),
                    }
                )
    result = {
        "schema_version": 1,
        "source_git_sha": git_sha(),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source": "KIS official Open Trading API",
        "pilot_scope": {
            "current_symbols": SYMBOLS,
            "dates": {year: day.isoformat() for year, day in PILOT_DATES.items()},
            "oldest_probe": {"symbol": "005930", "date": "2018-06-01"},
            "delisted_symbols": DELISTED,
            "authenticated_requests_are_sequential": True,
            "minimum_interval_seconds": 4,
            "access_token_persisted": False,
            "oauth_token_exchange_in_query_count": False,
            "raw_response_values_persisted": False,
        },
        "request_count": len(records),
        "success_count": sum(record.get("status") == "OK" for record in records),
        "unavailable_count": sum(record.get("status") == "UNAVAILABLE" for record in records),
        "requests": records,
        "repeat_queries": repeats,
    }
    write_json(OUT / "pilot-request-manifest.json", result)
    return result


def load_previous_snapshot() -> dict[str, Any]:
    snapshot_path = Path("/tmp/phase15-pre-snapshot.json")
    if snapshot_path.is_file():
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    entries = []
    for phase in range(5, 15):
        folder = ROOT / "runtime" / "research" / f"phase{phase}"
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            if any(token in path.name for token in ("manifest", "index", "summary", "config", "integrity")):
                entries.append({"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"generated_before_phase15": datetime.now(UTC).isoformat(), "count": len(entries), "files": entries}


def records_for(source: str, pilot: dict[str, Any]) -> list[dict[str, Any]]:
    return [record for record in pilot.get("requests", []) if record.get("source") == source]


def returned_period(records: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    dates = sorted(
        {
            day
            for record in records
            if record.get("status") == "OK"
            for day in record.get("returned_dates", [])
        }
    )
    return (dates[0], dates[-1]) if dates else (None, None)


def generate_artifacts(pilot: dict[str, Any]) -> dict[str, Any]:
    snapshot = load_previous_snapshot()
    write_json(OUT / "phase15-prior-artifact-snapshot.json", snapshot)
    integrity = compare_artifact_snapshot(snapshot, ROOT)
    write_json(OUT / "phase15-prior-artifact-integrity.json", integrity)
    all_records = pilot.get("requests", [])
    flow_records = records_for("kis_per_stock_investor_flow", pilot)
    ok_flow = [record for record in flow_records if record.get("status") == "OK"]
    delisted_flow = records_for("kis_delisted_symbol_flow", pilot)
    delisted_prices = records_for("kis_delisted_symbol_price_history", pilot)
    market_records = records_for("kis_market_investor_flow", pilot)
    program_records = records_for("kis_program_trading", pilot)
    market_fields = sorted(
        {field for record in market_records for field in record.get("response_field_names", [])}
        | {field for record in market_records for field in record.get("category_field_names", [])}
    )
    program_fields = sorted(
        {field for record in program_records for field in record.get("response_field_names", [])}
        | {field for record in program_records for field in record.get("category_field_names", [])}
    )
    market_earliest, market_latest = returned_period(market_records)
    program_earliest, program_latest = returned_period(program_records)
    repeats = pilot.get("repeat_queries", [])
    repeat_states = [
        item["classification"] for item in repeats
        if item.get("source") == "kis_per_stock_investor_flow"
    ]
    market_repeat_state = next(
        (item["classification"] for item in repeats if item.get("source") == "kis_market_investor_flow"),
        "UNAVAILABLE",
    )
    program_repeat_state = next(
        (item["classification"] for item in repeats if item.get("source") == "kis_program_trading"),
        "UNAVAILABLE",
    )
    repeat_verdict = (
        "IDENTICAL" if repeat_states and all(state == "IDENTICAL" for state in repeat_states)
        else "SEMANTICALLY_IDENTICAL" if repeat_states and all(state in {"IDENTICAL", "SEMANTICALLY_IDENTICAL"} for state in repeat_states)
        else "REVISED" if "REVISED" in repeat_states else "UNAVAILABLE"
    )
    verified_source_dates = sorted(
        {
            source_date
            for record in ok_flow
            if record.get("response_row_count", 0) > 0
            and record.get("dq", {}).get("requested_date_matches") is True
            for source_date in record.get("returned_dates", [])
        }
    )
    earliest = verified_source_dates[0] if verified_source_dates else None
    latest = verified_source_dates[-1] if verified_source_dates else None
    flow_depth = classify_historical_depth(
        [date.fromisoformat(day) for day in verified_source_dates],
        required_start=date(2019, 1, 1),
    )
    observed_flow_window = max(
        (record.get("response_row_count", 0) for record in ok_flow), default=0
    )
    observed_market_window = max(
        (record.get("response_row_count", 0) for record in market_records if record.get("status") == "OK"),
        default=0,
    )
    observed_program_window = max(
        (record.get("response_row_count", 0) for record in program_records if record.get("status") == "OK"),
        default=0,
    )
    panel_sessions = 1_708
    flow_requests_per_symbol = (panel_sessions + max(1, observed_flow_window - 1) - 1) // max(1, observed_flow_window - 1)
    program_requests_per_symbol = (panel_sessions + max(1, observed_program_window - 1) - 1) // max(1, observed_program_window - 1)
    market_requests_per_market = (panel_sessions + max(1, observed_market_window - 1) - 1) // max(1, observed_market_window - 1)
    flow_cost = estimate_acquisition(
        symbols=100,
        sessions=flow_requests_per_symbol,
        retry_rate=0.01,
        interval_seconds=4,
        average_response_bytes=2_000,
    )
    program_cost = estimate_acquisition(
        symbols=100,
        sessions=program_requests_per_symbol,
        retry_rate=0.01,
        interval_seconds=4,
        average_response_bytes=2_000,
    )
    market_cost = estimate_acquisition(
        symbols=2,
        sessions=market_requests_per_market,
        retry_rate=0.01,
        interval_seconds=4,
        average_response_bytes=2_000,
    )
    delisted_flow_returned = any(
        record.get("status") == "OK" and record.get("response_row_count", 0) > 0
        for record in delisted_flow
    )
    delisted_price_returned = any(
        record.get("status") == "OK" and record.get("row_count", 0) > 0
        for record in delisted_prices
    )
    delisted_support = classify_delisted_support(
        metadata_found=True,
        flow_returned=delisted_flow_returned,
        price_returned=delisted_price_returned,
    )
    field_names = sorted({field for record in ok_flow for field in record.get("category_field_names", [])})
    source_meta = {
        "source_git_sha": git_sha(),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "qualification_logic_version": 1,
        "documentation_urls": DOCS,
        "no_strategy_or_return_analysis": True,
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
    }
    write_json(OUT / "phase15-source-inventory.json", {
        **source_meta,
        "sources": [
            {"name": "KIS per-stock investor daily flow", "tier": 1, "qualification": "PARTIAL", "endpoint": INVESTOR_FLOW_DAILY_PATH, "grain": "symbol-session"},
            {"name": "KIS market investor daily flow", "tier": 2, "qualification": "PARTIAL", "endpoint": MARKET_INVESTOR_FLOW_DAILY_PATH, "grain": "market-session"},
            {"name": "KIS per-stock daily program trading", "tier": 2, "qualification": "PARTIAL", "endpoint": PROGRAM_FLOW_DAILY_PATH, "grain": "symbol-session"},
            {"name": "KRX market investor statistics", "tier": 2, "qualification": "PARTIAL", "grain": "market-session"},
            {"name": "KRX sector/index history", "tier": 3, "qualification": "PARTIAL", "grain": "index-session"},
            {"name": "OpenDART filing list", "tier": 4, "qualification": "PARTIAL", "grain": "filing"},
            {"name": "KIS historical order book/ticks", "tier": 5, "qualification": "NOT_AVAILABLE", "grain": "snapshot/tick"},
        ],
    })
    common_score = {
        "official_or_third_party": "OFFICIAL",
        "access_method": "authenticated REST; sequential bounded request pilot",
        "data_type": "daily market/investor data",
        "earliest_date_tested": None,
        "latest_date_tested": None,
        "date_range_support": "date-addressable rolling response windows observed; exact retention bound is undocumented",
        "request_granularity": "date anchor per symbol/market; response is a bounded historical window",
        "pagination": "response continuation headers supported; observed page count recorded per pilot",
        "rate_limit": "official per-endpoint limit not located; local shared limiter used 4 seconds/request",
        "auth_required": True,
        "point_in_time_safety": "UNKNOWN",
        "publication_timestamp": None,
        "revision_semantics": "no historical vintage/correction timestamp documented",
        "delisted_support": "bounded KIS queries recorded in pilot; outcome may be empty",
        "symbol_lineage_support": "not exposed by endpoint",
        "reproducibility": repeat_verdict,
        "missing_data_behavior": "empty outputs/missing fields retained as missing; no imputation",
        "license_or_usage_limitation": "KIS portal states personal investment use; third-party provision restricted; confirm research redistribution terms before any sharing",
        "estimated_full_acquisition_requests": flow_cost["total_requests"],
        "estimated_full_acquisition_time": f"{flow_cost['estimated_hours']} hours continuous including 1% retry allowance at 4 seconds",
    }
    scorecards = []
    for name, url, qualification, cost, reproducibility, source_earliest, source_latest, granularity in (
        ("KIS per-stock investor daily flow", DOCS["kis_flow"], "PARTIAL", flow_cost, repeat_verdict, earliest, latest, "symbol and date anchor; observed up to 31 session rows"),
        ("KIS market investor daily flow", DOCS["kis_market_flow"], "PARTIAL", market_cost, market_repeat_state, market_earliest, market_latest, "market and date anchor; observed up to 300 session rows"),
        ("KIS per-stock program trading", DOCS["kis_program"], "PARTIAL", program_cost, program_repeat_state, program_earliest, program_latest, "symbol and date anchor; observed up to 30 session rows"),
    ):
        scorecards.append({
            **common_score,
            "source_name": name,
            "provider": "Korea Investment & Securities",
            "documentation_url": url,
            "qualification": qualification,
            "reproducibility": reproducibility,
            "earliest_date_tested": source_earliest,
            "latest_date_tested": source_latest,
            "request_granularity": granularity,
            "estimated_full_acquisition_requests": cost["total_requests"],
            "estimated_full_acquisition_time": f"{cost['estimated_hours']} hours continuous including 1% retry allowance at 4 seconds",
        })
    scorecards.extend([
        {
            **common_score,
            "source_name": "KRX Data Marketplace market/industry statistics",
            "provider": "Korea Exchange",
            "documentation_url": DOCS["krx_market_flow"],
            "access_method": "official web data services; product-specific API/download contract requires review",
            "data_type": "market investor statistics and sector/index history",
            "request_granularity": "market or index/session; stock-level offerings need product-specific audit",
            "auth_required": "product-dependent",
            "point_in_time_safety": "END_OF_SESSION",
            "publication_timestamp": "KRX page states finalized daily market data after 20:00 KST",
            "revision_semantics": "historical vintage/correction behavior not established",
            "reproducibility": "UNAVAILABLE",
            "qualification": "PARTIAL",
            "earliest_date_tested": None,
            "latest_date_tested": None,
            "estimated_full_acquisition_requests": None,
            "estimated_full_acquisition_time": None,
        },
        {
            **common_score,
            "source_name": "OpenDART filing list",
            "provider": "Financial Supervisory Service",
            "documentation_url": DOCS["opendart_guide"],
            "access_method": "API key REST, paginated filing list",
            "data_type": "corporate disclosure filings",
            "request_granularity": "filing date and corp code",
            "pagination": "page_no/page_count; default page size 20,000",
            "rate_limit": "guide states 20,000 requests/day",
            "auth_required": True,
            "point_in_time_safety": "UNKNOWN",
            "publication_timestamp": "receipt date is YYYYMMDD; intraday publication time not established in inspected guide",
            "revision_semantics": "corrected reports can be labeled in report name/remarks; normalized original-to-correction chain not established",
            "reproducibility": "UNAVAILABLE",
            "qualification": "PARTIAL",
            "earliest_date_tested": None,
            "latest_date_tested": None,
            "estimated_full_acquisition_requests": None,
            "estimated_full_acquisition_time": None,
        },
    ])
    scorecards = [validate_scorecard(record) for record in scorecards]
    write_json(OUT / "source-scorecards.json", scorecards)
    write_json(OUT / "per-stock-flow-audit.json", {
        **source_meta,
        "verdict": "PARTIAL",
        "endpoint": INVESTOR_FLOW_DAILY_PATH,
        "tr_id": "FHPTJ04160001",
        "earliest_verified_response_date": earliest,
        "earliest_positive_response_request_date": min(
            (
                record["requested_date"]
                for record in ok_flow
                if record.get("response_row_count", 0) > 0
            ),
            default=None,
        ),
        "latest_verified_response_date": latest,
        "pilot_successes": sum(record.get("status") == "OK" for record in flow_records),
        "pilot_requests": len(flow_records),
        "category_field_names": field_names,
        "field_semantics": "field names include foreign-registered/nonregistered buy/sell/net quantity and value, organization net, and individual net; exact units and the organization-to-institution mapping still require the official field dictionary",
        "timestamp_safety": "UNKNOWN; date-keyed completed-session values with no publication timestamp or vintage selector verified",
        "repeat_query_classification": repeat_verdict,
        "repeat_query_details": repeats,
        "historic_depth": flow_depth,
        "observed_rows_per_date_anchor": observed_flow_window,
        "observed_date_window": "single-page responses ended on requested date; observed 31 flow rows per query, including dates before the anchor",
        "sampled_history_is_contiguous": False,
        "wire_hash_limitation": "Exact provider HTTP body bytes are not exposed by KisRestClient; hashes cover decoded payload serialization only.",
        "pagination": "adapter follows KIS tr_cont M/F continuation; pilot records page count.",
        "rate_limit": "4 second local shared limiter; official exact per-endpoint limit not verified.",
        "acquisition": {
            "requests_per_symbol_estimate": flow_requests_per_symbol,
            "one_session_overlap_buffer": True,
            "cost": flow_cost,
            "basis": "assume 30 new sessions per 31-row response; no-pagination pilot observation, with one-session overlap buffer",
        },
    })
    write_json(OUT / "market-flow-audit.json", {
        **source_meta,
        "verdict": "PARTIAL",
        "kis_endpoint": MARKET_INVESTOR_FLOW_DAILY_PATH,
        "scope": "market aggregate; not stock-level",
        "pilot": market_records,
        "repeat_query_classification": market_repeat_state,
        "field_names": market_fields,
        "observed_rows_per_date_anchor": observed_market_window,
        "observed_window_ends_on_anchor": all(
            record.get("requested_date") in record.get("returned_dates", [])
            and max(record.get("returned_dates", [record.get("requested_date", "")])) == record.get("requested_date")
            for record in market_records
            if record.get("status") == "OK" and record.get("returned_dates")
        ),
        "krx_source": DOCS["krx_market_flow"],
        "krx_categories": ["foreign", "institutional", "individual", "other categories vary by view"],
        "availability": "KRX page reports daily final data after 20:00 KST; KIS exact publication time unknown",
    })
    write_json(OUT / "program-trading-audit.json", {
        **source_meta,
        "verdict": "PARTIAL",
        "endpoint": PROGRAM_FLOW_DAILY_PATH,
        "tr_id": "FHPPG04650201",
        "grain": "date anchor returns observed 30-row historical window per symbol (bounded pilot)",
        "pilot": program_records,
        "repeat_query_classification": program_repeat_state,
        "field_names": program_fields,
        "historical_range": "sampled historical windows extend at least across 2021 and 2024; full retention bound not established",
        "observed_rows_per_date_anchor": observed_program_window,
        "sampled_history_is_contiguous": False,
        "timestamp_safety": "UNKNOWN pending official publication-time/vintage semantics",
    })
    write_json(OUT / "sector-source-audit.json", {
        **source_meta,
        "verdict": "PARTIAL",
        "sources": [DOCS["krx_home"], DOCS["kis_catalog"]],
        "sector_index_history": "official industry/sector daily/period index products appear in KIS catalog; no bounded series pilot performed",
        "historical_point_in_time_membership": "current sector label cannot establish historical membership; dated membership lineage not verified",
        "classification_changes": "not fully reconstructible from inspected product descriptions",
        "sector_pit_membership": "NOT_AVAILABLE",
    })
    write_json(OUT / "corporate-event-source-audit.json", {
        **source_meta,
        "verdict": "PARTIAL",
        "sources": {"OpenDART": DOCS["opendart_guide"], "KIND": "https://kind.krx.co.kr"},
        "historical_start_date": "NOT_VERIFIED_IN_PHASE15",
        "earliest_date_tested": None,
        "timestamp_resolution": "receipt date available at day precision in inspected filing-list guide; intraday publication timestamp not established",
        "revision_support": "corrected filing labels/remarks distinguish some corrections; stable parent linkage not verified",
        "symbol_mapping": "corp_code plus stock_code fields; lifecycle mapping over time not verified",
        "request_model": "API key, date/query filters, paginated; 20,000/day documented in guide",
        "qualification": "PARTIAL",
    })
    write_json(OUT / "microstructure-source-audit.json", {
        **source_meta,
        "historical_verdict": "NOT_AVAILABLE",
        "prospective_verdict": "AVAILABLE",
        "evidence": "KIS official catalog exposes real-time quote/order-book products; no historical order-book/tick archive or retrospective selector was found in inspected docs",
        "future_scope": ["best bid/ask", "depth snapshots", "tick trades"],
    })
    write_json(OUT / "historical-depth-matrix.json", {
        **source_meta,
        "verified_dates": sorted({record["requested_date"] for record in all_records if record.get("status") == "OK"}),
        "source_rows": [
            {"source": "KIS per-stock investor flow", "earliest_returned_date": earliest, "latest_returned_date": latest, "2018": any(record.get("requested_date", "").startswith("2018") and record.get("status") == "OK" for record in flow_records), "2019_2022": "bounded sample only", "2023_2025": "bounded sample only", "depth_status": "PARTIAL_COVERAGE_SAMPLED"},
            {"source": "KIS market investor flow", "earliest_returned_date": min((d for r in market_records if r.get("status") == "OK" for d in r.get("returned_dates", [])), default=None), "latest_returned_date": max((d for r in market_records if r.get("status") == "OK" for d in r.get("returned_dates", [])), default=None), "observed_rows_per_anchor": observed_market_window, "depth_status": "PARTIAL_COVERAGE_SAMPLED"},
            {"source": "KIS program trading", "earliest_returned_date": min((d for r in program_records if r.get("status") == "OK" for d in r.get("returned_dates", [])), default=None), "latest_returned_date": max((d for r in program_records if r.get("status") == "OK" for d in r.get("returned_dates", [])), default=None), "observed_rows_per_anchor": observed_program_window, "depth_status": "PARTIAL_COVERAGE_SAMPLED"},
            {"source": "OpenDART disclosures", "earliest_date_tested": None, "depth_status": "DOCUMENTED_API_NOT_PILOTED"},
            {"source": "KRX sector indexes/membership", "earliest_date_tested": None, "depth_status": "DOCUMENTED_PRODUCTS_NOT_PILOTED"},
        ],
    })
    write_json(OUT / "point-in-time-safety-matrix.json", {
        **source_meta,
        "sources": [
            {"source": "per-stock investor flow", "state": "UNKNOWN", "availability_time": "NEXT_SESSION_ONLY conditional on completed official row", "publication_timestamp": None},
            {"source": "market investor flow", "state": "UNKNOWN", "availability_time": "KRX market aggregate final after 20:00 KST; KIS series exact time unknown", "publication_timestamp": None},
            {"source": "program trading", "state": "UNKNOWN", "availability_time": "date only", "publication_timestamp": None},
            {"source": "OpenDART filings", "state": "UNKNOWN", "availability_time": "receipt date available; intraday time absent in inspected list guide", "publication_timestamp": "day precision"},
            {"source": "sector classification", "state": "UNKNOWN", "availability_time": "historical membership vintage unavailable", "publication_timestamp": None},
        ],
    })
    write_json(OUT / "delisted-support-audit.json", {
        **source_meta,
        "symbols": DELISTED,
        "flow_pilot": delisted_flow,
        "price_pilot": delisted_prices,
        "metadata_sources": {"I-HQ": DOCS["kind_ihq_delisting"], "IzMedia": DOCS["kind_izmedia_delisting"]},
        "support_verdict": "PARTIAL",
        "pilot_sample_support": delisted_support,
        "delisted_flow": "PARTIAL" if delisted_flow_returned else "NOT_AVAILABLE",
        "delisted_price_history": "PARTIAL" if delisted_price_returned else "NOT_AVAILABLE",
        "universe_verdict": "PARTIAL",
    })
    write_json(OUT / "symbol-lineage-audit.json", {
        **source_meta,
        "verdict": "PARTIAL",
        "available_fields": ["corp_code", "stock_code", "listing/delisting notices on KIND"],
        "not_established": ["complete effective-dated ticker code changes", "merger successor mapping", "spin-off lineage", "market migration history"],
        "examples": DELISTED,
    })
    write_json(OUT / "pilot-response-integrity.json", {
        **source_meta,
        "request_count": pilot.get("request_count"),
        "success_count": pilot.get("success_count"),
        "unavailable_count": pilot.get("unavailable_count"),
        "hashes_only": True,
        "raw_response_values_persisted": False,
        "repeat_queries": repeats,
        "canonicalization": "object keys sorted; row-like dict lists sorted; scalar list order preserved",
    })
    write_json(OUT / "acquisition-cost-estimates.json", {
        **source_meta,
        "assumptions": {"panel_symbols": 100, "sessions_2019_2025": panel_sessions, "observed_rows_per_flow_anchor": observed_flow_window, "effective_new_sessions_per_anchor": max(1, observed_flow_window - 1), "retry_rate": 0.01, "interval_seconds": 4.0, "average_response_bytes": 2000},
        "full_panel": {"coverage_sessions": panel_sessions, "estimated_requests_per_symbol": flow_requests_per_symbol, **flow_cost},
        "program_panel": {"coverage_sessions": panel_sessions, "observed_rows_per_anchor": observed_program_window, "estimated_requests_per_symbol": program_requests_per_symbol, **program_cost},
        "market_panel_both_markets": {"coverage_sessions_per_market": panel_sessions, "observed_rows_per_anchor": observed_market_window, "estimated_requests_per_market": market_requests_per_market, **market_cost},
        "three_year_reference": estimate_acquisition(symbols=100, sessions=(731 + max(1, observed_flow_window - 1) - 1) // max(1, observed_flow_window - 1), retry_rate=0.01, interval_seconds=4, average_response_bytes=2000),
        "official_endpoint_limit": "not found in reviewed docs",
        "classification": "MANAGEABLE only under the observed rolling-window assumption: conservatively 30 new sessions per 31-row flow response, one-session overlap buffer, 4 seconds/request, and 1% retries; full contiguous date coverage and official rate limits remain unverified",
    })
    used = [
        {"start": "2023-01-02", "end": "2025-12-30", "source": "Phase 11/14 outcomes"},
        {"start": "2025-07-01", "end": "2025-12-30", "source": "Phase 13 Confirmation"},
    ]
    write_json(OUT / "fresh-evidence-feasibility.json", {
        **source_meta,
        "status": "PARTIAL",
        "used_outcome_periods": used,
        "prior_new_source_used": False,
        "price_only_touched_periods": [{"start": "2022-11-01", "end": "2022-12-30", "source": "Phase 11 warmup only"}],
        "date_search": "docs/research and research-ledger.json show 2023-2025 outcome usage, no 2019-2021 outcome period found; Phase 3-9 intraday manifests need exact date reconciliation before freezing.",
        "candidate_periods": [
            {"name": "NEW_DISCOVERY", "start": "2019-01-02", "end": "2020-12-30", "status": "CONDITIONAL_FRESH"},
            {"name": "NEW_REPLICATION", "start": "2021-01-04", "end": "2021-12-30", "status": "CONDITIONAL_FRESH"},
            {"name": "NEW_CONFIRMATION", "start": "2022-01-03", "end": "2022-10-31", "status": "PRICE_ONLY_WARMUP_CAVEAT"},
        ],
        "caveat": "Phase 3-9 detailed date windows are not fully enumerated in the current ledger, so fresh status is provisional pending manifest audit.",
    })
    write_json(OUT / "proposed-evidence-plan.json", {
        "schema_version": 1,
        "mode": "conditional historical source qualification followed by future source factor discovery",
        "periods": [
            {"name": "WARMUP", "start": "2018-01-01", "end": "2018-12-31", "purpose": "feature warmup only if adequate source depth verified"},
            {"name": "NEW_DISCOVERY", "start": "2019-01-02", "end": "2020-12-30"},
            {"name": "NEW_REPLICATION", "start": "2021-01-04", "end": "2021-12-30"},
            {"name": "NEW_CONFIRMATION", "start": "2022-01-03", "end": "2022-10-31"},
        ],
        "exact_krx_sessions": "resolve later against official calendar",
        "gate": "reconcile Phase 3-9 manifests, source depth, licensing, and publication timing before any return evaluation",
        "2023_2025": "historical sensitivity only",
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
    })
    write_json(OUT / "pipeline-v2-source-compatibility.json", {
        **source_meta,
        "compatible_after_adapter": ["daily IC", "quantile spread", "walk-forward", "null permutations", "economic gate", "market-adjusted gate", "concentration analysis"],
        "required_adapter_work": ["as-of availability_time", "feature registry metadata", "point-in-time missingness", "session/symbol join", "source revision audit", "frozen source cache hash"],
        "current_source_gate": "do not add to active registry until timestamp semantics and licensing are established",
        "availability_time_policy": "session-dated only means NEXT_SESSION_ONLY; unknown publication remains UNKNOWN",
    })
    write_json(OUT / "proposed-factor-registry-extension.json", proposed_factor_registry())
    write_json(OUT / "source-ranking.json", {
        "ranking_basis": ["information novelty", "point-in-time safety", "historical depth", "fresh evidence", "stock-level granularity", "cost", "coverage", "survivorship usefulness", "pipeline compatibility"],
        "profitability_used": False,
        "ranked": [
            {"rank": 1, "source": "per-stock investor flow", "qualification": "PARTIAL", "reason": "highest novelty and stock granularity; sampled history is deep and estimated panel cost is manageable, but repeat hashes changed and PIT timing is unknown"},
            {"rank": 2, "source": "OpenDART corporate disclosure filings", "qualification": "PARTIAL", "reason": "novel event data and filing dates; intraday time/correction links need audit"},
            {"rank": 3, "source": "sector/index context", "qualification": "PARTIAL", "reason": "historical index products listed; point-in-time membership not established"},
            {"rank": 4, "source": "program trading", "qualification": "PARTIAL", "reason": "dated stock-level API exists; history/timestamp unknown"},
            {"rank": 5, "source": "market aggregate flow", "qualification": "PARTIAL", "reason": "historically useful market control but not stock-specific"},
            {"rank": 6, "source": "historical order book/ticks", "qualification": "NOT_AVAILABLE", "reason": "no retrospective official archive verified"},
        ],
        "primary_source": "NONE",
        "secondary_source": "NONE",
        "next_research_mode": "PROSPECTIVE_DATA_COLLECTION",
    })
    summary = {
        **source_meta,
        "phase15_source_audit": "PARTIAL",
        "per_stock_flow": "PARTIAL",
        "market_investor_flow": "PARTIAL",
        "program_trading_data": "PARTIAL",
        "sector_context": "PARTIAL",
        "sector_pit_membership": "NOT_AVAILABLE",
        "corporate_event_data": "PARTIAL",
        "historical_microstructure": "NOT_AVAILABLE",
        "prospective_microstructure": "AVAILABLE",
        "point_in_time_universe": "PARTIAL",
        "delisted_price_history": "PARTIAL" if delisted_price_returned else "NOT_AVAILABLE",
        "symbol_lineage": "PARTIAL",
        "fresh_historical_evidence": "PARTIAL",
        "primary_source": "NONE",
        "secondary_source": "NONE",
        "next_research_mode": "PROSPECTIVE_DATA_COLLECTION",
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
        "alpha": "UNPROVEN",
        "shadow_next_session": "NO",
        "live": "DISABLED",
        "answers": {
            "Q1": "Yes for sampled historical windows: 50/50 KIS per-stock requests returned rows, but repeated same-date payload hashes changed.",
            "Q2": f"Earliest actual source date in the pilot responses: {earliest}; 2018-06-01 was the oldest date anchor tested.",
            "Q3": "No. Publication time/vintage semantics are unknown; candidate use is next-session-only after the endpoint is fully qualified.",
            "Q4": f"Observed flow responses contained up to {observed_flow_window} rows. Assuming 30 new sessions per anchor (one-session overlap buffer), 100 × ceil(1,708/30) = {100 * flow_requests_per_symbol} requests before retries.",
            "Q5": f"At the shared 4-second interval plus 1% retries, estimate {flow_cost['total_requests']} requests and {flow_cost['estimated_hours']} continuous hours. This is a planning estimate; official KIS endpoint limits and full date-window continuity are unverified.",
            "Q6": f"Program endpoint returned up to {observed_program_window} dated rows at a date anchor in two samples. Repeated query stability and deeper retention were not tested.",
            "Q7": f"KIS market-flow endpoint returned up to {observed_market_window} dated market-level rows per anchor; sector/index products are listed but no sector series pilot was run.",
            "Q8": "Not verified; current labels do not establish point-in-time sector membership.",
            "Q9": "OpenDART filing list provides receipt date, but intraday publication timestamp is not established by inspected guide.",
            "Q10": "Some corrected reports can be recognized from labels/remarks; a stable original-to-correction chain was not verified.",
            "Q11": "No historical order-book/tick archive was verified.",
            "Q12": "Yes, KIS lists prospective real-time quote/order-book products.",
            "Q13": "The two documented delisted samples (I-HQ and IzMedia) returned historical flow and price rows; broad coverage remains partial.",
            "Q14": "Yes for both selected delisted samples on the tested 2023 dates; all delisted symbols and all periods remain unverified.",
            "Q15": "Partially: filing and listing/delisting notices exist, but full effective-dated successor lineage is not established.",
            "Q16": f"Potentially, PARTIAL: pilot source dates reach {earliest}, and no 2019-2021 outcome windows appear in the Phase 14 ledger; Phase 3-9 date manifests still need reconciliation.",
            "Q17": "Per-stock investor flow has the strongest novelty and granularity among reviewed source families.",
            "Q18": f"KIS per-stock flow appears operationally manageable under the observed rolling window ({flow_cost['estimated_hours']}h estimate); KRX market aggregate is much cheaper but not stock-level. Limits and complete coverage need verification.",
            "Q19": "NONE qualifies as primary yet; do not force a winner.",
            "Q20": "PROSPECTIVE_DATA_COLLECTION while source timestamp, licensing, and full historical behavior are resolved.",
        },
    }
    write_json(OUT / "phase15-summary.json", summary)
    index = build_artifact_index(OUT, exclude={"phase15-artifact-index.json", "artifact-integrity.json"})
    write_json(OUT / "phase15-artifact-index.json", index)
    artifact_check = verify_artifact_index(OUT, index)
    write_json(OUT / "artifact-integrity.json", artifact_check)
    return {"summary": summary, "previous_integrity": integrity, "artifact_integrity": artifact_check}


def main() -> int:
    pilot = run_pilot()
    results = generate_artifacts(pilot)
    print(json.dumps({
        "pilot_requests": pilot["request_count"],
        "pilot_successes": pilot["success_count"],
        "pilot_unavailable": pilot["unavailable_count"],
        "artifact_integrity": results["artifact_integrity"]["status"],
        "previous_phase_integrity": results["previous_integrity"]["status"],
        "previous_phase_files": results["previous_integrity"]["count"],
        "per_stock_flow": results["summary"]["per_stock_flow"],
        "primary_source": results["summary"]["primary_source"],
        "next_research_mode": results["summary"]["next_research_mode"],
    }, sort_keys=True))
    return 0 if results["artifact_integrity"]["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

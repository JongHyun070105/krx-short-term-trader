#!/usr/bin/env python3
"""Run only the frozen, old-date Phase 16C KIS per-stock contract probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pyarrow import parquet

from krx_trader.kis.auth import KisAuthError
from krx_trader.kis.rest import KisApiError
from krx_trader.kis.transport import UrllibTransport
from krx_trader.research import phase16, phase16b, phase16c

ROOT = phase16.REPO_ROOT
ARTIFACT_ROOT = ROOT / "runtime/research/phase16c"
SAMPLE_PATH = ARTIFACT_ROOT / "phase16c-probe-sample.json"
SAMPLE_SHA_PATH = ARTIFACT_ROOT / "phase16c-probe-sample.sha256"
PLAN_PATH = ARTIFACT_ROOT / "phase16c-probe-plan.json"
MANIFEST_PATH = ARTIFACT_ROOT / "phase16c-probe-request-manifest.json"
RESULTS_PATH = ARTIFACT_ROOT / "phase16c-probe-results.json"
CONFIG_PATH = phase16b.DEFAULT_PHASE16B_CONFIG
PER_STOCK_PATH = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
PROTECTED = [
    (date(2026, 1, 5), date(2026, 4, 16), "EXTERNAL_2026"),
    (date(2026, 7, 28), date(2026, 8, 28), "JUL_AUG_BLOCK"),
]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_calendar(market: str) -> tuple[list[date], tuple[date, date]]:
    path = ROOT / f"runtime/research/phase11/daily_cache/indexes/{market}-1d-kis-index.parquet"
    if not path.is_file():
        raise RuntimeError(f"local KRX index-date cache unavailable for {market}")
    rows = parquet.read_table(path, columns=["timestamp"]).to_pylist()
    sessions = sorted({date.fromisoformat(row["timestamp"][:10]) for row in rows})
    return sessions, (sessions[0], sessions[-1])


def _load_price_observations(symbol: str) -> tuple[list[date], list[date]] | None:
    path = ROOT / f"runtime/research/phase11/daily_cache/daily/{symbol}-1d-adjusted.parquet"
    if not path.is_file():
        return None
    rows = parquet.read_table(path, columns=["timestamp", "volume"]).to_pylist()
    sessions = sorted({date.fromisoformat(row["timestamp"][:10]) for row in rows})
    active_sessions = sorted(
        {date.fromisoformat(row["timestamp"][:10]) for row in rows if (row["volume"] or 0) > 0}
    )
    return sessions, active_sessions


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _NoRetryPerStockTransport:
    """Forward transport calls while preventing an endpoint retry in one probe."""

    def __init__(self) -> None:
        self._transport = UrllibTransport()
        self._probe_id: str | None = None
        self._endpoint_calls: dict[str, int] = {}

    def start_probe(self, probe_id: str) -> None:
        self._probe_id = probe_id
        self._endpoint_calls[probe_id] = 0

    def request(self, method: str, url: str, **kwargs: Any):
        if PER_STOCK_PATH in url:
            if self._probe_id is None:
                raise KisApiError("per-stock request attempted outside a Phase16C probe")
            count = self._endpoint_calls[self._probe_id]
            if count >= 1:
                raise KisApiError("Phase16C blocks automatic per-stock request retries")
            self._endpoint_calls[self._probe_id] = count + 1
        return self._transport.request(method, url, **kwargs)

    def endpoint_calls(self, probe_id: str) -> int:
        return self._endpoint_calls.get(probe_id, 0)


def _validate_frozen_inputs(sample_bytes: bytes, plan: dict[str, Any]) -> str:
    sample_hash = hashlib.sha256(sample_bytes).hexdigest()
    recorded_hash = SAMPLE_SHA_PATH.read_text(encoding="utf-8").split()[0]
    sample = json.loads(sample_bytes)
    if sample_hash != recorded_hash or plan.get("sample_sha256") != sample_hash:
        raise RuntimeError("Phase16C frozen sample SHA-256 mismatch")
    if sample.get("frozen_before_live_probe_results") is not True:
        raise RuntimeError("Phase16C sample is not marked frozen before probe results")
    if plan.get("evidence_class") != "CONTRACT_REQUALIFICATION_PROBE":
        raise RuntimeError("unexpected probe evidence class")
    requests = plan.get("requests")
    if not isinstance(requests, list) or len(requests) != plan.get("planned_new_requests"):
        raise RuntimeError("probe plan request count differs from its declared budget")
    if len(requests) > min(60, int(plan.get("request_ceiling", 0))):
        raise RuntimeError("probe plan exceeds the Phase16C request ceiling")
    return sample_hash


def _new_manifest_entry(item: dict[str, Any], *, request_hash: str) -> dict[str, Any]:
    return {
        "probe_id": item["probe_id"],
        "source": item["source"],
        "symbol": item["symbol"],
        "market": item["market"],
        "anchor": item["anchor"],
        "purpose": "CONTRACT_REQUALIFICATION_PROBE",
        "evidence_class": "CONTRACT_REQUALIFICATION_PROBE",
        "request_hash": request_hash,
        "pre_request_safety_decision": "NOT_YET_CHECKED",
        "request_time_utc": None,
        "elapsed_ms": None,
        "request_count": 0,
        "request_status": "NOT_STARTED",
        "retry_count": 0,
        "response_row_count": None,
        "response_min_date": None,
        "response_max_date": None,
        "response_hash": None,
        "schema_fingerprint": None,
        "contract_result": "NOT_RUN",
        "classification": "NOT_RUN",
    }


def _run(*, validate_only: bool) -> dict[str, Any]:
    sample_bytes = SAMPLE_PATH.read_bytes()
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    sample_hash = _validate_frozen_inputs(sample_bytes, plan)
    config, config_sha, _raw_config = phase16.load_config(CONFIG_PATH)
    if config_sha != plan.get("config_sha256"):
        raise RuntimeError("collector config changed after the frozen probe plan")
    if hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest() != config_sha:
        raise RuntimeError("collector config changed while preparing probes")
    if not phase16b.credentials_available():
        raise RuntimeError("KIS credentials unavailable; no probes attempted")

    manifest = {
        "schema_version": 1,
        "phase": "PHASE16C",
        "evidence_class": "CONTRACT_REQUALIFICATION_PROBE",
        "source": "kis_per_stock_flow",
        "sample_sha256": sample_hash,
        "config_sha256": config_sha,
        "contract_version": plan["contract_version_at_plan"],
        "planned_requests": len(plan["requests"]),
        "actual_requests": 0,
        "retries": 0,
        "probe_range_is_old_and_prevalidated": True,
        "protected_ranges": [
            {"label": label, "start": start.isoformat(), "end": end.isoformat()}
            for start, end, label in PROTECTED
        ],
        "requests": [],
    }
    records: list[dict[str, Any]] = []
    for item in plan["requests"]:
        params = {
            "endpoint": PER_STOCK_PATH,
            "method": "GET",
            "symbol": item["symbol"],
            "anchor": item["anchor"],
            "max_pages": 1,
        }
        entry = _new_manifest_entry(item, request_hash=_stable_hash(params))
        manifest["requests"].append(entry)
    _write_json(MANIFEST_PATH, manifest)
    if validate_only:
        return {
            "status": "VALIDATED_NO_NETWORK",
            "sample_sha256": sample_hash,
            "planned_requests": len(plan["requests"]),
            "credentials_available": True,
        }

    source_contract = phase16b.contracts_from_config(config)["kis_per_stock_flow"]
    endpoint_transport = _NoRetryPerStockTransport()
    client = phase16b._kis_client(config)
    # Replace only the transport reference used for endpoint and token operations;
    # the client's persistent shared limiter and zero-retry policy remain intact.
    client._transport = endpoint_transport
    client._tokens._transport = endpoint_transport
    started_all = time.monotonic()
    started_utc = datetime.now(UTC).isoformat()
    source_root = phase16.DEFAULT_ROOT
    lock_path = source_root / "locks/contract-probe.lock"
    status = "COMPLETE"
    with phase16._file_lock(lock_path, blocking=False) as locked:
        if not locked:
            raise RuntimeError("shared Phase16 KIS contract-probe lock is already held")
        for index, item in enumerate(plan["requests"]):
            entry = manifest["requests"][index]
            anchor = date.fromisoformat(item["anchor"])
            preflight = phase16c.preflight_old_probe_anchor(
                anchor,
                protected_ranges=PROTECTED,
                upper_anchor_semantics_previously_observed=True,
            )
            if not preflight["allowed"] or item.get("protected_ranges_touched"):
                entry["pre_request_safety_decision"] = "DENY_UNSAFE_OLD_ANCHOR"
                entry["classification"] = "PRE_REQUEST_DENIED"
                status = "STOPPED_PRE_REQUEST_GUARD"
                _write_json(MANIFEST_PATH, manifest)
                break
            entry["pre_request_safety_decision"] = preflight["request_decision"]
            entry["pre_request_safety_basis"] = preflight["reason"]
            entry["request_time_utc"] = datetime.now(UTC).isoformat()
            entry["request_status"] = "IN_FLIGHT"
            _write_json(MANIFEST_PATH, manifest)
            endpoint_transport.start_probe(item["probe_id"])
            start = time.monotonic()
            try:
                payload = client.get_investor_flow_by_date(
                    item["symbol"], anchor, max_pages=1
                )
            except ValueError:
                entry["classification"] = "INVALID_PROBE"
                entry["contract_result"] = "INVALID_PROBE"
                entry["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)
                entry["request_count"] = endpoint_transport.endpoint_calls(item["probe_id"])
                entry["request_status"] = "FINISHED"
                manifest["actual_requests"] = sum(x["request_count"] for x in manifest["requests"])
                record = {key: entry[key] for key in (
                    "probe_id", "symbol", "market", "anchor", "classification",
                    "pre_request_safety_decision", "request_time_utc", "elapsed_ms",
                    "request_count", "retry_count", "request_hash",
                )}
                records.append(record)
                status = "COMPLETE_WITH_PROBE_ERRORS"
                _write_json(MANIFEST_PATH, manifest)
                continue
            except (KisApiError, KisAuthError, OSError) as exc:
                entry["classification"] = "TRANSPORT_ERROR"
                entry["transport_error_type"] = type(exc).__name__
                entry["contract_result"] = "TRANSPORT_ERROR"
                entry["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)
                entry["request_count"] = endpoint_transport.endpoint_calls(item["probe_id"])
                entry["request_status"] = "FINISHED"
                manifest["actual_requests"] = sum(x["request_count"] for x in manifest["requests"])
                records.append({key: entry.get(key) for key in (
                    "probe_id", "symbol", "market", "anchor", "classification",
                    "pre_request_safety_decision", "request_time_utc", "elapsed_ms",
                    "request_count", "retry_count", "request_hash", "transport_error_type",
                )})
                status = "COMPLETE_WITH_PROBE_ERRORS"
                _write_json(MANIFEST_PATH, manifest)
                continue
            except (RuntimeError, TypeError, KeyError, AttributeError) as exc:
                entry["classification"] = "RUNNER_ERROR"
                entry["runner_error_type"] = type(exc).__name__
                entry["contract_result"] = "RUNNER_ERROR"
                entry["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)
                entry["request_count"] = endpoint_transport.endpoint_calls(item["probe_id"])
                entry["request_status"] = "FINISHED"
                manifest["actual_requests"] = sum(x["request_count"] for x in manifest["requests"])
                records.append({key: entry.get(key) for key in (
                    "probe_id", "symbol", "market", "anchor", "classification",
                    "pre_request_safety_decision", "request_time_utc", "elapsed_ms",
                    "request_count", "retry_count", "request_hash", "runner_error_type",
                )})
                status = "STOPPED_RUNNER_ERROR"
                _write_json(MANIFEST_PATH, manifest)
                break

            elapsed = round((time.monotonic() - start) * 1000, 3)
            market_sessions, market_coverage = _load_calendar(item["market"])
            response_hash = phase16.canonical_payload_sha256(payload)
            date_field = source_contract.date_field
            raw_date_extract = phase16c.extract_observation_dates(payload, date_field=date_field)
            response_dates = raw_date_extract["sessions"]
            minimum_returned = min(response_dates) if response_dates else None
            maximum_returned = max(response_dates) if response_dates else None
            if (
                minimum_returned is None
                or maximum_returned is None
                or not market_coverage[0] <= minimum_returned <= maximum_returned <= market_coverage[1]
            ):
                comparable_market_sessions = None
                comparable_market_coverage = None
            else:
                comparable_market_sessions = market_sessions
                comparable_market_coverage = market_coverage
            price_data = _load_price_observations(item["symbol"])
            if (
                price_data is None
                or minimum_returned is None
                or maximum_returned is None
                or not price_data[0][0] <= minimum_returned <= maximum_returned <= price_data[0][-1]
            ):
                price_sessions = None
                active_price_sessions = None
            else:
                price_sessions, active_price_sessions = price_data
            metrics = phase16c.analyze_probe_response(
                payload,
                symbol=item["symbol"],
                market=item["market"],
                anchor=anchor,
                date_field=source_contract.date_field,
                maximum_objects=31,
                anchor_expected=True,
                market_sessions=comparable_market_sessions,
                market_coverage=comparable_market_coverage,
                symbol_price_sessions=price_sessions,
                symbol_active_sessions=active_price_sessions,
                protected_ranges=PROTECTED,
                response_sha256=response_hash,
            )
            entry.update({
                "elapsed_ms": elapsed,
                "request_count": endpoint_transport.endpoint_calls(item["probe_id"]),
                "request_status": "FINISHED",
                "response_row_count": metrics["row_count"],
                "dated_observation_count": metrics["dated_observation_count"],
                "response_min_date": metrics["minimum_returned_date"],
                "response_max_date": metrics["maximum_returned_date"],
                "response_hash": response_hash,
                "schema_fingerprint": metrics["schema_fingerprint"],
                "contract_result": metrics["contract_result"],
                "classification": "CONTRACT_MISMATCH" if metrics["failures"] else "MATCHED_SAMPLE",
                "contract_failures": metrics["failures"],
            })
            manifest["actual_requests"] = sum(x["request_count"] for x in manifest["requests"])
            manifest["retries"] += max(0, entry["request_count"] - 1)
            if "PROTECTED_RANGE_BREACH" in metrics["failures"]:
                # Do not persist returned dates if a protected period is detected.
                entry["response_min_date"] = None
                entry["response_max_date"] = None
                entry["classification"] = "PROTECTED_RANGE_BREACH"
                record_metrics = {
                    "row_count": metrics["row_count"],
                    "dated_observation_count": metrics["dated_observation_count"],
                    "protected_range_breach": True,
                }
                status = "STOPPED_PROTECTED_RANGE_BREACH"
            else:
                record_metrics = metrics
            records.append({
                "probe_id": item["probe_id"],
                "source": item["source"],
                "symbol": item["symbol"],
                "market": item["market"],
                "anchor": item["anchor"],
                "evidence_class": "CONTRACT_REQUALIFICATION_PROBE",
                "request_hash": entry["request_hash"],
                "pre_request_safety_decision": entry["pre_request_safety_decision"],
                "request_time_utc": entry["request_time_utc"],
                "elapsed_ms": elapsed,
                "request_count": entry["request_count"],
                "retry_count": 0,
                "response_hash": response_hash,
                "classification": entry["classification"],
                "metrics": record_metrics,
            })
            _write_json(MANIFEST_PATH, manifest)
            if status == "STOPPED_PROTECTED_RANGE_BREACH":
                break

    result = {
        "schema_version": 1,
        "phase": "PHASE16C",
        "evidence_class": "CONTRACT_REQUALIFICATION_PROBE",
        "source": "kis_per_stock_flow",
        "contract_version": plan["contract_version_at_plan"],
        "sample_sha256": sample_hash,
        "config_sha256": config_sha,
        "planned_requests": len(plan["requests"]),
        "actual_requests": manifest["actual_requests"],
        "retries": manifest["retries"],
        "started_at_utc": started_utc,
        "elapsed_seconds": round(time.monotonic() - started_all, 3),
        "status": status,
        "probe_results": records,
    }
    _write_json(RESULTS_PATH, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true", help="validate frozen inputs; do not request provider data")
    args = parser.parse_args()
    result = _run(validate_only=args.validate_only)
    print(json.dumps({key: value for key, value in result.items() if key != "probe_results"}, sort_keys=True))
    return 0 if result.get("status") in {"VALIDATED_NO_NETWORK", "COMPLETE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from krx_trader.kis.rest import _PersistentRateLimiter
from krx_trader.research.phase15 import EXTERNAL_2026, HOLDOUT_2026
from krx_trader.universe.master import MASTER_URLS, parse_master_archive

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config/phase16/phase16-collector-config.json"
DEFAULT_ROOT = REPO_ROOT / "runtime/research/phase16"
CONFIG_VERSION_DIR = "config"
SCHEMA_VERSION = 1
CANONICALIZATION_VERSION = 1
SECRET_KEYS = re.compile(
    r"(authorization|access[_-]?token|refresh[_-]?token|app[_-]?(key|secret)|"
    r"client[_-]?secret|account([_-]?number)?|acnt[_-]?prdt[_-]?cd|cano|"
    r"customer([_-]?(id|number))?|session[_-]?cookie|cookie)",
    re.IGNORECASE,
)
NUMERIC_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
SENSITIVE_TEXT = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
NON_SEMANTIC_TRANSPORT_KEYS = {"_phase15_pages", "_phase16_http_status", "_phase16_transport_headers"}


class Phase16Error(RuntimeError):
    pass


class AppendOnlyError(Phase16Error):
    pass


class ProtectedRangeError(Phase16Error):
    pass


class CollectionFailure(Phase16Error):
    def __init__(self, category: str, message: str, *, request_count: int, retry_count: int):
        super().__init__(message)
        self.category = category
        self.request_count = request_count
        self.retry_count = retry_count


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
            "utf-8"
        )
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in NON_SEMANTIC_TRANSPORT_KEYS
        }
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        if all(isinstance(item, dict) for item in items):
            return sorted(items, key=_json_bytes)
        return items
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numeric values are not valid evidence")
    return value


def canonical_payload_bytes(value: Any) -> bytes:
    return _json_bytes(_canonical(value))


def sanitized_representation_bytes(value: Any) -> bytes:
    safe, _ = sanitize_payload(value)
    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_payload_bytes(value)).hexdigest()


def sanitize_payload(value: Any) -> tuple[Any, list[str]]:
    """Redact credential-bearing fields while preserving their field names and all other values."""
    flags: list[str] = []

    def visit(item: Any, key: str | None = None) -> Any:
        if key is not None and SECRET_KEYS.search(key):
            flags.append("CREDENTIAL_FIELD_REDACTED")
            return "[REDACTED]"
        if isinstance(item, dict):
            return {str(k): visit(v, str(k)) for k, v in item.items()}
        if isinstance(item, list):
            return [visit(v) for v in item]
        if isinstance(item, str):
            return SENSITIVE_TEXT.sub("Bearer [REDACTED]", item)
        return item

    return visit(value), sorted(set(flags))


def sanitize_received_bytes(value: bytes, *, persist: bool) -> tuple[bytes, list[str]]:
    """Keep exact bytes when safe; redact structured credential values before storage."""
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if persist:
            raise ValueError("raw payload persistence requires a parseable JSON body for sanitization")
        return value, []
    safe, flags = sanitize_payload(decoded)
    if safe != decoded:
        # Re-serialization is needed only when a credential value was found and redacted.
        body = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return body, flags
    return value, flags


def raw_payload_sha256(value: Any) -> str:
    if isinstance(value, bytes):
        body = value
    else:
        body = sanitized_representation_bytes(value)
    return hashlib.sha256(body).hexdigest()


def timezone_pair(value: datetime) -> tuple[str, str]:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evidence timestamps must be timezone-aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds"),
        value.astimezone(KST).isoformat(timespec="microseconds"),
    )


def parse_aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return parsed


def schema_fingerprint(value: Any) -> str:
    fields: dict[str, set[str]] = defaultdict(set)

    def walk(item: Any, path: str) -> None:
        if isinstance(item, dict):
            fields[path or "$"].add("object")
            for key, child in item.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(item, list):
            fields[path or "$"].add("array")
            for child in item:
                walk(child, f"{path}[]")
        else:
            fields[path or "$"].add(type(item).__name__ if item is not None else "null")

    walk(value, "")
    shape = {key: sorted(types) for key, types in sorted(fields.items())}
    return hashlib.sha256(_json_bytes(shape)).hexdigest()


def _safe_component(value: str | None, fallback: str) -> str:
    source = fallback if value is None or value == "" else value
    return re.sub(r"[^A-Za-z0-9.-]+", "-", source).strip("-.")[:72] or fallback


def make_snapshot_id(
    *,
    source_name: str,
    logical_key: str,
    symbol: str | None,
    requested_session: date | None,
    observed_at: datetime,
    canonical_sha256: str,
    raw_sha256: str | None = None,
) -> str:
    timezone_pair(observed_at)
    stamp = observed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    session = requested_session.isoformat() if requested_session else "CURRENT"
    key_hash = hashlib.sha256(logical_key.encode("utf-8")).hexdigest()[:16]
    pieces = [
        _safe_component(source_name, "source")[:40],
        session,
        _safe_component(symbol, "MARKET"),
        key_hash,
        stamp,
        canonical_sha256,
    ]
    if raw_sha256:
        pieces.append(raw_sha256)
    return "__".join(pieces)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_create(path: Path, body: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError as exc:
            raise AppendOnlyError(f"refusing to overwrite existing evidence: {path.name}") from exc
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_replace(path: Path, body: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _file_lock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> tuple[dict[str, Any], str, bytes]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    sidecar = path.with_suffix(".sha256")
    if sidecar.exists():
        expected = sidecar.read_text(encoding="utf-8").split()[0]
        if expected != digest:
            raise Phase16Error("collector config SHA-256 sidecar mismatch")
    config = json.loads(raw)
    if config.get("schema_version") != SCHEMA_VERSION:
        raise Phase16Error("unsupported Phase 16 config schema")
    return config, digest, raw


def protected_ranges(config: dict[str, Any]) -> list[tuple[date, date, str]]:
    configured = config.get("protected_ranges", [])
    if configured:
        return [(date.fromisoformat(row["start"]), date.fromisoformat(row["end"]), row["label"]) for row in configured]
    return [(EXTERNAL_2026[0], EXTERNAL_2026[1], "EXTERNAL_2026"), (HOLDOUT_2026[0], HOLDOUT_2026[1], "HOLDOUT_2026")]


def request_range_guard(
    source_id: str,
    requested_session: date,
    *,
    config: dict[str, Any],
    evidence_class: str = "PROSPECTIVE_OBSERVED",
) -> dict[str, Any]:
    source = config.get("sources", {}).get(source_id)
    if not isinstance(source, dict):
        return {"allowed": False, "classification": "UNKNOWN_SOURCE", "reason": "source is not configured"}
    mode = source.get("request_semantics", "UNKNOWN")
    response_start = requested_session
    response_end = requested_session
    details: list[str] = []
    if mode == "CURRENT_ONLY" and source.get("range_contract_proven"):
        classification = "CURRENT_ONLY_CONFIRMED"
    elif mode == "EXACT_DATE" and source.get("range_contract_proven") and source.get("exact_date_semantics_proven"):
        classification = "EXACT_DATE_CONFIRMED"
    elif mode == "TRAILING_WINDOW":
        classification = "TRAILING_WINDOW_UNPROVEN"
        rows = source.get("observed_rows_per_request")
        if isinstance(rows, int) and rows > 0:
            # Three calendar days per observed row is deliberately conservative for holidays.
            response_start = requested_session - timedelta(days=rows * 3 + 1)
            details.append(f"observed_response_rows={rows}")
        if not source.get("range_contract_proven"):
            details.append("provider_response_range_cap_not_proven")
    else:
        classification = "RESPONSE_RANGE_UNKNOWN"
        details.append("no exact-date or current-only response contract is established")

    overlaps = []
    for protected_start, protected_end, label in protected_ranges(config):
        if response_start <= protected_end and response_end >= protected_start:
            overlaps.append(label)
    requested_protected = [
        label for start, end, label in protected_ranges(config) if start <= requested_session <= end
    ]
    if requested_protected:
        details.append("requested_session_is_protected")
    if overlaps:
        details.append("potential_response_window_overlaps=" + ",".join(overlaps))

    if evidence_class not in {"PROSPECTIVE_OBSERVED", "HISTORICAL_REVISION_PROBE"}:
        details.append("invalid_evidence_class")
    safe_contract = classification in {"CURRENT_ONLY_CONFIRMED", "EXACT_DATE_CONFIRMED"}
    if classification == "TRAILING_WINDOW_UNPROVEN" and evidence_class == "HISTORICAL_REVISION_PROBE":
        # A date anchor before the first protected interval cannot return a later date;
        # Phase 15 verified that these date-anchored response windows end on the anchor.
        first_protected = min(start for start, _, _ in protected_ranges(config))
        safe_contract = requested_session < first_protected
        if safe_contract:
            details.append("historical_anchor_precedes_all_protected_ranges")
    allowed = safe_contract and not requested_protected and not overlaps
    if not safe_contract:
        details.append("response_range_not_proven_safe")
    if not allowed and not details:
        details.append("request denied by fail-closed range guard")
    return {
        "allowed": allowed,
        "request_guard": "ALLOW" if allowed else "DENY",
        "classification": classification,
        "source_id": source_id,
        "evidence_class": evidence_class,
        "requested_session": requested_session.isoformat(),
        "potential_response_start": response_start.isoformat(),
        "potential_response_end": response_end.isoformat(),
        "protected_overlaps": overlaps,
        "reasons": details,
    }


def execute_guarded_request(
    source_id: str,
    requested_session: date,
    *,
    config: dict[str, Any],
    transport: Callable[[], Any],
    evidence_class: str = "PROSPECTIVE_OBSERVED",
) -> Any:
    decision = request_range_guard(source_id, requested_session, config=config, evidence_class=evidence_class)
    if not decision["allowed"]:
        raise ProtectedRangeError("request denied before network access: " + "; ".join(decision["reasons"]))
    return transport()


def source_safety_matrix(config: dict[str, Any], anchor: date) -> dict[str, Any]:
    matrix = []
    for source_id, source in config.get("sources", {}).items():
        decision = request_range_guard(source_id, anchor, config=config)
        enabled = source_id in config.get("enabled_sources", [])
        state = "ACTIVE" if enabled and decision["allowed"] else (
            "DEFERRED_SAFETY_GUARD" if not decision["allowed"] else "READY"
        )
        if source_id == "krx_market_flow":
            state = "DEFERRED_SEMANTICS"
        matrix.append({
            "source_id": source_id,
            "source_name": source.get("source_name"),
            "provider": source.get("provider"),
            "endpoint_id": source.get("endpoint_id"),
            "enabled": enabled,
            "state": state,
            "request_semantics": source.get("request_semantics"),
            "observed_rows_per_request": source.get("observed_rows_per_request"),
            "decision": decision,
            "deferred_reason": source.get("deferred_reason"),
        })
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "anchor_session": anchor.isoformat(),
        "protected_ranges": [
            {"start": start.isoformat(), "end": end.isoformat(), "label": label}
            for start, end, label in protected_ranges(config)
        ],
        "sources": matrix,
    }


def _relative_payload_path(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise Phase16Error("manifest payload path escapes evidence root")
    path = (root / Path(*rel.parts)).resolve()
    if root.resolve() not in path.parents:
        raise Phase16Error("manifest payload path escapes evidence root")
    return path


def _manifest_digest(manifest: dict[str, Any]) -> str:
    value = {key: item for key, item in manifest.items() if key != "current_manifest_sha256"}
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _chain_records(root: Path) -> list[dict[str, Any]]:
    chain = root / "prospective/manifests/manifest-chain.jsonl"
    if not chain.exists():
        return []
    records = []
    for line_number, line in enumerate(chain.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Phase16Error(f"manifest chain has invalid JSON at line {line_number}") from exc
        if not isinstance(item, dict):
            raise Phase16Error(f"manifest chain line {line_number} is not an object")
        records.append(item)
    return records


def _manifest_path(root: Path, snapshot_id: str, evidence_class: str) -> Path:
    tree = "revision_probes" if evidence_class == "HISTORICAL_REVISION_PROBE" else "prospective"
    return root / tree / "manifests/snapshots" / f"{snapshot_id}.json"


def _read_manifest(root: Path, snapshot_id: str, evidence_class: str | None = None) -> dict[str, Any]:
    if evidence_class:
        path = _manifest_path(root, snapshot_id, evidence_class)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    for tree in ("prospective", "revision_probes"):
        path = root / tree / "manifests" / f"{snapshot_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"snapshot manifest not found: {snapshot_id}")


def verify_store(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    errors: list[str] = []
    records = _chain_records(root)
    previous = "0" * 64
    seen_ids: set[str] = set()
    referenced_manifests: set[Path] = set()
    referenced_payloads: set[Path] = set()
    snapshots = []
    for index, record in enumerate(records, start=1):
        snapshot_id = record.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            errors.append(f"chain line {index}: missing snapshot_id")
            continue
        if snapshot_id in seen_ids:
            errors.append(f"chain line {index}: duplicate snapshot_id")
        seen_ids.add(snapshot_id)
        if record.get("sequence") != index:
            errors.append(f"chain line {index}: sequence mismatch")
        if record.get("previous_manifest_sha256") != previous:
            errors.append(f"chain line {index}: previous hash mismatch")
        try:
            manifest = _read_manifest(root, snapshot_id, record.get("evidence_class"))
            mpath = _manifest_path(root, snapshot_id, manifest.get("evidence_class"))
            referenced_manifests.add(mpath.resolve())
            exact_file_hash = hashlib.sha256(mpath.read_bytes()).hexdigest()
            current = _manifest_digest(manifest)
            if exact_file_hash != record.get("manifest_file_sha256"):
                errors.append(f"{snapshot_id}: manifest file hash mismatch")
            if current != manifest.get("current_manifest_sha256") or current != record.get("current_manifest_sha256"):
                errors.append(f"{snapshot_id}: manifest content hash mismatch")
            if manifest.get("previous_manifest_sha256") != previous:
                errors.append(f"{snapshot_id}: manifest previous hash mismatch")
            if manifest.get("snapshot_id") != snapshot_id:
                errors.append(f"{snapshot_id}: manifest identity mismatch")
            for field in ("raw_payload_path", "normalized_payload_path"):
                rel = manifest.get(field)
                if not rel:
                    if field == "normalized_payload_path":
                        errors.append(f"{snapshot_id}: normalized payload reference missing")
                    continue
                try:
                    payload_path = _relative_payload_path(root, rel)
                    referenced_payloads.add(payload_path.resolve())
                    if not payload_path.is_file():
                        errors.append(f"{snapshot_id}: missing {field}")
                        continue
                    actual = hashlib.sha256(payload_path.read_bytes()).hexdigest()
                    expected = manifest.get("raw_payload_sha256") if field == "raw_payload_path" else manifest.get("normalized_payload_sha256")
                    if expected and actual != expected:
                        errors.append(f"{snapshot_id}: {field} hash mismatch")
                except (OSError, Phase16Error) as exc:
                    errors.append(f"{snapshot_id}: invalid {field} ({type(exc).__name__})")
            previous = current
            snapshots.append(manifest)
        except (OSError, ValueError, KeyError, Phase16Error) as exc:
            errors.append(f"{snapshot_id}: manifest verification failed ({type(exc).__name__})")
    for tree in ("prospective", "revision_probes"):
        manifests = root / tree / "manifests/snapshots"
        if manifests.exists():
            for path in manifests.glob("*.json"):
                if path.resolve() not in referenced_manifests:
                    errors.append(f"orphan manifest: {path.relative_to(root)}")
        for subtree in ("raw", "normalized"):
            folder = root / tree / subtree
            if folder.exists():
                for path in folder.glob("*.json"):
                    if path.resolve() not in referenced_payloads:
                        errors.append(f"orphan payload: {path.relative_to(root)}")
    return {
        "valid": not errors,
        "status": "PASS" if not errors else "FAIL",
        "snapshot_count": len(snapshots),
        "chain_tip_sha256": previous if records else None,
        "errors": errors,
    }


class EvidenceStore:
    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def manifests(self) -> list[dict[str, Any]]:
        result = []
        for record in _chain_records(self.root):
            result.append(_read_manifest(self.root, record["snapshot_id"], record.get("evidence_class")))
        return result

    def create_snapshot(
        self,
        *,
        evidence_class: str,
        source_name: str,
        provider: str,
        endpoint_id: str,
        request_schema_version: str,
        logical_key: str,
        payload: Any,
        observed_at: datetime,
        config_sha256: str,
        collector_git_sha: str,
        symbol: str | None = None,
        market: str | None = None,
        requested_session: date | None = None,
        provider_session: date | None = None,
        raw_payload: bytes | Any | None = None,
        persist_raw: bool = True,
        normalized_payload: Any | None = None,
        result_classification: str = "LOCAL_OK",
        record_count: int | None = None,
        provider_timestamp: str | None = None,
        availability_label: str = "MANUAL_BOOTSTRAP",
        quality_flags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        raw_representation_basis: str = "SANITIZED_DECODED_JSON",
    ) -> dict[str, Any]:
        if evidence_class not in {"PROSPECTIVE_OBSERVED", "HISTORICAL_REVISION_PROBE"}:
            raise ValueError("invalid evidence_class")
        observed_utc, observed_kst = timezone_pair(observed_at)
        safe_payload, redact_flags = sanitize_payload(payload)
        norm_value, norm_flags = sanitize_payload(payload if normalized_payload is None else normalized_payload)
        raw_value = payload if raw_payload is None else raw_payload
        if isinstance(raw_value, bytes):
            raw_body, raw_flags = sanitize_received_bytes(raw_value, persist=persist_raw)
            persisted_raw_body = raw_body if persist_raw else None
            redact_flags.extend(raw_flags)
        else:
            _, raw_flags = sanitize_payload(raw_value)
            raw_body = sanitized_representation_bytes(raw_value)
            persisted_raw_body = raw_body
            redact_flags.extend(raw_flags)
        canonical_hash = canonical_payload_sha256(safe_payload)
        raw_hash = hashlib.sha256(raw_body).hexdigest()
        normalized_body = canonical_payload_bytes(norm_value)
        normalized_hash = hashlib.sha256(normalized_body).hexdigest()
        snapshot_id = make_snapshot_id(
            source_name=source_name,
            logical_key=logical_key,
            symbol=symbol,
            requested_session=requested_session,
            observed_at=observed_at,
            canonical_sha256=canonical_hash,
            raw_sha256=raw_hash,
        )
        tree = "revision_probes" if evidence_class == "HISTORICAL_REVISION_PROBE" else "prospective"
        raw_path = self.root / tree / "raw" / f"{snapshot_id}.json" if persist_raw else None
        normalized_path = self.root / tree / "normalized" / f"{snapshot_id}.json"
        manifest_path = _manifest_path(self.root, snapshot_id, evidence_class)

        with _file_lock(self.root / "locks/store.lock") as locked:
            if not locked:
                raise Phase16Error("could not acquire evidence-store lock")
            integrity = verify_store(self.root)
            if not integrity["valid"]:
                raise Phase16Error("existing store is invalid; refusing to append")
            if manifest_path.exists():
                raise AppendOnlyError(f"snapshot ID already exists: {snapshot_id}")
            prior = [m for m in self.manifests() if m.get("logical_key") == logical_key]
            prior.sort(key=lambda item: item["observed_at_utc"])
            previous_vintage = prior[-1]["snapshot_id"] if prior else None
            records = _chain_records(self.root)
            prev_hash = records[-1]["current_manifest_sha256"] if records else "0" * 64
            if persist_raw:
                raw_to_save = persisted_raw_body
                if raw_to_save is None:
                    raw_to_save = _json_bytes(safe_payload)
                _atomic_create(raw_path, raw_to_save)
            _atomic_create(normalized_path, normalized_body)
            all_flags = sorted(set((quality_flags or []) + redact_flags + norm_flags))
            count = record_count
            if count is None:
                count = len(norm_value) if isinstance(norm_value, list) else 1
            manifest: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "evidence_class": evidence_class,
                "source_name": source_name,
                "provider": provider,
                "endpoint_id": endpoint_id,
                "request_schema_version": request_schema_version,
                "logical_key": logical_key,
                "symbol": symbol,
                "market": market,
                "requested_session": requested_session.isoformat() if requested_session else None,
                "provider_session": provider_session.isoformat() if provider_session else None,
                "observed_at_utc": observed_utc,
                "observed_at_kst": observed_kst,
                "collector_git_sha": collector_git_sha,
                "collector_git_dirty": _git_dirty(),
                "collector_config_sha256": config_sha256,
                "raw_payload_sha256": raw_hash,
                "raw_payload_path": raw_path.relative_to(self.root).as_posix() if raw_path else None,
                "raw_payload_persisted": bool(raw_path),
                "raw_representation_basis": raw_representation_basis,
                "canonical_payload_sha256": canonical_hash,
                "normalized_payload_sha256": normalized_hash,
                "normalized_payload_path": normalized_path.relative_to(self.root).as_posix(),
                "schema_fingerprint": schema_fingerprint(safe_payload),
                "result_classification": result_classification,
                "record_count": count,
                "provider_timestamp": provider_timestamp,
                "availability_label": availability_label,
                "quality_flags": all_flags,
                "previous_vintage_id": previous_vintage,
                "previous_manifest_sha256": prev_hash,
                "metadata": metadata or {},
            }
            manifest["current_manifest_sha256"] = _manifest_digest(manifest)
            manifest_body = _json_bytes(manifest, pretty=True)
            _atomic_create(manifest_path, manifest_body)
            chain_record = {
                "sequence": len(records) + 1,
                "snapshot_id": snapshot_id,
                "evidence_class": evidence_class,
                "previous_manifest_sha256": prev_hash,
                "current_manifest_sha256": manifest["current_manifest_sha256"],
                "manifest_file_sha256": hashlib.sha256(manifest_body).hexdigest(),
            }
            chain_path = self.root / "prospective/manifests/manifest-chain.jsonl"
            chain_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(chain_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, _json_bytes(chain_record) + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_directory(chain_path.parent)
        return manifest


def _git_dirty() -> bool:
    try:
        result = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return True
    return bool(result.stdout.strip())


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNAVAILABLE"


def freeze_runtime_config(root: Path, config: dict[str, Any], config_sha: str, config_bytes: bytes) -> Path:
    path = root / CONFIG_VERSION_DIR / f"{config_sha}.json"
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != config_sha:
            raise Phase16Error("frozen runtime config was mutated")
    else:
        _atomic_create(path, config_bytes)
    latest = {"config_version": config.get("config_version"), "config_sha256": config_sha, "path": path.relative_to(root).as_posix()}
    _atomic_replace(root / "config/active.json", _json_bytes(latest, pretty=True))
    return path


def verify_previous_phase_snapshot(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    snapshot = root / "previous-phase-artifact-snapshot.json"
    if not snapshot.exists():
        return {"status": "NOT_AVAILABLE", "verified_count": 0, "changed": ["snapshot missing"]}
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    changed = []
    for item in data.get("artifacts", []):
        path = REPO_ROOT / item["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            changed.append(item["path"])
    return {
        "status": "PASS" if not changed else "FAIL",
        "verified_count": len(data.get("artifacts", [])) - len(changed),
        "artifact_count": len(data.get("artifacts", [])),
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "changed": changed,
    }


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(child, name))
    elif isinstance(value, list):
        ordered = sorted(value, key=_json_bytes) if all(isinstance(item, dict) for item in value) else value
        for index, child in enumerate(ordered):
            result.update(_flatten(child, f"{prefix}[{index}]"))
    else:
        result[prefix or "$root"] = value
    return result


def _numeric_difference(field: str, old: Any, new: Any) -> tuple[str | None, str | None]:
    tail = field.rsplit(".", 1)[-1].lower()
    if any(name in tail for name in ("date", "symbol", "code", "id", "name")):
        return None, None
    if not isinstance(old, (int, float, Decimal, str)) or isinstance(old, bool):
        return None, None
    if not isinstance(new, (int, float, Decimal, str)) or isinstance(new, bool):
        return None, None
    if isinstance(old, str) and not NUMERIC_TEXT.fullmatch(old.strip()):
        return None, None
    if isinstance(new, str) and not NUMERIC_TEXT.fullmatch(new.strip()):
        return None, None
    try:
        left, right = Decimal(str(old)), Decimal(str(new))
    except InvalidOperation:
        return None, None
    difference = abs(right - left)
    relative = difference / abs(left) if left != 0 else None
    return str(difference), str(relative) if relative is not None else None


def classify_revision(
    old_manifest: dict[str, Any] | None,
    new_manifest: dict[str, Any],
    old_payload: Any | None,
    new_payload: Any | None,
) -> dict[str, Any]:
    if old_manifest is None or old_payload is None or new_payload is None:
        classification = "UNCOMPARABLE"
        field_changes: list[dict[str, Any]] = []
    elif old_manifest.get("raw_payload_sha256") == new_manifest.get("raw_payload_sha256"):
        classification = "IDENTICAL"
        field_changes = []
    elif old_manifest.get("canonical_payload_sha256") == new_manifest.get("canonical_payload_sha256"):
        classification = "SEMANTICALLY_IDENTICAL"
        field_changes = []
    else:
        old_fields, new_fields = _flatten(old_payload), _flatten(new_payload)
        all_fields = sorted(set(old_fields) | set(new_fields))
        field_changes = []
        missing_to_present = False
        present_to_missing = False
        for field in all_fields:
            old_exists, new_exists = field in old_fields, field in new_fields
            old_value, new_value = old_fields.get(field), new_fields.get(field)
            if not old_exists:
                field_changes.append({"field": field, "change": "FIELD_ADDED", "old_value": None, "new_value": new_value})
                continue
            if not new_exists:
                field_changes.append({"field": field, "change": "FIELD_REMOVED", "old_value": old_value, "new_value": None})
                continue
            if old_value is None and new_value is not None:
                missing_to_present = True
                change_type = "MISSING_TO_PRESENT"
            elif old_value is not None and new_value is None:
                present_to_missing = True
                change_type = "PRESENT_TO_MISSING"
            elif type(old_value) is not type(new_value):
                change_type = "SCHEMA_CHANGED"
            elif old_value != new_value:
                change_type = "VALUE_REVISED"
            else:
                continue
            absolute, relative = _numeric_difference(field, old_value, new_value)
            field_changes.append({
                "field": field,
                "change": change_type,
                "old_value": old_value,
                "new_value": new_value,
                "absolute_difference": absolute,
                "relative_difference": relative,
            })
        changes = {item["change"] for item in field_changes}
        if changes == {"FIELD_ADDED"}:
            classification = "FIELD_ADDED"
        elif changes == {"FIELD_REMOVED"}:
            classification = "FIELD_REMOVED"
        elif "SCHEMA_CHANGED" in changes or old_manifest.get("schema_fingerprint") != new_manifest.get("schema_fingerprint"):
            classification = "SCHEMA_CHANGED"
        elif missing_to_present and not present_to_missing:
            classification = "MISSING_TO_PRESENT"
        elif present_to_missing and not missing_to_present:
            classification = "PRESENT_TO_MISSING"
        elif any(item["change"] == "VALUE_REVISED" for item in field_changes):
            classification = "VALUE_REVISED"
        elif "FIELD_ADDED" in changes:
            classification = "FIELD_ADDED"
        elif "FIELD_REMOVED" in changes:
            classification = "FIELD_REMOVED"
        else:
            classification = "UNCOMPARABLE"

    earlier = parse_aware_datetime(old_manifest["observed_at_utc"]) if old_manifest else None
    later = parse_aware_datetime(new_manifest["observed_at_utc"])
    age = max(0.0, (later - earlier).total_seconds()) if earlier else None
    return {
        "schema_version": 1,
        "revision_id": f"{old_manifest['snapshot_id'] if old_manifest else 'NONE'}__{new_manifest['snapshot_id']}",
        "logical_key": new_manifest.get("logical_key"),
        "session_date": new_manifest.get("provider_session") or new_manifest.get("requested_session"),
        "earlier_snapshot_id": old_manifest.get("snapshot_id") if old_manifest else None,
        "later_snapshot_id": new_manifest.get("snapshot_id"),
        "earlier_observed_at_utc": old_manifest.get("observed_at_utc") if old_manifest else None,
        "later_observed_at_utc": new_manifest.get("observed_at_utc"),
        "age_at_later_observation_seconds": age,
        "business_sessions_since_event": None,
        "business_session_count_status": "NOT_COMPUTABLE_NO_VERIFIED_KRX_CALENDAR",
        "classification": classification,
        "field_changes": field_changes,
    }


def _load_normalized(root: Path, manifest: dict[str, Any]) -> Any:
    path = _relative_payload_path(root, manifest["normalized_payload_path"])
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_flow_payload(
    payload: dict[str, Any],
    *,
    symbol: str,
    market: str | None,
    observed_at: datetime,
    source: str,
    snapshot_id: str,
) -> list[dict[str, Any]]:
    """Preserve provider fields without assigning unresolved semantics or units."""
    observed_utc, observed_kst = timezone_pair(observed_at)
    records: list[dict[str, Any]] = []
    for key in ("output1", "output2", "output"):
        block = payload.get(key)
        if isinstance(block, dict):
            block = [block]
        if not isinstance(block, list):
            continue
        for row in block:
            if not isinstance(row, dict):
                continue
            value = row.get("stck_bsop_date") or row.get("bsop_date") or row.get("date")
            session_date = None
            if isinstance(value, str):
                raw_date = value.replace("-", "")
                if re.fullmatch(r"\d{8}", raw_date):
                    session_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            for field_name, field_value in row.items():
                numeric = isinstance(field_value, (int, float, Decimal)) and not isinstance(field_value, bool)
                numeric = numeric or (isinstance(field_value, str) and bool(NUMERIC_TEXT.fullmatch(field_value.strip())))
                records.append({
                    "session_date": session_date,
                    "symbol": symbol,
                    "market": market,
                    "provider_field_name": str(field_name),
                    "semantic_status": "UNRESOLVED",
                    "raw_numeric_value": field_value if numeric else None,
                    "raw_value": field_value,
                    "normalized_numeric_value": None,
                    "unit": None,
                    "unit_status": "UNKNOWN",
                    "observed_at_utc": observed_utc,
                    "observed_at_kst": observed_kst,
                    "source": source,
                    "snapshot_id": snapshot_id,
                    "provider_field_group": "orgn_UNRESOLVED" if str(field_name).startswith("orgn") else None,
                })
    return records


def _revision_event_path(root: Path, revision_id: str) -> Path:
    name = hashlib.sha256(revision_id.encode("utf-8")).hexdigest() + ".json"
    return root / "revision/events" / name


def build_revision_records(root: Path = DEFAULT_ROOT) -> list[dict[str, Any]]:
    manifests = EvidenceStore(root).manifests()
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in manifests:
        by_key[item["logical_key"]].append(item)
    output = []
    for group in by_key.values():
        group.sort(key=lambda item: item["observed_at_utc"])
        for old, new in pairwise(group):
            record = classify_revision(old, new, _load_normalized(root, old), _load_normalized(root, new))
            path = _revision_event_path(root, record["revision_id"])
            body = _json_bytes(record, pretty=True)
            if path.exists():
                if path.read_bytes() != body:
                    raise AppendOnlyError("revision event ID was reused with different content")
            else:
                _atomic_create(path, body)
            output.append(record)
    output.sort(key=lambda row: (row["later_observed_at_utc"], row["revision_id"]))
    _atomic_replace(root / "revision/revision-index.json", _json_bytes(output, pretty=True))
    summary = summarize_revisions(output, manifests)
    _atomic_replace(root / "revision/revision-summary.json", _json_bytes(summary, pretty=True))
    _record_schema_drift_events(root, manifests)
    return output


def _record_schema_drift_events(root: Path, manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_source: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for manifest in manifests:
        by_source[(manifest["source_name"], manifest["evidence_class"])].append(manifest)
    events = []
    for (source_name, evidence_class), group in by_source.items():
        group.sort(key=lambda row: row["observed_at_utc"])
        for old, new in pairwise(group):
            if old.get("schema_fingerprint") == new.get("schema_fingerprint"):
                continue
            event = {
                "schema_version": 1,
                "event_type": "SCHEMA_CHANGED",
                "source_name": source_name,
                "evidence_class": evidence_class,
                "earlier_snapshot_id": old["snapshot_id"],
                "later_snapshot_id": new["snapshot_id"],
                "earlier_observed_at_utc": old["observed_at_utc"],
                "later_observed_at_utc": new["observed_at_utc"],
                "earlier_schema_fingerprint": old.get("schema_fingerprint"),
                "later_schema_fingerprint": new.get("schema_fingerprint"),
            }
            event_id = hashlib.sha256(_json_bytes(event)).hexdigest()
            path = root / "revision/schema-events" / f"{event_id}.json"
            body = _json_bytes(event, pretty=True)
            if path.exists():
                if path.read_bytes() != body:
                    raise AppendOnlyError("schema event ID was reused with different content")
            else:
                _atomic_create(path, body)
            events.append(event)
    _atomic_replace(root / "revision/schema-drift-index.json", _json_bytes(events, pretty=True))
    return events


def summarize_revisions(records: list[dict[str, Any]], manifests: list[dict[str, Any]]) -> dict[str, Any]:
    comparisons = len(records)
    classes = Counter(row["classification"] for row in records)
    ages_by_key: dict[str, list[float]] = defaultdict(list)
    for row in records:
        if row.get("age_at_later_observation_seconds") is not None:
            ages_by_key[str(row.get("logical_key"))].append(row["age_at_later_observation_seconds"])
    terminal_ages = sorted((max(values) for values in ages_by_key.values() if values), key=float)
    def percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        return values[min(len(values) - 1, math.ceil(p * len(values)) - 1)]
    multi_vintage_sessions = {
        row.get("provider_session") or row.get("requested_session")
        for row in manifests
        if row.get("previous_vintage_id") and (row.get("provider_session") or row.get("requested_session"))
    }
    return {
        "schema_version": 1,
        "comparison_count": comparisons,
        "observations_with_multiple_vintages": len(multi_vintage_sessions),
        "classification_counts": dict(sorted(classes.items())),
        "identical_fraction": classes.get("IDENTICAL", 0) / comparisons if comparisons else None,
        "semantic_identical_fraction": classes.get("SEMANTICALLY_IDENTICAL", 0) / comparisons if comparisons else None,
        "revised_fraction": sum(classes.get(key, 0) for key in ("VALUE_REVISED", "MISSING_TO_PRESENT", "PRESENT_TO_MISSING", "FIELD_ADDED", "FIELD_REMOVED", "SCHEMA_CHANGED")) / comparisons if comparisons else None,
        "schema_change_fraction": classes.get("SCHEMA_CHANGED", 0) / comparisons if comparisons else None,
        "median_time_to_last_observed_revision_seconds": percentile(terminal_ages, 0.5),
        "p90_time_to_last_observed_revision_seconds": percentile(terminal_ages, 0.9),
        "stability_interpretation": "DESCRIPTIVE_ONLY; no final stability claim without the predeclared prospective evidence gate",
    }


def _atomic_report(root: Path, relative: str, value: Any) -> Path:
    path = root / relative
    _atomic_replace(path, _json_bytes(value, pretty=True))
    return path


def _hash_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_config(root: Path, config: dict[str, Any], config_sha: str, config_bytes: bytes) -> None:
    freeze_runtime_config(root, config, config_sha, config_bytes)
    _atomic_replace(root / "phase16-collector-config.json", config_bytes)
    _atomic_replace(root / "phase16-collector-config.sha256", f"{config_sha}  phase16-collector-config.json\n".encode())


def _record_source_safety(root: Path, config: dict[str, Any], now: datetime) -> dict[str, Any]:
    matrix = source_safety_matrix(config, now.astimezone(KST).date())
    _atomic_report(root, "reports/source-safety-matrix.json", matrix)
    _atomic_report(root, "reports/protected-response-guard.json", {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "protected_ranges": matrix["protected_ranges"],
        "source_decisions": [
            {"source_id": row["source_id"], "request_guard": row["decision"]["request_guard"], "reasons": row["decision"]["reasons"], "potential_response_start": row["decision"]["potential_response_start"], "potential_response_end": row["decision"]["potential_response_end"]}
            for row in matrix["sources"]
        ],
    })
    return matrix


def _read_cohort_symbols(path: Path, expected_sha: str) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    values = data.get("cohort_100", {}).get("symbols", [])
    if len(values) != 100 or any(not isinstance(value, str) or not re.fullmatch(r"\d{6}", value) for value in values):
        raise Phase16Error("frozen Phase 4 cohort is missing or malformed")
    actual = hashlib.sha256(_json_bytes(values)).hexdigest()
    stored_hash = data.get("cohort_100", {}).get("symbols_sha256") or data.get("cohort_100", {}).get("cohort_sha256")
    if stored_hash and stored_hash != actual:
        raise Phase16Error("Phase 4 frozen cohort symbol hash mismatch")
    if expected_sha and expected_sha not in {actual, stored_hash}:
        # Some prior phase manifests hash a newline-joined list rather than JSON.
        alternate = hashlib.sha256(("\n".join(values) + "\n").encode()).hexdigest()
        if expected_sha != alternate:
            raise Phase16Error("configured frozen cohort hash does not match Phase 4 cohort")
    return values


def _listing_rows(archives: dict[str, bytes]) -> list[dict[str, Any]]:
    from krx_trader.universe.master import StockMaster

    rows: list[StockMaster] = []
    for market in sorted(archives):
        rows.extend(parse_master_archive(archives[market], market))
    rows.sort(key=lambda item: (item.market, item.symbol))
    return [
        {
            "symbol": row.symbol,
            "market": row.market,
            "company_name": row.name,
            "listing_status": row.listing_status,
            "instrument_type": row.instrument_type,
            "halted": row.halted,
            "management": row.management,
            "warning_status": row.warning_status,
            "is_preferred": row.is_preferred,
            "is_etp": row.is_etp,
            "is_spac": row.is_spac,
            "symbol_lineage": "UNKNOWN",
        }
        for row in rows
    ]


def _fetch_public_archive(url: str, *, limiter: _PersistentRateLimiter, max_retries: int, sleeper: Callable[[float], None]) -> tuple[bytes, int]:
    request = urllib.request.Request(url, headers={"User-Agent": "krx-short-term-trader/0.1"})
    attempts = 0
    while True:
        limiter.wait()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = getattr(response, "status", 200)
                body = response.read()
            if status != 200:
                raise urllib.error.HTTPError(url, status, "KIS master request failed", None, None)
            return body, attempts
        except urllib.error.HTTPError as exc:
            transient = exc.code == 429 or exc.code in {500, 502, 503, 504}
            if not transient or attempts >= max_retries:
                raise CollectionFailure("PROVIDER_ERROR", f"current listing request failed (HTTP {exc.code})", request_count=attempts + 1, retry_count=attempts) from None
            if exc.code == 429:
                limiter.defer(60, rate_limited=True)
            else:
                sleeper(0.5 * (2**attempts))
            attempts += 1
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempts >= max_retries:
                raise CollectionFailure("TRANSPORT_ERROR", f"current listing transport failed ({type(exc).__name__})", request_count=attempts + 1, retry_count=attempts) from None
            sleeper(0.5 * (2**attempts))
            attempts += 1


def classify_observation_delay(scheduled_for: datetime, observed_at: datetime) -> str:
    timezone_pair(scheduled_for)
    timezone_pair(observed_at)
    if observed_at.astimezone(UTC).date() > scheduled_for.astimezone(UTC).date():
        return "LATE_CATCHUP"
    delay_seconds = (observed_at.astimezone(UTC) - scheduled_for.astimezone(UTC)).total_seconds()
    return "POST_20KST" if 0 <= delay_seconds <= 3600 else "LATE_CATCHUP"


def _batch_id(observed_at: datetime) -> str:
    return f"phase16-{observed_at.astimezone(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"


def _write_batch(root: Path, batch: dict[str, Any]) -> Path:
    path = root / "prospective/manifests/batches" / f"{batch['batch_id']}.json"
    _atomic_create(path, _json_bytes(batch, pretty=True))
    return path


def _record_failed_universe_batch(
    *,
    root: Path,
    config: dict[str, Any],
    config_sha: str,
    observed_at: datetime,
    batch_id: str,
    target_symbols: list[str],
    request_count: int,
    retry_count: int,
    category: str,
    failure_market: str | None,
    error: Exception,
) -> dict[str, Any]:
    batch = {
        "schema_version": 1,
        "batch_id": batch_id,
        "status": "FAILED",
        "evidence_class": "PROSPECTIVE_OBSERVED",
        "source_name": "KIS current listings",
        "observed_at_utc": observed_at.astimezone(UTC).isoformat(),
        "target_symbols": len(target_symbols),
        "successful_symbols": 0,
        "missing_symbols": [],
        "failed_symbols": target_symbols,
        "provider_errors": int(category == "PROVIDER_ERROR"),
        "transport_errors": int(category == "TRANSPORT_ERROR"),
        "schema_errors": int(category == "SCHEMA_ERROR"),
        "failure_category": category,
        "failure_market": failure_market,
        "error_class": type(error).__name__,
        "request_count": request_count,
        "retry_count": retry_count,
        "snapshot_count": 0,
        "coverage": 0.0,
        "config_sha256": config_sha,
    }
    _write_batch(root, batch)
    _atomic_report(root, "reports/live-bootstrap.json", {
        "status": "FAILED",
        "universe_snapshot": "FAILED",
        "flow_bootstrap": "DEFERRED_SAFETY_GUARD",
        "flow_requests_made": 0,
        "target_symbols": len(target_symbols),
        "successful_symbols": 0,
        "request_count": request_count,
        "retry_count": retry_count,
        "failure_category": category,
        "failure_market": failure_market,
        "error_class": type(error).__name__,
        "config_sha256": config_sha,
    })
    _refresh_indexes(root, config, config_sha)
    _write_artifact_index(root)
    return batch


def _snapshot_index(root: Path) -> list[dict[str, Any]]:
    manifests = EvidenceStore(root).manifests()
    return [
        {
            "snapshot_id": item["snapshot_id"],
            "evidence_class": item["evidence_class"],
            "source_name": item["source_name"],
            "logical_key": item["logical_key"],
            "symbol": item.get("symbol"),
            "market": item.get("market"),
            "requested_session": item.get("requested_session"),
            "observed_at_utc": item["observed_at_utc"],
            "canonical_payload_sha256": item["canonical_payload_sha256"],
            "current_manifest_sha256": item["current_manifest_sha256"],
        }
        for item in manifests
    ]


def _write_operational_artifacts(
    root: Path,
    config: dict[str, Any],
    config_sha: str,
    report: dict[str, Any],
) -> None:
    probe = config["cohorts"]["probe_cohort"]
    probe_symbols = probe["symbols"]
    probe_sha = hashlib.sha256(_json_bytes(probe_symbols)).hexdigest()
    _atomic_report(root, "config/collection-schedule.json", config["observation_schedule"])
    _atomic_report(root, "config/probe-cohort.json", {
        "scope": probe["scope"],
        "symbols": probe_symbols,
        "symbols_sha256": probe_sha,
    })
    _atomic_report(root, "health/storage-usage.json", report["storage_usage"])

    safety_path = root / "reports/source-safety-matrix.json"
    safety = json.loads(safety_path.read_text(encoding="utf-8")) if safety_path.is_file() else {"sources": []}
    states = {row["source_id"]: row["state"] for row in safety.get("sources", [])}
    bootstrap_path = root / "reports/live-bootstrap.json"
    bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8")) if bootstrap_path.is_file() else {"status": "NOT_RUN"}
    scheduler_path = root / "reports/scheduler-status.json"
    scheduler = json.loads(scheduler_path.read_text(encoding="utf-8")) if scheduler_path.is_file() else {"status": "READY_NOT_INSTALLED", "reason": "not installed"}
    schema_events = root / "revision/schema-events"
    comparison_count = int(report.get("revision_comparison_count", 0))
    summary = {
        "schema_version": 1,
        "phase16_builder": "COMPLETE" if report.get("manifest_chain_valid") else "PARTIAL",
        "immutable_vintage_store": "PASS" if report.get("manifest_chain_valid") else "FAIL",
        "manifest_chain": "PASS" if report.get("manifest_chain_valid") else "FAIL",
        "protected_response_guard": "PASS" if safety.get("sources") and all(
            not row["decision"].get("protected_overlaps") or row["decision"].get("request_guard") == "DENY"
            for row in safety["sources"]
        ) else "NOT_RUN" if not safety.get("sources") else "FAIL",
        "per_stock_flow_prospective": states.get("kis_per_stock_flow", "UNAVAILABLE"),
        "market_flow_prospective": states.get("kis_market_flow", "UNAVAILABLE"),
        "program_flow_prospective": states.get("kis_program_flow", "UNAVAILABLE"),
        "universe_snapshot": "ACTIVE" if any(
            item.get("metadata", {}).get("universe_scope") for item in EvidenceStore(root).manifests()
        ) else "READY",
        "microstructure_prospective": config["microstructure"]["status"],
        "revision_tracker": "PASS" if report.get("manifest_chain_valid") else "FAIL",
        "live_bootstrap": bootstrap.get("status", "NOT_RUN"),
        "first_observation_at_utc": report.get("first_observation") or report.get("first_observation_at_utc"),
        "snapshot_count": report.get("snapshot_count", 0),
        "prospective_sessions": report.get("sessions_observed", 0),
        "multi_vintage_sessions": len({
            item.get("provider_session") or item.get("requested_session")
            for item in EvidenceStore(root).manifests()
            if item.get("previous_vintage_id") and (item.get("provider_session") or item.get("requested_session"))
        }),
        "revision_comparison_count": comparison_count,
        "revised_observations": report.get("revised_observations", 0),
        "schema_drift_status": "SCHEMA_CHANGED" if schema_events.exists() and any(schema_events.glob("*.json")) else "NONE_OBSERVED",
        "manifest_chain_tip_sha256": report.get("manifest_chain_tip_sha256"),
        "coverage": report.get("coverage", []),
        "collector_config_sha256": config_sha,
        "schedule": config["observation_schedule"],
        "scheduler": scheduler,
        "missed_observation_policy": config["observation_schedule"]["missed_observation_policy"],
        "storage_usage": report["storage_usage"],
        "evidence_export": "phase16 export-evidence",
        "source_stability": report.get("source_stability"),
        "phase17_source_stability_ready": report.get("phase17_source_stability_ready"),
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
        "alpha": "UNPROVEN",
        "shadow_next_session": "NO",
        "live": "DISABLED",
    }
    _atomic_report(root, "reports/phase16-summary.json", summary)


def _refresh_indexes(root: Path, config: dict[str, Any], config_sha: str) -> dict[str, Any]:
    integrity = verify_store(root)
    manifests = EvidenceStore(root).manifests()
    _atomic_report(root, "prospective/manifests/snapshot-index.json", _snapshot_index(root))
    batches = []
    batch_dir = root / "prospective/manifests/batches"
    if batch_dir.exists():
        for path in sorted(batch_dir.glob("*.json")):
            batches.append(json.loads(path.read_text(encoding="utf-8")))
    _atomic_report(root, "prospective/manifests/batch-index.json", batches)
    universe = [m for m in manifests if m.get("metadata", {}).get("universe_scope")]
    _atomic_report(root, "universe/universe-snapshot-index.json", [
        {"snapshot_id": m["snapshot_id"], "scope": m["metadata"]["universe_scope"], "observed_at_utc": m["observed_at_utc"], "record_count": m["record_count"], "coverage": m["metadata"].get("coverage")}
        for m in universe
    ])
    revisions = build_revision_records(root)
    _atomic_report(root, "revision/revision-index.json", revisions)
    core_source = "KIS per-stock investor daily flow"
    flow_manifests = [m for m in manifests if m.get("source_name") == core_source and m.get("evidence_class") == "PROSPECTIVE_OBSERVED"]
    sessions = {m.get("requested_session") or m.get("provider_session") for m in flow_manifests if m.get("requested_session") or m.get("provider_session")}
    multi_sessions = {m.get("provider_session") or m.get("requested_session") for m in flow_manifests if m.get("previous_vintage_id") and (m.get("provider_session") or m.get("requested_session"))}
    probe_hash = hashlib.sha256(_json_bytes(config["cohorts"]["probe_cohort"]["symbols"])).hexdigest()
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "collector_config_sha256": config_sha,
        "first_observation": min((m["observed_at_utc"] for m in manifests), default=None),
        "latest_observation": max((m["observed_at_utc"] for m in manifests), default=None),
        "sources_enabled": config.get("enabled_sources", []),
        "snapshot_count": len(manifests),
        "sessions_observed": len(sessions),
        "full_cohort_sessions": len({m.get("requested_session") for m in manifests if m.get("metadata", {}).get("universe_scope") == "FROZEN_RESEARCH_COHORT" and m.get("requested_session")}),
        "probe_sessions": len({m.get("requested_session") for m in flow_manifests if m.get("metadata", {}).get("cohort") == "PROBE"}),
        "revision_comparison_count": len(revisions),
        "revised_observations": sum(r["classification"] not in {"IDENTICAL", "SEMANTICALLY_IDENTICAL"} for r in revisions),
        "probe_cohort_sha256": probe_hash,
        "coverage": [m.get("metadata", {}).get("coverage") for m in universe],
        "manifest_chain_valid": integrity["valid"],
        "manifest_chain_tip_sha256": integrity["chain_tip_sha256"],
        "latest_batch_status": batches[-1].get("status") if batches else None,
        "storage_usage": storage_usage(root, config),
        "source_stability": "INSUFFICIENT_OBSERVATIONS" if len(sessions) < config["source_stability_gate"]["minimum_completed_sessions"] or len(multi_sessions) < config["source_stability_gate"]["minimum_multi_vintage_sessions"] else "READY_FOR_REVIEW",
        "phase17_source_stability_ready": "YES" if len(sessions) >= config["source_stability_gate"]["minimum_completed_sessions"] and len(multi_sessions) >= config["source_stability_gate"]["minimum_multi_vintage_sessions"] else "NO",
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
        "alpha": "UNPROVEN",
        "shadow_next_session": "NO",
        "live": "DISABLED",
    }
    _atomic_report(root, "reports/collection-status.json", report)
    _write_operational_artifacts(root, config, config_sha, report)
    first_path = root / "prospective/manifests/first-observation.json"
    if report["first_observation"] and not first_path.exists():
        first_manifest = min(manifests, key=lambda m: m["observed_at_utc"])
        _atomic_create(first_path, _json_bytes({
            "first_observed_at_utc": first_manifest["observed_at_utc"],
            "first_observed_at_kst": first_manifest["observed_at_kst"],
            "first_observation_date": first_manifest["observed_at_kst"][:10],
            "first_session_date": first_manifest.get("requested_session") or first_manifest.get("provider_session"),
            "source_name": first_manifest["source_name"],
            "evidence_class": first_manifest["evidence_class"],
            "snapshot_id": first_manifest["snapshot_id"],
            "note": "A current-listings snapshot does not imply a completed investor-flow market session.",
        }, pretty=True))
    return report


def storage_usage(root: Path = DEFAULT_ROOT, config: dict[str, Any] | None = None) -> dict[str, Any]:
    counts = {"raw_bytes": 0, "normalized_bytes": 0, "manifest_bytes": 0, "other_bytes": 0}
    categories = (("raw", "raw_bytes"), ("normalized", "normalized_bytes"), ("manifests", "manifest_bytes"))
    for tree in ("prospective", "revision_probes"):
        for folder_name, key in categories:
            folder = root / tree / folder_name
            if folder.exists():
                counts[key] += sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
    known = sum(counts.values())
    if root.exists():
        total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and ".tmp" not in p.name)
    else:
        total = 0
    counts["other_bytes"] = max(0, total - known)
    counts["total_bytes"] = total
    thresholds = (config or {}).get("storage", {})
    warn = int(thresholds.get("warning_bytes", 5 * 1024**3))
    critical = int(thresholds.get("critical_bytes", 10 * 1024**3))
    counts["status"] = "CRITICAL" if total >= critical else "WARNING" if total >= warn else "OK"
    counts["warning_threshold_bytes"] = warn
    counts["critical_threshold_bytes"] = critical
    return counts


def _append_missed_events(root: Path, observed_at: datetime, schedule_time: str = "20:15") -> list[dict[str, Any]]:
    state_path = root / "health/scheduler-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    today = observed_at.astimezone(KST).date()
    previous = date.fromisoformat(state["last_observed_date"]) if state.get("last_observed_date") else None
    missing = []
    if previous:
        day = previous + timedelta(days=1)
        while day < today:
            missing.append({"status": "MISSED_OBSERVATION", "scheduled_date": day.isoformat(), "scheduled_slot_kst": schedule_time, "observed_later": False, "backfilled": False})
            day += timedelta(days=1)
    for item in missing:
        path = root / "health/missed-observations" / f"{item['scheduled_date']}.json"
        if not path.exists():
            _atomic_create(path, _json_bytes(item, pretty=True))
    _atomic_replace(state_path, _json_bytes({"last_observed_date": today.isoformat(), "last_observed_at_utc": observed_at.astimezone(UTC).isoformat()}, pretty=True))
    return missing


def collect_universe(
    *,
    root: Path = DEFAULT_ROOT,
    config_path: Path = DEFAULT_CONFIG_PATH,
    observed_at: datetime | None = None,
    availability_label: str = "MANUAL_BOOTSTRAP",
    fetch_archive: Callable[[str, _PersistentRateLimiter, int], tuple[bytes, int]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    config, config_sha, config_bytes = load_config(config_path)
    clock = clock or (lambda: datetime.now(KST))
    observed_at = observed_at or clock()
    timezone_pair(observed_at)
    _runtime_config(root, config, config_sha, config_bytes)
    before = verify_previous_phase_snapshot(root)
    if before["status"] != "PASS":
        raise Phase16Error("previous-phase evidence integrity check failed before collection")
    _record_source_safety(root, config, observed_at)
    decision = request_range_guard("kis_current_listings", observed_at.astimezone(KST).date(), config=config)
    if not decision["allowed"]:
        _atomic_report(root, "reports/collection-decision.json", decision)
        raise ProtectedRangeError("current listing request denied before network access")
    configured_urls = config["sources"]["kis_current_listings"].get("urls", {})
    if configured_urls != MASTER_URLS:
        raise Phase16Error("configured current-listing URLs differ from the reviewed repository adapter")
    lock_path = root / "locks/collector.lock"
    with _file_lock(lock_path, blocking=False) as locked:
        if not locked:
            now = clock()
            batch = {
                "schema_version": 1,
                "batch_id": _batch_id(now),
                "status": "ALREADY_RUNNING",
                "source_name": "KIS current listings",
                "observed_at_utc": now.astimezone(UTC).isoformat(),
                "request_count": 0,
                "retry_count": 0,
                "snapshot_count": 0,
                "target_symbols": 0,
                "successful_symbols": 0,
                "missing_symbols": [],
                "failed_symbols": [],
                "provider_errors": 0,
                "transport_errors": 0,
                "schema_errors": 0,
                "coverage": 0.0,
            }
            _write_batch(root, batch)
            result = {**batch, "snapshot_count": 0}
            _atomic_report(root, "reports/collection-decision.json", result)
            return result
        start = time.monotonic()
        limiter = _PersistentRateLimiter(
            REPO_ROOT / config["request_policy"]["shared_limiter_path"],
            float(config["request_policy"]["authenticated_kis_min_interval_seconds"]),
            time.sleep,
            time.time,
        )
        if fetch_archive is None:
            fetch_archive = lambda url, shared_limiter, retries: _fetch_public_archive(
                url,
                limiter=shared_limiter,
                max_retries=retries,
                sleeper=time.sleep,
            )
        cohort_config = config["cohorts"]["full_research_cohort"]
        symbols = _read_cohort_symbols(REPO_ROOT / cohort_config["path"], cohort_config["cohort_sha256"])
        archives: dict[str, bytes] = {}
        retry_count = 0
        request_count = 0
        received_at: dict[str, str] = {}
        current_market = None
        try:
            for market in ("KOSPI", "KOSDAQ"):
                current_market = market
                body, retries = fetch_archive(MASTER_URLS[market], limiter, int(config["request_policy"]["max_retries"]))
                if not isinstance(body, bytes) or not body:
                    raise CollectionFailure("TRANSPORT_ERROR", f"empty current listing archive for {market}", request_count=1, retry_count=0)
                archives[market] = body
                retry_count += retries
                request_count += 1 + retries
                received_at[market] = clock().astimezone(UTC).isoformat(timespec="microseconds")
            rows = _listing_rows(archives)
        except CollectionFailure as exc:
            request_count += exc.request_count
            retry_count += exc.retry_count
            _record_failed_universe_batch(
                root=root,
                config=config,
                config_sha=config_sha,
                observed_at=clock(),
                batch_id=_batch_id(clock()),
                target_symbols=symbols,
                request_count=request_count,
                retry_count=retry_count,
                category=exc.category,
                failure_market=current_market,
                error=exc,
            )
            return {
                "status": "FAILED",
                "failure_category": exc.category,
                "failure_market": current_market,
                "target_symbols": len(symbols),
                "successful_symbols": 0,
                "request_count": request_count,
                "retry_count": retry_count,
                "snapshot_count": 0,
            }
        except (ValueError, Phase16Error) as exc:
            category = "SCHEMA_ERROR" if isinstance(exc, ValueError) else "TRANSPORT_ERROR"
            _record_failed_universe_batch(
                root=root,
                config=config,
                config_sha=config_sha,
                observed_at=clock(),
                batch_id=_batch_id(clock()),
                target_symbols=symbols,
                request_count=request_count,
                retry_count=retry_count,
                category=category,
                failure_market=current_market,
                error=exc,
            )
            return {
                "status": "FAILED",
                "failure_category": category,
                "failure_market": current_market,
                "target_symbols": len(symbols),
                "successful_symbols": 0,
                "request_count": request_count,
                "retry_count": retry_count,
                "snapshot_count": 0,
            }
        if not rows:
            raise Phase16Error("current listing source returned no parsed listings")
        by_symbol = {row["symbol"]: row for row in rows}
        cohort_rows = []
        missing_symbols = []
        for symbol in symbols:
            current = by_symbol.get(symbol)
            if current is None:
                missing_symbols.append(symbol)
                cohort_rows.append({"symbol": symbol, "market": None, "listing_status": None, "current_master_presence": False, "symbol_lineage": "UNKNOWN"})
            else:
                cohort_rows.append({**current, "current_master_presence": True})
        observed_at = clock()
        effective_availability = availability_label
        if availability_label == "POST_20KST":
            local_now = observed_at.astimezone(KST)
            scheduled_for = datetime.combine(local_now.date(), datetime.min.time(), tzinfo=KST).replace(hour=20, minute=15)
            if local_now < scheduled_for:
                scheduled_for -= timedelta(days=1)
            effective_availability = classify_observation_delay(scheduled_for, observed_at)
        observed_utc, _ = timezone_pair(observed_at)
        raw_combined = b"".join(market.encode() + b"\0" + archives[market] + b"\0" for market in sorted(archives))
        store = EvidenceStore(root)
        batch_id = _batch_id(observed_at)
        snapshot_metadata = {
            "universe_scope": "FULL_CURRENT_LISTINGS",
            "source_urls": MASTER_URLS,
            "source_received_at_utc_by_market": received_at,
            "source_archive_sha256_by_market": {market: hashlib.sha256(archives[market]).hexdigest() for market in sorted(archives)},
            "target_symbols": len(rows),
            "successful_symbols": len(rows),
            "missing_symbols": [],
            "coverage": 1.0,
            "data_quality": {
                "symbol_coverage": 1.0,
                "field_coverage": 1.0,
                "duplicate_count": len(rows) - len({row["symbol"] for row in rows}),
                "invalid_numeric_count": 0,
                "unexpected_date_count": 0,
                "schema_fingerprint": schema_fingerprint(rows),
                "source_latency_seconds": time.monotonic() - start,
            },
            "lineage": "UNKNOWN unless an explicit provider mapping exists",
            "batch_id": batch_id,
        }
        full = store.create_snapshot(
            evidence_class="PROSPECTIVE_OBSERVED",
            source_name="KIS current listings",
            provider="Korea Investment & Securities",
            endpoint_id="kis_current_listing_master_archive_v1",
            request_schema_version="kis-stock-master-v1",
            logical_key=f"universe:FULL_CURRENT_LISTINGS:{observed_at.date().isoformat()}",
            payload=rows,
            normalized_payload=rows,
            raw_payload=raw_combined,
            persist_raw=False,
            raw_representation_basis="CONCATENATED_KOSPI_KOSDAQ_ZIP_ARCHIVES_SHA_ONLY",
            observed_at=observed_at,
            config_sha256=config_sha,
            collector_git_sha=_git_sha(),
            record_count=len(rows),
            availability_label=effective_availability,
            result_classification="HTTP_200",
            metadata=snapshot_metadata,
        )
        cohort_meta = {
            "universe_scope": "FROZEN_RESEARCH_COHORT",
            "cohort_name": "cohort_100",
            "cohort_sha256": cohort_config["cohort_sha256"],
            "target_symbols": len(symbols),
            "successful_symbols": len(symbols) - len(missing_symbols),
            "missing_symbols": missing_symbols,
            "coverage": (len(symbols) - len(missing_symbols)) / len(symbols),
            "data_quality": {
                "symbol_coverage": (len(symbols) - len(missing_symbols)) / len(symbols),
                "field_coverage": sum(1 for row in cohort_rows for key in ("symbol", "current_master_presence") if key in row) / max(1, len(cohort_rows) * 2),
                "duplicate_count": len(symbols) - len(set(symbols)),
                "invalid_numeric_count": 0,
                "unexpected_date_count": 0,
                "schema_fingerprint": schema_fingerprint(cohort_rows),
                "source_latency_seconds": time.monotonic() - start,
            },
            "symbol_lineage": "UNKNOWN; no merger or code-change inference",
            "batch_id": batch_id,
        }
        cohort = store.create_snapshot(
            evidence_class="PROSPECTIVE_OBSERVED",
            source_name="KIS current listings / frozen research cohort intersection",
            provider="Korea Investment & Securities",
            endpoint_id="kis_current_listing_master_archive_v1",
            request_schema_version="phase4-cohort-100-v1+kis-stock-master-v1",
            logical_key=f"universe:FROZEN_RESEARCH_COHORT:{observed_at.date().isoformat()}",
            payload=cohort_rows,
            normalized_payload=cohort_rows,
            raw_payload=raw_combined,
            persist_raw=False,
            raw_representation_basis="CONCATENATED_KOSPI_KOSDAQ_ZIP_ARCHIVES_SHA_ONLY",
            observed_at=observed_at,
            config_sha256=config_sha,
            collector_git_sha=_git_sha(),
            record_count=len(cohort_rows),
            availability_label=effective_availability,
            result_classification="HTTP_200",
            metadata=cohort_meta,
        )
        elapsed = time.monotonic() - start
        batch = {
            "schema_version": 1,
            "batch_id": batch_id,
            "status": "COMPLETE" if not missing_symbols else "PARTIAL",
            "evidence_class": "PROSPECTIVE_OBSERVED",
            "source_name": "KIS current listings",
            "observed_at_utc": observed_utc,
            "target_symbols": len(symbols),
            "successful_symbols": len(symbols) - len(missing_symbols),
            "missing_symbols": missing_symbols,
            "failed_symbols": [],
            "provider_errors": 0,
            "transport_errors": 0,
            "schema_errors": 0,
            "request_count": request_count,
            "retry_count": retry_count,
            "elapsed_seconds": elapsed,
            "snapshot_ids": [full["snapshot_id"], cohort["snapshot_id"]],
            "snapshot_count": 2,
            "frozen_cohort_target_symbols": len(symbols),
            "frozen_cohort_successful_symbols": len(symbols) - len(missing_symbols),
            "frozen_cohort_missing_symbols": missing_symbols,
            "frozen_cohort_coverage": cohort_meta["coverage"],
            "current_listing_count": len(rows),
            "current_listing_scope": "FULL_CURRENT_LISTINGS",
            "current_listing_coverage": 1.0,
            "coverage": cohort_meta["coverage"],
            "data_quality": cohort_meta["data_quality"],
        }
        _write_batch(root, batch)
        missing = _append_missed_events(root, observed_at)
        revision_records = build_revision_records(root)
        integrity = verify_store(root)
        after = verify_previous_phase_snapshot(root)
        if after["status"] != "PASS":
            raise Phase16Error("previous-phase evidence integrity changed during collection")
        witness = {
            "observation_date": observed_at.astimezone(KST).date().isoformat(),
            "session": None,
            "observation_timestamps": [full["observed_at_utc"], cohort["observed_at_utc"]],
            "source_names": [full["source_name"], cohort["source_name"]],
            "snapshot_count": 2,
            "coverage": [1.0, cohort_meta["coverage"]],
            "hash_chain_tip": integrity["chain_tip_sha256"],
            "config_sha256": config_sha,
            "collector_git_sha": _git_sha(),
        }
        witness_id = hashlib.sha256(_json_bytes(witness)).hexdigest()[:16]
        _atomic_create(root / "witness/daily" / f"{observed_at.astimezone(KST):%Y%m%dT%H%M%S%z}__{witness_id}.json", _json_bytes(witness, pretty=True))
        _atomic_report(root, "reports/live-bootstrap.json", {
            "status": "PARTIAL",
            "universe_snapshot": "PASS",
            "universe_scope": "FULL_CURRENT_LISTINGS",
            "current_listing_count": len(rows),
            "current_listing_coverage": 1.0,
            "frozen_cohort_target_symbols": len(symbols),
            "frozen_cohort_successful_symbols": len(symbols) - len(missing_symbols),
            "frozen_cohort_missing_symbols": missing_symbols,
            "frozen_cohort_coverage": cohort_meta["coverage"],
            "per_stock_flow_target_symbols": len(symbols),
            "per_stock_flow_successful_symbols": 0,
            "per_stock_flow_requests_made": 0,
            "per_stock_flow_status": "DEFERRED_SAFETY_GUARD",
            "request_count": request_count,
            "retry_count": retry_count,
            "elapsed_seconds": elapsed,
            "snapshot_ids": batch["snapshot_ids"],
            "manifest_hash": integrity["chain_tip_sha256"],
            "config_hash": config_sha,
            "flow_bootstrap": "DEFERRED_SAFETY_GUARD",
            "flow_requests_made": 0,
            "first_observation_at_utc": min(full["observed_at_utc"], cohort["observed_at_utc"]),
            "missed_observations_recorded": missing,
            "previous_phase_artifact_immutability": after["status"],
            "revision_comparison_count": len(revision_records),
            "reason": "Only current-only listing archives passed the protected-response guard; dated flow endpoints remain deferred.",
        })
        report = _refresh_indexes(root, config, config_sha)
        _write_artifact_index(root)
        return {**batch, "manifest_chain_valid": integrity["valid"], "manifest_hash": integrity["chain_tip_sha256"], "config_sha256": config_sha, "status_report": report}


def _write_artifact_index(root: Path) -> dict[str, Any]:
    excluded = {"phase16-artifact-index.json", "artifact-integrity.json"}
    items = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".tmp") and "locks" not in p.parts):
        rel = path.relative_to(root).as_posix()
        if rel in excluded or rel.startswith("exports/"):
            continue
        body = path.read_bytes()
        items.append({"path": rel, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
    index = {"schema_version": 1, "artifact_count": len(items), "artifacts": items}
    _atomic_report(root, "phase16-artifact-index.json", index)
    integrity = verify_store(root)
    _atomic_report(root, "artifact-integrity.json", {
        "status": "PASS" if integrity["valid"] else "FAIL",
        "manifest_chain_valid": integrity["valid"],
        "manifest_chain_tip_sha256": integrity["chain_tip_sha256"],
        "snapshot_count": integrity["snapshot_count"],
        "artifact_index_sha256": _hash_path(root / "phase16-artifact-index.json"),
        "previous_phase_artifact_immutability": verify_previous_phase_snapshot(root)["status"],
        "errors": integrity["errors"],
    })
    return index


def collect_flow_preflight(
    source_id: str,
    symbols: list[str],
    *,
    config: dict[str, Any],
    session: date,
    evidence_class: str = "PROSPECTIVE_OBSERVED",
) -> dict[str, Any]:
    decision = request_range_guard(source_id, session, config=config, evidence_class=evidence_class)
    return {
        "status": "READY" if decision["allowed"] else "DEFERRED_SAFETY_GUARD",
        "request_count": 0,
        "network_accessed": False,
        "target_symbols": list(symbols),
        "successful_symbols": [],
        "missing_symbols": [],
        "failed_symbols": [],
        "coverage": 0.0,
        "guard_decision": decision,
    }


def make_status(root: Path = DEFAULT_ROOT, config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config, config_sha, _ = load_config(config_path)
    manifests = EvidenceStore(root).manifests() if root.exists() else []
    chain = verify_store(root)
    batches = []
    batch_dir = root / "prospective/manifests/batches"
    if batch_dir.exists():
        for path in sorted(batch_dir.glob("*.json")):
            batches.append(json.loads(path.read_text(encoding="utf-8")))
    core = [m for m in manifests if m.get("source_name") == "KIS per-stock investor daily flow" and m.get("evidence_class") == "PROSPECTIVE_OBSERVED"]
    sessions = {m.get("provider_session") or m.get("requested_session") for m in core if m.get("provider_session") or m.get("requested_session")}
    multi = {m.get("provider_session") or m.get("requested_session") for m in core if m.get("previous_vintage_id") and (m.get("provider_session") or m.get("requested_session"))}
    first = min((m["observed_at_utc"] for m in manifests), default=None)
    latest = max((m["observed_at_utc"] for m in manifests), default=None)
    full_cohort_sessions = {m.get("requested_session") for m in manifests if m.get("metadata", {}).get("universe_scope") == "FROZEN_RESEARCH_COHORT" and m.get("requested_session")}
    gate = config["source_stability_gate"]
    enough = len(sessions) >= gate["minimum_completed_sessions"] and len(multi) >= gate["minimum_multi_vintage_sessions"]
    report = {
        "phase16_builder": "COMPLETE" if chain["valid"] else "PARTIAL",
        "collector_config_sha256": config_sha,
        "first_observation_at_utc": first,
        "latest_observation_at_utc": latest,
        "prospective_first_session_date": min(sessions) if sessions else None,
        "sources_enabled": config.get("enabled_sources", []),
        "source_states": {row["source_id"]: row["state"] for row in source_safety_matrix(config, datetime.now(KST).date()).get("sources", [])},
        "snapshot_count": len(manifests),
        "prospective_sessions": len(sessions),
        "multi_vintage_sessions": len(multi),
        "full_cohort_sessions": len(full_cohort_sessions),
        "revision_comparison_count": len(list((root / "revision/events").glob("*.json"))) if (root / "revision/events").exists() else 0,
        "revised_observations": sum(m.get("classification") not in {"IDENTICAL", "SEMANTICALLY_IDENTICAL"} for p in (root / "revision/events").glob("*.json") for m in [json.loads(p.read_text(encoding="utf-8"))]) if (root / "revision/events").exists() else 0,
        "coverage": [batch.get("coverage") for batch in batches],
        "manifest_chain_valid": chain["valid"],
        "manifest_chain_tip_sha256": chain["chain_tip_sha256"],
        "latest_batch_status": batches[-1].get("status") if batches else None,
        "storage_usage": storage_usage(root, config),
        "source_stability": "READY_FOR_REVIEW" if enough else "INSUFFICIENT_OBSERVATIONS",
        "phase17_source_stability_ready": "YES" if enough else "NO",
        "external_2026": "NOT_READ",
        "holdout_2026": "NOT_READ",
        "alpha": "UNPROVEN",
        "shadow_next_session": "NO",
        "live": "DISABLED",
    }
    _write_operational_artifacts(root, config, config_sha, report)
    return report


def health_report(root: Path = DEFAULT_ROOT, config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config, config_sha, _ = load_config(config_path)
    integrity = verify_store(root)
    status = make_status(root, config_path)
    previous = verify_previous_phase_snapshot(root)
    errors = list(integrity["errors"])
    warnings = []
    if previous["status"] != "PASS":
        errors.append("previous phase artifact mutation or missing snapshot")
    latest = status["latest_observation_at_utc"]
    if latest:
        age = (datetime.now(UTC) - parse_aware_datetime(latest).astimezone(UTC)).total_seconds()
        if age > 36 * 3600:
            warnings.append("STALE_COLLECTION")
    else:
        warnings.append("NO_OBSERVATIONS_YET")
    usage = storage_usage(root, config)
    if usage["status"] != "OK":
        warnings.append(f"STORAGE_{usage['status']}")
    if status["source_stability"] == "INSUFFICIENT_OBSERVATIONS":
        warnings.append("SOURCE_STABILITY_INSUFFICIENT")
    batch_dir = root / "prospective/manifests/batches"
    batches = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(batch_dir.glob("*.json"))] if batch_dir.exists() else []
    consecutive_errors = 0
    for batch in reversed(batches):
        if batch.get("provider_errors", 0) or batch.get("transport_errors", 0):
            consecutive_errors += 1
        else:
            break
    if consecutive_errors >= 3:
        warnings.append("REPEATED_PROVIDER_ERRORS")
    schema_folder = root / "revision/schema-events"
    if schema_folder.exists() and any(schema_folder.glob("*.json")):
        warnings.append("SCHEMA_DRIFT_REQUIRES_REVIEW")
    if root.exists():
        low_coverage = [
            manifest for manifest in EvidenceStore(root).manifests()
            if manifest.get("metadata", {}).get("universe_scope")
            and manifest.get("metadata", {}).get("coverage", 0) < 0.95
        ]
        if low_coverage:
            warnings.append("LOW_UNIVERSE_COVERAGE")
    if datetime.now().astimezone().utcoffset() != datetime.now(KST).utcoffset():
        warnings.append("CLOCK_TIMEZONE_NOT_ASIA_SEOUL")
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, test_name = tempfile.mkstemp(prefix=".health-write-test.", dir=root)
        os.close(fd)
        Path(test_name).unlink()
    except OSError:
        errors.append("STORAGE_WRITE_FAILED")
    active = root / "config/active.json"
    if active.exists():
        active_data = json.loads(active.read_text(encoding="utf-8"))
        frozen_path = root / active_data.get("path", "")
        if not frozen_path.is_file() or _hash_path(frozen_path) != config_sha:
            errors.append("COLLECTOR_CONFIG_FREEZE_MISMATCH")
    return {
        "status": "FAIL" if errors else "PASS_WITH_WARNINGS" if warnings else "PASS",
        "collector_config_sha256": config_sha,
        "manifest_chain": integrity,
        "previous_phase_artifact_immutability": previous,
        "warnings": warnings,
        "errors": errors,
        "storage_usage": usage,
        "latest_batch_status": status["latest_batch_status"],
        "clock_timezone": str(datetime.now().astimezone().tzinfo),
        "clock_kst_offset_seconds": int(datetime.now(KST).utcoffset().total_seconds()),
    }


def deterministic_archive(root: Path) -> bytes:
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".tmp" not in p.name):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(("locks/", "exports/")) or path.name.startswith((".env",)) or path.suffix in {".token", ".secret"}:
            continue
        files.append((path, relative))
    with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as stream:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path, relative in files:
                body = path.read_bytes()
                info = tarfile.TarInfo(name=f"phase16/{relative}")
                info.size = len(body)
                info.mtime = 0
                info.mode = 0o600
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                import io

                archive.addfile(info, io.BytesIO(body))
        stream.seek(0)
        return stream.read()


def export_evidence(root: Path = DEFAULT_ROOT, output_dir: Path | None = None) -> dict[str, Any]:
    body = deterministic_archive(root)
    digest = hashlib.sha256(body).hexdigest()
    target_dir = output_dir or root / "exports"
    target = target_dir / f"phase16-evidence-{digest}.tar"
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise AppendOnlyError("content-addressed evidence archive path has different bytes")
    else:
        _atomic_create(target, body)
    manifest = {
        "schema_version": 1,
        "archive": target.name,
        "archive_sha256": digest,
        "archive_bytes": len(body),
        "snapshot_count": verify_store(root)["snapshot_count"],
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    _atomic_replace(root / "exports/evidence-export-manifest.json", _json_bytes(manifest, pretty=True))
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phase16", description="Point-in-time-safe Phase 16 evidence collection")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("collect-full", help="Preflight the full frozen cohort; unsafe flow sources remain network-disabled")
    subs.add_parser("collect-probe", help="Preflight the deterministic six-symbol flow probe cohort")
    universe = subs.add_parser("collect-universe", help="Collect current-only KIS listings and the frozen cohort intersection")
    universe.add_argument("--availability-label", choices=["MANUAL_BOOTSTRAP", "POST_20KST", "LATE_CATCHUP"], default="MANUAL_BOOTSTRAP")
    hist = subs.add_parser("historical-probe", help="Preflight a historical revision probe without making a request")
    hist.add_argument("--session", type=date.fromisoformat, required=True)
    subs.add_parser("revisions", help="Build append-only vintage comparisons and revision summaries")
    subs.add_parser("verify-store", help="Verify immutable snapshot payloads and the manifest hash chain")
    subs.add_parser("health", help="Check evidence integrity, freshness, storage and configuration")
    subs.add_parser("status", help="Report source, vintage, coverage and storage status")
    export = subs.add_parser("export-evidence", help="Create a deterministic, content-addressed local evidence archive")
    export.add_argument("--output-dir", type=Path)
    subs.add_parser("install-scheduler", help="Install the user-level daily current-listings LaunchAgent")
    subs.add_parser("uninstall-scheduler", help="Remove only the Phase 16 LaunchAgent installed by this tool")
    return parser


def _save_flow_preflight(args: argparse.Namespace, *, source_id: str, symbols: list[str], evidence_class: str = "PROSPECTIVE_OBSERVED") -> dict[str, Any]:
    config, config_sha, config_bytes = load_config(args.config)
    _runtime_config(args.root, config, config_sha, config_bytes)
    session = datetime.now(KST).date()
    result = collect_flow_preflight(source_id, symbols, config=config, session=session, evidence_class=evidence_class)
    result.update({"source_id": source_id, "session": session.isoformat(), "collector_config_sha256": config_sha})
    _atomic_report(args.root, "reports/collection-decision.json", result)
    _record_source_safety(args.root, config, datetime.now(KST))
    _refresh_indexes(args.root, config, config_sha)
    _write_artifact_index(args.root)
    return result


def _install_scheduler(root: Path) -> dict[str, Any]:
    label = "com.krxtrader.phase16.universe"
    launch_agents = Path.home() / "Library/LaunchAgents"
    plist_path = launch_agents / f"{label}.plist"
    if plist_path.exists():
        return {"status": "READY_NOT_INSTALLED", "reason": "LaunchAgent plist already exists; refusing to replace unmanaged file", "path": str(plist_path)}
    launch_agents.mkdir(parents=True, exist_ok=True)
    import plistlib

    plist = build_launchd_plist(root)
    (root / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_create(plist_path, plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
    uid = os.getuid()
    try:
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "READY_NOT_INSTALLED", "reason": f"launchctl unavailable ({type(exc).__name__}); plist was created", "path": str(plist_path)}
    if result.returncode:
        return {"status": "READY_NOT_INSTALLED", "reason": f"launchctl bootstrap failed ({result.returncode}); plist was created", "path": str(plist_path)}
    return {"status": "ACTIVE", "label": label, "path": str(plist_path), "schedule_local_time": "20:15 daily", "source": "KIS current-only listings"}


def build_launchd_plist(root: Path = DEFAULT_ROOT, python_path: Path | None = None) -> dict[str, Any]:
    label = "com.krxtrader.phase16.universe"
    python_path = python_path or REPO_ROOT / ".venv/bin/python"
    log_dir = root / "logs"
    return {
        "Label": label,
        "ProgramArguments": [str(python_path), "-m", "krx_trader.research.phase16", "--root", str(root), "collect-universe", "--availability-label", "POST_20KST"],
        "WorkingDirectory": str(REPO_ROOT),
        "StartCalendarInterval": {"Hour": 20, "Minute": 15},
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "universe.stdout.log"),
        "StandardErrorPath": str(log_dir / "universe.stderr.log"),
    }


def _uninstall_scheduler() -> dict[str, Any]:
    label = "com.krxtrader.phase16.universe"
    plist_path = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    if not plist_path.exists():
        return {"status": "NOT_INSTALLED", "path": str(plist_path)}
    import plistlib

    try:
        existing = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return {"status": "NOT_REMOVED", "reason": "existing plist is not a valid Phase 16 plist", "path": str(plist_path)}
    if existing.get("Label") != label or "krx_trader.research.phase16" not in " ".join(existing.get("ProgramArguments", [])):
        return {"status": "NOT_REMOVED", "reason": "existing plist is not managed by Phase 16", "path": str(plist_path)}
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, text=True, timeout=20, check=False)
    plist_path.unlink()
    return {"status": "REMOVED", "path": str(plist_path)}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "collect-universe":
            result = collect_universe(root=args.root, config_path=args.config, availability_label=args.availability_label)
        elif args.command in {"collect-full", "collect-probe"}:
            config, _, _ = load_config(args.config)
            if args.command == "collect-probe":
                cohort = config["cohorts"]["probe_cohort"]["symbols"]
                symbols = cohort["KOSPI"] + cohort["KOSDAQ"]
                source_id = "kis_per_stock_flow"
            else:
                symbols = _read_cohort_symbols(REPO_ROOT / config["cohorts"]["full_research_cohort"]["path"], config["cohorts"]["full_research_cohort"]["cohort_sha256"])
                source_id = "kis_per_stock_flow"
            result = _save_flow_preflight(args, source_id=source_id, symbols=symbols)
        elif args.command == "historical-probe":
            config, config_sha, config_bytes = load_config(args.config)
            _runtime_config(args.root, config, config_sha, config_bytes)
            result = request_range_guard("kis_per_stock_flow", args.session, config=config, evidence_class="HISTORICAL_REVISION_PROBE")
            result.update({"network_accessed": False, "status": "PREFLIGHT_ONLY", "historical_evidence_class": "HISTORICAL_REVISION_PROBE", "note": "No live historical probe was issued by this Phase 16 bootstrap."})
            _atomic_report(args.root, "reports/historical-probe-decision.json", result)
        elif args.command == "revisions":
            records = build_revision_records(args.root)
            result = {"status": "PASS", "comparison_count": len(records), "summary": json.loads((args.root / "revision/revision-summary.json").read_text())}
            config, config_sha, _ = load_config(args.config)
            _refresh_indexes(args.root, config, config_sha)
            _write_artifact_index(args.root)
        elif args.command == "verify-store":
            result = verify_store(args.root)
        elif args.command == "health":
            result = health_report(args.root, args.config)
            _atomic_report(args.root, "reports/collection-health.json", result)
            _write_artifact_index(args.root)
        elif args.command == "status":
            result = make_status(args.root, args.config)
            _atomic_report(args.root, "reports/collection-status.json", result)
            _write_artifact_index(args.root)
        elif args.command == "export-evidence":
            result = export_evidence(args.root, args.output_dir)
        elif args.command == "install-scheduler":
            result = _install_scheduler(args.root)
            _atomic_report(args.root, "reports/scheduler-status.json", result)
            config, config_sha, _ = load_config(args.config)
            _refresh_indexes(args.root, config, config_sha)
            _write_artifact_index(args.root)
        elif args.command == "uninstall-scheduler":
            result = _uninstall_scheduler()
            _atomic_report(args.root, "reports/scheduler-status.json", result)
        else:
            return 2
    except (Phase16Error, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if result.get("status") not in {"FAIL", "FAILED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

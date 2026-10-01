from __future__ import annotations

import gzip
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from krx_trader.research.pipeline.evidence_ledger import (
    DISCOVERY,
    SAFE_PANEL_END,
    ProtectedEvidenceError,
    assert_phase14_evidence_date,
)
from krx_trader.research.pipeline.factor_registry import FactorSpec, factor_value

SAFE_PANEL_RELATIVE_PATH = Path("runtime/research/phase13/phase13-safe-factor-panel.jsonl.gz")
TARGET_FIELDS = {
    "ABSOLUTE_RETURN": "stock_gross_return_pct",
    "MATCHED_MARKET_COMPONENT": "matched_market_return_pct",
    "SIMPLE_EXCESS_RETURN": "excess_return_pct",
    "BETA_RESIDUAL_RETURN": "beta_residual_return_pct",
}
PRIMARY_HORIZONS = (3, 5, 10)


class PanelContractError(ValueError):
    """Raised when an input row violates the frozen factor/target contract."""


@dataclass(frozen=True)
class PanelRecord:
    signal_date: date
    symbol: str
    market: str
    horizon_sessions: int
    availability_time: str
    participant_count: int
    quality_flags: tuple[str, ...]
    features: dict[str, Any]
    targets: dict[str, float | None]
    entry_date: date
    exit_date: date

    @property
    def key(self) -> tuple[str, date, int]:
        return (self.symbol, self.signal_date, self.horizon_sessions)


@dataclass(frozen=True)
class FactorObservation:
    date: date
    symbol: str
    market: str
    factor_name: str
    factor_value: float | None
    availability_time: str
    participant_count: int
    quality_flags: tuple[str, ...]


@dataclass(frozen=True)
class TargetObservation:
    signal_date: date
    target_end_date: date
    symbol: str
    market: str
    horizon_sessions: int
    target_name: str
    target_value: float | None
    quality_flags: tuple[str, ...]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def validate_factor_timing(signal_date: date | str, availability_time: str) -> None:
    day = date.fromisoformat(signal_date) if isinstance(signal_date, str) else signal_date
    try:
        available_day = date.fromisoformat(availability_time[:10])
    except (TypeError, ValueError) as exc:
        raise PanelContractError("factor availability_time must be an ISO timestamp") from exc
    if available_day > day:
        raise PanelContractError("factor value is unavailable at its signal date")


def validate_record_dates(signal_date: date | str, exit_date: date | str) -> None:
    signal = date.fromisoformat(signal_date) if isinstance(signal_date, str) else signal_date
    target_end = date.fromisoformat(exit_date) if isinstance(exit_date, str) else exit_date
    assert_phase14_evidence_date(signal)
    if target_end <= signal:
        raise PanelContractError("forward target must end strictly after its signal date")
    if target_end > SAFE_PANEL_END:
        raise PanelContractError("forward target may not cross the pre-Confirmation data boundary")


def parse_panel_record(raw: dict[str, Any], *, minimum_horizon: int = 3) -> PanelRecord:
    try:
        signal_day = date.fromisoformat(str(raw["date"]))
        outcome = raw["forward_outcome"]
        horizon = int(outcome["horizon_sessions"])
        entry_day = date.fromisoformat(str(outcome["entry_date"]))
        exit_day = date.fromisoformat(str(outcome["exit_date"]))
        symbol = str(raw["symbol"])
        market = str(raw["market"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PanelContractError(
            "safe factor panel row is missing required identity/target fields"
        ) from exc
    validate_record_dates(signal_day, exit_day)
    if horizon not in PRIMARY_HORIZONS or horizon < minimum_horizon:
        raise PanelContractError(f"unsupported forward horizon: {horizon}")
    availability = f"{signal_day.isoformat()}T15:30:00+09:00"
    validate_factor_timing(signal_day, availability)
    if entry_day <= signal_day or exit_day < entry_day:
        raise PanelContractError("target entry/exit dates must be strictly forward from the signal")
    targets = {name: _number(outcome.get(field)) for name, field in TARGET_FIELDS.items()}
    feature_snapshot = {key: value for key, value in raw.items() if key != "forward_outcome"}
    flags: list[str] = []
    if outcome.get("beta_residual_return_pct") is None:
        flags.append("BETA_RESIDUAL_MISSING")
    if raw.get("beta_status") == "OUTSIDE_FIXED_SANITY_RANGE":
        flags.append("BETA_OUTSIDE_FIXED_SANITY_RANGE")
    return PanelRecord(
        signal_date=signal_day,
        symbol=symbol,
        market=market,
        horizon_sessions=horizon,
        availability_time=availability,
        participant_count=int(raw.get("market_breadth_participants") or 0),
        quality_flags=tuple(flags),
        features=feature_snapshot,
        targets=targets,
        entry_date=entry_day,
        exit_date=exit_day,
    )


def verify_safe_panel_path(path: Path) -> None:
    resolved = path.resolve()
    expected_name = SAFE_PANEL_RELATIVE_PATH.name
    if path.name != expected_name or path.parent.name != "phase13":
        raise ProtectedEvidenceError(
            "Phase 14 accepts only the Phase 13 pre-Confirmation safe factor panel"
        )
    lowered = str(resolved).lower()
    if (
        "external" in lowered
        or "holdout" in lowered
        or "2026" in lowered
        or "confirmation" in lowered
    ):
        raise ProtectedEvidenceError(
            "protected/external evidence paths are outside the Phase 14 input contract"
        )
    if not resolved.is_file():
        raise FileNotFoundError(resolved)


def load_safe_panel(path: Path) -> list[PanelRecord]:
    verify_safe_panel_path(path)
    records: list[PanelRecord] = []
    seen: set[tuple[str, date, int]] = set()
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PanelContractError(
                    f"invalid JSON in safe factor panel at line {line_number}"
                ) from exc
            record = parse_panel_record(raw)
            if record.key in seen:
                raise PanelContractError(
                    f"duplicate factor/target observation at line {line_number}"
                )
            seen.add(record.key)
            records.append(record)
    if not records:
        raise PanelContractError("safe factor panel contains no usable observations")
    return records


def panel_file_sha256(path: Path) -> str:
    verify_safe_panel_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discovery_idio_cuts(records: list[PanelRecord]) -> tuple[float, float] | None:
    by_key: dict[tuple[str, date], float] = {}
    for record in records:
        if not DISCOVERY[0] <= record.signal_date <= DISCOVERY[1]:
            continue
        value = _number(record.features.get("idio_volatility_20d_pct"))
        if value is not None:
            by_key[(record.symbol, record.signal_date)] = value
    values = sorted(by_key.values())
    if len(values) < 3:
        return None

    def quantile(probability: float) -> float:
        position = (len(values) - 1) * probability
        low, high = math.floor(position), math.ceil(position)
        if low == high:
            return values[low]
        return values[low] * (high - position) + values[high] * (position - low)

    return quantile(1 / 3), quantile(2 / 3)


def factor_matrix_values(
    records: list[PanelRecord], specs: list[FactorSpec], idio_cuts: tuple[float, float] | None
) -> dict[str, list[float | None]]:
    return {
        spec.factor_id: [factor_value(spec, record.features, idio_cuts) for record in records]
        for spec in specs
    }


def iter_factor_observations(
    records: list[PanelRecord],
    specs: list[FactorSpec],
    factor_values: dict[str, list[float | None]],
):
    participant_counts: dict[tuple[str, str, str], int] = {}
    for spec in specs:
        for record, value in zip(records, factor_values[spec.factor_id], strict=True):
            if value is not None:
                key = (spec.factor_id, record.signal_date.isoformat(), record.market)
                participant_counts[key] = participant_counts.get(key, 0) + 1
    for spec in specs:
        for record, value in zip(records, factor_values[spec.factor_id], strict=True):
            yield FactorObservation(
                date=record.signal_date,
                symbol=record.symbol,
                market=record.market,
                factor_name=spec.factor_id,
                factor_value=value,
                availability_time=record.availability_time,
                participant_count=participant_counts.get(
                    (spec.factor_id, record.signal_date.isoformat(), record.market), 0
                ),
                quality_flags=record.quality_flags,
            )


def iter_target_observations(records: list[PanelRecord]):
    for record in records:
        for name, value in record.targets.items():
            yield TargetObservation(
                signal_date=record.signal_date,
                target_end_date=record.exit_date,
                symbol=record.symbol,
                market=record.market,
                horizon_sessions=record.horizon_sessions,
                target_name=name,
                target_value=value,
                quality_flags=record.quality_flags,
            )

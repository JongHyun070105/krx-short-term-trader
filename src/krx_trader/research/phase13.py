from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import random
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.dataset as ds

from krx_trader.models import Bar
from krx_trader.research.phase11 import _load_frozen_cohort

KST = ZoneInfo("Asia/Seoul")
WARMUP_START = date(2022, 11, 1)
DISCOVERY = (date(2023, 1, 2), date(2024, 6, 28))
TOUCHED_REPLICATION = (date(2024, 7, 1), date(2025, 6, 30))
SAFE_DATA_END = TOUCHED_REPLICATION[1]
CONFIRMATION = (date(2025, 7, 1), date(2025, 12, 30))
EXTERNAL_2026 = (date(2026, 1, 5), date(2026, 4, 16))
HOLDOUT_2026 = (date(2026, 7, 28), date(2026, 8, 28))
OUTPUT_ROOT = Path("runtime/research/phase13")
PHASE11_ROOT = Path("runtime/research/phase11")
COHORT_PATH = Path("runtime/research/phase4/cohort-manifest.json")
CACHE_ROOT = PHASE11_ROOT / "daily_cache"
ADJUSTMENT = "FID_ORG_ADJ_PRC=0 (KIS adjusted daily OHLCV)"
HORIZONS = (3, 5, 10)
RETURN_HORIZONS = (1, 3, 5, 10, 20)
BETA_WINDOW = 120
BETA_MIN_PAIRS = 60
COST_PCT = 0.53
COST_MULTIPLIERS = (1.0, 1.5, 2.0)
BOOTSTRAP_ITERATIONS = 2_000
BOOTSTRAP_SEED = 1_313
EXTREME_BETA_RANGE = (-2.0, 5.0)

OFFICIAL_SOURCES: dict[str, dict[str, Any]] = {
    "kind_current_list": {
        "provider": "KRX KIND",
        "url": "https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage",
        "coverage_dates": "current listing snapshot; page exposes listing date and KOSPI/KOSDAQ/KONEX market label",
        "update_frequency": "current web page; exact publication cadence not documented on inspected page",
        "historical_revision_behavior": "no versioned as-of snapshots or revision log identified",
        "point_in_time_semantics": "current list is not membership as known on each historical date",
        "availability_today": "available on public web; Excel export is exposed",
        "delisted_symbols_retained": "not established by current-list page",
        "ticker_reuse_and_code_changes": "not established by listed-company table",
        "license_access_limitation": "public site; bulk/API rights and historical redistribution terms not established here",
        "reproducibility": "current snapshot can be manually downloaded; historical snapshots not established",
        "verdict": "REFERENCE_FOR_CURRENT_LIST_ONLY",
    },
    "kind_delisted_list": {
        "provider": "KRX KIND",
        "url": "https://kind.krx.co.kr/investwarn/delcompany.do?currentPageSize=30&method=searchDelCompanySub",
        "coverage_dates": "current paginated delisting-warning/company list; inspected page showed 52 records and dates, not a documented full historical series",
        "update_frequency": "current web list; exact cadence not documented",
        "historical_revision_behavior": "no versioned snapshot or revision policy identified",
        "point_in_time_semantics": "does not by itself establish date-by-date listing eligibility or final trading date",
        "availability_today": "available on public web; current pages are paginated",
        "delisted_symbols_retained": "some recently delisted/delisting cases appear; complete retention and code coverage not established",
        "ticker_reuse_and_code_changes": "not established by inspected list",
        "license_access_limitation": "public web access; automated bulk interface/terms not established",
        "reproducibility": "current page can be revisited; complete historical extract not verified",
        "verdict": "PARTIAL_LIFECYCLE_REFERENCE",
    },
    "krx_delisting_lookup": {
        "provider": "Korea Exchange Global",
        "url": "https://global.krx.co.kr/contents/GLB/03/0306/0306050000/GLB0306050000.jsp",
        "coverage_dates": "search form offers market and period; table is labeled code, name, delisting date, reason",
        "update_frequency": "web lookup; exact cadence and maximum query range not documented in inspected page",
        "historical_revision_behavior": "no versioned as-of response or revision log identified",
        "point_in_time_semantics": "delisting-date lookup alone does not provide historical market membership, listing date, or security lineage",
        "availability_today": "web form available; dynamic result table did not expose a machine-readable bulk extract in this audit",
        "delisted_symbols_retained": "delisting records are the page's subject, but complete historical retention not established",
        "ticker_reuse_and_code_changes": "not established by inspected page",
        "license_access_limitation": "public web form; API/bulk download terms not established",
        "reproducibility": "individual web lookup possible; full reproducible panel not verified",
        "verdict": "PARTIAL_DELISTING_REFERENCE",
    },
    "krx_open_api": {
        "provider": "Korea Exchange Data Marketplace",
        "url": "https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd",
        "coverage_dates": "service catalogue states 2010 onward for KOSPI/KOSDAQ daily trading and stock basic information",
        "update_frequency": "daily trading service; historical correction schedule not specified on inspected catalogue",
        "historical_revision_behavior": "vintage/correction semantics not specified in inspected service catalogue",
        "point_in_time_semantics": "dated daily trade records are useful inputs, but historical membership snapshots and security-lineage semantics are not specified in the catalogue",
        "availability_today": "service catalogue public; API use needs account, API key approval, and service approval",
        "delisted_symbols_retained": "not established by the inspected service description; no API call/key used",
        "ticker_reuse_and_code_changes": "not established by the inspected service description",
        "license_access_limitation": "authentication and administrator approval required; terms limit API use to non-commercial purposes; data purchase may have separate review/fees",
        "reproducibility": "dated daily endpoints are documented at catalogue level; historical metadata lineage was not verified",
        "verdict": "PROMISING_BUT_NOT_SUFFICIENTLY_VERIFIED_FOR_PIT",
    },
    "kis_adjusted_daily": {
        "provider": "Korea Investment & Securities Open API",
        "url": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_examples.py",
        "endpoint": "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        "coverage_dates": "local Phase 11 cache 2022-11-01 through 2025-12-30 where present; primary factor panel is clipped at 2025-06-30",
        "update_frequency": "queryable daily bars; endpoint's historical vintage/revision cadence not documented in inspected sample",
        "historical_revision_behavior": "no as-of vintage selector; later corporate-action adjustments may revise historical adjusted rows",
        "point_in_time_semantics": "completed-session adjusted OHLCV, usable after close for next-session decisions; not a strict historical-vintage snapshot",
        "availability_today": "existing local Phase 11 cache; no API/authentication call made by Phase 13",
        "delisted_symbols_retained": "not documented or tested against a delisted symbol in this run",
        "ticker_reuse_and_code_changes": "not established by inspected sample",
        "license_access_limitation": "KIS application credentials are required for API access; this run used local cache only",
        "reproducibility": "local cache sidecars and SHA-256 hashes are recorded; daily values use FID_ORG_ADJ_PRC=0",
        "verdict": "CURRENT_COHORT_PRICES_ONLY",
    },
}

FACTOR_BUCKETS: dict[str, tuple[tuple[float | None, float | None, str], ...]] = {
    "excess_return_5d_pct": (
        (None, -4.0, "LE_−4"), (-4.0, -2.0, "−4_TO_−2"), (-2.0, 0.0, "−2_TO_0"),
        (0.0, 2.0, "0_TO_2"), (2.0, 4.0, "2_TO_4"), (4.0, None, "GE_4"),
    ),
    "excess_return_10d_pct": (
        (None, -4.0, "LE_−4"), (-4.0, -2.0, "−4_TO_−2"), (-2.0, 0.0, "−2_TO_0"),
        (0.0, 2.0, "0_TO_2"), (2.0, 4.0, "2_TO_4"), (4.0, None, "GE_4"),
    ),
    "excess_return_20d_pct": (
        (None, -8.0, "LE_−8"), (-8.0, -4.0, "−8_TO_−4"), (-4.0, -2.0, "−4_TO_−2"),
        (-2.0, 0.0, "−2_TO_0"), (0.0, 2.0, "0_TO_2"), (2.0, 4.0, "2_TO_4"),
        (4.0, 8.0, "4_TO_8"), (8.0, None, "GE_8"),
    ),
    "residual_return_5d_pct": (
        (None, -4.0, "LE_−4"), (-4.0, -2.0, "−4_TO_−2"), (-2.0, 0.0, "−2_TO_0"),
        (0.0, 2.0, "0_TO_2"), (2.0, 4.0, "2_TO_4"), (4.0, None, "GE_4"),
    ),
    "residual_return_10d_pct": (
        (None, -4.0, "LE_−4"), (-4.0, -2.0, "−4_TO_−2"), (-2.0, 0.0, "−2_TO_0"),
        (0.0, 2.0, "0_TO_2"), (2.0, 4.0, "2_TO_4"), (4.0, None, "GE_4"),
    ),
    "residual_return_20d_pct": (
        (None, -8.0, "LE_−8"), (-8.0, -4.0, "−8_TO_−4"), (-4.0, -2.0, "−4_TO_−2"),
        (-2.0, 0.0, "−2_TO_0"), (0.0, 2.0, "0_TO_2"), (2.0, 4.0, "2_TO_4"),
        (4.0, 8.0, "4_TO_8"), (8.0, None, "GE_8"),
    ),
    "residual_drawdown_20d_pct": (
        (None, -8.0, "DEEP_LE_−8"), (-8.0, -4.0, "−8_TO_−4"),
        (-4.0, 0.0, "−4_TO_0"), (0.0, None, "AT_HIGH_OR_ABOVE"),
    ),
    "residual_drawdown_60d_pct": (
        (None, -15.0, "DEEP_LE_−15"), (-15.0, -8.0, "−15_TO_−8"),
        (-8.0, -4.0, "−8_TO_−4"), (-4.0, 0.0, "−4_TO_0"), (0.0, None, "AT_HIGH_OR_ABOVE"),
    ),
    "turnover_ratio_5d_vs_prior20_median": (
        (None, 0.75, "CONTRACTED"), (0.75, 1.5, "NORMAL"), (1.5, None, "EXPANDED"),
    ),
    "range_position_20d": (
        (None, 0.25, "LOWER_QUARTER"), (0.25, 0.75, "MIDDLE_HALF"), (0.75, None, "UPPER_QUARTER"),
    ),
}

MAP_FAMILIES: dict[str, tuple[str, ...]] = {
    "EXCESS_RETURN_MOMENTUM": (
        "excess_return_5d_pct", "excess_return_10d_pct", "excess_return_20d_pct",
    ),
    "EXCESS_RETURN_REVERSAL": (
        "excess_return_5d_pct", "excess_return_10d_pct", "excess_return_20d_pct",
    ),
    "BETA_RESIDUAL_MOMENTUM": (
        "residual_return_5d_pct", "residual_return_10d_pct", "residual_return_20d_pct",
    ),
    "BETA_RESIDUAL_REVERSAL": (
        "residual_return_5d_pct", "residual_return_10d_pct", "residual_return_20d_pct",
    ),
    "RESIDUAL_DRAWDOWN": ("residual_drawdown_20d_pct", "residual_drawdown_60d_pct"),
    "IDIOSYNCRATIC_VOLATILITY": ("idio_volatility_20d_pct",),
    "ABNORMAL_TURNOVER": ("turnover_ratio_5d_vs_prior20_median",),
    "RESIDUAL_RANK": ("residual_rank_5d_state", "residual_rank_20d_state"),
    "RANGE_POSITION": ("range_position_20d",),
}


class ProtectedPeriodError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_source_sha(path: Path, metadata: dict[str, Any]) -> str:
    expected = metadata.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected.lower()):
        raise ValueError(f"Phase 11 cache metadata has no valid SHA-256 for {path.name}")
    return expected.lower()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n")
    temp.replace(path)


def _round(value: float | None, places: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, places)


def _period_for(session: date) -> tuple[str, tuple[date, date]] | None:
    if DISCOVERY[0] <= session <= DISCOVERY[1]:
        return "DISCOVERY", DISCOVERY
    if TOUCHED_REPLICATION[0] <= session <= TOUCHED_REPLICATION[1]:
        return "TOUCHED_REPLICATION", TOUCHED_REPLICATION
    return None


def assert_phase13_data_date(session: date, *, allow_confirmation: bool = False) -> None:
    """Permit warmup and touched hypothesis-generation dates only."""
    if allow_confirmation and CONFIRMATION[0] <= session <= CONFIRMATION[1]:
        return
    if not WARMUP_START <= session <= SAFE_DATA_END:
        raise ProtectedPeriodError(f"date outside Phase 13 safe data boundary: {session.isoformat()}")
    if CONFIRMATION[0] <= session <= CONFIRMATION[1]:
        raise ProtectedPeriodError("Confirmation data is protected until a valid pre-confirmation freeze")
    if EXTERNAL_2026[0] <= session <= EXTERNAL_2026[1]:
        raise ProtectedPeriodError("External 2026 data is reserved for a later phase")
    if HOLDOUT_2026[0] <= session <= HOLDOUT_2026[1]:
        raise ProtectedPeriodError("2026 holdout data must never be read in Phase 13")


def _verify_confirmation_freeze(expected_freeze_sha: str, output_root: Path) -> dict[str, Any]:
    local = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    remote = subprocess.run(["git", "rev-parse", "origin/main"], check=True, capture_output=True, text=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain"], check=True, capture_output=True, text=True).stdout
    if local != expected_freeze_sha or remote != expected_freeze_sha or status.strip():
        raise ProtectedPeriodError("clean local and remote main must equal the expected pre-confirmation freeze SHA")
    preregistration = json.loads((output_root / "phase13-preregistration.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_root / "phase13-dataset-manifest.json").read_text(encoding="utf-8"))
    if preregistration.get("status") != "PREREGISTERED" or not preregistration.get("candidate"):
        raise ProtectedPeriodError("a frozen Phase 13 candidate preregistration is required")
    if any(
        preregistration.get(key) != manifest.get(key)
        for key in ("source_git_sha", "dataset_sha256", "config_sha256")
    ):
        raise ProtectedPeriodError("preregistration and frozen Phase 13 source/dataset/config hashes do not match")
    if preregistration.get("confirmation_period") != [d.isoformat() for d in CONFIRMATION]:
        raise ProtectedPeriodError("preregistration Confirmation dates do not match the protected window")
    if manifest.get("phase13_source_sha256") != _sha256(Path(__file__)):
        raise ProtectedPeriodError("Phase 13 implementation differs from the frozen source digest")
    index_path = output_root / "phase13-artifact-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if verify_artifacts(output_root, index).get("status") != "PASS":
        raise ProtectedPeriodError("Phase 13 pre-confirmation artifact integrity must pass")
    return preregistration


def read_confirmation_outcomes(
    *, expected_freeze_sha: str | None = None, output_root: Path = OUTPUT_ROOT,
    cache_root: Path = CACHE_ROOT, cohort_path: Path = COHORT_PATH,
) -> dict[str, Any]:
    """One-shot Confirmation read guarded by the exact pushed freeze and preregistration."""
    if expected_freeze_sha is None:
        raise ProtectedPeriodError("Confirmation reader requires a committed pre-confirmation freeze SHA")
    preregistration = _verify_confirmation_freeze(expected_freeze_sha, output_root)
    marker_path = output_root / "phase13-confirmation.json"
    if not marker_path.is_file():
        raise ProtectedPeriodError("Phase 13 Confirmation marker is missing; regenerate the safe pre-confirmation artifacts first")
    prior = json.loads(marker_path.read_text(encoding="utf-8"))
    if prior.get("confirmation") != "NOT_RUN":
        raise ProtectedPeriodError("Phase 13 Confirmation is one-shot and has already started")
    _write_json(marker_path, {
        "confirmation": "ACCESS_STARTED", "freeze_sha": expected_freeze_sha,
        "external_2026": "NOT_READ", "holdout_2026": "NOT_READ",
        "reason": "One-shot Confirmation access marker written after clean local/remote freeze verification.",
    })
    _, symbols, markets, cohort_sha, prices, indexes, cache_hashes = load_phase13_cache(
        cache_root=cache_root, cohort_path=cohort_path, end_date=CONFIRMATION[1], allow_confirmation=True
    )
    sessions = _date_sessions(indexes, end_date=CONFIRMATION[1])
    features = build_phase13_features(
        prices_by_symbol=prices, indexes=indexes, symbols=symbols,
        market_by_symbol=markets, sessions=sessions, end_date=CONFIRMATION[1], allow_confirmation=True,
    )
    confirmation_features = [row for row in features if row["period"] == "CONFIRMATION"]
    outcomes = _forward_outcomes(confirmation_features, prices, indexes, sessions, allow_confirmation=True)
    idio_cuts = _period_idio_cuts(features)
    candidate = preregistration["candidate"]
    left_factor, right_factor = next(
        (left, right) for name, left, right in INTERACTION_DEFINITIONS if name == candidate["interaction"]
    )
    left_state = candidate["features"][left_factor]["state"]
    right_state = candidate["features"][right_factor]["state"]
    selected = []
    feature_by_key = {(row["symbol"], row["date"]): row for row in confirmation_features}
    for outcome in outcomes:
        feature = feature_by_key[(outcome["symbol"], outcome["signal_date"])]
        if (
            outcome["period"] == "CONFIRMATION"
            and outcome["horizon_sessions"] == candidate["holding_horizon_sessions"]
            and outcome["market"] == candidate["market"]
            and _state(feature.get(left_factor), left_factor, idio_cuts=idio_cuts) == left_state
            and _state(feature.get(right_factor), right_factor, idio_cuts=idio_cuts) == right_state
        ):
            selected.append(outcome)
    executions = non_overlapping_events(selected)
    return {
        "freeze_sha": expected_freeze_sha, "candidate": candidate,
        "cohort_sha256": cohort_sha, "confirmation_dataset_sha256": _data_digest(prices, indexes),
        "input_cache_sha256_by_symbol_or_index": cache_hashes,
        "metrics": _metrics(selected, execution_events=executions), "raw_events": selected,
        "executions": executions,
    }


def run_phase13_confirmation(
    *, expected_freeze_sha: str, output_root: Path = OUTPUT_ROOT,
    cache_root: Path = CACHE_ROOT, cohort_path: Path = COHORT_PATH,
) -> dict[str, Any]:
    """Run the preregistered candidate once after the exact pushed freeze is verified."""
    result = read_confirmation_outcomes(
        expected_freeze_sha=expected_freeze_sha, output_root=output_root,
        cache_root=cache_root, cohort_path=cohort_path,
    )
    metrics = result["metrics"]
    concentration = _concentration(result["raw_events"])
    bootstrap = _mean_date_cluster_bootstrap(result["executions"])
    checks = {
        "adequate_non_overlap_sample": metrics["non_overlapping_executions"] >= 50,
        "adequate_unique_signal_dates": metrics["unique_signal_dates"] >= 40,
        "positive_absolute_gross": (metrics.get("stock_gross_return_pct") or 0) > 0,
        "positive_absolute_net_1x": (metrics.get("net_return_pct", {}).get("1x") or 0) > 0,
        "positive_simple_excess": (metrics.get("simple_excess_return_pct") or 0) > 0,
        "positive_beta_adjusted_residual": (metrics.get("beta_adjusted_residual_return_pct") or 0) > 0,
        "payoff_ratio_over_one": (metrics.get("payoff_ratio") or 0) > 1,
        "reasonable_symbol_concentration": concentration["top_3_symbol_share_pct"] <= 50,
        "reasonable_signal_date_concentration": concentration["top_5_signal_date_share_pct"] <= 50,
        "market_component_not_dominant": metrics.get("market_component_share_pct") is None or metrics["market_component_share_pct"] <= 80,
    }
    passed = all(checks.values())
    summary_path = output_root / "phase13-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    phase13_candidate = "VALIDATION_CANDIDATE" if passed else "REJECTED"
    external = "PENDING_NEXT_PHASE" if passed else "NOT_READ"
    report = {
        "confirmation": "PASS" if passed else "FAIL",
        "candidate": result["candidate"], "freeze_sha": expected_freeze_sha,
        "confirmation_period": [d.isoformat() for d in CONFIRMATION],
        "cohort_sha256": result["cohort_sha256"],
        "confirmation_dataset_sha256": result["confirmation_dataset_sha256"],
        "input_cache_sha256_by_symbol_or_index": result["input_cache_sha256_by_symbol_or_index"],
        "metrics": metrics, "checks": checks,
        "preferred_absolute_gross_1pct_met": (metrics.get("stock_gross_return_pct") or 0) >= 1,
        "preferred_absolute_net_1_5x_met": (metrics.get("net_return_pct", {}).get("1.5x") or 0) > 0,
        "concentration": concentration, "signal_date_cluster_bootstrap": bootstrap,
        "outcome_rows": metrics["raw_observations"],
        "external_2026": external, "holdout_2026": "NOT_READ",
        "shadow_next_session": "NO", "alpha": "UNPROVEN", "live": "DISABLED",
    }
    _write_json(output_root / "phase13-confirmation.json", report)
    summary.update({
        "phase13_candidate": phase13_candidate, "residual_mechanism": "SUPPORTED" if passed else "WEAK",
        "confirmation": report["confirmation"], "external_2026": external,
        "holdout_2026": "NOT_READ", "shadow_next_session": "NO",
        "alpha": "UNPROVEN", "live": "DISABLED",
        "confirmation_freeze_sha": expected_freeze_sha,
        "confirmation_dataset_sha256": result["confirmation_dataset_sha256"],
        "confirmation_metrics": metrics, "confirmation_checks": checks,
        "generated_at_kst": datetime.now(KST).isoformat(),
    })
    _write_json(summary_path, summary)
    hypothesis_path = output_root / "phase13-hypothesis.json"
    hypothesis = json.loads(hypothesis_path.read_text(encoding="utf-8"))
    hypothesis.update({"phase13_candidate": phase13_candidate, "residual_mechanism": "SUPPORTED" if passed else "WEAK", "confirmation": report["confirmation"]})
    _write_json(hypothesis_path, hypothesis)
    index = _index_artifacts(output_root)
    _write_json(output_root / "phase13-artifact-index.json", index)
    integrity = verify_artifacts(output_root, index)
    _write_json(output_root / "artifact-integrity.json", integrity)
    if integrity["status"] != "PASS":
        raise RuntimeError("Phase 13 post-confirmation artifact integrity verification failed")
    return report


def read_external_2026() -> None:
    raise ProtectedPeriodError("2026 External is reserved for a later phase")


def read_holdout_2026() -> None:
    raise ProtectedPeriodError("2026 Holdout must remain unread")


def _bar_from_row(row: dict[str, Any]) -> Bar:
    timestamp = datetime.fromisoformat(str(row["timestamp"]))
    return Bar(
        time=timestamp,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row["volume"] or 0),
        turnover_krw=(int(row["turnover_krw"]) if row.get("turnover_krw") is not None else None),
    )


def _load_safe_parquet(
    path: Path, *, end_date: date = SAFE_DATA_END, allow_confirmation: bool = False
) -> list[Bar]:
    """Return safe rows only; never expose Confirmation/2026 row values to callers."""
    if end_date > SAFE_DATA_END and not (allow_confirmation and end_date == CONFIRMATION[1]):
        raise ProtectedPeriodError("cache row bound exceeds Phase 13 safe window without a verified Confirmation read")
    if not path.is_file():
        return []
    table = ds.dataset(path, format="parquet").to_table(
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover_krw"],
        filter=ds.field("timestamp") <= f"{end_date.isoformat()}T23:59:59+09:00",
        use_threads=False,
    )
    bars: list[Bar] = []
    for row in table.to_pylist():
        session = datetime.fromisoformat(row["timestamp"]).astimezone(KST).date()
        assert_phase13_data_date(session, allow_confirmation=allow_confirmation)
        bars.append(_bar_from_row(row))
    bars.sort(key=lambda item: item.time)
    if len({bar.time.astimezone(KST).date() for bar in bars}) != len(bars):
        raise ValueError(f"duplicate daily dates in cache file {path.name}")
    return bars


def load_phase13_cache(
    *, cache_root: Path = CACHE_ROOT, cohort_path: Path = COHORT_PATH,
    end_date: date = SAFE_DATA_END, allow_confirmation: bool = False,
) -> tuple[str, list[str], dict[str, str], str, dict[str, list[Bar]], dict[str, list[Bar]], dict[str, str]]:
    if end_date > SAFE_DATA_END and not (allow_confirmation and end_date == CONFIRMATION[1]):
        raise ProtectedPeriodError("Phase 13 cache load cannot exceed the safe row bound without Confirmation authorization")
    name, symbols, market_by_symbol, cohort_sha = _load_frozen_cohort(cohort_path)
    prices: dict[str, list[Bar]] = {}
    source_hashes: dict[str, str] = {}
    for symbol in symbols:
        path = cache_root / "daily" / f"{symbol}-1d-adjusted.parquet"
        sidecar = path.with_suffix(".metadata.json")
        if not path.is_file() or not sidecar.is_file():
            continue
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        if (
            metadata.get("symbol") != symbol
            or metadata.get("provider") != "KIS Open API"
            or metadata.get("adjustment_convention") != ADJUSTMENT
            or metadata.get("market") != market_by_symbol[symbol]
        ):
            raise ValueError(f"Phase 11 cache metadata mismatch for {symbol}")
        first_timestamp = metadata.get("first_timestamp")
        if first_timestamp and datetime.fromisoformat(first_timestamp).astimezone(KST).date() > end_date:
            continue
        source_hashes[symbol] = _cache_source_sha(path, metadata)
        bars = _load_safe_parquet(path, end_date=end_date, allow_confirmation=allow_confirmation)
        if bars:
            prices[symbol] = bars
    indexes: dict[str, list[Bar]] = {}
    for market in ("KOSPI", "KOSDAQ"):
        path = cache_root / "indexes" / f"{market}-1d-kis-index.parquet"
        sidecar = path.with_suffix(".metadata.json")
        if not path.is_file() or not sidecar.is_file():
            continue
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        if metadata.get("market") != market or metadata.get("provider") != "KIS Open API":
            raise ValueError(f"Phase 11 index metadata mismatch for {market}")
        source_hashes[f"INDEX_{market}"] = _cache_source_sha(path, metadata)
        indexes[market] = _load_safe_parquet(path, end_date=end_date, allow_confirmation=allow_confirmation)
    if set(indexes) != {"KOSPI", "KOSDAQ"}:
        raise ValueError("both matched KRX index caches are required")
    return name, symbols, market_by_symbol, cohort_sha, prices, indexes, source_hashes


def _bar_maps(bars_by_symbol: dict[str, list[Bar]]) -> dict[str, dict[date, Bar]]:
    return {
        symbol: {bar.time.astimezone(KST).date(): bar for bar in bars}
        for symbol, bars in bars_by_symbol.items()
    }


def _return_pct(bars: dict[date, Bar], sessions: list[date], index: int, count: int) -> float | None:
    if index < count:
        return None
    window = sessions[index - count:index + 1]
    if any(session not in bars for session in window):
        return None
    start, end = bars[window[0]].close, bars[window[-1]].close
    return (end / start - 1) * 100 if start > 0 else None


def _daily_pairs(
    stock: dict[date, Bar], market: dict[date, Bar], sessions: list[date], index: int, count: int,
    *, allow_short: bool = False,
) -> list[tuple[float, float]] | None:
    if index <= 0 or (index < count and not allow_short):
        return None
    window = min(count, index)
    pairs: list[tuple[float, float]] = []
    for current in range(index - window + 1, index + 1):
        previous_date, current_date = sessions[current - 1], sessions[current]
        if previous_date not in stock or current_date not in stock:
            return None
        if previous_date not in market or current_date not in market:
            return None
        prior_stock, now_stock = stock[previous_date].close, stock[current_date].close
        prior_market, now_market = market[previous_date].close, market[current_date].close
        if prior_stock <= 0 or prior_market <= 0:
            return None
        pairs.append((now_stock / prior_stock - 1, now_market / prior_market - 1))
    return pairs


def rolling_beta(
    stock_returns: Iterable[float], market_returns: Iterable[float], *, minimum_pairs: int = BETA_MIN_PAIRS
) -> tuple[float | None, int, float | None]:
    stock = list(stock_returns)[-BETA_WINDOW:]
    market = list(market_returns)[-BETA_WINDOW:]
    if len(stock) != len(market):
        raise ValueError("rolling-beta stock and market return arrays must have equal length")
    if len(stock) < minimum_pairs:
        return None, len(stock), None
    mean_stock, mean_market = statistics.mean(stock), statistics.mean(market)
    var_market = sum((value - mean_market) ** 2 for value in market)
    if var_market <= 0:
        return None, len(stock), None
    beta = sum((s - mean_stock) * (m - mean_market) for s, m in zip(stock, market, strict=True)) / var_market
    fitted = [mean_stock + beta * (m - mean_market) for m in market]
    total = sum((s - mean_stock) ** 2 for s in stock)
    r_squared = 1 - sum((s - fit) ** 2 for s, fit in zip(stock, fitted, strict=True)) / total if total > 0 else None
    return beta, len(stock), r_squared


def _residual_drawdown(
    stock: dict[date, Bar], market: dict[date, Bar], sessions: list[date], index: int, window: int
) -> tuple[float | None, float | None]:
    if index < window:
        return None, None
    residual_daily: list[float] = []
    for current in range(index - window + 1, index + 1):
        prior_date, current_date = sessions[current - 1], sessions[current]
        if prior_date not in stock or current_date not in stock:
            return None, None
        if prior_date not in market or current_date not in market:
            return None, None
        prior_stock, now_stock = stock[prior_date].close, stock[current_date].close
        prior_market, now_market = market[prior_date].close, market[current_date].close
        if prior_stock <= 0 or prior_market <= 0:
            return None, None
        residual_daily.append((now_stock / prior_stock - 1) - (now_market / prior_market - 1))
    path = [1.0]
    for value in residual_daily:
        path.append(path[-1] * (1 + value))
    peak = path[0]
    maximum_drawdown = 0.0
    for wealth in path:
        peak = max(peak, wealth)
        if peak > 0:
            maximum_drawdown = min(maximum_drawdown, wealth / peak - 1)
    distance = path[-1] / max(path) - 1 if max(path) > 0 else None
    return maximum_drawdown * 100, distance * 100 if distance is not None else None


def _turnover_ratio(stock: dict[date, Bar], sessions: list[date], index: int) -> float | None:
    if index < 20:
        return None
    prior = [stock.get(day).turnover_krw if stock.get(day) else None for day in sessions[index - 20:index]]
    recent = [stock.get(day).turnover_krw if stock.get(day) else None for day in sessions[index - 4:index + 1]]
    if any(value is None for value in prior + recent):
        return None
    baseline = statistics.median(value for value in prior if value is not None)
    return statistics.mean(value for value in recent if value is not None) / baseline if baseline > 0 else None


def _range_position(stock: dict[date, Bar], sessions: list[date], index: int, window: int = 20) -> float | None:
    if index < window - 1:
        return None
    rows = [stock.get(day) for day in sessions[index - window + 1:index + 1]]
    if any(row is None for row in rows):
        return None
    low = min(row.low for row in rows if row is not None)
    high = max(row.high for row in rows if row is not None)
    current = stock[sessions[index]].close
    return min(1.0, max(0.0, (current - low) / (high - low))) if high > low else None


def _index_context(indexes: dict[str, dict[date, Bar]], sessions: list[date], index: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for market in ("KOSPI", "KOSDAQ"):
        bars = indexes[market]
        for horizon in (5, 20):
            result[f"{market.lower()}_return_{horizon}d_pct"] = _return_pct(bars, sessions, index, horizon)
        pairs = _daily_pairs(bars, bars, sessions, index, 20)
        result[f"{market.lower()}_volatility_20d_pct"] = (
            statistics.stdev(pair[0] for pair in pairs) * 100 if pairs is not None else None
        )
    return result


def build_phase13_features(
    *, prices_by_symbol: dict[str, list[Bar]], indexes: dict[str, list[Bar]],
    symbols: list[str], market_by_symbol: dict[str, str], sessions: list[date],
    end_date: date = SAFE_DATA_END, allow_confirmation: bool = False,
) -> list[dict[str, Any]]:
    stock_maps = _bar_maps(prices_by_symbol)
    index_maps = _bar_maps(indexes)
    if end_date > SAFE_DATA_END and not (allow_confirmation and end_date == CONFIRMATION[1]):
        raise ProtectedPeriodError("feature construction cannot exceed the Phase 13 safe window without Confirmation authorization")
    safe_sessions = [day for day in sessions if WARMUP_START <= day <= end_date]
    if len(safe_sessions) != len(set(safe_sessions)):
        raise ValueError("duplicate index session in Phase 13 calendar")
    rows: list[dict[str, Any]] = []
    context_by_date = {day: _index_context(index_maps, safe_sessions, i) for i, day in enumerate(safe_sessions)}
    for symbol in symbols:
        market_name = market_by_symbol[symbol]
        stock, market = stock_maps.get(symbol, {}), index_maps[market_name]
        for i, session in enumerate(safe_sessions):
            period = _period_for(session)
            if period is None and allow_confirmation and CONFIRMATION[0] <= session <= CONFIRMATION[1]:
                period = "CONFIRMATION", CONFIRMATION
            if period is None or session not in stock:
                continue
            assert_phase13_data_date(session, allow_confirmation=allow_confirmation)
            row: dict[str, Any] = {
                "symbol": symbol, "market": market_name, "date": session.isoformat(),
                "period": period[0], "close_adjusted": stock[session].close,
                "turnover_krw": stock[session].turnover_krw,
            }
            for horizon in RETURN_HORIZONS:
                stock_return = _return_pct(stock, safe_sessions, i, horizon)
                market_return = _return_pct(market, safe_sessions, i, horizon)
                row[f"stock_return_{horizon}d_pct"] = stock_return
                row[f"matched_market_return_{horizon}d_pct"] = market_return
                row[f"excess_return_{horizon}d_pct"] = (
                    stock_return - market_return
                    if stock_return is not None and market_return is not None else None
                )
            pairs = _daily_pairs(stock, market, safe_sessions, i, BETA_WINDOW, allow_short=True)
            beta: float | None = None
            beta_n = 0
            r_squared: float | None = None
            if pairs is not None:
                beta, beta_n, r_squared = rolling_beta(
                    (item[0] for item in pairs), (item[1] for item in pairs)
                )
            row["beta_120d"] = beta
            row["beta_observations"] = beta_n
            row["beta_r_squared"] = r_squared
            row["beta_status"] = (
                "MISSING" if beta is None else
                "OUTSIDE_FIXED_SANITY_RANGE" if beta < EXTREME_BETA_RANGE[0] or beta > EXTREME_BETA_RANGE[1]
                else "IN_RANGE_UNCLIPPED"
            )
            for horizon in RETURN_HORIZONS:
                market_return = row[f"matched_market_return_{horizon}d_pct"]
                row[f"residual_return_{horizon}d_pct"] = (
                    row[f"stock_return_{horizon}d_pct"] - beta * market_return
                    if beta is not None and market_return is not None else None
                )
            daily20 = _daily_pairs(stock, market, safe_sessions, i, 20)
            row["idio_volatility_20d_pct"] = (
                statistics.stdev([a - b for a, b in daily20]) * 100 if daily20 is not None else None
            )
            row["turnover_ratio_5d_vs_prior20_median"] = _turnover_ratio(stock, safe_sessions, i)
            row["range_position_20d"] = _range_position(stock, safe_sessions, i)
            row["residual_drawdown_20d_pct"], row["residual_distance_20d_high_pct"] = _residual_drawdown(
                stock, market, safe_sessions, i, 20
            )
            row["residual_drawdown_60d_pct"], row["residual_distance_60d_high_pct"] = _residual_drawdown(
                stock, market, safe_sessions, i, 60
            )
            row.update(context_by_date[session])
            rows.append(row)
    _assign_cross_sectional_ranks(rows)
    _assign_market_breadth_and_dispersion(rows)
    return rows


def _assign_cross_sectional_ranks(rows: list[dict[str, Any]]) -> None:
    fields = {
        "excess_return_1d_pct": "excess_rank_1d_pct",
        "excess_return_5d_pct": "excess_rank_5d_pct",
        "excess_return_20d_pct": "excess_rank_20d_pct",
        "residual_return_5d_pct": "residual_rank_5d_pct",
        "residual_return_20d_pct": "residual_rank_20d_pct",
        "idio_volatility_20d_pct": "idio_volatility_rank_pct",
        "turnover_ratio_5d_vs_prior20_median": "turnover_rank_pct",
    }
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["market"], row["date"])].append(row)
    for group in groups.values():
        for source, target in fields.items():
            valid = sorted(
                (row for row in group if row.get(source) is not None),
                key=lambda row: (float(row[source]), row["symbol"]),
            )
            count = len(valid)
            cursor = 0
            while cursor < count:
                end = cursor + 1
                while end < count and float(valid[end][source]) == float(valid[cursor][source]):
                    end += 1
                average_rank = ((cursor + 1) + end) / 2
                percentile = ((average_rank - 1) / (count - 1)) if count > 1 else 0.5
                for row in valid[cursor:end]:
                    row[target] = percentile * 100
                    row[f"{target}_participants"] = count
                cursor = end
            for row in group:
                if row.get(source) is None:
                    row[target] = None
                    row[f"{target}_participants"] = count
        valid_residual = [row for row in group if row.get("residual_rank_20d_pct") is not None]
        for row in valid_residual:
            rank = float(row["residual_rank_20d_pct"])
            row["residual_rank_20d_state"] = "LOWER" if rank < 33.333333 else "UPPER" if rank >= 66.666667 else "MIDDLE"
        for row in group:
            if row.get("residual_rank_20d_pct") is None:
                row["residual_rank_20d_state"] = None
        valid_5d = [row for row in group if row.get("residual_rank_5d_pct") is not None]
        for row in valid_5d:
            rank = float(row["residual_rank_5d_pct"])
            row["residual_rank_5d_state"] = "LOWER" if rank < 33.333333 else "UPPER" if rank >= 66.666667 else "MIDDLE"
        for row in group:
            if row.get("residual_rank_5d_pct") is None:
                row["residual_rank_5d_state"] = None


def _assign_market_breadth_and_dispersion(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["market"], row["date"])].append(row)
    for group in groups.values():
        observed = [row for row in group if row.get("excess_return_1d_pct") is not None]
        breadth = (
            sum(float(row["stock_return_1d_pct"]) > 0 for row in observed) / len(observed) * 100
            if observed else None
        )
        dispersion = (
            statistics.stdev(float(row["excess_return_1d_pct"]) for row in observed)
            if len(observed) >= 2 else None
        )
        for row in group:
            row["market_breadth_positive_pct"] = breadth
            row["market_breadth_participants"] = len(observed)
            row["market_excess_dispersion_1d_pct"] = dispersion


def _forward_outcomes(
    features: list[dict[str, Any]], prices_by_symbol: dict[str, list[Bar]],
    indexes: dict[str, list[Bar]], sessions: list[date], *, allow_confirmation: bool = False,
) -> list[dict[str, Any]]:
    stocks, market_maps = _bar_maps(prices_by_symbol), _bar_maps(indexes)
    positions = {session: index for index, session in enumerate(sessions)}
    outcomes: list[dict[str, Any]] = []
    for feature in features:
        signal_date = date.fromisoformat(feature["date"])
        period_info = _period_for(signal_date)
        if period_info is None and allow_confirmation and CONFIRMATION[0] <= signal_date <= CONFIRMATION[1]:
            period_info = "CONFIRMATION", CONFIRMATION
        _, period = period_info or (None, None)
        if period is None:
            continue
        index = positions[signal_date]
        symbol, market_name = feature["symbol"], feature["market"]
        stock, market = stocks.get(symbol, {}), market_maps[market_name]
        for horizon in HORIZONS:
            entry_i, exit_i = index + 1, index + horizon
            if exit_i >= len(sessions) or sessions[exit_i] > period[1]:
                continue
            entry_date, exit_date = sessions[entry_i], sessions[exit_i]
            if entry_date not in stock or exit_date not in stock or entry_date not in market or exit_date not in market:
                continue
            entry_open, exit_close = stock[entry_date].open, stock[exit_date].close
            market_open, market_close = market[entry_date].open, market[exit_date].close
            if min(entry_open, market_open) <= 0:
                continue
            gross = (exit_close / entry_open - 1) * 100
            market_return = (market_close / market_open - 1) * 100
            beta = feature.get("beta_120d")
            highs = [stock[sessions[j]].high for j in range(entry_i, exit_i + 1) if sessions[j] in stock]
            lows = [stock[sessions[j]].low for j in range(entry_i, exit_i + 1) if sessions[j] in stock]
            row = {
                "symbol": symbol, "market": market_name, "signal_date": signal_date.isoformat(),
                "period": feature.get("period", _period_for(signal_date)[0] if _period_for(signal_date) else "UNKNOWN"),
                "entry_date": entry_date.isoformat(),
                "entry_open": entry_open, "exit_date": exit_date.isoformat(), "exit_close": exit_close,
                "horizon_sessions": horizon, "stock_gross_return_pct": gross,
                "matched_market_return_pct": market_return,
                "excess_return_pct": gross - market_return,
                "beta_120d": beta,
                "beta_residual_return_pct": gross - beta * market_return if beta is not None else None,
                "mfe_pct": (max(highs) / entry_open - 1) * 100 if highs else None,
                "mae_pct": (min(lows) / entry_open - 1) * 100 if lows else None,
            }
            outcomes.append(row)
    return outcomes


def non_overlapping_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    last_exit: dict[str, str] = {}
    for event in sorted(events, key=lambda row: (row["symbol"], row["signal_date"], row.get("market", ""))):
        if event["entry_date"] <= last_exit.get(event["symbol"], ""):
            continue
        result.append(event)
        last_exit[event["symbol"]] = event["exit_date"]
    return result


def _state(value: float | None, feature: str, *, idio_cuts: tuple[float, float] | None = None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        if feature in {"residual_rank_5d_state", "residual_rank_20d_state"}:
            return value
        return None
    if not math.isfinite(float(value)):
        return None
    if feature in FACTOR_BUCKETS:
        for low, high, label in FACTOR_BUCKETS[feature]:
            if (low is None or value >= low) and (high is None or value < high):
                return label
        return None
    if feature == "idio_volatility_20d_pct":
        if idio_cuts is None:
            return None
        return "LOW" if value < idio_cuts[0] else "MID" if value < idio_cuts[1] else "HIGH"
    return None


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - index) + ordered[high] * (index - low)


def _period_idio_cuts(features: list[dict[str, Any]]) -> tuple[float, float] | None:
    values = [float(row["idio_volatility_20d_pct"]) for row in features if row["period"] == "DISCOVERY" and row.get("idio_volatility_20d_pct") is not None]
    low, high = _quantile(values, 1 / 3), _quantile(values, 2 / 3)
    return (low, high) if low is not None and high is not None else None


def _metrics(events: list[dict[str, Any]], *, execution_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    selected = events if execution_events is None else execution_events
    def mean(field: str, rows: list[dict[str, Any]]) -> float | None:
        values = [float(row[field]) for row in rows if row.get(field) is not None]
        return statistics.mean(values) if values else None
    gross = mean("stock_gross_return_pct", selected)
    market = mean("matched_market_return_pct", selected)
    excess = mean("excess_return_pct", selected)
    residual = mean("beta_residual_return_pct", selected)
    wins = [float(row["stock_gross_return_pct"]) for row in selected if row["stock_gross_return_pct"] > 0]
    losses = [float(row["stock_gross_return_pct"]) for row in selected if row["stock_gross_return_pct"] < 0]
    unique_dates = {row["signal_date"] for row in selected}
    unique_market_dates = {(row["market"], row["signal_date"]) for row in selected}
    raw_unique_dates = {row["signal_date"] for row in events}
    raw_market_dates = {(row["market"], row["signal_date"]) for row in events}
    return {
        "raw_observations": len(events),
        "non_overlapping_executions": len(selected),
        "raw_unique_signal_dates": len(raw_unique_dates),
        "raw_unique_market_state_dates": len(raw_market_dates),
        "unique_signal_dates": len(unique_dates),
        "unique_market_state_dates": len(unique_market_dates),
        "unique_symbols": len({row["symbol"] for row in selected}),
        "stock_gross_return_pct": _round(gross),
        "matched_market_return_pct": _round(market),
        "simple_excess_return_pct": _round(excess),
        "beta_adjusted_residual_return_pct": _round(residual),
        "net_return_pct": {
            f"{multiple:g}x": _round(gross - COST_PCT * multiple if gross is not None else None)
            for multiple in COST_MULTIPLIERS
        },
        "break_even_friction_pct": _round(gross),
        "payoff_ratio": _round(statistics.mean(wins) / abs(statistics.mean(losses)) if wins and losses else None),
        "market_component_share_pct": _round(market / gross * 100 if gross is not None and market is not None and abs(gross) >= 0.1 else None),
        "mean_mfe_pct": mean("mfe_pct", selected),
        "mean_mae_pct": mean("mae_pct", selected),
    }


def _mean_date_cluster_bootstrap(
    events: list[dict[str, Any]], *, iterations: int = BOOTSTRAP_ITERATIONS, seed: int = BOOTSTRAP_SEED
) -> dict[str, Any]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        clusters[event["signal_date"]].append(event)
    if len(clusters) < 2:
        return {"status": "INSUFFICIENT_DATE_CLUSTERS", "date_clusters": len(clusters)}
    metrics = {
        "stock_gross_return_pct": "stock_gross_return_pct",
        "simple_excess_return_pct": "excess_return_pct",
        "beta_adjusted_residual_return_pct": "beta_residual_return_pct",
    }
    by_cluster = {
        day: {output_key: statistics.mean(float(row[source_key]) for row in rows if row.get(source_key) is not None)
              for output_key, source_key in metrics.items()
              if any(row.get(source_key) is not None for row in rows)}
        for day, rows in clusters.items()
    }
    rng = random.Random(seed)
    distribution: dict[str, list[float]] = {key: [] for key in metrics}
    days = sorted(by_cluster)
    for _ in range(iterations):
        sample = [rng.choice(days) for _ in days]
        for key in metrics:
            values = [by_cluster[day][key] for day in sample if key in by_cluster[day]]
            if values:
                distribution[key].append(statistics.mean(values))
    return {
        "status": "PASS", "cluster_unit": "signal_date (both market cohorts on the same date remain together)",
        "date_clusters": len(clusters), "iterations": iterations, "seed": seed,
        "interval_pct_5_95": {
            key: [_round(_quantile(values, 0.05)), _round(_quantile(values, 0.95))]
            for key, values in distribution.items()
        },
    }


def _factor_groups(
    features: list[dict[str, Any]], outcomes: list[dict[str, Any]], *, idio_cuts: tuple[float, float] | None
) -> dict[str, list[dict[str, Any]]]:
    feature_by_key = {(row["symbol"], row["date"]): row for row in features}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        feature = feature_by_key[(outcome["symbol"], outcome["signal_date"])]
        for family, factors in MAP_FAMILIES.items():
            for factor in factors:
                raw = feature.get(factor)
                label = _state(raw, factor, idio_cuts=idio_cuts)
                if label is None:
                    continue
                negative_state = label.startswith("LE_−") or label in {"−8_TO_−4", "−4_TO_−2", "−2_TO_0"}
                if family.endswith("MOMENTUM") and negative_state:
                    continue
                if family.endswith("REVERSAL") and not negative_state:
                    continue
                record = dict(outcome)
                record["factor_family"], record["factor"], record["state"] = family, factor, label
                record["factor_value"] = raw
                groups[f"{family}|{factor}|{label}|{outcome['period']}|{outcome['horizon_sessions']}"] .append(record)
    return groups


def _factor_map_payload(groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for key in sorted(groups):
        family, factor, state, period, horizon = key.split("|", 4)
        events = groups[key]
        selected = non_overlapping_events(events)
        entries.append({
            "family": family, "factor": factor, "state": state, "period": period,
            "forward_horizon_sessions": int(horizon), "metrics": _metrics(events, execution_events=selected),
        })
    return {
        "status": "PASS" if entries else "FAIL", "factor_definition": "T-only broad states; missing values excluded",
        "cost_round_trip_pct": COST_PCT, "maps": entries,
    }


def _market_split(groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    for key in sorted(groups):
        family, factor, state, period, horizon = key.split("|", 4)
        for market in ("KOSPI", "KOSDAQ"):
            events = [row for row in groups[key] if row["market"] == market]
            if not events:
                continue
            selected = non_overlapping_events(events)
            result.append({
                "family": family, "factor": factor, "state": state, "period": period,
                "forward_horizon_sessions": int(horizon), "market": market,
                "metrics": _metrics(events, execution_events=selected),
            })
    return {"scope": "separate descriptive splits; not a post-hoc cohort restriction", "results": result}


INTERACTION_DEFINITIONS = (
        ("EXCESS_5D_X_TURNOVER", "excess_return_5d_pct", "turnover_ratio_5d_vs_prior20_median"),
        ("RESIDUAL_DRAWDOWN_X_IDIO_VOL", "residual_drawdown_20d_pct", "idio_volatility_20d_pct"),
        ("RESIDUAL_RANK_X_TURNOVER", "residual_rank_20d_state", "turnover_ratio_5d_vs_prior20_median"),
)


def _interaction_event_groups(
    features: list[dict[str, Any]], outcomes: list[dict[str, Any]], idio_cuts: tuple[float, float] | None
) -> dict[tuple[str, str, str, str, str, int], list[dict[str, Any]]]:
    features_by_key = {(row["symbol"], row["date"]): row for row in features}
    grouped: dict[tuple[str, str, str, str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for name, left, right in INTERACTION_DEFINITIONS:
        for event in outcomes:
            feature = features_by_key[(event["symbol"], event["signal_date"])]
            left_state = _state(feature.get(left), left, idio_cuts=idio_cuts)
            right_state = _state(feature.get(right), right, idio_cuts=idio_cuts)
            if left_state is not None and right_state is not None:
                grouped[(name, event["period"], event["market"], left_state, right_state, event["horizon_sessions"])].append(event)
    return grouped


def _candidate_name(
    market: str, left_factor: str, left_state: str, right_factor: str, right_state: str
) -> str:
    negative_states = {"LE_−8", "LE_−4", "−8_TO_−4", "−4_TO_−2", "−2_TO_0"}
    if left_factor.startswith("excess_return"):
        left_name = "EXCESS_REVERSAL" if left_state in negative_states else "EXCESS_MOMENTUM"
    elif left_factor.startswith("residual_drawdown"):
        left_name = "RESIDUAL_DRAWDOWN_RECOVERY"
    elif left_factor.startswith("residual_rank"):
        left_name = "RESIDUAL_RANK"
    else:
        left_name = left_factor.upper()

    if right_factor == "turnover_ratio_5d_vs_prior20_median":
        right_name = f"{right_state}_TURNOVER"
    elif right_factor == "idio_volatility_20d_pct":
        right_name = f"IDIOSYNCRATIC_VOLATILITY_{right_state}"
    else:
        right_name = f"{right_factor.upper()}_{right_state}"
    return f"{market}_{left_name}_WITH_{right_name}"


def _two_factor_interactions(
    features: list[dict[str, Any]], outcomes: list[dict[str, Any]], idio_cuts: tuple[float, float] | None
) -> dict[str, Any]:
    grouped = _interaction_event_groups(features, outcomes, idio_cuts)
    output: list[dict[str, Any]] = []
    for name, left, right in INTERACTION_DEFINITIONS:
        cells = []
        for (interaction, period, market, left_state, right_state, horizon), events in sorted(grouped.items()):
            if interaction != name:
                continue
            selected = non_overlapping_events(events)
            cells.append({
                "period": period, "market": market, "left_state": left_state, "right_state": right_state,
                "forward_horizon_sessions": horizon, "metrics": _metrics(events, execution_events=selected),
            })
        output.append({"interaction": name, "left_factor": left, "right_factor": right, "cells": cells})
    return {"maximum_factors_per_interaction": 2, "interactions": output}


def _interaction_candidate_candidates(
    interaction_groups: dict[tuple[str, str, str, str, str, int], list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    keys = {key for key in interaction_groups if key[1] == "DISCOVERY" and key[5] == 5}
    candidates: list[dict[str, Any]] = []
    for name, _, market, left_state, right_state, horizon in sorted(keys):
        discovery = interaction_groups[(name, "DISCOVERY", market, left_state, right_state, horizon)]
        touched_key = (name, "TOUCHED_REPLICATION", market, left_state, right_state, horizon)
        touched = interaction_groups.get(touched_key, [])
        discovery_exec = non_overlapping_events(discovery)
        touched_exec = non_overlapping_events(touched)
        discovery_metrics = _metrics(discovery, execution_events=discovery_exec)
        touched_metrics = _metrics(touched, execution_events=touched_exec)
        all_events = discovery + touched
        block_specs = (
            ("2023_H1", date(2023, 1, 2), date(2023, 6, 30)),
            ("2023_H2", date(2023, 7, 1), date(2023, 12, 31)),
            ("2024_H1", date(2024, 1, 1), date(2024, 6, 28)),
            ("2024_H2", date(2024, 7, 1), date(2024, 12, 31)),
            ("2025_H1", date(2025, 1, 1), date(2025, 6, 30)),
        )
        blocks = []
        for label, start, end in block_specs:
            events = [row for row in all_events if start <= date.fromisoformat(row["signal_date"]) <= end]
            blocks.append({"block": label, "metrics": _metrics(events, execution_events=non_overlapping_events(events))})
        positive_blocks = sum(
            1 for block in blocks
            if (block["metrics"].get("stock_gross_return_pct") or 0) > 0
            and (block["metrics"].get("simple_excess_return_pct") or 0) > 0
            and (block["metrics"].get("beta_adjusted_residual_return_pct") or 0) > 0
        )
        discovery_concentration = _concentration(discovery)
        touched_concentration = _concentration(touched)
        checks = {
            "discovery_non_overlap_min_50": discovery_metrics["non_overlapping_executions"] >= 50,
            "touched_non_overlap_min_50": touched_metrics["non_overlapping_executions"] >= 50,
            "discovery_unique_dates_min_40": discovery_metrics["unique_signal_dates"] >= 40,
            "touched_unique_dates_min_40": touched_metrics["unique_signal_dates"] >= 40,
            "same_market_and_factor_states": bool(discovery and touched),
            "positive_excess_both_periods": (discovery_metrics.get("simple_excess_return_pct") or 0) > 0 and (touched_metrics.get("simple_excess_return_pct") or 0) > 0,
            "positive_beta_residual_both_periods": (discovery_metrics.get("beta_adjusted_residual_return_pct") or 0) > 0 and (touched_metrics.get("beta_adjusted_residual_return_pct") or 0) > 0,
            "positive_absolute_gross_both_periods": (discovery_metrics.get("stock_gross_return_pct") or 0) > 0 and (touched_metrics.get("stock_gross_return_pct") or 0) > 0,
            "positive_absolute_net_1x_both_periods": (discovery_metrics.get("net_return_pct", {}).get("1x") or 0) > 0 and (touched_metrics.get("net_return_pct", {}).get("1x") or 0) > 0,
            "payoff_ratio_over_one_both_periods": (discovery_metrics.get("payoff_ratio") or 0) > 1 and (touched_metrics.get("payoff_ratio") or 0) > 1,
            "at_least_three_chronological_blocks_same_direction": positive_blocks >= 3,
            "concentration_within_fixed_limits": all(
                concentration["top_3_symbol_share_pct"] <= 50
                and concentration["top_5_signal_date_share_pct"] <= 50
                for concentration in (discovery_concentration, touched_concentration)
            ),
            "market_component_not_dominant": all(
                metrics.get("market_component_share_pct") is None or metrics["market_component_share_pct"] <= 80
                for metrics in (discovery_metrics, touched_metrics)
            ),
            "date_cluster_bootstrap_available": discovery_metrics["unique_signal_dates"] >= 40 and touched_metrics["unique_signal_dates"] >= 40,
        }
        candidate = {
            "interaction": name, "market": market, "left_state": left_state, "right_state": right_state,
            "forward_horizon_sessions": horizon, "discovery_metrics": discovery_metrics,
            "touched_replication_metrics": touched_metrics, "discovery_concentration": discovery_concentration,
            "touched_replication_concentration": touched_concentration,
            "discovery_cluster_bootstrap": None, "touched_cluster_bootstrap": None,
            "chronological_blocks": blocks, "positive_chronological_blocks": positive_blocks,
            "gate": {"pass": all(checks.values()), "checks": checks, "failed_checks": [k for k, v in checks.items() if not v]},
        }
        if candidate["gate"]["pass"]:
            candidate["discovery_cluster_bootstrap"] = _mean_date_cluster_bootstrap(discovery_exec)
            candidate["touched_cluster_bootstrap"] = _mean_date_cluster_bootstrap(touched_exec)
        candidates.append(candidate)
    return candidates


def _select_interaction_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [candidate for candidate in candidates if candidate["gate"]["pass"]]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: (
            min(item["discovery_metrics"]["beta_adjusted_residual_return_pct"], item["touched_replication_metrics"]["beta_adjusted_residual_return_pct"]),
            item["positive_chronological_blocks"],
            min(item["discovery_metrics"]["unique_signal_dates"], item["touched_replication_metrics"]["unique_signal_dates"]),
            -max(item["discovery_concentration"]["top_3_symbol_share_pct"], item["touched_replication_concentration"]["top_3_symbol_share_pct"]),
            item["interaction"], item["market"], item["left_state"], item["right_state"],
        ),
    )


def _select_discovery_leader(groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    choices = []
    for key, events in groups.items():
        family, factor, state, period, horizon = key.split("|", 4)
        if period != "DISCOVERY" or int(horizon) != 5:
            continue
        selected = non_overlapping_events(events)
        metrics = _metrics(events, execution_events=selected)
        residual = metrics["beta_adjusted_residual_return_pct"]
        if residual is None:
            continue
        choices.append((float(residual), float(metrics["simple_excess_return_pct"] or -999), factor, state, family, metrics, events, selected))
    if not choices:
        return None
    best = max(choices, key=lambda row: (row[0], row[1], row[4], row[2], row[3]))
    return {
        "family": best[4], "factor": best[2], "state": best[3], "period": "DISCOVERY",
        "forward_horizon_sessions": 5, "metrics": best[5], "events": best[6], "executions": best[7],
    }


def _chronological_stability(leader: dict[str, Any] | None, groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    if leader is None:
        return {"status": "NOT_AVAILABLE", "blocks": []}
    periods = (
        ("2023_H1", date(2023, 1, 2), date(2023, 6, 30)),
        ("2023_H2", date(2023, 7, 1), date(2023, 12, 31)),
        ("2024_H1", date(2024, 1, 1), date(2024, 6, 28)),
        ("2024_H2", date(2024, 7, 1), date(2024, 12, 31)),
        ("2025_H1", date(2025, 1, 1), date(2025, 6, 30)),
    )
    blocks: list[dict[str, Any]] = []
    for label, start, end in periods:
        phase = "DISCOVERY" if end <= DISCOVERY[1] else "TOUCHED_REPLICATION"
        group_key = f"{leader['family']}|{leader['factor']}|{leader['state']}|{phase}|5"
        events = [
            row for row in groups.get(group_key, [])
            if start <= date.fromisoformat(row["signal_date"]) <= end
        ]
        selected = non_overlapping_events(events)
        blocks.append({"block": label, "metrics": _metrics(events, execution_events=selected)})
    return {"leader_factor_state": {k: leader[k] for k in ("family", "factor", "state")}, "blocks": blocks}


def _concentration(events: list[dict[str, Any]]) -> dict[str, Any]:
    selected = non_overlapping_events(events)
    positive_total = sum(max(0.0, float(row["stock_gross_return_pct"])) for row in selected)
    top_trades = sorted(
        (max(0.0, float(row["stock_gross_return_pct"])) for row in selected), reverse=True
    )
    def top_shares(key: str, count: int) -> list[dict[str, Any]]:
        grouped: dict[str, float] = defaultdict(float)
        for row in selected:
            grouped[str(row[key])] += max(0.0, float(row["stock_gross_return_pct"]))
        total = sum(grouped.values())
        return [
            {"key": label, "positive_contribution_pct": _round(amount), "share_of_positive_contribution_pct": _round(amount / total * 100 if total > 0 else None)}
            for label, amount in sorted(grouped.items(), key=lambda item: (-item[1], item[0]))[:count]
        ]
    return {
        "non_overlapping_executions": len(selected),
        "positive_contribution_total_pct_points": _round(positive_total),
        "top_trade_contribution": {
            "gross_pct": _round(top_trades[0] if top_trades else 0.0),
            "share_of_positive_contribution_pct": _round(top_trades[0] / positive_total * 100 if positive_total else None),
        },
        "top_5_trade_contribution": {
            "gross_pct_points": _round(sum(top_trades[:5])),
            "share_of_positive_contribution_pct": _round(sum(top_trades[:5]) / positive_total * 100 if positive_total else None),
        },
        "top_symbols": top_shares("symbol", 5), "top_signal_dates": top_shares("signal_date", 5),
        "top_3_symbol_share_pct": _round(sum(row["share_of_positive_contribution_pct"] or 0 for row in top_shares("symbol", 3))),
        "top_5_signal_date_share_pct": _round(sum(row["share_of_positive_contribution_pct"] or 0 for row in top_shares("signal_date", 5))),
        "market_event_clustering": {
            "unique_signal_dates": len({row["signal_date"] for row in selected}),
            "unique_market_state_dates": len({(row["market"], row["signal_date"]) for row in selected}),
        },
    }


def _candidate_gate(
    leader: dict[str, Any] | None,
    replication_metrics: dict[str, Any],
    chronological: dict[str, Any],
    concentration: dict[str, Any] | None,
) -> dict[str, Any]:
    if leader is None:
        return {"pass": False, "checks": {}, "failed_checks": ["NO_DISCOVERY_STATE"]}
    discovery = leader["metrics"]
    touched = replication_metrics
    blocks = chronological.get("blocks", [])
    same_direction_blocks = sum(
        1 for block in blocks
        if (block["metrics"].get("stock_gross_return_pct") or 0) > 0
        and (block["metrics"].get("beta_adjusted_residual_return_pct") or 0) > 0
    )
    checks = {
        "discovery_non_overlap_min_50": discovery["non_overlapping_executions"] >= 50,
        "touched_non_overlap_min_50": touched["non_overlapping_executions"] >= 50,
        "discovery_unique_dates_min_40": discovery["unique_signal_dates"] >= 40,
        "touched_unique_dates_min_40": touched["unique_signal_dates"] >= 40,
        "positive_excess_both_periods": (discovery.get("simple_excess_return_pct") or 0) > 0 and (touched.get("simple_excess_return_pct") or 0) > 0,
        "positive_beta_residual_both_periods": (discovery.get("beta_adjusted_residual_return_pct") or 0) > 0 and (touched.get("beta_adjusted_residual_return_pct") or 0) > 0,
        "positive_absolute_gross_both_periods": (discovery.get("stock_gross_return_pct") or 0) > 0 and (touched.get("stock_gross_return_pct") or 0) > 0,
        "positive_absolute_net_1x_both_periods": (discovery.get("net_return_pct", {}).get("1x") or 0) > 0 and (touched.get("net_return_pct", {}).get("1x") or 0) > 0,
        "payoff_ratio_over_one_both_periods": (discovery.get("payoff_ratio") or 0) > 1 and (touched.get("payoff_ratio") or 0) > 1,
        "at_least_three_chronological_blocks_same_direction": same_direction_blocks >= 3,
        "concentration_within_fixed_limits": bool(
            concentration
            and concentration.get("top_3_symbol_share_pct", 100) <= 50
            and concentration.get("top_5_signal_date_share_pct", 100) <= 50
        ),
        "market_component_not_dominant": all(
            metric.get("market_component_share_pct") is None or metric["market_component_share_pct"] <= 80
            for metric in (discovery, touched)
        ),
    }
    failed = [key for key, passed in checks.items() if not passed]
    return {
        "pass": not failed, "checks": checks, "failed_checks": failed,
        "chronological_positive_blocks": same_direction_blocks,
        "absolute_gross_1pct_is_preferred_not_mandatory": True,
        "absolute_net_1_5x_is_preferred_not_mandatory": True,
    }


def _artifact_snapshot() -> dict[str, str]:
    result: dict[str, str] = {}
    root = Path("runtime/research")
    for phase in range(5, 13):
        directory = root / f"phase{phase}"
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            name = path.name
            if path.is_file() and ("manifest" in name or "artifact-index" in name):
                result[path.relative_to(root).as_posix()] = _sha256(path)
    return result


def _source_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNAVAILABLE"


def _data_digest(prices: dict[str, list[Bar]], indexes: dict[str, list[Bar]]) -> str:
    digest = hashlib.sha256()
    for source, series in [(f"STOCK:{key}", prices[key]) for key in sorted(prices)] + [
        (f"INDEX:{key}", indexes[key]) for key in sorted(indexes)
    ]:
        for bar in series:
            row = {
                "source": source, "date": bar.time.astimezone(KST).date().isoformat(),
                "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
                "volume": bar.volume, "turnover_krw": bar.turnover_krw,
            }
            digest.update(_json_bytes(row))
            digest.update(b"\n")
    return digest.hexdigest()


def _date_sessions(indexes: dict[str, list[Bar]], *, end_date: date = SAFE_DATA_END) -> list[date]:
    sessions = sorted({bar.time.astimezone(KST).date() for bars in indexes.values() for bar in bars})
    return [day for day in sessions if WARMUP_START <= day <= end_date]


def _feature_availability(features: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(features)
    fields = (
        "excess_return_1d_pct", "excess_return_3d_pct", "excess_return_5d_pct", "excess_return_10d_pct",
        "excess_return_20d_pct", "beta_120d", "residual_return_5d_pct", "residual_return_20d_pct",
        "residual_drawdown_20d_pct", "residual_drawdown_60d_pct", "idio_volatility_20d_pct",
        "turnover_ratio_5d_vs_prior20_median", "range_position_20d",
        "market_breadth_positive_pct", "market_excess_dispersion_1d_pct",
    )
    return {
        "feature_rows": count,
        "outcome_rows_by_horizon": {str(h): sum(row["horizon_sessions"] == h for row in outcomes) for h in HORIZONS},
        "non_null_feature_count": {field: sum(row.get(field) is not None for row in features) for field in fields},
        "missing_beta_rows": sum(row.get("beta_120d") is None for row in features),
        "missing_turnover_rows": sum(row.get("turnover_ratio_5d_vs_prior20_median") is None for row in features),
        "missing_values_policy": "excluded from the corresponding factor map; never filled with zero",
        "features_use_current_or_prior_session_only": True,
    }


def _market_context_diagnostics(features: list[dict[str, Any]]) -> dict[str, Any]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in features:
        unique[(row["market"], row["date"])] = row
    result = []
    for period in ("DISCOVERY", "TOUCHED_REPLICATION"):
        for market in ("KOSPI", "KOSDAQ"):
            rows = [row for (market_name, _), row in unique.items() if market_name == market and row["period"] == period]
            if not rows:
                continue
            prefix = market.lower()
            def avg(field: str, source_rows: list[dict[str, Any]] = rows) -> float | None:
                values = [float(row[field]) for row in source_rows if row.get(field) is not None]
                return statistics.mean(values) if values else None
            result.append({
                "period": period, "market": market, "unique_market_dates": len(rows),
                "mean_index_return_5d_pct": _round(avg(f"{prefix}_return_5d_pct")),
                "mean_index_return_20d_pct": _round(avg(f"{prefix}_return_20d_pct")),
                "mean_index_volatility_20d_pct": _round(avg(f"{prefix}_volatility_20d_pct")),
                "mean_sample_breadth_positive_pct": _round(avg("market_breadth_positive_pct")),
                "mean_market_relative_dispersion_1d_pct": _round(avg("market_excess_dispersion_1d_pct")),
                "breadth_participants_mean": _round(avg("market_breadth_participants")),
            })
    return {
        "role": "diagnostic conditioning only; no market context is a primary alpha factor",
        "definition": "breadth is current frozen-cohort positive stock-return share; dispersion is cross-sectional stdev of one-day stock-minus-index returns",
        "results": result,
    }


def _emit_panel(path: Path, features: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> None:
    features_by_key = {(row["symbol"], row["date"]): row for row in features}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = io.StringIO()
    for outcome in sorted(outcomes, key=lambda row: (row["signal_date"], row["symbol"], row["horizon_sessions"])):
        record = dict(features_by_key[(outcome["symbol"], outcome["signal_date"])])
        record["forward_outcome"] = outcome
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    path.write_bytes(gzip.compress(stream.getvalue().encode("utf-8"), mtime=0))


def _index_artifacts(output_root: Path) -> dict[str, Any]:
    files = []
    for path in sorted(output_root.iterdir()):
        if not path.is_file() or path.name in {"phase13-artifact-index.json", "artifact-integrity.json"} or path.name.endswith(".tmp"):
            continue
        files.append({"path": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)})
    return {"artifact": "phase13-artifact-index", "algorithm": "SHA-256", "artifacts": files}


def verify_artifacts(output_root: Path, index: dict[str, Any]) -> dict[str, Any]:
    failures = []
    for item in index.get("artifacts", []):
        path = output_root / item["path"]
        if not path.is_file() or path.stat().st_size != item["size_bytes"] or _sha256(path) != item["sha256"]:
            failures.append(item["path"])
    return {"status": "PASS" if not failures else "FAIL", "verified": len(index.get("artifacts", [])), "failures": failures}


def run_phase13(
    *, output_root: Path = OUTPUT_ROOT, cache_root: Path = CACHE_ROOT, cohort_path: Path = COHORT_PATH
) -> dict[str, Any]:
    """Run Phase 13's frozen current-cohort research without opening protected outcomes."""
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prior_before = _artifact_snapshot()
    cohort_name, symbols, markets, cohort_sha, prices, indexes, cache_hashes = load_phase13_cache(
        cache_root=cache_root, cohort_path=cohort_path
    )
    sessions = _date_sessions(indexes)
    features = build_phase13_features(
        prices_by_symbol=prices, indexes=indexes, symbols=symbols,
        market_by_symbol=markets, sessions=sessions,
    )
    outcomes = _forward_outcomes(features, prices, indexes, sessions)
    idio_cuts = _period_idio_cuts(features)
    groups = _factor_groups(features, outcomes, idio_cuts=idio_cuts)
    interaction_groups = _interaction_event_groups(features, outcomes, idio_cuts)
    interaction_candidates = _interaction_candidate_candidates(interaction_groups)
    selected_interaction = _select_interaction_candidate(interaction_candidates)
    family_payloads = {
        "excess-return-map.json": ("EXCESS_RETURN_MOMENTUM", "EXCESS_RETURN_REVERSAL"),
        "beta-residual-map.json": ("BETA_RESIDUAL_MOMENTUM", "BETA_RESIDUAL_REVERSAL"),
        "residual-drawdown-map.json": ("RESIDUAL_DRAWDOWN",),
        "idio-volatility-map.json": ("IDIOSYNCRATIC_VOLATILITY",),
        "abnormal-turnover-map.json": ("ABNORMAL_TURNOVER",),
        "residual-rank-map.json": ("RESIDUAL_RANK",),
        "range-position-map.json": ("RANGE_POSITION",),
    }
    for filename, families in family_payloads.items():
        payload = _factor_map_payload({key: value for key, value in groups.items() if key.split("|", 1)[0] in families})
        _write_json(output_root / filename, payload)

    leader = _select_discovery_leader(groups)
    replication_events = []
    if leader:
        replication_key = f"{leader['family']}|{leader['factor']}|{leader['state']}|TOUCHED_REPLICATION|5"
        replication_events = groups.get(replication_key, [])
    replication_execs = non_overlapping_events(replication_events)
    discovery_positive = bool(leader and (leader["metrics"].get("simple_excess_return_pct") or 0) > 0 and (leader["metrics"].get("beta_adjusted_residual_return_pct") or 0) > 0)
    replication_metrics = _metrics(replication_events, execution_events=replication_execs) if replication_events else _metrics([])
    replication_positive = bool(replication_events and (replication_metrics.get("simple_excess_return_pct") or 0) > 0 and (replication_metrics.get("beta_adjusted_residual_return_pct") or 0) > 0)
    period_consistency = bool(discovery_positive and replication_positive and (replication_metrics.get("stock_gross_return_pct") or 0) > 0 and (leader["metrics"].get("stock_gross_return_pct") or 0) > 0)
    chronology = _chronological_stability(leader, groups)
    leader_concentration = _concentration(leader["events"]) if leader else None
    gate = _candidate_gate(leader, replication_metrics, chronology, leader_concentration)
    gate_pass = bool(selected_interaction)
    overall_gate = {
        "pass": gate_pass,
        "selected_mechanism": "two_factor_interaction" if selected_interaction else None,
        "standalone_discovery_leader": gate,
        "selected_interaction_gate": selected_interaction["gate"] if selected_interaction else None,
        "eligible_interaction_count": sum(bool(item["gate"]["pass"]) for item in interaction_candidates),
    }
    candidate_status = "RESEARCH_CANDIDATE" if gate_pass else "NOT_CREATED"
    bootstrap_uncertainty = bool(
        selected_interaction
        and (
            selected_interaction["discovery_cluster_bootstrap"]["interval_pct_5_95"]["beta_adjusted_residual_return_pct"][0] <= 0
            or selected_interaction["touched_cluster_bootstrap"]["interval_pct_5_95"]["beta_adjusted_residual_return_pct"][0] <= 0
        )
    )
    mechanism = "WEAK" if bootstrap_uncertainty else "SUPPORTED" if gate_pass else "WEAK" if period_consistency else "NOT_SUPPORTED"

    top_promising = sorted(
        (item for key, events in groups.items()
         if key.endswith("|DISCOVERY|5")
         for item in [{"key": key, "events": events, "metrics": _metrics(events, execution_events=non_overlapping_events(events))}]
         if item["metrics"].get("beta_adjusted_residual_return_pct") is not None),
        key=lambda item: (item["metrics"]["beta_adjusted_residual_return_pct"], item["metrics"].get("simple_excess_return_pct") or -999),
        reverse=True,
    )[:10]
    bootstrap = []
    concentration = []
    for item in top_promising:
        family, factor, state, _, _ = item["key"].split("|", 4)
        execs = non_overlapping_events(item["events"])
        bootstrap.append({"family": family, "factor": factor, "state": state, "metrics": _mean_date_cluster_bootstrap(execs)})
        concentration.append({"family": family, "factor": factor, "state": state, "metrics": _concentration(item["events"])})

    _write_json(output_root / "phase13-source-audit.json", {
        "audit_date": datetime.now(KST).date().isoformat(), "sources": OFFICIAL_SOURCES,
        "third_party_sources": [],
        "conclusion": "Official sources provide useful current and delisting references, but this audit did not verify a complete machine-readable date-varying KRX membership and security-lineage panel.",
    })
    _write_json(output_root / "point-in-time-universe-feasibility.json", {
        "point_in_time_universe": "PARTIAL",
        "construction_status": "NOT_BUILT",
        "reason": "Listing dates, current market labels, and delisting references exist, but verified historical market-transfer/code-change/security-lineage coverage and archived as-of snapshots are insufficient to assign defensible daily membership.",
        "sources": ["kind_current_list", "kind_delisted_list", "krx_delisting_lookup", "krx_open_api"],
        "no_pit_cohort_substitution": True,
        "next_evidence_needed": "An approved, documented KRX/KIND time-series or archive with effective dates for listings, delistings, market transfers, mergers, corporate transformations, and code lineage, plus reproducible delisted-price history.",
    })
    _write_json(output_root / "phase13-cohort-manifest.json", {
        "cohort": "CURRENT_COHORT", "cohort_name": cohort_name, "symbols": len(symbols),
        "available_price_symbols": len(prices), "missing_price_symbols": sorted(set(symbols) - set(prices)),
        "market_counts_frozen": {market: list(markets.values()).count(market) for market in ("KOSPI", "KOSDAQ")},
        "cohort_sha256": cohort_sha, "current_listing_survivorship_bias": True,
        "membership_rule": "Frozen Phase 4 current-listing cohort applied historically; KOSPI/KOSDAQ classification is frozen and not reconstructed as-of each date.",
        "pit_cohort_mixed": False,
    })
    dataset_sha = _data_digest(prices, indexes)
    code_path = Path(__file__)
    config = {
        "periods": {"discovery": [d.isoformat() for d in DISCOVERY], "touched_replication": [d.isoformat() for d in TOUCHED_REPLICATION]},
        "safe_data_end": SAFE_DATA_END.isoformat(), "beta_window": BETA_WINDOW,
        "beta_min_pairs": BETA_MIN_PAIRS, "beta_extreme_range": list(EXTREME_BETA_RANGE),
        "horizons": list(HORIZONS), "cost_round_trip_pct": COST_PCT,
        "cost_multipliers": list(COST_MULTIPLIERS), "factor_buckets": FACTOR_BUCKETS,
        "two_factor_interactions": [list(definition) for definition in INTERACTION_DEFINITIONS],
        "candidate_gate": {
            "minimum_non_overlapping_per_period": 50, "minimum_signal_dates_per_period": 40,
            "simple_excess_positive_both": True, "beta_residual_positive_both": True,
            "absolute_gross_positive_both": True, "absolute_net_1x_positive_both": True,
            "payoff_ratio_gt": 1.0, "chronological_positive_blocks_minimum": 3,
            "top_three_symbol_share_max_pct": 50, "top_five_signal_date_share_max_pct": 50,
            "market_component_share_warning_max_pct": 80, "date_cluster_minimum_per_period": 40,
            "maximum_candidates": 1,
        },
        "momentum_reversal_state_rule": {
            "momentum": "positive return buckets only",
            "reversal": "negative return buckets only, including buckets ending at 0%",
            "zero_boundary": "0% belongs to the negative-to-zero reversal bucket by half-open intervals",
        },
        "idio_cuts_discovery_quantiles": list(idio_cuts) if idio_cuts else None,
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS, "bootstrap_seed": BOOTSTRAP_SEED,
        "entry": "T+1 open", "exit": "3/5/10-session close counting entry session",
        "market_context": "matched index return/volatility, frozen-cohort breadth, and one-day excess dispersion are diagnostics only",
    }
    config_sha = hashlib.sha256(_json_bytes(config)).hexdigest()
    _write_json(output_root / "phase13-dataset-manifest.json", {
        "source_git_sha": _source_sha(), "phase13_source_sha256": _sha256(code_path),
        "dataset_sha256": dataset_sha, "cohort_sha256": cohort_sha, "config_sha256": config_sha,
        "generated_at_kst": datetime.now(KST).isoformat(), "cohort": "CURRENT_COHORT",
        "provider": "KIS Open API local Phase 11 cache", "price_adjustment": ADJUSTMENT,
        "input_cache_sha256_by_symbol_or_index": cache_hashes,
        "warmup": [WARMUP_START.isoformat(), DISCOVERY[0].isoformat()],
        "primary_data_window": [DISCOVERY[0].isoformat(), TOUCHED_REPLICATION[1].isoformat()],
        "protection": {"confirmation": "NOT_READ", "external_2026": "NOT_READ", "holdout_2026": "NOT_READ"},
        "market_return_comparison": "stock simple return minus same-market KOSPI/KOSDAQ index simple return over same dates",
        "beta_specification": {"window_completed_pairs": BETA_WINDOW, "minimum_pairs": BETA_MIN_PAIRS, "includes_signal_close_return": True, "uses_only_through_T": True, "out_of_range_label": list(EXTREME_BETA_RANGE), "clips": False},
        "residual_specification": "forward stock gross return - beta_at_T * matched index gross return; descriptive residual only",
        "cost_assumptions": {"round_trip_pct": COST_PCT, "multipliers": list(COST_MULTIPLIERS), "fee_each_side_pct_assumed": 0.015, "sell_tax_equivalent_pct": 0.20, "slippage_each_side_pct": 0.15},
        "point_in_time_price_vintage": "KIS adjusted history lacks historical vintage selector; restatements may revise earlier rows.",
        "input_row_filter": f"Only sessions <= {SAFE_DATA_END.isoformat()} are passed into Phase 13 research functions.",
        "configuration": config,
    })
    _write_json(output_root / "phase13-factor-availability.json", _feature_availability(features, outcomes))
    _write_json(output_root / "market-context-diagnostics.json", _market_context_diagnostics(features))
    _write_json(output_root / "two-factor-interactions.json", _two_factor_interactions(features, outcomes, idio_cuts))
    _write_json(output_root / "discovery-replication-comparison.json", {
        "selection_priority": "Discovery beta residual then simple excess; broad states fixed before touched replication",
        "leader": ({k: v for k, v in leader.items() if k not in {"events", "executions"}} if leader else None),
        "touched_replication_metrics": replication_metrics,
        "same_factor_state": bool(leader and replication_events),
        "same_residual_direction": discovery_positive and replication_positive,
        "same_absolute_return_direction": bool(leader and (leader["metrics"].get("stock_gross_return_pct") or 0) > 0 and (replication_metrics.get("stock_gross_return_pct") or 0) > 0),
        "structural_consistency": period_consistency,
        "touched_replication_is_independent": False,
    })
    _write_json(output_root / "market-beta-share.json", {
        "definition": "matched-index average divided by stock gross average; descriptive decomposition, undefined when |stock gross| < 0.1%",
        "leader_discovery": leader["metrics"] if leader else None,
        "leader_touched_replication": replication_metrics if replication_events else None,
        "candidate_discovery": selected_interaction["discovery_metrics"] if selected_interaction else None,
        "candidate_touched_replication": selected_interaction["touched_replication_metrics"] if selected_interaction else None,
        "warning_threshold_pct": 80,
    })
    _write_json(output_root / "chronological-stability.json", chronology)
    _write_json(output_root / "market-split.json", _market_split(groups))
    _write_json(output_root / "concentration.json", {"promising_discovery_states": concentration})
    _write_json(output_root / "cluster-bootstrap.json", {"promising_discovery_states": bootstrap})
    _write_json(output_root / "survivorship-sensitivity.json", {
        "survivorship_sensitivity": "NOT_TESTABLE", "reason": "No defensible PIT cohort or verified delisted adjusted-price panel was available; a current-listing-only dataset cannot estimate excluded-symbol effects.",
        "current_listing_bias": "PRESENT", "pit_current_comparison_run": False,
    })
    _write_json(output_root / "phase13-hypothesis.json", {
        "phase13_factor_map": "PARTIAL" if len(prices) < len(symbols) else "PASS",
        "residual_mechanism": mechanism, "phase13_candidate": candidate_status,
        "periods": {"discovery": list(map(str, DISCOVERY)), "touched_replication": list(map(str, TOUCHED_REPLICATION))},
        "selection_gates": {
            "minimum_non_overlapping_per_period": 50, "minimum_signal_dates_per_period": 40,
            "simple_excess_positive_both": True, "beta_residual_positive_both": True,
            "absolute_gross_1pct_preferred": True, "absolute_net_1x_positive_both": True,
            "payoff_ratio_gt": 1.0, "periods_same_absolute_and_residual_direction": True,
            "chronological_consistency_blocks_minimum": 3,
            "top_three_symbol_contribution_share_max_pct": 50,
            "top_five_signal_date_contribution_share_max_pct": 50,
            "market_component_share_warning_gate_pct": 80,
            "candidate_maximum": 1,
        },
        "candidate_gate": overall_gate,
        "confirmation_access": (
            "NOT_RUN; preregistered candidate requires freeze commit and local/remote main verification"
            if gate_pass else "NOT_RUN; no qualifying candidate and no preregistration"
        ),
    })
    candidate = None
    if selected_interaction:
        interaction_name, left_factor, right_factor = next(
            definition for definition in INTERACTION_DEFINITIONS if definition[0] == selected_interaction["interaction"]
        )
        left_boundary = "<= -4%" if selected_interaction["left_state"] == "LE_−4" else selected_interaction["left_state"]
        right_boundary = ">= 1.5" if selected_interaction["right_state"] == "EXPANDED" else selected_interaction["right_state"]
        candidate = {
            "name": _candidate_name(
                selected_interaction["market"], left_factor, selected_interaction["left_state"],
                right_factor, selected_interaction["right_state"],
            ),
            "interaction": interaction_name,
            "universe": "CURRENT_COHORT; frozen 100 symbols with current-listing survivorship bias retained; KOSPI only using frozen cohort classification",
            "point_in_time_status": "PARTIAL; no PIT universe mixed into result",
            "market": selected_interaction["market"],
            "features": {
                left_factor: {"state": selected_interaction["left_state"], "boundary": left_boundary},
                right_factor: {"state": selected_interaction["right_state"], "boundary": right_boundary},
            },
            "beta_specification": {"window": BETA_WINDOW, "minimum_pairs": BETA_MIN_PAIRS, "known_through": "T close", "clip": False},
            "residual_formula": "forward stock gross return - beta_T * matched index gross return",
            "entry": "T+1 open", "holding_horizon_sessions": 5, "exit": "5th session close including entry session",
            "missing_data_rule": "exclude the corresponding observation; no fill",
            "non_overlap_rule": "one active execution per symbol; next entry must occur after prior exit",
            "cost_assumptions": {"round_trip_pct_1x": COST_PCT, "multipliers": list(COST_MULTIPLIERS)},
            "discovery_metrics": selected_interaction["discovery_metrics"],
            "touched_replication_metrics": selected_interaction["touched_replication_metrics"],
            "discovery_cluster_bootstrap": selected_interaction["discovery_cluster_bootstrap"],
            "touched_cluster_bootstrap": selected_interaction["touched_cluster_bootstrap"],
            "chronological_blocks": selected_interaction["chronological_blocks"],
            "concentration": {
                "discovery": selected_interaction["discovery_concentration"],
                "touched_replication": selected_interaction["touched_replication_concentration"],
            },
            "candidate_gate": selected_interaction["gate"],
            "mechanism_uncertainty": "Date-cluster 90% residual intervals cross zero in both periods; one-shot Confirmation is required.",
        }
    _write_json(output_root / "phase13-candidate-analysis.json", {
        "selection_priority": ["residual direction", "period consistency", "simple interpretation", "independent date breadth", "after-cost economics", "concentration"],
        "maximum_candidates": 1,
        "interaction_candidates": interaction_candidates,
        "selected_candidate": candidate,
    })
    _write_json(output_root / "phase13-preregistration.json", {
        "status": "PREREGISTERED" if candidate else "NOT_CREATED", "candidate": candidate,
        "reason": None if candidate else "No residual-first mechanism met the predeclared Discovery and Touched Replication candidate gate.",
        "confirmation_period": [d.isoformat() for d in CONFIRMATION],
        "source_git_sha": _source_sha(), "dataset_sha256": dataset_sha, "config_sha256": config_sha,
    })
    _write_json(output_root / "phase13-confirmation.json", {
        "confirmation": "NOT_RUN",
        "reason": "Candidate did not pass the pre-confirmation gates; protected outcomes were not accessed."
        if not gate_pass else "Candidate is preregistered; an exact pushed freeze is required before a separate one-shot Confirmation run.",
        "external_2026": "NOT_READ", "holdout_2026": "NOT_READ",
    })
    panel = output_root / "phase13-safe-factor-panel.jsonl.gz"
    _emit_panel(panel, features, outcomes)
    prior_after = _artifact_snapshot()
    prior_immutable = prior_before == prior_after
    _write_json(output_root / "phase13-prior-phase-artifact-snapshot.json", {
        "before": prior_before, "after": prior_after,
        "previous_phase_artifact_immutability": "PASS" if prior_immutable else "FAIL",
        "changed_paths": sorted(set(prior_before) ^ set(prior_after) | {k for k in prior_before.keys() & prior_after.keys() if prior_before[k] != prior_after[k]}),
    })
    _write_json(output_root / "phase13-summary.json", {
        "point_in_time_universe": "PARTIAL", "pit_price_history": "NOT_AVAILABLE",
        "survivorship_sensitivity": "NOT_TESTABLE",
        "phase13_factor_map": "PARTIAL" if len(prices) < len(symbols) else "PASS",
        "residual_mechanism": mechanism, "phase13_candidate": candidate_status,
        "confirmation": "NOT_RUN", "external_2026": "NOT_READ", "holdout_2026": "NOT_READ",
        "shadow_next_session": "NO", "alpha": "UNPROVEN", "live": "DISABLED",
        "cohort_symbols": len(symbols), "available_price_symbols": len(prices),
        "feature_rows": len(features), "outcome_rows": len(outcomes),
        "discovery_leader": ({k: v for k, v in leader.items() if k not in {"events", "executions"}} if leader else None),
        "touched_replication_metrics": replication_metrics,
        "candidate_gate": overall_gate,
        "previous_phase_artifact_immutability": "PASS" if prior_immutable else "FAIL",
        "source_git_sha": _source_sha(), "dataset_sha256": dataset_sha, "config_sha256": config_sha,
        "generated_at_kst": datetime.now(KST).isoformat(),
    })
    index = _index_artifacts(output_root)
    _write_json(output_root / "phase13-artifact-index.json", index)
    integrity = verify_artifacts(output_root, index)
    _write_json(output_root / "artifact-integrity.json", integrity)
    if not prior_immutable or integrity["status"] != "PASS":
        raise RuntimeError("Phase 13 artifact or previous-phase immutability verification failed")
    return json.loads((output_root / "phase13-summary.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Phase 13 safe research or its frozen one-shot Confirmation.")
    parser.add_argument("--confirmation", action="store_true", help="run the preregistered one-shot Confirmation")
    parser.add_argument("--expected-freeze-sha", help="required pushed local/remote main SHA for Confirmation")
    args = parser.parse_args()
    if args.confirmation:
        if not args.expected_freeze_sha:
            parser.error("--confirmation requires --expected-freeze-sha")
        result = run_phase13_confirmation(expected_freeze_sha=args.expected_freeze_sha)
    else:
        if args.expected_freeze_sha:
            parser.error("--expected-freeze-sha is only valid with --confirmation")
        result = run_phase13()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

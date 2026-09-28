from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from krx_trader.backtest.portfolio import run_portfolio_backtest
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.resample import resample_session_minutes
from krx_trader.models import Bar, Decision, Signal
from krx_trader.research.phase5_backfill import (
    LOCKED_HOLDOUT_START,
    assert_safe_research_date,
)
from krx_trader.research.relative_strength import (
    BASE_COST,
    compute_relative_strength_anatomy,
    simulate_relative_strength_signals,
)
from krx_trader.strategies.relative_strength import (
    DEFAULT_RS_CONFIG,
    CrossSectionalEngine,
    RelativeStrengthConfig,
    RelativeStrengthVariant,
    evaluate_relative_strength,
    preregistration_rs_config,
)

KST = ZoneInfo("Asia/Seoul")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_phase5_bars(
    cache_root: Path,
    cohort_symbols: list[str],
    sessions: list[date],
) -> tuple[dict[str, list[Bar]], dict[str, list[Bar]], dict[str, str], list[str]]:
    """Load cached minute bars and resample to 15m and 30m; fail closed on locked holdout."""
    for s in sessions:
        assert_safe_research_date(s)

    cache = ParquetBarCache(cache_root)
    bars_15m: dict[str, list[Bar]] = {}
    bars_30m: dict[str, list[Bar]] = {}
    partition_hashes: dict[str, str] = {}
    succeeded_symbols: list[str] = []

    for symbol in sorted(cohort_symbols):
        symbol_1m_bars: list[Bar] = []
        for session in sessions:
            if not cache.contains("minute", symbol, "1m", session):
                continue
            path = cache.partition_path("minute", symbol, "1m", session)
            meta_path = path.with_suffix(".metadata.json")
            if not path.is_file() or not meta_path.is_file():
                continue
            try:
                session_bars = cache.load("minute", symbol, "1m", session)
                symbol_1m_bars.extend(session_bars)
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                partition_hashes[f"{symbol}:{session.isoformat()}"] = str(meta.get("sha256", ""))
            except (OSError, ValueError, json.JSONDecodeError):
                continue

        if symbol_1m_bars:
            # Resample to 15m and 30m
            r15 = resample_session_minutes(symbol_1m_bars, 15)
            r30 = resample_session_minutes(symbol_1m_bars, 30)
            if r15:
                bars_15m[symbol] = r15
                bars_30m[symbol] = r30
                succeeded_symbols.append(symbol)

    return bars_15m, bars_30m, partition_hashes, succeeded_symbols


def run_phase5_study(
    *,
    output_dir: Path = Path("runtime/research/phase5"),
    cohort_manifest_path: Path = Path("runtime/research/phase4/cohort-manifest.json"),
    split_manifest_path: Path = Path("runtime/research/phase25-diagnostic-dataset-manifest-v1.json"),
    cache_root: Path = Path("data"),
    git_sha: str = "HEAD",
    config: RelativeStrengthConfig = DEFAULT_RS_CONFIG,
    target_cohort: str = "cohort_60",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cohort_data = json.loads(cohort_manifest_path.read_text(encoding="utf-8"))
    symbols = list(cohort_data[target_cohort]["symbols"])

    split_manifest = json.loads(split_manifest_path.read_text(encoding="utf-8"))
    splits = split_manifest.get("splits", {})
    dev_dates = sorted([date.fromisoformat(d) for d in splits.get("development", {}).get("sessions", [])])
    val_dates = sorted([date.fromisoformat(d) for d in splits.get("validation", {}).get("sessions", [])])
    all_used_dates = sorted(set(dev_dates + val_dates))

    for d in all_used_dates:
        assert_safe_research_date(d)

    # 1. Load bars
    bars_15m, bars_30m, partition_hashes, succeeded_symbols = load_phase5_bars(
        cache_root, symbols, all_used_dates
    )

    kospi_symbols = [s for s in succeeded_symbols if s in cohort_data["cohort_60"]["symbols"][:30]]
    kosdaq_symbols = [s for s in succeeded_symbols if s in cohort_data["cohort_60"]["symbols"][30:]]

    dq_manifest = {
        "schema_version": 1,
        "artifact": "phase5-expanded-dq",
        "git_sha": git_sha,
        "target_cohort": target_cohort,
        "total_cohort_symbols": len(symbols),
        "succeeded_symbols_count": len(succeeded_symbols),
        "kospi_symbols_count": len(kospi_symbols),
        "kosdaq_symbols_count": len(kosdaq_symbols),
        "total_partitions_loaded": len(partition_hashes),
        "holdout_integrity": {
            "locked_holdout_start": LOCKED_HOLDOUT_START.isoformat(),
            "state": "LOCKED_NOT_EVALUATED",
            "holdout_partitions_opened": 0,
        },
        "period": [all_used_dates[0].isoformat(), all_used_dates[-1].isoformat()],
    }
    (output_dir / "expanded-dq.json").write_text(
        json.dumps(dq_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # 2. Relative Strength Anatomy: 15m and 30m
    print("Running Relative Strength Anatomy (15m)...", flush=True)
    anatomy_15m = compute_relative_strength_anatomy(bars_15m, config=config, interval_name="15m")
    (output_dir / "rs-anatomy-15m.json").write_text(
        json.dumps(anatomy_15m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("Running Relative Strength Anatomy (30m)...", flush=True)
    anatomy_30m = compute_relative_strength_anatomy(bars_30m, config=config, interval_name="30m")
    (output_dir / "rs-anatomy-30m.json").write_text(
        json.dumps(anatomy_30m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # 3. Development Evaluation: RS-A and RS-B (15m primary)
    dev_bars_15m = {
        s: [b for b in bars if b.time.date() in dev_dates]
        for s, bars in bars_15m.items()
    }
    print("Evaluating RS-A on Development (15m)...", flush=True)
    rsa_dev = simulate_relative_strength_signals(
        dev_bars_15m, variant=RelativeStrengthVariant.RS_A, config=config
    )
    (output_dir / "rs-a-development-results.json").write_text(
        json.dumps(rsa_dev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("Evaluating RS-B on Development (15m)...", flush=True)
    rsb_dev = simulate_relative_strength_signals(
        dev_bars_15m, variant=RelativeStrengthVariant.RS_B, config=config
    )
    (output_dir / "rs-b-development-results.json").write_text(
        json.dumps(rsb_dev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # 4. Secondary Diagnostic Evaluation: RS-A and RS-B (15m)
    val_bars_15m = {
        s: [b for b in bars if b.time.date() in val_dates]
        for s, bars in bars_15m.items()
    }
    print("Evaluating RS-A on Secondary Diagnostic (15m)...", flush=True)
    rsa_val = simulate_relative_strength_signals(
        val_bars_15m, variant=RelativeStrengthVariant.RS_A, config=config
    )
    (output_dir / "rs-a-secondary-results.json").write_text(
        json.dumps(rsa_val, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("Evaluating RS-B on Secondary Diagnostic (15m)...", flush=True)
    rsb_val = simulate_relative_strength_signals(
        val_bars_15m, variant=RelativeStrengthVariant.RS_B, config=config
    )
    (output_dir / "rs-b-secondary-results.json").write_text(
        json.dumps(rsb_val, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Cost Stress tests (1.0x, 1.5x, 2.0x) on Development
    stress_results: dict[str, Any] = {}
    for mult in [1.0, 1.5, 2.0]:
        stress_results[f"RS-A_{mult}x"] = simulate_relative_strength_signals(
            dev_bars_15m, variant=RelativeStrengthVariant.RS_A, config=config, stress_multiplier=mult
        )
        stress_results[f"RS-B_{mult}x"] = simulate_relative_strength_signals(
            dev_bars_15m, variant=RelativeStrengthVariant.RS_B, config=config, stress_multiplier=mult
        )
    (output_dir / "cost-stress-results.json").write_text(
        json.dumps(
            {k: {m: v[m] for m in ["variant", "trades", "gross_expectancy", "net_expectancy", "profit_factor", "stress_multiplier"]}
             for k, v in stress_results.items()},
            indent=2, ensure_ascii=False
        ) + "\n",
        encoding="utf-8",
    )

    # 5. 100K Portfolio Feasibility Replay
    print("Running 100K portfolio feasibility simulation...", flush=True)
    class PortfolioRSAdapter:
        def __init__(self, cfg: RelativeStrengthConfig, var: RelativeStrengthVariant):
            self.cfg = cfg
            self.var = var
            self.engine = CrossSectionalEngine(cfg)
            self.current_time_histories: dict[str, list[Bar]] = {}

        def __call__(self, symbol: str, history: list[Bar]) -> Signal:
            if not history:
                return Signal(
                    timestamp=datetime.now(KST), symbol=symbol, strategy_id="rs_a",
                    decision=Decision.HOLD, reason_codes=("NO_BARS",)
                )
            t = history[-1].time
            self.current_time_histories[symbol] = history
            obs_dict = self.engine.evaluate_timestamp(t, self.current_time_histories)
            obs = obs_dict.get(symbol)
            return evaluate_relative_strength(
                obs, history, symbol, variant=self.var, config=self.cfg
            )

    adapter = PortfolioRSAdapter(config, RelativeStrengthVariant.RS_A)
    portfolio_res = run_portfolio_backtest(
        dev_bars_15m,
        adapter,
        starting_cash_krw=100_000,
        capital_cap_krw=100_000,
        order_cap_krw=20_000,
        risk_per_trade_pct=0.25,
        max_concurrent_positions=2,
        cost_model=BASE_COST,
    )
    from krx_trader.backtest.metrics import calculate_metrics
    pm = calculate_metrics(portfolio_res)
    total_net_pnl_krw = sum(t.net_pnl_krw for t in portfolio_res.trades)
    portfolio_summary = {
        "capital_krw": 100_000,
        "order_cap_krw": 20_000,
        "risk_pct": 0.25,
        "max_positions": 2,
        "trades": len(portfolio_res.trades),
        "total_net_pnl_krw": total_net_pnl_krw,
        "total_return_pct": pm.total_return_pct,
        "win_rate_pct": pm.win_rate_pct,
        "profit_factor": pm.profit_factor,
        "max_drawdown_pct": pm.max_drawdown_pct,
        "fills": len(portfolio_res.trades),
    }
    (output_dir / "100k-feasibility.json").write_text(
        json.dumps(portfolio_summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # 6. Generate exact Preregistration Manifest
    prereg_manifest = {
        "schema_version": 1,
        "artifact": "phase5-relative-strength-preregistration",
        "status": "FROZEN_BEFORE_NEW_EVIDENCE",
        "freeze_commit_sha": git_sha,
        "strategy_family": "RELATIVE-STRENGTH CONTINUATION",
        "strategy_rules": {
            "RS-A": preregistration_rs_config(RelativeStrengthVariant.RS_A, config),
            "RS-B": preregistration_rs_config(RelativeStrengthVariant.RS_B, config),
        },
        "interval_primary": "15m",
        "interval_secondary": "30m",
        "used_design_periods": {
            "development": [dev_dates[0].isoformat(), dev_dates[-1].isoformat()],
            "secondary_diagnostic": [val_dates[0].isoformat(), val_dates[-1].isoformat()],
        },
        "new_external_block": {
            "start_date_inclusive": "2026-01-05",
            "end_date_inclusive": "2026-04-16",
            "target_cohort": target_cohort,
            "status": "UNTOUCHED_PENDING_EXTERNAL_VALIDATION",
        },
        "locked_holdout": {
            "start_date": LOCKED_HOLDOUT_START.isoformat(),
            "state": "LOCKED_NOT_EVALUATED",
            "access_rule": "FORBIDDEN_FAIL_CLOSED",
        },
    }
    (output_dir / "rs-preregistration.json").write_text(
        json.dumps(prereg_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    summary = {
        "schema_version": 1,
        "artifact": "phase5-study-summary",
        "git_sha": git_sha,
        "target_cohort": target_cohort,
        "succeeded_symbols": len(succeeded_symbols),
        "total_partitions": len(partition_hashes),
        "development_results": {
            "RS-A": {
                "trades": rsa_dev["trades"],
                "win_rate": rsa_dev["win_rate"],
                "gross_expectancy": rsa_dev["gross_expectancy"],
                "net_expectancy": rsa_dev["net_expectancy"],
                "profit_factor": rsa_dev["profit_factor"],
            },
            "RS-B": {
                "trades": rsb_dev["trades"],
                "win_rate": rsb_dev["win_rate"],
                "gross_expectancy": rsb_dev["gross_expectancy"],
                "net_expectancy": rsb_dev["net_expectancy"],
                "profit_factor": rsb_dev["profit_factor"],
            },
        },
        "secondary_diagnostic_results": {
            "RS-A": {
                "trades": rsa_val["trades"],
                "win_rate": rsa_val["win_rate"],
                "gross_expectancy": rsa_val["gross_expectancy"],
                "net_expectancy": rsa_val["net_expectancy"],
                "profit_factor": rsa_val["profit_factor"],
            },
            "RS-B": {
                "trades": rsb_val["trades"],
                "win_rate": rsb_val["win_rate"],
                "gross_expectancy": rsb_val["gross_expectancy"],
                "net_expectancy": rsb_val["net_expectancy"],
                "profit_factor": rsb_val["profit_factor"],
            },
        },
        "100k_portfolio": portfolio_summary,
    }
    (output_dir / "phase5-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 5 Relative Strength Study")
    parser.add_argument("--cohort", default="cohort_60", choices=["cohort_60", "cohort_100"])
    parser.add_argument("--git-sha", default="HEAD")
    args = parser.parse_args()
    res = run_phase5_study(target_cohort=args.cohort, git_sha=args.git_sha)
    print("Phase 5 Study Run Complete:")
    print(json.dumps(res, indent=2, ensure_ascii=False))

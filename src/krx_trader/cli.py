from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.engine import run_backtest
from krx_trader.backtest.metrics import calculate_metrics
from krx_trader.config import Settings
from krx_trader.data.cache import ParquetBarCache
from krx_trader.data.historical import load_bars_csv
from krx_trader.data.research_set import build_research_set
from krx_trader.execution.realtime_shadow import RealtimeShadowRunner, scheduled_market_window
from krx_trader.execution.shadow import run_shadow_replay
from krx_trader.execution.shadow_verify import verify_shadow_run
from krx_trader.kis.auth import KisAuthError, TokenManager
from krx_trader.kis.rest import KisApiError, KisRestClient
from krx_trader.kis.transport import UrllibTransport
from krx_trader.market.regime import Regime, classify_regime
from krx_trader.models import Bar
from krx_trader.research.anatomy import (
    run_breakout_anatomy,
    run_breakout_v2_comparison,
    run_breakout_v2_validation,
)
from krx_trader.research.runner import run_baseline, run_feasibility_matrix, run_validation
from krx_trader.research.scenario import ResearchScenario
from krx_trader.status import write_project_status
from krx_trader.storage.sqlite_store import SQLiteStore
from krx_trader.strategies.breakout import evaluate_breakout
from krx_trader.strategies.pullback import evaluate_pullback
from krx_trader.universe.master import refresh_stock_master
from krx_trader.universe.scanner import rank_market_candidates, scan_market


def _resolve_regime(
    stock_bars: list[Bar],
    kospi_bars: list[Bar] | None,
    kosdaq_bars: list[Bar] | None,
    settings: Settings,
    regime_filter: str,
) -> Regime | None:
    if regime_filter == "off":
        return Regime.NEUTRAL
    if regime_filter != "on" or not stock_bars or kospi_bars is None or kosdaq_bars is None:
        return None
    market_date = stock_bars[-1].time.astimezone(ZoneInfo("Asia/Seoul")).date()

    def previous_sessions(bars: list[Bar]) -> list[Bar]:
        return [bar for bar in bars if bar.time.astimezone(ZoneInfo("Asia/Seoul")).date() < market_date]

    kospi = classify_regime(
        previous_sessions(kospi_bars), trend_lookback=settings.regime_trend_lookback,
        volatility_lookback=settings.regime_volatility_lookback,
        high_vol_threshold=settings.regime_high_vol_threshold,
    )
    kosdaq = classify_regime(
        previous_sessions(kosdaq_bars), trend_lookback=settings.regime_trend_lookback,
        volatility_lookback=settings.regime_volatility_lookback,
        high_vol_threshold=settings.regime_high_vol_threshold,
    )
    if kospi is None or kosdaq is None:
        return None
    if Regime.HIGH_VOL in {kospi, kosdaq}:
        return Regime.HIGH_VOL
    if Regime.DOWN in {kospi, kosdaq}:
        return Regime.DOWN
    if kospi == kosdaq == Regime.UP:
        return Regime.UP
    return Regime.NEUTRAL


def _strategy_fn(
    strategy: str,
    symbol: str,
    settings: Settings,
    kospi_bars: list[Bar] | None,
    kosdaq_bars: list[Bar] | None,
    regime_filter: str,
):
    def evaluate(bars):
        regime = _resolve_regime(bars, kospi_bars, kosdaq_bars, settings, regime_filter)
        if strategy == "breakout":
            return evaluate_breakout(bars, symbol, regime=regime)
        return evaluate_pullback(bars, symbol, regime=regime)

    return evaluate


def _cost_model(settings: Settings) -> CostModel:
    return CostModel(settings.broker_fee_rate, settings.sell_tax_rate, settings.slippage_bps)


def _kis_client(settings: Settings) -> KisRestClient:
    if settings.trading_mode != "shadow" or settings.live_trading_enabled:
        raise ValueError("read-only market commands require TRADING_MODE=shadow and LIVE_TRADING_ENABLED=false")
    transport = UrllibTransport()
    manager = TokenManager(settings.kis_app_key, settings.kis_app_secret, transport)
    return KisRestClient(settings.kis_app_key, settings.kis_app_secret, manager, transport)


def _parse_iso_date(value: str):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _emit_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def _data_command(args, settings: Settings) -> int:
    client = _kis_client(settings)
    cache = ParquetBarCache()
    if args.data_command == "build-research-set":
        summary = build_research_set(
            client,
            symbols=args.symbols,
            start=args.start,
            end=args.end,
            max_sessions=args.max_sessions,
            cache=cache,
            progress=lambda message: print(message, file=sys.stderr),
        )
        _emit_json(summary)
        return 0 if summary["symbols_succeeded"] else 1
    if args.data_command == "quote":
        quote = client.get_quote(args.symbol)
        _emit_json({
            "symbol": quote.symbol, "timestamp": quote.observed_at.isoformat(), "price": quote.price,
            "open": quote.open, "high": quote.high, "low": quote.low, "volume": quote.volume,
            "turnover_krw": quote.turnover_krw,
        })
        return 0
    if args.data_command == "daily":
        bars = client.get_daily_bars(args.symbol, args.start, args.end)
        if bars:
            path, provenance = cache.save(
                bars, kind="daily", symbol=args.symbol, interval="1d", market="KRX",
                source="KIS daily OHLCV",
            )
        else:
            path, provenance = None, None
        _emit_json({"symbol": args.symbol, "rows": len(bars), "first": bars[0].time.isoformat() if bars else None,
                    "last": bars[-1].time.isoformat() if bars else None, "cache_path": str(path) if path else None,
                    "provenance": provenance})
        return 0 if bars else 1
    if args.data_command == "minute":
        bars = client.get_minute_bars(args.symbol, args.date)
        path, provenance = cache.save(
            bars, kind="minute", symbol=args.symbol, interval="1m", market="KRX",
            source="KIS regular-session minute OHLCV", session_date=args.date,
        )
        _emit_json({"symbol": args.symbol, "session_date": args.date.isoformat(),
                    "timestamp_convention": "bar_start", "rows": len(bars),
                    "first": bars[0].time.isoformat() if bars else None,
                    "last": bars[-1].time.isoformat() if bars else None,
                    "cache_path": str(path), "provenance": provenance})
        return 0
    if args.data_command == "index":
        codes = {"kospi": "0001", "kosdaq": "1001"}
        code = codes[args.market]
        bars = client.get_index_bars(code, args.start, args.end)
        if bars:
            path, provenance = cache.save(
                bars, kind="indexes", symbol=args.market, interval="1d", market=args.market.upper(),
                source="KIS index daily OHLCV",
            )
        else:
            path, provenance = None, None
        _emit_json({"market": args.market, "index_code": code, "rows": len(bars),
                    "first": bars[0].time.isoformat() if bars else None,
                    "last": bars[-1].time.isoformat() if bars else None,
                    "cache_path": str(path) if path else None, "provenance": provenance})
        return 0 if bars else 1
    return 2


def _universe_command(args) -> int:
    metadata = refresh_stock_master()
    _emit_json(metadata)
    return 0


def _scan_command(args, settings: Settings) -> int:
    client = _kis_client(settings)
    if args.scan_type == "market":
        contexts, excluded = scan_market(client, settings, top_n=args.top)
        rows = [{
            "rank": rank, "symbol": row.stock.symbol, "name": row.stock.name, "market": row.stock.market,
            "price": row.activity.price, "turnover_krw": row.activity.turnover_krw,
            "volume": row.activity.volume, "score": None, "reason_codes": ["RANKING_SHORTLIST"],
        } for rank, row in enumerate(contexts, start=1)]
    else:
        rows, failures, excluded = rank_market_candidates(
            client, settings, strategy=args.scan_type, top_n=min(args.top, 20), market_limit=50,
            cache=ParquetBarCache(),
        )
        rows = [{
            "rank": row.rank, "symbol": row.stock.symbol, "name": row.stock.name, "market": row.stock.market,
            "price": row.activity.price, "turnover_krw": row.activity.turnover_krw,
            "volume": row.activity.volume, "score": row.score, "reason_codes": list(row.reason_codes),
        } for row in rows]
        _emit_json({"strategy": args.scan_type, "status": "PASS" if rows else "NO_CANDIDATES",
                    "candidates": rows, "excluded_for_eligibility": excluded, "data_failures": failures})
        return 0
    _emit_json({"scan": "market", "status": "PASS" if rows else "NO_CANDIDATES",
                "candidates": rows, "excluded_for_eligibility": excluded})
    return 0


def _shadow_live(args, settings: Settings) -> int:
    if settings.trading_mode != "shadow" or settings.live_trading_enabled:
        print("Blocked: realtime Shadow requires shadow mode and LIVE_TRADING_ENABLED=false", file=sys.stderr)
        return 2
    scenario = None
    if args.research_scenario is not None:
        try:
            payload = json.loads(args.research_scenario.read_text(encoding="utf-8"))
            scenario = ResearchScenario(
                name=str(payload["name"]),
                capital_krw=int(payload["capital_krw"]),
                order_cap_krw=int(payload["order_cap_krw"]),
                risk_per_trade_pct=float(payload["risk_per_trade_pct"]),
                regime_mode=str(payload.get("regime_mode", "on")),
                max_positions=int(payload.get("max_positions", settings.max_concurrent_positions)),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            print(f"Invalid frozen research scenario: {type(exc).__name__}", file=sys.stderr)
            return 2
    start, stop = scheduled_market_window(datetime.now(ZoneInfo("Asia/Seoul")).date(), args.start, args.stop)
    run_id = args.run_id or datetime.now(ZoneInfo("Asia/Seoul")).strftime("shadow-%Y%m%dT%H%M%S%z")
    try:
        runner = RealtimeShadowRunner(
            settings,
            _kis_client(settings),
            run_id=run_id,
            scheduled_start=start,
            stop_at=stop,
            research_scenario=scenario,
            poll_seconds=args.poll_seconds,
            monitor_top=args.monitor_top,
        )
        report = runner.run()
    except (KisApiError, OSError, RuntimeError, ValueError) as exc:
        print(f"Realtime Shadow failed: {type(exc).__name__}; details redacted", file=sys.stderr)
        return 1
    _emit_json({
        "run_id": report["run_id"],
        "status": report["status"],
        "started_at": report.get("started_at"),
        "stopped_at": report.get("stopped_at"),
        "market_status": report.get("market_status", "NOT_CONFIRMED"),
        "poll_cycles": report.get("poll_cycles", 0),
        "scanner_cycles": report.get("scanner_cycles", 0),
        "completed_minute_bars": report.get("completed_minute_bars", 0),
        "strategy_decisions": report.get("strategy_decisions", 0),
        "api_errors": report.get("api_errors", 0),
        "order_api_calls": 0,
        "manifest": str(runner.manifest_path),
    })
    return 0 if report["status"] == "PASS" else 1


def _doctor(settings: Settings) -> int:
    checks: list[tuple[str, bool]] = [("Python >= 3.11", sys.version_info >= (3, 11))]
    checks.extend((name, present) for name, present in settings.credential_status().items())
    checks.extend(
        [
            ("TRADING_MODE", settings.trading_mode in {"shadow", "live"}),
            ("LIVE_TRADING_ENABLED", not settings.live_trading_enabled or settings.trading_mode == "live"),
            ("Capital cap <= 100000 KRW", settings.max_live_capital_krw <= 100_000),
            ("Asia/Seoul timezone", ZoneInfo("Asia/Seoul").key == "Asia/Seoul"),
        ]
    )
    try:
        version = importlib.metadata.version("websockets")
        checks.append((f"websockets {version}", True))
    except importlib.metadata.PackageNotFoundError:
        checks.append(("websockets dependency", False))
    try:
        branch = subprocess.run(["git", "branch", "--show-current"], check=True, capture_output=True, text=True).stdout.strip()
        checks.append((f"Git branch main ({branch or 'detached'})", branch == "main"))
        origin = subprocess.run(["git", "remote", "get-url", "origin"], check=True, capture_output=True, text=True).stdout.strip()
        checks.append(("origin URL", origin == "https://github.com/JongHyun070105/krx-short-term-trader.git"))
        ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], check=False).returncode == 0
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"], capture_output=True, check=False
        ).returncode == 0
        checks.append((".env ignored and untracked", ignored and not tracked))
    except (OSError, subprocess.CalledProcessError):
        checks.append(("Git branch main", False))
    try:
        runtime = Path("runtime")
        runtime.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".doctor-", dir=runtime):
            pass
        checks.append(("Runtime storage writable", True))
    except OSError:
        checks.append(("Runtime storage writable", False))
    for name, passed in checks:
        print(f"{name:<32} {'PASS' if passed else 'FAIL'}")
    print(f"TRADING_MODE{'':<20} {settings.trading_mode}")
    print(f"LIVE_TRADING_ENABLED{'':<12} {str(settings.live_trading_enabled).lower()}")
    print("KIS REST connectivity          NOT RUN (doctor is local-only)")
    print("KIS account verification       NOT RUN")
    print("Token cache                    " + ("PRESENT" if Path("runtime/kis_token.json").is_file() else "ABSENT"))
    return 0 if all(passed for _, passed in checks) else 1


def _live_preflight(settings: Settings, confirm_live: bool) -> int:
    reasons = [
        "KIS account read-only verification not run",
        "ledger reconciliation evidence unavailable",
        "OOS and shadow evidence not qualified",
        "live order adapter unavailable in this release",
    ]
    if settings.trading_mode != "live":
        reasons.append("TRADING_MODE is not live")
    if not settings.live_trading_enabled:
        reasons.append("LIVE_TRADING_ENABLED is false")
    if not confirm_live:
        reasons.append("--confirm-live not supplied")
    print("LIVE_READY=false")
    for reason in reasons:
        print(f"BLOCK: {reason}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="krx-trader")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("status")
    auth = sub.add_parser("auth-test", help="request/reuse a KIS REST token; no account/order API")
    auth.add_argument("--read-only", action="store_true", help="required acknowledgement for auth-only request")
    universe = sub.add_parser("universe", help="refresh the official KIS KOSPI/KOSDAQ stock master")
    universe_sub = universe.add_subparsers(dest="universe_command", required=True)
    universe_sub.add_parser("refresh")
    data = sub.add_parser("data", help="read-only KIS market data and Parquet cache")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    quote = data_sub.add_parser("quote")
    quote.add_argument("symbol")
    daily = data_sub.add_parser("daily")
    daily.add_argument("symbol")
    daily.add_argument("--start", type=_parse_iso_date, required=True)
    daily.add_argument("--end", type=_parse_iso_date, required=True)
    minute = data_sub.add_parser("minute")
    minute.add_argument("symbol")
    minute.add_argument("--date", type=_parse_iso_date, required=True)
    index = data_sub.add_parser("index")
    index.add_argument("market", choices=("kospi", "kosdaq"))
    index.add_argument("--start", type=_parse_iso_date, required=True)
    index.add_argument("--end", type=_parse_iso_date, required=True)
    research_set = data_sub.add_parser("build-research-set", help="resume KIS daily/minute Parquet collection")
    research_set.add_argument("--symbols", nargs="+", required=True)
    research_set.add_argument("--start", type=_parse_iso_date, required=True)
    research_set.add_argument("--end", type=_parse_iso_date, required=True)
    research_set.add_argument("--max-sessions", type=int, default=5)
    scan = sub.add_parser("scan", help="cheap KIS shortlist and separate strategy rank")
    scan.add_argument("scan_type", choices=("market", "breakout", "pullback"))
    scan.add_argument("--top", type=int, default=50)
    research = sub.add_parser("research", help="offline cached-data portfolio research")
    research_sub = research.add_subparsers(dest="research_command", required=True)
    for strategy in ("breakout", "pullback"):
        strategy_research = research_sub.add_parser(strategy)
        strategy_research.add_argument("--interval", choices=("15m", "30m"), required=True)
        strategy_research.add_argument("--manifest", type=Path, default=Path("runtime/research/latest_dataset.json"))
        strategy_research.add_argument("--segment", choices=("development", "validation"), default="development")
    research_validate = research_sub.add_parser("validate", help="run four baselines, OOS, stress, scanner and regime comparisons")
    research_validate.add_argument("--top", type=int, default=10)
    feasibility = research_sub.add_parser("feasibility-matrix", help="compare fixed 100K execution constraints offline")
    feasibility.add_argument("--top", type=int, default=10)
    feasibility.add_argument("--start", type=_parse_iso_date)
    feasibility.add_argument("--end", type=_parse_iso_date)
    feasibility.add_argument("--manifest", type=Path, default=Path("runtime/research/latest_dataset.json"))
    anatomy = research_sub.add_parser("anatomy", help="build point-in-time Breakout signal anatomy")
    anatomy_sub = anatomy.add_subparsers(dest="anatomy_strategy", required=True)
    anatomy_breakout = anatomy_sub.add_parser("breakout")
    anatomy_breakout.add_argument("--interval", choices=("15m", "30m"), required=True)
    anatomy_breakout.add_argument("--manifest", type=Path, default=Path(
        "runtime/research/phase25-diagnostic-dataset-manifest-v1.json"
    ))
    anatomy_breakout.add_argument("--cache-root", type=Path, default=Path("data"))
    anatomy_breakout.add_argument("--output", type=Path, default=Path("runtime/research/phase3"))
    breakout_v2 = research_sub.add_parser("breakout-v2", help="compare bounded evidence-based Breakout hypotheses")
    breakout_v2_sub = breakout_v2.add_subparsers(dest="breakout_v2_command", required=True)
    breakout_v2_compare = breakout_v2_sub.add_parser("compare")
    breakout_v2_compare.add_argument("--interval", choices=("15m", "30m"), required=True)
    breakout_v2_compare.add_argument("--output", type=Path, default=Path("runtime/research/phase3"))
    breakout_v2_validate = breakout_v2_sub.add_parser("validate")
    breakout_v2_validate.add_argument("--output", type=Path, default=Path("runtime/research/phase3"))
    validate = sub.add_parser("validate", help="alias for research validate")
    validate.add_argument("--top", type=int, default=10)
    backtest = sub.add_parser("backtest")
    backtest.add_argument("strategy", choices=("breakout", "pullback"))
    backtest.add_argument("--input", type=Path, required=True)
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument("--regime-filter", choices=("on", "off"), default="on")
    backtest.add_argument("--kospi-index-input", type=Path)
    backtest.add_argument("--kosdaq-index-input", type=Path)
    backtest.add_argument("--stress-multipliers", type=float, nargs="+", default=[1.0, 1.5, 2.0])
    shadow = sub.add_parser("shadow-replay", help="replay a supplied OHLCV CSV; performs no KIS calls")
    shadow.add_argument("strategy", choices=("breakout", "pullback"))
    shadow.add_argument("--input", type=Path, required=True)
    shadow.add_argument("--symbol", required=True)
    shadow.add_argument("--run-id", default=None)
    shadow.add_argument("--regime-filter", choices=("on", "off"), default="on")
    shadow.add_argument("--kospi-index-input", type=Path)
    shadow.add_argument("--kosdaq-index-input", type=Path)
    realtime_shadow = sub.add_parser(
        "shadow-live", help="collect live KIS market data and emit only simulated Shadow orders"
    )
    realtime_shadow.add_argument("--run-id")
    realtime_shadow.add_argument("--start", default="09:00")
    realtime_shadow.add_argument("--stop", default="13:00")
    realtime_shadow.add_argument("--poll-seconds", type=int, default=60)
    realtime_shadow.add_argument("--monitor-top", type=int, default=5)
    realtime_shadow.add_argument("--research-scenario", type=Path)
    shadow_verify = sub.add_parser("shadow-verify", help="replay captured Shadow decisions without network access")
    shadow_verify.add_argument("--run-dir", type=Path, required=True)
    live = sub.add_parser("live-preflight")
    live.add_argument("--confirm-live", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "shadow-verify":
        try:
            report = verify_shadow_run(args.run_dir)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f"Shadow replay verification failed: {type(exc).__name__}; details redacted", file=sys.stderr)
            raise SystemExit(1) from None
        report_path = args.run_dir / "parity.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        report["report_path"] = str(report_path)
        _emit_json(report)
        return
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    if args.command == "doctor":
        raise SystemExit(_doctor(settings))
    if args.command == "status":
        print(json.dumps(write_project_status(), indent=2, sort_keys=True))
        return
    if args.command == "auth-test":
        if not args.read_only:
            print("Blocked: pass --read-only to acknowledge token-only KIS access", file=sys.stderr)
            raise SystemExit(2)
        manager = TokenManager(settings.kis_app_key, settings.kis_app_secret, UrllibTransport())
        try:
            manager.get_token()
        except (KisAuthError, OSError) as exc:
            print(f"KIS auth failed: {type(exc).__name__}; details redacted", file=sys.stderr)
            raise SystemExit(1) from None
        print("KIS REST token available; token value redacted")
        return
    if args.command == "universe":
        raise SystemExit(_universe_command(args))
    if args.command == "data":
        try:
            raise SystemExit(_data_command(args, settings))
        except (KisAuthError, KisApiError, OSError, ValueError) as exc:
            print(f"KIS data request failed: {type(exc).__name__}; details redacted", file=sys.stderr)
            raise SystemExit(1) from None
    if args.command == "scan":
        try:
            raise SystemExit(_scan_command(args, settings))
        except (KisAuthError, KisApiError, OSError, ValueError) as exc:
            print(f"KIS scan failed: {type(exc).__name__}; details redacted", file=sys.stderr)
            raise SystemExit(1) from None
    if args.command == "shadow-live":
        raise SystemExit(_shadow_live(args, settings))
    if args.command == "research":
        try:
            if args.research_command == "validate":
                _emit_json(run_validation(settings, top_n=args.top))
            elif args.research_command == "feasibility-matrix":
                _emit_json(run_feasibility_matrix(
                    settings, top_n=args.top,
                    start_date=args.start, end_date=args.end,
                    dataset_manifest=args.manifest,
                ))
            elif args.research_command == "anatomy":
                try:
                    git_sha = subprocess.run(
                        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
                    ).stdout.strip()
                except (OSError, subprocess.CalledProcessError):
                    git_sha = "UNAVAILABLE"
                _emit_json(run_breakout_anatomy(
                    interval=args.interval,
                    manifest_path=args.manifest,
                    cache_root=args.cache_root,
                    report_root=args.output,
                    git_sha=git_sha,
                ))
            elif args.research_command == "breakout-v2":
                if args.breakout_v2_command == "compare":
                    _emit_json(run_breakout_v2_comparison(
                        interval=args.interval, report_root=args.output,
                    ))
                else:
                    _emit_json(run_breakout_v2_validation(report_root=args.output))
            else:
                _emit_json(run_baseline(
                    settings,
                    strategy=args.research_command,
                    interval=args.interval,
                    dataset_manifest=args.manifest,
                    segment=args.segment,
                ))
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f"Offline research failed: {type(exc).__name__}; details redacted", file=sys.stderr)
            raise SystemExit(1) from None
        return
    if args.command == "validate":
        try:
            _emit_json(run_validation(settings, top_n=args.top))
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f"Validation failed: {type(exc).__name__}; details redacted", file=sys.stderr)
            raise SystemExit(1) from None
        return
    if args.command == "backtest":
        if args.regime_filter == "on" and (args.kospi_index_input is None or args.kosdaq_index_input is None):
            print("Blocked: regime filter requires both --kospi-index-input and --kosdaq-index-input", file=sys.stderr)
            raise SystemExit(2)
        bars = load_bars_csv(args.input)
        kospi_bars = load_bars_csv(args.kospi_index_input) if args.regime_filter == "on" else None
        kosdaq_bars = load_bars_csv(args.kosdaq_index_input) if args.regime_filter == "on" else None
        output = {
            "scope": "EXPLORATORY_FULL_SERIES",
            "regime_filter": args.regime_filter,
            "regime_inputs": {
                "kospi": args.kospi_index_input.name if kospi_bars is not None else None,
                "kosdaq": args.kosdaq_index_input.name if kosdaq_bars is not None else None,
            },
            "results": [],
        }
        for multiplier in args.stress_multipliers:
            result = run_backtest(
                bars,
                _strategy_fn(args.strategy, args.symbol, settings, kospi_bars, kosdaq_bars, args.regime_filter),
                symbol=args.symbol,
                starting_cash_krw=settings.max_live_capital_krw,
                capital_cap_krw=settings.max_live_capital_krw,
                order_cap_krw=settings.max_order_notional_krw,
                risk_per_trade_pct=settings.risk_per_trade_pct,
                min_price_krw=settings.min_price_krw,
                max_price_krw=settings.max_price_krw,
                cost_model=_cost_model(settings),
                stress_multiplier=multiplier,
            )
            metrics = calculate_metrics(result)
            metrics_data = {field: getattr(metrics, field) for field in metrics.__dataclass_fields__}
            if isinstance(metrics_data.get("profit_factor"), float) and math.isinf(metrics_data["profit_factor"]):
                metrics_data["profit_factor"] = "INF"
            output["results"].append({
                "cost_multiplier": multiplier,
                "metrics": metrics_data,
                "trades": len(result.trades),
                "open_position": result.open_position,
            })
        print(json.dumps(output, indent=2, allow_nan=False))
        return
    if args.command == "shadow-replay":
        if args.regime_filter == "on" and (args.kospi_index_input is None or args.kosdaq_index_input is None):
            print("Blocked: regime filter requires both --kospi-index-input and --kosdaq-index-input", file=sys.stderr)
            raise SystemExit(2)
        bars = load_bars_csv(args.input)
        kospi_bars = load_bars_csv(args.kospi_index_input) if args.regime_filter == "on" else None
        kosdaq_bars = load_bars_csv(args.kosdaq_index_input) if args.regime_filter == "on" else None
        run_id = args.run_id or datetime.now(ZoneInfo("Asia/Seoul")).strftime("replay-%Y%m%dT%H%M%S%z")
        result = run_shadow_replay(
            bars,
            _strategy_fn(args.strategy, args.symbol, settings, kospi_bars, kosdaq_bars, args.regime_filter),
            symbol=args.symbol,
            run_id=run_id,
            store=SQLiteStore(),
            cost_model=_cost_model(settings),
        )
        print(json.dumps({
            "run_id": result.run_id,
            "decisions": len(result.result.decisions),
            "closed_trades": len(result.result.trades),
            "regime_filter": args.regime_filter,
            "regime_inputs": {
                "kospi": args.kospi_index_input.name if kospi_bars is not None else None,
                "kosdaq": args.kosdaq_index_input.name if kosdaq_bars is not None else None,
            },
        }, indent=2))
        return
    if args.command == "live-preflight":
        raise SystemExit(_live_preflight(settings, args.confirm_live))


if __name__ == "__main__":
    main()

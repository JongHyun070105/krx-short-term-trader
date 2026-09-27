from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.engine import run_backtest
from krx_trader.backtest.metrics import calculate_metrics
from krx_trader.config import Settings
from krx_trader.data.historical import load_bars_csv
from krx_trader.execution.shadow import run_shadow_replay
from krx_trader.kis.auth import KisAuthError, TokenManager
from krx_trader.kis.transport import UrllibTransport
from krx_trader.market.regime import Regime, classify_regime
from krx_trader.models import Bar
from krx_trader.status import write_project_status
from krx_trader.storage.sqlite_store import SQLiteStore
from krx_trader.strategies.breakout import evaluate_breakout
from krx_trader.strategies.pullback import evaluate_pullback


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
    live = sub.add_parser("live-preflight")
    live.add_argument("--confirm-live", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
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

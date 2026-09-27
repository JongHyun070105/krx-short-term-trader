from __future__ import annotations

import hashlib
from dataclasses import dataclass

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.engine import BacktestResult, SignalFunction, run_backtest
from krx_trader.models import Bar
from krx_trader.storage.sqlite_store import SQLiteStore


@dataclass(frozen=True, slots=True)
class ShadowReplay:
    run_id: str
    result: BacktestResult


def run_shadow_replay(
    bars: list[Bar],
    signal_fn: SignalFunction,
    *,
    symbol: str,
    run_id: str,
    store: SQLiteStore,
    cost_model: CostModel,
    starting_cash_krw: float = 100_000,
) -> ShadowReplay:
    """Replay supplied observed bars through the shared rules; it does not fetch data or submit orders."""
    result = run_backtest(
        bars,
        signal_fn,
        symbol=symbol,
        starting_cash_krw=starting_cash_krw,
        capital_cap_krw=100_000,
        order_cap_krw=20_000,
        cost_model=cost_model,
    )
    bars_by_time = {bar.time: bar for bar in bars}
    for signal in result.decisions:
        decision_id = hashlib.sha256(
            f"{run_id}|{signal.strategy_id}|{signal.symbol}|{signal.timestamp.isoformat()}".encode()
        ).hexdigest()
        store.record_decision(decision_id, run_id, signal)
        observed = bars_by_time.get(signal.timestamp)
        snapshot_id = None
        if observed is not None:
            snapshot_id = hashlib.sha256(f"{decision_id}|snapshot".encode()).hexdigest()
            store.record_event(
                snapshot_id, run_id, signal.timestamp.isoformat(), "MARKET_SNAPSHOT", decision_id,
                {"open": observed.open, "high": observed.high, "low": observed.low,
                 "close": observed.close, "volume": observed.volume},
            )
        store.record_event(
            hashlib.sha256(f"{decision_id}|signal".encode()).hexdigest(),
            run_id,
            signal.timestamp.isoformat(),
            "SIGNAL",
            decision_id,
            {"strategy_id": signal.strategy_id, "reference_price": signal.reference_price,
             "stop_price": signal.stop_price, "reason_codes": list(signal.reason_codes)},
        )
        store.record_event(
            hashlib.sha256(f"{decision_id}|decision".encode()).hexdigest(),
            run_id,
            signal.timestamp.isoformat(),
            "DECISION",
            decision_id,
            {"decision": signal.decision.value, "reasons": list(signal.reason_codes),
             "market_snapshot_id": snapshot_id},
        )
    for index, trade in enumerate(result.trades):
        event_id = hashlib.sha256(f"{run_id}|shadow-trade|{index}|{trade.entry_time}".encode()).hexdigest()
        common = {"symbol": trade.symbol, "strategy_id": trade.strategy_id, "quantity": trade.quantity}
        stages = (
            ("RISK_APPROVED", trade.entry_signal_time, {"expected_stop_risk_krw": max(0, trade.entry_price - trade.stop_price) * trade.quantity}),
            ("ORDER_INTENT", trade.entry_signal_time, {"side": "BUY", "reference_price": trade.entry_reference_price,
             "fill_model": "NEXT_BAR_OPEN_WITH_COSTS"}),
            ("SIMULATED_SUBMIT", trade.entry_signal_time, {"status": "SIMULATED"}),
            ("SIMULATED_FILL", trade.entry_time, {"side": "BUY", "price": trade.entry_price, "quantity": trade.quantity}),
            ("POSITION_OPENED", trade.entry_time, {"average_price": trade.entry_price, "quantity": trade.quantity}),
            ("SIMULATED_EXIT_FILL", trade.exit_time, {"side": "SELL", "reference_price": trade.exit_reference_price,
             "price": trade.exit_price,
             "quantity": trade.quantity, "exit_reason": trade.exit_reason}),
            ("POSITION_CLOSED", trade.exit_time, {"quantity": trade.quantity, "mfe_krw": trade.mfe_krw, "mae_krw": trade.mae_krw}),
            ("PNL_RECORDED", trade.exit_time, {"gross_pnl_krw": trade.gross_pnl_krw,
             "fees_krw": trade.fees_krw, "tax_krw": trade.tax_krw, "slippage_krw": trade.slippage_krw,
             "net_pnl_krw": trade.net_pnl_krw}),
        )
        for stage_index, (event_type, timestamp, payload) in enumerate(stages):
            stage_id = hashlib.sha256(f"{event_id}|{stage_index}|{event_type}".encode()).hexdigest()
            store.record_event(stage_id, run_id, timestamp.isoformat(), event_type, event_id, {**common, **payload})
    return ShadowReplay(run_id, result)

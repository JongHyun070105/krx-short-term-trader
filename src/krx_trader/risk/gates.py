from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateInput:
    data_healthy: bool
    market_session_valid: bool
    symbol_allowed: bool
    price_allowed: bool
    liquidity_adequate: bool
    daily_loss_breached: bool
    daily_trade_limit_reached: bool
    duplicate_decision: bool
    kill_switch_active: bool
    ledger_consistent: bool
    kis_healthy: bool


@dataclass(frozen=True, slots=True)
class GateResult:
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_entry_gates(state: GateInput) -> GateResult:
    checks = (
        (state.data_healthy, "DATA_UNHEALTHY"),
        (state.market_session_valid, "MARKET_SESSION_INVALID"),
        (state.symbol_allowed, "SYMBOL_NOT_ALLOWED"),
        (state.price_allowed, "PRICE_NOT_ALLOWED"),
        (state.liquidity_adequate, "LIQUIDITY_INADEQUATE"),
        (not state.daily_loss_breached, "DAILY_LOSS_LIMIT"),
        (not state.daily_trade_limit_reached, "DAILY_TRADE_LIMIT"),
        (not state.duplicate_decision, "DUPLICATE_DECISION"),
        (not state.kill_switch_active, "KILL_SWITCH_ACTIVE"),
        (state.ledger_consistent, "LEDGER_INCONSISTENT"),
        (state.kis_healthy, "KIS_UNHEALTHY"),
    )
    reasons = tuple(code for passed, code in checks if not passed)
    return GateResult(not reasons, reasons)

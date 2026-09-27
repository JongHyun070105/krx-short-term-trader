from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from krx_trader.backtest.costs import CostModel
from krx_trader.data.quality import require_healthy_bars
from krx_trader.models import Bar, Decision, Signal
from krx_trader.risk.sizing import size_long_position


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    strategy_id: str
    entry_signal_time: datetime
    entry_time: datetime
    entry_reference_price: float
    entry_price: float
    exit_time: datetime
    exit_reference_price: float
    exit_price: float
    quantity: int
    stop_price: float
    gross_pnl_krw: float
    fees_krw: float
    tax_krw: float
    slippage_krw: float
    mfe_krw: float
    mae_krw: float
    net_pnl_krw: float
    holding_bars: int
    exit_reason: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    starting_cash_krw: float
    ending_cash_krw: float
    trades: tuple[Trade, ...]
    open_position: dict[str, float] | None
    decisions: tuple[Signal, ...]
    equity_curve: tuple[float, ...]


@dataclass(slots=True)
class _Position:
    signal: Signal
    quantity: int
    entry_time: datetime
    entry_reference: float
    entry_price: float
    entry_fee: float
    stop: float
    best_price: float
    worst_price: float
    bars_held: int = 0
    exit_next_open: bool = False


SignalFunction = Callable[[list[Bar]], Signal]


def run_backtest(
    bars: list[Bar],
    signal_fn: SignalFunction,
    *,
    symbol: str,
    starting_cash_krw: float = 100_000,
    capital_cap_krw: float = 100_000,
    order_cap_krw: float = 20_000,
    risk_per_trade_pct: float = 0.25,
    cost_model: CostModel,
    stress_multiplier: float = 1.0,
) -> BacktestResult:
    require_healthy_bars(bars)
    if starting_cash_krw <= 0 or starting_cash_krw > capital_cap_krw or not 0 < capital_cap_krw <= 100_000 or stress_multiplier <= 0:
        raise ValueError("invalid starting cash, capital cap, or cost stress")
    cash = float(starting_cash_krw)
    position: _Position | None = None
    pending_entry: Signal | None = None
    trades: list[Trade] = []
    decisions: list[Signal] = []
    equity: list[float] = []

    def close_position(current: _Position, current_bar: Bar, reference: float, reason: str, holding_bars: int) -> tuple[Trade, float]:
        fill = cost_model.sell_fill_price(reference, stress_multiplier)
        gross = (reference - current.entry_reference) * current.quantity
        slippage = ((current.entry_price - current.entry_reference) + (reference - fill)) * current.quantity
        sell_notional = fill * current.quantity
        fee = sell_notional * cost_model.broker_fee_rate * stress_multiplier
        tax = sell_notional * cost_model.sell_tax_rate * stress_multiplier
        net = gross - current.entry_fee - fee - tax - slippage
        trade = Trade(
            symbol, current.signal.strategy_id, current.signal.timestamp, current.entry_time, current.entry_reference,
            current.entry_price, current_bar.time, reference, fill, current.quantity, current.stop, gross,
            current.entry_fee + fee, tax, slippage,
            max(0.0, current.best_price - current.entry_price) * current.quantity,
            min(0.0, current.worst_price - current.entry_price) * current.quantity,
            net, holding_bars, reason,
        )
        return trade, sell_notional - fee - tax

    for index, bar in enumerate(bars):
        if position is not None:
            if position.exit_next_open:
                reference = bar.open
                trade, cash_delta = close_position(position, bar, reference, "TIME_EXIT", position.bars_held)
                trades.append(trade)
                cash += cash_delta
                position = None
            elif bar.low <= position.stop:
                reference = min(bar.open, position.stop)
                position.best_price = max(position.best_price, bar.high)
                position.worst_price = min(position.worst_price, bar.low)
                trade, cash_delta = close_position(position, bar, reference, "STOP", position.bars_held + 1)
                trades.append(trade)
                cash += cash_delta
                position = None
            else:
                position.best_price = max(position.best_price, bar.high)
                position.worst_price = min(position.worst_price, bar.low)
                position.bars_held += 1
                if position.bars_held >= (position.signal.max_holding_bars or 1):
                    position.exit_next_open = True

        if position is None and pending_entry is not None:
            signal = pending_entry
            pending_entry = None
            entry = cost_model.buy_fill_price(bar.open, stress_multiplier)
            stop = signal.stop_price
            if stop is not None and bar.open > stop and stop < entry:
                sizing = size_long_position(
                    entry_price=entry,
                    stop_price=stop,
                    bot_cash_krw=cash,
                    current_exposure_krw=0,
                    capital_cap_krw=min(capital_cap_krw, starting_cash_krw),
                    order_cap_krw=order_cap_krw,
                    risk_per_trade_pct=risk_per_trade_pct,
                    fee_rate=cost_model.broker_fee_rate * stress_multiplier,
                    sell_tax_rate=cost_model.sell_tax_rate * stress_multiplier,
                    slippage_bps=cost_model.slippage_bps * stress_multiplier,
                )
                if sizing.quantity and entry <= bar.high:
                    notional = entry * sizing.quantity
                    fee = cost_model.buy_cost(notional, stress_multiplier)
                    if notional + fee <= cash:
                        cash -= notional + fee
                        position = _Position(signal, sizing.quantity, bar.time, bar.open, entry, fee, stop, bar.high, bar.low)
                        if bar.low <= stop:
                            trade, cash_delta = close_position(position, bar, min(bar.open, stop), "STOP", 1)
                            trades.append(trade)
                            cash += cash_delta
                            position = None

        if index < len(bars) - 1:
            signal = signal_fn(bars[: index + 1])
            decisions.append(signal)
            if position is None and pending_entry is None and signal.decision == Decision.ENTER:
                pending_entry = signal

        marked_value = cash
        if position is not None:
            marked_value += position.quantity * bar.close
        equity.append(marked_value)

    open_position = None
    if position is not None:
        open_position = {
            "quantity": float(position.quantity),
            "entry_price": position.entry_price,
            "mark_price": bars[-1].close,
            "unrealized_gross_pnl_krw": (bars[-1].close - position.entry_price) * position.quantity,
            "entry_fee_krw": position.entry_fee,
            "entry_slippage_krw": (position.entry_price - position.entry_reference) * position.quantity,
        }
    return BacktestResult(starting_cash_krw, cash, tuple(trades), open_position, tuple(decisions), tuple(equity))

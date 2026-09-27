from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.engine import BacktestResult, Trade
from krx_trader.data.quality import require_healthy_bars
from krx_trader.models import Bar, Decision, Signal
from krx_trader.risk.sizing import size_long_position

PortfolioSignalFunction = Callable[[str, list[Bar]], Signal]


@dataclass(slots=True)
class _Position:
    signal: Signal
    symbol: str
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


def _mean_turnover(history: list[Bar], window: int = 20) -> float:
    sample = history[-window:]
    return sum(bar.close * bar.volume for bar in sample) / len(sample) if sample else 0.0


def run_portfolio_backtest(
    bars_by_symbol: dict[str, list[Bar]],
    signal_fn: PortfolioSignalFunction,
    *,
    starting_cash_krw: float = 100_000,
    capital_cap_krw: float = 100_000,
    order_cap_krw: float = 20_000,
    risk_per_trade_pct: float = 0.25,
    min_price_krw: float = 1_000,
    max_price_krw: float = 50_000,
    max_concurrent_positions: int = 2,
    cost_model: CostModel,
    stress_multiplier: float = 1.0,
) -> BacktestResult:
    """Simulate one cash-constrained portfolio across symbols in event-time order.

    Simultaneous pending entries are prioritized by trailing traded value, then symbol.
    Signals use bars through the current close and can fill only at the next bar open.
    """
    if not 0 < starting_cash_krw <= capital_cap_krw <= 100_000:
        raise ValueError("portfolio cash must fit within the 100000 KRW capital cap")
    if order_cap_krw <= 0 or max_concurrent_positions < 1 or stress_multiplier <= 0:
        raise ValueError("invalid order cap, position count, or cost stress")
    if not bars_by_symbol:
        raise ValueError("portfolio requires at least one symbol series")
    series: dict[str, list[Bar]] = {}
    for symbol, values in sorted(bars_by_symbol.items()):
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("portfolio symbol must be a six-digit KRX code")
        require_healthy_bars(values)
        if values:
            series[symbol] = values
    timeline = sorted({bar.time for values in series.values() for bar in values})
    if not timeline:
        raise ValueError("portfolio has no bars")
    by_time = {symbol: {bar.time: bar for bar in values} for symbol, values in series.items()}
    final_time_by_symbol = {symbol: values[-1].time for symbol, values in series.items()}
    positions: dict[str, _Position] = {}
    pending: dict[str, tuple[Signal, float]] = {}
    histories: dict[str, list[Bar]] = {symbol: [] for symbol in series}
    last_prices: dict[str, float] = {}
    cash = float(starting_cash_krw)
    trades: list[Trade] = []
    decisions: list[Signal] = []
    equity: list[float] = []

    def close(position: _Position, bar: Bar, reference: float, reason: str, held: int) -> None:
        nonlocal cash
        fill = cost_model.sell_fill_price(reference, stress_multiplier)
        sell_notional = fill * position.quantity
        fee = sell_notional * cost_model.broker_fee_rate * stress_multiplier
        tax = sell_notional * cost_model.sell_tax_rate * stress_multiplier
        slippage = (
            (position.entry_price - position.entry_reference) + (reference - fill)
        ) * position.quantity
        gross = (reference - position.entry_reference) * position.quantity
        net = gross - position.entry_fee - fee - tax - slippage
        trades.append(
            Trade(
                position.symbol,
                position.signal.strategy_id,
                position.signal.timestamp,
                position.entry_time,
                position.entry_reference,
                position.entry_price,
                bar.time,
                reference,
                fill,
                position.quantity,
                position.stop,
                gross,
                position.entry_fee + fee,
                tax,
                slippage,
                max(0.0, position.best_price - position.entry_price) * position.quantity,
                min(0.0, position.worst_price - position.entry_price) * position.quantity,
                net,
                held,
                reason,
            )
        )
        cash += sell_notional - fee - tax

    for timestamp in timeline:
        current = {symbol: by_time[symbol][timestamp] for symbol in series if timestamp in by_time[symbol]}
        for symbol, position in list(positions.items()):
            bar = current.get(symbol)
            if bar is None:
                continue
            if position.exit_next_open:
                close(position, bar, bar.open, "TIME_EXIT", position.bars_held)
                del positions[symbol]
            elif bar.low <= position.stop:
                position.best_price = max(position.best_price, bar.high)
                position.worst_price = min(position.worst_price, bar.low)
                close(position, bar, min(bar.open, position.stop), "STOP", position.bars_held + 1)
                del positions[symbol]
            else:
                position.best_price = max(position.best_price, bar.high)
                position.worst_price = min(position.worst_price, bar.low)
                position.bars_held += 1
                if position.bars_held >= (position.signal.max_holding_bars or 1):
                    position.exit_next_open = True
        candidates = [
            (symbol, item[0], item[1]) for symbol, item in pending.items() if symbol in current
        ]
        candidates.sort(key=lambda item: (-item[2], item[0]))
        for symbol, signal, _priority in candidates:
            pending.pop(symbol, None)
            bar = current[symbol]
            if symbol in positions or len(positions) >= max_concurrent_positions:
                continue
            if signal.stop_price is None:
                continue
            entry = cost_model.buy_fill_price(bar.open, stress_multiplier)
            exposure = sum(position.entry_price * position.quantity for position in positions.values())
            sizing = size_long_position(
                entry_price=entry,
                stop_price=signal.stop_price,
                bot_cash_krw=cash,
                current_exposure_krw=exposure,
                capital_cap_krw=min(capital_cap_krw, starting_cash_krw),
                order_cap_krw=order_cap_krw,
                risk_per_trade_pct=risk_per_trade_pct,
                min_price_krw=min_price_krw,
                max_price_krw=max_price_krw,
                fee_rate=cost_model.broker_fee_rate * stress_multiplier,
                sell_tax_rate=cost_model.sell_tax_rate * stress_multiplier,
                slippage_bps=cost_model.slippage_bps * stress_multiplier,
            )
            if not sizing.quantity or entry <= signal.stop_price or entry > bar.high:
                continue
            notional = entry * sizing.quantity
            fee = cost_model.buy_cost(notional, stress_multiplier)
            if notional + fee > cash:
                continue
            cash -= notional + fee
            position = _Position(
                signal, symbol, sizing.quantity, bar.time, bar.open, entry, fee,
                signal.stop_price, bar.high, bar.low,
            )
            positions[symbol] = position
            if bar.low <= position.stop:
                close(position, bar, min(bar.open, position.stop), "STOP", 1)
                del positions[symbol]
        for symbol, bar in current.items():
            last_prices[symbol] = bar.close
            histories[symbol].append(bar)
        for symbol, history in histories.items():
            if symbol not in current or len(history) >= len(series[symbol]):
                continue
            signal = signal_fn(symbol, history)
            decisions.append(signal)
            if signal.decision == Decision.ENTER and symbol not in positions and symbol not in pending:
                pending[symbol] = (signal, _mean_turnover(history))
        for symbol, position in list(positions.items()):
            if timestamp != final_time_by_symbol[symbol]:
                continue
            bar = current[symbol]
            close(position, bar, bar.close, "SEGMENT_END", max(1, position.bars_held))
            del positions[symbol]
        marked = cash + sum(position.quantity * last_prices.get(symbol, position.entry_price)
                            for symbol, position in positions.items())
        equity.append(marked)

    open_position = None
    if positions:
        open_position = {
            "quantity": float(sum(position.quantity for position in positions.values())),
            "entry_price": 0.0,
            "mark_price": 0.0,
            "unrealized_gross_pnl_krw": sum(
                (last_prices[symbol] - position.entry_price) * position.quantity
                for symbol, position in positions.items()
            ),
            "entry_fee_krw": sum(position.entry_fee for position in positions.values()),
            "entry_slippage_krw": sum(
                (position.entry_price - position.entry_reference) * position.quantity
                for position in positions.values()
            ),
        }
    return BacktestResult(
        float(starting_cash_krw), cash, tuple(trades), open_position, tuple(decisions), tuple(equity)
    )

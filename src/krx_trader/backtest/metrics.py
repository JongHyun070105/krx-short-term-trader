from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from krx_trader.backtest.engine import BacktestResult


@dataclass(frozen=True, slots=True)
class Metrics:
    total_return_pct: float
    max_drawdown_pct: float
    profit_factor: float | None
    win_rate_pct: float
    average_win_krw: float
    average_loss_krw: float
    expectancy_krw: float
    trade_count: int
    average_holding_bars: float
    turnover_krw: float
    cost_drag_krw: float
    best_trade_krw: float | None
    worst_trade_krw: float | None
    top_trade_contribution_pct: float | None
    sharpe_trade_series: float | None
    sortino_trade_series: float | None


def calculate_metrics(result: BacktestResult) -> Metrics:
    pnls = [trade.net_pnl_krw for trade in result.trades]
    winners = [pnl for pnl in pnls if pnl > 0]
    losers = [pnl for pnl in pnls if pnl < 0]
    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))
    returns = [pnl / result.starting_cash_krw for pnl in pnls]
    mean_return = sum(returns) / len(returns) if returns else 0.0
    deviations = [value - mean_return for value in returns]
    std = sqrt(sum(value * value for value in deviations) / max(1, len(deviations) - 1)) if len(returns) > 1 else 0.0
    downside = [min(0.0, value) for value in returns]
    downside_std = sqrt(sum(value * value for value in downside) / len(downside)) if downside else 0.0
    peak = result.starting_cash_krw
    max_dd = 0.0
    for value in result.equity_curve:
        peak = max(peak, value)
        if peak:
            max_dd = max(max_dd, (peak - value) / peak)
    ending_value = result.equity_curve[-1] if result.equity_curve else result.ending_cash_krw
    total_return = (ending_value - result.starting_cash_krw) / result.starting_cash_krw * 100
    turnover = sum((trade.entry_price + trade.exit_price) * trade.quantity for trade in result.trades)
    costs = sum(trade.fees_krw + trade.tax_krw + trade.slippage_krw for trade in result.trades)
    if result.open_position:
        costs += result.open_position.get("entry_fee_krw", 0.0) + result.open_position.get("entry_slippage_krw", 0.0)
    top_contribution = None
    if pnls and sum(pnls) != 0:
        top_contribution = max(pnls) / sum(pnls) * 100
    return Metrics(
        total_return,
        max_dd * 100,
        gross_wins / gross_losses if gross_losses else (float("inf") if gross_wins else None),
        len(winners) / len(pnls) * 100 if pnls else 0.0,
        sum(winners) / len(winners) if winners else 0.0,
        sum(losers) / len(losers) if losers else 0.0,
        sum(pnls) / len(pnls) if pnls else 0.0,
        len(pnls),
        sum(trade.holding_bars for trade in result.trades) / len(result.trades) if result.trades else 0.0,
        turnover,
        costs,
        max(pnls) if pnls else None,
        min(pnls) if pnls else None,
        top_contribution,
        mean_return / std * sqrt(len(returns)) if std else None,
        mean_return / downside_std * sqrt(len(returns)) if downside_std else None,
    )

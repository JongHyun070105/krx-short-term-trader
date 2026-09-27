import random
from datetime import datetime, timedelta

import pytest
from conftest import KST, make_bar

from krx_trader.backtest.costs import CostModel
from krx_trader.backtest.engine import run_backtest
from krx_trader.backtest.metrics import Metrics, calculate_metrics
from krx_trader.backtest.portfolio import run_portfolio_backtest
from krx_trader.backtest.validation import StrategyGateState, evaluate_strategy_gate
from krx_trader.execution.live import (
    LiveBlocked,
    LiveBrokerAdapter,
    LiveGates,
    authorize_live_order,
)
from krx_trader.execution.state_machine import OrderStatus, transition
from krx_trader.market.regime import Regime
from krx_trader.models import Bar, Decision, Signal
from krx_trader.risk.gates import GateInput, evaluate_entry_gates
from krx_trader.risk.kill_switch import DailyRiskState
from krx_trader.risk.sizing import size_long_position


def test_sizing_skips_expensive_one_share_risk_and_enforces_order_cap():
    result = size_long_position(
        entry_price=20_000, stop_price=19_000, bot_cash_krw=100_000, current_exposure_krw=0,
        capital_cap_krw=100_000, order_cap_krw=20_000, risk_per_trade_pct=0.25,
    )
    assert result.quantity == 0
    assert result.reason == "UNAFFORDABLE_ONE_SHARE"
    risk_limited = size_long_position(
        entry_price=10_000, stop_price=9_700, bot_cash_krw=100_000, current_exposure_krw=0,
        capital_cap_krw=100_000, order_cap_krw=20_000, risk_per_trade_pct=0.25,
    )
    assert risk_limited.quantity == 0
    assert risk_limited.reason == "RISK_BUDGET_BELOW_ONE_SHARE"
    bounded = size_long_position(
        entry_price=10_000, stop_price=9_990, bot_cash_krw=100_000, current_exposure_krw=90_000,
        capital_cap_krw=100_000, order_cap_krw=20_000, risk_per_trade_pct=1,
    )
    assert bounded.notional_krw <= 10_000


def test_sizing_risk_uses_buy_fill_and_slipped_stop_exit_once():
    result = size_long_position(
        entry_price=1_000,
        stop_price=990,
        bot_cash_krw=100_000,
        current_exposure_krw=0,
        capital_cap_krw=100_000,
        order_cap_krw=100_000,
        risk_per_trade_pct=1.0,
        fee_rate=0.005,
        sell_tax_rate=0.005,
        slippage_bps=100,
    )
    stop_fill = 990 * 0.99
    expected_per_share_risk = 1_000 - stop_fill + 1_000 * 0.005 + stop_fill * 0.01
    assert result.quantity > 0
    assert result.expected_risk_krw / result.quantity == pytest.approx(expected_per_share_risk)


def test_entry_gates_fail_closed_for_data_loss_and_duplicates():
    result = evaluate_entry_gates(GateInput(True, True, True, True, True, True, False, True, False, True, True))
    assert not result.allowed
    assert result.reasons == ("DAILY_LOSS_LIMIT", "DUPLICATE_DECISION")


def test_daily_risk_state_persists_same_session_and_corruption_blocks(tmp_path):
    state = DailyRiskState(tmp_path / "risk.json")
    now = datetime(2026, 9, 27, 11, tzinfo=KST)
    saved = state.record_trade(-800, now)
    assert saved.trades == 1
    assert state.load(now).realized_pnl_krw == -800
    assert state.loss_limit_breached(100_000, 0.75, now=now)
    (tmp_path / "risk.json").write_text("not-json")
    with pytest.raises(RuntimeError, match="must remain blocked"):
        state.load(now)


def test_live_needs_all_three_gates_and_includes_pending_exposure():
    base = LiveGates("shadow", False, False, 100_000, 0)
    with pytest.raises(LiveBlocked):
        authorize_live_order(base, 10_000)
    allowed_gates = LiveGates("live", True, True, 100_000, 80_000, pending_buy_notional_krw=10_000)
    with pytest.raises(LiveBlocked, match="capital cap"):
        authorize_live_order(allowed_gates, 20_000)
    with pytest.raises(LiveBlocked, match="unavailable"):
        LiveBrokerAdapter().submit(LiveGates("live", True, True, 100_000, 0), 20_000)


def test_order_state_machine_requires_reconciliation_after_unknown():
    assert transition(OrderStatus.SUBMITTING, OrderStatus.UNKNOWN) == OrderStatus.UNKNOWN
    assert transition(OrderStatus.UNKNOWN, OrderStatus.ACCEPTED) == OrderStatus.ACCEPTED
    with pytest.raises(ValueError):
        transition(OrderStatus.FILLED, OrderStatus.OPEN)


def test_golden_pnl_uses_next_bar_open_and_separates_all_costs():
    bars = [
        make_bar(0, open_=1_000, high=1_010, low=990, close=1_000),
        make_bar(1, open_=1_000, high=1_020, low=990, close=1_010),
        make_bar(2, open_=1_020, high=1_100, low=1_000, close=1_090),
        make_bar(3, open_=1_100, high=1_110, low=1_080, close=1_100),
    ]

    def signal_fn(history):
        last = history[-1]
        if len(history) == 1:
            return Signal(last.time, "005930", "golden", Decision.ENTER, ("TEST",), 1_000, 900, 1)
        return Signal(last.time, "005930", "golden", Decision.HOLD, ("NO_SIGNAL",))

    costs = CostModel(broker_fee_rate=0.01, sell_tax_rate=0.02, slippage_bps=100)
    result = run_backtest(
        bars, signal_fn, symbol="005930", starting_cash_krw=100_000, capital_cap_krw=100_000,
        order_cap_krw=20_000, risk_per_trade_pct=1.0, cost_model=costs,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_time == bars[1].time
    assert trade.entry_price == pytest.approx(1_010)
    assert trade.exit_time == bars[3].time
    assert trade.exit_price == pytest.approx(1_089)
    assert trade.quantity == 6
    assert trade.gross_pnl_krw == pytest.approx(600)
    assert trade.fees_krw == pytest.approx(125.94)
    assert trade.tax_krw == pytest.approx(130.68)
    assert trade.slippage_krw == pytest.approx(126)
    assert trade.mfe_krw == pytest.approx(540)
    assert trade.mae_krw == pytest.approx(-120)
    assert trade.net_pnl_krw == pytest.approx(217.38)
    assert result.ending_cash_krw == pytest.approx(100_217.38)
    metrics = calculate_metrics(result)
    assert metrics.trade_count == 1
    assert metrics.cost_drag_krw == pytest.approx(382.62)


def test_stop_can_fill_within_the_entry_bar():
    bars = [
        make_bar(0, open_=1_000, high=1_010, low=990, close=1_000),
        make_bar(1, open_=1_000, high=1_020, low=800, close=850),
        make_bar(2, open_=850, high=860, low=840, close=850),
    ]

    def signal_fn(history):
        last = history[-1]
        decision = Decision.ENTER if len(history) == 1 else Decision.HOLD
        return Signal(last.time, "005930", "same-bar-stop", decision, ("TEST",), 1_000, 900, 3)

    result = run_backtest(bars, signal_fn, symbol="005930", cost_model=CostModel(0, 0, 0))
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "STOP"
    assert result.trades[0].entry_time == bars[1].time
    assert result.trades[0].exit_time == bars[1].time
    assert result.trades[0].exit_price == 900
    assert result.trades[0].net_pnl_krw < 0


def test_strategy_signal_does_not_use_future_bars_in_backtest():
    bars = [
        make_bar(0, open_=100, high=102, low=99, close=101, volume=100),
        make_bar(1, open_=101, high=103, low=100, close=102, volume=100),
        make_bar(2, open_=103, high=108, low=102, close=107, volume=200),
        make_bar(3, open_=107, high=108, low=106, close=107, volume=100),
        make_bar(4, open_=108, high=109, low=107, close=108, volume=100),
    ]
    from krx_trader.strategies.breakout import BreakoutConfig, evaluate_breakout

    config = BreakoutConfig(lookback=2, volume_lookback=2, volume_multiple=1.5)
    signal_fn = lambda history: evaluate_breakout(history, "005930", regime=Regime.NEUTRAL, config=config)
    changed = list(bars)
    changed[4] = make_bar(4, open_=1, high=1_000_000, low=1, close=900_000, volume=10_000_000)
    original = run_backtest(bars, signal_fn, symbol="005930", cost_model=CostModel(0.0, 0.0, 0.0))
    modified = run_backtest(changed, signal_fn, symbol="005930", cost_model=CostModel(0.0, 0.0, 0.0))
    assert original.decisions[:4] == modified.decisions[:4]


def test_randomized_portfolio_execution_invariants():
    """Stress whole-share sizing and next-bar fills across varied valid OHLC paths."""
    costs = CostModel(0.00015, 0.002, 15)
    symbols = {"005930": 5_000.0, "000660": 10_000.0, "035420": 25_000.0, "051910": 45_000.0}

    for seed in range(20):
        rng = random.Random(seed)
        bars_by_symbol = {}
        for symbol, base in symbols.items():
            previous_close = base
            bars = []
            for minute in range(40):
                open_price = previous_close * (1 + rng.uniform(-0.01, 0.01))
                close = open_price * (1 + rng.uniform(-0.015, 0.015))
                high = max(open_price, close) * (1 + rng.uniform(0, 0.01))
                low = min(open_price, close) * (1 - rng.uniform(0, 0.01))
                bars.append(
                    Bar(
                        datetime(2026, 1, 5, 9, tzinfo=KST) + timedelta(minutes=minute),
                        open_price,
                        high,
                        low,
                        close,
                        rng.randint(1_000, 100_000),
                    )
                )
                previous_close = close
            bars_by_symbol[symbol] = bars

        def signal_fn(symbol, history):
            last = history[-1]
            if len(history) >= 2 and len(history) % 6 == 2:
                return Signal(last.time, symbol, "randomized-invariant", Decision.ENTER,
                              ("TEST",), last.close, last.low * 0.95, 2)
            return Signal(last.time, symbol, "randomized-invariant", Decision.HOLD, ("TEST",))

        result = run_portfolio_backtest(
            bars_by_symbol,
            signal_fn,
            starting_cash_krw=100_000,
            capital_cap_krw=100_000,
            order_cap_krw=50_000,
            risk_per_trade_pct=1.0,
            max_price_krw=50_000,
            max_concurrent_positions=2,
            cost_model=costs,
        )

        assert result.ending_cash_krw >= 0
        assert min(result.equity_curve) >= 0
        for trade in result.trades:
            assert type(trade.quantity) is int and trade.quantity > 0
            assert trade.entry_signal_time < trade.entry_time
            assert trade.entry_price * trade.quantity <= 50_000
        fill_keys = [
            (trade.symbol, trade.strategy_id, trade.entry_signal_time)
            for trade in result.trades
        ]
        assert len(fill_keys) == len(set(fill_keys))
        assert result.open_position is None

        # Apply exits before same-timestamp fills, matching the portfolio event loop.
        by_entry: dict[datetime, list] = {}
        by_exit: dict[datetime, list] = {}
        for trade in result.trades:
            by_entry.setdefault(trade.entry_time, []).append(trade)
            by_exit.setdefault(trade.exit_time, []).append(trade)
        active = {}
        for timestamp in sorted(set(by_entry) | set(by_exit)):
            for trade in by_exit.get(timestamp, []):
                if trade.entry_time < timestamp:
                    active.pop(id(trade), None)
            for trade in by_entry.get(timestamp, []):
                active[id(trade)] = trade
            assert len(active) <= 2
            assert sum(item.entry_price * item.quantity for item in active.values()) <= 100_000
            for trade in by_exit.get(timestamp, []):
                active.pop(id(trade), None)


def test_strategy_gate_requires_oos_stress_trade_count_and_neighborhood():
    good = Metrics(1.0, 1.0, 1.2, 55.0, 100.0, -70.0, 20.0, 40, 3.0, 10_000, 100.0, 300.0, -150.0, 15.0, 0.5, 0.7)
    fail_stress = Metrics(-0.1, 1.0, 0.9, 45.0, 100.0, -120.0, -5.0, 40, 3.0, 10_000, 200.0, 200.0, -250.0, 20.0, -0.2, -0.3)
    result = evaluate_strategy_gate(good, good, good, [good, good])
    assert result.state == StrategyGateState.ACCEPTED_FOR_SHADOW
    rejected = evaluate_strategy_gate(good, fail_stress, good, [good])
    assert rejected.state == StrategyGateState.REJECTED
    assert "COST_STRESS_1_5X_FAILED" in rejected.reasons


def test_portfolio_competition_uses_trailing_turnover_and_symbol_tiebreaker():
    symbols = ("000001", "000002")
    bars = {
        symbol: [make_bar(index, open_=10_000, high=10_100, low=9_900, close=10_000,
                          volume=(200 if symbol == "000002" else 100)) for index in range(4)]
        for symbol in symbols
    }

    def signal_fn(symbol, history):
        bar = history[-1]
        if len(history) == 1:
            return Signal(bar.time, symbol, "portfolio-test", Decision.ENTER, ("TEST",), 10_000, 9_000, 1)
        return Signal(bar.time, symbol, "portfolio-test", Decision.HOLD, ("HOLD",))

    result = run_portfolio_backtest(
        bars, signal_fn, max_concurrent_positions=1, cost_model=CostModel(0, 0, 0),
        order_cap_krw=20_000, risk_per_trade_pct=1,
    )
    assert [trade.symbol for trade in result.trades] == ["000002"]
    tied = {symbol: [make_bar(index, open_=10_000, high=10_100, low=9_900, close=10_000,
                              volume=100) for index in range(4)] for symbol in symbols}
    tie_result = run_portfolio_backtest(
        tied, signal_fn, max_concurrent_positions=1, cost_model=CostModel(0, 0, 0),
        order_cap_krw=20_000, risk_per_trade_pct=1,
    )
    assert [trade.symbol for trade in tie_result.trades] == ["000001"]


def test_portfolio_cash_constraint_blocks_second_simultaneous_order():
    symbols = ("000001", "000002")
    bars = {
        symbol: [make_bar(index, open_=15_000, high=15_100, low=14_900, close=15_000,
                          volume=200 if symbol == "000002" else 100) for index in range(4)]
        for symbol in symbols
    }

    def signal_fn(symbol, history):
        bar = history[-1]
        if len(history) == 1:
            return Signal(bar.time, symbol, "cash-test", Decision.ENTER, ("TEST",), 15_000, 14_000, 1)
        return Signal(bar.time, symbol, "cash-test", Decision.HOLD, ("HOLD",))

    result = run_portfolio_backtest(
        bars, signal_fn, starting_cash_krw=25_000, max_concurrent_positions=2,
        order_cap_krw=20_000, risk_per_trade_pct=4, cost_model=CostModel(0, 0, 0),
    )
    assert [trade.symbol for trade in result.trades] == ["000002"]


def test_portfolio_marks_final_segment_bar_as_a_costed_exit():
    early_end = [make_bar(index, open_=1_000, high=1_010, low=990, close=1_005) for index in range(2)]
    normal_end = [make_bar(index, open_=1_000, high=1_010, low=990, close=1_005) for index in range(3)]

    def signal_fn(symbol, history):
        bar = history[-1]
        decision = Decision.ENTER if len(history) == 1 else Decision.HOLD
        return Signal(bar.time, symbol, "segment-test", decision, ("TEST",), 1_000, 900, 10)

    result = run_portfolio_backtest(
        {"000001": early_end, "000002": normal_end}, signal_fn,
        max_concurrent_positions=2, cost_model=CostModel(0.001, 0, 25), risk_per_trade_pct=1,
    )
    assert len(result.trades) == 2
    assert all(trade.exit_reason == "SEGMENT_END" for trade in result.trades)
    assert {trade.symbol: trade.exit_time for trade in result.trades} == {
        "000001": early_end[-1].time, "000002": normal_end[-1].time,
    }
    assert result.open_position is None

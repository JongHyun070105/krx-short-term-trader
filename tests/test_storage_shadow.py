from conftest import make_bar

from krx_trader.execution.state_machine import OrderStatus
from krx_trader.models import Decision, Signal
from krx_trader.storage.sqlite_store import LedgerError, SQLiteStore


def _signal(minute, strategy, decision):
    bar = make_bar(minute)
    return Signal(bar.time, "005930", strategy, decision, ("TEST",), 100, 90, 2)


def test_positions_change_only_from_unique_fills_and_are_bot_owned(tmp_path):
    store = SQLiteStore(tmp_path / "runtime" / "ledger.sqlite3")
    store.initialize_cash(1_000)
    buy = _signal(0, "breakout_volume", Decision.ENTER)
    assert store.record_decision("d1", "run", buy)
    assert not store.record_decision("d1", "run", buy)
    assert store.create_order("o1", "d1", "005930", "BUY", 3, buy.strategy_id, buy.timestamp.isoformat(), 100, 303)
    assert not store.create_order("o2", "d1", "005930", "BUY", 3, buy.strategy_id, buy.timestamp.isoformat(), 100, 303)
    for status in (OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING, OrderStatus.UNKNOWN, OrderStatus.ACCEPTED):
        store.transition_order("o1", status)
    assert store.bot_positions() == []
    assert store.record_fill(fill_id="f1", order_id="o1", quantity=2, price=100, fee_krw=1, tax_krw=0, timestamp="t1")
    assert not store.record_fill(fill_id="f1", order_id="o1", quantity=2, price=100, fee_krw=1, tax_krw=0, timestamp="t1")
    assert store.order_status("o1") == OrderStatus.PARTIALLY_FILLED
    assert store.bot_positions()[0]["quantity"] == 2
    assert store.bot_cash_krw() == 799
    assert store.pending_buy_notional_krw() == 101
    assert store.available_cash_krw() == 698
    assert store.bot_nav_krw({"005930": 100}) == 999
    assert store.bot_exposure_krw({"005930": 100}) == 301
    assert store.record_fill(fill_id="f2", order_id="o1", quantity=1, price=102, fee_krw=1, tax_krw=0, timestamp="t2")
    assert store.order_status("o1") == OrderStatus.FILLED
    assert store.bot_positions()[0]["quantity"] == 3
    assert store.pending_buy_notional_krw() == 0

    sell = _signal(1, "breakout_volume", Decision.EXIT)
    store.record_decision("d2", "run", sell)
    store.create_order("o2", "d2", "005930", "SELL", 2, sell.strategy_id, sell.timestamp.isoformat(), 110)
    for status in (OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING, OrderStatus.ACCEPTED):
        store.transition_order("o2", status)
    assert store.record_fill(fill_id="f3", order_id="o2", quantity=2, price=110, fee_krw=1, tax_krw=2, timestamp="t3")
    assert store.bot_positions()[0]["quantity"] == 1


def test_sell_cannot_claim_manual_account_positions(tmp_path):
    store = SQLiteStore(tmp_path / "ledger.sqlite3")
    store.initialize_cash(10_000)
    signal = _signal(0, "breakout_volume", Decision.EXIT)
    store.record_decision("sell-d", "run", signal)
    store.create_order("sell-o", "sell-d", "005930", "SELL", 1, signal.strategy_id, signal.timestamp.isoformat(), 100)
    for status in (OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING, OrderStatus.ACCEPTED):
        store.transition_order("sell-o", status)
    try:
        store.record_fill(fill_id="sell-f", order_id="sell-o", quantity=1, price=100, fee_krw=0, tax_krw=0, timestamp="t")
    except LedgerError as error:
        assert "bot-owned position" in str(error)
    else:
        raise AssertionError("manual position was sold by the bot ledger")
    assert store.order_status("sell-o") == OrderStatus.ACCEPTED
    assert store.bot_positions() == []
    assert store.bot_cash_krw() == 10_000

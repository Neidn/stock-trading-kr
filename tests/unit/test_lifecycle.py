"""Order lifecycle tests — stock-trading-execution references/order-lifecycle.md 4절 목록.

가짜 브로커로 실전에서 오는 순서대로 이벤트를 만든다. DB 에 행을 직접 넣지 않는다.
"""

from __future__ import annotations

import pytest

from src.broker.fake_broker import FakeBroker
from src.execution import order_state as st
from src.execution.costs import CostModel
from src.execution.order_manager import DuplicateOrderBlocked, OrderManager
from src.execution.reconcile import Reconciler
from src.execution.store import Store
from src.risk.kill_switch import KillSwitch
from src.risk.stop_monitor import StopMonitor

pytestmark = pytest.mark.usefixtures("db")

CODE = "005930"


@pytest.fixture
def env(clock, rules, notes):
    broker = FakeBroker(clock=clock)
    broker.set_quote(CODE, 70000)
    store = Store()
    store.upsert_instrument(CODE, "삼성전자", "KOSPI", True, None)
    costs = CostModel(rules, 0.00015, lambda c: "KOSPI")
    om = OrderManager(broker, store, costs, notes, clock)
    kill = KillSwitch(store, notes)
    stops = StopMonitor(broker, store, om, kill, daily_loss_limit_krw=200_000, stale_sec=30,
                        notify=notes, clock=clock)
    rec = Reconciler(broker, store, om, kill, notes)
    return broker, store, om, stops, rec, kill, clock


def buy(om, qty=10, price=70000, stop=65000):
    return om.submit(code=CODE, side="buy", qty=qty, order_type="limit", price=price,
                     purpose="entry", strategy_name="ema_crossover", stop_price=stop,
                     take_profit=80000, initial_risk=price - stop)


def test_accepted_without_fill_has_no_position(env):
    broker, store, om, *_ , clock = env
    o = buy(om)
    assert o["state"] == st.ACCEPTED
    om.sync(clock().date())
    assert store.positions() == {}


def test_three_partial_fills(env):
    broker, store, om, *_, clock = env
    o = buy(om)
    for qty, px in [(3, 70000), (3, 69900), (4, 69800)]:
        broker.fill(o["order_no"], qty, px)
        om.sync(clock().date())
    pos = store.positions()[CODE]
    assert pos["qty"] == 10
    assert float(pos["avg_price"]) == pytest.approx((3 * 70000 + 3 * 69900 + 4 * 69800) / 10)
    assert pos["stop_price"] == 65000
    assert store.get_order(o["client_id"])["state"] == st.FILLED


def test_same_fill_seen_twice_applies_once(env):
    broker, store, om, *_, clock = env
    o = buy(om)
    broker.fill(o["order_no"], 5, 70000)
    assert om.sync(clock().date()) == 1
    assert om.sync(clock().date()) == 0
    assert store.positions()[CODE]["qty"] == 5


def test_cancel_races_full_fill(env):
    """취소 요청 중 원주문 전량 체결 → 취소 거부, 상태 FILLED (정정 경합과 같은 구조)."""
    broker, store, om, *_, clock = env
    o = buy(om)
    broker.fill_before_cancel = True
    om.cancel(store.get_order(o["client_id"]))
    om.sync(clock().date())
    assert store.get_order(o["client_id"])["state"] == st.FILLED
    assert store.positions()[CODE]["qty"] == 10


def test_cancel_after_partial_keeps_filled_part(env):
    broker, store, om, *_, clock = env
    o = buy(om)
    broker.fill(o["order_no"], 4, 70000)
    om.sync(clock().date())
    om.cancel(store.get_order(o["client_id"]))
    assert store.get_order(o["client_id"])["state"] == st.CANCELLING   # 요청만으로 확정하지 않는다
    om.sync(clock().date())
    assert store.get_order(o["client_id"])["state"] == st.CANCELLED
    assert store.positions()[CODE]["qty"] == 4


def test_timeout_then_found_does_not_reorder(env):
    broker, store, om, *_, clock = env
    broker.next_place = "timeout_accepted"
    o = buy(om)
    assert o["state"] == st.UNKNOWN
    with pytest.raises(DuplicateOrderBlocked):
        buy(om)                                           # UNKNOWN 이면 같은 종목·방향 재주문 금지
    om.sync(clock().date())
    o = store.get_order(o["client_id"])
    assert o["state"] == st.ACCEPTED and o["order_no"] == broker.last_order_no()
    assert sum(c.startswith("place") for c in broker.calls) == 1


def test_timeout_lost_is_rejected_after_grace(env):
    broker, store, om, *_, clock = env
    broker.next_place = "timeout_lost"
    o = buy(om)
    om.sync(clock().date())
    assert store.get_order(o["client_id"])["state"] == st.UNKNOWN    # 유예 시간 안
    clock.advance(seconds=90)
    om.sync(clock().date())
    assert store.get_order(o["client_id"])["state"] == st.REJECTED


def test_session_end_expires_remaining(env):
    broker, store, om, *_, clock = env
    o = buy(om)
    broker.fill(o["order_no"], 2, 70000)
    om.sync(clock().date())
    clock.advance(hours=6)
    broker.expire_all()
    leftover = om.close_session(clock().date())
    assert leftover == []
    assert store.get_order(o["client_id"])["state"] in (st.EXPIRED, st.CANCELLED)
    assert store.open_orders() == []
    assert store.positions()[CODE]["qty"] == 2


def test_reject_is_recorded_not_retried(env):
    broker, store, om, *_, clock = env
    broker.next_place = "reject"
    o = buy(om)
    assert o["state"] == st.REJECTED and "호가단위" in o["reject_reason"]
    assert sum(c.startswith("place") for c in broker.calls) == 1


def test_missed_fills_recovered_by_reconcile(env):
    broker, store, om, stops, rec, kill, clock = env
    o = buy(om)
    broker.fill(o["order_no"], 10, 70000)                 # poll 이 돌기 전 (웹소켓 끊김과 같은 상황)
    res = rec.run(clock().date())
    assert res.fills_recovered == 1 and res.clean
    assert store.fill_sources_on(clock().date()) == {"reconcile": 1}


def test_split_locks_symbol_and_does_not_fire_stop(env):
    broker, store, om, stops, rec, kill, clock = env
    o = buy(om)
    broker.fill(o["order_no"], 10, 70000)
    om.sync(clock().date())
    broker.split(CODE, 5)
    broker.set_quote(CODE, 14000)                          # 가격 1/5 → 손절가 65,000 아래처럼 보인다
    assert stops.tick(can_trade=True) == []                # 손절 미발동
    res = rec.run(clock().date())
    assert "corporate action" in res.mismatches[0]["reason"]
    assert CODE in store.locked_symbols()
    assert kill.level() == 1


def test_stop_fires_once_and_does_not_stack(env):
    broker, store, om, stops, rec, kill, clock = env
    o = buy(om)
    broker.fill(o["order_no"], 10, 70000)
    om.sync(clock().date())
    broker.set_quote(CODE, 64000)
    assert len(stops.tick(can_trade=True)) == 1
    assert stops.tick(can_trade=True) == []                # 단일가·미체결 중 재주문 금지
    sell = store.open_orders(code=CODE, side="sell")[0]
    broker.fill(sell["order_no"], 10, 64000)
    om.sync(clock().date())
    assert store.positions() == {}


def test_roundtrip_net_includes_fee_and_tax(env):
    broker, store, om, stops, rec, kill, clock = env
    o = buy(om)
    broker.fill(o["order_no"], 10, 70000)
    om.sync(clock().date())
    broker.set_quote(CODE, 64000)
    stops.tick(can_trade=True)
    sell = store.open_orders(code=CODE, side="sell")[0]
    broker.fill(sell["order_no"], 10, 64000)
    om.sync(clock().date())
    from src.db.connection import db_conn
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM trades")
        t = cur.fetchone()
    buy_fee = int(700000 * 0.00015)
    sell_fee = int(640000 * 0.00015)
    tax = int(640000 * 0.002)
    assert t["gross_pnl"] == -60000
    assert (t["fee"], t["tax"]) == (buy_fee + sell_fee, tax)
    assert t["net_pnl"] == -60000 - buy_fee - sell_fee - tax
    assert float(t["realized_r"]) == pytest.approx(t["net_pnl"] / (5000 * 10), abs=1e-3)
    assert t["close_reason"] == "stop" and t["cost_source"] == "estimated"


def test_restart_checks_stops_before_order_sync(settings, rules, notes, clock):
    """프로세스 강제 종료 후 재시작 → 손절 점검이 대조·구독보다 먼저 실행된다."""
    from src.engine.main import Engine

    broker = FakeBroker(clock=clock)
    store = Store()
    # 이전 프로세스가 남긴 상태: 봇 포지션 10주, 손절가 65,000
    store.upsert_instrument(CODE, "삼성전자", "KOSPI", True, None)
    om = OrderManager(broker, store, CostModel(rules, 0.00015, lambda c: "KOSPI"), notes, clock)
    o = buy(om)
    broker.fill(o["order_no"], 10, 70000)
    om.sync(clock().date())
    broker.set_quote(CODE, 60000)                          # 죽어 있는 동안 손절가 아래로
    broker.calls.clear()

    eng = Engine(settings, broker, store, rules, notes, clock=clock,
                 rules_loader=lambda: rules, calendar_saver=lambda *a, **k: None)
    eng.startup()
    calls = [c for c in broker.calls if not c.startswith("quote")]
    first_sell = calls.index(f"place:{CODE}:sell:10")
    assert calls[:first_sell] == ["holdings"]              # 잔고 → 즉시 손절, 대조는 그 뒤
    assert "daily_orders" in calls[first_sell:]


def test_kill_level1_blocks_entries_but_stops_work(settings, rules, notes, clock):
    from src.engine.main import Engine

    broker = FakeBroker(clock=clock)
    broker.set_quote(CODE, 70000)
    store = Store()
    eng = Engine(settings, broker, store, rules, notes, clock=clock,
                 rules_loader=lambda: rules, calendar_saver=lambda *a, **k: None)
    eng.startup()
    eng.kill.auto_trip("test")
    assert eng.entries.run(clock()) == []
    assert not any(c.startswith("place") for c in broker.calls)

    o = buy(eng.orders)                                    # (손절 경로 확인용 포지션)
    broker.fill(o["order_no"], 10, 70000)
    eng.orders.sync(clock().date())
    broker.set_quote(CODE, 60000)
    assert len(eng.stops.tick(can_trade=True)) == 1


def test_external_order_is_imported_and_locked(env, notes):
    broker, store, om, stops, rec, kill, clock = env
    ack = broker.place_order(CODE, "buy", 3, "limit", 70000)   # HTS 에서 사람이 낸 주문
    broker.fill(ack.order_no, 3, 70000)
    om.sync(clock().date())
    assert CODE in store.locked_symbols()
    assert store.positions()[CODE]["qty"] == 3
    assert any("봇이 내지 않은 주문" in n for n in notes)


def test_unmanaged_holding_is_not_sold(env):
    broker, store, om, stops, rec, kill, clock = env
    broker.set_holding("000660", 5, 200000)
    broker.set_quote("000660", 100000)
    assert stops.tick(can_trade=True) == []
    res = rec.run(clock().date())
    assert res.unmanaged == ["000660"] and res.clean and kill.level() == 0


def test_cancel_record_of_own_order_is_not_external(env, notes):
    """증권사 목록에 우리 주문의 취소 레코드가 따로 와도 외부 주문으로 보지 않는다."""
    from dataclasses import replace

    broker, store, om, stops, rec, kill, clock = env
    o = buy(om)
    real = broker.daily_orders(clock().date())[0]
    cancel_row = replace(real, order_no="9999999999", orig_order_no=o["order_no"],
                         is_cancel_order=False)
    broker.daily_orders = lambda d: [real, cancel_row]
    om.sync(clock().date())
    assert CODE not in store.locked_symbols()
    assert not any("봇이 내지 않은 주문" in n for n in notes)

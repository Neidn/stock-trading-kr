"""Engine-level tests: entries, kill-switch transitions, strategy loading."""

from __future__ import annotations

from datetime import time, timedelta

import numpy as np
import pandas as pd
import pytest

from src.broker.fake_broker import FakeBroker
from src.execution import order_state as st
from src.execution.store import Store
from src.signal.base_strategy import BaseStrategy, SignalResult
from src.utils.time_utils import ymd

CODE = "005930"


class StubStrategy(BaseStrategy):
    """Fires one long signal with a fixed stop; records the frame it saw."""

    seen: pd.DataFrame | None = None

    def get_name(self):
        return "stub"

    def get_min_candles(self):
        return 3

    def get_timeframe(self):
        return "1d"

    def generate_signal(self, df, symbol):
        StubStrategy.seen = df
        p = self.params
        return SignalResult(signal_type=p.get("type", "long"), strength_score=2,
                            entry_price=70000, sl=p.get("sl", 65000), tp1=80000, reason="stub")


def candles(clock, n=10):
    days = [clock().date() - timedelta(days=n - i) for i in range(n)] + [clock().date()]
    return pd.DataFrame({"date": [ymd(d) for d in days], "open": 70000.0, "high": 71000.0,
                         "low": 69000.0, "close": 70000.0, "volume": 1e6})


@pytest.fixture
def engine(db, settings, rules, notes, clock):
    from src.engine.main import Engine

    broker = FakeBroker(clock=clock)
    broker.set_quote(CODE, 70050)
    broker.candles[CODE] = candles(clock)
    eng = Engine(settings, broker, Store(), rules, notes, clock=clock,
                 rules_loader=lambda: rules, calendar_saver=lambda d, v: None)
    eng.startup()
    return eng, broker


def use_stub(eng, **params):
    eng.entries.strategies = [StubStrategy(params)]


def test_entry_uses_completed_candles_and_rounds_buy_price_down(engine, clock):
    eng, broker = engine
    use_stub(eng)
    orders = eng.entries.run(clock())
    assert len(orders) == 1
    o = orders[0]
    assert o["price"] == 70000                           # 70,050 → 매수 내림
    assert o["qty"] == 10                                # 50,000 / (70,000 − 65,000)
    assert (o["stop_price"], o["take_profit"]) == (65000, 80000)
    assert ymd(clock().date()) not in set(StubStrategy.seen["date"])   # 오늘 미완성 봉 제외
    assert Store().positions() == {}                     # 접수만으로 포지션 없음


def test_short_signal_is_recorded_as_blocked(engine, clock):
    eng, broker = engine
    use_stub(eng, type="short")
    assert eng.entries.run(clock()) == []
    from src.db.connection import db_conn
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT block_reason FROM signals")
        assert cur.fetchone()["block_reason"] == "long-only"


def test_upper_limit_blocks_entry(engine, clock):
    eng, broker = engine
    use_stub(eng)
    broker.set_quote(CODE, 91000, base_price=70000, upper_limit=91000)
    assert eng.entries.run(clock()) == []


def test_halted_symbol_blocks_entry(engine, clock):
    eng, broker = engine
    use_stub(eng)
    broker.set_quote(CODE, 70000, block_reasons=("거래정지",))
    assert eng.entries.run(clock()) == []


def test_stale_entry_is_cancelled(engine, clock):
    eng, broker = engine
    use_stub(eng)
    o = eng.entries.run(clock())[0]
    clock.advance(seconds=eng.s.engine.entry_order_timeout_sec + 1)
    eng._cancel_stale_entries(clock())
    eng.orders.sync(clock().date())
    assert Store().get_order(o["client_id"])["state"] == st.CANCELLED


def test_kill_level3_cancels_then_liquidates(engine, clock):
    eng, broker = engine
    use_stub(eng)
    o = eng.entries.run(clock())[0]
    broker.fill(o["order_no"], 10, 70000)
    eng.orders.sync(clock().date())
    tp_order = eng.orders.submit(code=CODE, side="sell", qty=10, order_type="limit", price=90000,
                                 purpose="take_profit")
    eng.kill.set(3, "test", "pytest")
    eng.step()                                            # 전환 시 1회 전부 취소 (같은 step 의 sync 가 확정)
    assert Store().get_order(tp_order["client_id"])["state"] == st.CANCELLED
    assert not [s for s in Store().open_orders(side="sell")]   # 취소 확정 전엔 매도를 쌓지 않았다
    eng._due.clear()
    eng.step()                                            # 그다음 전량 시장가 매도
    sells = [s for s in Store().open_orders(side="sell") if s["purpose"] == "kill_switch"]
    assert len(sells) == 1 and sells[0]["order_type"] == "market" and sells[0]["qty"] == 10


def test_all_ported_strategies_load_and_run():
    from src.signal.registry import load_strategy

    rng = np.random.default_rng(0)
    close = 50000 + np.cumsum(rng.normal(0, 500, 400))
    df = pd.DataFrame({"open": close, "high": close + 300, "low": close - 300, "close": close,
                       "volume": rng.uniform(1e5, 1e6, 400)})
    for name in ("ema_crossover", "rsi_macd", "bb_rsi_chartart", "momentum"):
        s = load_strategy(name)
        assert s.get_timeframe() == "1d"
        assert s.generate_signal(df, CODE).signal_type in ("long", "short", "none", "hold")


def test_signal_time_gate(engine, clock):
    eng, broker = engine
    use_stub(eng)
    clock.now = clock.now.replace(hour=9, minute=5)
    eng.step()
    assert not eng.signals_done
    clock.now = clock.now.replace(hour=9, minute=10)
    eng.step()
    assert eng.signals_done
    assert any(c.startswith(f"place:{CODE}:buy") for c in broker.calls)
    assert time(9, 10) <= clock().time()

"""모의투자 운영에 필요한 동작: 휴장일 API 없이 개장 확인, 낙폭 정지, 자산 기록, 순자산 기준 사이징."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta

import pytest

from src.broker.fake_broker import FakeBroker
from src.execution.store import Store
from src.market.rules import Session
from src.utils.time_utils import KST, ymd
from tests.unit.test_engine import CODE, candles, use_stub

PREV_DAY = date(2026, 10, 9)


def make_engine(settings, rules, notes, clock, *, open_day, with_today=True):
    from src.engine.main import Engine

    broker = FakeBroker(clock=clock, open_day=open_day)
    broker.set_quote(CODE, 70050)
    df = candles(clock)
    broker.candles[CODE] = df if with_today else df[df["date"] != ymd(clock().date())]
    saved = []
    eng = Engine(settings, broker, Store(), rules, notes, clock=clock,
                 rules_loader=lambda: rules,
                 calendar_saver=lambda d, v, source="kis": saved.append((d, v, source)))
    return eng, broker, saved


@pytest.fixture
def unknown_rules(rules):
    rules.calendar.clear()          # 휴장일 API 가 답하지 않은 상태 (모의투자)
    return rules


class TestCalendarFallback:
    def test_open_confirmed_by_todays_candle(self, db, settings, unknown_rules, notes, clock):
        clock.now = clock.now.replace(hour=8, minute=50)
        eng, broker, saved = make_engine(settings, unknown_rules, notes, clock, open_day=None)
        eng.startup()
        assert unknown_rules.session(clock()) == Session.UNKNOWN
        clock.now = clock.now.replace(hour=9, minute=1)
        eng.step()
        assert unknown_rules.is_open_day(clock().date()) is None      # 지연 전에는 확인 안 함
        clock.now = clock.now.replace(hour=9, minute=3)
        eng.step()
        assert unknown_rules.is_open_day(clock().date()) is True
        assert saved[-1] == (clock().date(), True, "candle")

    def test_entries_run_after_confirmation(self, db, settings, unknown_rules, notes, clock):
        clock.now = clock.now.replace(hour=9, minute=3)
        eng, broker, _ = make_engine(settings, unknown_rules, notes, clock, open_day=None)
        use_stub(eng)
        eng.startup()
        clock.now = clock.now.replace(hour=9, minute=10)
        eng.step()
        assert eng.signals_done
        assert any(c.startswith(f"place:{CODE}:buy") for c in broker.calls)

    def test_no_candle_keeps_unknown_and_stops_still_work(self, db, settings, unknown_rules,
                                                          notes, clock):
        clock.now = clock.now.replace(hour=9, minute=3)
        eng, broker, _ = make_engine(settings, unknown_rules, notes, clock, open_day=None,
                                     with_today=False)
        use_stub(eng)
        broker.set_holding(CODE, 10, 70000)
        Store().adopt_position(CODE, 10, 70000, stop_price=69000)
        broker.set_quote(CODE, 68000)
        eng.startup()
        clock.now = clock.now.replace(hour=9, minute=40)
        eng._due.clear()
        eng.step()
        # 휴장으로 단정하지 않는다 → 손절은 나가고, 신규 진입은 막힌다.
        assert unknown_rules.is_open_day(clock().date()) is None
        assert any(c == f"place:{CODE}:sell:10" for c in broker.calls)
        assert not eng.signals_done
        assert any("오늘 일봉이 없음" in n for n in notes)

    def test_weekend_is_closed_without_api(self, db, settings, unknown_rules, notes):
        from tests.conftest import Clock
        sat = Clock(datetime(2026, 10, 17, 10, 0, tzinfo=KST))
        eng, broker, saved = make_engine(settings, unknown_rules, notes, sat, open_day=None)
        eng.startup()
        assert saved[-1] == (sat().date(), False, "weekday")
        assert unknown_rules.session(sat()) == Session.CLOSED


class TestEquityAndDrawdown:
    def test_drawdown_trips_level1_on_startup(self, db, settings, rules, notes, clock):
        Store().record_equity(PREV_DAY, 12_500_000, 12_500_000)   # 고점
        eng, broker, _ = make_engine(settings, rules, notes, clock, open_day=True)
        eng.startup()                                            # FakeBroker 자산 10,000,000 (−20%)
        assert eng.kill.level() == 1
        assert "drawdown" in Store().kill_level()[1]

    def test_small_drawdown_does_not_trip(self, db, settings, rules, notes, clock):
        Store().record_equity(PREV_DAY, 10_500_000, 10_500_000)   # −4.8%
        eng, broker, _ = make_engine(settings, rules, notes, clock, open_day=True)
        eng.startup()
        assert eng.kill.level() == 0

    def test_end_of_day_records_snapshot(self, db, settings, rules, notes, clock):
        eng, broker, _ = make_engine(settings, rules, notes, clock, open_day=True)
        eng.startup()
        clock.now = clock.now.replace(hour=15, minute=40)
        eng.end_of_day(clock().date())
        assert Store().equity_peak() == 10_000_000
        assert any("고점 대비" in n for n in notes)

    def test_entry_sized_by_equity_pct(self, db, settings, rules, notes, clock):
        # 순자산 10,000,000 × 5% = 500,000 → 70,000원 7주 (리스크 기준이면 10주)
        eng, broker, _ = make_engine(settings, rules, notes, clock, open_day=True)
        eng.entries.risk = dataclasses.replace(eng.entries.risk, max_position_pct=0.05)
        use_stub(eng)
        eng.startup()
        orders = eng.entries.run(clock())
        assert orders[0]["qty"] == 7

    def test_no_equity_no_entries(self, db, settings, rules, notes, clock):
        eng, broker, _ = make_engine(settings, rules, notes, clock, open_day=True)
        use_stub(eng)
        eng.startup()
        broker.cash = 0
        assert eng.entries.run(clock()) == []
        assert not any(c.startswith("place:") for c in broker.calls)


def test_signal_frame_excludes_today(db, settings, rules, notes, clock):
    """일봉 확인용으로 오늘 봉을 쓰더라도 신호 계산에는 여전히 완성된 봉만 쓴다."""
    eng, broker, _ = make_engine(settings, rules, notes, clock, open_day=True)
    use_stub(eng)
    eng.startup()
    eng.entries.run(clock())
    from tests.unit.test_engine import StubStrategy
    assert ymd(clock().date()) not in set(StubStrategy.seen["date"])
    assert ymd(clock().date() - timedelta(days=1)) in set(StubStrategy.seen["date"])


def test_export_for_trade_history_analysis(db, settings, rules, notes, clock, tmp_path):
    import csv

    from src.audit.export import FILL_COLUMNS, export

    eng, broker, _ = make_engine(settings, rules, notes, clock, open_day=True)
    use_stub(eng)
    eng.startup()
    o = eng.entries.run(clock())[0]
    broker.fill(o["order_no"], 10, 70000)
    eng.orders.sync(clock().date())
    broker.set_holding("000660", 5, 100000)          # 봇이 사지 않은 종목 (외부)
    Store().record_equity(clock().date(), 10_000_000, 9_300_000)

    n = export(tmp_path, 30)
    assert n == {"fills": 1, "equity_days": 1}
    rows = list(csv.DictReader(open(tmp_path / "fills.csv", encoding="utf-8")))
    assert tuple(rows[0]) == FILL_COLUMNS
    r = rows[0]
    assert (r["symbol"], r["side"], r["qty"], r["order_type"], r["purpose"]) == \
        (CODE, "buy", "10", "limit", "entry")
    assert r["symbol"].startswith("00")               # 종목코드 앞자리 0 유지
    assert int(r["fee"]) > 0                          # 수수료(추정) 포함


def test_restart_does_not_rerun_entries_same_day(db, settings, rules, notes, clock):
    eng, broker, _ = make_engine(settings, rules, notes, clock, open_day=True)
    use_stub(eng)
    eng.startup()
    assert eng.entries.run(clock())
    clock.advance(seconds=eng.s.engine.entry_order_timeout_sec + 1)
    eng._cancel_stale_entries(clock())                 # 미체결 → 포기
    eng.orders.sync(clock().date())

    eng2, _, _ = make_engine(settings, rules, notes, clock, open_day=True)
    eng2.startup()                                      # 같은 날 재시작
    assert eng2.signals_done


def test_calendar_probe_backs_off_and_stops_after_close(db, settings, unknown_rules, notes, clock):
    clock.now = clock.now.replace(hour=9, minute=40)
    eng, broker, _ = make_engine(settings, unknown_rules, notes, clock, open_day=None,
                                 with_today=False)
    eng.startup()
    probes = []
    real = broker.daily_candles
    broker.daily_candles = lambda code, n: probes.append(clock()) or real(code, n)
    eng._due.clear()
    for _ in range(5):                       # 포기 후: 10분 간격
        eng._confirm_calendar(clock())
        clock.advance(minutes=2)
    assert len(probes) == 1
    clock.now = clock.now.replace(hour=15, minute=31)
    eng._due.clear()
    eng._confirm_calendar(clock())           # 장 마감 후에는 보지 않는다
    assert len(probes) == 1

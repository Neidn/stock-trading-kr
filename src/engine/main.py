"""Trading engine — one process, one pod (k8s Deployment, replicas 1, Recreate).

손절·체결 동기화·진입이 같은 프로세스에 있다. 매도 주문을 내는 주체가 둘이면 같은
수량을 두 번 팔 수 있어서 나누지 않았다.

재시작 복구 순서 (stock-trading-execution references/order-lifecycle.md 3절)
  1. 토큰 확보 (DB 공유 캐시 우선)
  2. 증권사 잔고 → 손절가 점검 → 손절 대상이면 즉시 매도      ← 가장 먼저
  3. 미체결·당일 체결 대조
  4. 잔고 대조
  5. (실시간 구독 — 1차 범위는 REST 폴링)
  6. 그다음에 신규 진입 활성화
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import date, datetime

from src.broker.base import Broker, BrokerAuthError, BrokerError
from src.engine.entries import EntryPlanner
from src.execution import order_state as st
from src.execution.costs import CostModel
from src.execution.order_manager import OrderManager
from src.execution.reconcile import Reconciler
from src.execution.store import Store
from src.market.rules import MarketRules, Session, save_calendar_day
from src.monitoring.health import HealthState
from src.risk.kill_switch import KillSwitch
from src.risk.sizing import worst_case_loss
from src.risk.stop_monitor import StopMonitor
from src.signal.registry import load_strategies
from src.utils.config import Settings
from src.utils.time_utils import now_kst, parse_hhmm

log = logging.getLogger(__name__)

TRADING_SESSIONS = (Session.CONTINUOUS, Session.CLOSING_AUCTION)


class Engine:
    def __init__(self, settings: Settings, broker: Broker, store: Store, rules: MarketRules,
                 notify, *, clock=now_kst, health: HealthState | None = None,
                 rules_loader=MarketRules.load, calendar_saver=save_calendar_day):
        self.s = settings
        self.broker = broker
        self.store = store
        self.rules = rules
        self.notify = notify
        self.clock = clock
        self.health = health or HealthState()
        self._load_rules = rules_loader
        self._save_calendar = calendar_saver

        self.kill = KillSwitch(store, notify)
        self.costs = CostModel(rules, settings.risk.fee_rate, self._market_of)
        self.orders = OrderManager(broker, store, self.costs, notify, clock)
        self.stops = StopMonitor(broker, store, self.orders, self.kill,
                                 daily_loss_limit_krw=settings.risk.daily_loss_limit_krw,
                                 stale_sec=settings.engine.quote_stale_sec,
                                 notify=notify, clock=clock)
        self.reconciler = Reconciler(broker, store, self.orders, self.kill, notify)
        self.entries = EntryPlanner(broker, store, self.orders, self.kill, rules,
                                    load_strategies(settings.engine.strategies,
                                                    settings.engine.strategy_params),
                                    settings.engine, settings.risk, notify)

        self.day: date | None = None
        self.signals_done = False
        self.eod_done = False
        self.last_level = 0
        self.errors = 0
        self._due: dict[str, float] = {}
        self._running = True

    # ── helpers ──────────────────────────────────────────────────────────────
    def _market_of(self, code: str) -> str:
        inst = self.store.instrument(code)
        return inst["market"] if inst and inst["market"] else ""

    def _due_now(self, name: str, every: float) -> bool:
        now = time.monotonic()
        if now >= self._due.get(name, 0.0):
            self._due[name] = now + every
            return True
        return False

    def _session(self, now: datetime) -> tuple[Session, bool]:
        """(세션, 보호 주문 가능 여부). 캘린더를 모르면 손절만 허용한다."""
        sess = self.rules.session(now)
        if sess == Session.UNKNOWN:
            phase = self.rules.session(now, assume_open=True)
            return sess, phase in TRADING_SESSIONS
        return sess, sess in TRADING_SESSIONS

    # ── 시작 ────────────────────────────────────────────────────────────────
    def startup(self) -> None:
        now = self.clock()
        log.info("startup: env=%s universe=%d strategies=%s", self.s.kis.env,
                 len(self.s.engine.universe), ",".join(self.s.engine.strategies))
        self.daily_prep(now.date())
        _, can_exit = self._session(now)
        # 2. 손절 점검이 다른 어떤 것보다 먼저다.
        self.stops.tick(can_trade=can_exit)
        # 3-4. 주문·체결·잔고 대조
        self.reconciler.run(now.date())
        self.last_level = self.kill.level()
        self.health.ready = True
        self.notify(f"▶️ 엔진 시작 ({self.s.kis.env}) · 킬 스위치 {self.last_level}단계", key=None)

    def daily_prep(self, d: date) -> None:
        """하루 한 번: 휴장일 조회 → 규칙 다시 읽기 → 종목 상태(정지·경고) 갱신."""
        try:
            open_day = self.broker.is_market_open_day(d)
        except BrokerError as exc:
            # 휴장일 조회는 하루 1회 권장 API 다. 실패해도 루프에서 재시도하지 않는다.
            log.warning("holiday lookup failed: %s", exc)
            open_day = None
        if open_day is not None:
            self._save_calendar(d, open_day)
        else:
            self.notify(f"⚠️ {d} 개장일 여부 확인 실패 — 신규 진입 중지, 손절만 동작", key=f"cal:{d}")
        self.rules = self._load_rules()
        if open_day is not None:
            self.rules.calendar[d] = open_day
        self.costs.rules = self.rules
        self.entries.rules = self.rules

        codes = set(self.s.engine.universe) | set(self.store.positions())
        for code in sorted(codes):
            try:
                q = self.broker.quote(code)
            except BrokerError as exc:
                log.warning("quote failed for %s: %s", code, exc)
                continue
            reason = ",".join(q.block_reasons) or None
            self.store.upsert_instrument(code, q.name, q.market, reason is None, reason)
            if reason and code in self.store.positions():
                self.notify(f"⚠️ 보유 종목 {code} 상태: {reason}", key=f"inst:{code}:{reason}")
        self.day = d
        self.signals_done = False
        self.eod_done = False

    # ── 루프 ────────────────────────────────────────────────────────────────
    def step(self) -> None:
        now = self.clock()
        if now.date() != self.day:
            self.daily_prep(now.date())
        sess, can_exit = self._session(now)
        e = self.s.engine

        self._apply_kill_transitions(can_exit)

        if sess not in (Session.CLOSED, Session.AFTER_CLOSE) and self._due_now("stop", e.stop_poll_sec):
            self.stops.tick(can_trade=can_exit)
            self.stops.check_daily_loss(self.store.realized_net_on(now.date()))

        if can_exit and self._due_now("orders", e.order_poll_sec):
            self.orders.sync(now.date())
            self._cancel_stale_entries(now)

        if (sess == Session.CONTINUOUS and not self.signals_done
                and now.time() >= parse_hhmm(e.signal_time)):
            self.signals_done = True
            self.entries.run(now)

        if can_exit and self._due_now("reconcile", e.reconcile_sec):
            self.reconciler.run(now.date())

        if sess == Session.AFTER_CLOSE and not self.eod_done:
            self.end_of_day(now.date())

        self.health.beat(session=sess.value, kill=self.last_level)

    def _apply_kill_transitions(self, can_exit: bool) -> None:
        level = self.kill.level()
        if level >= 2 and self.last_level < 2:
            # 2·3단계로 올라가는 순간 한 번만 전부 취소한다. 이후 손절 주문은 다시 낼 수 있다.
            n = self.orders.cancel_all_open()
            self.notify(f"킬 스위치 {level}단계: 미체결 {n}건 취소 요청", key=None)
        if level != self.last_level:
            log.warning("kill switch level %d → %d", self.last_level, level)
        self.last_level = level

    def _cancel_stale_entries(self, now: datetime) -> None:
        timeout = self.s.engine.entry_order_timeout_sec
        for o in self.store.open_orders(side="buy"):
            if (o["purpose"] == "entry" and o["state"] in (st.ACCEPTED, st.PARTIAL)
                    and (now - o["created_at"]).total_seconds() > timeout):
                log.info("cancel stale entry %s (%s)", o["order_no"], o["code"])
                self.orders.cancel(o)

    def end_of_day(self, d: date) -> None:
        leftover = self.orders.close_session(d)
        res = self.reconciler.run(d, session_over=True)
        sources = self.store.fill_sources_on(d)
        realized = self.store.realized_net_on(d)
        positions = list(self.store.positions().values())
        by_code = {h.code: h.current_price for h in self.stops.last_holdings}
        for p in positions:
            p["last_price"] = by_code.get(p["code"])
        wc = worst_case_loss(positions, self.s.risk.gap_scenario_pct, self.s.risk.daily_loss_limit_krw)
        lines = [
            f"📊 장 마감 {d} ({self.s.kis.env})",
            f"실현 net(추정 비용 차감): {realized:,}원",
            f"체결 경로: {sources or '없음'}",
            f"보유 {len(positions)}종목 · 최대손실 시나리오 {wc['total']:,}원 "
            f"(일한도 {wc['daily_loss_limit']:,} + 갭 {wc['gap_scenario']:,})",
            f"대조: {'불일치 0' if res.clean else f'불일치 {len(res.mismatches)}'}",
        ]
        if leftover:
            lines.append(f"⚠️ 마감 후에도 open 상태 주문 {len(leftover)}건 — 확인 필요")
        self.notify("\n".join(lines), key=None)
        self.eod_done = True

    def run_forever(self) -> None:
        self.startup()
        max_err = self.s.risk.max_consecutive_errors
        while self._running:
            try:
                self.step()
                self.errors = 0
            except BrokerAuthError:
                raise
            except BrokerError as exc:
                self.errors += 1
                log.warning("loop error %d/%d: %s", self.errors, max_err, exc)
                if self.errors >= max_err:
                    self.kill.auto_trip(f"{self.errors} consecutive broker errors: {exc}")
            time.sleep(1)

    def stop(self, *_):
        log.info("shutdown requested")
        self._running = False


def main() -> None:
    from src.broker.kis_broker import KisBroker
    from src.broker.kis_client import KisClient
    from src.broker.token_store import PgTokenStore
    from src.db.migrate import migrate
    from src.monitoring.health import serve
    from src.monitoring.telegram import Notifier
    from src.utils.config import load_settings
    from src.utils.logger import setup_logging

    setup_logging()
    settings = load_settings()
    migrate()
    notify = Notifier(settings.telegram_token, settings.telegram_chat_id,
                      prefix="[모의] " if settings.kis.env == "demo" else "[실전] ")
    broker = KisBroker(KisClient(settings.kis, PgTokenStore()))
    health = HealthState()
    serve(health, settings.engine.health_port)
    engine = Engine(settings, broker, Store(), MarketRules.load(), notify, health=health)
    signal.signal(signal.SIGTERM, engine.stop)
    signal.signal(signal.SIGINT, engine.stop)
    try:
        engine.run_forever()
    except BrokerAuthError as exc:
        # 토큰 실패는 치명 오류 — 손절이 멈춘다. 알리고 죽는다 (k8s 가 재시작, 토큰은 DB 캐시).
        notify(f"🚨 KIS 인증 실패 — 엔진 종료. 손절 감시가 멈췄습니다: {exc}", key=None)
        log.critical("auth failure: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()

"""Client-side stop-loss / take-profit monitor.

국내주식 손절은 거래소에 걸려 있지 않다. 이 프로세스가 살아 있을 때만 존재한다.

* 기준은 **증권사 잔고**(수량·현재가)다. 잔고 조회 1회로 보유 종목 전부의 현재가를 받는다.
* 봇 포지션(positions 테이블)이 있는 종목만 관리한다. 봇이 사지 않은 기존 보유 종목은
  팔지 않는다 — 대조(reconcile)가 경보만 낸다.
* 같은 종목에 열린 매도 주문(UNKNOWN 포함)이 있으면 재주문하지 않는다.
  동시호가·VI 구간의 시장가 매도는 단일가로 모아 체결되므로 재주문을 쌓으면 안 된다.
* 잔고 조회가 quote_stale_sec 이상 실패하면 경보 + 킬 스위치 1단계.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.broker.base import AccountSummary, Broker, BrokerError, Holding
from src.execution.order_manager import DuplicateOrderBlocked, OrderManager
from src.execution.store import Store
from src.risk.kill_switch import KillSwitch
from src.utils.time_utils import now_kst

log = logging.getLogger(__name__)


class StopMonitor:
    def __init__(self, broker: Broker, store: Store, orders: OrderManager, kill: KillSwitch,
                 *, daily_loss_limit_krw: int, stale_sec: int, notify=None, clock=now_kst):
        self.broker = broker
        self.store = store
        self.orders = orders
        self.kill = kill
        self.daily_loss_limit = daily_loss_limit_krw
        self.stale_sec = stale_sec
        self.notify = notify if notify is not None else (lambda *a, **k: None)
        self.clock = clock
        self.last_ok: datetime | None = None
        self.last_holdings: list[Holding] = []
        self.last_summary: AccountSummary | None = None

    def tick(self, *, can_trade: bool) -> list[dict]:
        """Check every bot position once. Returns the exit orders submitted.

        can_trade=False (장 시간 외)이면 점검·경보만 하고 주문하지 않는다.
        """
        now = self.clock()
        try:
            holdings, summary = self.broker.holdings()
        except BrokerError as exc:
            log.warning("holdings query failed: %s", exc)
            if self.last_ok and (now - self.last_ok).total_seconds() > self.stale_sec:
                self.notify(f"⚠️ 잔고·시세 갱신 {self.stale_sec}s 이상 실패 — 손절 감시 공백",
                            key="stale")
                self.kill.auto_trip(f"holdings stale > {self.stale_sec}s ({exc})")
            return []
        self.last_ok = now
        self.last_holdings = holdings
        self.last_summary = summary

        by_code = {h.code: h for h in holdings}
        positions = self.store.positions()
        level = self.kill.level()
        submitted: list[dict] = []

        for code, pos in positions.items():
            h = by_code.get(code)
            if h is None or h.qty <= 0:
                continue        # 대조가 처리한다 (로컬엔 있는데 증권사엔 없음)
            price = h.current_price
            lq = int(pos["qty"])
            if (h.qty != lq and (h.qty % lq == 0 or lq % h.qty == 0)
                    and not self.store.open_orders(code=code)):
                # 미체결 없이 수량이 정수배로 달라졌다 → 분할·무상증자 의심. 가격도 1/N 이 되어
                # 손절가 아래로 "폭락"한 것처럼 보인다. 손절을 내지 않고 사람에게 넘긴다.
                self.notify(f"⚠️ {code} 수량 정수배 불일치 (로컬 {lq}, 증권사 {h.qty}) — "
                            "기업행위 의심, 손절 보류. 손절가 재설정 필요", key=f"ca:{code}")
                continue
            reason = None
            if level >= 3:
                reason = "kill_switch"
            elif pos["stop_price"] is None:
                self.notify(f"⚠️ {code} 손절가 없음 — 수동 확인 필요", key=f"nostop:{code}")
            elif price <= int(pos["stop_price"]):
                reason = "stop"
            elif pos["take_profit"] is not None and price >= int(pos["take_profit"]):
                reason = "take_profit"
            if reason is None:
                continue
            if not can_trade:
                self.notify(f"⚠️ {code} {reason} 조건 충족 (현재가 {price:,}) — 장 시간 외라 대기",
                            key=f"wait:{code}:{reason}")
                continue
            # 매도 수량은 증권사 기준: 매도가능수량, 로컬 수량 중 작은 값
            qty = min(h.sellable_qty, int(pos["qty"]))
            if qty <= 0:
                continue
            if self.store.open_orders(code=code, side="sell"):
                continue        # 이미 매도 주문이 있다 (단일가 대기 포함). 쌓지 않는다.
            try:
                order = self.orders.submit(code=code, side="sell", qty=qty, order_type="market",
                                           price=None, purpose=reason,
                                           strategy_name=pos["strategy_name"])
            except DuplicateOrderBlocked:
                continue
            submitted.append(order)
            self.notify(f"{'🔻 손절' if reason == 'stop' else '청산'} {code} {qty}주 시장가 "
                        f"(현재가 {price:,}, 손절가 {pos['stop_price']}) → {order['state']}", key=None)
        return submitted

    def bot_unrealized(self) -> int:
        """봇 포지션의 미실현 손익 (증권사 현재가 × 로컬 수량 − 평균단가). 수수료 제외."""
        by_code = {h.code: h for h in self.last_holdings}
        total = 0.0
        for code, pos in self.store.positions().items():
            h = by_code.get(code)
            if h:
                total += (h.current_price - float(pos["avg_price"])) * int(pos["qty"])
        return round(total)

    def check_daily_loss(self, realized_today: int) -> int:
        """봇 기준 일손실 = 오늘 실현 net + 보유분 미실현. 한도 초과 시 1단계. 추정치다."""
        pnl = realized_today + self.bot_unrealized()
        if pnl <= -self.daily_loss_limit:
            self.kill.auto_trip(f"daily loss {pnl:,} <= -{self.daily_loss_limit:,}")
        return pnl

    def check_drawdown(self, peak: int, halt_pct: float) -> float | None:
        """계좌 고점(일별 스냅샷 최대) 대비 현재 총평가금액 낙폭. 넘으면 1단계.

        aoa 사례의 "큰 낙폭 뒤 축소"를 옮긴 것이다. 자동으로는 1단계(신규 진입 중지)까지만
        걸고, 해제는 사람이 원인을 본 뒤 CLI 로 한다. 입출금이 섞이면 틀린다 — 출금 직후에는
        사람이 확인한다.
        """
        cur = self.last_summary.total_eval if self.last_summary else 0
        if peak <= 0 or cur <= 0:
            return None
        dd = 1 - cur / peak
        if dd >= halt_pct:
            self.kill.auto_trip(f"drawdown {dd:.1%} >= {halt_pct:.0%} (peak {peak:,}, now {cur:,})")
        return dd

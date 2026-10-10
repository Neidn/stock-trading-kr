"""Order submission and synchronisation with the broker.

규칙 (stock-trading-execution 1절)
  * 주문 접수 응답으로 포지션을 만들지 않는다. 포지션은 sync() 가 반영하는 체결로만 바뀐다.
  * 타임아웃 → UNKNOWN. 같은 종목·방향에 새 주문을 내지 않는다. 증권사 조회로 확정한다.
  * 거부는 재시도하지 않는다. 원인을 기록한다.
  * 취소 요청을 보냈다고 CANCELLED 로 바꾸지 않는다. CANCELLING 에서 조회로 확정한다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from src.broker.base import Broker, BrokerRejected, BrokerTransient, OrderStatus
from src.execution import order_state as st
from src.execution.costs import CostModel
from src.execution.store import Store
from src.utils.time_utils import now_kst

log = logging.getLogger(__name__)

# UNKNOWN 주문을 "접수 안 됨"으로 확정하기 전 기다리는 시간
UNKNOWN_GRACE = timedelta(seconds=60)


class DuplicateOrderBlocked(RuntimeError):
    """같은 종목·방향에 열린(또는 UNKNOWN) 주문이 있다."""


class OrderManager:
    def __init__(self, broker: Broker, store: Store, costs: CostModel, notify=None,
                 clock=now_kst):
        self.broker = broker
        self.store = store
        self.costs = costs
        self.notify = notify if notify is not None else (lambda *a, **k: None)
        self.clock = clock

    # ── 제출 ────────────────────────────────────────────────────────────────
    def submit(self, *, code: str, side: str, qty: int, order_type: str, price: int | None,
               purpose: str, strategy_name: str | None = None, stop_price: int | None = None,
               take_profit: int | None = None, initial_risk: float | None = None) -> dict:
        if self.store.open_orders(code=code, side=side):
            raise DuplicateOrderBlocked(f"{code} {side}: open/unknown order exists")
        now = self.clock()
        order = self.store.create_order(
            code=code, side=side, order_type=order_type, qty=qty, price=price,
            purpose=purpose, strategy_name=strategy_name, trade_date=now.date(),
            stop_price=stop_price, take_profit=take_profit, initial_risk=initial_risk,
            created_at=now,
        )
        cid = order["client_id"]
        try:
            ack = self.broker.place_order(code, side, qty, order_type, price)
        except BrokerRejected as exc:
            self.store.update_order(cid, state=st.REJECTED, reject_reason=str(exc)[:300])
            self.store.event("reject", f"{code} {side} {qty} 거부: {exc}", {"purpose": purpose})
            log.warning("order rejected %s %s %d: %s", code, side, qty, exc)
            return self.store.get_order(cid)
        except BrokerTransient as exc:
            # 접수됐는지 모른다. 재시도하지 않는다 — sync() 가 조회로 확정한다.
            self.store.update_order(cid, state=st.UNKNOWN, reject_reason=str(exc)[:300])
            self.store.event("unknown", f"{code} {side} {qty} 응답 불명: {exc}")
            log.warning("order outcome unknown %s %s %d: %s", code, side, qty, exc)
            return self.store.get_order(cid)
        if not ack.order_no:
            self.store.update_order(cid, state=st.UNKNOWN, reject_reason="empty order_no in ack")
            return self.store.get_order(cid)
        self.store.update_order(cid, state=st.ACCEPTED, order_no=ack.order_no, org_no=ack.org_no)
        log.info("order accepted %s %s %d @%s (%s) no=%s", code, side, qty, price, purpose,
                 ack.order_no)
        return self.store.get_order(cid)

    # ── 취소 ────────────────────────────────────────────────────────────────
    def cancel(self, order: dict) -> None:
        if order["state"] not in (st.ACCEPTED, st.PARTIAL) or not order["order_no"]:
            return
        self.store.update_order(order["client_id"], state=st.CANCELLING)
        try:
            self.broker.cancel_order(order["order_no"], order["org_no"] or "")
        except BrokerRejected as exc:
            # 대부분 "이미 체결됨" 경합. 상태는 다음 sync 가 체결 결과로 확정한다.
            log.info("cancel rejected for %s: %s", order["order_no"], exc)
            self.store.update_order(order["client_id"], state=order["state"])
        except BrokerTransient as exc:
            log.warning("cancel outcome unknown for %s: %s", order["order_no"], exc)
            # CANCELLING 유지 — sync 가 확정한다.

    def cancel_all_open(self) -> int:
        n = 0
        for o in self.store.open_orders():
            if o["state"] in (st.ACCEPTED, st.PARTIAL):
                self.cancel(o)
                n += 1
        return n

    # ── 동기화 ───────────────────────────────────────────────────────────────
    def sync(self, trade_date: date, *, source: str = "poll", session_over: bool = False) -> int:
        """Pull today's orders from the broker and apply state changes and fills.

        Returns the number of new fills applied.
        """
        remote = [o for o in self.broker.daily_orders(trade_date) if not o.is_cancel_order]
        by_no = {o.order_no: o for o in remote}
        self._resolve_unknown(remote)
        self._import_external(remote, trade_date)

        n_fills = 0
        for local in self.store.open_orders():
            if not local["order_no"]:
                continue
            status = by_no.get(local["order_no"])
            if status is None:
                if local["trade_date"] < trade_date:
                    # 전일 주문인데 아직 open — 장 마감 처리가 누락됐다.
                    self.store.update_order(local["client_id"], state=st.EXPIRED)
                    self.store.event("alert", f"전일 미확정 주문 {local['order_no']} → EXPIRED")
                    continue
                if self.clock() - local["created_at"] > UNKNOWN_GRACE:
                    self.store.event("alert", f"로컬 open 주문 {local['order_no']} 이 증권사에 없음")
                continue
            n_fills += self._apply_status(local, status, source=source, session_over=session_over)
        return n_fills

    def _apply_status(self, local: dict, status: OrderStatus, *, source: str,
                      session_over: bool) -> int:
        applied = 0
        try:
            delta = st.fill_delta(local["filled_qty"], local["filled_amount"], status)
        except st.FillRegression as exc:
            self.store.lock_symbol(local["code"], "fill regression")
            self.store.event("alert", f"local-only fill: {exc}")
            self.notify(f"⚠️ 로컬에만 있는 체결: {exc}", key=f"regress:{local['order_no']}")
            return 0
        if delta:
            cost = self.costs.cost(local["code"], local["side"], delta.amount, status.ordered_at.date())
            if self.store.apply_fill(order=local, qty=delta.qty, amount=delta.amount,
                                     cum_qty=delta.cum_qty, cum_amount=delta.cum_amount,
                                     executed_at=self.clock(), cost=cost, source=source):
                applied = 1
                log.info("fill %s %s %s +%d (cum %d/%d) amount=%d src=%s", local["code"],
                         local["side"], local["order_no"], delta.qty, delta.cum_qty,
                         status.qty, delta.amount, source)
                self.notify(
                    f"체결 {local['code']} {'매수' if local['side'] == 'buy' else '매도'} "
                    f"{delta.qty}주 @{delta.amount / delta.qty:,.0f} ({local['purpose']})",
                    key=None,
                )
        new = st.next_state(local["state"], status, session_over=session_over)
        if new != local["state"]:
            self.store.update_order(local["client_id"], state=new)
        return applied

    def _resolve_unknown(self, remote: list[OrderStatus]) -> None:
        unknown = [o for o in self.store.open_orders() if o["state"] == st.UNKNOWN]
        if not unknown:
            return
        known = self.store.known_order_nos()
        for local in unknown:
            match = next(
                (r for r in remote
                 if r.order_no not in known and r.code == local["code"] and r.side == local["side"]
                 and r.qty == local["qty"]
                 and (local["price"] is None or r.price in (0, local["price"]))
                 and r.ordered_at >= local["created_at"] - timedelta(seconds=10)),
                None,
            )
            if match:
                self.store.update_order(local["client_id"], state=st.ACCEPTED,
                                        order_no=match.order_no, org_no=match.org_no)
                known.add(match.order_no)
                self.store.event("unknown", f"UNKNOWN 주문 확정: 접수됨 {match.order_no}")
            elif self.clock() - local["created_at"] > UNKNOWN_GRACE:
                self.store.update_order(local["client_id"], state=st.REJECTED,
                                        reject_reason="not found at broker after timeout")
                self.store.event("unknown", f"UNKNOWN 주문 확정: 미접수 {local['code']} {local['side']}")

    def _import_external(self, remote: list[OrderStatus], trade_date: date) -> None:
        """증권사에는 있는데 로컬에 없는 주문 (HTS·앱 수동주문). 로컬에 들이고 종목을 잠근다."""
        known = self.store.known_order_nos()
        unknown_pending = any(o["state"] == st.UNKNOWN for o in self.store.open_orders())
        for r in remote:
            if r.order_no in known:
                continue
            if r.orig_order_no and r.orig_order_no in known:
                # 우리 주문을 원주문으로 하는 정정·취소 레코드다. 외부 주문이 아니다.
                known.add(r.order_no)
                continue
            if unknown_pending:
                # UNKNOWN 확정 전에는 외부 주문으로 단정하지 않는다.
                continue
            self.store.create_order(
                code=r.code, side=r.side, order_type="limit" if r.price else "market",
                qty=r.qty, price=r.price or None, purpose="external", strategy_name=None,
                trade_date=trade_date, client_id=f"ext-{r.order_no}", state=st.ACCEPTED,
                created_at=r.ordered_at,
            )
            self.store.update_order(f"ext-{r.order_no}", order_no=r.order_no, org_no=r.org_no)
            self.store.lock_symbol(r.code, f"external order {r.order_no}")
            self.store.event("alert", f"외부 주문 감지 {r.code} {r.side} {r.qty} ({r.order_no})")
            self.notify(f"⚠️ 봇이 내지 않은 주문 감지: {r.code} {r.side} {r.qty}주 → 종목 잠금",
                        key=f"ext:{r.order_no}")

    # ── 세션 종료 ────────────────────────────────────────────────────────────
    def close_session(self, trade_date: date) -> list[dict]:
        """장 마감 후: 증권사 조회로 확인한 뒤 잔량을 EXPIRED 로. 남은 UNKNOWN 을 돌려준다."""
        self.sync(trade_date, source="reconcile", session_over=True)
        return [o for o in self.store.open_orders()]


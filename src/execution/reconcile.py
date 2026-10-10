"""Broker ↔ local reconciliation (장 시작 전 · 장중 주기 · 재연결 직후 · 장 마감 후).

    증권사 당일 체결     == 로컬 fills          → OrderManager.sync(source='reconcile')
    증권사 미체결        == 로컬 open 주문       → 같은 sync
    증권사 잔고(종목·수량) == 로컬 포지션         → 여기서 비교

* 증권사가 진실이다. 다만 잔고 수량 불일치는 자동으로 "고치지" 않는다:
  - 정수배 차이 → 기업행위(분할·무상증자) 의심. 종목 잠금 + 경보.
  - 그 외 → 종목 잠금 + 경보.
  잠금은 신규 진입만 막는다. 손절은 증권사 잔고 수량 기준으로 계속한다.
* 불일치가 하나라도 있으면 킬 스위치 1단계를 자동으로 건다 (스킬 6절).
* 봇 포지션이 없는 기존 보유 종목은 "unmanaged"로 경보만 한다 (잠금·킬 없음).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from src.broker.base import Broker
from src.execution.order_manager import OrderManager
from src.execution.store import Store
from src.risk.kill_switch import KillSwitch

log = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    fills_recovered: int = 0
    mismatches: list[dict] = field(default_factory=list)
    unmanaged: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.mismatches


class Reconciler:
    def __init__(self, broker: Broker, store: Store, orders: OrderManager, kill: KillSwitch,
                 notify=None):
        self.broker = broker
        self.store = store
        self.orders = orders
        self.kill = kill
        self.notify = notify if notify is not None else (lambda *a, **k: None)

    def run(self, trade_date: date, *, session_over: bool = False) -> ReconcileResult:
        res = ReconcileResult()
        res.fills_recovered = self.orders.sync(trade_date, source="reconcile",
                                               session_over=session_over)
        if res.fills_recovered:
            # 실시간 경로(poll)가 놓친 체결이다. 비율이 높으면 그 경로가 망가진 것이다.
            self.notify(f"대조에서 체결 {res.fills_recovered}건 복구 (poll 경로 누락)",
                        key=f"recovered:{trade_date}")

        holdings, _ = self.broker.holdings()
        remote = {h.code: h.qty for h in holdings}
        local = {code: int(p["qty"]) for code, p in self.store.positions().items()}

        for code in sorted(remote.keys() | local.keys()):
            rq, lq = remote.get(code, 0), local.get(code, 0)
            if rq == lq:
                continue
            if lq == 0:
                res.unmanaged.append(code)
                continue
            if rq and (rq % lq == 0 or lq % rq == 0):
                reason = f"qty ratio mismatch local={lq} broker={rq} — corporate action?"
            else:
                reason = f"qty mismatch local={lq} broker={rq}"
            res.mismatches.append({"code": code, "local": lq, "broker": rq, "reason": reason})
            self.store.lock_symbol(code, reason)
            self.store.event("reconcile", f"{code} {reason}", {"local": lq, "broker": rq})

        if res.mismatches:
            lines = "\n".join(f"- {m['code']}: {m['reason']}" for m in res.mismatches)
            self.notify(f"⚠️ 잔고 불일치 → 종목 잠금\n{lines}", key=f"mismatch:{lines}")
            self.kill.auto_trip(f"reconcile mismatch ({len(res.mismatches)} symbols)")
        if res.unmanaged:
            self.notify(f"ℹ️ 봇이 관리하지 않는 보유 종목: {', '.join(res.unmanaged)} "
                        "(손절 감시 대상 아님)", key=f"unmanaged:{','.join(res.unmanaged)}")
        log.info("reconcile %s: recovered=%d mismatches=%d unmanaged=%d", trade_date,
                 res.fills_recovered, len(res.mismatches), len(res.unmanaged))
        return res

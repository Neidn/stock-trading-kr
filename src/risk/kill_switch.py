"""Three-level kill switch.

  0  정상
  1  신규 진입 중지. 손절·익절·청산은 계속.          ← 일손실 한도·연속 오류·대조 불일치가 자동으로 건다
  2  1단계 + 미체결 주문 전부 취소 (손절 주문 포함)   ← 사람만 건다
  3  2단계 + 봇 보유 종목 전량 시장가 매도            ← 사람만 건다
     동시호가·VI·하한가 구간에서는 즉시 체결되지 않는다.

자동 로직은 레벨을 올리기만 하고 내리지 않는다. 해제는 사람이 CLI 로 한다.
"""

from __future__ import annotations

import logging

from src.execution.store import Store

log = logging.getLogger(__name__)

DESCRIPTIONS = {
    0: "정상",
    1: "신규 진입 중지 (손절·청산 계속)",
    2: "신규 진입 중지 + 미체결 전부 취소",
    3: "미체결 취소 + 봇 보유 전량 시장가 매도 (동시호가·VI·하한가 구간은 즉시 체결 안 됨)",
}


class KillSwitch:
    def __init__(self, store: Store, notify=None):
        self.store = store
        self.notify = notify if notify is not None else (lambda *a, **k: None)

    def level(self) -> int:
        return self.store.kill_level()[0]

    def auto_trip(self, reason: str) -> None:
        """Automatic triggers only ever raise to level 1."""
        level, _ = self.store.kill_level()
        if level >= 1:
            return
        self.store.set_kill_level(1, reason, "auto")
        log.error("KILL SWITCH level 1: %s", reason)
        self.notify(f"🛑 킬 스위치 1단계 (자동): {reason}\n{DESCRIPTIONS[1]}", key="kill1")

    def set(self, level: int, reason: str, by: str) -> None:
        if level not in DESCRIPTIONS:
            raise ValueError(level)
        self.store.set_kill_level(level, reason, by)
        self.notify(f"킬 스위치 → {level} ({by}): {reason}\n{DESCRIPTIONS[level]}", key=None)

    def entries_allowed(self) -> bool:
        return self.level() == 0

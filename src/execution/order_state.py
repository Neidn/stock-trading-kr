"""Order state machine (pure functions, no I/O).

    NEW ──접수──▶ ACCEPTED ──체결──▶ PARTIAL ──잔량0──▶ FILLED
     │  └거부──▶ REJECTED     │                │
     └타임아웃▶ UNKNOWN        ├─취소요청─▶ CANCELLING ─확인─▶ CANCELLED (부분체결분 유지)
       (조회로 확정)            └─세션종료─▶ EXPIRED (잔량 소멸)

* 포지션은 체결로만 바뀐다. 이 모듈은 주문 상태만 계산한다.
* 상태는 요청을 보냈다고 바꾸지 않는다. 증권사 조회 결과로 확정한다.
* 정정(REPLACE)은 1차 범위에서 쓰지 않는다. 가격을 바꿀 때는 취소 확인 후 새 주문을 낸다.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.broker.base import OrderStatus

NEW = "NEW"
UNKNOWN = "UNKNOWN"
ACCEPTED = "ACCEPTED"
PARTIAL = "PARTIAL"
CANCELLING = "CANCELLING"
FILLED = "FILLED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"

OPEN_STATES = frozenset({NEW, UNKNOWN, ACCEPTED, PARTIAL, CANCELLING})
TERMINAL_STATES = frozenset({FILLED, REJECTED, CANCELLED, EXPIRED})


@dataclass(frozen=True)
class FillDelta:
    qty: int
    amount: int
    cum_qty: int
    cum_amount: int


class FillRegression(RuntimeError):
    """증권사 누적 체결이 로컬보다 작다 — 로컬에만 있는 체결. 버그이므로 자동 삭제하지 않는다."""


def fill_delta(local_filled_qty: int, local_filled_amount: int,
               status: OrderStatus) -> FillDelta | None:
    """Return the new fill since the last sync, or None.

    KIS 일별주문체결조회는 주문별 누적 체결만 준다. 누적이 늘어난 만큼을 체결 1건으로 본다.
    같은 조회 결과를 두 번 반영해도 delta 가 0 이라 중복되지 않는다.
    """
    if status.filled_qty < local_filled_qty:
        raise FillRegression(
            f"order {status.order_no}: broker filled {status.filled_qty} < local {local_filled_qty}"
        )
    dq = status.filled_qty - local_filled_qty
    if dq == 0:
        return None
    da = status.filled_amount - local_filled_amount
    if da <= 0:
        # 수량은 늘었는데 금액이 안 늘었다 — 응답 이상. 가격을 지어내지 않는다.
        raise FillRegression(f"order {status.order_no}: qty +{dq} but amount {da}")
    return FillDelta(qty=dq, amount=da, cum_qty=status.filled_qty, cum_amount=status.filled_amount)


def next_state(local_state: str, status: OrderStatus, *, session_over: bool) -> str:
    """Derive the order state from the broker's view of the order."""
    if local_state in TERMINAL_STATES and local_state != EXPIRED:
        return local_state
    if status.filled_qty >= status.qty:
        return FILLED
    if status.rejected_qty > 0 and status.filled_qty == 0 and status.remaining_qty == 0:
        return REJECTED
    if status.remaining_qty <= 0:
        # 잔량이 없고 전량 체결도 아니다 → 취소(또는 장마감 소멸)로 잔량이 사라졌다.
        if status.cancelled_qty > 0 and local_state != EXPIRED:
            return CANCELLED
        return EXPIRED if session_over else CANCELLED
    # 잔량이 남아 있다.
    if session_over:
        return EXPIRED
    if local_state == CANCELLING:
        return CANCELLING
    return PARTIAL if status.filled_qty > 0 else ACCEPTED

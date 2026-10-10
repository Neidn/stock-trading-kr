"""Broker-neutral types and the Broker protocol.

엔진·대조·리스크 코드는 이 인터페이스에만 의존한다. 실제 구현은 KisBroker,
테스트는 FakeBroker 를 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import pandas as pd


# ── 오류 분류 ────────────────────────────────────────────────────────────────
class BrokerError(RuntimeError):
    """Base class for broker failures."""


class BrokerRejected(BrokerError):
    """증권사가 요청을 거부했다 (호가단위·가격제한·잔고부족·장운영시간 외 등).

    재시도해도 같은 결과다. 재시도하지 말고 원인을 기록한다.
    """

    def __init__(self, msg_cd: str, msg: str):
        super().__init__(f"[{msg_cd}] {msg}")
        self.msg_cd = msg_cd
        self.msg = msg


class BrokerTransient(BrokerError):
    """네트워크·5xx·타임아웃. 주문이면 접수 여부가 불확실하다 (ambiguous)."""

    def __init__(self, msg: str, *, ambiguous: bool = True):
        super().__init__(msg)
        self.ambiguous = ambiguous


class BrokerRateLimited(BrokerTransient):
    """호출 제한 초과. 게이트웨이에서 거절됐으므로 접수되지 않았다."""

    def __init__(self, msg: str):
        super().__init__(msg, ambiguous=False)


class BrokerAuthError(BrokerError):
    """토큰 발급·갱신 실패. 치명 오류로 다룬다 — 조용히 재시도하지 않는다."""


# ── 데이터 ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OrderAck:
    order_no: str
    org_no: str
    ordered_at: datetime


@dataclass(frozen=True)
class Holding:
    code: str
    name: str
    qty: int
    sellable_qty: int
    avg_price: float
    current_price: int


@dataclass(frozen=True)
class AccountSummary:
    deposit: int                 # 예수금총금액
    total_eval: int              # 총평가금액
    asset_change: int            # 자산증감액 (전일 대비). 입출금이 섞인다 — 추정치
    prev_total_asset: int        # 전일총자산평가금액


@dataclass(frozen=True)
class OrderStatus:
    """증권사 일별주문체결조회의 주문 1건 (누적 체결 기준)."""

    order_no: str
    org_no: str
    orig_order_no: str           # 정정·취소 주문이면 원주문번호
    code: str
    side: str                    # 'buy' | 'sell'
    qty: int
    price: int
    filled_qty: int
    filled_amount: int
    remaining_qty: int
    cancelled_qty: int
    rejected_qty: int
    is_cancel_order: bool        # 취소 주문 레코드 자체인가
    ordered_at: datetime
    market: str = ""


@dataclass(frozen=True)
class Quote:
    code: str
    price: int
    upper_limit: int
    lower_limit: int
    base_price: int
    market: str                  # 'KOSPI' | 'KOSDAQ' | 'KONEX' | ''
    name: str
    block_reasons: tuple[str, ...]   # 거래정지·관리·경고·단기과열·정리매매 등
    vi_active: bool
    at: datetime


class Broker(Protocol):
    env: str

    def place_order(self, code: str, side: str, qty: int, order_type: str,
                    price: int | None) -> OrderAck: ...

    def cancel_order(self, order_no: str, org_no: str, qty: int | None = None) -> OrderAck: ...

    def holdings(self) -> tuple[list[Holding], AccountSummary]: ...

    def daily_orders(self, trade_date: date) -> list[OrderStatus]: ...

    def buyable_cash(self, code: str, price: int) -> int: ...

    def quote(self, code: str) -> Quote: ...

    def daily_candles(self, code: str, count: int) -> pd.DataFrame: ...

    def is_market_open_day(self, d: date) -> bool | None: ...

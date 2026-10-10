"""In-memory broker for tests and dry runs.

테스트는 실전에서 오는 순서대로 이벤트를 만든다: 주문 → 접수 → (부분)체결 → 취소/만료.
아무도 만들지 않는 행을 DB 에 직접 넣지 않는다.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import date

import pandas as pd

from src.broker.base import (
    AccountSummary,
    BrokerRejected,
    BrokerTransient,
    Holding,
    OrderAck,
    OrderStatus,
    Quote,
)
from src.utils.time_utils import now_kst


@dataclass
class _Order:
    order_no: str
    code: str
    side: str
    qty: int
    price: int
    order_type: str
    filled_qty: int = 0
    filled_amount: int = 0
    cancelled_qty: int = 0
    ordered_at: object = None


@dataclass
class FakeBroker:
    env: str = "demo"
    cash: int = 10_000_000
    quotes: dict[str, Quote] = field(default_factory=dict)
    candles: dict[str, pd.DataFrame] = field(default_factory=dict)
    open_day: bool | None = True
    # 다음 place_order 호출의 동작: None | 'reject' | 'timeout_accepted' | 'timeout_lost'
    next_place: str | None = None
    # 다음 cancel 호출 전에 원주문을 전량 체결시킨다 (정정·취소 경합 재현)
    fill_before_cancel: bool = False
    calls: list[str] = field(default_factory=list)
    clock: object = now_kst

    def __post_init__(self) -> None:
        self._orders: dict[str, _Order] = {}
        self._seq = itertools.count(1)
        self._positions: dict[str, tuple[int, float]] = {}   # code -> (qty, avg)

    # ── test controls ────────────────────────────────────────────────────────
    def set_quote(self, code: str, price: int, **kw) -> None:
        base = kw.pop("base_price", price)
        self.quotes[code] = Quote(
            code=code, price=price,
            upper_limit=kw.pop("upper_limit", int(base * 1.3)),
            lower_limit=kw.pop("lower_limit", int(base * 0.7)),
            base_price=base, market=kw.pop("market", "KOSPI"), name=kw.pop("name", code),
            block_reasons=tuple(kw.pop("block_reasons", ())),
            vi_active=kw.pop("vi_active", False), at=self.clock(),
        )

    def fill(self, order_no: str, qty: int, price: int) -> None:
        o = self._orders[order_no]
        remaining = o.qty - o.filled_qty - o.cancelled_qty
        if qty > remaining:
            raise AssertionError(f"overfill {order_no}: {qty} > {remaining}")
        o.filled_qty += qty
        o.filled_amount += qty * price
        q, avg = self._positions.get(o.code, (0, 0.0))
        if o.side == "buy":
            new_q = q + qty
            avg = (q * avg + qty * price) / new_q
            self._positions[o.code] = (new_q, avg)
            self.cash -= qty * price
        else:
            new_q = q - qty
            self._positions[o.code] = (new_q, avg if new_q else 0.0)
            self.cash += qty * price

    def expire_all(self) -> None:
        """장 마감: 당일 유효 주문의 잔량 소멸."""
        for o in self._orders.values():
            o.cancelled_qty = o.qty - o.filled_qty

    def split(self, code: str, ratio: int) -> None:
        """액면분할 1:ratio — 증권사 잔고 수량만 바뀐다."""
        q, avg = self._positions[code]
        self._positions[code] = (q * ratio, avg / ratio)

    def set_holding(self, code: str, qty: int, avg: float) -> None:
        self._positions[code] = (qty, avg)

    def last_order_no(self) -> str:
        return list(self._orders)[-1]

    def open_order_nos(self) -> list[str]:
        return [o.order_no for o in self._orders.values()
                if o.qty - o.filled_qty - o.cancelled_qty > 0]

    # ── Broker protocol ─────────────────────────────────────────────────────
    def place_order(self, code, side, qty, order_type, price):
        self.calls.append(f"place:{code}:{side}:{qty}")
        mode, self.next_place = self.next_place, None
        if mode == "reject":
            raise BrokerRejected("APBK0919", "호가단위 오류 (fake)")
        order_no = f"{next(self._seq):010d}"
        if mode != "timeout_lost":
            self._orders[order_no] = _Order(order_no, code, side, qty, price or 0, order_type,
                                            ordered_at=self.clock())
        if mode in ("timeout_accepted", "timeout_lost"):
            raise BrokerTransient("fake timeout", ambiguous=True)
        return OrderAck(order_no=order_no, org_no="00950", ordered_at=self.clock())

    def cancel_order(self, order_no, org_no, qty=None):
        self.calls.append(f"cancel:{order_no}")
        o = self._orders.get(order_no)
        if o is None:
            raise BrokerRejected("APBK0000", "원주문 없음 (fake)")
        if self.fill_before_cancel:
            self.fill_before_cancel = False
            self.fill(order_no, o.qty - o.filled_qty - o.cancelled_qty, o.price)
        remaining = o.qty - o.filled_qty - o.cancelled_qty
        if remaining <= 0:
            raise BrokerRejected("APBK0001", "정정취소 가능수량 없음 (fake)")
        o.cancelled_qty += remaining if qty is None else min(qty, remaining)
        return OrderAck(order_no=f"{next(self._seq):010d}", org_no=org_no, ordered_at=self.clock())

    def holdings(self):
        self.calls.append("holdings")
        hs = []
        for code, (q, avg) in self._positions.items():
            if q <= 0:
                continue
            quote = self.quotes.get(code)
            hs.append(Holding(code=code, name=code, qty=q, sellable_qty=q, avg_price=avg,
                              current_price=quote.price if quote else int(avg)))
        total = self.cash + sum(h.qty * h.current_price for h in hs)
        return hs, AccountSummary(deposit=self.cash, total_eval=total,
                                  asset_change=0, prev_total_asset=total)

    def daily_orders(self, trade_date: date):
        self.calls.append("daily_orders")
        return [
            OrderStatus(
                order_no=o.order_no, org_no="00950", orig_order_no="", code=o.code,
                side=o.side, qty=o.qty, price=o.price, filled_qty=o.filled_qty,
                filled_amount=o.filled_amount,
                remaining_qty=o.qty - o.filled_qty - o.cancelled_qty,
                cancelled_qty=o.cancelled_qty, rejected_qty=0, is_cancel_order=False,
                ordered_at=o.ordered_at,
            )
            for o in self._orders.values()
        ]

    def buyable_cash(self, code, price):
        return self.cash

    def quote(self, code):
        self.calls.append(f"quote:{code}")
        q = self.quotes[code]
        return replace(q, at=self.clock())

    def daily_candles(self, code, count):
        return self.candles.get(code, pd.DataFrame()).tail(count).reset_index(drop=True)

    def is_market_open_day(self, d):
        return self.open_day

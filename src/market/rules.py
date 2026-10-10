"""Market rules loaded from the DB: tick sizes, tax rates, sessions, calendar.

장 운영 규칙은 데이터이지 상수가 아니다. 이 모듈은 DB 테이블(tick_sizes, tax_rates,
market_sessions, trading_calendar)을 읽어 메모리에 들고 있다. 코드에 09:00 이나
0.20% 를 박지 않는다.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum

from src.db.connection import db_conn


class Session(str, Enum):
    CLOSED = "closed"              # 휴장일 또는 장 시간 외
    PRE_OPEN = "pre_open"          # 개장 전 (장전 동시호가 포함)
    CONTINUOUS = "continuous"      # 정규장 접속매매
    CLOSING_AUCTION = "closing_auction"  # 장마감 동시호가 (단일가)
    AFTER_CLOSE = "after_close"    # 정규장 종료 후
    UNKNOWN = "unknown"            # 개장일 여부를 모른다


@dataclass(frozen=True)
class SessionTimes:
    regular_open: time
    closing_auction_start: time
    regular_close: time


@dataclass(frozen=True)
class TickTable:
    effective_from: date
    bounds: tuple[int, ...]        # price_from (오름차순)
    ticks: tuple[int, ...]

    def tick(self, price: float) -> int:
        i = bisect.bisect_right(self.bounds, int(price)) - 1
        return self.ticks[max(i, 0)]


class MarketRules:
    def __init__(self, tick_tables: list[TickTable],
                 tax_rates: dict[str, list[tuple[date, float]]],
                 default_sessions: list[tuple[date, SessionTimes]],
                 session_overrides: dict[date, SessionTimes],
                 calendar: dict[date, bool]):
        self._ticks = sorted(tick_tables, key=lambda t: t.effective_from)
        self._tax = {m: sorted(v) for m, v in tax_rates.items()}
        self._sessions = sorted(default_sessions, key=lambda x: x[0])
        self._overrides = session_overrides
        self.calendar = calendar

    # ── 호가단위 ─────────────────────────────────────────────────────────────
    def _tick_table(self, on: date) -> TickTable:
        eligible = [t for t in self._ticks if t.effective_from <= on]
        if not eligible:
            raise LookupError(f"no tick table effective on {on}")
        return eligible[-1]

    def tick_size(self, price: float, on: date) -> int:
        return self._tick_table(on).tick(price)

    def round_to_tick(self, price: float, side: str, on: date) -> int:
        """매수는 내림, 매도는 올림 — 의도보다 불리한 가격으로 체결되지 않게 한다.

        경계(예: 19,995 → 올림 20,000)는 반올림 후 그 가격의 호가단위로 다시 검증한다.
        """
        table = self._tick_table(on)
        p = float(price)
        for _ in range(3):
            t = table.tick(p)
            r = math.floor(p / t) * t if side == "buy" else math.ceil(p / t) * t
            if r % table.tick(r) == 0:
                return int(r)
            p = r
        raise ValueError(f"cannot align {price} to tick")

    def is_valid_price(self, price: int, on: date) -> bool:
        return price > 0 and price % self.tick_size(price, on) == 0

    # ── 세금 ────────────────────────────────────────────────────────────────
    def sell_tax_rate(self, market: str, on: date) -> float:
        rows = [r for r in self._tax.get(market, []) if r[0] <= on]
        if not rows:
            raise LookupError(f"no tax rate for {market} on {on}")
        return rows[-1][1]

    # ── 세션 ────────────────────────────────────────────────────────────────
    def session_times(self, d: date) -> SessionTimes:
        if d in self._overrides:
            return self._overrides[d]
        eligible = [s for eff, s in self._sessions if eff <= d]
        if not eligible:
            raise LookupError(f"no session times for {d}")
        return eligible[-1]

    def is_open_day(self, d: date) -> bool | None:
        return self.calendar.get(d)

    def session(self, now: datetime, *, assume_open: bool = False) -> Session:
        """Session at ``now``. 개장일 여부를 모르면 UNKNOWN.

        assume_open=True 는 캘린더를 모를 때 "열려 있다면 지금 어느 구간인가"를 묻는다.
        손절처럼 보호 목적의 주문에만 쓴다 (신규 진입에는 쓰지 않는다).
        """
        d = now.date()
        open_day = self.is_open_day(d)
        if open_day is False:
            return Session.CLOSED
        st = self.session_times(d)
        t = now.time()
        if open_day is None and not assume_open:
            return Session.UNKNOWN
        if t < st.regular_open:
            return Session.PRE_OPEN
        if t < st.closing_auction_start:
            return Session.CONTINUOUS
        if t < st.regular_close:
            return Session.CLOSING_AUCTION
        return Session.AFTER_CLOSE

    # ── DB 로드 ─────────────────────────────────────────────────────────────
    @classmethod
    def load(cls) -> "MarketRules":
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT effective_from, price_from, tick FROM tick_sizes "
                        "ORDER BY effective_from, price_from")
            by_eff: dict[date, list[tuple[int, int]]] = {}
            for r in cur.fetchall():
                by_eff.setdefault(r["effective_from"], []).append((int(r["price_from"]), r["tick"]))
            ticks = [TickTable(eff, tuple(b for b, _ in rows), tuple(t for _, t in rows))
                     for eff, rows in by_eff.items()]

            cur.execute("SELECT market, effective_from, sell_tax_rate FROM tax_rates")
            tax: dict[str, list[tuple[date, float]]] = {}
            for r in cur.fetchall():
                tax.setdefault(r["market"], []).append((r["effective_from"], float(r["sell_tax_rate"])))

            cur.execute("SELECT trade_date, effective_from, regular_open, closing_auction_start, "
                        "regular_close FROM market_sessions WHERE venue = 'KRX'")
            defaults, overrides = [], {}
            for r in cur.fetchall():
                st = SessionTimes(r["regular_open"], r["closing_auction_start"], r["regular_close"])
                if r["trade_date"] is None:
                    defaults.append((r["effective_from"], st))
                else:
                    overrides[r["trade_date"]] = st

            cur.execute("SELECT trade_date, is_open FROM trading_calendar "
                        "WHERE trade_date >= CURRENT_DATE - 30")
            cal = {r["trade_date"]: r["is_open"] for r in cur.fetchall()}
        return cls(ticks, tax, defaults, overrides, cal)


def save_calendar_day(d: date, is_open: bool, source: str = "kis") -> None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO trading_calendar (trade_date, is_open, source, fetched_at)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (trade_date) DO UPDATE
               SET is_open = EXCLUDED.is_open, source = EXCLUDED.source, fetched_at = now()""",
            (d, is_open, source),
        )

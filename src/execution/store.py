"""PostgreSQL-backed book: orders, fills, positions, trades, locks, kill switch, events.

체결 반영(apply_fill)은 한 트랜잭션에서 fills INSERT → orders 누적 갱신 → positions 갱신 →
(청산되면) trades INSERT 를 한다. fills 의 UNIQUE(order_no, fill_seq) 로 같은 체결을 두 번
반영하지 않는다.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from src.db.connection import db_conn
from src.execution.order_state import OPEN_STATES

log = logging.getLogger(__name__)


def _finite(d: dict) -> dict:
    """JSONB 는 NaN/Infinity 를 받지 않는다."""
    out = {}
    for k, v in d.items():
        try:
            f = float(v)
            out[k] = f if math.isfinite(f) else None
        except (TypeError, ValueError):
            out[k] = v
    return out


@dataclass(frozen=True)
class FillCost:
    fee: int
    tax: int
    market: str


class Store:
    # ── orders ───────────────────────────────────────────────────────────────
    def create_order(self, *, code: str, side: str, order_type: str, qty: int,
                     price: int | None, purpose: str, strategy_name: str | None,
                     trade_date: date, stop_price: int | None = None,
                     take_profit: int | None = None, initial_risk: float | None = None,
                     client_id: str | None = None, state: str = "NEW",
                     created_at: datetime | None = None) -> dict:
        cid = client_id or uuid.uuid4().hex
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO orders (client_id, code, side, order_type, qty, price, state,
                                       purpose, strategy_name, trade_date, stop_price,
                                       take_profit, initial_risk, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, now()))
                   RETURNING *""",
                (cid, code, side, order_type, qty, price, state, purpose, strategy_name,
                 trade_date, stop_price, take_profit, initial_risk, created_at),
            )
            return cur.fetchone()

    def update_order(self, client_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = %s" for k in fields)
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE orders SET {cols}, updated_at = now() WHERE client_id = %s",
                        (*fields.values(), client_id))

    def get_order(self, client_id: str) -> dict | None:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE client_id = %s", (client_id,))
            return cur.fetchone()

    def order_by_no(self, order_no: str) -> dict | None:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE order_no = %s", (order_no,))
            return cur.fetchone()

    def open_orders(self, code: str | None = None, side: str | None = None) -> list[dict]:
        sql = "SELECT * FROM orders WHERE state = ANY(%s)"
        args: list[Any] = [list(OPEN_STATES)]
        if code:
            sql += " AND code = %s"
            args.append(code)
        if side:
            sql += " AND side = %s"
            args.append(side)
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql + " ORDER BY id", args)
            return cur.fetchall()

    def known_order_nos(self) -> set[str]:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT order_no FROM orders WHERE order_no IS NOT NULL "
                        "AND trade_date >= CURRENT_DATE - 7")
            return {r["order_no"] for r in cur.fetchall()}

    # ── fills → positions → trades ──────────────────────────────────────────
    def apply_fill(self, *, order: dict, qty: int, amount: int, cum_qty: int,
                   cum_amount: int, executed_at: datetime, cost: FillCost,
                   source: str, cost_source: str = "estimated") -> bool:
        """Apply one fill atomically. Returns False if it was already applied."""
        price = round(amount / qty, 2)
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO fills (order_no, fill_seq, code, market, side, qty, amount, price,
                                      fee, tax, cost_source, strategy_name, source, executed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (order_no, fill_seq) DO NOTHING RETURNING id""",
                (order["order_no"], cum_qty, order["code"], cost.market, order["side"], qty,
                 amount, price, cost.fee, cost.tax, cost_source, order["strategy_name"],
                 source, executed_at),
            )
            if cur.fetchone() is None:
                return False
            cur.execute(
                "UPDATE orders SET filled_qty = %s, filled_amount = %s, updated_at = now() "
                "WHERE id = %s",
                (cum_qty, cum_amount, order["id"]),
            )
            if order["side"] == "buy":
                self._apply_buy(cur, order, qty, amount, cost, executed_at)
            else:
                self._apply_sell(cur, order, qty, amount, cost, executed_at, cost_source)
        return True

    @staticmethod
    def _apply_buy(cur, order, qty, amount, cost: FillCost, at) -> None:
        cur.execute("SELECT * FROM positions WHERE code = %s FOR UPDATE", (order["code"],))
        pos = cur.fetchone()
        if pos is None:
            cur.execute(
                """INSERT INTO positions (code, qty, avg_price, strategy_name, stop_price,
                                          take_profit, initial_risk, opened_at, rt_buy_qty,
                                          rt_buy_amount, rt_fee)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (order["code"], qty, round(amount / qty, 2), order["strategy_name"],
                 order["stop_price"], order["take_profit"], order["initial_risk"], at,
                 qty, amount, cost.fee),
            )
            return
        new_qty = pos["qty"] + qty
        avg = (float(pos["avg_price"]) * pos["qty"] + amount) / new_qty
        cur.execute(
            """UPDATE positions SET qty = %s, avg_price = %s,
                   stop_price = COALESCE(stop_price, %s), take_profit = COALESCE(take_profit, %s),
                   initial_risk = COALESCE(initial_risk, %s),
                   opened_at = COALESCE(opened_at, %s),
                   rt_buy_qty = rt_buy_qty + %s, rt_buy_amount = rt_buy_amount + %s,
                   rt_fee = rt_fee + %s, updated_at = now()
               WHERE code = %s""",
            (new_qty, round(avg, 2), order["stop_price"], order["take_profit"],
             order["initial_risk"], at, qty, amount, cost.fee, order["code"]),
        )

    def _apply_sell(self, cur, order, qty, amount, cost: FillCost, at, cost_source) -> None:
        cur.execute("SELECT * FROM positions WHERE code = %s FOR UPDATE", (order["code"],))
        pos = cur.fetchone()
        if pos is None or pos["qty"] < qty:
            # 로컬 포지션보다 많이 팔렸다 (외부 주문·대조 누락). 숫자를 지어내지 않고 잠근다.
            held = pos["qty"] if pos else 0
            self._lock(cur, order["code"], f"sell fill {qty} > local position {held}")
            self._event(cur, "alert", f"{order['code']} 매도 체결 {qty}주 > 로컬 보유 {held}주",
                        {"order_no": order["order_no"]})
            if pos is None:
                return
            qty = pos["qty"]
        remaining = pos["qty"] - qty
        rt_sell = pos["rt_sell_amount"] + amount
        rt_fee = pos["rt_fee"] + cost.fee
        rt_tax = pos["rt_tax"] + cost.tax
        if remaining > 0:
            cur.execute(
                """UPDATE positions SET qty = %s, rt_sell_amount = %s, rt_fee = %s, rt_tax = %s,
                       updated_at = now() WHERE code = %s""",
                (remaining, rt_sell, rt_fee, rt_tax, order["code"]),
            )
            return
        gross = rt_sell - pos["rt_buy_amount"]
        net = gross - rt_fee - rt_tax
        risk = float(pos["initial_risk"]) if pos["initial_risk"] else None
        realized_r = round(net / (risk * pos["rt_buy_qty"]), 3) if risk and pos["rt_buy_qty"] else None
        cur.execute(
            """INSERT INTO trades (code, market, strategy_name, entry_at, exit_at, qty,
                                   buy_amount, sell_amount, gross_pnl, fee, tax, net_pnl,
                                   realized_r, close_reason, cost_source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (order["code"], cost.market, pos["strategy_name"], pos["opened_at"] or at, at,
             pos["rt_buy_qty"], pos["rt_buy_amount"], rt_sell, gross, rt_fee, rt_tax, net,
             realized_r, order["purpose"], cost_source),
        )
        cur.execute("DELETE FROM positions WHERE code = %s", (order["code"],))

    # ── positions ────────────────────────────────────────────────────────────
    def positions(self) -> dict[str, dict]:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM positions WHERE qty > 0")
            return {r["code"]: r for r in cur.fetchall()}

    def adopt_position(self, code: str, qty: int, avg_price: float, stop_price: int | None,
                       strategy_name: str = "adopted") -> None:
        """사람이 명시적으로 증권사 잔고를 봇 포지션으로 편입할 때만 쓴다 (CLI)."""
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO positions (code, qty, avg_price, strategy_name, stop_price,
                                          opened_at, rt_buy_qty, rt_buy_amount)
                   VALUES (%s,%s,%s,%s,%s,now(),%s,%s)
                   ON CONFLICT (code) DO UPDATE SET qty = EXCLUDED.qty,
                       avg_price = EXCLUDED.avg_price, stop_price = EXCLUDED.stop_price,
                       updated_at = now()""",
                (code, qty, avg_price, strategy_name, stop_price, qty, round(qty * avg_price)),
            )
            self._event(cur, "adopt", f"{code} {qty}주 편입", {"avg": avg_price, "stop": stop_price})

    # ── instruments ──────────────────────────────────────────────────────────
    def upsert_instrument(self, code: str, name: str, market: str, tradable: bool,
                          block_reason: str | None) -> None:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO instruments (code, name, market, tradable, block_reason, updated_at)
                   VALUES (%s,%s,%s,%s,%s,now())
                   ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name,
                       market = COALESCE(NULLIF(EXCLUDED.market, ''), instruments.market),
                       tradable = EXCLUDED.tradable, block_reason = EXCLUDED.block_reason,
                       updated_at = now()""",
                (code, name, market, tradable, block_reason),
            )

    def instrument(self, code: str) -> dict | None:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM instruments WHERE code = %s", (code,))
            return cur.fetchone()

    # ── locks · kill switch · events · signals ──────────────────────────────
    @staticmethod
    def _lock(cur, code: str, reason: str) -> None:
        cur.execute("INSERT INTO symbol_locks (code, reason) VALUES (%s, %s) "
                    "ON CONFLICT (code) DO NOTHING", (code, reason))

    def lock_symbol(self, code: str, reason: str) -> None:
        with db_conn() as conn, conn.cursor() as cur:
            self._lock(cur, code, reason)

    def unlock_symbol(self, code: str) -> None:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM symbol_locks WHERE code = %s", (code,))

    def locked_symbols(self) -> dict[str, str]:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT code, reason FROM symbol_locks")
            return {r["code"]: r["reason"] for r in cur.fetchall()}

    def kill_level(self) -> tuple[int, str]:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT level, reason FROM kill_switch WHERE id = 1")
            r = cur.fetchone()
            return (r["level"], r["reason"] or "") if r else (0, "")

    def set_kill_level(self, level: int, reason: str, set_by: str) -> None:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE kill_switch SET level = %s, reason = %s, set_by = %s, "
                        "set_at = now() WHERE id = 1", (level, reason, set_by))
            self._event(cur, "kill", f"kill switch → {level}: {reason}", {"by": set_by})

    @staticmethod
    def _event(cur, kind: str, message: str, detail: dict | None = None) -> None:
        cur.execute("INSERT INTO events (kind, message, detail) VALUES (%s, %s, %s)",
                    (kind, message, json.dumps(detail or {}, default=str)))

    def event(self, kind: str, message: str, detail: dict | None = None) -> None:
        with db_conn() as conn, conn.cursor() as cur:
            self._event(cur, kind, message, detail)

    def record_signal(self, *, code: str, strategy_name: str, signal_type: str,
                      entry_price: float | None, stop_price: float | None,
                      take_profit: float | None, block_reason: str | None, reason: str,
                      indicators: dict) -> None:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO signals (code, strategy_name, signal_type, entry_price, stop_price,
                                        take_profit, block_reason, reason, indicators)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (code, strategy_name, signal_type, entry_price, stop_price, take_profit,
                 block_reason, reason, json.dumps(_finite(indicators), default=float)),
            )

    def realized_net_on(self, d: date) -> int:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(net_pnl), 0) AS s FROM trades "
                        "WHERE (exit_at AT TIME ZONE 'Asia/Seoul')::date = %s", (d,))
            return int(cur.fetchone()["s"])

    def fill_sources_on(self, d: date) -> dict[str, int]:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT source, COUNT(*) AS n FROM fills "
                        "WHERE (executed_at AT TIME ZONE 'Asia/Seoul')::date = %s "
                        "GROUP BY source", (d,))
            return {r["source"]: r["n"] for r in cur.fetchall()}

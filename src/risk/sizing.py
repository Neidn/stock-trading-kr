"""Position sizing (pure).

* 수량 = floor(건당 리스크 금액 ÷ 주당 리스크). 1주 미만이면 진입하지 않는다 —
  1주로 올리면 리스크가 의도보다 커진다.
* 주문금액 ≤ 증권사 주문가능금액(미수 없는 매수금액). 예수금을 직접 계산하지 않는다.
* 구조 규칙: 종목당 매수금액 ≤ 순자산 × max_position_pct, 진입 후 현금 ≥ 순자산 ×
  min_cash_pct. 하한가 잠김·갭은 손절로 못 막으므로 종목당 상한이 실제 최대손실을 정한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizeResult:
    qty: int
    reason: str            # qty == 0 이면 차단 사유


def size_entry(*, entry_price: int, stop_price: float, risk_per_trade_krw: int,
               max_position_krw: int, buyable_cash: int, fee_rate: float,
               equity: int | None = None, max_position_pct: float = 1.0,
               min_cash_pct: float = 0.0) -> SizeResult:
    per_share_risk = entry_price - stop_price
    if entry_price <= 0 or per_share_risk <= 0:
        return SizeResult(0, f"invalid stop (entry={entry_price}, stop={stop_price})")
    by_risk = math.floor(risk_per_trade_krw / per_share_risk)
    if by_risk < 1:
        return SizeResult(0, f"risk per share {per_share_risk:,.0f} > budget {risk_per_trade_krw:,}"
                             " (1주 미만 — 올림 진입하지 않음)")
    caps = {
        "max_position_krw": math.floor(max_position_krw / entry_price),
        "cash": math.floor(buyable_cash / (entry_price * (1 + fee_rate))),
    }
    if equity is not None:
        caps["max_position_pct"] = math.floor(equity * max_position_pct / entry_price)
        spendable = buyable_cash - equity * min_cash_pct
        caps["min_cash_pct"] = max(math.floor(spendable / (entry_price * (1 + fee_rate))), 0)
    qty = min(by_risk, *caps.values())
    if qty < 1:
        which = next(k for k, v in caps.items() if v < 1)
        return SizeResult(0, f"{which} limit (entry {entry_price:,})")
    return SizeResult(qty, "")


def worst_case_loss(positions: list[dict], gap_pct: float, daily_loss_limit: int) -> dict:
    """최대손실 시나리오: 일손실한도 + 보유 전 종목 손절가 손실 + 시가 갭.

    갭 시나리오는 보유 종목 전부가 (현재가 기준) gap_pct 만큼 갭하락해 손절가 아래에서
    체결되는 경우다. 하한가 잠김(매도 불가)은 이 숫자로 막을 수 없다 — 비중 상한으로 대응.
    """
    stop_loss = 0.0
    gap_loss = 0.0
    for p in positions:
        qty = p["qty"]
        price = float(p.get("last_price") or p["avg_price"])
        stop = float(p["stop_price"] or 0)
        stop_loss += max(price - stop, 0) * qty if stop else price * qty
        gap_loss += price * gap_pct * qty
    return {
        "daily_loss_limit": daily_loss_limit,
        "to_stops": round(stop_loss),
        "gap_scenario": round(max(gap_loss, stop_loss)),
        "total": round(daily_loss_limit + max(gap_loss, stop_loss)),
    }

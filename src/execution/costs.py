"""Estimated per-fill costs (fee + sell tax).

KIS 일별주문체결조회는 주문별 수수료·제세금을 주지 않는다. 그래서 체결마다 추정하고
cost_source='estimated' 로 표시한다. 증권사 기간별매매손익(실전 전용 TR)과의 대조는
audit 단계에서 한다. 추정치를 실측처럼 쓰지 않는다.
"""

from __future__ import annotations

import math
from datetime import date

from src.execution.store import FillCost
from src.market.rules import MarketRules


class CostModel:
    def __init__(self, rules: MarketRules, fee_rate: float, market_of):
        """market_of: code -> 'KOSPI' | 'KOSDAQ' | '' (instruments 테이블)."""
        self.rules = rules
        self.fee_rate = fee_rate
        self.market_of = market_of

    def cost(self, code: str, side: str, amount: int, on: date) -> FillCost:
        market = self.market_of(code) or ""
        fee = math.floor(amount * self.fee_rate)
        tax = 0
        if side == "sell":
            if market:
                rate = self.rules.sell_tax_rate(market, on)
            else:
                # 시장을 모르면 가장 높은 세율을 적용한다 (낙관 편향을 피한다).
                rate = max(self.rules.sell_tax_rate(m, on) for m in ("KOSPI", "KOSDAQ"))
            tax = math.floor(amount * rate)
        return FillCost(fee=fee, tax=tax, market=market)

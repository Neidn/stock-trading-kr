"""Daily signal evaluation and entry orders (롱 온리, KRX 정규장).

신호는 **완성된 일봉**으로만 계산한다. 장중에 받은 일봉에는 오늘의 미완성 봉이 섞여
있으므로 오늘 날짜 행을 버린다. 차단된 신호도 사유와 함께 signals 테이블에 남긴다
(shadow 분석용).
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.broker.base import Broker, BrokerError
from src.execution.order_manager import DuplicateOrderBlocked, OrderManager
from src.execution.store import Store
from src.market.rules import MarketRules
from src.risk.kill_switch import KillSwitch
from src.risk.sizing import size_entry
from src.signal.base_strategy import BaseStrategy, SignalResult
from src.utils.config import EngineConfig, RiskConfig
from src.utils.time_utils import ymd

log = logging.getLogger(__name__)


class EntryPlanner:
    def __init__(self, broker: Broker, store: Store, orders: OrderManager, kill: KillSwitch,
                 rules: MarketRules, strategies: list[BaseStrategy], engine_cfg: EngineConfig,
                 risk_cfg: RiskConfig, notify=None):
        self.broker = broker
        self.store = store
        self.orders = orders
        self.kill = kill
        self.rules = rules
        self.strategies = strategies
        self.cfg = engine_cfg
        self.risk = risk_cfg
        self.notify = notify if notify is not None else (lambda *a, **k: None)

    def _min_candles(self) -> int:
        return max((s.get_min_candles() for s in self.strategies), default=0) + 5

    def run(self, now: datetime) -> list[dict]:
        if not self.kill.entries_allowed():
            log.info("entries skipped: kill switch level %d", self.kill.level())
            return []
        submitted: list[dict] = []
        locks = self.store.locked_symbols()
        for code in self.cfg.universe:
            positions = self.store.positions()
            pending = [o for o in self.store.open_orders(side="buy")]
            if len(positions) + len(pending) >= self.risk.max_positions:
                log.info("max positions reached (%d)", self.risk.max_positions)
                break
            if code in locks or code in positions or self.store.open_orders(code=code):
                continue
            inst = self.store.instrument(code)
            if inst and not inst["tradable"]:
                continue
            try:
                order = self._evaluate(code, now)
            except BrokerError as exc:
                log.warning("entry evaluation failed for %s: %s", code, exc)
                continue
            if order:
                submitted.append(order)
        return submitted

    def _evaluate(self, code: str, now: datetime) -> dict | None:
        df = self.broker.daily_candles(code, self._min_candles())
        if not df.empty and "date" in df.columns:
            df = df[df["date"] != ymd(now.date())].reset_index(drop=True)
        for strat in self.strategies:
            if not strat.is_data_sufficient(df):
                continue
            sig = strat.generate_signal(df, code)
            if sig.signal_type in ("none", "hold"):
                continue
            block = self._block_reason(sig)
            if block:
                self._record(code, strat, sig, block)
                continue
            return self._enter(code, strat, sig, now)
        return None

    @staticmethod
    def _block_reason(sig: SignalResult) -> str | None:
        if sig.signal_type != "long":
            return "long-only"
        if not sig.is_actionable():
            return f"strength {sig.strength_score} < 2"
        if sig.sl is None:
            return "no stop"
        return None

    def _enter(self, code: str, strat: BaseStrategy, sig: SignalResult, now: datetime) -> dict | None:
        quote = self.broker.quote(code)
        block = None
        if quote.block_reasons:
            block = "blocked: " + ",".join(quote.block_reasons)
        elif quote.vi_active:
            block = "VI 발동 중"
        elif quote.upper_limit and quote.price >= quote.upper_limit:
            block = "상한가 (매수 체결 불가)"
        if block:
            self._record(code, strat, sig, block)
            return None

        entry = self.rules.round_to_tick(quote.price, "buy", now.date())
        stop = float(sig.sl)
        if stop >= entry:
            self._record(code, strat, sig, f"stop {stop:,.0f} >= entry {entry:,}")
            return None
        size = size_entry(
            entry_price=entry, stop_price=stop,
            risk_per_trade_krw=self.risk.risk_per_trade_krw,
            max_position_krw=self.risk.max_position_krw,
            buyable_cash=self.broker.buyable_cash(code, entry),
            fee_rate=self.risk.fee_rate,
        )
        if size.qty == 0:
            self._record(code, strat, sig, size.reason)
            return None
        tp = int(sig.tp1) if sig.tp1 else None
        try:
            order = self.orders.submit(
                code=code, side="buy", qty=size.qty, order_type="limit", price=entry,
                purpose="entry", strategy_name=strat.get_name(),
                stop_price=int(stop), take_profit=tp, initial_risk=round(entry - stop, 2),
            )
        except DuplicateOrderBlocked as exc:
            self._record(code, strat, sig, str(exc))
            return None
        self._record(code, strat, sig, None if order["state"] != "REJECTED" else
                     f"rejected: {order['reject_reason']}")
        self.notify(f"진입 주문 {code} {size.qty}주 @{entry:,} 손절 {int(stop):,} "
                    f"({strat.get_name()}) → {order['state']}", key=None)
        return order

    def _record(self, code: str, strat: BaseStrategy, sig: SignalResult, block: str | None) -> None:
        self.store.record_signal(
            code=code, strategy_name=strat.get_name(), signal_type=sig.signal_type,
            entry_price=sig.entry_price, stop_price=sig.sl, take_profit=sig.tp1,
            block_reason=block, reason=sig.reason, indicators=sig.indicators,
        )

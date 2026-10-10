"""Tests that need no database."""

from __future__ import annotations

import logging
from datetime import date, datetime, time

import pytest

from src.broker.base import OrderStatus
from src.execution import order_state as st
from src.market.rules import Session
from src.risk.sizing import size_entry, worst_case_loss
from src.utils.time_utils import KST

D = date(2026, 10, 12)


def status(**kw) -> OrderStatus:
    base = dict(order_no="1", org_no="", orig_order_no="", code="005930", side="buy", qty=10,
                price=70000, filled_qty=0, filled_amount=0, remaining_qty=10, cancelled_qty=0,
                rejected_qty=0, is_cancel_order=False, ordered_at=datetime(2026, 10, 12, 9, 10, tzinfo=KST))
    base.update(kw)
    return OrderStatus(**base)


# ── 상태 머신 ────────────────────────────────────────────────────────────────
class TestOrderState:
    def test_accepted_without_fill(self):
        assert st.next_state(st.ACCEPTED, status(), session_over=False) == st.ACCEPTED
        assert st.fill_delta(0, 0, status()) is None

    def test_partial_then_filled(self):
        s1 = status(filled_qty=3, filled_amount=210000, remaining_qty=7)
        assert st.next_state(st.ACCEPTED, s1, session_over=False) == st.PARTIAL
        d = st.fill_delta(0, 0, s1)
        assert (d.qty, d.amount) == (3, 210000)
        s2 = status(filled_qty=10, filled_amount=701000, remaining_qty=0)
        d2 = st.fill_delta(3, 210000, s2)
        assert (d2.qty, d2.amount, d2.cum_qty) == (7, 491000, 10)
        assert st.next_state(st.PARTIAL, s2, session_over=False) == st.FILLED

    def test_same_snapshot_twice_is_not_a_new_fill(self):
        s = status(filled_qty=3, filled_amount=210000, remaining_qty=7)
        assert st.fill_delta(3, 210000, s) is None

    def test_regression_raises(self):
        with pytest.raises(st.FillRegression):
            st.fill_delta(5, 350000, status(filled_qty=3, filled_amount=210000))

    def test_cancelling_stays_until_confirmed(self):
        assert st.next_state(st.CANCELLING, status(), session_over=False) == st.CANCELLING

    def test_cancel_after_partial(self):
        s = status(filled_qty=4, filled_amount=280000, remaining_qty=0, cancelled_qty=6)
        assert st.next_state(st.CANCELLING, s, session_over=False) == st.CANCELLED

    def test_session_end_expires_remaining(self):
        s = status(filled_qty=2, filled_amount=140000, remaining_qty=8)
        assert st.next_state(st.PARTIAL, s, session_over=True) == st.EXPIRED

    def test_terminal_states_stick(self):
        assert st.next_state(st.FILLED, status(), session_over=False) == st.FILLED


# ── 시장 규칙 ────────────────────────────────────────────────────────────────
class TestRules:
    @pytest.mark.parametrize("price,side,expected", [
        (70050, "buy", 70000), (70050, "sell", 70100),
        (19995, "sell", 20000), (19995, "buy", 19990),
        (4998, "sell", 5000), (199950, "sell", 200000), (1999, "buy", 1999),
    ])
    def test_round_to_tick(self, rules, price, side, expected):
        r = rules.round_to_tick(price, side, D)
        assert r == expected
        assert rules.is_valid_price(r, D)

    def test_tax_lookup_by_date(self, rules):
        assert rules.sell_tax_rate("KOSPI", D) == 0.002
        with pytest.raises(LookupError):
            rules.sell_tax_rate("KOSPI", date(2025, 12, 31))

    def test_sessions(self, rules):
        at = lambda h, m: datetime.combine(D, time(h, m), tzinfo=KST)  # noqa: E731
        assert rules.session(at(8, 50)) == Session.PRE_OPEN
        assert rules.session(at(9, 0)) == Session.CONTINUOUS
        assert rules.session(at(15, 25)) == Session.CLOSING_AUCTION
        assert rules.session(at(15, 31)) == Session.AFTER_CLOSE
        unknown_day = datetime(2026, 10, 13, 10, 0, tzinfo=KST)
        assert rules.session(unknown_day) == Session.UNKNOWN
        assert rules.session(unknown_day, assume_open=True) == Session.CONTINUOUS

    def test_session_override(self, rules):
        from src.market.rules import SessionTimes
        rules._overrides[D] = SessionTimes(time(10, 0), time(16, 20), time(16, 30))  # 수능일 예
        assert rules.session(datetime.combine(D, time(9, 30), tzinfo=KST)) == Session.PRE_OPEN


# ── 사이징 ──────────────────────────────────────────────────────────────────
class TestSizing:
    kw = dict(risk_per_trade_krw=50_000, max_position_krw=5_000_000,
              buyable_cash=10_000_000, fee_rate=0.00015)

    def test_risk_based(self):
        assert size_entry(entry_price=70000, stop_price=65000, **self.kw).qty == 10

    def test_less_than_one_share_is_skipped_not_rounded_up(self):
        r = size_entry(entry_price=900_000, stop_price=800_000, **self.kw)
        assert r.qty == 0 and "1주 미만" in r.reason

    def test_capped_by_cash(self):
        kw = {**self.kw, "buyable_cash": 140_000}
        assert size_entry(entry_price=70000, stop_price=65000, **kw).qty == 1

    def test_invalid_stop(self):
        assert size_entry(entry_price=70000, stop_price=71000, **self.kw).qty == 0

    def test_worst_case(self):
        wc = worst_case_loss([{"qty": 10, "avg_price": 70000, "stop_price": 65000,
                               "last_price": 70000}], 0.10, 200_000)
        assert wc["to_stops"] == 50_000 and wc["gap_scenario"] == 70_000
        assert wc["total"] == 270_000


# ── 설정 · 마스킹 ────────────────────────────────────────────────────────────
class TestConfig:
    def test_real_requires_confirmation(self, settings, monkeypatch):
        from src.utils.config import ConfigError, load_kis
        monkeypatch.setenv("KIS_ENV", "real")
        with pytest.raises(ConfigError):
            load_kis()

    def test_fee_rate_has_no_default(self, settings, monkeypatch):
        from src.utils.config import ConfigError, load_risk
        monkeypatch.delenv("FEE_RATE")
        with pytest.raises(ConfigError):
            load_risk()

    def test_demo_defaults(self, settings):
        assert settings.kis.base_url.startswith("https://openapivts")
        assert settings.kis.rps == 2.0
        assert "12345678" not in repr(settings)

    def test_codes_are_strings(self, settings, monkeypatch):
        from src.utils.config import ConfigError, load_engine
        monkeypatch.setenv("UNIVERSE", "5930")
        with pytest.raises(ConfigError):
            load_engine()


def test_log_masking(settings, caplog):
    from src.utils.logger import MaskingFilter, mask_account
    assert mask_account("12345678") == "12****78"
    rec = logging.LogRecord("x", logging.INFO, "", 0, "acct %s key %s auth Bearer abc.def",
                            ("12345678", "test-app-key-123456"), None)
    MaskingFilter().filter(rec)
    assert "12345678" not in rec.msg and "test-app-key" not in rec.msg and "abc.def" not in rec.msg

"""Shared fixtures.

DB 테스트는 실제 PostgreSQL 에 붙는다 (SQL 을 그대로 검증하려고). TEST_DATABASE_URL 이
없으면 DB 테스트는 건너뛴다. 매 테스트마다 public 스키마를 지우고 마이그레이션을 다시 적용한다.

    TEST_DATABASE_URL=postgresql://trading@localhost:5432/trading_test pytest
"""

from __future__ import annotations

import os
from datetime import date, datetime, time

import pytest

from src.utils.time_utils import KST

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
def db(monkeypatch):
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg2

    from src.db import connection
    from src.db.migrate import migrate

    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    connection.reset_pool()
    conn = psycopg2.connect(TEST_DB)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.close()
    migrate()
    yield
    connection.reset_pool()


class Clock:
    def __init__(self, at: datetime):
        self.now = at

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        from datetime import timedelta
        self.now = self.now + timedelta(**kw)


TRADE_DAY = date(2026, 10, 12)   # 월요일


@pytest.fixture
def clock():
    return Clock(datetime.combine(TRADE_DAY, time(10, 0), tzinfo=KST))


@pytest.fixture
def rules():
    from src.market.rules import MarketRules, SessionTimes, TickTable

    ticks = TickTable(date(2023, 1, 25),
                      (0, 2000, 5000, 20000, 50000, 200000, 500000),
                      (1, 5, 10, 50, 100, 500, 1000))
    return MarketRules(
        [ticks],
        {"KOSPI": [(date(2026, 1, 1), 0.002)], "KOSDAQ": [(date(2026, 1, 1), 0.002)]},
        [(date(2000, 1, 1), SessionTimes(time(9, 0), time(15, 20), time(15, 30)))],
        {},
        {TRADE_DAY: True},
    )


class Notes(list):
    def __call__(self, text, key=None):
        self.append(text)


@pytest.fixture
def notes():
    return Notes()


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("KIS_ENV", "demo")
    monkeypatch.setenv("KIS_APP_KEY", "test-app-key-123456")
    monkeypatch.setenv("KIS_APP_SECRET", "test-app-secret-123456")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("FEE_RATE", "0.00015")
    monkeypatch.setenv("UNIVERSE", "005930")
    monkeypatch.setenv("STRATEGIES", "ema_crossover")
    monkeypatch.setenv("RISK_PER_TRADE_KRW", "50000")
    monkeypatch.setenv("MAX_POSITION_KRW", "5000000")
    monkeypatch.setenv("DAILY_LOSS_LIMIT_KRW", "200000")
    from src.utils.config import load_settings
    return load_settings()

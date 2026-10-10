"""KIS client: error classification, token cache, TR mapping (HTTP is faked)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import requests

from src.broker.base import BrokerAuthError, BrokerRateLimited, BrokerRejected, BrokerTransient
from src.broker.kis_client import KisClient, RateLimiter
from src.broker.token_store import MemoryTokenStore
from src.utils.time_utils import now_kst


class Resp:
    def __init__(self, status: int, body: dict | None, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHttp:
    def __init__(self):
        self.queue: list = []
        self.calls: list[tuple[str, str]] = []

    def _next(self, method, url):
        self.calls.append((method, url))
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, **kw):
        return self._next("POST", url)

    def get(self, url, **kw):
        return self._next("GET", url)


TOKEN_OK = Resp(200, {"access_token": "tok", "access_token_token_expired": "2099-01-01 00:00:00"})


@pytest.fixture
def client(settings):
    http = FakeHttp()
    c = KisClient(settings.kis, MemoryTokenStore(), session=http,
                  limiter=RateLimiter(0), sleep=lambda s: None)
    return c, http


def test_tr_mapping(client):
    c, _ = client
    assert c.tr("TTTC0012U") == "VTTC0012U"        # demo
    assert c.tr("FHKST01010100") == "FHKST01010100"  # 시세 TR 은 그대로


def test_token_cached_in_store(settings):
    store = MemoryTokenStore()
    store.save("demo", "cached", now_kst() + timedelta(hours=20))
    http = FakeHttp()
    c = KisClient(settings.kis, store, session=http, limiter=RateLimiter(0))
    assert c.token() == "cached"
    assert http.calls == []          # 새로 발급하지 않았다


def test_token_issue_failure_is_fatal(client):
    c, http = client
    http.queue = [Resp(403, {"error_description": "x"})]
    with pytest.raises(BrokerAuthError):
        c.token()


def test_business_reject(client):
    c, http = client
    http.queue = [TOKEN_OK, Resp(200, {"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "호가단위 오류"})]
    with pytest.raises(BrokerRejected) as e:
        c.post("/x", "VTTC0012U", {})
    assert e.value.msg_cd == "APBK0919"


def test_rate_limit(client):
    c, http = client
    http.queue = [TOKEN_OK, Resp(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과"})]
    with pytest.raises(BrokerRateLimited):
        c.post("/x", "VTTC0012U", {})


def test_order_timeout_is_ambiguous_and_not_retried(client):
    c, http = client
    http.queue = [TOKEN_OK, requests.Timeout()]
    with pytest.raises(BrokerTransient) as e:
        c.post("/x", "VTTC0012U", {})
    assert e.value.ambiguous
    assert [m for m, _ in http.calls] == ["POST", "POST"]   # 토큰 1 + 주문 1, 재시도 없음


def test_query_retries_transient(client):
    c, http = client
    http.queue = [TOKEN_OK, Resp(502, None), Resp(200, {"rt_cd": "0", "output": {}})]
    body, _ = c.get("/q", "FHKST01010100", {})
    assert body["rt_cd"] == "0"


def test_expired_token_refreshes_once(client):
    c, http = client
    http.queue = [TOKEN_OK, Resp(500, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token"}),
                  TOKEN_OK, Resp(200, {"rt_cd": "0"})]
    body, _ = c.get("/q", "FHKST01010100", {})
    assert body["rt_cd"] == "0"
    assert [m for m, _ in http.calls].count("POST") == 2    # 토큰 2회 발급 (거절된 토큰은 캐시에서 제거)

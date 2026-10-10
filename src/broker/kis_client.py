"""Low-level HTTP client for 한국투자증권 OpenAPI.

책임
  * 토큰: DB 공유 캐시 → 만료 전 갱신 → 발급 실패는 BrokerAuthError(치명)
  * 호출 제한: 프로세스 내 최소 호출 간격 (KisConfig.rps)
  * 오류 분류: 거부(rt_cd != '0') / 호출제한 / 일시오류(네트워크·5xx) / 인증

주문(POST)은 자동 재시도하지 않는다. 타임아웃 후 재시도는 이중 주문이 될 수 있으므로
호출자(OrderManager)가 미체결·체결 조회로 접수 여부를 먼저 확인한다.

참고: koreainvestment/open-trading-api examples_user/kis_auth.py (조회일 2026-10-09)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import requests

from src.broker.base import (
    BrokerAuthError,
    BrokerRateLimited,
    BrokerRejected,
    BrokerTransient,
)
from src.broker.token_store import TokenStore
from src.utils.config import KisConfig
from src.utils.time_utils import KST, now_kst

log = logging.getLogger(__name__)

# 메시지 코드 (출처: 개발자센터 공지·커뮤니티. 공식 오류코드 표로는 미확인)
MSG_RATE_LIMIT = "EGW00201"     # 초당 거래건수 초과
MSG_TOKEN_EXPIRED = "EGW00123"  # 기간이 만료된 token
MSG_TOKEN_INVALID = "EGW00121"  # 유효하지 않은 token

TOKEN_REFRESH_MARGIN = timedelta(hours=1)
# 토큰 발급 빈도 제한 대비 최소 간격 (공식 수치 미확인 — 보수적으로 65초)
TOKEN_MIN_ISSUE_INTERVAL = timedelta(seconds=65)


class RateLimiter:
    """Minimum spacing between calls within one process."""

    def __init__(self, rps: float, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()
        self._sleep = sleep
        self._clock = clock

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


class KisClient:
    def __init__(self, cfg: KisConfig, tokens: TokenStore,
                 session: requests.Session | None = None,
                 limiter: RateLimiter | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self._tokens = tokens
        self._http = session or requests.Session()
        self._limiter = limiter or RateLimiter(cfg.rps)
        self._sleep = sleep
        self._token: str | None = None
        self._token_exp: datetime | None = None

    # ── token ────────────────────────────────────────────────────────────────
    def token(self) -> str:
        now = now_kst()
        if self._token and self._token_exp and self._token_exp - now > TOKEN_REFRESH_MARGIN:
            return self._token
        with self._tokens.issue_lock(self.cfg.env):
            cached = self._tokens.get(self.cfg.env)
            if cached and cached[1] - now > TOKEN_REFRESH_MARGIN:
                self._token, self._token_exp = cached[0], cached[1]
                return self._token
            if cached and now - cached[2] < TOKEN_MIN_ISSUE_INTERVAL and cached[1] > now:
                # 방금 누가 발급했는데 만료가 1시간 안쪽이라면 그대로 쓴다 (발급 제한 회피).
                self._token, self._token_exp = cached[0], cached[1]
                return self._token
            token, exp = self._issue_token()
            self._tokens.save(self.cfg.env, token, exp)
            self._token, self._token_exp = token, exp
            log.info("issued new KIS access token (env=%s, expires=%s)", self.cfg.env, exp)
            return token

    def _issue_token(self) -> tuple[str, datetime]:
        body = {
            "grant_type": "client_credentials",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
        }
        try:
            self._limiter.wait()
            res = self._http.post(
                f"{self.cfg.base_url}/oauth2/tokenP",
                data=json.dumps(body),
                headers={"content-type": "application/json"},
                timeout=self.cfg.timeout_sec,
            )
        except requests.RequestException as exc:
            raise BrokerAuthError(f"token issue failed: {type(exc).__name__}") from exc
        if res.status_code != 200:
            raise BrokerAuthError(f"token issue failed: HTTP {res.status_code} {_short(res.text)}")
        data = res.json()
        token = data.get("access_token")
        if not token:
            raise BrokerAuthError("token issue failed: no access_token in response")
        exp_raw = data.get("access_token_token_expired")
        if exp_raw:
            exp = datetime.strptime(exp_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        else:
            exp = now_kst() + timedelta(seconds=int(data.get("expires_in", 0)))
        return token, exp

    def invalidate_token(self) -> None:
        if self._token:
            self._tokens.invalidate(self.cfg.env, self._token)
        self._token = None
        self._token_exp = None

    # ── request ──────────────────────────────────────────────────────────────
    def tr(self, real_tr_id: str) -> str:
        """Map a real-account TR id to the paper one ('TTTC…' → 'VTTC…').

        모의투자는 주문·계좌 TR 의 첫 글자 T/J/C 를 V 로 바꾼다 (공식 샘플 _url_fetch 와 동일).
        시세 TR(F…)은 그대로다.
        """
        if not self.cfg.is_real and real_tr_id[0] in ("T", "J", "C"):
            return "V" + real_tr_id[1:]
        return real_tr_id

    def get(self, path: str, tr_id: str, params: dict[str, Any], *,
            tr_cont: str = "", retries: int = 2) -> tuple[dict, dict]:
        """Query call. Transient failures are retried (queries are idempotent)."""
        attempt = 0
        while True:
            try:
                return self._call("GET", path, tr_id, params, tr_cont)
            except BrokerTransient as exc:
                if attempt >= retries:
                    raise
                attempt += 1
                backoff = 0.5 * (2 ** attempt)
                log.warning("query %s transient (%s), retry %d in %.1fs", tr_id, exc, attempt, backoff)
                self._sleep(backoff)

    def post(self, path: str, tr_id: str, body: dict[str, Any]) -> tuple[dict, dict]:
        """Order call. Never retried here (see module docstring)."""
        return self._call("POST", path, tr_id, body, "")

    def _call(self, method: str, path: str, tr_id: str, payload: dict[str, Any],
              tr_cont: str, _reauth: bool = True) -> tuple[dict, dict]:
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token()}",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "tr_cont": tr_cont,
        }
        url = f"{self.cfg.base_url}{path}"
        self._limiter.wait()
        try:
            if method == "POST":
                res = self._http.post(url, headers=headers, data=json.dumps(payload),
                                      timeout=self.cfg.timeout_sec)
            else:
                res = self._http.get(url, headers=headers, params=payload,
                                     timeout=self.cfg.timeout_sec)
        except requests.Timeout as exc:
            raise BrokerTransient(f"{tr_id} timeout", ambiguous=True) from exc
        except requests.RequestException as exc:
            # 연결 자체가 안 됐는지 응답만 못 받았는지 구분할 수 없다 → ambiguous
            raise BrokerTransient(f"{tr_id} {type(exc).__name__}", ambiguous=True) from exc

        body: dict = {}
        try:
            body = res.json()
        except ValueError:
            pass
        msg_cd = str(body.get("msg_cd", ""))

        if msg_cd == MSG_RATE_LIMIT:
            raise BrokerRateLimited(f"{tr_id} rate limited")
        if msg_cd in (MSG_TOKEN_EXPIRED, MSG_TOKEN_INVALID) or res.status_code == 401:
            self.invalidate_token()
            if _reauth:
                return self._call(method, path, tr_id, payload, tr_cont, _reauth=False)
            raise BrokerAuthError(f"{tr_id} token rejected after refresh ({msg_cd})")
        if res.status_code >= 500 or (res.status_code != 200 and not body):
            raise BrokerTransient(f"{tr_id} HTTP {res.status_code}", ambiguous=True)
        if res.status_code != 200:
            raise BrokerRejected(msg_cd or str(res.status_code), str(body.get("msg1", ""))[:200])
        if str(body.get("rt_cd", "")) != "0":
            raise BrokerRejected(msg_cd, str(body.get("msg1", "")).strip()[:200])
        return body, {k.lower(): v for k, v in res.headers.items()}


def _short(text: str, n: int = 200) -> str:
    return text[:n].replace("\n", " ")

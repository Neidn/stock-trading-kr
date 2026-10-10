"""Telegram alerts (send-only).

* 같은 key 의 경보는 dedupe_sec 동안 한 번만 보낸다 (5분 대조 주기마다 같은 경보가 쌓이지 않게).
* 전송 실패는 로그만 남기고 삼킨다 — 알림 실패가 손절 루프를 멈추면 안 된다.
* 토큰·계좌번호는 메시지에 넣지 않는다. 넣더라도 scrub() 이 가린다.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from src.utils.logger import scrub

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str, chat_id: str, *, prefix: str = "", dedupe_sec: int = 1800,
                 session: requests.Session | None = None):
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix
        self.dedupe_sec = dedupe_sec
        self._http = session or requests.Session()
        self._sent: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def __call__(self, text: str, key: str | None = None) -> None:
        self.send(text, key=key)

    def send(self, text: str, key: str | None = None) -> None:
        if key is not None:
            with self._lock:
                now = time.monotonic()
                last = self._sent.get(key)
                if last is not None and now - last < self.dedupe_sec:
                    return
                self._sent[key] = now
        msg = scrub(f"{self.prefix}{text}")
        log.info("notify: %s", msg.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            res = self._http.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": msg[:4000]},
                timeout=5,
            )
            if res.status_code != 200:
                log.warning("telegram send failed: HTTP %s", res.status_code)
        except requests.RequestException as exc:
            log.warning("telegram send failed: %s", type(exc).__name__)

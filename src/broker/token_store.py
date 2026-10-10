"""Shared access-token cache.

토큰은 DB 한 곳에 두고 모든 프로세스(엔진·크론잡)가 공유한다. 프로세스마다 새로
발급하면 발급 빈도 제한에 걸리고, 재시작 루프가 제한에 걸리면 손절 공백이 생긴다.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Protocol

from src.db.connection import db_conn


class TokenStore(Protocol):
    def get(self, env: str) -> tuple[str, datetime, datetime] | None:
        """Return (token, expires_at, issued_at) or None."""

    def save(self, env: str, token: str, expires_at: datetime) -> None: ...

    def invalidate(self, env: str, token: str) -> None:
        """증권사가 거절한 토큰을 캐시에서 지운다 (다른 프로세스도 재사용하지 않게)."""

    def issue_lock(self, env: str) -> "contextmanager[None]": ...


class PgTokenStore:
    def get(self, env: str):
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT access_token, expires_at, issued_at FROM broker_tokens WHERE env = %s",
                (env,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return row["access_token"], row["expires_at"], row["issued_at"]

    def save(self, env: str, token: str, expires_at: datetime) -> None:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO broker_tokens (env, access_token, expires_at, issued_at)
                   VALUES (%s, %s, %s, now())
                   ON CONFLICT (env) DO UPDATE
                   SET access_token = EXCLUDED.access_token,
                       expires_at   = EXCLUDED.expires_at,
                       issued_at    = now()""",
                (env, token, expires_at),
            )

    def invalidate(self, env: str, token: str) -> None:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM broker_tokens WHERE env = %s AND access_token = %s",
                        (env, token))

    @contextmanager
    def issue_lock(self, env: str) -> Iterator[None]:
        """Cross-process lock so only one process issues a token at a time."""
        key = 911000 + (1 if env == "real" else 2)
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (key,))
            try:
                yield
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))


class MemoryTokenStore:
    """For tests."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, datetime, datetime]] = {}
        self._lock = threading.Lock()

    def get(self, env: str):
        return self._rows.get(env)

    def save(self, env: str, token: str, expires_at: datetime) -> None:
        from src.utils.time_utils import now_kst
        self._rows[env] = (token, expires_at, now_kst())

    def invalidate(self, env: str, token: str) -> None:
        if env in self._rows and self._rows[env][0] == token:
            del self._rows[env]

    @contextmanager
    def issue_lock(self, env: str) -> Iterator[None]:
        with self._lock:
            yield

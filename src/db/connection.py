"""PostgreSQL connection pool.

binance_futures_autotrading 의 src/db/connection.py 와 같은 패턴이다.
모든 호출자는 ``db_conn()`` 컨텍스트로 연결을 빌리고 반납한다.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

_lock = threading.Lock()
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=10,
                    dsn=os.environ["DATABASE_URL"],
                    cursor_factory=psycopg2.extras.RealDictCursor,
                )
    return _pool


def reset_pool() -> None:
    """Close the pool (tests switch DATABASE_URL between runs)."""
    global _pool
    with _lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def db_conn():
    """Borrow a connection; commit on success, rollback on error, always return it."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

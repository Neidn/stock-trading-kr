"""Apply SQL migrations in order and record them in ``schema_migrations``.

파일이 디스크에 있는 것 ≠ 적용된 것. 적용 여부는 schema_migrations 테이블로 판단한다.

Usage:
    python -m src.db.migrate            # 미적용 마이그레이션 적용
    python -m src.db.migrate --status   # 적용 목록만 출력
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from src.db.connection import db_conn

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _ensure_table(cur) -> None:
    cur.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               name TEXT PRIMARY KEY,
               applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
    )


def applied() -> set[str]:
    with db_conn() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute("SELECT name FROM schema_migrations")
        return {r["name"] for r in cur.fetchall()}


def migrate() -> list[str]:
    """Apply pending migrations; each file runs in its own transaction."""
    done = applied()
    newly: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        with db_conn() as conn, conn.cursor() as cur:
            # 동시에 뜬 두 프로세스가 같은 마이그레이션을 돌리지 않게 잠근다.
            cur.execute("SELECT pg_advisory_xact_lock(727274)")
            cur.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (path.name,))
            if cur.fetchone():
                continue
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        log.info("applied migration %s", path.name)
        newly.append(path.name)
    return newly


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        for name in sorted(applied()):
            print(name)
        return
    names = migrate()
    print("applied:", ", ".join(names) if names else "(none)")


if __name__ == "__main__":
    main()

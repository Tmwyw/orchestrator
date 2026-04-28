from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from orchestrator.config import get_config


@contextmanager
def connect():
    cfg = get_config()
    if not cfg.database_url:
        raise RuntimeError("DATABASE_URL is required")
    conn = psycopg.connect(cfg.database_url, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(query: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params or ())
        return list(cur.fetchall())


def fetch_one(query: str, params: Iterable[Any] | None = None) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params or ())
        row = cur.fetchone()
        return dict(row) if row else None


def execute(query: str, params: Iterable[Any] | None = None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params or ())

from __future__ import annotations

from typing import Optional

import psycopg
from psycopg_pool import ConnectionPool

from src.config import get_config

_pool: Optional[ConnectionPool] = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        config = get_config()
        _pool = ConnectionPool(
            config.postgres.connection_string,
            min_size=2,
            max_size=10,
            open=True,
        )
    return _pool


def get_connection():
    return get_pool().getconn()


def put_connection(conn):
    get_pool().putconn(conn)


def close_pools():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None

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
        pool = ConnectionPool(
            config.postgres.connection_string,
            min_size=2,
            max_size=10,
            timeout=config.postgres.pool_timeout_seconds,
            # open=False: construction must never block. open=True calls
            # open(wait=True), which blocks the constructor itself for up to
            # `timeout` trying to fill min_size — and if that raises, `_pool`
            # is never assigned, so the NEXT call reconstructs a fresh pool
            # from scratch and blocks the full timeout again. Against a
            # managed/auto-suspending Postgres (cold start > one timeout
            # window), every request pays that wait independently, forever.
            # open(wait=False) kicks off background connect workers and
            # returns immediately; the pool object is memoized either way, so
            # those workers keep retrying across requests instead of each
            # request restarting the attempt from zero.
            open=False,
        )
        pool.open(wait=False)
        _pool = pool
    return _pool


def get_connection(timeout: float | None = None):
    """Acquire a connection. `timeout` overrides the pool's default wait —
    used for a fast liveness probe that shouldn't hang as long as a real
    query is allowed to."""
    return get_pool().getconn(timeout=timeout)


def put_connection(conn):
    get_pool().putconn(conn)


def close_pools():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None

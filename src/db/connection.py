from __future__ import annotations

from typing import Optional

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from src.config import get_config

_pool: Optional[ConnectionPool] = None
_async_pool: Optional[AsyncConnectionPool] = None


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


async def get_async_pool() -> AsyncConnectionPool:
    """Async pool, for the async checkpointer.

    The pipeline runs on `ainvoke`, so LangGraph calls the checkpointer's
    `aget_tuple`/`aput` — which the synchronous PostgresSaver does not
    implement. The async saver needs an async pool; the sync pool above stays
    as it is for the store's own writes, which are synchronous.
    """
    global _async_pool
    if _async_pool is None:
        config = get_config()
        pool = AsyncConnectionPool(
            config.postgres.connection_string,
            min_size=1,
            max_size=10,
            timeout=config.postgres.pool_timeout_seconds,
            # Same reasoning as the sync pool: construction must not block, and
            # the object must be memoized even if the first connect fails.
            open=False,
        )
        await pool.open(wait=False)
        _async_pool = pool
    return _async_pool


async def close_async_pool():
    global _async_pool
    if _async_pool is not None:
        await _async_pool.close()
        _async_pool = None


def close_pools():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None

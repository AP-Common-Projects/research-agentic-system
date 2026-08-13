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
        _pool = ConnectionPool(
            config.postgres.connection_string,
            min_size=2,
            max_size=10,
            open=True,
        )
    return _pool


def get_async_pool() -> AsyncConnectionPool:
    global _async_pool
    if _async_pool is None:
        config = get_config()
        _async_pool = AsyncConnectionPool(
            config.postgres.async_connection_string,
            min_size=2,
            max_size=10,
            open=True,
        )
    return _async_pool


def get_connection():
    return get_pool().getconn()


def put_connection(conn):
    get_pool().putconn(conn)


async def get_async_connection():
    pool = get_async_pool()
    return await pool.getconn()


async def put_async_connection(conn):
    pool = get_async_pool()
    await pool.putconn(conn)


def close_pools():
    global _pool, _async_pool
    if _pool is not None:
        _pool.close()
        _pool = None
    if _async_pool is not None:
        _async_pool.close()
        _async_pool = None
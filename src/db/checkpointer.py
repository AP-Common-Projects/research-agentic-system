from __future__ import annotations

import psycopg
import structlog
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from src.config import get_config
from src.db.connection import get_async_pool, get_pool

logger = structlog.get_logger(__name__)

_tables_ready = False


def _ensure_checkpoint_tables() -> None:
    """Create the checkpointer's tables on a dedicated autocommit connection.

    PostgresSaver.setup() issues `CREATE INDEX CONCURRENTLY`, which Postgres
    refuses to run inside a transaction block. Connections from the shared pool
    are not autocommit — deliberately, since the store's writes depend on
    explicit commit/rollback — so setup gets its own short-lived connection
    rather than changing the semantics every other caller relies on.

    Idempotent, and memoized: setup() is safe to re-run, but a run should not
    pay for a fresh connection and a migration check on every invocation.
    """
    global _tables_ready
    if _tables_ready:
        return

    config = get_config()
    with psycopg.connect(
        config.postgres.connection_string, autocommit=True
    ) as conn:
        PostgresSaver(conn).setup()

    _tables_ready = True
    logger.info("checkpointer_tables_ready")


def get_checkpointer() -> PostgresSaver:
    """Synchronous saver — for sync callers and inspection only.

    The pipeline itself must use `get_async_checkpointer`; see below.
    """
    _ensure_checkpoint_tables()
    return PostgresSaver(get_pool())


async def get_async_checkpointer() -> AsyncPostgresSaver:
    """The saver the pipeline actually runs on.

    `run_pipeline` drives the graph with `ainvoke`, so LangGraph reaches for
    `aget_tuple`/`aput`. `PostgresSaver` inherits those from the base class as
    `raise NotImplementedError`, so a run with the sync saver fails on the very
    first superstep — before any node executes. Table creation is still done
    synchronously above: both savers use the same tables, and doing it once at
    startup keeps `CREATE INDEX CONCURRENTLY` on its own autocommit connection.
    """
    _ensure_checkpoint_tables()
    return AsyncPostgresSaver(await get_async_pool())

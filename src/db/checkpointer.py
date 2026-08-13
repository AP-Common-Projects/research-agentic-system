from __future__ import annotations

from langgraph.checkpoint.postgres import PostgresSaver

from src.db.connection import get_pool


def get_checkpointer() -> PostgresSaver:
    checkpointer = PostgresSaver(get_pool())
    checkpointer.setup()
    return checkpointer
"""Thin async query helpers for the Postgres structured store.

Track B nodes (compact_branch, synthesize) read from here to honour the
invariant that numbers come from the structured store, not invented by LLMs.
"""

from __future__ import annotations

from typing import Any


class StoreAccess:

    async def get_channels_for_node(
        self, node_id: str, channel_ids: list[str]
    ) -> list[dict[str, Any]]:
        try:
            from src.db.connection import get_async_connection, put_async_connection

            if not channel_ids:
                return []
            conn = await get_async_connection()
            try:
                cur = conn.cursor()
                placeholders = ", ".join(["%s"] * len(channel_ids))
                cur.execute(
                    f"SELECT channel_id, title, subscriber_count, description, first_seen_at, discovery_method FROM channels WHERE channel_id IN ({placeholders})",
                    channel_ids,
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]
            finally:
                await put_async_connection(conn)
        except Exception:
            return []

    async def get_videos_for_channels(
        self, channel_ids: list[str]
    ) -> list[dict[str, Any]]:
        try:
            from src.db.connection import get_async_connection, put_async_connection

            if not channel_ids:
                return []
            conn = await get_async_connection()
            try:
                cur = conn.cursor()
                placeholders = ", ".join(["%s"] * len(channel_ids))
                cur.execute(
                    f"SELECT video_id, channel_id, title, view_count, like_count, comment_count, published_at, outlier_score FROM videos WHERE channel_id IN ({placeholders}) ORDER BY channel_id, published_at DESC",
                    channel_ids,
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]
            finally:
                await put_async_connection(conn)
        except Exception:
            return []

    async def get_all_videos(self) -> list[dict[str, Any]]:
        try:
            from src.db.connection import get_async_connection, put_async_connection

            conn = await get_async_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT video_id, channel_id, title, view_count, like_count, comment_count, published_at, outlier_score FROM videos ORDER BY channel_id, published_at DESC"
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]
            finally:
                await put_async_connection(conn)
        except Exception:
            return []

    async def get_channels_by_ids(
        self, channel_ids: list[str]
    ) -> list[dict[str, Any]]:
        return await self.get_channels_for_node("", channel_ids)


_store: StoreAccess | None = None


def get_store() -> StoreAccess:
    global _store
    if _store is None:
        _store = StoreAccess()
    return _store
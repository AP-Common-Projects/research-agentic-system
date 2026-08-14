"""Sync query helpers for the Postgres structured store, run off the event loop.

Track B nodes (compact_branch, synthesize) read from here to honour the
invariant that numbers come from the structured store, not invented by LLMs.

Uses the sync psycopg pool via asyncio.to_thread so reads never block the
event loop, and so the harness works on Windows (async psycopg requires a
SelectorEventLoop that the default ProactorEventLoop cannot provide).
"""

from __future__ import annotations

import asyncio
from typing import Any


def _fetch_channels(channel_ids: list[str]) -> list[dict[str, Any]]:
    if not channel_ids:
        return []
    from src.db.connection import get_connection, put_connection

    conn = get_connection()
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
        put_connection(conn)


def _fetch_videos(channel_ids: list[str] | None = None) -> list[dict[str, Any]]:
    from src.db.connection import get_connection, put_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        if channel_ids:
            placeholders = ", ".join(["%s"] * len(channel_ids))
            cur.execute(
                f"SELECT video_id, channel_id, title, view_count, like_count, comment_count, published_at, outlier_score FROM videos WHERE channel_id IN ({placeholders}) ORDER BY channel_id, published_at DESC",
                channel_ids,
            )
        else:
            cur.execute(
                "SELECT video_id, channel_id, title, view_count, like_count, comment_count, published_at, outlier_score FROM videos ORDER BY channel_id, published_at DESC"
            )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        put_connection(conn)


def _fetch_discovery_edges(channel_refs: list[str]) -> list[dict[str, Any]]:
    """Discovery edges whose source lies inside the given ref set.

    The ref set for a node is what `_kw_refs`/`_gw_refs` track (handles/URLs).
    `target_channel_ref` is the always-present edge identity (ADR-0006);
    source_channel_id is the UC id, which is only present once the channel
    record has been fetched. Filtering on the *ref* column (both endpoints)
    is what makes the edge set for a node actually resolvable before and
    after hydration.
    """
    if not channel_refs:
        return []
    from src.db.connection import get_connection, put_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        # Match either endpoint against the ref set. Edges are deduplicated
        # by the UNIQUE(source, target_ref, type) constraint, so one pass
        # suffices.
        placeholders = ", ".join(["%s"] * len(channel_refs))
        cur.execute(
            f"SELECT source_channel_id, target_channel_id, target_channel_ref, edge_type FROM discovery_edges WHERE target_channel_ref IN ({placeholders})",
            channel_refs,
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        put_connection(conn)


class StoreAccess:

    async def get_channels_for_node(
        self, node_id: str, channel_ids: list[str]
    ) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(_fetch_channels, channel_ids)
        except Exception:
            return []

    async def get_videos_for_channels(
        self, channel_ids: list[str]
    ) -> list[dict[str, Any]]:
        try:
            if not channel_ids:
                return []
            return await asyncio.to_thread(_fetch_videos, channel_ids)
        except Exception:
            return []

    async def get_all_videos(self) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(_fetch_videos, None)
        except Exception:
            return []

    async def get_channels_by_ids(
        self, channel_ids: list[str]
    ) -> list[dict[str, Any]]:
        return await self.get_channels_for_node("", channel_ids)

    async def get_discovery_edges_for_channels(
        self, channel_refs: list[str]
    ) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(_fetch_discovery_edges, list(channel_refs))
        except Exception:
            return []


_store: StoreAccess | None = None


def get_store() -> StoreAccess:
    global _store
    if _store is None:
        _store = StoreAccess()
    return _store

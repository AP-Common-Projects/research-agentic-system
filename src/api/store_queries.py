"""Read-only Postgres queries for the web API.

Deliberately separate from ``src/nodes/store.py``: that module swallows every
exception and returns ``[]`` so a mid-run store hiccup cannot kill a graph
node. An API must do the opposite — a failed query has to surface as an error
the operator can see, not as an empty table that looks like "no results".
"""

from __future__ import annotations

from typing import Any

from src.db.connection import get_connection, put_connection


def _fetch(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_connection(conn)


def ping(timeout: float = 3.0) -> None:
    """Fast liveness probe for the health banner.

    Deliberately not `store_counts()`: that runs three real aggregates and
    waits on the pool's normal (multi-second) timeout, which makes the
    health check itself a slow way to notice the store is down. This just
    proves a connection is acquirable within `timeout` and gives it straight
    back — cheap enough to poll every 30s indefinitely.
    """
    conn = get_connection(timeout=timeout)
    put_connection(conn)


def store_counts() -> dict[str, int]:
    rows = _fetch(
        """
        SELECT
          (SELECT COUNT(*) FROM channels)        AS channels,
          (SELECT COUNT(*) FROM videos)          AS videos,
          (SELECT COUNT(*) FROM discovery_edges) AS discovery_edges
        """
    )
    return rows[0] if rows else {"channels": 0, "videos": 0, "discovery_edges": 0}


def discovery_method_breakdown() -> list[dict[str, Any]]:
    """Channel counts per discovery track — the headline comparison."""
    return _fetch(
        """
        SELECT COALESCE(NULLIF(discovery_method, ''), 'unattributed') AS discovery_method,
               COUNT(*) AS channel_count
        FROM channels
        GROUP BY 1
        ORDER BY channel_count DESC
        """
    )


def search_channels(query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    if query:
        return _fetch(
            """
            SELECT channel_id, title, subscriber_count, description,
                   first_seen_at, discovery_method
            FROM channels
            WHERE title ILIKE %s OR channel_id ILIKE %s
            ORDER BY subscriber_count DESC NULLS LAST
            LIMIT %s
            """,
            (f"%{query}%", f"%{query}%", limit),
        )
    return _fetch(
        """
        SELECT channel_id, title, subscriber_count, description,
               first_seen_at, discovery_method
        FROM channels
        ORDER BY subscriber_count DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    )


def top_outlier_videos(limit: int = 100) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT v.video_id, v.channel_id, c.title AS channel_title,
               c.discovery_method, v.title, v.view_count, v.like_count,
               v.comment_count, v.outlier_score, v.published_at
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE v.outlier_score IS NOT NULL
        ORDER BY v.outlier_score DESC
        LIMIT %s
        """,
        (limit,),
    )


def discovery_graph(channel_id: str = "", limit: int = 400) -> dict[str, Any]:
    """Nodes + edges for the discovery-graph view.

    Without ``channel_id`` this returns the most recent edges across the whole
    store; with one, it returns that channel's immediate neighbourhood. Node
    rows carry ``discovery_method`` because the graph's entire job is to show
    which channels only the graph-walk track could reach.
    """
    if channel_id:
        edges = _fetch(
            """
            SELECT source_channel_id,
                   COALESCE(target_channel_id, target_channel_ref) AS target_channel_id,
                   edge_type, discovered_at
            FROM discovery_edges
            WHERE source_channel_id = %s
               OR target_channel_id = %s
               OR target_channel_ref = %s
            ORDER BY discovered_at DESC
            LIMIT %s
            """,
            (channel_id, channel_id, channel_id, limit),
        )
    else:
        edges = _fetch(
            """
            -- An edge exists as soon as the walk sees it, which is before the
            -- target has been fetched and given a UC id. Falling back to the
            -- ref keeps the target identifiable, and the placeholder-node pass
            -- below renders it as an unhydrated frontier node rather than
            -- dropping the edge or emitting a null.
            SELECT source_channel_id,
                   COALESCE(target_channel_id, target_channel_ref) AS target_channel_id,
                   edge_type, discovered_at
            FROM discovery_edges
            ORDER BY discovered_at DESC
            LIMIT %s
            """,
            (limit,),
        )

    ids: set[str] = set()
    for e in edges:
        ids.add(e["source_channel_id"])
        ids.add(e["target_channel_id"])

    nodes: list[dict[str, Any]] = []
    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        nodes = _fetch(
            f"""
            SELECT c.channel_id, c.title, c.subscriber_count, c.discovery_method,
                   COALESCE(MAX(v.outlier_score), 0) AS max_outlier_score,
                   COUNT(v.video_id) AS video_count
            FROM channels c
            LEFT JOIN videos v ON v.channel_id = c.channel_id
            WHERE c.channel_id IN ({placeholders})
            GROUP BY c.channel_id, c.title, c.subscriber_count, c.discovery_method
            """,
            tuple(ids),
        )

    # Edges can reference channels that were discovered but never hydrated
    # (the crawl saw the link before metadata was fetched). Emit them as
    # placeholder nodes so the graph shows the real frontier instead of
    # dropping edges to nowhere.
    known = {n["channel_id"] for n in nodes}
    for missing in ids - known:
        nodes.append(
            {
                "channel_id": missing,
                "title": None,
                "subscriber_count": None,
                "discovery_method": "unhydrated",
                "max_outlier_score": 0,
                "video_count": 0,
            }
        )

    return {"nodes": nodes, "edges": edges}

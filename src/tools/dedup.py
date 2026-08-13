"""Tree-wide dedup and entity cache — upserts keyed on natural keys."""

from __future__ import annotations

import difflib
import string
from typing import Any


def is_known_channel(channel_id: str, known_ids: set[str]) -> bool:
    return channel_id in known_ids


def check_near_duplicate(
    title_a: str,
    title_b: str,
    similarity_threshold: float = 0.92,
) -> bool:
    if not title_a or not title_b:
        return False

    def normalize(t: str) -> str:
        t = t.lower().translate(str.maketrans("", "", string.punctuation))
        return " ".join(t.split())

    na = normalize(title_a)
    nb = normalize(title_b)
    if not na or not nb:
        return False

    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return ratio >= similarity_threshold


def persist_channel(conn: Any, channel: dict) -> None:
    sql = """
        INSERT INTO channels (channel_id, title, subscriber_count, description,
            first_seen_at, discovery_method, extra)
        VALUES (%(channel_id)s, %(title)s, %(subscriber_count)s, %(description)s,
            %(first_seen_at)s, %(discovery_method)s, %(extra)s)
        ON CONFLICT (channel_id) DO UPDATE SET
            title = EXCLUDED.title,
            subscriber_count = EXCLUDED.subscriber_count,
            description = EXCLUDED.description,
            extra = EXCLUDED.extra
    """
    cur = conn.cursor()
    try:
        cur.execute(
            sql,
            {
                "channel_id": channel.get("channel_id", ""),
                "title": channel.get("title", ""),
                "subscriber_count": channel.get("subscriber_count", 0),
                "description": channel.get("description", ""),
                "first_seen_at": channel.get("first_seen_at", "now"),
                "discovery_method": channel.get("discovery_method", "unknown"),
                "extra": channel.get("extra", "{}"),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def persist_video(conn: Any, video: dict) -> None:
    sql = """
        INSERT INTO videos (video_id, channel_id, title, view_count, like_count,
            comment_count, published_at, outlier_score, scraped_at, extra)
        VALUES (%(video_id)s, %(channel_id)s, %(title)s, %(view_count)s, %(like_count)s,
            %(comment_count)s, %(published_at)s, %(outlier_score)s, %(scraped_at)s, %(extra)s)
        ON CONFLICT (video_id) DO UPDATE SET
            title = EXCLUDED.title,
            view_count = EXCLUDED.view_count,
            like_count = EXCLUDED.like_count,
            comment_count = EXCLUDED.comment_count,
            outlier_score = EXCLUDED.outlier_score,
            scraped_at = EXCLUDED.scraped_at,
            extra = EXCLUDED.extra
    """
    cur = conn.cursor()
    try:
        cur.execute(
            sql,
            {
                "video_id": video.get("video_id", ""),
                "channel_id": video.get("channel_id", ""),
                "title": video.get("title", ""),
                "view_count": video.get("view_count", 0),
                "like_count": video.get("like_count", 0),
                "comment_count": video.get("comment_count", 0),
                "published_at": video.get("published_at"),
                "outlier_score": video.get("outlier_score"),
                "scraped_at": video.get("scraped_at", "now"),
                "extra": video.get("extra", "{}"),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def persist_edge(conn: Any, edge: dict) -> None:
    sql = """
        INSERT INTO discovery_edges (source_channel_id, target_channel_id, edge_type,
            discovered_at, run_id)
        VALUES (%(source)s, %(target)s, %(edge_type)s, %(discovered_at)s, %(run_id)s)
        ON CONFLICT (source_channel_id, target_channel_id, edge_type) DO NOTHING
    """
    cur = conn.cursor()
    try:
        cur.execute(
            sql,
            {
                "source": edge.get("source_channel_id", ""),
                "target": edge.get("target_channel_id", ""),
                "edge_type": edge.get("edge_type", "playlist"),
                "discovered_at": edge.get("discovered_at", "now"),
                "run_id": edge.get("run_id", ""),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
"""Tree-wide dedup and entity cache — upserts keyed on natural keys."""

from __future__ import annotations

import difflib
import json
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
            COALESCE(NULLIF(%(first_seen_at)s, '')::timestamptz, NOW()),
            %(discovery_method)s, %(extra)s::jsonb)
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
                "first_seen_at": channel.get("first_seen_at") or None,
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
            %(comment_count)s, NULLIF(%(published_at)s, '')::timestamptz, %(outlier_score)s,
            COALESCE(NULLIF(%(scraped_at)s, '')::timestamptz, NOW()), %(extra)s::jsonb)
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
                "published_at": video.get("published_at") or None,
                "outlier_score": video.get("outlier_score"),
                "scraped_at": video.get("scraped_at") or None,
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
    """Upsert one discovery edge.

    Keyed on the target *ref* rather than its UC id, because that is the only
    identifier present at discovery time. When a later round expands that ref
    and learns its id, the ON CONFLICT branch fills it in — so re-walking a
    channel enriches the edge instead of duplicating it, and stays a genuine
    no-op once there is nothing left to learn.
    """
    sql = """
        INSERT INTO discovery_edges (source_channel_id, target_channel_id,
            target_channel_ref, edge_type, discovered_at, run_id)
        VALUES (%(source)s, %(target)s, %(target_ref)s, %(edge_type)s,
                COALESCE(%(discovered_at)s, NOW()), %(run_id)s)
        ON CONFLICT (source_channel_id, target_channel_ref, edge_type)
        DO UPDATE SET target_channel_id =
            COALESCE(discovery_edges.target_channel_id, EXCLUDED.target_channel_id)
    """
    cur = conn.cursor()
    try:
        cur.execute(
            sql,
            {
                "source": edge.get("source_channel_id", ""),
                "target": edge.get("target_channel_id") or None,
                "target_ref": edge.get("target_channel_ref", ""),
                "edge_type": edge.get("edge_type", "featured_channel"),
                "discovered_at": edge.get("discovered_at") or None,
                "run_id": edge.get("run_id", ""),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def persist_edges(conn: Any, edges: list[dict]) -> int:
    """Persist a batch, skipping individual failures.

    One malformed edge must not cost the whole round's graph data — the count
    returned lets the caller report how many actually landed.
    """
    written = 0
    for edge in edges:
        if not edge.get("source_channel_id") or not edge.get("target_channel_ref"):
            continue
        try:
            persist_edge(conn, edge)
            written += 1
        except Exception:
            continue
    return written


def fetch_videos_by_channels(conn: Any, channel_ids: list[str]) -> list[dict]:
    if not channel_ids:
        return []
    cur = conn.cursor()
    try:
        placeholders = ", ".join(["%s"] * len(channel_ids))
        cur.execute(
            f"SELECT video_id, channel_id, title, view_count, like_count, comment_count, published_at, outlier_score FROM videos WHERE channel_id IN ({placeholders}) ORDER BY channel_id, published_at DESC NULLS LAST",
            channel_ids,
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        cur.close()


def persist_channel_signals(conn: Any, channel_id: str, signals: dict) -> None:
    sql = """
        UPDATE channels SET extra = extra || %(signals)s::jsonb
        WHERE channel_id = %(channel_id)s
    """
    cur = conn.cursor()
    try:
        cur.execute(
            sql,
            {
                "channel_id": channel_id,
                "signals": json.dumps(signals),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

def persist_category_tags(
    conn: Any,
    run_id: str,
    tree_node_id: str,
    channel_ids: list[str],
    video_ids: list[str],
) -> int:
    """Record which entities belong to which branch of which run.

    This is what makes a per-run export possible without run_id columns on
    `channels` and `videos` — those are shared entities, and a channel can
    legitimately belong to a Finance run and a Legal one at once. The table
    existed in the schema from the start and nothing ever wrote to it, so the
    store could not answer "which channels came from this run".

    Idempotent: re-running a branch re-tags the same rows to no effect.
    """
    if not run_id or not tree_node_id:
        return 0

    sql = """
        INSERT INTO category_tags (entity_type, entity_id, tree_node_id, run_id)
        VALUES (%(etype)s, %(eid)s, %(node)s, %(run)s)
        ON CONFLICT (entity_type, entity_id, tree_node_id, run_id) DO NOTHING
    """
    written = 0
    cur = conn.cursor()
    try:
        for etype, ids in (("channel", channel_ids), ("video", video_ids)):
            for eid in ids:
                if not eid:
                    continue
                cur.execute(
                    sql,
                    {"etype": etype, "eid": eid, "node": tree_node_id, "run": run_id},
                )
                written += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return written


# ---------------------------------------------------------------------------
# v3 dataset-first enrichment upserts
# ---------------------------------------------------------------------------

def persist_channel_v3(conn: Any, channel_id: str, run_id: str, fields: dict) -> None:
    """Update v3 enrichment columns for a channel that already exists.

    Every real caller (hydrate_metadata, resolve_geo_language,
    classify_channel, extract_success_failure_factors) only ever enriches a
    channel row persist_channel() already created — so this is a plain
    UPDATE, not an upsert. That matters beyond style: an
    `INSERT ... ON CONFLICT DO UPDATE` still has to construct a full
    candidate row and satisfy every NOT NULL constraint on it (title,
    discovery_method) even when the conflict means it will just become an
    UPDATE — Postgres validates NOT NULL at tuple construction, before
    conflict resolution runs. A prior version of this function used that
    pattern, which (once a separate column-naming bug in the same query was
    fixed) raised NotNullViolation on every real call. UPDATE only touches
    the columns named in SET, so it never hits that trap, and correctly
    no-ops on a channel_id that doesn't exist rather than inventing a
    half-populated row.

    first_discovered_run_id is set only if it's still NULL (the original
    discovering run should never be overwritten); last_enriched_run_id
    always moves to this run.
    """
    setters: list[str] = []
    params: dict[str, Any] = {"channel_id": channel_id, "run_id": run_id}
    v3_cols = {
        "country_code", "country_source", "country_confidence", "region",
        "is_us_market", "primary_language_code", "audience_language_code",
        "language_confidence", "face_status", "dominant_format",
        "primary_niche_id", "meets_subscriber_floor", "floor_override_reason",
        "evergreen_score", "is_likely_news", "engagement_score",
        "entertainment_score",
        "engagement_components", "priority_score", "has_affiliate_signal",
        "has_sponsor_signal", "has_membership_signal", "uploads_per_week_avg",
        "upload_consistency_score", "data_completeness_score",
        "missing_required_fields", "classifier_model", "classifier_version",
        "first_video_published_at",
    }
    for col in v3_cols:
        if col in fields:
            setters.append(f"{col} = %(p_{col})s")
            value = fields[col]
            if isinstance(value, dict):
                # engagement_components is JSONB — psycopg cannot adapt a
                # raw dict to a placeholder ("cannot adapt type 'dict'"),
                # so every score_signals call silently failed to persist
                # any v3 field for a channel that had a components dict,
                # discarding evergreen_score/engagement_score/
                # meets_subscriber_floor along with it (the whole UPDATE is
                # one statement, so one bad column fails the entire call).
                from psycopg.types.json import Json

                value = Json(value)
            params[f"p_{col}"] = value
    if not setters:
        return
    sql = f"""
        UPDATE channels SET
            {', '.join(setters)},
            first_discovered_run_id = COALESCE(first_discovered_run_id, %(run_id)s),
            last_enriched_run_id = %(run_id)s,
            updated_at = now()
        WHERE channel_id = %(channel_id)s
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def persist_channel_snapshot(conn: Any, channel_id: str, run_id: str,
                              subscriber_count: int, view_count: int, video_count: int,
                              long_video_count: int | None = None,
                              shorts_count: int | None = None,
                              live_stream_count: int | None = None) -> None:
    """One row per channel per run — longitudinal tracking.

    The three breakdown counts are optional because they cost 4 extra
    quota units per channel; a caller that skipped the lookup passes None
    and the columns stay NULL, which reads as "not looked up" rather than
    a misleading zero.
    """
    sql = """
        INSERT INTO channel_snapshots (channel_id, run_id, subscriber_count, total_view_count,
                                       total_video_count, long_video_count, shorts_count, live_stream_count)
        VALUES (%(channel_id)s, %(run_id)s, %(subs)s, %(views)s, %(vids)s,
                %(long)s, %(shorts)s, %(live)s)
        ON CONFLICT (channel_id, run_id) DO NOTHING
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, {
            "channel_id": channel_id,
            "run_id": run_id,
            "subs": subscriber_count or 0,
            "views": view_count or 0,
            "vids": video_count or 0,
            "long": long_video_count,
            "shorts": shorts_count,
            "live": live_stream_count,
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def persist_video_v3(conn: Any, video_id: str, fields: dict) -> None:
    """Update v3 enrichment columns for a video that already exists.

    Same reasoning as persist_channel_v3: every real caller enriches a row
    persist_video() already created, so this is a plain UPDATE rather than
    an INSERT ... ON CONFLICT DO UPDATE — which would have to satisfy
    channel_id/title NOT NULL on a candidate row it never actually needs to
    insert.
    """
    setters: list[str] = []
    params: dict[str, Any] = {"video_id": video_id}
    v3_cols = {
        "hashtags", "duration_seconds", "is_short", "language_code",
        "evergreen_score", "is_likely_news", "views_per_day_since_publish",
        "title_char_count", "title_word_count", "title_has_number",
        "title_is_question", "title_capitalization", "title_emoji_count",
        "thumbnail_has_face", "thumbnail_text_density",
        "data_completeness_score", "missing_required_fields",
        "video_description",
    }
    for col in v3_cols:
        if col in fields:
            setters.append(f"{col} = %(p_{col})s")
            params[f"p_{col}"] = fields[col]
    if not setters:
        return
    sql = f"""
        UPDATE videos SET {', '.join(setters)}
        WHERE video_id = %(video_id)s
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def persist_channel_niche_membership(conn: Any, channel_id: str, niche_id: int,
                                      is_primary: bool = False, confidence: float | None = None) -> None:
    """Record a channel's canonical niche membership."""
    sql = """
        INSERT INTO channel_niches (channel_id, niche_id, is_primary, confidence)
        VALUES (%(channel_id)s, %(niche_id)s, %(primary)s, %(conf)s)
        ON CONFLICT (channel_id, niche_id) DO UPDATE SET
            is_primary = EXCLUDED.is_primary,
            confidence = EXCLUDED.confidence
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, {
            "channel_id": channel_id,
            "niche_id": niche_id,
            "primary": is_primary,
            "conf": confidence,
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

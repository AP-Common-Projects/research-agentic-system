"""Deterministic signal computation — no LLM.

Engagement rate, upload cadence, view velocity. score_signals reads
hydrated data from Postgres, computes signals, and persists results.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def compute_engagement_rate(video: dict) -> float:
    views = int(video.get("view_count") or video.get("views") or 0)
    likes = int(video.get("like_count") or video.get("likes") or 0)
    comments = int(video.get("comment_count") or video.get("comments") or 0)
    if views == 0:
        return 0.0
    return round((likes + comments) / views, 6)


def compute_cadence(videos: list[dict]) -> float:
    if len(videos) < 2:
        return 0.0

    # Parse once, drop unparseable rows, then sort. Sorting by a key that can
    # return None raises TypeError the moment one row lacks a usable date.
    timestamps = [
        ts for ts in (_parse_timestamp(v.get("published_at")) for v in videos)
        if ts is not None
    ]

    if len(timestamps) < 2:
        return 0.0

    newest = max(timestamps)
    oldest = min(timestamps)
    span_days = (newest - oldest).total_seconds() / 86400.0
    if span_days <= 0:
        return 0.0

    return round(len(timestamps) / span_days * 30, 2)


def compute_velocity(videos: list[dict]) -> float:
    if len(videos) < 6:
        return 1.0

    dated = [(ts, v) for v in videos
             if (ts := _parse_timestamp(v.get("published_at"))) is not None]
    if len(dated) < 6:
        return 1.0
    sorted_videos = [v for _, v in sorted(dated, key=lambda p: p[0], reverse=True)]

    recent = sorted_videos[:5]
    older = sorted_videos[5:15]

    recent_views = [int(v.get("view_count") or v.get("views") or 0) for v in recent]
    older_views = [int(v.get("view_count") or v.get("views") or 0) for v in older]

    if not recent_views or not older_views:
        return 1.0

    recent_mean = sum(recent_views) / len(recent_views)
    older_mean = sum(older_views) / len(older_views)

    if older_mean == 0:
        return 1.0 if recent_mean == 0 else 2.0

    return round(recent_mean / older_mean, 4)


def _parse_timestamp(ts: Any) -> datetime | None:
    """Accept what the store actually hands back, not just what the API does.

    `published_at` arrives as an ISO *string* from the YouTube API but as a
    `datetime` from Postgres, because the column is TIMESTAMPTZ and psycopg
    adapts it. `strptime` raises TypeError on a datetime — and the loop below
    only caught ValueError, so it escaped, and score_signals wrapped its whole
    channel loop in one try/except, so the first row killed every signal for
    the round.

    Net effect before this fix: engagement_rate, cadence and velocity were
    never computed or stored in any run — 425 channels, zero signals — while
    the node still logged a channels_scored count and reported success.
    """
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if not isinstance(ts, str):
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def score_signals(state: dict) -> dict:
    from collections import defaultdict

    from src.db.connection import get_connection, put_connection
    from src.tools.dedup import fetch_videos_by_channels, persist_channel_signals
    from src.state import ErrorRecord, NodeLog

    channel_ids = state.get("discovered_channel_ids", [])
    thread_id = state.get("thread_id", "")
    errors: list[dict] = []
    scored = 0

    if not channel_ids:
        return {"next_action": "continue"}

    try:
        conn = get_connection()
        try:
            videos = fetch_videos_by_channels(conn, channel_ids)
            by_channel: dict[str, list[dict]] = defaultdict(list)
            for v in videos:
                by_channel[v.get("channel_id", "")].append(v)

            for ch_id, vids in by_channel.items():
                # Per channel. This loop used to sit inside the single outer
                # try/except, so the first row that raised discarded every
                # signal for the round — which is precisely what happened, on
                # every run, for the whole life of the project.
                try:
                    engagement = round(
                        sum(compute_engagement_rate(v) for v in vids) / len(vids), 6
                    ) if vids else 0.0
                    signals = {
                        "engagement_rate": engagement,
                        "cadence": compute_cadence(vids),
                        "velocity": compute_velocity(vids),
                    }
                    persist_channel_signals(conn, ch_id, signals)
                    scored += 1
                except Exception as exc:
                    errors.append(
                        ErrorRecord(
                            node_name="score_signals",
                            error_type=type(exc).__name__,
                            message=f"signal scoring failed for {ch_id}: {exc}",
                            recoverable=True,
                        ).model_dump()
                    )
        finally:
            put_connection(conn)
    except Exception as exc:
        errors.append(
            ErrorRecord(
                node_name="score_signals",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=False,
            ).model_dump()
        )

    return {
        "next_action": "continue",
        "errors": errors,
        "node_logs": [
            NodeLog(
                node_name="score_signals",
                thread_id=thread_id,
                input_summary={
                    # What actually succeeded. This previously reported
                    # len(channel_ids) — so it read "50 scored" on rounds
                    # where zero signals were computed or stored.
                    "channels_scored": scored,
                    "channels_attempted": len(channel_ids),
                    "scoring_errors": len(errors),
                },
                cost_usd=0.0,
            ).model_dump()
        ],
    }
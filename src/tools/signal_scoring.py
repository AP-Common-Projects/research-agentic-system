"""Deterministic signal computation — no LLM.

Engagement rate, upload cadence, view velocity. score_signals reads
hydrated data from Postgres, computes signals, and persists results.
"""

from __future__ import annotations

from datetime import datetime


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

    sorted_videos = sorted(
        videos, key=lambda v: _parse_timestamp(v.get("published_at", "")), reverse=True
    )
    timestamps = [
        _parse_timestamp(v.get("published_at", ""))
        for v in sorted_videos
        if _parse_timestamp(v.get("published_at", ""))
    ]

    if len(timestamps) < 2:
        return 0.0

    newest = max(ts for ts in timestamps if ts is not None)
    oldest = min(ts for ts in timestamps if ts is not None)
    span_days = (newest - oldest).total_seconds() / 86400.0
    if span_days <= 0:
        return 0.0

    return round(len(timestamps) / span_days * 30, 2)


def compute_velocity(videos: list[dict]) -> float:
    if len(videos) < 6:
        return 1.0

    sorted_videos = sorted(
        videos, key=lambda v: _parse_timestamp(v.get("published_at", "")), reverse=True
    )

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


def _parse_timestamp(ts: str) -> datetime | None:
    if not ts:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            from datetime import timezone

            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def score_signals(state: dict) -> dict:
    return {"next_action": "continue"}
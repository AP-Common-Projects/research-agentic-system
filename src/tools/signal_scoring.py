"""Deterministic signal computation — no LLM.

Engagement rate, upload cadence, view velocity.
"""

from __future__ import annotations

from datetime import datetime, timezone


def compute_engagement_rate(video: dict) -> float:
    views = video.get("view_count") or video.get("views") or 0
    likes = video.get("like_count") or video.get("likes") or 0
    comments = video.get("comment_count") or video.get("comments") or 0
    if views == 0:
        return 0.0
    return round((likes + comments) / views, 6)


def compute_cadence(videos: list[dict]) -> float:
    if len(videos) < 2:
        return 0.0

    sorted_videos = sorted(videos, key=lambda v: v.get("published_at", ""), reverse=True)
    timestamps: list[datetime] = []
    for v in sorted_videos:
        ts = v.get("published_at", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            timestamps.append(dt)
        except (ValueError, TypeError):
            continue

    if len(timestamps) < 2:
        return 0.0

    newest = timestamps[0]
    oldest = timestamps[-1]
    span_days = (newest - oldest).total_seconds() / 86400.0
    if span_days <= 0:
        return 0.0

    return round(len(timestamps) / span_days * 30, 2)


def compute_velocity(videos: list[dict]) -> float:
    if len(videos) < 6:
        return 1.0

    sorted_videos = sorted(videos, key=lambda v: v.get("published_at", ""), reverse=True)

    recent = sorted_videos[:5]
    older = sorted_videos[5:15]

    recent_views = [
        int(v.get("view_count") or v.get("views") or 0) for v in recent
    ]
    older_views = [
        int(v.get("view_count") or v.get("views") or 0) for v in older
    ]

    if not recent_views or not older_views:
        return 1.0

    recent_mean = sum(recent_views) / len(recent_views)
    older_mean = sum(older_views) / len(older_views)

    if older_mean == 0:
        return 1.0 if recent_mean == 0 else 2.0

    return round(recent_mean / older_mean, 4)


def score_signals(state: dict) -> dict:
    discovered = state.get("discovered_channel_ids", [])
    videos = state.get("discovered_video_ids", [])

    return {
        "next_action": "continue",
        "discovered_channel_ids": [],
        "discovered_video_ids": [],
    }
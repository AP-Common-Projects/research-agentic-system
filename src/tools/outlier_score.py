"""Outlier score — per-video views vs. local channel window.

outlier_score(video) = video.views / mean(views of the ~9-10 videos
                        this channel published immediately before and after it)

Known limitation: distorted on rapidly growing/declining channels where
the "normal" baseline shifts faster than window_size captures.
"""

from __future__ import annotations


def compute_outlier_score(video_views: int, window_views: list[int]) -> float | None:
    if not window_views:
        return None
    total = sum(window_views)
    if total == 0:
        return None
    mean = total / len(window_views)
    return round(video_views / mean, 4)


def build_window(
    videos: list[dict],
    video_id: str,
    window_size: int = 5,
) -> list[int]:
    views_list: list[int] = []

    target_idx: int | None = None
    for i, v in enumerate(videos):
        if v.get("video_id") == video_id or v.get("id") == video_id:
            target_idx = i
            break

    if target_idx is None:
        return views_list

    start = max(0, target_idx - window_size)
    end = min(len(videos), target_idx + window_size + 1)

    for i in range(start, end):
        if i == target_idx:
            continue
        views = videos[i].get("view_count") or videos[i].get("views") or 0
        views_list.append(int(views))

    return views_list


def score_channel_videos(videos: list[dict]) -> list[dict]:
    scored: list[dict] = []
    for video in videos:
        vid = dict(video)
        vid_id = vid.get("video_id") or vid.get("id") or ""
        views = vid.get("view_count") or vid.get("views") or 0
        window = build_window(videos, vid_id)
        score = compute_outlier_score(int(views), window)
        vid["outlier_score"] = score
        scored.append(vid)
    return scored
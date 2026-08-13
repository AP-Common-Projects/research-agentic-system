"""YouTube Data API v3 client with quota tracking and rate-limit handling."""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.config import get_config

logger = structlog.get_logger(__name__)

DAILY_QUOTA_CEILING = 10000
QUOTA_CEILING_TARGET_RATIO = 0.90

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _is_retryable(exception: BaseException) -> bool:
    if isinstance(exception, httpx.HTTPStatusError):
        code = exception.response.status_code
        if code == 403:
            try:
                body = exception.response.json()
            except Exception:
                body = {}
            reason = ""
            if isinstance(body, dict):
                for err in body.get("error", {}).get("errors", []):
                    reason = err.get("reason", "")
            if reason == "quotaExceeded":
                return False
        return code in RETRYABLE_STATUSES
    if isinstance(exception, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    return False


def _create_retry_decorator():
    return retry(
        wait=wait_exponential_jitter(initial=1, max=60, jitter=2),
        stop=stop_after_attempt(5),
        retry=retry_if_exception(_is_retryable),
        before_sleep=lambda retry_state: logger.warning(
            "youtube_api_retry",
            attempt=retry_state.attempt_number,
            exc_type=type(retry_state.outcome.exception()).__name__,
        ),
    )


class YouTubeAPIClient:
    BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self) -> None:
        cfg = get_config()
        self._api_key = cfg.youtube.api_key
        self._quota_used: int = 0

    @property
    def quota_used(self) -> int:
        return self._quota_used

    def get_quota_used(self) -> int:
        return self._quota_used

    def seed_quota_used(self, units: int) -> None:
        """Carry a run's accumulated quota into a freshly constructed client.

        hydrate_metadata builds a new client every round, so without this the
        instance counter restarts at zero each time and `check_quota` can never
        see a run approaching the daily ceiling.
        """
        self._quota_used = max(self._quota_used, int(units or 0))

    def quota_consumed_this_call(self, baseline: int) -> int:
        """Units spent since `baseline` — what the caller reports into state."""
        return max(0, self._quota_used - int(baseline or 0))

    def check_quota(self, planned_units: int) -> bool:
        cfg = get_config().harness
        ceiling = int(cfg.youtube_daily_quota_ceiling * cfg.youtube_quota_target_ratio)
        return (self._quota_used + planned_units) <= ceiling

    def _track_quota(self, units: int) -> None:
        self._quota_used += units

    @_create_retry_decorator()
    def _get(self, endpoint: str, params: dict[str, Any]) -> dict:
        params["key"] = self._api_key
        response = httpx.get(
            f"{self.BASE_URL}/{endpoint}",
            params=params,
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    def get_channels(self, channel_ids: list[str]) -> list[dict]:
        results: list[dict] = []
        for i in range(0, len(channel_ids), 50):
            batch = channel_ids[i : i + 50]
            if not self.check_quota(1):
                logger.error("quota_ceiling_hit", quota_used=self._quota_used)
                break
            data = self._get(
                "channels",
                {
                    "part": "snippet,statistics,contentDetails",
                    "id": ",".join(batch),
                    "maxResults": 50,
                },
            )
            self._track_quota(1)
            for item in data.get("items", []):
                results.append(self._parse_channel(item))
        return results

    def get_channel_videos(
        self, channel_id: str, max_results: int = 50
    ) -> list[dict]:
        if not self.check_quota(1):
            logger.error("quota_ceiling_hit", quota_used=self._quota_used)
            return []

        channels_data = self._get(
            "channels",
            {
                "part": "contentDetails",
                "id": channel_id,
            },
        )
        self._track_quota(1)

        items = channels_data.get("items", [])
        if not items:
            return []

        uploads_playlist = (
            items[0]
            .get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads", "")
        )
        if not uploads_playlist:
            return []

        if not self.check_quota(1):
            return []

        try:
            playlist_data = self._get(
                "playlistItems",
                {
                    "part": "snippet",
                    "playlistId": uploads_playlist,
                    "maxResults": min(max_results, 50),
                },
            )
        except httpx.HTTPStatusError as exc:
            # A channel can advertise an uploads playlist that 404s — no public
            # uploads, or the channel was terminated between discovery and
            # hydration. That is a fact about one channel, not a run failure,
            # but hydrate_metadata is the fan-in join and is NOT wrapped by
            # graph.py's _guarded, so an escape here kills the whole run. One
            # such channel ended a paid run after 64 records.
            if exc.response.status_code in (403, 404):
                logger.warning(
                    "youtube_uploads_playlist_unavailable",
                    channel_id=channel_id,
                    playlist_id=uploads_playlist,
                    status=exc.response.status_code,
                )
                self._track_quota(1)
                return []
            raise
        self._track_quota(1)

        video_ids = [
            item["snippet"]["resourceId"]["videoId"]
            for item in playlist_data.get("items", [])
            if "snippet" in item
            and "resourceId" in item["snippet"]
            and "videoId" in item["snippet"]["resourceId"]
        ]

        if not video_ids:
            return []

        return self.get_videos(video_ids)

    def get_videos(self, video_ids: list[str]) -> list[dict]:
        results: list[dict] = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            if not self.check_quota(1):
                logger.error("quota_ceiling_hit", quota_used=self._quota_used)
                break
            data = self._get(
                "videos",
                {
                    "part": "snippet,statistics",
                    "id": ",".join(batch),
                    "maxResults": 50,
                },
            )
            self._track_quota(1)
            for item in data.get("items", []):
                results.append(self._parse_video(item))
        return results

    def _parse_channel(self, item: dict) -> dict:
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        return {
            "channel_id": item.get("id", ""),
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "subscriber_count": int(stats.get("subscriberCount", 0)),
            "video_count": int(stats.get("videoCount", 0)),
            "view_count": int(stats.get("viewCount", 0)),
            "published_at": snippet.get("publishedAt", ""),
            "thumbnails": snippet.get("thumbnails", {}),
        }

    def _parse_video(self, item: dict) -> dict:
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        return {
            "video_id": item.get("id", ""),
            "channel_id": snippet.get("channelId", ""),
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "view_count": int(stats.get("viewCount", 0)),
            "like_count": int(stats.get("likeCount", 0)),
            "comment_count": int(stats.get("commentCount", 0)),
            "published_at": snippet.get("publishedAt", ""),
        }
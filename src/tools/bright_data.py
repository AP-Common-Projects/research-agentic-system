"""Bright Data Scraper API client with concurrency control and retry discipline."""

from __future__ import annotations

import asyncio
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

MAX_CONCURRENCY = 10

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _is_retryable(exception: BaseException) -> bool:
    if isinstance(exception, httpx.HTTPStatusError):
        code = exception.response.status_code
        if 400 <= code < 500 and code not in RETRYABLE_STATUSES:
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
            "brightdata_retry",
            attempt=retry_state.attempt_number,
            exc_type=type(retry_state.outcome.exception()).__name__,
        ),
    )


class BrightDataClient:
    BASE_URL = "https://api.brightdata.com"

    def __init__(self) -> None:
        cfg = get_config()
        self._api_key = cfg.brightdata.api_key
        self._dataset_id = cfg.brightdata.dataset_id
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _get_sync(self, endpoint: str, params: dict[str, Any] | None = None) -> dict:
        response = httpx.get(
            f"{self.BASE_URL}/{endpoint}",
            headers=self._headers(),
            params=params or {},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    @_create_retry_decorator()
    async def _get_async(
        self, client: httpx.AsyncClient, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict:
        async with self._semaphore:
            response = await client.get(
                f"{self.BASE_URL}/{endpoint}",
                headers=self._headers(),
                params=params or {},
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()

    def search_youtube(self, keyword: str, max_results: int = 20) -> list[dict]:
        data = self._get_sync(
            "scraper/youtube/search",
            {"keyword": keyword, "max_results": max_results},
        )
        return self._parse_search_results(data)

    def get_channel_details(self, channel_id: str) -> dict | None:
        try:
            data = self._get_sync(
                "scraper/youtube/channel",
                {"channel_id": channel_id},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return self._parse_channel(data)

    def get_channel_playlists(self, channel_id: str) -> list[dict]:
        data = self._get_sync(
            "scraper/youtube/playlists",
            {"channel_id": channel_id},
        )
        return self._parse_playlists(data)

    def get_video_comments(
        self, video_id: str, max_results: int = 50
    ) -> list[dict]:
        data = self._get_sync(
            "scraper/youtube/comments",
            {"video_id": video_id, "max_results": max_results},
        )
        return self._parse_comments(data)

    def get_video_details(self, video_id: str) -> dict | None:
        try:
            data = self._get_sync(
                "scraper/youtube/video",
                {"video_id": video_id},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return self._parse_video(data)

    def crawl_channel_relationships(
        self, channel_ids: list[str]
    ) -> list[dict]:
        edges: list[dict] = []
        for ch_id in channel_ids:
            edges.extend(self._crawl_one_channel(ch_id))
        return edges

    def _crawl_one_channel(self, channel_id: str) -> list[dict]:
        edges: list[dict] = []

        playlists = self.get_channel_playlists(channel_id)
        for pl in playlists:
            linked_channel_id = pl.get("channel_id", "")
            if linked_channel_id and linked_channel_id != channel_id:
                edges.append({
                    "source_channel_id": channel_id,
                    "target_channel_id": linked_channel_id,
                    "edge_type": "playlist",
                })

        try:
            videos = self.search_youtube(f"uploader:{channel_id}", max_results=5)
        except Exception:
            videos = []

        for vid in videos[:5]:
            vid_id = vid.get("video_id") or vid.get("id", "")
            if not vid_id:
                continue
            comments = self.get_video_comments(vid_id, max_results=20)
            for comment in comments:
                mentioned_ch = comment.get("mentioned_channel_id", "")
                if mentioned_ch and mentioned_ch != channel_id:
                    edges.append({
                        "source_channel_id": channel_id,
                        "target_channel_id": mentioned_ch,
                        "edge_type": comment.get("edge_type", "comment_mention"),
                    })

        return edges

    def _parse_search_results(self, data: dict) -> list[dict]:
        results: list[dict] = []
        items = data.get("items") or data.get("results") or []
        for item in items:
            snippet = item.get("snippet", {}) or item
            results.append({
                "video_id": item.get("id", {}).get("videoId", "") if isinstance(item.get("id"), dict) else item.get("id", ""),
                "channel_id": snippet.get("channelId", ""),
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),
                "thumbnails": snippet.get("thumbnails", {}),
            })
        return results

    def _parse_channel(self, data: dict) -> dict:
        snippet = data.get("snippet", {}) or data
        stats = data.get("statistics", {}) or data
        return {
            "channel_id": data.get("id", ""),
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "subscriber_count": int(str(stats.get("subscriberCount", 0))),
            "video_count": int(str(stats.get("videoCount", 0))),
            "view_count": int(str(stats.get("viewCount", 0))),
            "published_at": snippet.get("publishedAt", ""),
        }

    def _parse_video(self, data: dict) -> dict:
        snippet = data.get("snippet", {}) or data
        stats = data.get("statistics", {}) or data
        return {
            "video_id": data.get("id", ""),
            "channel_id": snippet.get("channelId", ""),
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "view_count": int(str(stats.get("viewCount", 0))),
            "like_count": int(str(stats.get("likeCount", 0))),
            "comment_count": int(str(stats.get("commentCount", 0))),
            "published_at": snippet.get("publishedAt", ""),
        }

    def _parse_playlists(self, data: dict) -> list[dict]:
        results: list[dict] = []
        items = data.get("items") or data.get("playlists") or []
        for item in items:
            snippet = item.get("snippet", {}) or item
            results.append({
                "playlist_id": item.get("id", ""),
                "title": snippet.get("title", ""),
                "channel_id": snippet.get("channelId", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "item_count": int(str(item.get("contentDetails", {}).get("itemCount", 0)) if isinstance(item.get("contentDetails"), dict) else 0),
            })
        return results

    def _parse_comments(self, data: dict) -> list[dict]:
        results: list[dict] = []
        items = data.get("items") or data.get("comments") or []
        for item in items:
            snippet = (
                item.get("snippet", {})
                or item.get("topLevelComment", {}).get("snippet", {})
                or item
            )
            text = snippet.get("textDisplay", "") or snippet.get("textOriginal", "") or ""
            mentioned_channel_id = ""
            mentioned_channel_title = ""
            if "@" in text:
                lines = text.split("\n")
                for line in lines:
                    if line.strip().startswith("@"):
                        handle = line.strip().split()[0].lstrip("@")
                        mentioned_channel_title = handle

            results.append({
                "comment_id": item.get("id", ""),
                "text": text,
                "author_channel_id": snippet.get("authorChannelId", {}).get("value", "") if isinstance(snippet.get("authorChannelId"), dict) else snippet.get("authorChannelId", ""),
                "mentioned_channel_id": mentioned_channel_id,
                "mentioned_channel_title": mentioned_channel_title,
                "edge_type": "comment_mention",
                "like_count": int(str(snippet.get("likeCount", 0))),
                "published_at": snippet.get("publishedAt", ""),
            })
        return results
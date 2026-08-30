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

# Shorts are analysed separately from long-form rather than exhaustively —
# a census of a channel's 4,000 Shorts answers no question either brief asks.
_SHORTS_SAMPLE_LIMIT = 50


def _is_quota_exceeded(response: httpx.Response) -> bool:
    """A 403 that specifically means the daily allowance is spent.

    Distinguished from other 403s (bad key, API not enabled, referrer
    restriction) because only this one is fixed by switching projects —
    the others would fail identically on every key.
    """
    try:
        body = response.json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    for err in body.get("error", {}).get("errors", []):
        if err.get("reason") == "quotaExceeded":
            return True
    return False


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


def _parse_duration_seconds(iso: str) -> int | None:
    """Parse ISO 8601 duration (PT1H2M3S) to total seconds.

    Returns None, not 0, when YouTube didn't report a fixed-length duration
    at all — livestreams and 24/7 rebroadcasts report contentDetails.duration
    as "P0D" (a date-only ISO 8601 duration, no "T" time component, so it
    never matches this pattern) rather than a real PT... value. Collapsing
    that into 0 read as "a zero-second video" on a 64-episode compilation or
    an ongoing livestream — a real value with no reasonable interpretation,
    not a missing one. None lets the caller leave the field genuinely blank.
    """
    import re

    if not iso:
        return None
    match = re.match(r"PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?", iso)
    if not match:
        return None
    return int(match.group("h") or 0) * 3600 + int(match.group("m") or 0) * 60 + int(match.group("s") or 0)


class YouTubeAPIClient:
    BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self) -> None:
        cfg = get_config()
        # Each key belongs to a different Google Cloud project, so each
        # carries its own 10,000-unit daily allowance. When one is spent the
        # client moves to the next rather than failing the run — an
        # exhausted key previously ended a run mid-flight, and on another
        # occasion produced 3,025 consecutive 403s before anyone noticed.
        self._api_keys = cfg.youtube.api_keys or [cfg.youtube.api_key]
        self._key_index = 0
        self._quota_used: int = 0

    @property
    def _api_key(self) -> str:
        return self._api_keys[self._key_index]

    def _rotate_key(self) -> bool:
        """Switch to the next unexhausted key. False when none remain.

        The instance quota counter resets: it tracks spend against ONE
        project's ceiling, and the next key starts with its own full
        allowance. Leaving it high would make check_quota refuse work the
        new key can perfectly well do.
        """
        if self._key_index + 1 >= len(self._api_keys):
            return False
        self._key_index += 1
        self._quota_used = 0
        logger.warning(
            "youtube_key_rotated",
            key_index=self._key_index, keys_available=len(self._api_keys),
        )
        return True

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
        while True:
            params["key"] = self._api_key
            response = httpx.get(
                f"{self.BASE_URL}/{endpoint}",
                params=params,
                timeout=30.0,
            )
            if response.status_code == 403 and _is_quota_exceeded(response):
                # Not retryable on this key — the allowance is gone until
                # midnight Pacific. Another project's key can serve it now.
                if self._rotate_key():
                    continue
                logger.error("youtube_all_keys_exhausted",
                             keys=len(self._api_keys))
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
        self, channel_id: str, max_results: int = 50, deep_scan: bool = True
    ) -> list[dict]:
        """The channel's long-form catalogue plus a recent Shorts sample.

        deep_scan=False asks for one page of uploads instead — roughly 3
        quota units rather than ~14. Callers use it for channels that cannot
        reach the deliverable anyway (below the subscriber floor), where the
        videos are only needed for outlier scoring and graph traversal.
        Scanning those deeply exhausted the 10,000-unit daily quota in about
        an hour on the live augment runs, since ~90% of discovered channels
        are sub-floor.

        This used to fetch a single page of the uploads playlist — the 50
        newest videos, Shorts and long-form mixed together. That shape could
        not satisfy either client brief: "Top 20 lifetime videos" ranked
        within the newest 50 means "best of the last few months" (for a
        2,761-video channel, its most recent 1.8%), and a "latest 50
        long-form" target was really ~33 once Shorts had eaten into the
        budget.

        Now it walks UULF for long-form and samples UUSH for Shorts, so the
        two are collected against separate budgets and arrive already
        classified by YouTube rather than by a duration guess.

        max_results bounds the long-form walk. The Shorts sample stays
        small and fixed — the briefs ask for Shorts kept apart from
        long-form analysis, not for a complete Shorts census.
        """
        if not deep_scan:
            return self._one_page_of_uploads(channel_id, max_results)
        videos = self.get_channel_long_form_scan(channel_id, max_scan=max_results)
        videos += self.get_channel_shorts_sample(
            channel_id, limit=min(_SHORTS_SAMPLE_LIMIT, max_results)
        )
        return videos

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
                    "part": "snippet,statistics,contentDetails",
                    "id": ",".join(batch),
                    "maxResults": 50,
                },
            )
            self._track_quota(1)
            for item in data.get("items", []):
                results.append(self._parse_video(item))
        return results

    def get_channel_first_video_published_at(
        self, channel_id: str, max_pages: int = 200
    ) -> str | None:
        """The channel's TRUE first-ever upload's publish date.

        get_channel_videos only ever fetches one page (<=50 items) of the
        uploads playlist, which the API returns newest-first with no
        "oldest first" sort available — so that one page is the channel's
        most recent uploads, not its earliest. For any channel with more
        than 50 videos ever, the "first video" derived from that one page is
        actually just the oldest video in a RECENT window, sometimes off by
        over a decade (verified live: a channel active since 2013 showed a
        "first video" from 2025, because it has hundreds of uploads and only
        the newest 50 were ever fetched).

        The only way to get the real answer is to paginate the uploads
        playlist all the way to its last page — playlistItems.list costs
        just 1 quota unit per call regardless of page size, so this is cheap
        even for a channel with a few thousand videos. max_pages is a safety
        ceiling (200 pages = up to 10,000 videos) so a pathological channel
        can't turn one channel's lookup into an unbounded call sequence.
        """
        if not self.check_quota(1):
            return None
        channels_data = self._get("channels", {"part": "contentDetails", "id": channel_id})
        self._track_quota(1)
        items = channels_data.get("items", [])
        if not items:
            return None
        uploads_playlist = (
            items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
        )
        if not uploads_playlist:
            return None

        oldest_published_at: str | None = None
        page_token: str | None = None
        for _ in range(max_pages):
            if not self.check_quota(1):
                logger.warning("quota_ceiling_hit_mid_pagination", channel_id=channel_id)
                break
            params = {"part": "snippet", "playlistId": uploads_playlist, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            try:
                page = self._get("playlistItems", params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 404):
                    logger.warning(
                        "youtube_uploads_playlist_unavailable",
                        channel_id=channel_id, status=exc.response.status_code,
                    )
                    break
                raise
            self._track_quota(1)
            page_items = page.get("items", [])
            if page_items:
                last_snippet = page_items[-1].get("snippet", {})
                published = last_snippet.get("publishedAt")
                if published:
                    oldest_published_at = published
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return oldest_published_at

    def get_channel_upload_breakdown(self, channel_id: str) -> dict[str, int | None]:
        """How many of the channel's uploads are long-form vs Shorts vs live.

        statistics.videoCount is a single lifetime total with no type
        breakdown, and the API exposes no other endpoint that splits it.
        YouTube does, however, auto-generate one playlist per upload type,
        addressed by swapping the uploads playlist's `UU` prefix:

            UU<suffix>    every upload      (== statistics.videoCount)
            UULF<suffix>  long-form only
            UUSH<suffix>  Shorts only
            UULV<suffix>  live stream VODs only

        playlistItems.list reports a playlist's size in
        pageInfo.totalResults for 1 quota unit, so the whole breakdown
        costs 4 units per channel — versus paginating every upload, which
        for an 11k-video channel would be ~460 units and blow the daily
        ceiling across a full run.

        Verified on real channels: UULF + UUSH + UULV == UU exactly. A 404
        means the channel has none of that type (never posted a Short,
        never went live), which is a real zero rather than an error.

        Returns keys total/long/shorts/live; a value is None only when the
        lookup genuinely failed, so callers can tell "no Shorts" (0) from
        "we don't know" (None) instead of persisting a wrong zero.
        """
        suffix = channel_id[2:] if channel_id.startswith("UC") else channel_id
        out: dict[str, int | None] = {}
        for key, prefix in (("total", "UU"), ("long", "UULF"),
                            ("shorts", "UUSH"), ("live", "UULV")):
            if not self.check_quota(1):
                logger.warning("quota_ceiling_hit_breakdown", channel_id=channel_id)
                out[key] = None
                continue
            try:
                page = self._get(
                    "playlistItems",
                    {"part": "id", "playlistId": prefix + suffix, "maxResults": 1},
                )
                self._track_quota(1)
                out[key] = page.get("pageInfo", {}).get("totalResults", 0)
            except httpx.HTTPStatusError as exc:
                self._track_quota(1)
                if exc.response.status_code == 404:
                    out[key] = 0
                else:
                    logger.warning(
                        "youtube_breakdown_playlist_failed",
                        channel_id=channel_id, playlist_type=key,
                        status=exc.response.status_code,
                    )
                    out[key] = None
        return out

    def get_channel_shorts_ids(
        self, channel_id: str, published_after: str | None = None, max_pages: int = 40
    ) -> set[str]:
        """Video IDs YouTube itself files under this channel's Shorts.

        The duration heuristic this replaces is wrong in both directions:
        YouTube's Shorts ceiling is 3 minutes, not 60 seconds (so a 61s
        vertical Short reads as long-form), and a brief LANDSCAPE upload is
        not a Short at all (so it reads as one). Membership in the UUSH
        playlist is YouTube's own answer to the question.

        UUSH is ordered newest-first, so `published_after` lets a caller
        that only sampled a channel's recent uploads stop paginating as
        soon as the playlist runs older than anything it holds.
        """
        suffix = channel_id[2:] if channel_id.startswith("UC") else channel_id
        playlist_id = "UUSH" + suffix
        ids: set[str] = set()
        page_token: str | None = None
        for _ in range(max_pages):
            if not self.check_quota(1):
                logger.warning("quota_ceiling_hit_shorts_ids", channel_id=channel_id)
                break
            params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            try:
                page = self._get("playlistItems", params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 404):
                    # No Shorts playlist at all — the channel has never
                    # posted one. An empty set is the correct answer.
                    break
                raise
            self._track_quota(1)
            passed_window = False
            for item in page.get("items", []):
                details = item.get("contentDetails", {})
                video_id = details.get("videoId")
                if video_id:
                    ids.add(video_id)
                published = details.get("videoPublishedAt")
                if published_after and published and published < published_after:
                    passed_window = True
            page_token = page.get("nextPageToken")
            if passed_window or not page_token:
                break
        return ids

    def _one_page_of_uploads(self, channel_id: str, max_results: int) -> list[dict]:
        """One page of the uploads playlist — the cheap path.

        What this method did for every channel before the lifetime scan
        existed: ~3 quota units, newest-first, Shorts and long-form mixed.
        Correct for channels whose videos only feed outlier scoring and
        graph traversal.
        """
        suffix = channel_id[2:] if channel_id.startswith("UC") else channel_id
        if not self.check_quota(1):
            logger.error("quota_ceiling_hit", quota_used=self._quota_used)
            return []
        try:
            page = self._get(
                "playlistItems",
                {"part": "contentDetails", "playlistId": "UU" + suffix,
                 "maxResults": min(max_results, 50)},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 404):
                logger.warning(
                    "youtube_uploads_playlist_unavailable",
                    channel_id=channel_id, status=exc.response.status_code,
                )
                return []
            raise
        self._track_quota(1)
        ids = [
            item.get("contentDetails", {}).get("videoId")
            for item in page.get("items", [])
        ]
        return self.get_videos([v for v in ids if v])

    def get_channel_shorts_sample(self, channel_id: str, limit: int = 50) -> list[dict]:
        """The channel's most recent Shorts, hydrated.

        Reads the UUSH auto-playlist, so membership is YouTube's own
        classification rather than a duration guess — the `<= 60s` rule this
        replaces was wrong in both directions (YouTube's ceiling is 3
        minutes, and a brief landscape upload is not a Short at all).

        Bounded rather than exhaustive: the briefs ask for Shorts to be
        analysed separately from long-form, not for a complete census of a
        channel that may hold thousands.
        """
        suffix = channel_id[2:] if channel_id.startswith("UC") else channel_id
        video_ids: list[str] = []
        page_token: str | None = None
        while len(video_ids) < limit:
            if not self.check_quota(1):
                logger.warning("quota_ceiling_hit_shorts_sample", channel_id=channel_id)
                break
            params = {"part": "contentDetails", "playlistId": "UUSH" + suffix,
                      "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            try:
                page = self._get("playlistItems", params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 404):
                    # No Shorts playlist: the channel has never posted one.
                    break
                raise
            self._track_quota(1)
            for item in page.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if vid:
                    video_ids.append(vid)
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return self.get_videos(video_ids[:limit])

    def get_channel_long_form_scan(
        self, channel_id: str, max_scan: int = 3000
    ) -> list[dict]:
        """Every long-form upload the channel has, newest first.

        Reads the UULF auto-playlist rather than UU, so Shorts and live VODs
        never consume the scan budget — a channel that is 60% Shorts costs
        60% less to walk, and the result needs no post-filtering.

        This exists because "top 20 lifetime videos" cannot be answered from
        a recent-50 window. Ranking the newest 50 uploads by performance
        yields "best of the last few months", which for a 2,761-video
        channel is its most recent 1.8% — a different question than the one
        asked, and the same trap first_video_published_at had to be fixed
        for.

        Cost is 1 quota unit per 50 videos to list them plus 1 per 50 to
        fetch their statistics. Median channel here holds ~312 long-form
        uploads (~14 units); max_scan bounds the tail so one 15k-video
        channel cannot consume a day's quota on its own. Truncation is
        logged rather than silent, because a truncated scan makes
        'top_lifetime' mean "top within the newest max_scan", and a caller
        that cannot tell the difference would overstate it.
        """
        suffix = channel_id[2:] if channel_id.startswith("UC") else channel_id
        # UULF is only auto-generated for channels that actually have
        # long-form uploads. A Shorts-only channel, or one with a handful of
        # videos, returns 404 — and treating that as "no videos" would hand
        # back an empty catalogue for a channel that plainly has uploads
        # (observed live: UU=41 while UULF 404s). Falling back to the full
        # uploads playlist keeps those channels, and costs nothing in
        # correctness: is_short is decided by UUSH membership downstream and
        # _select_sample splits on it, so Shorts arriving here are labelled
        # and separated exactly as they would have been.
        playlist_id = "UULF" + suffix
        video_ids: list[str] = []
        page_token: str | None = None
        truncated = False
        used_fallback = False
        while len(video_ids) < max_scan:
            if not self.check_quota(1):
                logger.warning("quota_ceiling_hit_lifetime_scan", channel_id=channel_id)
                truncated = True
                break
            params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            try:
                page = self._get("playlistItems", params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 404):
                    if not used_fallback and playlist_id.startswith("UULF"):
                        logger.info(
                            "youtube_longform_playlist_absent_using_uploads",
                            channel_id=channel_id, status=exc.response.status_code,
                        )
                        playlist_id = "UU" + suffix
                        used_fallback = True
                        page_token = None
                        continue
                    logger.warning(
                        "youtube_uploads_playlist_unavailable",
                        channel_id=channel_id, status=exc.response.status_code,
                    )
                    break
                raise
            self._track_quota(1)
            for item in page.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if vid:
                    video_ids.append(vid)
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        else:
            truncated = page_token is not None
        if truncated:
            logger.info(
                "lifetime_scan_truncated",
                channel_id=channel_id, scanned=len(video_ids), max_scan=max_scan,
            )
        return self.get_videos(video_ids[:max_scan])

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
            # v3: geo/language enrichment fields from YouTube's self-report
            "country": snippet.get("country", ""),
            "default_language": snippet.get("defaultLanguage", ""),
            "default_audio_language": snippet.get("defaultAudioLanguage", ""),
        }

    def _parse_video(self, item: dict) -> dict:
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        content = item.get("contentDetails", {})
        duration_str = content.get("duration", "PT0S")
        return {
            "video_id": item.get("id", ""),
            "channel_id": snippet.get("channelId", ""),
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "view_count": int(stats.get("viewCount", 0)),
            "like_count": int(stats.get("likeCount", 0)),
            "comment_count": int(stats.get("commentCount", 0)),
            "published_at": snippet.get("publishedAt", ""),
            "tags": snippet.get("tags", []),
            "default_language": snippet.get("defaultLanguage", ""),
            "default_audio_language": snippet.get("defaultAudioLanguage", ""),
            "duration_seconds": _parse_duration_seconds(duration_str),
            # score_thumbnail_signals reads this back out of videos.extra —
            # it was never extracted here at all, so thumbnail vision
            # scoring had nothing to work with regardless of how the
            # persistence layer stored it.
            "thumbnails": snippet.get("thumbnails", {}),
        }
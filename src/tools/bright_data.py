"""Bright Data Datasets API client — trigger, poll, collect.

This replaces an earlier client written against endpoints that do not exist
(`scraper/youtube/search`, `/channel`, `/playlists`, ...) returning
YouTube-Data-API-v3-shaped JSON. The real surface, verified live 2026-08-13:

    POST /datasets/v3/trigger?dataset_id=<id>[&type=discover_new
         &discover_by=keyword][&limit_per_input=N]      -> {"snapshot_id": ...}
    GET  /datasets/v3/progress/<snapshot_id>            -> {"status": "running"|"ready"}
    GET  /datasets/v3/snapshot/<snapshot_id>?format=json-> [ {...}, ... ]

Three separate collectors (Channels / Videos posts / Comments), each billed per
record at $0.0015. Every public method therefore returns
`(parsed_rows, records_consumed)` — cost accounting is not optional here,
because the budget circuit breaker in check_saturation is only as honest as
the numbers it is given.

Cost controls, all measured rather than assumed:
  * `limit_per_input` caps discovery. Without it a single keyword returned 469
    records; with `limit_per_input=3` it returned exactly 3.
  * `num_of_comments` caps the Comments collector. `limit` and `max_results`
    are rejected by the API as unknown fields.
  * `featured_channels` on a Channels record carries the channel->channel edges
    for free, so graph expansion costs 1 record/channel. Comment mining is an
    escalation path, not the default. `Links` is NOT an edge source — it holds
    external websites (pwlcapital.com, x.com/...), not channels.

Timing: by-URL collection returns in ~5s; keyword discovery runs for MINUTES
(a Channels discovery took ~6min, a Videos discovery was still running at 12).
Hence async polling with a hard ceiling, and concurrent fan-out.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Iterable, Literal

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.config import get_config
from src.observability.logging_config import record_spend_intent
from src.tools import deadline as run_deadline

logger = structlog.get_logger(__name__)

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

CollectorName = Literal["channels", "videos", "comments"]


class BrightDataError(RuntimeError):
    """Non-retryable failure from the Datasets API (validation, auth, timeout)."""


class SnapshotNotReady(BrightDataError):
    """/snapshot says the job is still building, whatever /progress said.

    The two endpoints disagree, and the one holding the rows is the one to
    believe. Raised so _collect can go back to waiting instead of throwing
    away a snapshot that has already been paid for -- a crime run on
    2026-09-07 lost a whole discovery collection to a single fetch that
    arrived a few seconds early, on a response whose own message read
    "Snapshot is building, try again in 30s".
    """


def _is_retryable(exception: BaseException) -> bool:
    if isinstance(exception, httpx.HTTPStatusError):
        return exception.response.status_code in RETRYABLE_STATUSES
    if isinstance(exception, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    return False


def _is_retryable_trigger(exception: BaseException) -> bool:
    """Only 429 may be retried on the trigger call.

    `trigger` is the one request in this client that spends money, and the API
    offers no idempotency key. A timeout or a 5xx means the *response* was lost
    — the job may well have been created and be billing right now — so
    re-issuing buys a second snapshot and a second full charge, up to five
    times. A 429 is different: the server explicitly refused the request, so no
    job exists and retrying is free.

    Polling and fetching are read-only and keep the general retry policy.
    """
    if isinstance(exception, httpx.HTTPStatusError):
        return exception.response.status_code == 429
    return False


def _create_retry_decorator(predicate=_is_retryable):
    return retry(
        wait=wait_exponential_jitter(initial=1, max=60, jitter=2),
        stop=stop_after_attempt(5),
        retry=retry_if_exception(predicate),
        before_sleep=lambda retry_state: logger.warning(
            "brightdata_retry",
            attempt=retry_state.attempt_number,
            exc_type=type(retry_state.outcome.exception()).__name__,
        ),
    )


# ---------------------------------------------------------------------------
# Reference normalisation
# ---------------------------------------------------------------------------

_UC_ID = re.compile(r"^UC[A-Za-z0-9_-]{20,}$")


def normalize_channel_ref(ref: str) -> str:
    """Turn a channel handle / UC id / URL into a URL the collector accepts.

    Traversal is keyed on *refs* rather than UC ids because that is what the
    data gives us: `featured_channels` entries carry `url` and `handle` but no
    UC id, and the taxonomy LLM emits `@handle` seeds. The UC id only becomes
    known once the channel record itself is fetched — which is exactly the call
    that expands it, so nothing extra is spent resolving them.

    The result is canonical, because `expanded_channel_refs` is the only thing
    standing between the walk and paying twice for the same channel. Without
    canonicalisation `.../@Handle`, `.../@handle/videos` and `.../@handle?x=1`
    are three distinct refs for one channel, and each one re-bills.
    """
    ref = (ref or "").strip()
    if not ref:
        return ""

    if ref.startswith("http://") or ref.startswith("https://"):
        # Strip scheme/host variation, query and fragment, then re-derive.
        without_scheme = ref.split("://", 1)[1]
        path = without_scheme.split("/", 1)[1] if "/" in without_scheme else ""
        path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
        if not path:
            return ""
        segments = path.split("/")
        if segments[0] == "channel" and len(segments) > 1:
            return f"https://www.youtube.com/channel/{segments[1]}"
        # /@handle/videos, /c/name, /user/name -> keep the identifying segment
        head = segments[0]
        if head in ("c", "user") and len(segments) > 1:
            head = segments[1]
        return f"https://www.youtube.com/@{head.lstrip('@').lower()}"

    if _UC_ID.match(ref):
        return f"https://www.youtube.com/channel/{ref}"
    return f"https://www.youtube.com/@{ref.lstrip('@').lower()}"


def normalize_video_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if not ref:
        return ""
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref
    return f"https://www.youtube.com/watch?v={ref}"


def _to_int(value: Any) -> int:
    """Bright Data returns counts as ints, numeric strings, or None."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else 0


# ---------------------------------------------------------------------------
# Parsers — field names taken from live records, not from the vendor's docs
# ---------------------------------------------------------------------------

def parse_channel(row: dict) -> dict:
    """Channels collector record -> our channel shape.

    `featured_channels` is the channel->channel edge source and rides along on
    this record at no extra cost. `Links` is deliberately ignored: inspected
    live, it contains external sites, not channels.
    """
    featured = row.get("featured_channels") or []
    refs: list[str] = []
    edges: list[dict] = []
    for entry in featured:
        if not isinstance(entry, dict):
            continue
        target = entry.get("url") or entry.get("handle") or ""
        if not target:
            continue
        ref = normalize_channel_ref(str(target))
        refs.append(ref)
        # The subscriber count of the *target* comes along inside the edge, so
        # the frontier can be ranked and filtered before spending a record on
        # any of them. Measured: featured_channels is present on 1% of
        # sub-100-subscriber channels but 18% of 100k+ ones, so expanding small
        # channels costs records and returns no edges.
        edges.append(
            {
                "ref": ref,
                "name": str(entry.get("name") or ""),
                "subscriber_count": _to_int(entry.get("subscribers")),
            }
        )

    return {
        "channel_id": str(row.get("id") or ""),
        "channel_ref": str(row.get("url") or ""),
        "handle": str(row.get("handle") or ""),
        "title": str(row.get("name") or ""),
        "description": str(row.get("Description") or ""),
        "subscriber_count": _to_int(row.get("subscribers")),
        "video_count": _to_int(row.get("videos_count")),
        "view_count": _to_int(row.get("views")),
        "published_at": str(row.get("created_date") or ""),
        "featured_channel_refs": refs,
        "featured_channel_edges": edges,
        "discovery_input": row.get("discovery_input") or {},
    }


def parse_video(row: dict) -> dict:
    """Videos posts collector record -> our video shape.

    `next_recommended_videos` is a secondary (Tier B) edge source; entries are
    kept raw and mined by the caller, since only some carry a channel ref.

    Deliberately dropped: `transcript`, `formatted_transcript`, `chapters`,
    `preview_image`, `codecs`, `quality*`, `viewport_frames`, `audio_tracks`
    and the other media fields the collector returns. This harness does no
    image or video processing — v1 reasons over engagement metrics and graph
    structure only. Carrying media payloads would bloat state and the compaction
    prompts for data nothing consumes. Transcript/scene analysis stays a v2
    concern (master plan §8.4 `analysis_results`), and the `thumbnail_vision`
    tier in cascade.py is unused for the same reason.
    """
    recommended = row.get("next_recommended_videos") or []
    return {
        "video_id": str(row.get("video_id") or ""),
        "channel_id": str(row.get("youtuber_id") or row.get("channel_id") or ""),
        "channel_ref": str(row.get("channel_url") or row.get("youtuber") or ""),
        "title": str(row.get("title") or ""),
        "description": str(row.get("description") or ""),
        "view_count": _to_int(row.get("views")),
        "like_count": _to_int(row.get("likes")),
        "comment_count": _to_int(row.get("num_comments")),
        "published_at": str(row.get("date_posted") or ""),
        "next_recommended": recommended if isinstance(recommended, list) else [],
    }


def parse_comment(row: dict) -> dict:
    """Comments collector record -> our comment shape.

    `user_channel` is the commenter's own channel and `user_id` its UC id —
    the actual edge, structured. The older client tried to regex `@mentions`
    out of comment text; that is guesswork next to fields that are simply
    present.

    Timestamps: `date` is relative ("10 minutes ago") and useless downstream;
    `date_iso` is the real ISO-8601 value.
    """
    return {
        "comment_id": str(row.get("comment_id") or ""),
        "video_id": str(row.get("video_id") or ""),
        "text": str(row.get("comment_text") or ""),
        "author_name": str(row.get("username") or ""),
        "author_channel_ref": str(row.get("user_channel") or ""),
        "author_channel_id": str(row.get("user_id") or ""),
        "like_count": _to_int(row.get("likes")),
        "reply_count": _to_int(row.get("replies")),
        "published_at": str(row.get("date_iso") or ""),
    }


_PARSERS = {
    "channels": parse_channel,
    "videos": parse_video,
    "comments": parse_comment,
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class BrightDataClient:
    BASE_URL = "https://api.brightdata.com/datasets/v3"

    def __init__(self, run_id: str = "") -> None:
        self._run_id = run_id
        cfg = get_config()
        self._cfg = cfg.brightdata
        self._api_key = cfg.brightdata.api_key
        self._mode = cfg.brightdata.mode
        self._semaphore = asyncio.Semaphore(cfg.harness.brightdata_max_concurrency)
        self._dataset_ids: dict[str, str] = {
            "channels": cfg.brightdata.channels_dataset_id,
            "videos": cfg.brightdata.videos_dataset_id,
            "comments": cfg.brightdata.comments_dataset_id,
        }

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    @_create_retry_decorator(_is_retryable_trigger)
    async def _trigger(
        self,
        client: httpx.AsyncClient,
        dataset_id: str,
        payload: list[dict],
        params: dict[str, Any],
    ) -> str:
        response = await client.post(
            f"{self.BASE_URL}/trigger",
            headers=self._headers(),
            params={"dataset_id": dataset_id, **params},
            json=payload,
            timeout=60.0,
        )
        if response.status_code >= 400 and response.status_code not in RETRYABLE_STATUSES:
            # Validation errors are informative and permanent — surface the
            # body rather than burning five retries on an input the API will
            # never accept.
            raise BrightDataError(
                f"trigger rejected ({response.status_code}): {response.text[:400]}"
            )
        response.raise_for_status()
        data = response.json()
        snapshot_id = data.get("snapshot_id") if isinstance(data, dict) else None
        if not snapshot_id:
            raise BrightDataError(f"trigger returned no snapshot_id: {data}")
        return str(snapshot_id)

    @_create_retry_decorator()
    async def _progress(self, client: httpx.AsyncClient, snapshot_id: str) -> str:
        response = await client.get(
            f"{self.BASE_URL}/progress/{snapshot_id}",
            headers=self._headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("status", "")) if isinstance(data, dict) else ""

    @_create_retry_decorator()
    async def _fetch(self, client: httpx.AsyncClient, snapshot_id: str) -> list[dict]:
        response = await client.get(
            f"{self.BASE_URL}/snapshot/{snapshot_id}",
            headers=self._headers(),
            params={"format": "json"},
            timeout=180.0,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        # A dict here means the snapshot is not actually finished, however
        # /progress answered. Its own message says "try again in 30s", so
        # the caller waits rather than discarding the job -- see _collect.
        raise SnapshotNotReady(f"snapshot not ready: {str(data)[:200]}")

    async def _collect(
        self,
        collector: CollectorName,
        payload: list[dict],
        params: dict[str, Any] | None = None,
        worst_case_records: int = 0,
    ) -> list[dict]:
        """Trigger a job, poll it to completion, return its raw rows."""
        if not payload:
            return []
        if self._mode == "replay":
            return self._replay(collector)

        dataset_id = self._dataset_ids[collector]
        # Do not commit money to a snapshot the run has no time left to use:
        # the POST below is the moment it starts billing, and a job triggered
        # past the deadline is paid for and then discarded.
        if run_deadline.research_passed():
            raise BrightDataError(
                f"{collector} trigger skipped: the run's "
                f"{run_deadline.deadline_seconds()}s deadline has passed"
            )
        deadline = time.monotonic() + self._cfg.poll_max_seconds

        # Written BEFORE the POST. The trigger is the moment money is
        # committed, and a lost response leaves a job billing with nothing
        # anywhere recording it — the snapshot id is the only handle for
        # reconciling a charge back to the job that caused it.
        record_spend_intent(
            self._run_id,
            collector=collector,
            inputs=len(payload),
            worst_case_records=worst_case_records or len(payload),
            params=params or {},
            phase="trigger",
        )

        async with self._semaphore:
            async with httpx.AsyncClient() as client:
                snapshot_id = await self._trigger(
                    client, dataset_id, payload, params or {}
                )
                record_spend_intent(
                    self._run_id,
                    collector=collector,
                    inputs=len(payload),
                    worst_case_records=worst_case_records or len(payload),
                    params=params or {},
                    snapshot_id=snapshot_id,
                    phase="triggered",
                )
                logger.info(
                    "brightdata_triggered",
                    collector=collector,
                    snapshot_id=snapshot_id,
                    inputs=len(payload),
                    params=params or {},
                )

                rows: list[dict] | None = None
                while True:
                    status = await self._progress(client, snapshot_id)
                    if status == "ready":
                        try:
                            rows = await self._fetch(client, snapshot_id)
                            break
                        except SnapshotNotReady as exc:
                            # Believe /snapshot over /progress and keep
                            # waiting; the job is billed either way, and
                            # the only thing giving up saves is the wait.
                            status = "building"
                            logger.info(
                                "brightdata_snapshot_not_ready_yet",
                                collector=collector,
                                snapshot_id=snapshot_id,
                                detail=str(exc)[:120],
                            )
                    if status in ("failed", "canceled"):
                        raise BrightDataError(
                            f"snapshot {snapshot_id} ended with status={status}"
                        )
                    if time.monotonic() > deadline:
                        raise BrightDataError(
                            f"snapshot {snapshot_id} still {status!r} after "
                            f"{self._cfg.poll_max_seconds}s"
                        )
                    # The run's own wall-clock ceiling, checked every poll.
                    # A snapshot can legitimately take minutes; without this
                    # the run overshoots its stated duration by however long
                    # the collector happens to take, which is exactly what
                    # made the console's durations unreliable.
                    if run_deadline.research_passed():
                        raise BrightDataError(
                            f"snapshot {snapshot_id} abandoned at the run's "
                            f"{run_deadline.deadline_seconds()}s deadline "
                            f"(status={status!r})"
                        )
                    await asyncio.sleep(self._cfg.poll_interval_seconds)

                assert rows is not None  # the loop only breaks with rows

        record_spend_intent(
            self._run_id,
            collector=collector,
            inputs=len(payload),
            worst_case_records=len(rows),
            params=params or {},
            snapshot_id=snapshot_id,
            phase="collected",
        )
        logger.info(
            "brightdata_collected",
            collector=collector,
            snapshot_id=snapshot_id,
            records=len(rows),
        )
        return rows

    def _replay(self, collector: CollectorName) -> list[dict]:
        """Serve a recorded fixture instead of calling the API.

        Missing fixtures return empty rather than raising — a replay run should
        exercise the graph's empty-result paths too, not crash on them.
        """
        path = Path(self._cfg.fixtures_dir) / f"{collector}.json"
        if not path.exists():
            logger.warning("brightdata_replay_missing", collector=collector, path=str(path))
            return []
        with path.open() as handle:
            data = json.load(handle)
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    # -- public API --------------------------------------------------------

    async def discover_channels_by_keyword(
        self, keywords: Iterable[str], limit_per_input: int
    ) -> tuple[list[dict], int]:
        """Keyword -> channels, via the collector's discovery mode.

        `limit_per_input` is mandatory in practice: uncapped, one keyword
        returned 469 records ($0.70, 9% of the monthly free tier).
        """
        payload = [{"keyword": kw} for kw in keywords if kw and kw.strip()]
        if not payload:
            return [], 0
        params: dict[str, Any] = {"type": "discover_new", "discover_by": "keyword"}
        if limit_per_input > 0:
            params["limit_per_input"] = limit_per_input
        rows = await self._collect(
            "channels", payload, params,
            worst_case_records=len(payload) * max(1, limit_per_input),
        )
        return [parse_channel(row) for row in rows], len(rows)

    async def get_channels(self, refs: Iterable[str]) -> tuple[list[dict], int]:
        """Fetch channel records by handle / UC id / URL.

        This is the Tier A graph-walk call: one record per channel, and the
        `featured_channels` edges come back inside it for free.
        """
        urls = _dedupe(normalize_channel_ref(r) for r in refs)
        if not urls:
            return [], 0
        rows = await self._collect("channels", [{"url": u} for u in urls])
        return [parse_channel(row) for row in rows], len(rows)

    async def get_channel_videos(
        self, refs: Iterable[str], limit_per_input: int
    ) -> tuple[list[dict], int]:
        """Tier B: recent videos for channels, capped per channel."""
        urls = _dedupe(normalize_channel_ref(r) for r in refs)
        if not urls:
            return [], 0
        params: dict[str, Any] = {"type": "discover_new", "discover_by": "url"}
        if limit_per_input > 0:
            params["limit_per_input"] = limit_per_input
        rows = await self._collect("videos", [{"url": u} for u in urls], params)
        return [parse_video(row) for row in rows], len(rows)

    async def get_videos(self, refs: Iterable[str]) -> tuple[list[dict], int]:
        urls = _dedupe(normalize_video_ref(r) for r in refs)
        if not urls:
            return [], 0
        rows = await self._collect("videos", [{"url": u} for u in urls])
        return [parse_video(row) for row in rows], len(rows)

    async def get_comments(
        self, refs: Iterable[str], num_of_comments: int
    ) -> tuple[list[dict], int]:
        """Tier B: comments per video, capped by `num_of_comments`.

        That field name is not a guess — `limit` and `max_results` are rejected
        by the API as unknown fields, and `num_of_comments=10` was measured
        returning exactly 10 records against videos with hundreds.
        """
        urls = _dedupe(normalize_video_ref(r) for r in refs)
        if not urls:
            return [], 0
        payload: list[dict] = []
        for url in urls:
            item: dict[str, Any] = {"url": url}
            if num_of_comments > 0:
                item["num_of_comments"] = num_of_comments
            payload.append(item)
        rows = await self._collect(
            "comments", payload,
            worst_case_records=len(payload) * max(1, num_of_comments),
        )
        return [parse_comment(row) for row in rows], len(rows)


def _dedupe(values: Iterable[str]) -> list[str]:
    """Order-preserving dedupe.

    Bright Data bills per record, and the API offers no request idempotency
    key — so sending the same URL twice in one payload is charged twice. This
    is the one place that can be prevented cheaply.
    """
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out

"""hydrate_metadata — L2 metadata hydration node.

Batches channel IDs into groups of 50 for the YouTube Data API v3
(1 quota unit per batch), fetches videos per channel, computes outlier
scores, and persists to Postgres via upserts.

Hydrates only the round's delta (channels not yet hydrated) to avoid
re-burning quota on already-hydrated channels. Persistence failures are
recorded in state["errors"], never silently swallowed.

Track A — deterministic, no LLM.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.tools.youtube_api import YouTubeAPIClient
from src.tools.outlier_score import score_channel_videos
from src.tools.dedup import persist_channel, persist_video
from src.state import NodeLog, ErrorRecord


def hydrate_metadata(state: dict) -> dict:
    channel_ids = state.get("discovered_channel_ids", [])
    thread_id = state.get("thread_id", "")
    hydrated = set(state.get("hydrated_channel_ids", set()))

    to_hydrate = [c for c in channel_ids if c not in hydrated]
    if not to_hydrate:
        return {
            "next_action": "continue",
            "node_logs": [
                NodeLog(
                    node_name="hydrate_metadata",
                    thread_id=thread_id,
                    input_summary={"reason": "no new channels to hydrate"},
                ).model_dump()
            ],
        }

    client = YouTubeAPIClient()
    channels = client.get_channels(to_hydrate)

    all_video_ids: list[str] = []
    for ch in channels:
        ch.setdefault("discovery_method", "keyword_and_graph_walk")
        ch["first_seen_at"] = ch.get("first_seen_at") or datetime.now(timezone.utc).isoformat()
        videos = client.get_channel_videos(ch["channel_id"], max_results=50)
        scored = score_channel_videos(videos)
        ch["_videos"] = scored
        all_video_ids.extend(v["video_id"] for v in scored)

    errors: list[dict] = []
    try:
        from src.db.connection import get_connection, put_connection

        conn = get_connection()
        try:
            for ch in channels:
                persist_channel(conn, ch)
                for vid in ch.get("_videos", []):
                    persist_video(conn, vid)
        finally:
            put_connection(conn)
    except Exception as exc:
        errors.append(
            ErrorRecord(
                node_name="hydrate_metadata",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=False,
            ).model_dump()
        )

    newly_hydrated = set(ch["channel_id"] for ch in channels)

    node_log = NodeLog(
        node_name="hydrate_metadata",
        thread_id=thread_id,
        input_summary={
            "channels_hydrated": len(channels),
            "videos_fetched": len(all_video_ids),
            "quota_used": client.get_quota_used(),
            "hydration_errors": len(errors),
        },
        latency_ms=None,
        cost_usd=0.0,
    )

    return {
        "discovered_video_ids": all_video_ids,
        "hydrated_channel_ids": newly_hydrated,
        "node_logs": [node_log.model_dump()],
        "errors": errors,
        "next_action": "continue",
    }

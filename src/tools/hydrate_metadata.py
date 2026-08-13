"""hydrate_metadata — L2 metadata hydration node.

Batches channel IDs into groups of 50 for the YouTube Data API v3
(1 quota unit per batch), fetches videos per channel, computes outlier
scores, and persists to Postgres via upserts.

Track A — deterministic, no LLM.
"""

from __future__ import annotations

from src.tools.youtube_api import YouTubeAPIClient
from src.tools.outlier_score import score_channel_videos
from src.tools.dedup import persist_channel, persist_video
from src.state import NodeLog


def hydrate_metadata(state: dict) -> dict:
    channel_ids = state.get("discovered_channel_ids", [])
    thread_id = state.get("thread_id", "")
    run_id = state.get("run_id", "")
    if not channel_ids:
        return {
            "next_action": "continue",
            "node_logs": [
                NodeLog(
                    node_name="hydrate_metadata",
                    thread_id=thread_id,
                    input_summary={"reason": "no channels to hydrate"},
                ).model_dump()
            ],
        }

    client = YouTubeAPIClient()
    channels = client.get_channels(channel_ids)

    all_video_ids: list[str] = []
    for ch in channels:
        ch.setdefault("discovery_method", "keyword_and_graph_walk")
        ch.setdefault("first_seen_at", "")
        videos = client.get_channel_videos(ch["channel_id"], max_results=50)
        scored = score_channel_videos(videos)
        ch["_videos"] = scored
        all_video_ids.extend(v["video_id"] for v in scored)

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
    except Exception:
        pass

    node_log = NodeLog(
        node_name="hydrate_metadata",
        thread_id=thread_id,
        input_summary={
            "channels_hydrated": len(channels),
            "videos_fetched": len(all_video_ids),
            "quota_used": client.get_quota_used(),
        },
        latency_ms=None,
        cost_usd=0.0,
    )

    return {
        "discovered_video_ids": all_video_ids,
        "node_logs": [node_log.model_dump()],
        "next_action": "continue",
    }
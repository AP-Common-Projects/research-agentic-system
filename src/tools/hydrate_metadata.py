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
from src.tools.dedup import persist_channel, persist_video, persist_channel_v3, persist_channel_snapshot, persist_video_v3
from src.state import NodeLog, ErrorRecord


def _tag_video_samples(conn, videos: list[dict]) -> None:
    """v4: tag latest 50 by date as 'latest', top 20 by outlier as 'top_lifetime'.

    Plan §12 Crime requirement #5: standardize video sampling.
    'latest' is set first, then 'top_lifetime' overrides for videos in both sets
    (a video can be both a recent publish and a top outlier)."""
    if not videos:
        return
    sorted_by_date = sorted(videos, key=lambda v: v.get("published_at") or "", reverse=True)
    sorted_by_outlier = sorted(
        [v for v in videos if (v.get("outlier_score") or 0) > 0],
        key=lambda v: v.get("outlier_score") or 0, reverse=True,
    )
    cur = conn.cursor()
    try:
        for vid in sorted_by_date[:50]:
            vid_id = vid.get("video_id")
            if not vid_id:
                continue
            try:
                cur.execute(
                    "UPDATE videos SET sample_reason = COALESCE(sample_reason, 'latest') WHERE video_id = %s",
                    (vid_id,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
        for vid in sorted_by_outlier[:20]:
            vid_id = vid.get("video_id")
            if not vid_id:
                continue
            try:
                cur.execute(
                    "UPDATE videos SET sample_reason = 'top_lifetime' WHERE video_id = %s "
                    "AND (sample_reason IS NULL OR sample_reason = 'latest')",
                    (vid_id,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
    finally:
        cur.close()


def _attribute(channel_id: str, kw_found: set[str], gw_found: set[str]) -> str:
    """Which discovery track(s) found this channel.

    Returns "graph_walk" only when the keyword track never returned it —
    that exclusivity is the whole claim being tested. "unattributed" covers
    channels carried over from pre-v4 checkpoints, which cannot be
    retroactively assigned to a track.
    """
    in_kw = channel_id in kw_found
    in_gw = channel_id in gw_found
    if in_kw and in_gw:
        return "both"
    if in_gw:
        return "graph_walk"
    if in_kw:
        return "keyword"
    return "unattributed"


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

    # A fresh client each round means its internal quota counter starts at zero
    # every time, so seed it from the run-level total carried in state —
    # otherwise the 90%-of-ceiling guard only ever sees one round's usage.
    client = YouTubeAPIClient()
    quota_baseline = state.get("youtube_quota_used", 0)
    client.seed_quota_used(quota_baseline)
    channels = client.get_channels(to_hydrate)

    # Per-track attribution, not a flat label. "graph_walk" here means the
    # keyword track never returned this channel — those are exactly the
    # channels the project exists to find (master plan §1), so the label has
    # to be earned, never assumed.
    kw_found = set(state.get("keyword_channel_ids", set()))
    gw_found = set(state.get("graph_walk_channel_ids", set()))

    all_video_ids: list[str] = []
    errors: list[dict] = []
    for ch in channels:
        ch["discovery_method"] = _attribute(ch["channel_id"], kw_found, gw_found)
        # v4 competitor_ecosystem: channels resolved from competitor-benchmark
        # seeds get a distinct label (plan §10 item 3)
        active_node = state.get("tree", {}).get(state.get("active_node_id") or "", {})
        competitor_seeds = set(active_node.get("_competitor_seeds") or [])
        if ch["channel_id"] in gw_found and ch["channel_id"] in competitor_seeds:
            ch["discovery_method"] = "competitor_ecosystem"
        ch["first_seen_at"] = ch.get("first_seen_at") or datetime.now(timezone.utc).isoformat()
        # Per channel, not per round. This node is the fan-in join and is not
        # wrapped by graph.py's _guarded, so anything escaping here ends the
        # run — and by this point the round's Bright Data records are already
        # paid for. One channel's metadata is worth losing; a round is not.
        try:
            videos = client.get_channel_videos(ch["channel_id"], max_results=50)
        except Exception as exc:
            errors.append(
                ErrorRecord(
                    node_name="hydrate_metadata",
                    error_type=type(exc).__name__,
                    message=f"video hydration failed for {ch['channel_id']}: {exc}",
                    recoverable=True,
                ).model_dump()
            )
            videos = []
        scored = score_channel_videos(videos)
        ch["_videos"] = scored
        all_video_ids.extend(v["video_id"] for v in scored)

    try:
        from src.db.connection import get_connection, put_connection

        conn = get_connection()
        try:
            import json as _json

            for ch in channels:
                persist_channel(conn, ch)
                for vid in ch.get("_videos", []):
                    if vid.get("thumbnails"):
                        # persist_video only writes an explicit "extra" key
                        # — the raw "thumbnails" dict from youtube_api.py
                        # otherwise never reaches the DB at all, which is
                        # what score_thumbnail_signals reads back out via
                        # extra->'thumbnails'.
                        vid = dict(vid)
                        vid["extra"] = _json.dumps({"thumbnails": vid["thumbnails"]})
                    persist_video(conn, vid)
                # v3: snapshot + enrichment provenance
                run_id = state.get("run_id", "")
                persist_channel_snapshot(
                    conn, ch["channel_id"], run_id,
                    ch.get("subscriber_count", 0),
                    ch.get("view_count", 0),
                    ch.get("video_count", 0),
                )
                v3_fields = {
                    "first_discovered_run_id": run_id,
                    "last_enriched_run_id": run_id,
                }
                if ch.get("country"):
                    v3_fields["country_code"] = ch["country"]
                    v3_fields["country_source"] = "self_reported"
                if ch.get("default_language"):
                    v3_fields["primary_language_code"] = ch["default_language"]
                persist_channel_v3(conn, ch["channel_id"], run_id, v3_fields)
                for vid in ch.get("_videos", []):
                    v3_vid = {}
                    if vid.get("description"):
                        v3_vid["description"] = vid["description"]
                    if vid.get("tags"):
                        v3_vid["tags"] = vid["tags"]
                    if vid.get("duration_seconds") is not None:
                        # _parse_duration_seconds returns None (not 0) when
                        # YouTube reported no fixed-length duration at all —
                        # livestreams and 24/7 rebroadcasts send "P0D"
                        # rather than a real PT... value. A live/rebroadcast
                        # video genuinely has no duration to record here;
                        # leaving both fields unset keeps them blank in the
                        # export instead of showing a misleading "0 seconds"
                        # on a multi-hour or ongoing stream.
                        dur = vid["duration_seconds"]
                        v3_vid["duration_seconds"] = dur
                        v3_vid["is_short"] = 0 < dur <= 60
                    if vid.get("default_language"):
                        v3_vid["language_code"] = vid["default_language"]
                    if v3_vid:
                        persist_video_v3(conn, vid["video_id"], v3_vid)
                # v4 sample_reason: latest 50 by date, top 20 lifetime by outlier
                _tag_video_samples(conn, ch.get("_videos", []))

            # Membership, so this run's slice can be exported later without
            # run_id columns on the shared entity tables. This node is the only
            # place that knows the run, the active branch and the entities at
            # the same moment.
            from src.tools.dedup import persist_category_tags

            persist_category_tags(
                conn,
                run_id=state.get("run_id", ""),
                tree_node_id=state.get("active_node_id") or "",
                channel_ids=[ch["channel_id"] for ch in channels],
                video_ids=all_video_ids,
            )
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
    quota_spent = client.quota_consumed_this_call(quota_baseline)

    node_log = NodeLog(
        node_name="hydrate_metadata",
        thread_id=thread_id,
        input_summary={
            "channels_hydrated": len(channels),
            "videos_fetched": len(all_video_ids),
            "quota_spent_this_round": quota_spent,
            "quota_used_total": client.get_quota_used(),
            "hydration_errors": len(errors),
        },
        latency_ms=None,
        cost_usd=0.0,
    )

    return {
        "discovered_video_ids": all_video_ids,
        "hydrated_channel_ids": newly_hydrated,
        "youtube_quota_used": quota_spent,
        "node_logs": [node_log.model_dump()],
        "errors": errors,
        "next_action": "continue",
    }

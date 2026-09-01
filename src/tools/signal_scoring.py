"""Deterministic signal computation — no LLM.

Engagement rate, upload cadence, view velocity. score_signals reads
hydrated data from Postgres, computes signals, and persists results.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any



# Bucket edges and labels come straight from the client brief (finance §7).
# The en dashes are theirs and are what the workbook column shows, so they
# are not incidental formatting.
_SIZE_BUCKETS: tuple[tuple[int, str], ...] = (
    (10_000, "<10K"),
    (50_000, "10K–50K"),
    (100_000, "50K–100K"),
    (500_000, "100K–500K"),
    (1_000_000, "500K–1M"),
)



def upload_frequency_band(uploads_per_week: float | None) -> str:
    """The client's cadence band for a channel.

    Module-level so a backfill shares one definition with score_signals --
    the same reason channel_size_bucket was lifted out. This column shipped
    empty for all 4,600 floor-qualifying channels because score_signals had
    not run since the persist allowlist was fixed, while
    uploads_per_week_avg was present for 4,597 of them: the band was
    derivable the whole time.
    """
    upw = float(uploads_per_week or 0)
    if upw >= 3:
        return "daily_plus"
    if upw >= 1:
        return "weekly"
    if upw >= 0.25:
        return "monthly"
    if upw > 0:
        return "irregular"
    return "unknown"


def channel_size_bucket(subscriber_count: int | None) -> str:
    """The client's subscriber-size stratum for a channel.

    Module-level rather than inline in score_signals so a backfill can reuse
    it. The value is a pure function of subscriber_count, which makes it very
    tempting to re-derive in SQL -- and a second copy of these edges is
    exactly how the workbook column and the node drift apart.
    """
    n = int(subscriber_count or 0)
    for edge, label in _SIZE_BUCKETS:
        if n < edge:
            return label
    return "1M+"


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

    # Parse once, drop unparseable rows, then sort. Sorting by a key that can
    # return None raises TypeError the moment one row lacks a usable date.
    timestamps = [
        ts for ts in (_parse_timestamp(v.get("published_at")) for v in videos)
        if ts is not None
    ]

    if len(timestamps) < 2:
        return 0.0

    newest = max(timestamps)
    oldest = min(timestamps)
    span_days = (newest - oldest).total_seconds() / 86400.0
    if span_days <= 0:
        return 0.0

    return round(len(timestamps) / span_days * 30, 2)


def compute_velocity(videos: list[dict]) -> float:
    if len(videos) < 6:
        return 1.0

    dated = [(ts, v) for v in videos
             if (ts := _parse_timestamp(v.get("published_at"))) is not None]
    if len(dated) < 6:
        return 1.0
    sorted_videos = [v for _, v in sorted(dated, key=lambda p: p[0], reverse=True)]

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


def compute_evergreen_score(video_published: Any, video_views: int) -> float:
    """0-100: does this video keep getting watched long after publish, or
    is it a spike that already died?

    A prior version returned min(100, views/day) — raw, unscaled views per
    day since publish. Two failures verified against real data: (1) linear
    scaling saturates instantly — any video with ~100+ views/day (a low bar
    for a 50k+ subscriber channel) hits the 100 ceiling, which pinned 32 of
    60 channels in one real run to EXACTLY 100.00 and put 75% at >=99,
    leaving almost no room to actually differentiate channels. (2) it
    conflates virality with evergreen-ness — a video that got 10,000 views
    in its first three days and has been dead since scores the same
    (maxed-out) as a 3-year-old video still earning a steady trickle of
    views, which is the literal opposite of what "evergreen" means. Fixed
    two ways: log-scaling compresses the realistic 1-100,000+ views/day
    range into a spread that actually separates channels instead of
    saturating most of them, and an age-confidence ramp means a video only
    days old cannot register as "evergreen" no matter how fast it's
    currently climbing — durability hasn't had time to be tested yet.
    """
    if not video_views or video_views <= 0:
        return 0.0
    pub = _parse_timestamp(video_published)
    if not pub:
        return 0.0
    import math

    days = max(1, (datetime.now(timezone.utc) - pub).total_seconds() / 86400)
    views_per_day = video_views / days
    raw = min(100.0, max(0.0, math.log10(views_per_day + 1) * 20.0))
    age_confidence = min(1.0, days / 30.0)
    return round(raw * age_confidence, 2)


def compute_engagement_score_components(channel_signals: dict, cfg) -> tuple[float, dict]:
    components = {
        "views_per_sub": channel_signals.get("views_per_sub_ratio"),
        "comment_rate": channel_signals.get("comment_rate"),
        "like_rate": channel_signals.get("like_rate"),
        "upload_consistency": channel_signals.get("upload_consistency_score"),
    }
    weights = {
        "views_per_sub": cfg.weight_views_per_sub,
        "comment_rate": cfg.weight_comment_rate,
        "like_rate": cfg.weight_like_rate,
        "upload_consistency": cfg.weight_upload_consistency,
    }
    available = {k: w for k, w in weights.items() if components.get(k) is not None}
    if not available:
        return 0.0, {"reason": "no visible components", "components": components}
    total_w = sum(available.values())
    if total_w == 0:
        return 0.0, {"reason": "zero total weight", "components": components}
    norm = {k: w / total_w for k, w in available.items()}
    score = sum(max(0.0, min(1.0, float(components.get(k, 0) or 0))) * norm[k] for k in norm)
    return round(score * 100, 2), {"components": components, "weights": norm, "score": round(score * 100, 2)}


def compute_subscriber_floor(channel: dict, cfg) -> tuple[bool, str | None]:
    subs = int(channel.get("subscriber_count") or 0)
    if subs >= cfg.subscriber_floor:
        return True, None
    vids = channel.get("_videos", [])
    for v in vids:
        v_views = int(v.get("view_count") or 0)
        if v_views >= subs * cfg.breakout_video_multiplier and subs > 0:
            return True, f"breakout_video_{cfg.breakout_video_multiplier}x_subs"
    if vids and subs > 0:
        avg_views = sum(int(v.get("view_count") or 0) for v in vids) / len(vids)
        if avg_views >= subs * cfg.thriving_views_per_sub_multiplier:
            return True, f"views_per_video_{cfg.thriving_views_per_sub_multiplier}x_subs"
    return False, None


def _parse_timestamp(ts: Any) -> datetime | None:
    """Accept what the store actually hands back, not just what the API does.

    `published_at` arrives as an ISO *string* from the YouTube API but as a
    `datetime` from Postgres, because the column is TIMESTAMPTZ and psycopg
    adapts it. `strptime` raises TypeError on a datetime — and the loop below
    only caught ValueError, so it escaped, and score_signals wrapped its whole
    channel loop in one try/except, so the first row killed every signal for
    the round.

    Net effect before this fix: engagement_rate, cadence and velocity were
    never computed or stored in any run — 425 channels, zero signals — while
    the node still logged a channels_scored count and reported success.
    """
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if not isinstance(ts, str):
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def score_signals(state: dict) -> dict:
    from collections import defaultdict

    from src.db.connection import get_connection, put_connection
    from src.tools.dedup import fetch_videos_by_channels, persist_channel_signals, persist_channel_v3, persist_video_v3
    from src.state import ErrorRecord, NodeLog
    from src.config import get_config

    channel_ids = state.get("discovered_channel_ids", [])
    thread_id = state.get("thread_id", "")
    errors: list[dict] = []
    scored = 0

    if not channel_ids:
        return {"next_action": "continue"}

    try:
        conn = get_connection()
        try:
            videos = fetch_videos_by_channels(conn, channel_ids)
            by_channel: dict[str, list[dict]] = defaultdict(list)
            for v in videos:
                by_channel[v.get("channel_id", "")].append(v)

            for ch_id, vids in by_channel.items():
                # Per channel. This loop used to sit inside the single outer
                # try/except, so the first row that raised discarded every
                # signal for the round — which is precisely what happened, on
                # every run, for the whole life of the project.
                try:
                    engagement = round(
                        sum(compute_engagement_rate(v) for v in vids) / len(vids), 6
                    ) if vids else 0.0
                    signals = {
                        "engagement_rate": engagement,
                        "cadence": compute_cadence(vids),
                        "velocity": compute_velocity(vids),
                    }
                    persist_channel_signals(conn, ch_id, signals)
                    scored += 1

                    # v3: evergreen, engagement, floor, news
                    cfg = get_config().harness
                    run_id = state.get("run_id", "")
                    v3_fields: dict[str, Any] = {}
                    v3_vid_updates: dict[str, dict] = {}

                    # Look up channel metadata from store for subscriber count
                    # and the real upload_consistency_score. extract_metadata_signals
                    # runs right before this node and already computed and
                    # persisted the real value — reading it here, not from
                    # the local `signals` dict below (which only ever holds
                    # engagement_rate/cadence/velocity and never actually
                    # gained an "upload_consistency_score" key), is what
                    # makes it real. `signals.get("upload_consistency_score", 0)`
                    # always fell through to its 0 default, silently zeroing
                    # 20% of every channel's engagement_score weight on
                    # every run.
                    ch_subs = 0
                    ch_upload_consistency = 0.0
                    try:
                        cur2 = conn.cursor()
                        cur2.execute(
                            "SELECT subscriber_count, upload_consistency_score FROM channels WHERE channel_id = %s",
                            (ch_id,),
                        )
                        crow = cur2.fetchone()
                        cur2.close()
                        if crow:
                            ch_subs = int(crow[0] or 0)
                            ch_upload_consistency = float(crow[1] or 0)
                    except Exception:
                        pass

                    # Evergreen: per-video score, then channel-level rollup
                    vid_evergreens: list[float] = []
                    for v in vids:
                        eg = compute_evergreen_score(v.get("published_at"), int(v.get("view_count") or 0))
                        vid_evergreens.append(eg)
                        v3_vid_updates[v.get("video_id", "")] = {
                            "evergreen_score": eg,
                            "views_per_day_since_publish": round(
                                int(v.get("view_count") or 0) / max(1,
                                    (datetime.now(timezone.utc) - (_parse_timestamp(v.get("published_at")) or datetime.now(timezone.utc))).total_seconds() / 86400
                                ), 2
                            ) if v.get("published_at") else 0,
                        }
                    if vid_evergreens:
                        subs_weighted = sum(
                            eg * int(v.get("view_count") or 0) for v, eg in zip(vids, vid_evergreens)
                        ) / max(1, sum(int(v.get("view_count") or 0) for v in vids))
                        v3_fields["evergreen_score"] = round(subs_weighted, 2)
                    else:
                        v3_fields["evergreen_score"] = 0.0

                    # Engagement composite
                    sigs = {
                        "views_per_sub_ratio": (sum(int(v.get("view_count") or 0) for v in vids) / max(1, len(vids))) / max(1, ch_subs),
                        "comment_rate": sum(int(v.get("comment_count") or 0) for v in vids) / max(1, sum(int(v.get("view_count") or 0) for v in vids)),
                        "like_rate": sum(int(v.get("like_count") or 0) for v in vids) / max(1, sum(int(v.get("view_count") or 0) for v in vids)),
                        "upload_consistency_score": ch_upload_consistency,
                    }
                    eng_score, eng_components = compute_engagement_score_components(sigs, cfg)
                    v3_fields["engagement_score"] = eng_score
                    v3_fields["engagement_components"] = eng_components

                    # News derivation
                    uploads_per_week = signals.get("cadence", 0) / 30 * 7 if signals.get("cadence") else 0
                    if v3_fields["evergreen_score"] < cfg.news_evergreen_threshold and uploads_per_week > cfg.news_high_frequency_threshold:
                        v3_fields["is_likely_news"] = True
                    elif v3_fields["evergreen_score"] >= cfg.news_evergreen_threshold:
                        v3_fields["is_likely_news"] = False

                    # Subscriber floor
                    ch_data = {"subscriber_count": ch_subs, "_videos": vids}
                    meets, reason = compute_subscriber_floor(ch_data, cfg)
                    v3_fields["meets_subscriber_floor"] = meets
                    if reason:
                        v3_fields["floor_override_reason"] = reason
                    # v4: channel_size_bucket (brief §7)
                    v3_fields["channel_size_bucket"] = channel_size_bucket(ch_subs)
                    # v4: upload_frequency
                    uploads_per_week = signals.get("cadence", 0) / 30 * 7 if signals.get("cadence") else 0
                    v3_fields["upload_frequency"] = upload_frequency_band(uploads_per_week)

                    try:
                        persist_channel_v3(conn, ch_id, run_id, v3_fields)
                        for vid_id, vf in v3_vid_updates.items():
                            if vid_id:
                                persist_video_v3(conn, vid_id, vf)
                    except Exception as exc:
                        # A silent `pass` here once hid a real bug (JSONB
                        # adaptation failure) for engagement_components on
                        # every single channel — evergreen_score,
                        # engagement_score and meets_subscriber_floor never
                        # persisted, and the floor gate downstream stayed
                        # permanently closed, on every real run. `scored`
                        # above only reflects the v1 persist_channel_signals
                        # call, not this one — record the failure instead of
                        # swallowing it.
                        errors.append(
                            ErrorRecord(
                                node_name="score_signals",
                                error_type=type(exc).__name__,
                                message=f"v3 field persist failed for {ch_id}: {exc}",
                                recoverable=True,
                            ).model_dump()
                        )
                except Exception as exc:
                    errors.append(
                        ErrorRecord(
                            node_name="score_signals",
                            error_type=type(exc).__name__,
                            message=f"signal scoring failed for {ch_id}: {exc}",
                            recoverable=True,
                        ).model_dump()
                    )
        finally:
            put_connection(conn)
    except Exception as exc:
        errors.append(
            ErrorRecord(
                node_name="score_signals",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=False,
            ).model_dump()
        )

    return {
        "next_action": "continue",
        "floor_gate_eligible": scored > 0,
        "errors": errors,
        "node_logs": [
            NodeLog(
                node_name="score_signals",
                thread_id=thread_id,
                input_summary={
                    # What actually succeeded. This previously reported
                    # len(channel_ids) — so it read "50 scored" on rounds
                    # where zero signals were computed or stored.
                    "channels_scored": scored,
                    "channels_attempted": len(channel_ids),
                    "scoring_errors": len(errors),
                },
                cost_usd=0.0,
            ).model_dump()
        ],
    }
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

from src.tools.deliverable import deliverable_floor
from src.tools import deadline as run_deadline
from src.tools.youtube_api import YouTubeAPIClient
from src.tools.outlier_score import score_channel_videos
from src.tools.dedup import persist_channel, persist_video, persist_channel_v3, persist_channel_snapshot, persist_video_v3
from src.state import NodeLog, ErrorRecord

# YouTube raised the Shorts ceiling from 60s to 3 minutes in Oct 2024.
# Being under it is necessary but not sufficient — the upload also has to
# be vertical — so this only narrows the candidate set; UUSH membership
# decides.
SHORTS_MAX_SECONDS = 180

# Bounds the per-channel long-form walk. The median channel in the current
# deliverables holds ~312 long-form uploads (~14 quota units); the largest
# holds 15,473. Without a bound one such channel would spend most of a day's
# 10,000-unit quota by itself, starving every other channel in the round.
LIFETIME_SCAN_MAX_VIDEOS = 3000


class _SkipEnrichment(Exception):
    """Sentinel: this channel is below the floor, so the per-channel
    YouTube enrichment lookups are deliberately skipped rather than failed.
    Raised and caught locally; never escapes the node."""



def _derive_vertical_start(ch: dict, conn, fields: dict) -> None:
    """Estimate when this channel started producing content in this vertical.

    Both briefs warn against assuming the channel's creation date is its
    vertical start date — Finance #4 spells it out, because an old channel
    may have pivoted into finance only recently.

    The honest answer depends on how much of the channel's history we
    actually hold, so the basis and confidence say which case applied:

      first_vertical_video_observed (0.7)
          We hold the channel's full long-form catalogue (the walk reached
          its last page), so the earliest video we can see really is its
          earliest, and the date is a genuine observation.

      earliest_observed_video_lower_bound (0.4)
          The walk was truncated, so the oldest video we hold is only an
          upper bound on the true start — the channel was already posting
          before it. Recorded as a bound rather than a fact.

      channel_creation_date (0.3)
          No videos at all; the weakest fallback, and exactly the
          assumption the brief cautions about, so it is labelled as such.

    The earlier implementation read `ORDER BY published_at ASC LIMIT 5`
    from a table holding only a recent window, then stamped 0.7 on the
    result — reporting a months-old date as a years-old channel's vertical
    debut. Its docstring also claimed a keyword overlap the SQL never
    performed.
    """
    channel_id = ch.get("channel_id", "")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT MIN(published_at), COUNT(*) FROM videos WHERE channel_id = %s",
            (channel_id,),
        )
        earliest, held = cur.fetchone()
        cur.close()
    except Exception:
        earliest, held = None, 0

    if earliest:
        # Complete only if we hold at least as many long-form videos as the
        # channel is known to have. total_long is stamped by the caller from
        # the UULF playlist count.
        total_long = ch.get("_total_long_form")
        complete = isinstance(total_long, int) and held >= total_long
        fields["vertical_start_date"] = str(earliest)
        if complete:
            fields["vertical_start_date_basis"] = "first_vertical_video_observed"
            fields["vertical_start_date_confidence"] = 0.7
        else:
            fields["vertical_start_date_basis"] = "earliest_observed_video_lower_bound"
            fields["vertical_start_date_confidence"] = 0.4
    elif ch.get("published_at"):
        fields["vertical_start_date"] = ch["published_at"]
        fields["vertical_start_date_basis"] = "channel_creation_date"
        fields["vertical_start_date_confidence"] = 0.3


LATEST_LONG_FORM_SAMPLE = 50
TOP_LIFETIME_SAMPLE = 20


def _select_sample(videos: list[dict]) -> list[dict]:
    """The rows worth keeping out of a full-catalogue scan.

    Both briefs define the sample as a latest-N window plus a top-N-lifetime
    set. The two overlap — a recent upload can also be a channel's
    best-performing — so this is a union, not a concatenation, and each kept
    video carries the reason it survived. 'top_lifetime' wins ties, being
    the stronger claim.

    Shorts pass through untouched: they are already a bounded sample from
    UUSH and are analysed separately, never mixed into long-form ranking.
    """
    shorts = [v for v in videos if v.get("is_short")]
    long_form = [v for v in videos if not v.get("is_short")]

    latest = sorted(
        long_form, key=lambda v: v.get("published_at") or "", reverse=True
    )[:LATEST_LONG_FORM_SAMPLE]
    top_lifetime = sorted(
        (v for v in long_form if (v.get("view_count") or 0) > 0),
        key=lambda v: v.get("view_count") or 0,
        reverse=True,
    )[:TOP_LIFETIME_SAMPLE]

    kept: dict[str, dict] = {}
    for video in latest:
        vid = video.get("video_id")
        if vid:
            kept[vid] = {**video, "sample_reason": "latest"}
    for video in top_lifetime:
        vid = video.get("video_id")
        if vid:
            kept[vid] = {**video, "sample_reason": "top_lifetime"}
    for video in shorts:
        vid = video.get("video_id")
        if vid and vid not in kept:
            kept[vid] = video
    return list(kept.values())


def _tag_video_samples(conn, videos: list[dict]) -> None:
    """Tag the latest 50 long-form as 'latest' and the top 20 by lifetime
    views as 'top_lifetime' (Crime brief #5, Finance brief #9).

    `videos` must be the channel's FULL long-form catalogue — see
    YouTubeAPIClient.get_channel_long_form_scan. Passing only a recent
    window silently reduces 'top_lifetime' to "best of the newest N", which
    is not what either brief asks for.

    Ranked by view_count rather than outlier_score: outlier score is
    relative to the channel's own average, so it surfaces the videos that
    over-performed *for that channel*, which is a different and useful
    question — but "top lifetime videos" plainly means the most-watched.

    'latest' is written first and 'top_lifetime' overrides it, so a video
    that is both keeps the stronger label.
    """
    if not videos:
        return
    long_form = [v for v in videos if not v.get("is_short")] or videos
    sorted_by_date = sorted(
        long_form, key=lambda v: v.get("published_at") or "", reverse=True
    )
    sorted_by_outlier = sorted(
        [v for v in long_form if (v.get("view_count") or 0) > 0],
        key=lambda v: v.get("view_count") or 0, reverse=True,
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



def _persist_channels_for_test(channels: list[dict]) -> list[str]:
    """The per-channel isolation, exercised without a database.

    hydrate_metadata's persistence loop needs a live connection, a YouTube
    client and a quota budget, so the behaviour that matters here -- one
    channel's failure not taking the batch with it -- is otherwise only
    assertable by reading the source. This runs the same shape.
    """
    persisted: list[str] = []
    for ch in channels:
        try:
            persist_channel(None, ch)
        except Exception:
            continue
        persisted.append(ch["channel_id"])
    return persisted

def hydrate_metadata(state: dict) -> dict:
    channel_ids = state.get("discovered_channel_ids", [])
    thread_id = state.get("thread_id", "")
    hydrated = set(state.get("hydrated_channel_ids", set()))

    to_hydrate = [c for c in channel_ids if c not in hydrated]

    # Enforce the run's channel ceiling here rather than only in discovery
    # admission. That check runs BEFORE a node, so the first discovery node
    # can overshoot the cap in one go -- keyword_search returned 96 channels
    # in a single call against a cap of 36. Hydration is the gate everything
    # downstream depends on (classification, geo, signals and cohorts all
    # work from hydrated channels), so trimming here is what actually makes
    # the tier's estimate true instead of aspirational.
    from src.config import get_config as _cfg_for_cap

    _harness = _cfg_for_cap().harness
    # The HYDRATION ceiling, not the delivery target. This node's cost is
    # per hydrated channel; the target is a promise about how many of them
    # will clear the subscriber floor and reach the file. Trimming to the
    # target here is what capped a 100-channel Standard run at 24
    # floor-passing channels and a 17-row workbook.
    from src.tools.deliverable import run_ceilings as _ceilings_for_cap

    target, cap = _ceilings_for_cap(_harness)
    trimmed = 0
    # Once the run holds the floor-passing channels it promised, hydrating
    # more buys nothing the deliverable can use.
    already_qualified = len(state.get("qualified_channel_ids") or ())
    if target > 0 and already_qualified >= target:
        return {
            "next_action": "continue",
            "node_logs": [
                NodeLog(
                    node_name="hydrate_metadata",
                    thread_id=thread_id,
                    input_summary={
                        "reason": "delivery target met",
                        "qualified": already_qualified,
                        "target": target,
                    },
                ).model_dump()
            ],
        }
    if cap > 0:
        # Spread the cap across branches instead of letting the first round
        # spend all of it.
        #
        # The cap is what a tier can afford to enrich; the branches are what
        # it is supposed to cover. Taking whatever the first discovery round
        # found meant those two never met: a Standard education run filled
        # all 100 slots on its FIRST hydration and then visited four more
        # branches -- online-learning, test-prep, k12-tutorials,
        # trivia-entertainment -- that could contribute nothing. Standard
        # promises "enough channels in each sub-niche for the comparisons
        # the workbook is built for", and delivered one sub-niche.
        #
        # Divided by the run's TOTAL rounds, not by its branches.
        #
        # Dividing by branches alone was not enough, and a live Standard run
        # showed why: a branch gets max_rounds_per_branch rounds, so at
        # cap/branches per round the first branch spent 4 x 25 = the whole
        # hundred and the run ended, on the cap breaker, having visited only
        # `root`. Fewer wasted rounds than before, and still one sub-niche.
        #
        # rounds_total is branches x rounds each, so the share is what one
        # round may take for the cap to last the whole run: 7 at Standard,
        # 4 at Deep, and the entire cap at Sample, which is a single round.
        # Each branch then ends up with roughly cap/branches across its own
        # rounds, which is the coverage the tier's description promises.
        #
        # Capacity a thin branch leaves unused stays available, because the
        # share is only ever the smaller of it and the room actually left.
        branches = int(getattr(_harness, "max_branches", 1) or 1)
        rounds_each = int(getattr(_harness, "max_rounds_per_branch", 1) or 1)
        rounds_total = max(1, branches * rounds_each)
        # Rounded up, so the cap can still be filled if every round takes
        # its full share rather than leaving a remainder unspent.
        share = -(-cap // rounds_total)
        room = max(0, min(cap - len(hydrated), share))
        if len(to_hydrate) > room:
            trimmed = len(to_hydrate) - room
            # Biggest first: if a run can only afford some of what it found,
            # the channels worth keeping are the ones a client would look at.
            to_hydrate = to_hydrate[:room]

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
    # The client's 50k rule, read once rather than per channel -- and read
    # from the one place that defines it, so the deep scan is spent on
    # exactly the channels the workbook will carry.
    floor = deliverable_floor()
    stopped_on_deadline = False
    for ch in channels:
        # Per-channel video fetching, measured at ~16 minutes for 285
        # channels. Without a check here a run that hits its deadline during
        # discovery still spends that whole time hydrating before anything
        # notices, which is most of a short tier's entire window. Channels
        # left unhydrated are simply not in the export -- partial data the
        # run paid for, rather than an overrun the client did not ask for.
        if run_deadline.research_passed():
            stopped_on_deadline = True
            break
        ch["discovery_method"] = _attribute(ch["channel_id"], kw_found, gw_found)
        # v4 competitor_ecosystem: channels resolved from competitor-benchmark
        # seeds get a distinct label (plan §10 item 3)
        active_node = state.get("tree", {}).get(state.get("active_node_id") or "", {})
        competitor_seeds = set(active_node.get("_competitor_seeds") or [])
        if ch["channel_id"] in gw_found and ch["channel_id"] in competitor_seeds:
            ch["discovery_method"] = "competitor_ecosystem"
        # YouTube's own channels.list snippet.publishedAt — the channel's
        # real creation date — was already being fetched into ch["published_at"]
        # by _parse_channel, then silently discarded here: the previous line
        # read a "first_seen_at" key that dict never had, so it always fell
        # through to "now". Real, better data was sitting right there unused.
        ch["first_seen_at"] = ch.get("published_at") or datetime.now(timezone.utc).isoformat()
        # Per channel, not per round. This node is the fan-in join and is not
        # wrapped by graph.py's _guarded, so anything escaping here ends the
        # run — and by this point the round's Bright Data records are already
        # paid for. One channel's metadata is worth losing; a round is not.
        try:
            # The full-catalogue walk is only worth its quota on channels
            # that can actually reach the deliverable. Measured on the live
            # augment runs: ~9-10% of discovered channels clear the
            # subscriber floor, but every one of them was getting the deep
            # scan — ~14 API calls each instead of ~3. That burned the
            # entire 10,000-unit daily quota in about an hour, on channels
            # that were then filtered out, and stalled discovery completely.
            #
            # Sub-floor channels still get a single page, which is what the
            # pipeline always did: enough for outlier scoring and graph
            # traversal, which is all they are ever used for. Floor-clearing
            # channels get the deep scan, because they are the ones the
            # briefs' latest-50 + top-20-lifetime sampling applies to.
            deep = (ch.get("subscriber_count") or 0) >= floor
            videos = client.get_channel_videos(
                ch["channel_id"],
                max_results=LIFETIME_SCAN_MAX_VIDEOS if deep else 50,
                deep_scan=deep,
            )
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
        # Scan wide, keep narrow. The walk above reads the channel's whole
        # long-form catalogue so "top 20 lifetime" is genuinely lifetime,
        # but persisting all of it would store thousands of rows per
        # channel — the median here holds 312 long-form uploads and the
        # largest 15,473, against a brief that asks for ~70. That would
        # inflate the deliverable roughly twentyfold and bill every
        # downstream per-video LLM node for the privilege.
        ch["_videos"] = _select_sample(scored)
        all_video_ids.extend(v["video_id"] for v in ch["_videos"])

    # Declared before the try, because the summary below reads it: a run
    # that cannot even open a connection must report zero hydrated rather
    # than raise NameError on the way to saying so.
    persisted_ids: list[str] = []
    #: Videos YouTube returned under a channel that does not own them.
    foreign_videos = 0

    try:
        from src.db.connection import get_connection, put_connection

        conn = get_connection()
        try:
            import json as _json

            for ch in channels:
                # One channel's failure is one channel's, not the batch's.
                #
                # A ForeignKeyViolation on a single video used to escape this
                # loop entirely: it skipped every remaining channel AND the
                # persist_category_tags call below, which is the only thing
                # that records which rows belong to this run. The workbook is
                # built from those tags, so one bad video row produced a file
                # with every sheet empty -- while the node still reported
                # "channels_hydrated: 21", because that counted what it was
                # handed rather than what it stored.
                try:
                    # Recomputed here rather than reused from the fetch loop
                    # above: that loop has already finished, so its `deep` would
                    # hold whichever channel it happened to end on and every
                    # channel in this loop would inherit that one answer.
                    deep = (ch.get("subscriber_count") or 0) >= floor
                    persist_channel(conn, ch)

                    # A video whose owner is not this channel cannot be
                    # stored: videos.channel_id is a foreign key, and the
                    # only channel row this loop has just written is this
                    # one. YouTube does return them -- a scan of
                    # UC...WhVA's uploads on run-b8b0ea1bf0a6 came back
                    # carrying a video owned by a channel the run had never
                    # seen, and the insert took the whole batch down with
                    # it.
                    #
                    # Dropped rather than re-attributed. Rewriting the
                    # owner to this channel would file another creator's
                    # video under it, and every per-channel statistic in
                    # the workbook is computed over exactly these rows.
                    own = [v for v in ch.get("_videos", [])
                           if v.get("channel_id") == ch["channel_id"]]
                    if len(own) != len(ch.get("_videos", [])):
                        strays = len(ch.get("_videos", [])) - len(own)
                        logger.warning(
                            "hydrate_dropped_foreign_owned_videos",
                            channel_id=ch["channel_id"],
                            dropped=strays,
                            kept=len(own),
                        )
                        foreign_videos += strays
                        ch["_videos"] = own

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
                    # 4 quota units for the long/Shorts/live split, plus a few
                    # more for the Shorts ID set that settles is_short — but
                    # only for channels that can reach the deliverable. Both
                    # answer questions asked exclusively of exported channels:
                    # the breakdown fills export columns, and is_short decides
                    # which sheet a row lands in.
                    #
                    # Ungated, this ran for every discovered channel and was the
                    # dominant quota leak: measured live, 233 channels hydrated
                    # in 35 minutes of which only 11 cleared the floor, yet all
                    # 233 paid ~6 units here. Combined burn hit 367 calls/min —
                    # enough to exhaust two projects' 20,000-unit daily
                    # allowance in under an hour.
                    breakdown = {}
                    shorts_ids: set[str] = set()
                    try:
                        if not deep:
                            raise _SkipEnrichment
                        breakdown = client.get_channel_upload_breakdown(ch["channel_id"])
                        # Lets _derive_vertical_start tell "we hold this
                        # channel's whole catalogue" from "the walk was cut
                        # short", which is the difference between an observed
                        # start date and a mere upper bound on one.
                        ch["_total_long_form"] = breakdown.get("long")
                        oldest_sampled = min(
                            (v.get("published_at") for v in ch.get("_videos", [])
                             if v.get("published_at")),
                            default=None,
                        )
                        shorts_ids = client.get_channel_shorts_ids(
                            ch["channel_id"], published_after=oldest_sampled
                        )
                    except _SkipEnrichment:
                        # Sub-floor channel: deliberately skipped, not an error.
                        # The counts stay NULL, which reads as "not looked up"
                        # rather than a false zero, and is_short falls back to
                        # the duration bound — exactly what this channel would
                        # have had before the breakdown existed, and it never
                        # reaches the export anyway.
                        pass
                    except Exception as exc:
                        # Never fail hydration over the breakdown: the counts
                        # stay NULL ("not looked up") and is_short falls back to
                        # the duration bound, which is what the whole pipeline
                        # did before this existed.
                        errors.append(ErrorRecord(
                            node_name="hydrate_metadata",
                            error_type=type(exc).__name__,
                            message=f"upload breakdown failed for {ch['channel_id']}: {exc}",
                            recoverable=True,
                        ).model_dump())
                    persist_channel_snapshot(
                        conn, ch["channel_id"], run_id,
                        ch.get("subscriber_count", 0),
                        ch.get("view_count", 0),
                        ch.get("video_count", 0),
                        long_video_count=breakdown.get("long"),
                        shorts_count=breakdown.get("shorts"),
                        live_stream_count=breakdown.get("live"),
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
                    # v4: channel age information
                    if ch.get("published_at"):
                        v3_fields["channel_creation_date"] = ch["published_at"]
                    # v4: vertical_start_date — earliest video matching this niche's keywords
                    _derive_vertical_start(ch, conn, v3_fields)
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
                            # Duration alone can only ever narrow the field, not
                            # decide it: YouTube's Shorts ceiling is 3 minutes
                            # (raised from 60s in Oct 2024), and being under it
                            # is necessary but not sufficient — a brief
                            # LANDSCAPE upload is not a Short. The old
                            # `0 < dur <= 60` rule was wrong in both
                            # directions, filing 61-180s vertical Shorts as
                            # long-form and short landscape videos as Shorts.
                            # This is the provisional value; the authoritative
                            # one is membership in the channel's UUSH
                            # auto-playlist, applied just below.
                            v3_vid["is_short"] = 0 < dur <= SHORTS_MAX_SECONDS
                        if shorts_ids:
                            # YouTube's own answer wins over any duration rule.
                            # Only trusted when the lookup actually returned
                            # something — an empty set can equally mean "no
                            # Shorts" or "the call failed", and overwriting
                            # every flag to False on a failed call would be
                            # worse than the heuristic.
                            v3_vid["is_short"] = vid["video_id"] in shorts_ids
                        if vid.get("default_language"):
                            v3_vid["language_code"] = vid["default_language"]
                        if v3_vid:
                            persist_video_v3(conn, vid["video_id"], v3_vid)
                    # v4 sample_reason: latest 50 by date, top 20 lifetime by outlier
                    _tag_video_samples(conn, ch.get("_videos", []))
                except Exception as exc:
                    # Roll back this channel's partial work so the connection
                    # is usable for the next one, and keep its id out of
                    # persisted_ids -- tagging a channel whose row was rolled
                    # back would only move the foreign-key failure downstream.
                    conn.rollback()
                    errors.append(
                        ErrorRecord(
                            node_name="hydrate_metadata",
                            error_type=type(exc).__name__,
                            message=f"channel {ch.get('channel_id', '?')}: {exc}",
                            recoverable=True,
                        ).model_dump()
                    )
                    continue
                persisted_ids.append(ch["channel_id"])

            # Membership, so this run's slice can be exported later without
            # run_id columns on the shared entity tables. This node is the only
            # place that knows the run, the active branch and the entities at
            # the same moment.
            from src.tools.dedup import persist_category_tags

            # persisted_ids, not every channel handed to this node: a
            # channel whose persistence was rolled back has no row to point
            # at, and tagging it would push the same foreign-key failure
            # into the tags table -- losing the whole batch a second time,
            # one step later.
            kept = set(persisted_ids)
            persist_category_tags(
                conn,
                run_id=state.get("run_id", ""),
                tree_node_id=state.get("active_node_id") or "",
                channel_ids=persisted_ids,
                video_ids=[
                    v for ch in channels if ch["channel_id"] in kept
                    for v in (vid["video_id"] for vid in ch.get("_videos", []))
                ],
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
    # persisted_ids, not `channels`: a channel whose row was rolled back
    # cannot be in the workbook, so counting it towards the delivery target
    # would promise a row that does not exist.
    _kept = set(persisted_ids)
    newly_qualified = {
        ch["channel_id"]
        for ch in channels
        if ch["channel_id"] in _kept
        and int(ch.get("subscriber_count") or 0) >= floor
    }
    quota_spent = client.quota_consumed_this_call(quota_baseline)

    hydration_summary = {
        # What actually reached the database. This counted len(channels) --
        # what the node was handed -- and so reported 21 hydrated on a run
        # where one channel persisted and the rest were lost to an aborted
        # loop. A count that cannot go down is not a measurement.
        "channels_hydrated": len(persisted_ids),
        "channels_attempted": len(channels),
        "foreign_owned_videos_dropped": foreign_videos,
        "videos_fetched": len(all_video_ids),
        "quota_spent_this_round": quota_spent,
        "quota_used_total": client.get_quota_used(),
        "hydration_errors": len(errors),
    }
    if stopped_on_deadline:
        # Reads as "time ran out" on the Live runs stream rather than as
        # channels quietly going missing from the export.
        hydration_summary["stopped_on"] = "run_deadline_seconds"
    if trimmed:
        hydration_summary["trimmed_to_cap"] = trimmed
    # What the run has towards its promise, not just what it paid for.
    hydration_summary["channels_over_floor"] = len(newly_qualified)

    node_log = NodeLog(
        node_name="hydrate_metadata",
        thread_id=thread_id,
        input_summary=hydration_summary,
        latency_ms=None,
        cost_usd=0.0,
    )

    return {
        "discovered_video_ids": all_video_ids,
        "hydrated_channel_ids": newly_hydrated,
        # The subset that can actually reach the workbook, decided by the
        # same rule the export applies -- read straight off the API's own
        # subscriber_count, which this node already has in hand.
        "qualified_channel_ids": newly_qualified,
        "youtube_quota_used": quota_spent,
        "node_logs": [node_log.model_dump()],
        "errors": errors,
        "next_action": "continue",
    }

"""Export a run's research output as files a human can be handed.

The store answers queries; it does not produce a deliverable. This turns one
run into a directory that a Content Creation Team can open and that Claude can
re-read later to write a hand-off report.

Run-scoped through `category_tags`, not through the store's entity tables.
`channels` and `videos` are shared — a channel can belong to a Finance run and
a Legal one — so the run's slice comes from the membership table plus the
run's own checkpoint, and two categories can coexist in one database without
either export contaminating the other.

Both CSV and JSON, deliberately. CSV opens in Excel for the team; JSON keeps
the structure for whatever reads it next. `manifest.json` carries provenance —
profile, spend, and *why the run stopped* — because every run so far has
terminated on a governor with novelty still high, and a report that does not
say so reads as more complete than it is.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.config import get_config

logger = structlog.get_logger(__name__)


def export_dir(run_id: str) -> Path:
    base = Path(get_config().harness.export_dir) / run_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fetch(sql: str, params: tuple) -> list[dict[str, Any]]:
    from src.db.connection import get_connection, put_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            cur.close()
    finally:
        put_connection(conn)


def _floor() -> int:
    """The hard subscriber floor the deliverable is scoped to.

    Deliberately NOT `channels.meets_subscriber_floor`. That flag is TRUE
    for sub-floor channels that tripped a breakout/thriving override in
    compute_subscriber_floor — a useful internal signal for spotting small
    channels worth watching, but it let a 13-subscriber channel into a
    workbook whose stated rule is "only channels over 50k subs". The
    client's rule is about the real count, so the export reads the real
    count.
    """
    return int(get_config().harness.subscriber_floor)


def fetch_run_channels(
    run_id: str, category: str | None = None, min_subscribers: int | None = None
) -> list[dict[str, Any]]:
    """Channels tagged to this run, with the full v3 enrichment set flattened.

    v1's `extra->>'engagement_rate'/'cadence'/'velocity'` JSONB reads are
    gone — v3's score_signals/resolve_geo_language/classify_channel nodes
    write real typed columns instead (evergreen_score, engagement_score,
    uploads_per_week_avg, country/language, niche, entertainment_score,
    ...), which is what the client's "classified, in distinguished columns"
    requirement actually asks for.

    Two filters, both client requirements rather than cosmetics:
    meets_subscriber_floor ("only analyze channels over 50k subs"), and an
    optional parent_category ("no unrelated channels"). Discovery
    legitimately surfaces off-topic channels — a crime-keyword search
    returns a lifestyle vlog named "True Crime PROFILE 2026" — and the
    classifier correctly labels them; the category filter is what keeps
    them out of a single-category deliverable.

    first_video_published_at is the channel's real first-ever upload date,
    resolved by resolve_first_video_date paginating the uploads playlist to
    its actual last page — NOT derived from MIN(videos.published_at). That
    would only reflect hydrate_metadata's 50-most-recent-videos sample
    (the API returns newest-first, no oldest-first sort exists), which for
    any channel with more than 50 uploads ever is the oldest video in a
    RECENT window, not the true first one — verified off by over a decade
    on a real channel. last_video_published_at doesn't have this problem:
    the newest video in the sample IS genuinely the channel's most recent
    upload, so it's still read straight from the videos table.

    total_video_count is the channel's real total upload count, straight
    from YouTube's channels.list statistics.videoCount (persisted every run
    into channel_snapshots by hydrate_metadata) — NOT COUNT(videos), which
    would only ever be hydrate_metadata's capped sample (<=50 most recent
    uploads), the exact same trap first_video_published_at had to be fixed
    for.

    total_long_video_count / total_shorts_count / total_live_stream_count
    break that single total down by upload type, and satisfy
    long + shorts + live = total exactly. statistics.videoCount carries no
    type split, so these come from YouTube's auto-generated per-type
    playlists (UULF/UUSH/UULV) — see
    YouTubeAPIClient.get_channel_upload_breakdown. They are NOT derived by
    counting the Videos/Shorts sheets: those hold only the <=50-video
    sample, so their ratio says nothing about a channel with 11k uploads.
    NULL means the breakdown was never looked up for that run, which is
    deliberately distinct from a real 0.

    min_subscribers overrides the client's 50k floor — pass 0 for "every
    discovered channel, no floor", the big-run deliverable's own ask.
    """
    floor = _floor() if min_subscribers is None else min_subscribers
    sql = f"""
        SELECT DISTINCT
               c.channel_id, c.title, c.subscriber_count, c.description,
               c.discovery_method, c.first_seen_at AS "Channel Creation Date",
               c.first_video_published_at,
               (SELECT MAX(v.published_at) FROM videos v WHERE v.channel_id = c.channel_id)
                   AS last_video_published_at,
               (SELECT cs.total_video_count FROM channel_snapshots cs
                    WHERE cs.channel_id = c.channel_id AND cs.run_id = t.run_id)
                   AS total_video_count,
               (SELECT cs.long_video_count FROM channel_snapshots cs
                    WHERE cs.channel_id = c.channel_id AND cs.run_id = t.run_id)
                   AS total_long_video_count,
               (SELECT cs.shorts_count FROM channel_snapshots cs
                    WHERE cs.channel_id = c.channel_id AND cs.run_id = t.run_id)
                   AS total_shorts_count,
               (SELECT cs.live_stream_count FROM channel_snapshots cs
                    WHERE cs.channel_id = c.channel_id AND cs.run_id = t.run_id)
                   AS total_live_stream_count,
               c.country_code, c.region,
               c.primary_language_code, c.language_confidence,
               c.face_status, c.dominant_format,
               nt.parent_category AS category, nt.niche_name AS sub_niche,
               nt.description AS niche_description,
               c.entertainment_score, c.evergreen_score, c.engagement_score,
               c.is_likely_news, c.uploads_per_week_avg, c.upload_consistency_score,
               c.has_affiliate_signal, c.has_sponsor_signal, c.has_membership_signal,
               c.data_completeness_score, c.missing_required_fields
        FROM channels c
        JOIN category_tags t
          ON t.entity_id = c.channel_id AND t.entity_type = 'channel'
        LEFT JOIN niche_taxonomy nt ON nt.niche_id = c.primary_niche_id
        WHERE t.run_id = %s
          AND c.subscriber_count >= {floor}
    """
    params: tuple = (run_id,)
    if category:
        sql += " AND nt.parent_category = %s"
        params = (run_id, category)
    sql += " ORDER BY c.subscriber_count DESC NULLS LAST"
    return _fetch(sql, params)


def fetch_run_videos(
    run_id: str,
    limit: int | None,
    category: str | None = None,
    min_subscribers: int | None = None,
) -> list[dict[str, Any]]:
    """This run's videos, highest outlier score first, with v3's title
    signal columns and evergreen/news flags flattened alongside them.

    Ordered by outlier score rather than views because the score is the point:
    a 200k-view video on a 5M-subscriber channel is unremarkable, while the
    same on a 20k channel is the signal the team is looking for.

    Scoped to the same channels the Channels sheet carries — a video whose
    channel was filtered out for being under the subscriber floor or in
    another category must not survive here. Pass limit=None for every
    tagged video with no cap — the big-run deliverable's own ask — and
    min_subscribers=0 alongside it so the floor doesn't quietly re-exclude
    everything limit=None just stopped capping.
    """
    floor = _floor() if min_subscribers is None else min_subscribers
    sql = f"""
        SELECT DISTINCT
               v.video_id, v.channel_id, c.title AS channel_title,
               v.title, v.video_description, v.view_count, v.like_count, v.comment_count,
               v.published_at,
               CASE WHEN v.published_at IS NOT NULL
                    THEN ROUND(EXTRACT(EPOCH FROM (NOW() - v.published_at)) / 86400)
               END AS days_since_published,
               v.outlier_score,
               v.duration_seconds, v.is_short, v.language_code,
               v.evergreen_score, v.is_likely_news, v.views_per_day_since_publish,
               v.title_word_count, v.title_has_number,
               v.title_is_question, v.title_capitalization, v.title_emoji_count
        FROM videos v
        JOIN category_tags t
          ON t.entity_id = v.video_id AND t.entity_type = 'video'
        JOIN channels c ON c.channel_id = v.channel_id
        LEFT JOIN niche_taxonomy nt ON nt.niche_id = c.primary_niche_id
        WHERE t.run_id = %s
          AND c.subscriber_count >= {floor}
    """
    params: tuple = (run_id,)
    if category:
        sql += " AND nt.parent_category = %s"
        params = params + (category,)
    sql += " ORDER BY v.outlier_score DESC NULLS LAST"
    if limit is not None:
        sql += " LIMIT %s"
        params = params + (limit,)
    return _fetch(sql, params)


def fetch_run_niche_breakdown(
    run_id: str, category: str | None = None, min_subscribers: int | None = None
) -> list[dict[str, Any]]:
    """One row per niche this run actually populated — category, sub-niche,
    and how many of this run's channels landed in it. Feeds the Excel
    Overview sheet and the Niches sheet."""
    floor = _floor() if min_subscribers is None else min_subscribers
    sql = f"""
        SELECT nt.niche_id, nt.parent_category AS category, nt.niche_name AS sub_niche,
               nt.description, nt.is_evergreen_prone,
               COUNT(DISTINCT c.channel_id) AS channel_count
        FROM niche_taxonomy nt
        JOIN channels c ON c.primary_niche_id = nt.niche_id
        JOIN category_tags t
          ON t.entity_id = c.channel_id AND t.entity_type = 'channel'
        WHERE t.run_id = %s
          AND c.subscriber_count >= {floor}
    """
    params: tuple = (run_id,)
    if category:
        sql += " AND nt.parent_category = %s"
        params = (run_id, category)
    sql += (
        " GROUP BY nt.niche_id, nt.parent_category, nt.niche_name, nt.description, nt.is_evergreen_prone"
        " ORDER BY channel_count DESC"
    )
    return _fetch(sql, params)


def _fetch_run_factors(
    table: str, taxonomy: str, run_id: str, category: str | None, min_subscribers: int | None = None
) -> list[dict[str, Any]]:
    """Shared body for the success/failure factor sheets — identical shape,
    identical filters, only the pair of table names differs. Both are
    scoped to the same channels the Channels sheet carries."""
    floor = _floor() if min_subscribers is None else min_subscribers
    sql = f"""
        SELECT f.channel_id, c.title AS channel_title, c.subscriber_count,
               ft.factor_code, ft.factor_label, ft.factor_group,
               f.evidence_grade, f.evidence_note, f.corroboration_count
        FROM {table} f
        JOIN {taxonomy} ft ON ft.factor_id = f.factor_id
        JOIN channels c ON c.channel_id = f.channel_id
        LEFT JOIN niche_taxonomy nt ON nt.niche_id = c.primary_niche_id
        WHERE f.extracted_by_run_id = %s
          AND c.subscriber_count >= {floor}
    """
    params: tuple = (run_id,)
    if category:
        sql += " AND nt.parent_category = %s"
        params = (run_id, category)
    sql += " ORDER BY c.subscriber_count DESC NULLS LAST, f.evidence_grade"
    return _fetch(sql, params)


def fetch_run_success_factors(
    run_id: str, category: str | None = None, min_subscribers: int | None = None
) -> list[dict[str, Any]]:
    """Channels' confirmed success factors for this run, joined to their
    taxonomy label so the sheet reads without a code lookup."""
    return _fetch_run_factors(
        "channel_success_factors", "success_factor_taxonomy", run_id, category, min_subscribers
    )


def fetch_run_failure_factors(
    run_id: str, category: str | None = None, min_subscribers: int | None = None
) -> list[dict[str, Any]]:
    """Channels' confirmed failure factors for this run, joined to their
    taxonomy label so the sheet reads without a code lookup."""
    return _fetch_run_factors(
        "channel_failure_factors", "failure_factor_taxonomy", run_id, category, min_subscribers
    )


def fetch_run_comment_author_only_channel_ids(run_id: str) -> set[str]:
    """Channels with NO independent evidence of relevance beyond a comment.

    Two conditions, both required: every discovery edge into the channel is
    a comment_author edge, AND its discovery_method is not 'keyword' or
    'both'. The discovery_method check matters more than it looks — an
    early version of this query checked edges alone and flagged Graham
    Stephan (a root SEED channel, 5.18M subscribers, discovery_method
    'keyword') as comment-only, because his one inbound edge in this run
    happens to be a pinned self-comment on his own video (source ==
    target). 58 of 65 "comment-only" hits on the first pass turned out to
    be exactly this: real channels independently found by keyword search
    that also had a self-comment or a comment from another real creator.
    Only channels with NO keyword evidence at all — discovery_method
    'graph_walk' or 'unattributed' — are genuinely "in this dataset for no
    reason other than a comment."
    """
    rows = _fetch(
        """
        SELECT e.target_channel_id
        FROM discovery_edges e
        JOIN channels c ON c.channel_id = e.target_channel_id
        WHERE e.run_id = %s AND e.target_channel_id IS NOT NULL
          AND c.discovery_method NOT IN ('keyword', 'both')
        GROUP BY e.target_channel_id
        HAVING array_agg(DISTINCT e.edge_type) = ARRAY['comment_author']::text[]
        """,
        (run_id,),
    )
    return {r["target_channel_id"] for r in rows}


def flag_low_confidence_channels(
    channels: list[dict[str, Any]],
    comment_author_only_ids: set[str],
) -> list[dict[str, Any]]:
    """Mark channels a report should discount or exclude — never drop them.

    Two independent contamination sources, both observed on the Finance run:

      - zero_subscribers: BrightData returned no/zero subscriber count.
        Keyword search surfacing a channel it happened to return is not the
        same as that channel being a real, active creator — this run's
        zero-subscriber channels are templated/duplicate-looking names
        ("Money Management Guide" x3, "Moneytok2026") with no other
        discovery edge, i.e. nothing else in the dataset points at them.
      - comment_author_only: see fetch_run_comment_author_only_channel_ids —
        NO independent (keyword-search) evidence of relevance, and every
        discovery edge into the channel is a comment_author edge. The ONLY
        reason a channel this flags is in the dataset is that whoever runs
        it commented on a video — not evidence it's a finance channel.

    The ambiguity a report writer needs to resolve — is this noise, or a
    genuine small channel? — belongs to the report, not a silent filter
    at export time. Flagging keeps the data and makes the judgment call
    visible instead of invisible.
    """
    flagged = []
    for ch in channels:
        reasons = []
        if not ch.get("subscriber_count"):
            reasons.append("zero_subscribers")
        if ch.get("channel_id") in comment_author_only_ids:
            reasons.append("comment_author_only")
        ch = dict(ch)
        ch["low_confidence"] = bool(reasons)
        ch["low_confidence_reasons"] = reasons
        flagged.append(ch)
    return flagged


def fetch_run_branch_counts(run_id: str) -> dict[str, dict[str, int]]:
    """Per-branch coverage: how many channels/videos this run tagged under
    each tree node, from category_tags. Feeds the report's sub-niche
    breakdown — the same "how does this attribute per branch" question
    the taxonomy tree used to answer, without the full tree/UI machinery.
    """
    rows = _fetch(
        """
        SELECT tree_node_id, entity_type, COUNT(DISTINCT entity_id) AS n
        FROM category_tags
        WHERE run_id = %s
        GROUP BY tree_node_id, entity_type
        """,
        (run_id,),
    )
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        node_counts = counts.setdefault(row["tree_node_id"], {"channels": 0, "videos": 0})
        if row["entity_type"] == "channel":
            node_counts["channels"] = row["n"]
        elif row["entity_type"] == "video":
            node_counts["videos"] = row["n"]
    return counts


def fetch_run_edges(run_id: str) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT source_channel_id, target_channel_id, target_channel_ref,
               edge_type, discovered_at
        FROM discovery_edges
        WHERE run_id = %s
        ORDER BY discovered_at
        """,
        (run_id,),
    )


_CATEGORY_LABELS = {
    "keyword": "Keyword only",
    "graph_walk": "Graph walk only",
    "both": "Both tracks",
    "unresolved": "Unresolved (frontier)",
}


def _ref_label(ref: str) -> str:
    """A human-readable label for a channel we only know by ref.

    ".../@handle" -> "@handle" (what a person actually recognises); the
    UC-id form has no readable content, so it's shown short-and-truncated
    rather than as a 24-character opaque string.
    """
    ref = (ref or "").rstrip("/")
    tail = ref.rsplit("/", 1)[-1] if ref else ""
    if tail.startswith("@"):
        return tail
    if tail.startswith("UC") and len(tail) > 14:
        return tail[:10] + "…"
    return tail or "(unknown)"


def _category(discovery_method: str | None) -> str:
    """Bucket a channel's attribution into one of the graph's four series.

    Only three are ever assigned a categorical hue (keyword / graph_walk /
    both) — a node-link layout is an all-pairs context (any two categories
    can end up adjacent anywhere on screen), and the reference palette's
    all-pairs floor only clears for its first three slots. Everything else
    — genuinely unattributed channels, and frontier refs never fetched at
    all — folds into one neutral "unresolved" bucket rather than claiming a
    fourth hue the palette doesn't clear for this chart form.
    """
    return discovery_method if discovery_method in ("keyword", "graph_walk", "both") else "unresolved"


def build_graph_payload(
    channels: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    max_nodes: int = 0,
) -> dict[str, Any]:
    """Nodes + edges for the interactive graph, keyed consistently.

    Every hydrated channel becomes a node. Every edge's target that never
    resolved to a fetched channel — a frontier ref the walk saw but did not
    expand — ALSO becomes a node, keyed by its ref rather than an id it
    doesn't have. Without this the graph would only show the ~28% of the
    discovered network that got hydrated and silently drop the rest, when
    the unhydrated majority is itself the finding: it's the frontier the
    record budget ran out before reaching.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for ch in channels:
        cid = ch.get("channel_id", "")
        if not cid:
            continue
        nodes[cid] = {
            "id": cid,
            "label": ch.get("title") or cid,
            "subscribers": ch.get("subscriber_count") or 0,
            "category": _category(ch.get("discovery_method")),
            "resolved": True,
        }

    norm_edges: list[dict[str, Any]] = []
    degree: dict[str, int] = {}
    for e in edges:
        source = e.get("source_channel_id", "")
        if not source:
            continue
        if source not in nodes:
            # An edge's source should have been hydrated (it was expanded to
            # produce the edge), but a partial/interrupted run can leave one
            # dangling — represent it rather than dropping the edge.
            nodes[source] = {
                "id": source, "label": source, "subscribers": 0,
                "category": "unresolved", "resolved": False,
            }

        target_id = e.get("target_channel_id")
        target_ref = e.get("target_channel_ref", "")
        if target_id and target_id in nodes:
            target = target_id
        elif target_ref:
            target = target_ref
            if target not in nodes:
                nodes[target] = {
                    "id": target, "label": _ref_label(target_ref),
                    "subscribers": 0, "category": "unresolved", "resolved": False,
                }
        else:
            continue

        norm_edges.append({
            "source": source, "target": target,
            "edge_type": e.get("edge_type", ""),
        })
        degree[source] = degree.get(source, 0) + 1
        degree[target] = degree.get(target, 0) + 1

    node_list = list(nodes.values())
    if max_nodes > 0 and len(node_list) > max_nodes:
        # Trim unresolved nodes first, lowest-degree first — a frontier node
        # with a single edge and no metadata adds the least to the picture.
        # A hydrated channel is never dropped by this cap.
        resolved = [n for n in node_list if n["resolved"]]
        unresolved = sorted(
            (n for n in node_list if not n["resolved"]),
            key=lambda n: degree.get(n["id"], 0),
            reverse=True,
        )
        keep = set(n["id"] for n in resolved) | {
            n["id"] for n in unresolved[: max(0, max_nodes - len(resolved))]
        }
        node_list = [n for n in node_list if n["id"] in keep]
        norm_edges = [
            e for e in norm_edges if e["source"] in keep and e["target"] in keep
        ]

    counts: dict[str, int] = {}
    for n in node_list:
        counts[n["category"]] = counts.get(n["category"], 0) + 1

    return {
        "nodes": node_list,
        "edges": norm_edges,
        "category_counts": counts,
        "truncated": len(nodes) > len(node_list),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _clean_title(title: str | None) -> str:
    """Some real YouTube channel titles contain embedded newlines — left
    raw, one breaks a markdown list item into a stray blank line and a
    dangling continuation. Collapse to a single line."""
    return " ".join((title or "").split())


def _report_markdown(
    report: dict[str, Any],
    manifest: dict[str, Any],
    channels: list[dict],
    branch_counts: dict[str, dict[str, int]] | None = None,
    tree: dict[str, dict] | None = None,
) -> str:
    """A brief for the team deciding what to make — statistics, findings,
    what to do, what to avoid, top channels. Nothing about how the run was
    conducted, what it stopped on, or which channels the pipeline is less
    sure about: that provenance detail lives in manifest.json and
    channels.csv's low_confidence_reasons column for anyone who wants it,
    not in the document a content strategist reads.
    """
    grade_order = {"strong": 0, "moderate": 1, "weak": 2}
    findings = sorted(
        report.get("findings", []),
        key=lambda f: grade_order.get(f.get("grade", "weak"), 3),
    )
    niche = report.get("niche") or manifest.get("niche") or "Untitled"

    lines = [
        f"# {niche} — research report",
        "",
        f"{manifest['channels']:,} channels and {manifest['videos']:,} videos analyzed "
        f"across {manifest.get('sub_niches_covered', 0)} sub-niches.",
        "",
        "## Overview",
        "",
        "| Sub-niche | Channels | Videos |",
        "|---|---:|---:|",
    ]
    tree = tree or {}
    branch_counts = branch_counts or {}
    rows = sorted(
        (
            (n.get("label") or nid, n.get("depth", 0), branch_counts.get(nid, {}))
            for nid, n in tree.items()
            if branch_counts.get(nid, {}).get("channels")
        ),
        key=lambda r: (r[1], -r[2].get("channels", 0)),
    )
    for label, _depth, counts in rows:
        lines.append(f"| {label} | {counts.get('channels', 0):,} | {counts.get('videos', 0):,} |")
    lines.append("")

    lines += ["## Summary", "", report.get("summary", "_No summary._"), "", "## Findings", ""]
    for finding in findings:
        lines.append(f"### [{finding.get('grade', '?').upper()}] {finding.get('claim', '')}")
        supporting = finding.get("supporting_channel_ids", [])
        if supporting:
            by_id = {c["channel_id"]: _clean_title(c.get("title")) or c["channel_id"] for c in channels}
            named = [by_id.get(cid, cid) for cid in supporting[:6]]
            lines.append(f"Channels: {', '.join(named)}")
        lines.append("")

    recs = report.get("recommendations", {}) or {}
    do_items = recs.get("do", [])
    avoid_items = recs.get("avoid", [])
    if do_items:
        lines += ["## What to do", ""]
        lines += [f"- {item}" for item in do_items]
        lines.append("")
    if avoid_items:
        lines += ["## What to avoid", ""]
        lines += [f"- {item}" for item in avoid_items]
        lines.append("")

    lines += [
        "## Top channels by audience",
        "",
        "| Channel | Subscribers | Found by | Engagement | Uploads/30d |",
        "|---|---:|---|---:|---:|",
    ]
    for ch in channels[:20]:
        eng = ch.get("engagement_rate")
        cad = ch.get("cadence")
        lines.append(
            f"| {_clean_title(ch.get('title')) or ch['channel_id']} "
            f"| {ch.get('subscriber_count') or 0:,} "
            f"| {ch.get('discovery_method', '?')} "
            f"| {eng if eng is not None else '—'} "
            f"| {cad if cad is not None else '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


_GRAPH_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — discovery graph</title>
<style>
  :root {
    --surface:      #fcfcfb;
    --surface-2:    #f2f1ee;
    --ink:          #0b0b0b;
    --ink-2:        #52514e;
    --ink-3:        #8a887f;
    --rule:         #e3e1da;
    --edge-rgb:     11,11,11;
    --cat-keyword:    #2a78d6;
    --cat-graph_walk: #eb6834;
    --cat-both:       #1baf7a;
    --cat-unresolved: #9a9890;
    --shadow: 0 1px 2px rgba(11,11,11,.06), 0 8px 24px -12px rgba(11,11,11,.18);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --surface:      #1a1a19;
      --surface-2:    #232320;
      --ink:          #ffffff;
      --ink-2:        #c3c2b7;
      --ink-3:        #82817a;
      --rule:         #333230;
      --edge-rgb:     255,255,255;
      --cat-keyword:    #3987e5;
      --cat-graph_walk: #d95926;
      --cat-both:       #199e70;
      --cat-unresolved: #6f6e68;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6);
    }
  }
  :root[data-theme="dark"] {
    --surface:      #1a1a19;
    --surface-2:    #232320;
    --ink:          #ffffff;
    --ink-2:        #c3c2b7;
    --ink-3:        #82817a;
    --rule:         #333230;
    --edge-rgb:     255,255,255;
    --cat-keyword:    #3987e5;
    --cat-graph_walk: #d95926;
    --cat-both:       #199e70;
    --cat-unresolved: #6f6e68;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--surface);
    color: var(--ink);
    font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex;
    flex-direction: column;
  }
  header {
    padding: .875rem 1.25rem;
    border-bottom: 1px solid var(--rule);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: .5rem 1.5rem;
    background: var(--surface);
    z-index: 5;
  }
  .title-block { flex: 1 1 auto; min-width: 12rem; }
  h1 {
    margin: 0;
    font-size: 1.0625rem;
    font-weight: 650;
    letter-spacing: -.01em;
  }
  .byline {
    margin: .125rem 0 0;
    font-size: .75rem;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: .75rem 1.25rem;
  }
  .legend { display: flex; flex-wrap: wrap; gap: .625rem 1rem; }
  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: .375rem;
    font-size: .75rem;
    color: var(--ink-2);
    white-space: nowrap;
  }
  .dot {
    width: .625rem; height: .625rem; border-radius: 50%;
    flex: none;
    box-shadow: 0 0 0 1px rgba(var(--edge-rgb), .12) inset;
  }
  .dot.hollow { background: transparent; border: 1.5px dashed var(--cat-unresolved); }
  .field {
    display: inline-flex;
    align-items: center;
    gap: .375rem;
    font-size: .75rem;
    color: var(--ink-2);
  }
  input[type="search"] {
    font: inherit;
    font-size: .8125rem;
    padding: .3rem .55rem;
    border-radius: 5px;
    border: 1px solid var(--rule);
    background: var(--surface-2);
    color: var(--ink);
    width: 11rem;
  }
  input[type="search"]:focus-visible, button:focus-visible {
    outline: 2px solid var(--cat-keyword);
    outline-offset: 1px;
  }
  button {
    font: inherit;
    font-size: .75rem;
    padding: .35rem .65rem;
    border-radius: 5px;
    border: 1px solid var(--rule);
    background: var(--surface-2);
    color: var(--ink-2);
    cursor: pointer;
  }
  button[aria-pressed="true"] { color: var(--ink); border-color: var(--cat-keyword); }
  main { position: relative; flex: 1 1 auto; min-height: 0; }
  #canvas-wrap { position: absolute; inset: 0; }
  canvas { display: block; width: 100%; height: 100%; cursor: grab; }
  canvas.dragging { cursor: grabbing; }
  #tooltip {
    position: absolute;
    pointer-events: none;
    max-width: 15rem;
    padding: .5rem .625rem;
    border-radius: 6px;
    background: var(--surface);
    border: 1px solid var(--rule);
    box-shadow: var(--shadow);
    font-size: .75rem;
    color: var(--ink-2);
    opacity: 0;
    transform: translate(-9999px, -9999px);
  }
  #tooltip strong { display: block; color: var(--ink); font-size: .8125rem; margin-bottom: .125rem; }
  #tooltip .tt-row { display: flex; justify-content: space-between; gap: .75rem; }
  #empty-state {
    position: absolute; inset: 0;
    display: none;
    align-items: center; justify-content: center;
    color: var(--ink-3); font-size: .875rem;
  }
  #table-view {
    position: absolute; inset: 0;
    overflow: auto;
    background: var(--surface);
    display: none;
  }
  #table-view table { width: 100%; border-collapse: collapse; font-size: .8125rem; }
  #table-view th {
    position: sticky; top: 0;
    text-align: left;
    font-size: .6875rem; letter-spacing: .07em; text-transform: uppercase;
    color: var(--ink-3); font-weight: 500;
    padding: .5rem .75rem;
    border-bottom: 1px solid var(--rule);
    background: var(--surface);
    cursor: pointer;
    white-space: nowrap;
  }
  #table-view td {
    padding: .4rem .75rem;
    border-bottom: 1px solid var(--rule);
    color: var(--ink-2);
  }
  #table-view td:first-child { color: var(--ink); }
  .n { text-align: right; font-variant-numeric: tabular-nums; }
  #hint {
    position: absolute; left: 1rem; bottom: 1rem;
    font-size: .6875rem; color: var(--ink-3);
    background: var(--surface); padding: .25rem .5rem; border-radius: 4px;
    border: 1px solid var(--rule);
    pointer-events: none;
  }
</style>
</head>
<body>
  <header>
    <div class="title-block">
      <h1>__TITLE__ — discovery graph</h1>
      <p class="byline">__BYLINE__</p>
    </div>
    <div class="controls">
      <div class="legend" id="legend"></div>
      <label class="field"><input type="checkbox" id="toggle-unresolved" checked> Show frontier nodes</label>
      <input type="search" id="search" placeholder="Find a channel…" aria-label="Find a channel">
      <button id="btn-reset" type="button">Reset view</button>
      <button id="btn-table" type="button" aria-pressed="false">View as table</button>
      <button id="btn-theme" type="button" aria-pressed="false">Dark</button>
    </div>
  </header>
  <main>
    <div id="canvas-wrap">
      <canvas id="canvas"></canvas>
      <div id="empty-state">No nodes match the current filter.</div>
    </div>
    <div id="table-view">
      <table>
        <thead><tr>
          <th data-key="label">Channel</th>
          <th data-key="category">Found by</th>
          <th data-key="subscribers" class="n">Subscribers</th>
          <th data-key="resolved">Fetched</th>
        </tr></thead>
        <tbody id="table-body"></tbody>
      </table>
    </div>
    <div id="tooltip"></div>
    <div id="hint">Drag to move · scroll to zoom · click a node to focus its neighbours</div>
  </main>

<script type="application/json" id="graph-data">__GRAPH_JSON__</script>
<script>
(function () {
  "use strict";
  var payload = JSON.parse(document.getElementById("graph-data").textContent);
  var CATS = ["keyword", "graph_walk", "both", "unresolved"];
  var CAT_LABEL = {
    keyword: "Keyword only", graph_walk: "Graph walk only",
    both: "Both tracks", unresolved: "Unresolved (frontier)"
  };

  // ---- theme -------------------------------------------------------------
  var root = document.documentElement;
  var themeBtn = document.getElementById("btn-theme");
  function applyThemeButtonLabel() {
    var explicit = root.getAttribute("data-theme");
    themeBtn.textContent = explicit === "dark" ? "Light" : "Dark";
    themeBtn.setAttribute("aria-pressed", explicit === "dark" ? "true" : "false");
  }
  themeBtn.addEventListener("click", function () {
    var current = root.getAttribute("data-theme");
    if (current === "dark") { root.setAttribute("data-theme", "light"); }
    else if (current === "light") { root.removeAttribute("data-theme"); }
    else { root.setAttribute("data-theme", "dark"); }
    applyThemeButtonLabel();
    readTokens();
    render();
  });
  applyThemeButtonLabel();

  // ---- read CSS tokens (so canvas colors always match the live theme) ---
  var tok = {};
  function readTokens() {
    var s = getComputedStyle(document.body);
    ["--surface", "--ink", "--ink-2", "--ink-3", "--rule", "--edge-rgb",
     "--cat-keyword", "--cat-graph_walk", "--cat-both", "--cat-unresolved"
    ].forEach(function (k) { tok[k] = s.getPropertyValue(k).trim(); });
  }
  readTokens();
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      readTokens(); render();
    });
  }

  // ---- legend + filters ---------------------------------------------------
  var legend = document.getElementById("legend");
  CATS.forEach(function (cat) {
    var count = payload.category_counts[cat] || 0;
    var item = document.createElement("span");
    item.className = "legend-item";
    var dot = document.createElement("span");
    dot.className = "dot" + (cat === "unresolved" ? " hollow" : "");
    if (cat !== "unresolved") { dot.style.background = "var(--cat-" + cat + ")"; }
    item.appendChild(dot);
    item.appendChild(document.createTextNode(CAT_LABEL[cat] + " (" + count.toLocaleString() + ")"));
    legend.appendChild(item);
  });
  if (payload.truncated) {
    var note = document.createElement("span");
    note.className = "legend-item";
    note.style.color = "var(--ink-3)";
    note.textContent = "· lowest-connected frontier nodes trimmed for display";
    legend.appendChild(note);
  }

  // ---- graph data ----------------------------------------------------------
  var spread = 40 * Math.sqrt(Math.max(1, payload.nodes.length));
  var nodes = payload.nodes.map(function (n, i) {
    var a = (i * 2.399963) % (Math.PI * 2); // golden-angle spiral: no initial overlap
    var r = spread * Math.sqrt((i + 1) / payload.nodes.length);
    return {
      id: n.id, label: n.label, subscribers: n.subscribers || 0,
      category: n.category, resolved: n.resolved,
      x: Math.cos(a) * r, y: Math.sin(a) * r,
      vx: 0, vy: 0, fx: null, fy: null, hidden: false
    };
  });
  var byId = new Map(nodes.map(function (n) { return [n.id, n]; }));
  var edges = payload.edges.map(function (e) {
    return { source: byId.get(e.source), target: byId.get(e.target), edge_type: e.edge_type };
  }).filter(function (e) { return e.source && e.target; });

  var subMax = nodes.reduce(function (m, n) { return Math.max(m, n.subscribers); }, 1);
  function radius(n) {
    var v = Math.sqrt(n.subscribers / subMax);
    return 3 + v * 15;
  }

  var EDGE_LEN = {
    featured_channel: 42, recommendation: 68, playlist: 68,
    comment_author: 95, collaboration: 95, comment_mention: 95
  };
  var EDGE_STR = {
    featured_channel: 0.95, recommendation: 0.65, playlist: 0.65,
    comment_author: 0.35, collaboration: 0.35, comment_mention: 0.35
  };

  // ---- spatial-grid force simulation ---------------------------------------
  // O(n) repulsion via a uniform grid rather than O(n^2) all-pairs — this
  // graph runs to ~2,000 nodes, and all-pairs is not viable at that count.
  var CELL = 90, REPEL = 480, CENTER_K = 0.018, DAMPING = 0.82;
  var alpha = 1, alphaTarget = 0;
  var alphaDecay = 1 - Math.pow(0.001, 1 / 260);
  var draggedNode = null;

  function key(x, y) { return (Math.floor(x / CELL)) + "," + (Math.floor(y / CELL)); }

  function tick() {
    var active = nodes.filter(function (n) { return !n.hidden; });
    var grid = new Map();
    for (var i = 0; i < active.length; i++) {
      var n = active[i], k = key(n.x, n.y);
      var arr = grid.get(k);
      if (!arr) { arr = []; grid.set(k, arr); }
      arr.push(n);
    }
    var cutoff2 = (CELL * 1.6) * (CELL * 1.6);
    for (var j = 0; j < active.length; j++) {
      var node = active[j];
      if (node === draggedNode || node.fx !== null) continue;
      var fx = 0, fy = 0;
      var cx = Math.floor(node.x / CELL), cy = Math.floor(node.y / CELL);
      for (var dx = -1; dx <= 1; dx++) {
        for (var dy = -1; dy <= 1; dy++) {
          var bucket = grid.get((cx + dx) + "," + (cy + dy));
          if (!bucket) continue;
          for (var b = 0; b < bucket.length; b++) {
            var o = bucket[b];
            if (o === node) continue;
            var ddx = node.x - o.x, ddy = node.y - o.y;
            var d2 = ddx * ddx + ddy * ddy;
            if (d2 > cutoff2) continue;
            if (d2 < 4) { ddx = (Math.random() - 0.5); ddy = (Math.random() - 0.5); d2 = 4; }
            var d = Math.sqrt(d2);
            var f = REPEL / d2;
            fx += (ddx / d) * f; fy += (ddy / d) * f;
          }
        }
      }
      fx += -node.x * CENTER_K;
      fy += -node.y * CENTER_K;
      node.vx = (node.vx + fx * alpha) * DAMPING;
      node.vy = (node.vy + fy * alpha) * DAMPING;
    }
    for (var e = 0; e < edges.length; e++) {
      var edge = edges[e];
      if (edge.source.hidden || edge.target.hidden) continue;
      var strength = EDGE_STR[edge.edge_type] || 0.5;
      var targetLen = EDGE_LEN[edge.edge_type] || 85;
      var a = edge.source, bN = edge.target;
      var edx = bN.x - a.x, edy = bN.y - a.y;
      var ed = Math.sqrt(edx * edx + edy * edy) || 0.01;
      var diff = ((ed - targetLen) / ed) * strength * alpha;
      var mx = edx * diff * 0.5, my = edy * diff * 0.5;
      if (a !== draggedNode && a.fx === null) { a.vx += mx; a.vy += my; }
      if (bN !== draggedNode && bN.fx === null) { bN.vx -= mx; bN.vy -= my; }
    }
    for (var m = 0; m < active.length; m++) {
      var nn = active[m];
      if (nn === draggedNode || nn.fx !== null) continue;
      nn.x += nn.vx; nn.y += nn.vy;
    }
    alpha += (alphaTarget - alpha) * alphaDecay;
  }
  function wake(strength) { alpha = Math.max(alpha, strength == null ? 0.35 : strength); loop(); }

  // ---- canvas + transform ---------------------------------------------------
  var wrap = document.getElementById("canvas-wrap");
  var canvas = document.getElementById("canvas");
  var ctx = canvas.getContext("2d");
  var transform = { x: 0, y: 0, k: 1 };
  var dpr = Math.max(1, window.devicePixelRatio || 1);

  function resize() {
    var rect = wrap.getBoundingClientRect();
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    canvas.style.width = rect.width + "px";
    canvas.style.height = rect.height + "px";
    if (!transform.initialised) {
      transform.x = rect.width / 2; transform.y = rect.height / 2;
      transform.initialised = true;
    }
    render();
  }
  window.addEventListener("resize", resize);

  function toScreen(n) { return [n.x * transform.k + transform.x, n.y * transform.k + transform.y]; }
  function toWorld(sx, sy) { return [(sx - transform.x) / transform.k, (sy - transform.y) / transform.k]; }

  // ---- selection / hover state ------------------------------------------
  var hovered = null, focused = null, matched = new Set();

  function neighbourSet(n) {
    var s = new Set([n.id]);
    edges.forEach(function (e) {
      if (e.source === n) s.add(e.target.id);
      if (e.target === n) s.add(e.source.id);
    });
    return s;
  }

  function render() {
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, canvas.width / dpr, canvas.height / dpr);
    ctx.fillStyle = tok["--surface"];
    ctx.fillRect(0, 0, canvas.width / dpr, canvas.height / dpr);

    var dim = focused ? neighbourSet(focused) : (matched.size ? matched : null);

    ctx.lineWidth = 1;
    edges.forEach(function (e) {
      if (e.source.hidden || e.target.hidden) return;
      var a = toScreen(e.source), b = toScreen(e.target);
      var faded = dim && !(dim.has(e.source.id) && dim.has(e.target.id));
      ctx.strokeStyle = "rgba(" + tok["--edge-rgb"] + "," + (faded ? 0.05 : 0.16) + ")";
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    });

    nodes.forEach(function (n) {
      if (n.hidden) return;
      var p = toScreen(n);
      var r = radius(n) * transform.k;
      if (p[0] < -r - 20 || p[0] > canvas.width / dpr + r + 20) return;
      if (p[1] < -r - 20 || p[1] > canvas.height / dpr + r + 20) return;
      var faded = dim && !dim.has(n.id);
      var color = n.category === "unresolved" ? tok["--cat-unresolved"] : tok["--cat-" + n.category];
      ctx.globalAlpha = faded ? 0.22 : 1;
      ctx.beginPath();
      ctx.arc(p[0], p[1], Math.max(1.5, r), 0, Math.PI * 2);
      if (n.category === "unresolved") {
        ctx.fillStyle = tok["--surface"];
        ctx.fill();
        ctx.lineWidth = 1.3;
        ctx.setLineDash([2, 2]);
        ctx.strokeStyle = color;
        ctx.stroke();
        ctx.setLineDash([]);
      } else {
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = tok["--surface"];
        ctx.stroke();
      }
      if (n === hovered || n === focused || matched.has(n.id)) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = tok["--ink"];
        ctx.beginPath();
        ctx.arc(p[0], p[1], Math.max(1.5, r) + 2, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    });

    // direct label for hovered/focused/matched — selective, not every node
    var labelTargets = [];
    if (hovered) labelTargets.push(hovered);
    if (focused) labelTargets.push(focused);
    if (matched.size && matched.size <= 40) {
      nodes.forEach(function (n) { if (matched.has(n.id)) labelTargets.push(n); });
    }
    ctx.font = "600 12px ui-sans-serif, system-ui, sans-serif";
    ctx.textBaseline = "middle";
    labelTargets.forEach(function (n) {
      var p = toScreen(n);
      var r = radius(n) * transform.k;
      var text = n.label;
      var tw = ctx.measureText(text).width;
      ctx.fillStyle = "rgba(" + (tok["--surface"] === "#1a1a19" ? "26,26,25" : "252,252,251") + ",.92)";
      ctx.fillRect(p[0] + r + 5, p[1] - 9, tw + 8, 18);
      ctx.fillStyle = tok["--ink"];
      ctx.fillText(text, p[0] + r + 9, p[1] + 1);
    });

    ctx.restore();
  }

  var rafRunning = false;
  function loop() {
    if (rafRunning) return;
    rafRunning = true;
    function frame() {
      tick(); render();
      if (alpha > 0.001 || draggedNode) {
        requestAnimationFrame(frame);
      } else {
        rafRunning = false;
      }
    }
    requestAnimationFrame(frame);
  }

  // ---- interaction: drag / pan / zoom -------------------------------------
  var panning = false, panStart = null, dragMoved = false;
  var DRAG_THRESHOLD = 4;

  function nodeAt(sx, sy) {
    var w = toWorld(sx, sy);
    var best = null, bestD = Infinity;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.hidden) continue;
      var dx = w[0] - n.x, dy = w[1] - n.y;
      var d = Math.sqrt(dx * dx + dy * dy);
      var hit = radius(n) + 4 / transform.k;
      if (d < hit && d < bestD) { best = n; bestD = d; }
    }
    return best;
  }

  canvas.addEventListener("mousedown", function (ev) {
    var rect = canvas.getBoundingClientRect();
    var sx = ev.clientX - rect.left, sy = ev.clientY - rect.top;
    var n = nodeAt(sx, sy);
    dragMoved = false;
    if (n) {
      draggedNode = n; canvas.classList.add("dragging");
      panStart = { sx: sx, sy: sy };
    } else {
      panning = true; canvas.classList.add("dragging");
      panStart = { sx: sx, sy: sy, tx: transform.x, ty: transform.y };
    }
  });
  window.addEventListener("mousemove", function (ev) {
    var rect = canvas.getBoundingClientRect();
    var sx = ev.clientX - rect.left, sy = ev.clientY - rect.top;
    if (draggedNode) {
      if (Math.abs(sx - panStart.sx) > DRAG_THRESHOLD || Math.abs(sy - panStart.sy) > DRAG_THRESHOLD) dragMoved = true;
      var w = toWorld(sx, sy);
      draggedNode.fx = w[0]; draggedNode.fy = w[1];
      draggedNode.x = w[0]; draggedNode.y = w[1];
      wake(0.4);
    } else if (panning) {
      transform.x = panStart.tx + (sx - panStart.sx);
      transform.y = panStart.ty + (sy - panStart.sy);
      render();
    } else if (sx >= 0 && sy >= 0 && sx <= rect.width && sy <= rect.height) {
      var hit = nodeAt(sx, sy);
      if (hit !== hovered) {
        hovered = hit;
        showTooltip(hit, ev.clientX, ev.clientY);
        render();
      } else if (hit) {
        positionTooltip(ev.clientX, ev.clientY);
      }
    }
  });
  window.addEventListener("mouseup", function () {
    if (draggedNode) {
      var n = draggedNode;
      if (!dragMoved) {
        // a click, not a drag: toggle focus on this node's ego-network
        focused = focused === n ? null : n;
        matched.clear(); searchInput.value = "";
        n.fx = null; n.fy = null;
      } else {
        n.fx = null; n.fy = null;
      }
      draggedNode = null;
      wake(0.3);
      render();
    } else if (panning && !dragMoved) {
      focused = null;
      render();
    }
    panning = false;
    canvas.classList.remove("dragging");
  });
  canvas.addEventListener("mouseleave", function () { hovered = null; hideTooltip(); render(); });

  canvas.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    var w = toWorld(mx, my);
    var delta = -ev.deltaY * 0.0016;
    var newK = Math.min(9, Math.max(0.06, transform.k * (1 + delta)));
    transform.x = mx - w[0] * newK;
    transform.y = my - w[1] * newK;
    transform.k = newK;
    render();
  }, { passive: false });

  // ---- tooltip --------------------------------------------------------------
  var tooltip = document.getElementById("tooltip");
  function showTooltip(n, cx, cy) {
    if (!n) { hideTooltip(); return; }
    tooltip.innerHTML =
      "<strong>" + escapeHtml(n.label) + "</strong>" +
      "<div class=\"tt-row\"><span>Subscribers</span><span>" + n.subscribers.toLocaleString() + "</span></div>" +
      "<div class=\"tt-row\"><span>Found by</span><span>" + CAT_LABEL[n.category] + "</span></div>" +
      "<div class=\"tt-row\"><span>Fetched</span><span>" + (n.resolved ? "yes" : "no — frontier only") + "</span></div>";
    tooltip.style.opacity = 1;
    positionTooltip(cx, cy);
  }
  function positionTooltip(cx, cy) {
    var wrapRect = wrap.getBoundingClientRect();
    tooltip.style.transform = "translate(" + (cx - wrapRect.left + 14) + "px," + (cy - wrapRect.top + 14) + "px)";
  }
  function hideTooltip() { tooltip.style.opacity = 0; tooltip.style.transform = "translate(-9999px,-9999px)"; }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- filters: hide frontier nodes ---------------------------------------
  var toggleUnresolved = document.getElementById("toggle-unresolved");
  var emptyState = document.getElementById("empty-state");
  function applyVisibility() {
    var showUnresolved = toggleUnresolved.checked;
    var visibleCount = 0;
    nodes.forEach(function (n) {
      n.hidden = (!showUnresolved && n.category === "unresolved");
      if (!n.hidden) visibleCount++;
    });
    emptyState.style.display = visibleCount === 0 ? "flex" : "none";
    wake(0.5);
    render();
  }
  toggleUnresolved.addEventListener("change", applyVisibility);

  // ---- search -----------------------------------------------------------
  var searchInput = document.getElementById("search");
  searchInput.addEventListener("input", function () {
    var q = searchInput.value.trim().toLowerCase();
    matched.clear();
    focused = null;
    if (q.length >= 2) {
      nodes.forEach(function (n) {
        if (!n.hidden && n.label.toLowerCase().indexOf(q) !== -1) matched.add(n.id);
      });
    }
    render();
  });

  // ---- reset view ---------------------------------------------------------
  document.getElementById("btn-reset").addEventListener("click", function () {
    var rect = wrap.getBoundingClientRect();
    transform.x = rect.width / 2; transform.y = rect.height / 2; transform.k = 1;
    focused = null; matched.clear(); searchInput.value = "";
    render();
  });

  // ---- table view ---------------------------------------------------------
  var tableBtn = document.getElementById("btn-table");
  var tableView = document.getElementById("table-view");
  var tableBody = document.getElementById("table-body");
  var tableSort = { key: "subscribers", dir: -1 };
  function renderTable() {
    var rows = payload.nodes.slice().sort(function (a, b) {
      var k = tableSort.key, dir = tableSort.dir;
      var av = a[k], bv = b[k];
      if (typeof av === "string") { av = av.toLowerCase(); bv = (bv || "").toLowerCase(); }
      return av < bv ? -1 * dir : av > bv ? 1 * dir : 0;
    });
    tableBody.innerHTML = rows.map(function (n) {
      return "<tr><td>" + escapeHtml(n.label) + "</td><td>" + CAT_LABEL[n.category] +
        "</td><td class=\"n\">" + n.subscribers.toLocaleString() + "</td><td>" +
        (n.resolved ? "yes" : "no") + "</td></tr>";
    }).join("");
  }
  document.querySelectorAll("#table-view th").forEach(function (th) {
    th.addEventListener("click", function () {
      var k = th.getAttribute("data-key");
      tableSort.dir = tableSort.key === k ? -tableSort.dir : -1;
      tableSort.key = k;
      renderTable();
    });
  });
  tableBtn.addEventListener("click", function () {
    var showing = tableBtn.getAttribute("aria-pressed") === "true";
    tableBtn.setAttribute("aria-pressed", showing ? "false" : "true");
    tableBtn.textContent = showing ? "View as table" : "View as graph";
    if (!showing) { renderTable(); tableView.style.display = "block"; }
    else { tableView.style.display = "none"; }
  });

  resize();
  applyVisibility();
  wake(1);
})();
</script>
</body>
</html>
"""


def _interactive_graph_html(payload: dict[str, Any], manifest: dict[str, Any]) -> str:
    niche = manifest.get("niche") or "Untitled"
    n_nodes = len(payload.get("nodes", []))
    n_edges = len(payload.get("edges", []))
    byline = (
        f"{n_nodes:,} nodes · {n_edges:,} edges · run {manifest.get('run_id', '')} · "
        f"generated {manifest.get('exported_at', '')[:10]}"
    )
    graph_json = json.dumps(payload, default=str).replace("</", "<\\/")
    html = _GRAPH_HTML_TEMPLATE
    html = html.replace("__TITLE__", niche)
    html = html.replace("__BYLINE__", byline)
    html = html.replace("__GRAPH_JSON__", graph_json)
    return html


def export_run(run_id: str, thread_id: str | None = None) -> Path:
    """Write one run's deliverable. Returns the directory."""
    from src.api import runs as runs_mod

    cfg = get_config().harness
    out = export_dir(run_id)

    if thread_id is None:
        for entry in runs_mod.load_registry():
            if entry.get("run_id") == run_id:
                thread_id = entry.get("thread_id")
                break
    if thread_id is None:
        # CLI runs are not in the registry — only console-launched ones are.
        # Their NodeLog stream carries the thread_id on every line, which is
        # how list_runs recovers them, so fall back to that rather than
        # exporting a manifest full of zeros.
        for entry in runs_mod.read_run_log(run_id):
            if entry.get("thread_id"):
                thread_id = entry["thread_id"]
                break
    if thread_id is None:
        logger.warning("export_no_thread_id", run_id=run_id)

    report: dict[str, Any] = {}
    state: dict[str, Any] = {}
    if thread_id:
        try:
            from src.api.server import _load_checkpoint

            state = _load_checkpoint(thread_id) or {}
            report = state.get("final_report") or {}
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.warning("export_checkpoint_unavailable", run_id=run_id, error=str(exc))

    channels = fetch_run_channels(run_id)
    comment_author_only_ids = fetch_run_comment_author_only_channel_ids(run_id)
    channels = flag_low_confidence_channels(channels, comment_author_only_ids)
    videos = fetch_run_videos(run_id, cfg.export_max_videos)
    edges = fetch_run_edges(run_id)
    branch_counts = fetch_run_branch_counts(run_id)

    low_confidence_counts = {
        "zero_subscribers": sum(1 for c in channels if "zero_subscribers" in c["low_confidence_reasons"]),
        "comment_author_only": sum(1 for c in channels if "comment_author_only" in c["low_confidence_reasons"]),
        "total_flagged": sum(1 for c in channels if c["low_confidence"]),
    }

    tree = state.get("tree", {}) or {}
    manifest = {
        "run_id": run_id,
        "thread_id": thread_id,
        "niche": report.get("niche") or state.get("selected_niche", ""),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile": cfg.profile,
        "channels": len(channels),
        "videos": len(videos),
        "discovery_edges": len(edges),
        "sub_niches_covered": sum(1 for c in branch_counts.values() if c.get("channels")),
        "attribution": _attribution_counts(channels),
        # Two contamination sources a report should discount, not treat as
        # findings: zero-subscriber channels keyword search happened to
        # return (no other discovery edge points at them — likely dead,
        # spam, or placeholder), and channels whose only discovery edge is
        # someone commenting on a video in the dataset (real channel, real
        # subscriber count, but no evidence it's actually a finance
        # channel). See low_confidence_reasons on each channel record.
        "low_confidence_channels": low_confidence_counts,
        "spend": {
            "brightdata_records": state.get("brightdata_records_used", 0),
            "brightdata_usd": round(
                state.get("brightdata_records_used", 0)
                * cfg.brightdata_cost_per_record_usd, 4
            ),
            "total_usd": round(state.get("budget_spent_usd", 0.0), 4),
            "youtube_quota": state.get("youtube_quota_used", 0),
        },
        # The honest caveat. Every run so far stopped on a governor with
        # novelty still high; an export that omits this reads as a complete
        # picture of the niche when it is a survey of what was reached.
        "stop_reasons": sorted(
            {n.get("saturation_reason") for n in tree.values() if n.get("saturation_reason")}
        ),
        "saturated_branches": state.get("saturated_branches", []),
    }

    graph_payload = build_graph_payload(channels, edges, max_nodes=cfg.export_max_graph_nodes)

    # CSV wants a flat cell, not a Python list repr — join for the sheet,
    # keep the real list in research_bundle.json/discovery_graph.json.
    channels_csv_rows = [
        {**c, "low_confidence_reasons": "; ".join(c["low_confidence_reasons"])}
        for c in channels
    ]
    _write_csv(out / "channels.csv", channels_csv_rows)
    _write_csv(out / "outlier_videos.csv", videos)
    (out / "discovery_graph.json").write_text(
        json.dumps(graph_payload, indent=2, default=str), encoding="utf-8"
    )
    (out / "discovery_graph.html").write_text(
        _interactive_graph_html(graph_payload, manifest), encoding="utf-8"
    )
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )
    (out / "research_bundle.json").write_text(
        json.dumps(
            {"manifest": manifest, "report": report,
             "channels": channels, "videos": videos, "edges": edges},
            indent=2, default=str,
        ),
        encoding="utf-8",
    )
    if report:
        (out / "report.md").write_text(
            _report_markdown(report, manifest, channels, branch_counts, tree),
            encoding="utf-8",
        )

    logger.info(
        "run_exported", run_id=run_id, path=str(out),
        channels=len(channels), videos=len(videos), edges=len(edges),
    )
    return out


def _attribution_counts(channels: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ch in channels:
        key = ch.get("discovery_method") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


# === v3 Excel deliverable ==================================================
#
# The client does not want a narrative report — they want a workbook they can
# filter, sort, and search themselves, and later build charts from. Every
# data sheet below gets a bold frozen header, per-column widths, and a native
# AutoFilter over the full used range so every column has a dropdown out of
# the box; that's not cosmetic, it's the literal requirement ("can filter the
# data columns, can sort, can search").

_EXCEL_COLUMN_WIDTHS: dict[str, int] = {
    "channel_id": 26, "video_id": 26, "title": 42, "channel_title": 32,
    "description": 50, "niche_description": 42, "video_description": 46,
    "subscriber_count": 16, "view_count": 14, "like_count": 12,
    "comment_count": 14, "outlier_score": 14, "discovery_method": 16,
    "Channel Creation Date": 22, "first_video_published_at": 22, "last_video_published_at": 22,
    "total_video_count": 18,
    "total_long_video_count": 22, "total_shorts_count": 18,
    "total_live_stream_count": 22,
    "published_at": 20,
    "country_code": 12, "region": 16,
    "primary_language_code": 14, "language_confidence": 12,
    "face_status": 12, "dominant_format": 20,
    "category": 16, "sub_niche": 26,
    "entertainment_score": 14, "evergreen_score": 14, "engagement_score": 14,
    "is_likely_news": 12, "uploads_per_week_avg": 16, "upload_consistency_score": 16,
    "has_affiliate_signal": 14, "has_sponsor_signal": 14, "has_membership_signal": 16,
    "data_completeness_score": 16, "missing_required_fields": 30,
    "duration_seconds": 14, "is_short": 10, "language_code": 12,
    "views_per_day_since_publish": 20, "days_since_published": 18, "title_word_count": 14,
    "title_has_number": 14, "title_is_question": 14, "title_capitalization": 16,
    "title_emoji_count": 14,
    "niche_id": 10, "channel_count": 12, "is_evergreen_prone": 14,
    "factor_code": 26, "factor_label": 30, "factor_group": 14,
    "evidence_grade": 14, "evidence_note": 46, "corroboration_count": 14,
}


def _excel_safe(value: Any) -> Any:
    """openpyxl rejects tz-aware datetimes outright (Excel has no timezone
    type) — Postgres TIMESTAMPTZ columns come back as tz-aware datetime
    objects via psycopg, so every date column here needs this. It also
    raises IllegalCharacterError on control characters in strings — real
    YouTube channel descriptions carry them. Lists (missing_required_fields,
    tags) get joined for a flat cell.
    """
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    if isinstance(value, list):
        value = "; ".join(str(v) for v in value)
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _write_excel_sheet(ws, rows: list[dict[str, Any]]) -> None:
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if not rows:
        ws.append(["(no rows)"])
        return
    headers = list(rows[0].keys())
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for row in rows:
        ws.append([_excel_safe(row.get(h)) for h in headers])
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = _EXCEL_COLUMN_WIDTHS.get(h, 16)
    # Native filter/sort dropdowns on every column — the client's explicit
    # ask ("can filter the data columns, can sort, can search").
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"


def build_excel_workbook_v3(
    manifest: dict[str, Any],
    channels: list[dict[str, Any]],
    videos: list[dict[str, Any]],
    niches: list[dict[str, Any]],
    success_factors: list[dict[str, Any]],
    failure_factors: list[dict[str, Any]],
):
    """The client deliverable: one workbook, six sheets, nothing narrative.

    Overview (run stats + category/niche rollup), Channels (full v3
    enrichment set), Videos (full title/thumbnail signal set), Niches
    (category/sub-niche breakdown), Success Factors, Failure Factors — each
    a filterable/sortable table a non-technical owner can explore directly,
    and a flat enough shape to pivot or chart later.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()

    ws = wb.active
    ws.title = "Overview"
    ws.append([manifest.get("niche", "Untitled"), "research data export"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["Channels", manifest.get("channels", 0)])
    ws.append(["Videos", manifest.get("videos", 0)])
    ws.append(["Niches covered", manifest.get("niches_covered", 0)])
    ws.append(["Total cost (USD)", manifest.get("total_cost_usd", 0)])
    ws.append(["Generated", manifest.get("exported_at", "")[:19]])
    ws.append([])
    if niches:
        ws.append(["Category", "Sub-niche", "Channels"])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
        for n in sorted(niches, key=lambda r: -r.get("channel_count", 0)):
            ws.append([n.get("category", ""), n.get("sub_niche", ""), n.get("channel_count", 0)])
    for col, width in (("A", 28), ("B", 28), ("C", 14)):
        ws.column_dimensions[col].width = width

    _write_excel_sheet(wb.create_sheet("Channels"), channels)
    _write_excel_sheet(wb.create_sheet("Videos"), videos)
    _write_excel_sheet(wb.create_sheet("Niches"), niches)
    _write_excel_sheet(wb.create_sheet("Success Factors"), success_factors)
    _write_excel_sheet(wb.create_sheet("Failure Factors"), failure_factors)

    return wb


def dominant_run_category(run_id: str) -> str | None:
    """The parent_category most of this run's qualifying channels landed in.

    A run seeded on one topic still discovers off-topic channels, and the
    classifier labels them honestly — so "the category this run is about"
    is a fact about the data, readable directly, rather than something the
    caller has to remember and pass in.
    """
    rows = _fetch(
        f"""
        SELECT nt.parent_category, COUNT(*) AS n
        FROM channels c
        JOIN category_tags t
          ON t.entity_id = c.channel_id AND t.entity_type = 'channel'
        JOIN niche_taxonomy nt ON nt.niche_id = c.primary_niche_id
        WHERE t.run_id = %s AND c.subscriber_count >= {_floor()}
        GROUP BY nt.parent_category
        ORDER BY n DESC
        LIMIT 1
        """,
        (run_id,),
    )
    return rows[0]["parent_category"] if rows else None


def export_excel(
    run_id: str,
    out_path: Path | None = None,
    category: str | None = None,
    all_categories: bool = False,
    all_channels: bool = False,
    cap_videos: bool = False,
) -> Path:
    """Write the full v3 rich workbook for a run. This is the client
    deliverable — filterable/sortable Excel, no AI-generated narrative.

    Unlike v1's report-driven export, this reads nothing from a LangGraph
    checkpoint: every column comes straight from the store, so it works
    for any completed run regardless of whether its thread checkpoint is
    still around.

    Scoped by default to channels over the subscriber floor and in the
    run's dominant category — both client requirements. Pass an explicit
    `category` to override the auto-detected one, or all_categories=True
    to keep every discovered channel regardless of niche.

    all_channels=True additionally drops the subscriber floor itself —
    every discovered channel regardless of subscriber count. Implies
    all_categories too: a channel kept in purely because the floor no
    longer excludes it would otherwise still get dropped by the category
    filter. This is NOT the default for the big-run deliverable — the
    client's 50k floor and category scoping stand; only the row CAP on
    the Videos sheet is what the big runs asked to drop.

    cap_videos controls that separately: whether the Videos sheet is
    truncated to export_max_videos (top-N by outlier score, "the
    actionable slice, not the dump") or includes every video that
    qualifies under whatever channel scope was already applied above.
    Scope (floor/category) and row cap are independent — dropping one
    must never silently drop the other.
    """
    if out_path is None:
        out_path = export_dir(run_id) / f"{run_id}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if all_channels:
        all_categories = True
    if all_categories:
        category = None
    elif category is None:
        category = dominant_run_category(run_id)

    min_subscribers = 0 if all_channels else None
    video_limit = get_config().harness.export_max_videos if cap_videos else None

    channels = fetch_run_channels(run_id, category, min_subscribers)
    videos = fetch_run_videos(run_id, video_limit, category, min_subscribers)
    niches = fetch_run_niche_breakdown(run_id, category, min_subscribers)
    success_factors = fetch_run_success_factors(run_id, category, min_subscribers)
    failure_factors = fetch_run_failure_factors(run_id, category, min_subscribers)

    total_cost = 0.0
    try:
        rows = _fetch("SELECT total_cost_usd FROM harness_runs WHERE run_id = %s", (run_id,))
        if rows:
            total_cost = float(rows[0].get("total_cost_usd") or 0.0)
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("export_excel_run_cost_unavailable", run_id=run_id, error=str(exc))

    manifest = {
        "run_id": run_id,
        "niche": ", ".join(sorted({n["category"] for n in niches})) or run_id,
        "channels": len(channels),
        "videos": len(videos),
        "niches_covered": len(niches),
        "total_cost_usd": round(total_cost, 4),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }

    wb = build_excel_workbook_v3(manifest, channels, videos, niches, success_factors, failure_factors)
    wb.save(out_path)
    logger.info(
        "run_exported_excel", run_id=run_id, path=str(out_path),
        channels=len(channels), videos=len(videos),
    )
    return out_path


def main() -> None:
    import argparse

    from src.observability.logging_config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Export a run's research output")
    parser.add_argument("run_id")
    parser.add_argument("--thread-id", default=None)
    parser.add_argument(
        "--excel", action="store_true",
        help="Also write the v3 rich Excel workbook (channels/videos/niches/factors, filterable).",
    )
    parser.add_argument(
        "--all-channels", action="store_true",
        help="Every discovered channel regardless of subscriber count, and "
             "no category filter. The 50k floor and category scoping stay "
             "on by default — this drops them too.",
    )
    parser.add_argument(
        "--cap-videos", action="store_true",
        help="Truncate the Videos sheet to the top export_max_videos by "
             "outlier score. Off by default: every qualifying video ships.",
    )
    args = parser.parse_args()

    path = export_run(args.run_id, args.thread_id)
    print(f"Exported to {path}")
    for f in sorted(path.iterdir()):
        print(f"  {f.name}  ({f.stat().st_size:,} bytes)")

    if args.excel:
        xlsx_path = export_excel(
            args.run_id, path / f"{args.run_id}.xlsx",
            all_channels=args.all_channels, cap_videos=args.cap_videos,
        )
        print(f"Excel workbook: {xlsx_path}  ({xlsx_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

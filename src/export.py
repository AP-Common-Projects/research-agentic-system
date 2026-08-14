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


def fetch_run_channels(run_id: str) -> list[dict[str, Any]]:
    """Channels tagged to this run, with their computed signals flattened."""
    return _fetch(
        """
        SELECT DISTINCT c.channel_id, c.title, c.subscriber_count,
               c.discovery_method, c.first_seen_at, c.description,
               (c.extra->>'engagement_rate')::float AS engagement_rate,
               (c.extra->>'cadence')::float        AS cadence,
               (c.extra->>'velocity')::float       AS velocity
        FROM channels c
        JOIN category_tags t
          ON t.entity_id = c.channel_id AND t.entity_type = 'channel'
        WHERE t.run_id = %s
        ORDER BY c.subscriber_count DESC NULLS LAST
        """,
        (run_id,),
    )


def fetch_run_videos(run_id: str, limit: int) -> list[dict[str, Any]]:
    """This run's videos, highest outlier score first.

    Ordered by outlier score rather than views because the score is the point:
    a 200k-view video on a 5M-subscriber channel is unremarkable, while the
    same on a 20k channel is the signal the team is looking for.
    """
    return _fetch(
        """
        SELECT DISTINCT v.video_id, v.channel_id, c.title AS channel_title,
               v.title, v.view_count, v.like_count, v.comment_count,
               v.published_at, v.outlier_score
        FROM videos v
        JOIN category_tags t
          ON t.entity_id = v.video_id AND t.entity_type = 'video'
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE t.run_id = %s
        ORDER BY v.outlier_score DESC NULLS LAST
        LIMIT %s
        """,
        (run_id, limit),
    )


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


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _report_markdown(
    report: dict[str, Any], manifest: dict[str, Any], channels: list[dict]
) -> str:
    grade_order = {"strong": 0, "moderate": 1, "weak": 2}
    findings = sorted(
        report.get("findings", []),
        key=lambda f: grade_order.get(f.get("grade", "weak"), 3),
    )

    lines = [
        f"# {report.get('niche', 'Untitled')} — niche research",
        "",
        f"Run `{manifest['run_id']}` · generated {manifest['exported_at'][:19]}Z",
        "",
        "## How to read this",
        "",
        f"{manifest['channels']} channels and {manifest['videos']} videos were "
        f"discovered and scored. Findings are graded **strong / moderate / weak** "
        "by a deterministic four-axis check — corroboration, consistency, "
        "recency, effect size — not by how confident the writing sounds.",
        "",
    ]

    stop = manifest.get("stop_reasons", [])
    if stop and not all(s == "novelty_below_threshold" for s in stop):
        lines += [
            "> **This run did not reach saturation.** It stopped on "
            f"`{', '.join(sorted(set(stop)))}`, meaning the budget or round "
            "ceiling ended it while new channels were still being found. Treat "
            "this as a survey of what was reached, not a complete picture of "
            "the niche.",
            "",
        ]

    lines += ["## Summary", "", report.get("summary", "_No summary._"), "", "## Findings", ""]
    for finding in findings:
        lines.append(f"### [{finding.get('grade', '?').upper()}] {finding.get('claim', '')}")
        evidence = finding.get("evidence", {})
        if evidence:
            lines.append(
                f"*Corroboration: {evidence.get('corroboration', '?')} · "
                f"consistency: {evidence.get('consistency', '?')} · "
                f"recency: {evidence.get('recency', '?')} · "
                f"effect size: {evidence.get('effect_size', '?')}*"
            )
        supporting = finding.get("supporting_channel_ids", [])
        if supporting:
            by_id = {c["channel_id"]: c.get("title") or c["channel_id"] for c in channels}
            named = [by_id.get(cid, cid) for cid in supporting[:6]]
            lines.append(f"Channels: {', '.join(named)}")
        lines.append("")

    cannot = report.get("cannot_determine", [])
    if cannot:
        lines += ["## What this data cannot tell you", ""]
        lines += [f"- {item}" for item in cannot]
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
            f"| {ch.get('title') or ch['channel_id']} "
            f"| {ch.get('subscriber_count') or 0:,} "
            f"| {ch.get('discovery_method', '?')} "
            f"| {eng if eng is not None else '—'} "
            f"| {cad if cad is not None else '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


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
    videos = fetch_run_videos(run_id, cfg.export_max_videos)
    edges = fetch_run_edges(run_id)

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
        "attribution": _attribution_counts(channels),
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

    _write_csv(out / "channels.csv", channels)
    _write_csv(out / "outlier_videos.csv", videos)
    (out / "discovery_graph.json").write_text(
        json.dumps({"edges": edges}, indent=2, default=str), encoding="utf-8"
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
            _report_markdown(report, manifest, channels), encoding="utf-8"
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


def main() -> None:
    import argparse

    from src.observability.logging_config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Export a run's research output")
    parser.add_argument("run_id")
    parser.add_argument("--thread-id", default=None)
    args = parser.parse_args()

    path = export_run(args.run_id, args.thread_id)
    print(f"Exported to {path}")
    for f in sorted(path.iterdir()):
        print(f"  {f.name}  ({f.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

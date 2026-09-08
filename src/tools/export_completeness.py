"""The gate a workbook has to pass before anyone is handed it.

Every empty sheet and blank column this project has shipped came from the
same shape of failure: a node did not run, or ran against the wrong
channels, and nothing between it and the client noticed. The run reported
success, the export wrote whatever was in the table, and the gap was found
by a person opening the file.

So the export stops being the last step. This is:

    audit  ->  fill what is fillable  ->  audit again  ->  report

Columns with a node behind them are declared here with that node and the
fill rate below which they count as broken, so "is this workbook finished"
has a written-down answer rather than a judgement someone makes per
delivery.

Those declarations are not the whole workbook, and assuming they were is
how six Videos columns shipped 85% full under a COMPLETE report: the list
covered three of that sheet's forty columns and none of Niches, Success
Factors or Failure Factors. So every column of every sheet is measured,
from the rows the export is about to write. A column nobody classified is
held to _DEFAULT_MIN_FILL rather than skipped -- an exception has to be
claimed in SHEET_WAIVERS, with the reason a blank cell is correct there.

The rows are swept, not the finished file, because the export removes a
column that is entirely empty: the emptiest column of all is the one that
would leave no trace to find.

Two deliberate limits.

It heals by re-running the pipeline's own nodes, scoped to the run --
never by writing values itself. A gate that invents data to make its own
check pass is worse than the gap it hides.

And a threshold below 1.0 is not slack, it is honesty: a channel with no
country signal has no country, and a channel the model finds no failure
factors for has none. Demanding 100% would make the gate cry wolf on
every run and be switched off within a week. The thresholds below are set
from what the finance and crime deliverables actually achieved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import structlog

from src.db.connection import get_connection, put_connection

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ColumnCheck:
    """One column, what fills it, and how full it has to be."""

    column: str
    #: Node that produces it. None means nothing can fill it after the
    #: fact -- the gate reports it and does not pretend otherwise.
    node: str | None
    #: Fraction of rows that must be non-NULL for this to pass.
    min_fill: float
    #: Why this threshold, in one line. Rendered in the report.
    note: str = ""
    table: str = "channels"


#: Channel-level columns, keyed to the node that writes them.
CHANNEL_CHECKS: list[ColumnCheck] = [
    # Classification. Everything downstream keys off primary_niche_id, so
    # this is the one whose absence empties three sheets at once.
    ColumnCheck("primary_niche_id", "classify_channel", 0.95,
                "drives the Niches sheet and the discovery graph"),
    ColumnCheck("face_status", "classify_channel", 0.95),
    ColumnCheck("dominant_format", "classify_channel", 0.90),

    # Taxonomy dimensions.
    ColumnCheck("primary_topic", "populate_taxonomy_dimensions", 0.95,
                "the family a channel groups under on the graph"),
    ColumnCheck("secondary_topic", "populate_taxonomy_dimensions", 0.85),
    ColumnCheck("geography_focus", "populate_taxonomy_dimensions", 0.85),
    ColumnCheck("target_audience", "populate_taxonomy_dimensions", 0.85),
    ColumnCheck("content_approach", "populate_taxonomy_dimensions", 0.85),

    # Authority.
    ColumnCheck("creator_authority", "populate_shared_fields", 0.90),

    # Dates and geography.
    ColumnCheck("first_video_published_at", "resolve_first_video_date", 0.90),
    ColumnCheck("country_code", "resolve_geo_language", 0.70,
                "a channel with no country signal genuinely has none"),
    ColumnCheck("primary_language_code", "resolve_geo_language", 0.85),

    # Deterministic signals -- these have no excuse for being empty, since
    # nothing about them depends on a model answering.
    ColumnCheck("channel_size_bucket", "score_signals", 0.98,
                "a function of subscriber_count alone"),
    ColumnCheck("engagement_score", "score_signals", 0.95),
    ColumnCheck("evergreen_score", "score_signals", 0.95),
    ColumnCheck("is_likely_news", "score_signals", 0.95),
    ColumnCheck("uploads_per_week_avg", "extract_metadata_signals", 0.95),
]

#: Video-level columns. Scoped to videos of the run's floor-passing
#: channels, since those are the ones a workbook carries.
VIDEO_CHECKS: list[ColumnCheck] = [
    ColumnCheck("video_description", "describe_video_titles", 0.90,
                "the Videos sheet's only prose column", table="videos"),
    ColumnCheck("search_browse_estimate", "populate_shared_fields", 0.90, table="videos"),
    ColumnCheck("outlier_score", None, 0.95,
                "written during hydration; cannot be filled afterwards",
                table="videos"),
    # The title signals. Deterministic functions of the title -- nothing
    # about them depends on a model answering, so a gap is a node that did
    # not reach the row, not a judgement call, and 0.98 says so. They were
    # absent from this list entirely until run-3f649c9246a3 shipped them
    # 85% full: a gate only checks what it declares, and TestTheSpecCovers
    # TheWorkbook now refuses to let a Videos column go undeclared again.
    ColumnCheck("title_word_count", "extract_metadata_signals", 0.98, table="videos"),
    ColumnCheck("title_has_number", "extract_metadata_signals", 0.98, table="videos"),
    ColumnCheck("title_is_question", "extract_metadata_signals", 0.98, table="videos"),
    ColumnCheck("title_capitalization", "extract_metadata_signals", 0.98, table="videos"),
    ColumnCheck("title_emoji_count", "extract_metadata_signals", 0.98, table="videos"),
    ColumnCheck("is_likely_news", "extract_metadata_signals", 0.98,
                "the video-level flag, not the channel one of the same name",
                table="videos"),
]

#: The crime case-file columns. On a crime workbook these ARE the
#: deliverable; on any other the export drops them entirely. They sat in
#: the Videos waiver list as "crime only", which reads as "not applicable"
#: and was applied on every run -- so a crime workbook shipped them at 18%
#: with the gate reporting COMPLETE, because it had excused the one thing
#: the client had commissioned.
#:
#: Thresholds calibrated against the delivered crime.xlsx, which achieved
#: 99.4-99.8% on every one of these across 11,161 videos and 2,532 shorts.
CRIME_VIDEO_CHECKS: list[ColumnCheck] = [
    ColumnCheck(column, "populate_crime_metadata", 0.95,
                "a crime workbook's reason for existing", table="videos")
    for column in (
        "crime_type", "victim_type", "suspect_relationship",
        "investigation_type", "evidence_type_primary", "case_status",
        "case_fame_level", "case_country", "reveal_mechanisms",
        "interrogation_available", "bodycam_available", "cctv_available",
        "call_911_available", "court_footage_available",
    )
]

#: Crime columns that are sparse even in a good crime workbook, with the
#: measurement behind that claim.
CRIME_WAIVERS: dict[str, str] = {
    "case_year": (
        "only a dated, identifiable case has a year the model can give. "
        "The delivered crime.xlsx carries it on 27% of videos and 18% of "
        "shorts, so a fill rate here would fail every crime run forever"
    ),
}

#: Held to this when nobody has said otherwise. Deliberately strict: an
#: unclassified column that is legitimately full passes in silence, and one
#: that is quietly empty is exactly what this gate exists to catch. A column
#: that earns an exception earns it explicitly, in SHEET_WAIVERS, with the
#: reason written down.
_DEFAULT_MIN_FILL = 0.98

#: Videos-sheet columns deliberately NOT held to a fill rate, each with the
#: reason. This is not a way to silence a column that is merely awkward --
#: it is for ones where a blank cell is the correct answer, and a threshold
#: would make the gate cry wolf on every run until someone switched it off.
UNCHECKED_VIDEO_COLUMNS: dict[str, str] = {
    "video_id": "the key itself",
    "channel_id": "the key itself",
    "channel_title": "joined from channels, checked there",
    "title": "written at hydration; a video without one is not stored",
    "view_count": "hydration",
    "like_count": "creators can hide likes",
    "comment_count": "creators can disable comments",
    "published_at": "hydration",
    "days_since_published": "computed in SQL from published_at",
    "duration_seconds": "hydration",
    "is_short": "hydration",
    "language_code": "a video with no language signal has none",
    "sample_reason": "set only for videos pulled in by a specific sampler",
    "evergreen_score": "scored only where there is enough history to score",
    "views_per_day_since_publish": "hydration",
    "commercial_intent": "a niche-level attribute, absent for unclassified niches",
    "sponsor_status": "most videos have no sponsor, which is the finding",
    "sponsor_category": "only where a sponsor was found",
    "sponsor_name": "only where a sponsor was named",
    "extra": "the raw JSON blob thumbnail_url is read out of, not a column",
    "thumbnail_url": "absent for deleted or private videos",
    "thumbnail_has_face": "vision signals, run on a sample not the census",
    "thumbnail_text_density": "vision signals, run on a sample not the census",
}


#: Per sheet, the columns where blank is the right answer. Everything not
#: listed here is measured -- including columns nobody thought about, which
#: is the point.
SHEET_WAIVERS: dict[str, dict[str, str]] = {
    "Videos": UNCHECKED_VIDEO_COLUMNS,
    "Channels": {
        "channel_id": "the key itself",
        "description": "a channel is under no obligation to write one",
        "language_confidence": (
            "written only where the language had to be inferred. A channel "
            "whose language came straight from the API has the language and "
            "no confidence, because there was nothing to be unsure about -- "
            "so a blank here means known, not missing. The language itself "
            "is checked separately at 0.85"
        ),
        "missing_required_fields": (
            "the diagnostic itself -- empty means nothing was missing, so a "
            "fill rate on it would invert the meaning of the column"
        ),
        "banner_url": "not every channel sets a banner",
        "custom_url": "only channels that claimed a handle have one",
        "sponsor_name": "only where a sponsor was identified",
        "sponsor_category": "only where a sponsor was identified",
        "secondary_niche_id": "a channel with one clear niche has no second",
        "notes": "free-text, written only where there is something to note",
        "flagged_reason": "set only on a channel that was flagged",
        "primary_niche": (
            "the niche-family label. assign_niche_families now writes it "
            "for every vertical, so it should be full -- the waiver stays "
            "only for a channel whose niche was never classified at all, "
            "which has no family to belong to"
        ),
        "vertical_start_date_basis": "only where a start date was resolved",
        "vertical_start_date_confidence": "only where a start date was resolved",
        "creator_authority_evidence": "quoted only where authority was found",
        "crime_focus": "crime only",
        "case_coverage_style": "crime only",
    },
    "Niches": {
        "description": "a niche label can stand without a gloss",
    },
    "Success Factors": {
        "evidence_note": "quoted only where the model cited a number",
    },
    "Failure Factors": {
        "evidence_note": "quoted only where the model cited a number",
    },
}

#: Thresholds for columns the sweep finds that have no SQL-level check --
#: a sheet column whose right bar is not the strict default, with the reason
#: it differs. Without these the choice is a waiver (no floor at all) or the
#: default (a report that cries wolf every run), and neither is honest.
SHEET_CHECKS: list[ColumnCheck] = [
    ColumnCheck("region", "resolve_geo_language", 0.70,
                "derived from country_code, which sits at 0.70 for the same "
                "reason: a channel with no country signal has no region",
                table="Channels"),
    ColumnCheck("data_completeness_score", "finalize_dataset", 0.98,
                "computed once at the end of the graph; a channel written "
                "after it keeps NULL until the node is re-run",
                table="Channels"),
    ColumnCheck("raw_sub_niche", "populate_taxonomy_dimensions", 0.95,
                "what the model proposed before canonicalisation; it needs "
                "the channel_niches row classify_channel creates, so a heal "
                "that ran taxonomy before classification leaves it empty and "
                "nothing re-runs it unless it is declared here",
                table="Channels"),
    ColumnCheck("commercial_intent", "populate_shared_fields", 0.90,
                "a niche-level attribute; a niche created after the node ran "
                "keeps NULL until it is re-run", table="Channels"),
    ColumnCheck("is_evergreen_prone", "classify_channel", 0.80,
                "set when the model proposes a niche and it declines to "
                "judge some; never revisited, so a re-run cannot raise it",
                table="Niches"),
]

#: Declared checks, addressable by (sheet, column) for the sweep. Sheet names
#: are lowercased so "Channels" and the checks' table="channels" agree.
_DECLARED: dict[tuple[str, str], ColumnCheck] = {
    (c.table.lower(), c.column): c
    for c in CHANNEL_CHECKS + VIDEO_CHECKS + SHEET_CHECKS + CRIME_VIDEO_CHECKS
}


@dataclass
class Finding:
    column: str
    table: str
    filled: int
    total: int
    min_fill: float
    node: str | None
    note: str = ""

    @property
    def rate(self) -> float:
        return (self.filled / self.total) if self.total else 1.0

    @property
    def ok(self) -> bool:
        return self.rate >= self.min_fill

    def describe(self) -> str:
        pct = f"{self.rate * 100:5.1f}%"
        need = f"needs {self.min_fill * 100:.0f}%"
        fixer = f"<- {self.node}" if self.node else "(unfillable)"
        tail = f"  {self.note}" if self.note else ""
        return (f"{self.table}.{self.column:26} {self.filled:6}/{self.total:<6} "
                f"{pct}  {need:9} {fixer}{tail}")


@dataclass
class Report:
    run_id: str
    #: Channels re-tagged from the run's own checkpoint because its
    #: membership had been lost. 0 on a healthy run.
    recovered_channels: int = 0
    findings: list[Finding] = field(default_factory=list)
    empty_tables: list[str] = field(default_factory=list)
    rounds: int = 0
    healed: list[str] = field(default_factory=list)
    unfixable: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok]

    @property
    def complete(self) -> bool:
        return not self.failures and not self.empty_tables

    def render(self) -> str:
        lines = [f"  export completeness for {self.run_id}"]
        if self.recovered_channels:
            lines.append(
                f"  RECOVERED: {self.recovered_channels} channels re-tagged "
                f"from the run's own checkpoint; its membership had been lost"
            )
        if self.empty_tables:
            lines.append(f"  EMPTY: {', '.join(self.empty_tables)}")
        for f in sorted(self.findings, key=lambda f: (f.ok, f.column)):
            lines.append(("  PASS  " if f.ok else "  FAIL  ") + f.describe())
        if self.healed:
            lines.append(f"  healed by re-running: {', '.join(self.healed)}")
        if self.unfixable:
            lines.append(f"  still short after healing: {', '.join(self.unfixable)}")
        lines.append("  COMPLETE" if self.complete else "  INCOMPLETE")
        return "\n".join(lines)


def _run_channel_ids(conn, run_id: str, floor_only: bool = True) -> list[str]:
    """Channels this run discovered. NOT the same set the workbook writes.

    Kept for callers that genuinely mean "what this run found". Anything
    asking "what will be in the file" must use _workbook_ids instead --
    see the comment there.
    """
    cur = conn.cursor()
    try:
        sql = ("SELECT channel_id FROM channels WHERE first_discovered_run_id = %s")
        if floor_only:
            sql += " AND meets_subscriber_floor = TRUE"
        cur.execute(sql, (run_id,))
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()


def recover_membership(run_id: str) -> int:
    """Rebuild a run's category_tags from its own state. Returns channels tagged.

    A workbook is assembled from category_tags: they are the record of
    which shared rows belong to which run. hydrate_metadata writes them as
    the last statement of its persistence block, so anything that escaped
    that block took the tagging with it -- and the export then had no rows
    to draw, producing a file with every sheet empty.

    That happened on run-b8b0ea1bf0a6: a foreign-key violation on one video
    aborted the loop, 0 tags were written, and the client received an empty
    workbook. The isolation fix in hydrate_metadata stops the abort; this
    repairs a run that already suffered one, and any future cause of the
    same shape.

    Nothing here is invented. The channel ids come from the run's own
    checkpoint -- what it discovered and hydrated -- filtered to rows that
    exist, and the videos are the ones the store already holds for those
    channels. A run whose tags are intact is left alone entirely.

    The video set is what we hold rather than the in-memory sample the run
    chose, because that sample died with the aborted loop. It is the same
    channels either way, and every sheet is filtered by floor and category
    downstream regardless.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM category_tags "
                "WHERE run_id = %s AND entity_type = 'channel'",
                (run_id,),
            )
            if (cur.fetchone() or [0])[0]:
                return 0  # intact; not this function's business
    finally:
        put_connection(conn)

    ids = _run_state_channel_ids(run_id)
    if not ids:
        return 0

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel_id FROM channels WHERE channel_id = ANY(%s)",
                (ids,),
            )
            known = [r[0] for r in cur.fetchall()]
            if not known:
                return 0
            cur.execute(
                "SELECT video_id FROM videos WHERE channel_id = ANY(%s)",
                (known,),
            )
            video_ids = [r[0] for r in cur.fetchall()]

        from src.tools.dedup import persist_category_tags

        persist_category_tags(
            conn,
            run_id=run_id,
            # The tags carry a branch id; a recovered run has no single
            # branch to name, so it is labelled for what it is.
            tree_node_id="recovered",
            channel_ids=known,
            video_ids=video_ids,
        )
        logger.warning(
            "run_membership_recovered",
            run_id=run_id,
            channels=len(known),
            videos=len(video_ids),
        )
        return len(known)
    except Exception as exc:
        conn.rollback()
        logger.warning("run_membership_recovery_failed", run_id=run_id, error=str(exc))
        return 0
    finally:
        put_connection(conn)


def _run_state_channel_ids(run_id: str) -> list[str]:
    """The channels a run's checkpoint says it found. Empty if unreadable."""
    try:
        import json
        from pathlib import Path

        from src.api.runs import registry_path

        thread_id = ""
        for line in Path(registry_path()).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("run_id") == run_id:
                thread_id = entry.get("thread_id") or ""
        if not thread_id:
            return []

        from src.db.checkpointer import get_checkpointer
        from src.graph import compile_graph

        app = compile_graph(get_checkpointer())
        values = app.get_state(
            {"configurable": {"thread_id": thread_id}}
        ).values or {}
        return sorted({
            *(values.get("discovered_channel_ids") or []),
            *(values.get("hydrated_channel_ids") or []),
        })
    except Exception:
        return []


def _workbook_ids(run_id: str) -> tuple[list[str], list[str]]:
    """The channel and video ids the workbook will actually contain.

    This gate exists to check the file before a client opens it, so the only
    row set worth checking is the file's own. Deriving it independently is
    what let run-3f649c9246a3 pass: the gate audited the 15 channels the run
    discovered, the workbook was written from 9 -- a different set, because
    a resolved category admits channels earlier runs found. One of those was
    a science channel from run-kw-fail whose 70 videos no scoped enrichment
    node had ever touched, and six title-signal columns shipped 85% full
    under a COMPLETE report.

    Asking the export removes the possibility of disagreeing with it.
    """
    from src.export import workbook_rows

    channels, videos = workbook_rows(run_id)
    return (
        [c["channel_id"] for c in channels],
        [v["video_id"] for v in videos],
    )


def _sweep(report: Report, already: set[tuple[str, str]]) -> None:
    """Fill rates for every column of every sheet, declared or not.

    The declared checks above are the ones with a node behind them -- the
    gaps this gate can actually close. They are not, and cannot be, the
    whole workbook: a hand-kept list covered three of the Videos sheet's
    forty columns and none of Niches, Success Factors or Failure Factors,
    which is how six columns shipped 85% full under a COMPLETE report.

    So every column is measured, from the rows the export will write. A
    column nobody classified is held to _DEFAULT_MIN_FILL rather than
    ignored, because the cost of being wrong in that direction is one line
    in a report, and the cost of the other direction is what this gate was
    built after.

    Sweeping the fetched rows rather than the finished sheet is deliberate:
    the export drops a column that is entirely empty, so the emptiest
    column of all is the one that leaves no trace in the file.
    """
    from src.export import ALWAYS_DROPPED_COLUMNS, sheet_rows, workbook_scope

    # A waiver is only ever honest about a particular kind of workbook.
    # "crime only" means "absent from this file" on an automotive run and
    # "this is what the client paid for" on a crime one, and the gate has
    # to know which it is looking at.
    is_crime = workbook_scope(report.run_id).category == "crime"

    for sheet, rows in sheet_rows(report.run_id).items():
        if not rows:
            # An empty sheet is the loudest possible gap and was the one
            # thing the sweep passed over in silence.
            #
            # There is a separate empty-table check, but it counts rows in
            # the TABLE for the workbook's channels -- a different question
            # from what the export writes. The crime run of 2026-09-07 had
            # success and failure factors on all four of its channels and
            # shipped both sheets blank, so the table check said "not
            # empty", the sweep said nothing at all, and the gate reported
            # COMPLETE over a workbook with two empty sheets in it.
            #
            # Asked of the rows the export returns, there is no gap between
            # the question and the file.
            if sheet not in report.empty_tables:
                report.empty_tables.append(sheet)
            continue
        # Columns the export removes before writing are not in the file, so
        # a fill rate on them measures nothing. Read from the export's own
        # frozenset rather than copied, so removing a column from the
        # deliverable cannot leave the gate complaining about it forever.
        waived = dict(SHEET_WAIVERS.get(sheet, {}))
        waived.update({c: "removed from the deliverable" for c in ALWAYS_DROPPED_COLUMNS})
        if is_crime:
            waived.update(CRIME_WAIVERS)
        else:
            # Dropped from the file on every other vertical, so a fill rate
            # on them measures a column nobody will see.
            waived.update({c.column: "crime only" for c in CRIME_VIDEO_CHECKS})
            waived.update(CRIME_WAIVERS)
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)

        for column in columns:
            if column in waived:
                continue
            if (sheet.lower(), column) in already:
                continue
            declared = _DECLARED.get((sheet.lower(), column))
            filled = sum(
                1 for r in rows
                if r.get(column) is not None and r.get(column) != ""
            )
            report.findings.append(Finding(
                column, sheet, filled, len(rows),
                declared.min_fill if declared else _DEFAULT_MIN_FILL,
                declared.node if declared else None,
                declared.note if declared else "not individually declared",
            ))


def audit(run_id: str) -> Report:
    """Fill rates for every declared column, plus any empty table."""
    report = Report(run_id=run_id)
    ids, video_ids = _workbook_ids(run_id)
    conn = get_connection()
    try:
        if not ids:
            report.empty_tables.append("channels")
            return report

        cur = conn.cursor()
        try:
            for check in CHANNEL_CHECKS:
                cur.execute(
                    f"SELECT COUNT(*), COUNT({check.column}) FROM channels "
                    "WHERE channel_id = ANY(%s)",
                    (ids,),
                )
                total, filled = cur.fetchone()
                report.findings.append(Finding(
                    check.column, "channels", filled, total,
                    check.min_fill, check.node, check.note,
                ))

            for check in VIDEO_CHECKS:
                # By video id, not by channel: the Videos sheet carries the
                # rows the export selected, and a channel's other videos are
                # neither written nor worth billing a heal for.
                cur.execute(
                    f"SELECT COUNT(*), COUNT(v.{check.column}) FROM videos v "
                    "WHERE v.video_id = ANY(%s)",
                    (video_ids,),
                )
                total, filled = cur.fetchone()
                report.findings.append(Finding(
                    check.column, "videos", filled, total,
                    check.min_fill, check.node, check.note,
                ))

            # And then every remaining column of every sheet, so a gap in
            # one nobody declared is still a gap the client never sees.
            _sweep(report, {(f.table.lower(), f.column) for f in report.findings})

            # Tables whose emptiness is a sheet the client opens to nothing.
            for table, label in (
                ("channel_success_factors", "Success Factors"),
                ("channel_failure_factors", "Failure Factors"),
                ("channel_cohorts", "cohorts"),
            ):
                cur.execute(
                    f"SELECT COUNT(DISTINCT t.channel_id) FROM {table} t "
                    "WHERE t.channel_id = ANY(%s)",
                    (ids,),
                )
                covered = cur.fetchone()[0]
                if covered == 0:
                    report.empty_tables.append(label)

            cur.execute(
                "SELECT COUNT(DISTINCT c.primary_niche_id) FROM channels c "
                "WHERE c.channel_id = ANY(%s) AND c.primary_niche_id IS NOT NULL",
                (ids,),
            )
            if cur.fetchone()[0] == 0:
                report.empty_tables.append("Niches")
        finally:
            cur.close()
    finally:
        put_connection(conn)
    return report


#: node name -> callable. Imported lazily: several pull in the LLM client
#: and the graph, and audit() must stay usable without either.
def _nodes() -> dict[str, Callable[[dict], Any]]:
    from src.nodes.assign_cohorts import assign_cohorts
    from src.nodes.assign_niche_families import assign_niche_families
    from src.nodes.finalize_dataset import finalize_dataset
    from src.nodes.populate_crime_metadata import populate_crime_metadata
    from src.nodes.classify_channel import classify_channel
    from src.nodes.describe_video_titles import describe_video_titles
    from src.nodes.extract_metadata_signals import extract_metadata_signals
    from src.nodes.extract_success_failure_factors import extract_success_failure_factors
    from src.nodes.populate_shared_fields import populate_shared_fields
    from src.nodes.populate_taxonomy_dimensions import populate_taxonomy_dimensions
    from src.nodes.resolve_first_video_date import resolve_first_video_date
    from src.nodes.resolve_geo_language import resolve_geo_language
    from src.tools.signal_scoring import score_signals

    return {
        "classify_channel": classify_channel,
        "populate_taxonomy_dimensions": populate_taxonomy_dimensions,
        "populate_shared_fields": populate_shared_fields,
        "resolve_first_video_date": resolve_first_video_date,
        "resolve_geo_language": resolve_geo_language,
        "score_signals": score_signals,
        "extract_metadata_signals": extract_metadata_signals,
        "extract_success_failure_factors": extract_success_failure_factors,
        "describe_video_titles": describe_video_titles,
        # Before assign_cohorts, the same order the graph runs them in.
        # Idempotent: a niche already in a family big enough for this
        # workbook is left where it is, so a second pass over a healthy run
        # changes nothing and says so.
        "assign_niche_families": assign_niche_families,
        "assign_cohorts": assign_cohorts,
        # Absent until 2026-09-07, so the crime columns could not have been
        # healed even once they were checked.
        "populate_crime_metadata": populate_crime_metadata,
        # Idempotent: it recomputes a score from columns already written,
        # so re-running it after a heal picks up whatever the heal filled.
        "finalize_dataset": finalize_dataset,
    }


#: Hard ceiling on calls to any one node, and on the whole healing pass.
#:
#: A call count alone is the wrong guard. These nodes take a bounded batch
#: per call -- 50 channels, 30 video titles -- so the number of calls
#: needed scales with the backlog: filling 4,614 video descriptions takes
#: 154 calls of describe_video_titles, and a ceiling of 12 would silently
#: give up at 360 videos and report failure it had caused itself.
#:
#: A wall-clock budget is the honest bound. It answers the real question
#: -- how long may an export wait for its own gaps to be filled -- rather
#: than a proxy that means something different for every node.
_MAX_CALLS_PER_NODE = 400
_DEFAULT_HEAL_SECONDS = 1800.0


def heal(
    run_id: str,
    only: set[str] | None = None,
    budget_seconds: float = _DEFAULT_HEAL_SECONDS,
) -> list[str]:
    """Re-run the nodes that fill whatever the audit found short.

    Returns the node names that actually changed something. A node that
    reports no progress is not called again -- that is the difference
    between finishing a backlog and the re-billing loops this project has
    already paid for twice.
    """
    deadline = time.monotonic() + budget_seconds
    # The workbook's channels, not the run's. Every enrichment node is
    # scoped to the channels it is handed, so healing the run's own set
    # could never fix a row belonging to a channel an earlier run
    # discovered -- which is precisely the row that was short. Widening
    # the scope here is safe because a channel only reaches this list by
    # being in the file about to be written.
    ids, _ = _workbook_ids(run_id)
    if not ids:
        return []

    state = {
        "run_id": run_id,
        "thread_id": f"export-gate-{run_id}",
        "discovered_channel_ids": ids,
        "scope_channel_ids": ids,
        "hydrated_channel_ids": set(),
        # Exempt from the RUN deadline -- the gate runs after the run's
        # window has closed by design, that is the whole point of it -- but
        # bound by the gate's own budget, passed down so the write-up loops
        # can honour it per channel and per batch rather than only between
        # node calls.
        "healing": True,
        "heal_deadline_monotonic": deadline,
    }

    nodes = _nodes()
    ran: list[str] = []
    for name, node in nodes.items():
        if only is not None and name not in only:
            continue
        for _ in range(_MAX_CALLS_PER_NODE):
            if time.monotonic() >= deadline:
                break
            try:
                out = node(dict(state))
            except Exception:
                break
            summary = ((out.get("node_logs") or [{}])[-1] or {}).get("input_summary", {})
            progress = sum(
                int(v) for k, v in summary.items()
                if k in ("classified", "populated", "resolved", "extracted",
                         "assigned", "described", "processed", "scored",
                         "channels_scored", "sponsor_updated",
                         # extract_metadata_signals reports channels and
                         # videos separately: its video pass is independent
                         # of channel eligibility, so a call that enriches
                         # 2,000 videos and 0 channels is still progress.
                         "videos_enriched",
                         # populate_shared_fields propagates a channel's
                         # estimate onto videos its per-channel loop could
                         # not reach; that is progress with 0 classified.
                         "propagated", "footage_flags")
                and isinstance(v, (int, float))
            )
            if progress and name not in ran:
                ran.append(name)
            if not progress:
                break
    return ran


def ensure_complete(
    run_id: str,
    max_rounds: int = 2,
    budget_seconds: float = _DEFAULT_HEAL_SECONDS,
) -> Report:
    """Audit, fill what is short, audit again. The gate itself.

    `max_rounds` is small on purpose. One healing pass fixes a node that
    did not run; a second catches anything the first unblocked (cohorts
    need classification, factors need signals). A third would mean a node
    is failing rather than lagging, and the honest answer then is a report
    saying so, not another hour of retries.
    """
    deadline = time.monotonic() + budget_seconds

    # Before anything is measured: a run whose tags were lost has no rows
    # to audit, and every column of an empty workbook trivially "passes"
    # its fill rate over zero rows. Rebuilding the membership first is what
    # turns that into a workbook the rest of this can actually check.
    recovered = recover_membership(run_id)

    report = audit(run_id)
    report.recovered_channels = recovered
    for round_no in range(1, max_rounds + 1):
        if report.complete:
            break
        wanted = {f.node for f in report.failures if f.node}
        if report.empty_tables:
            # An empty sheet is downstream of a node, not a column: name
            # the producers directly.
            wanted |= {
                "extract_success_failure_factors", "assign_cohorts",
                "classify_channel",
            }
        if not wanted:
            break
        report.rounds = round_no
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        healed = heal(run_id, only=wanted, budget_seconds=remaining)
        after = audit(run_id)
        after.rounds = round_no
        after.healed = sorted(set(report.healed) | set(healed))
        report = after

    report.unfixable = sorted(
        f"{f.table}.{f.column}" for f in report.failures
    )
    return report


def gate_before_export(
    run_id: str,
    max_rounds: int = 2,
    budget_seconds: float = _DEFAULT_HEAL_SECONDS,
) -> Report:
    """Entry point for the pipeline. Never raises; always reports.

    A workbook that is 90% populated is still worth handing over, with its
    gaps named -- refusing to export would leave the client with nothing
    at all, which is strictly worse than a file plus an honest list of
    what is thin in it.
    """
    started = time.monotonic()
    try:
        report = ensure_complete(
            run_id, max_rounds=max_rounds, budget_seconds=budget_seconds
        )
    except Exception as exc:  # pragma: no cover - defensive
        report = Report(run_id=run_id)
        report.unfixable = [f"gate failed: {type(exc).__name__}: {exc}"]
    report.rounds = report.rounds or 0
    _ = time.monotonic() - started
    return report

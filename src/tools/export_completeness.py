"""The gate a workbook has to pass before anyone is handed it.

Every empty sheet and blank column this project has shipped came from the
same shape of failure: a node did not run, or ran against the wrong
channels, and nothing between it and the client noticed. The run reported
success, the export wrote whatever was in the table, and the gap was found
by a person opening the file.

So the export stops being the last step. This is:

    audit  ->  fill what is fillable  ->  audit again  ->  report

Each column is declared here with the node that produces it and the fill
rate below which it counts as broken, so "is this workbook finished" is a
question with a written-down answer rather than a judgement someone makes
per delivery.

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

from src.db.connection import get_connection, put_connection


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
]


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
    cur = conn.cursor()
    try:
        sql = ("SELECT channel_id FROM channels WHERE first_discovered_run_id = %s")
        if floor_only:
            sql += " AND meets_subscriber_floor = TRUE"
        cur.execute(sql, (run_id,))
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()


def audit(run_id: str) -> Report:
    """Fill rates for every declared column, plus any empty table."""
    report = Report(run_id=run_id)
    conn = get_connection()
    try:
        ids = _run_channel_ids(conn, run_id)
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
                cur.execute(
                    f"SELECT COUNT(*), COUNT(v.{check.column}) FROM videos v "
                    "WHERE v.channel_id = ANY(%s)",
                    (ids,),
                )
                total, filled = cur.fetchone()
                report.findings.append(Finding(
                    check.column, "videos", filled, total,
                    check.min_fill, check.node, check.note,
                ))

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
        "assign_cohorts": assign_cohorts,
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
    conn = get_connection()
    try:
        ids = _run_channel_ids(conn, run_id, floor_only=False)
    finally:
        put_connection(conn)
    if not ids:
        return []

    state = {
        "run_id": run_id,
        "thread_id": f"export-gate-{run_id}",
        "discovered_channel_ids": ids,
        "scope_channel_ids": ids,
        "hydrated_channel_ids": set(),
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
                         "channels_scored", "sponsor_updated")
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
    report = audit(run_id)
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

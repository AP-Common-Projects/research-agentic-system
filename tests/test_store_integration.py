"""Store integration — real SQL against a real Postgres.

Why this file exists: every persistence test in test_tools.py drives a
MagicMock cursor, which happily accepts any string as SQL. That is fine for
asserting *which* parameters a writer binds, and useless for asserting that the
statement is valid. A real bug lived behind that gap for the whole project —

    COALESCE(NULLIF(%(first_seen_at)s, ''), NOW())

Postgres cannot match `text` against `timestamptz`, so every persist_channel
and persist_video call raised DatatypeMismatch. Because hydrate_metadata wraps
the entire persistence loop in one try/except, the first failure discarded all
22 channels and 646 videos of a round, recorded a single "hydration_errors: 1",
and let the run continue. compact_branch and synthesize then queried an empty
store and produced an empty report — a run that looked successful end to end
and had nothing in it.

These tests skip when Postgres is unreachable so the suite stays runnable
without one, but they are the only place SQL is genuinely executed.
"""

from __future__ import annotations

import pytest

from src.db.schema import ensure_schema
from src.export import fetch_run_channels, fetch_run_videos
from src.tools.dedup import (
    fetch_videos_by_channels,
    persist_channel,
    persist_channel_signals,
    persist_channel_snapshot,
    persist_channel_v3,
    persist_edge,
    persist_edges,
    persist_video,
    persist_video_v3,
)

try:  # pragma: no cover - environment dependent
    from src.api import store_queries

    store_queries.ping(timeout=2.0)
    ensure_schema()
    _POSTGRES = True
except Exception:  # pragma: no cover
    _POSTGRES = False

pytestmark = pytest.mark.skipif(_POSTGRES is False, reason="Postgres not reachable")

CH1, CH2 = "UC_itest_1", "UC_itest_2"
RUN = "run-itest"


@pytest.fixture
def conn():
    from src.db.connection import get_connection, put_connection

    connection = get_connection()
    _cleanup(connection)
    yield connection
    _cleanup(connection)
    put_connection(connection)


def _cleanup(connection):
    cur = connection.cursor()
    cur.execute("DELETE FROM discovery_edges WHERE run_id = %s", (RUN,))
    # category_tags has no FK to channels/videos (entity_id is a plain TEXT
    # column), so deleting the channel below does not cascade into it — an
    # explicit delete is required or rows accumulate across test runs.
    cur.execute("DELETE FROM category_tags WHERE run_id = %s", (RUN,))
    # Same gap: channel_snapshots has no FK/cascade to channels either, so a
    # snapshot row from one test leaks into the next test's total_video_count
    # assertion (both use CH1 + RUN) unless deleted explicitly here too.
    cur.execute("DELETE FROM channel_snapshots WHERE run_id = %s", (RUN,))
    cur.execute("DELETE FROM channels WHERE channel_id LIKE 'UC\\_itest\\_%'")
    connection.commit()
    cur.close()


def _count(connection, table, where="", params=()):
    cur = connection.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table} {where}", params)
    value = cur.fetchone()[0]
    cur.close()
    return value


class TestPersistChannelSQL:
    def test_persists_with_an_explicit_timestamp(self, conn):
        persist_channel(conn, {
            "channel_id": CH1,
            "title": "Ben Felix",
            "subscriber_count": 624000,
            "description": "investing",
            "discovery_method": "keyword",
            "first_seen_at": "2026-08-14T00:00:00+00:00",
        })
        assert _count(conn, "channels", "WHERE channel_id = %s", (CH1,)) == 1

    def test_persists_when_the_timestamp_is_missing(self, conn):
        """The regression: an empty/absent first_seen_at has to fall back to
        NOW(), and the COALESCE has to type-check to do it."""
        persist_channel(conn, {
            "channel_id": CH1, "title": "T", "discovery_method": "graph_walk",
        })
        cur = conn.cursor()
        cur.execute("SELECT first_seen_at FROM channels WHERE channel_id = %s", (CH1,))
        assert cur.fetchone()[0] is not None
        cur.close()

    def test_upsert_is_idempotent(self, conn):
        for subs in (100, 200):
            persist_channel(conn, {
                "channel_id": CH1, "title": "T", "subscriber_count": subs,
                "discovery_method": "keyword",
            })
        assert _count(conn, "channels", "WHERE channel_id = %s", (CH1,)) == 1
        cur = conn.cursor()
        cur.execute("SELECT subscriber_count FROM channels WHERE channel_id = %s", (CH1,))
        assert cur.fetchone()[0] == 200, "re-scan must update, not duplicate"
        cur.close()


class TestPersistVideoSQL:
    def test_persists_with_and_without_published_at(self, conn):
        persist_channel(conn, {"channel_id": CH1, "title": "T", "discovery_method": "keyword"})
        persist_video(conn, {
            "video_id": "itest_v1", "channel_id": CH1, "title": "V1",
            "view_count": 47904190, "like_count": 544334, "comment_count": 23143,
            "published_at": "2026-01-01T00:00:00Z", "outlier_score": 3.2,
        })
        persist_video(conn, {
            "video_id": "itest_v2", "channel_id": CH1, "title": "V2", "published_at": "",
        })
        assert _count(conn, "videos", "WHERE channel_id = %s", (CH1,)) == 2

    def test_round_trips_through_fetch(self, conn):
        persist_channel(conn, {"channel_id": CH1, "title": "T", "discovery_method": "keyword"})
        persist_video(conn, {
            "video_id": "itest_v1", "channel_id": CH1, "title": "V1",
            "view_count": 1000, "like_count": 50, "comment_count": 7,
            "published_at": "2026-01-01T00:00:00Z", "outlier_score": 2.5,
        })
        rows = fetch_videos_by_channels(conn, [CH1])
        assert len(rows) == 1
        assert rows[0]["view_count"] == 1000

    def test_signals_persist(self, conn):
        persist_channel(conn, {"channel_id": CH1, "title": "T", "discovery_method": "keyword"})
        persist_channel_signals(
            conn, CH1, {"engagement_rate": 0.057, "cadence": 4.0, "velocity": 1.2}
        )
        assert _count(conn, "channels", "WHERE channel_id = %s", (CH1,)) == 1


class TestDiscoveryEdgesSQL:
    def test_edge_persists_without_a_resolved_target_id(self, conn):
        """featured_channels names its target by URL only — an edge must be
        storable before that channel has ever been fetched."""
        persist_edge(conn, {
            "source_channel_id": CH1,
            "target_channel_ref": "https://www.youtube.com/@rationalreminder",
            "edge_type": "featured_channel",
            "run_id": RUN,
        })
        assert _count(conn, "discovery_edges", "WHERE run_id = %s", (RUN,)) == 1

    def test_all_new_edge_types_pass_the_check_constraint(self, conn):
        edges = [
            {"source_channel_id": CH1, "target_channel_ref": f"https://x/{t}",
             "edge_type": t, "run_id": RUN}
            for t in ("featured_channel", "recommendation", "comment_author")
        ]
        assert persist_edges(conn, edges) == 3
        assert _count(conn, "discovery_edges", "WHERE run_id = %s", (RUN,)) == 3

    def test_rewalk_backfills_the_id_instead_of_duplicating(self, conn):
        ref = "https://www.youtube.com/@rationalreminder"
        persist_edge(conn, {
            "source_channel_id": CH1, "target_channel_ref": ref,
            "edge_type": "featured_channel", "run_id": RUN,
        })
        persist_edge(conn, {
            "source_channel_id": CH1, "target_channel_id": CH2,
            "target_channel_ref": ref, "edge_type": "featured_channel", "run_id": RUN,
        })
        cur = conn.cursor()
        # Filter on the source too: uniqueness is per (source, ref, type), and
        # a real run may legitimately hold the same target ref reached from a
        # different channel.
        cur.execute(
            """SELECT target_channel_id FROM discovery_edges
               WHERE source_channel_id = %s AND target_channel_ref = %s""",
            (CH1, ref),
        )
        rows = cur.fetchall()
        cur.close()
        assert len(rows) == 1, "re-walking a channel must not duplicate its edges"
        assert rows[0][0] == CH2

    def test_batch_skips_malformed_without_losing_the_rest(self, conn):
        edges = [
            {"source_channel_id": CH1, "target_channel_ref": "https://x/1",
             "edge_type": "featured_channel", "run_id": RUN},
            {"source_channel_id": "", "target_channel_ref": "https://x/2",
             "edge_type": "featured_channel", "run_id": RUN},
            {"source_channel_id": CH1, "target_channel_ref": "https://x/3",
             "edge_type": "featured_channel", "run_id": RUN},
        ]
        assert persist_edges(conn, edges) == 2


class TestPersistChannelV3SQL:
    """persist_channel_v3 is v3's own version of the same gap this file
    exists for. A prior version built the INSERT column list from the
    `p_`-prefixed *parameter* names instead of the real column names
    (raised UndefinedColumn on every call), and separately — once that was
    fixed — turned out to need `INSERT ... ON CONFLICT DO UPDATE`'s
    candidate row to satisfy channels.title/discovery_method NOT NULL even
    though every real call only ever updates a row persist_channel()
    already created. Both were invisible to test_tools.py's MagicMock
    cursor, which accepts any SQL string. Real Postgres is the only thing
    that catches either.
    """

    def test_updates_an_existing_channel(self, conn):
        persist_channel(conn, {"channel_id": CH1, "title": "T", "discovery_method": "keyword"})
        persist_channel_v3(conn, CH1, RUN, {
            "country_code": "US", "country_source": "inferred_language",
            "evergreen_score": 72.5, "entertainment_score": 40.0,
        })
        cur = conn.cursor()
        cur.execute(
            "SELECT country_code, country_source, evergreen_score, entertainment_score, "
            "first_discovered_run_id, last_enriched_run_id "
            "FROM channels WHERE channel_id = %s", (CH1,),
        )
        row = cur.fetchone()
        cur.close()
        assert row == ("US", "inferred_language", 72.5, 40.0, RUN, RUN)

    def test_first_discovered_run_id_is_never_overwritten(self, conn):
        persist_channel(conn, {"channel_id": CH1, "title": "T", "discovery_method": "keyword"})
        persist_channel_v3(conn, CH1, "run-first", {"evergreen_score": 10.0})
        persist_channel_v3(conn, CH1, "run-second", {"evergreen_score": 20.0})
        cur = conn.cursor()
        cur.execute(
            "SELECT evergreen_score, first_discovered_run_id, last_enriched_run_id "
            "FROM channels WHERE channel_id = %s", (CH1,),
        )
        row = cur.fetchone()
        cur.close()
        assert row == (20.0, "run-first", "run-second")

    def test_empty_fields_is_a_no_op_not_a_crash(self, conn):
        persist_channel(conn, {"channel_id": CH1, "title": "T", "discovery_method": "keyword"})
        persist_channel_v3(conn, CH1, RUN, {})  # must not raise
        assert _count(conn, "channels", "WHERE channel_id = %s", (CH1,)) == 1

    def test_a_channel_id_with_no_row_is_a_silent_no_op(self, conn):
        """Never invents a half-populated row — a channel row is always
        created by persist_channel() first."""
        persist_channel_v3(conn, "UC_itest_nonexistent", RUN, {"evergreen_score": 5.0})
        assert _count(conn, "channels", "WHERE channel_id = %s", ("UC_itest_nonexistent",)) == 0


class TestPersistVideoV3SQL:
    def test_updates_an_existing_video(self, conn):
        persist_channel(conn, {"channel_id": CH1, "title": "T", "discovery_method": "keyword"})
        persist_video(conn, {"video_id": "itest_v1", "channel_id": CH1, "title": "V1"})
        persist_video_v3(conn, "itest_v1", {
            "title_char_count": 14, "title_has_number": True, "is_short": False,
        })
        cur = conn.cursor()
        cur.execute(
            "SELECT title_char_count, title_has_number, is_short "
            "FROM videos WHERE video_id = %s", ("itest_v1",),
        )
        row = cur.fetchone()
        cur.close()
        assert row == (14, True, False)

    def test_empty_fields_is_a_no_op_not_a_crash(self, conn):
        persist_channel(conn, {"channel_id": CH1, "title": "T", "discovery_method": "keyword"})
        persist_video(conn, {"video_id": "itest_v1", "channel_id": CH1, "title": "V1"})
        persist_video_v3(conn, "itest_v1", {})  # must not raise
        assert _count(conn, "videos", "WHERE video_id = %s", ("itest_v1",)) == 1


class TestFetchRunVideosSQL:
    """The client's added ask, after reviewing the sample file: a
    days_since_published column on the Videos sheet. Computed in SQL
    (NOW() - published_at) rather than stored, since it's a pure function
    of a value already on the row and would go stale the moment it was
    persisted."""

    def test_days_since_published_is_computed_from_published_at(self, conn):
        persist_channel(conn, {
            "channel_id": CH1, "title": "T", "discovery_method": "keyword",
            "subscriber_count": 100000,
        })
        persist_video(conn, {
            "video_id": "itest_v1", "channel_id": CH1, "title": "V1",
            "published_at": "2026-01-01T00:00:00Z",
        })
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO category_tags (entity_type, entity_id, tree_node_id, run_id) "
            "VALUES ('video', %s, 'root', %s)", ("itest_v1", RUN),
        )
        conn.commit()
        cur.close()

        rows = fetch_run_videos(RUN, 10)
        assert len(rows) == 1
        assert rows[0]["days_since_published"] is not None
        assert rows[0]["days_since_published"] > 0, (
            "a video published in the past must show a positive day count"
        )

    def test_null_published_at_leaves_days_since_published_null(self, conn):
        persist_channel(conn, {
            "channel_id": CH1, "title": "T", "discovery_method": "keyword",
            "subscriber_count": 100000,
        })
        persist_video(conn, {"video_id": "itest_v1", "channel_id": CH1, "title": "V1"})
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO category_tags (entity_type, entity_id, tree_node_id, run_id) "
            "VALUES ('video', %s, 'root', %s)", ("itest_v1", RUN),
        )
        conn.commit()
        cur.close()

        rows = fetch_run_videos(RUN, 10)
        assert len(rows) == 1
        assert rows[0]["days_since_published"] is None


class TestFetchRunChannelsSQL:
    """first_video_published_at is read straight from channels (persisted
    by resolve_first_video_date, which paginates the uploads playlist to
    its real last page) — NOT derived from MIN(videos.published_at), which
    would only ever reflect hydrate_metadata's 50-most-recent-videos
    sample. last_video_published_at has no such problem: the newest video
    in the sample genuinely IS the channel's most recent upload, so it's
    still read straight from the videos table. "Channel Creation Date" is
    a deliberately human-readable alias for channels.first_seen_at."""

    def test_first_video_published_at_comes_from_the_persisted_column(self, conn):
        persist_channel(conn, {
            "channel_id": CH1, "title": "T", "discovery_method": "keyword",
            "subscriber_count": 100000, "first_seen_at": "2020-05-01T00:00:00Z",
        })
        persist_channel_v3(conn, CH1, RUN, {
            "first_video_published_at": "2013-09-12T00:00:00Z",
        })
        persist_video(conn, {
            "video_id": "itest_v1", "channel_id": CH1, "title": "Newest",
            "published_at": "2026-06-01T00:00:00Z",
        })
        # A video older than the persisted first_video_published_at must
        # NOT leak in via last_video_published_at's MAX() — the two columns
        # come from different sources and must not cross-contaminate.
        persist_video(conn, {
            "video_id": "itest_v2", "channel_id": CH1, "title": "Older sample video",
            "published_at": "2024-01-01T00:00:00Z",
        })
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO category_tags (entity_type, entity_id, tree_node_id, run_id) "
            "VALUES ('channel', %s, 'root', %s)", (CH1, RUN),
        )
        conn.commit()
        cur.close()

        rows = fetch_run_channels(RUN)
        assert len(rows) == 1
        assert rows[0]["first_video_published_at"].year == 2013, (
            "must be the persisted true-first-video date, not MIN(videos.published_at)"
        )
        assert rows[0]["last_video_published_at"].year == 2026, "must be the latest sampled video's date"
        assert rows[0]["Channel Creation Date"].year == 2020, 'first_seen_at aliased to "Channel Creation Date"'

    def test_unresolved_first_video_date_is_null_not_a_crash(self, conn):
        persist_channel(conn, {
            "channel_id": CH1, "title": "T", "discovery_method": "keyword",
            "subscriber_count": 100000,
        })
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO category_tags (entity_type, entity_id, tree_node_id, run_id) "
            "VALUES ('channel', %s, 'root', %s)", (CH1, RUN),
        )
        conn.commit()
        cur.close()

        rows = fetch_run_channels(RUN)
        assert len(rows) == 1
        assert rows[0]["first_video_published_at"] is None
        assert rows[0]["last_video_published_at"] is None

    def test_total_video_count_comes_from_the_snapshot_not_the_sample(self, conn):
        """channel_snapshots.total_video_count is YouTube's real
        statistics.videoCount, persisted every run. The videos table only
        ever holds hydrate_metadata's capped sample (<=50 most recent
        uploads) — a channel with hundreds of real uploads but 2 hydrated
        sample rows must still report the real total, not 2."""
        persist_channel(conn, {
            "channel_id": CH1, "title": "T", "discovery_method": "keyword",
            "subscriber_count": 100000,
        })
        persist_channel_snapshot(conn, CH1, RUN, 100000, 5_000_000, 743)
        persist_video(conn, {
            "video_id": "itest_v1", "channel_id": CH1, "title": "Sample video 1",
            "published_at": "2026-06-01T00:00:00Z",
        })
        persist_video(conn, {
            "video_id": "itest_v2", "channel_id": CH1, "title": "Sample video 2",
            "published_at": "2024-01-01T00:00:00Z",
        })
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO category_tags (entity_type, entity_id, tree_node_id, run_id) "
            "VALUES ('channel', %s, 'root', %s)", (CH1, RUN),
        )
        conn.commit()
        cur.close()

        rows = fetch_run_channels(RUN)
        assert len(rows) == 1
        assert rows[0]["total_video_count"] == 743, (
            "must be the real YouTube total from channel_snapshots, not COUNT(videos)=2"
        )

    def test_missing_snapshot_gives_null_total_video_count_not_a_crash(self, conn):
        persist_channel(conn, {
            "channel_id": CH1, "title": "T", "discovery_method": "keyword",
            "subscriber_count": 100000,
        })
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO category_tags (entity_type, entity_id, tree_node_id, run_id) "
            "VALUES ('channel', %s, 'root', %s)", (CH1, RUN),
        )
        conn.commit()
        cur.close()

        rows = fetch_run_channels(RUN)
        assert len(rows) == 1
        assert rows[0]["total_video_count"] is None


class TestV3ColumnAllowlistsCoverWhatCallersWrite:
    """persist_channel_v3 and persist_video_v3 filter writes through a
    hardcoded column allowlist. A column missing from it is dropped in
    silence — the caller computes a value, the function reports success,
    and nothing lands. That is exactly how the entire v4 channel
    enrichment layer became a no-op: creator_authority read 'unknown'
    across all 8,493 channels because the column default was never
    overwritten, not because anything classified them.

    These tests read the real schema so a newly added column that nothing
    can write fails here rather than after a paid run.
    """

    def _schema_columns(self, table: str) -> set[str]:
        import re
        from pathlib import Path

        ddl = Path("src/db/schema.py").read_text()
        return set(re.findall(
            rf"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS ([a-z0-9_]+)", ddl))

    def _allowlist(self, func_name: str) -> set[str]:
        import inspect
        import re

        from src.tools import dedup

        src = inspect.getsource(getattr(dedup, func_name))
        block = re.search(r"v3_cols = \{(.*?)\}", src, re.S)
        assert block, f"could not find v3_cols in {func_name}"
        return set(re.findall(r'"([a-z0-9_]+)"', block.group(1)))

    def test_every_v4_channel_column_is_writable(self):
        missing = self._schema_columns("channels") - self._allowlist(
            "persist_channel_v3")
        # Columns written by other paths (plain UPDATEs) are legitimately absent.
        written_elsewhere = {
            "updated_at", "first_discovered_run_id", "last_enriched_run_id",
            "primary_niche_group_id", "is_us_market",
            # _mark_checked in extract_success_failure_factors.py: a plain
            # UPDATE, not persist_channel_v3, because it fires from inside
            # both the success path and an except block, and has to
            # succeed even when the surrounding persist call is what failed.
            "success_failure_factors_checked_at",
            # _mark_creator_authority_checked in populate_shared_fields.py --
            # same reasoning: a plain UPDATE that has to succeed even when
            # the persist call around it is what failed.
            "creator_authority_checked_at",
        }
        assert not (missing - written_elsewhere), (
            "channels columns nothing can write via persist_channel_v3: "
            f"{sorted(missing - written_elsewhere)}"
        )

    def test_video_description_and_raw_description_are_both_writable(self):
        """They are different fields — `description` is the channel's own
        text from the API, `video_description` the LLM's title gloss — and
        the raw one was dropped for the whole life of the dataset."""
        allow = self._allowlist("persist_video_v3")
        assert "description" in allow
        assert "video_description" in allow

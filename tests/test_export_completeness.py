"""The gate that has to pass before a workbook is handed over.

Every empty sheet and blank column this project shipped came from a node
that did not run, or ran against the wrong channels, with nothing between
it and the client noticing. The export was the last step, and it wrote
whatever happened to be in the table.

These assert the gate's contract rather than its plumbing: what counts as
complete, that it heals by re-running real nodes instead of inventing
values, that it stops rather than looping, and that a failed check never
costs the client the file itself.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.tools import export_completeness as gate


class TestTheSpecIsHonest:
    def test_every_check_names_its_producer_or_admits_it_cannot_be_filled(self):
        """A column with no node behind it is reported, never silently
        tolerated -- outlier_score is written during hydration and genuinely
        cannot be backfilled afterwards, and saying so is the point."""
        for check in gate.CHANNEL_CHECKS + gate.VIDEO_CHECKS:
            assert check.node is not None or check.note, check.column

    def test_thresholds_are_below_one_on_purpose(self):
        """A channel with no country signal has no country. Demanding 100%
        would make the gate cry wolf on every run and be switched off."""
        for check in gate.CHANNEL_CHECKS + gate.VIDEO_CHECKS:
            assert 0.0 < check.min_fill <= 1.0, check.column

    def test_the_columns_that_empty_a_whole_sheet_are_the_strictest(self):
        """primary_niche_id absent is three empty sheets and a graph full of
        "Unclassified" -- it cannot sit at the same threshold as a column
        whose absence costs one cell."""
        by_col = {c.column: c for c in gate.CHANNEL_CHECKS}
        assert by_col["primary_niche_id"].min_fill >= 0.95
        assert by_col["primary_topic"].min_fill >= 0.95

    def test_deterministic_columns_are_held_to_a_higher_bar_than_llm_ones(self):
        """channel_size_bucket is a function of subscriber_count -- nothing
        about it depends on a model answering, so a gap is a bug, not a
        judgement call."""
        by_col = {c.column: c for c in gate.CHANNEL_CHECKS}
        assert by_col["channel_size_bucket"].min_fill > by_col["secondary_topic"].min_fill

    def test_every_named_node_actually_exists(self):
        """A typo here is a column the gate reports forever and never fixes,
        because the healer looks up a node that is not there."""
        available = set(gate._nodes())
        for check in gate.CHANNEL_CHECKS + gate.VIDEO_CHECKS:
            if check.node:
                assert check.node in available, check.node


class TestTheSpecCoversTheWorkbook:
    """The gate is only as good as its declaration, and the declaration was
    short. run-3f649c9246a3 shipped six title-signal columns at 85% behind a
    COMPLETE report because VIDEO_CHECKS named three columns and the Videos
    sheet writes forty. Hand-maintained lists drift; this makes the drift a
    test failure instead of something a client finds."""

    def _videos_sheet_columns(self):
        """The columns fetch_run_videos actually selects, read off a real
        query rather than a second hand-written list that could drift too."""
        import re

        src = open("src/export.py", encoding="utf-8").read()
        start = src.index("def fetch_run_videos(")
        body = src[start:src.index("def fetch_run_niche_breakdown(")]
        select = body[body.index("SELECT DISTINCT"):body.index("FROM videos v")]
        select = "\n".join(
            line for line in select.splitlines()
            if not line.strip().startswith("--")
        )
        cols = set(re.findall(r"\bAS\s+([a-z_][a-z0-9_]*)", select))
        cols |= set(re.findall(r"\b(?:v|c|nt|ccm)\.([a-z_][a-z0-9_]*)", select))
        return cols

    def test_every_videos_column_is_either_checked_or_explicitly_waived(self):
        checked = {c.column for c in gate.VIDEO_CHECKS}
        waived = set(gate.UNCHECKED_VIDEO_COLUMNS)
        undeclared = self._videos_sheet_columns() - checked - waived
        assert not undeclared, (
            "these Videos columns have no fill threshold and no stated "
            f"reason to be exempt: {sorted(undeclared)}. Add a ColumnCheck, "
            "or an entry in UNCHECKED_VIDEO_COLUMNS saying why blank is "
            "the right answer for it."
        )

    def test_a_waiver_has_to_give_a_reason(self):
        for column, reason in gate.UNCHECKED_VIDEO_COLUMNS.items():
            assert reason.strip(), column

    def test_nothing_is_both_checked_and_waived(self):
        overlap = {c.column for c in gate.VIDEO_CHECKS} & set(gate.UNCHECKED_VIDEO_COLUMNS)
        assert not overlap, overlap

    def test_the_title_signals_are_held_to_the_deterministic_bar(self):
        """They are pure functions of the title. A gap is a node that did
        not reach the row, never a model declining to answer."""
        by_col = {c.column: c for c in gate.VIDEO_CHECKS}
        for col in ("title_word_count", "title_has_number", "title_is_question",
                    "title_capitalization", "title_emoji_count"):
            assert by_col[col].min_fill >= 0.98, col
            assert by_col[col].node == "extract_metadata_signals", col


class TestTheGateChecksWhatTheFileContains:
    """The bug behind the missing columns was not the thresholds at all: the
    gate audited the 15 channels the run discovered while the workbook was
    written from 9, a set that includes channels earlier runs found."""

    def test_it_asks_the_export_which_rows_those_are(self):
        with patch("src.export.workbook_rows", return_value=(
            [{"channel_id": "c1"}, {"channel_id": "c2"}],
            [{"video_id": "v1"}, {"video_id": "v2"}, {"video_id": "v3"}],
        )) as rows:
            ids, video_ids = gate._workbook_ids("run-x")

        rows.assert_called_once_with("run-x")
        assert ids == ["c1", "c2"]
        assert video_ids == ["v1", "v2", "v3"]

    def test_a_channel_an_earlier_run_discovered_is_still_audited(self):
        """The exact shape that shipped: the workbook carries a channel the
        run did not discover, so scoping the audit to first_discovered_run_id
        looks past the only rows that were short."""
        with patch("src.export.workbook_rows", return_value=(
            [{"channel_id": "mine"}, {"channel_id": "from-an-older-run"}], [],
        )):
            ids, _ = gate._workbook_ids("run-x")
        assert "from-an-older-run" in ids

    def test_healing_is_scoped_to_the_workbook_not_the_run(self):
        """A node handed only the run's own channels can never fill the row
        that was actually blank."""
        seen = {}

        def _node(state):
            seen.update(state)
            return {"node_logs": [{"input_summary": {"populated": 0}}]}

        with patch.object(gate, "_workbook_ids", return_value=(["mine", "stranger"], [])), \
             patch.object(gate, "_nodes", lambda: {"extract_metadata_signals": _node}):
            gate.heal("run-x", only={"extract_metadata_signals"})

        assert seen["scope_channel_ids"] == ["mine", "stranger"]

    def test_video_columns_are_counted_over_the_exported_videos(self):
        """Counting every video of every workbook channel would measure rows
        the sheet does not carry, and bill a heal for filling them."""
        src = open("src/tools/export_completeness.py", encoding="utf-8").read()
        body = src[src.index("for check in VIDEO_CHECKS:"):]
        body = body[:body.index("empty_tables") if "empty_tables" in body else 800]
        code = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        assert "v.video_id = ANY(%s)" in code
        assert "v.channel_id = ANY(%s)" not in code


class TestFindingArithmetic:
    def test_a_finding_knows_whether_it_passes(self):
        f = gate.Finding("x", "channels", 90, 100, 0.90, "n")
        assert f.rate == pytest.approx(0.90)
        assert f.ok

        f = gate.Finding("x", "channels", 89, 100, 0.90, "n")
        assert not f.ok

    def test_an_empty_table_is_not_a_failure_by_division(self):
        """0/0 is 'nothing to check', not 'everything missing' -- a run with
        no videos must not fail every video column."""
        f = gate.Finding("x", "videos", 0, 0, 0.90, "n")
        assert f.rate == 1.0
        assert f.ok


class TestHealingIsBounded:
    def test_a_node_reporting_no_progress_is_not_called_again(self):
        """The difference between finishing a backlog and the re-billing
        loops this project has already paid for twice."""
        calls = []

        def _stuck(state):
            calls.append(1)
            return {"node_logs": [{"input_summary": {"classified": 0}}]}

        with patch.object(gate, "_nodes", lambda: {"classify_channel": _stuck}), \
             patch.object(gate, "_workbook_ids", lambda *a, **k: (["c1"], ["v1"])):
            gate.heal("run-x", only={"classify_channel"})

        assert len(calls) == 1

    def test_a_node_making_progress_is_called_until_it_stops(self):
        counter = {"n": 0}

        def _drains(state):
            counter["n"] += 1
            done = counter["n"] >= 3
            return {"node_logs": [{"input_summary": {"classified": 0 if done else 50}}]}

        with patch.object(gate, "_nodes", lambda: {"classify_channel": _drains}), \
             patch.object(gate, "_workbook_ids", lambda *a, **k: (["c1"], ["v1"])):
            ran = gate.heal("run-x", only={"classify_channel"})

        assert counter["n"] == 3
        assert ran == ["classify_channel"]

    def test_the_wall_clock_budget_stops_a_long_backlog(self):
        """A call ceiling is the wrong guard: these nodes take a bounded
        batch each, so the calls needed scale with the backlog -- filling
        4,614 video descriptions takes 154 calls of describe_video_titles,
        and a fixed ceiling would give up partway and blame the node."""
        def _slow(state):
            return {"node_logs": [{"input_summary": {"described": 30}}]}

        with patch.object(gate, "_nodes", lambda: {"describe_video_titles": _slow}), \
             patch.object(gate, "_workbook_ids", lambda *a, **k: (["c1"], ["v1"])):
            ran = gate.heal("run-x", only={"describe_video_titles"}, budget_seconds=0)

        assert ran == [], "a spent budget must stop before the first call"

    def test_a_node_that_raises_does_not_abort_the_whole_gate(self):
        def _boom(state):
            raise RuntimeError("db down")

        def _fine(state):
            return {"node_logs": [{"input_summary": {"populated": 0}}]}

        with patch.object(gate, "_nodes", lambda: {
                "classify_channel": _boom,
                "populate_taxonomy_dimensions": _fine,
             }), \
             patch.object(gate, "_workbook_ids", lambda *a, **k: (["c1"], ["v1"])):
            ran = gate.heal("run-x")

        assert ran == []


class TestTheGateNeverCostsTheClientTheFile:
    def test_an_incomplete_report_still_returns(self):
        """A workbook that is 90% populated is worth handing over WITH its
        gaps named. Refusing to export would leave the client with nothing,
        which is strictly worse."""
        report = gate.Report(run_id="run-x")
        report.findings = [gate.Finding("primary_topic", "channels", 1, 100, 0.95, "n")]
        assert not report.complete
        assert "INCOMPLETE" in report.render()

    def test_a_crashing_gate_is_reported_not_raised(self):
        with patch.object(gate, "ensure_complete", side_effect=RuntimeError("boom")):
            report = gate.gate_before_export("run-x")
        assert report.unfixable
        assert "boom" in report.unfixable[0]

    def test_the_report_names_what_is_short_and_what_fixed_it(self):
        report = gate.Report(run_id="run-x")
        report.findings = [
            gate.Finding("primary_topic", "channels", 100, 100, 0.95, "n"),
            gate.Finding("video_description", "videos", 0, 10, 0.90,
                         "describe_video_titles"),
        ]
        report.healed = ["classify_channel"]
        out = report.render()
        assert "video_description" in out
        assert "describe_video_titles" in out
        assert "classify_channel" in out


class TestItRunsBeforeTheExport:
    def test_the_cli_gates_before_writing_the_workbook(self):
        """The whole point: the export stops being the last step."""
        src = open("src/cli.py").read()
        gate_at = src.index("gate_before_export(")
        export_at = src.index("export_excel(export_run_id")
        assert gate_at < export_at, "the gate must run before the workbook is written"


class TestEveryColumnOfEverySheet:
    """The declared checks are the columns with a node behind them -- the
    gaps this gate can close. They were never the whole workbook: three of
    the Videos sheet's forty columns, none of Niches, Success Factors or
    Failure Factors. The sweep measures the rest, so a column nobody thought
    to declare is still a column nobody ships empty.
    """

    def _audit_with(self, sheets):
        with patch("src.export.sheet_rows", return_value=sheets), \
             patch.object(gate, "_workbook_ids", return_value=(["c1"], ["v1"])), \
             patch.object(gate, "get_connection"), \
             patch.object(gate, "put_connection"):
            report = gate.Report(run_id="run-x")
            gate._sweep(report, already=set())
            return report

    def test_an_undeclared_column_is_measured_not_ignored(self):
        """The whole point. Nobody declared it, so nobody was watching it."""
        report = self._audit_with({
            "Channels": [{"nobody_declared_me": None}, {"nobody_declared_me": "x"}],
        })
        found = [f for f in report.findings if f.column == "nobody_declared_me"]
        assert found, "an undeclared column must still be checked"
        assert found[0].min_fill == gate._DEFAULT_MIN_FILL
        assert not found[0].ok, "1 of 2 is short of the default bar"

    def test_every_data_sheet_is_swept(self):
        sheets = {
            name: [{"col_x": None}]
            for name in ("Channels", "Videos", "Niches",
                         "Success Factors", "Failure Factors")
        }
        report = self._audit_with(sheets)
        assert {f.table for f in report.findings} == set(sheets)

    def test_a_waived_column_is_left_alone(self):
        report = self._audit_with({"Channels": [{"description": None}]})
        assert not [f for f in report.findings if f.column == "description"]

    def test_a_column_the_export_removes_is_not_flagged(self):
        """It is not in the file, so a fill rate on it measures nothing --
        and these are the columns the client asked to have removed."""
        from src.export import ALWAYS_DROPPED_COLUMNS

        report = self._audit_with({
            "Channels": [{c: None for c in ALWAYS_DROPPED_COLUMNS}],
        })
        assert report.findings == []

    def test_the_dropped_list_is_read_from_the_export_not_copied(self):
        """A copy drifts. Removing a column from the deliverable must not
        leave the gate complaining about it for the rest of time."""
        src = open("src/tools/export_completeness.py", encoding="utf-8").read()
        assert "from src.export import ALWAYS_DROPPED_COLUMNS" in src

    def test_a_declared_column_is_not_reported_twice(self):
        report = gate.Report(run_id="run-x")
        with patch("src.export.sheet_rows", return_value={
            "Videos": [{"video_description": "a"}],
        }):
            gate._sweep(report, already={("videos", "video_description")})
        assert report.findings == []

    def test_a_declared_columns_own_threshold_wins_over_the_default(self):
        """country_code sits at 0.70 because a channel with no country
        signal has none. The sweep must not quietly hold it to 0.98."""
        report = self._audit_with({
            "Channels": [{"country_code": "US"}] * 8 + [{"country_code": None}] * 2,
        })
        found = next(f for f in report.findings if f.column == "country_code")
        assert found.min_fill == 0.70
        assert found.ok, "8 of 10 clears its own bar"

    def test_it_sweeps_the_rows_not_the_finished_sheet(self):
        """An entirely empty column is removed from the workbook before it
        is written, so the emptiest column of all is the one that leaves no
        trace in the file. Reading the file back would never find it."""
        report = self._audit_with({
            "Channels": [{"quietly_empty": None} for _ in range(9)],
        })
        found = next(f for f in report.findings if f.column == "quietly_empty")
        assert found.filled == 0
        assert not found.ok

    def test_an_empty_string_counts_as_missing(self):
        report = self._audit_with({"Niches": [{"label": ""}, {"label": "x"}]})
        assert next(f for f in report.findings if f.column == "label").filled == 1


class TestWaiversAreAccountable:
    def test_every_waiver_on_every_sheet_gives_a_reason(self):
        for sheet, columns in gate.SHEET_WAIVERS.items():
            for column, reason in columns.items():
                assert reason.strip(), f"{sheet}.{column}"

    def test_the_default_bar_is_strict(self):
        """A column nobody classified is more likely to be one nobody
        noticed than one that is legitimately sparse."""
        assert gate._DEFAULT_MIN_FILL >= 0.95

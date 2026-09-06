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
             patch.object(gate, "_run_channel_ids", lambda *a, **k: ["c1"]), \
             patch.object(gate, "get_connection", lambda: None), \
             patch.object(gate, "put_connection", lambda c: None):
            gate.heal("run-x", only={"classify_channel"})

        assert len(calls) == 1

    def test_a_node_making_progress_is_called_until_it_stops(self):
        counter = {"n": 0}

        def _drains(state):
            counter["n"] += 1
            done = counter["n"] >= 3
            return {"node_logs": [{"input_summary": {"classified": 0 if done else 50}}]}

        with patch.object(gate, "_nodes", lambda: {"classify_channel": _drains}), \
             patch.object(gate, "_run_channel_ids", lambda *a, **k: ["c1"]), \
             patch.object(gate, "get_connection", lambda: None), \
             patch.object(gate, "put_connection", lambda c: None):
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
             patch.object(gate, "_run_channel_ids", lambda *a, **k: ["c1"]), \
             patch.object(gate, "get_connection", lambda: None), \
             patch.object(gate, "put_connection", lambda c: None):
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
             patch.object(gate, "_run_channel_ids", lambda *a, **k: ["c1"]), \
             patch.object(gate, "get_connection", lambda: None), \
             patch.object(gate, "put_connection", lambda c: None):
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

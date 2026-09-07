"""populate_crime_metadata only shows itself on a run that has crime in it.

Every column this node writes is a crime case-file field, and the query
that finds its work is filtered on ``parent_category = 'crime'``. On a
technology run it therefore ran, found nothing, and reported itself --
putting "Filling case details" into the activity list of a run about
laptops, where it reads as a bug in the pipeline rather than a step that
correctly had nothing to do.

The console builds its activity list from the NodeLogs a run emits, so
these assert the node's silence rather than any console filtering: a node
that does not log does not appear, and there is no second place for the
rule to drift out of sync with.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import src.nodes.populate_crime_metadata as mod


def _conn_with_gate(found: bool):
    """A connection whose scope check answers `found`."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (1,) if found else None
    return conn


def _run(state, conn, llm=None):
    with (
        patch.object(mod, "get_connection", return_value=conn),
        patch.object(mod, "put_connection"),
        patch.object(mod, "complete_tier", side_effect=llm or (lambda *a, **k: {"content": "[]"})),
    ):
        return mod.populate_crime_metadata(state)


class TestTheStepStaysOffNonCrimeRuns:
    def test_a_run_with_no_crime_channels_emits_no_node_log(self):
        """No NodeLog is the whole mechanism -- the activity list is built
        from them, so this is what keeps the step off a technology run."""
        out = _run({"thread_id": "t", "run_id": "r"}, _conn_with_gate(False))
        assert out == {}

    def test_skipping_costs_nothing(self):
        """It should not reach the model, and it should hand the connection
        back rather than holding one for a run it is not participating in."""
        conn = _conn_with_gate(False)
        with (
            patch.object(mod, "get_connection", return_value=conn),
            patch.object(mod, "put_connection") as released,
            patch.object(mod, "complete_tier") as llm,
        ):
            mod.populate_crime_metadata({"thread_id": "t", "run_id": "r"})

        llm.assert_not_called()
        released.assert_called_once_with(conn)


class TestTheStepStillAppearsOnCrimeRuns:
    def test_a_crime_run_reports_itself(self):
        conn = _conn_with_gate(True)
        conn.cursor.return_value.fetchall.return_value = []
        out = _run({"thread_id": "t", "run_id": "r"}, conn)
        assert out.get("node_logs"), "a crime run must still show the step"

    def test_a_crime_run_with_nothing_left_to_do_still_reports_itself(self):
        """The skip is about the run's category, not about whether there
        happens to be work outstanding. A crime run whose case rows are
        already filled did run this step, and hiding it would misrepresent
        the pipeline."""
        conn = _conn_with_gate(True)
        conn.cursor.return_value.fetchall.return_value = []
        out = _run({"thread_id": "t", "run_id": "r"}, conn)
        log = out["node_logs"][0]
        assert log["node_name"] == "populate_crime_metadata"


class TestTheScopeCheckItself:
    def test_it_asks_about_the_runs_own_channels(self):
        conn = _conn_with_gate(True)
        mod._run_has_crime_channels(conn, {"discovered_channel_ids": ["c1", "c2"]})
        sql, params = conn.cursor.return_value.__enter__.return_value.execute.call_args[0]
        assert "parent_category = 'crime'" in sql
        assert "c.channel_id = ANY(%s)" in sql, "must not ask about the whole table"
        assert sorted(params[0]) == ["c1", "c2"]

    def test_a_run_owning_no_channels_has_no_crime_channels(self):
        """An empty scope selects nothing, which is the correct answer and
        not the 'unrestricted' one -- the distinction this codebase has
        already been bitten by."""
        conn = _conn_with_gate(False)
        assert not mod._run_has_crime_channels(conn, {"discovered_channel_ids": []})

    def test_a_broken_check_runs_the_node_rather_than_skipping_it(self):
        """Being wrong this way costs one no-op node. Being wrong the other
        way drops case metadata from a real crime run's workbook."""
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("db")
        assert mod._run_has_crime_channels(conn, {"discovered_channel_ids": ["c1"]}) is True


class TestTheConsoleSideOfIt:
    def test_the_step_keeps_its_own_name(self):
        """It is only ever seen on a crime run now, so it can say what it
        actually does instead of being generalised for an audience that
        will never see it."""
        src = open("web/src/pages/LiveRunsPage.tsx", encoding="utf-8").read()
        assert "populate_crime_metadata: 'Filling case details'" in src

    def test_a_locked_depth_names_every_provider_it_is_short_at(self):
        """A run spends at two providers and a depth can be short at both.
        Rendering only blockers[0] meant topping up OpenRouter cleared the
        message while the depth stayed locked on Bright Data, with nothing
        on screen saying why."""
        src = open("web/src/pages/NewRunPage.tsx", encoding="utf-8").read()
        code = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("//")
        )
        assert "tier.blockers.slice(1).map" in code, (
            "the depth card must render every blocker, not just the first"
        )
        assert "tier.warnings.map" in code, (
            "an unread balance must be surfaced too -- it does not lock a "
            "depth, so nothing else would tell the client it went unchecked"
        )

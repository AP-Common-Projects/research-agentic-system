"""The wall-clock ceiling, at every depth it is enforced.

The console names each depth by duration, so duration has to be a real
ceiling. Enforcing it in one place was not enough to make it precise:
checked only in check_saturation -- which runs once per branch cycle, after
discovery, hydration and classification -- a "30-minute" tier stopped at
whatever the first full cycle happened to cost. Measured at 55+ minutes on
a real run, and the overshoot grows with the tier.

So it is enforced at three depths, and each is asserted here:

  admission   an expensive node no-ops rather than starting work that
              cannot land (and, for Bright Data, cannot be paid for twice)
  iteration   the two loops that run for minutes yield near the limit
  termination check_saturation converts it into the clean budget-exhausted
              exit, so a deadline still produces a workbook
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.config import HarnessConfig
from src.tools import deadline as dl


def _cfg(seconds: int):
    """A config object shaped like the one get_config() returns."""
    return type("C", (), {"harness": HarnessConfig(run_deadline_seconds=seconds)})()


class TestDeadlineArithmetic:
    def test_uncapped_by_default_so_a_bare_cli_run_is_unaffected(self):
        with patch.object(dl, "get_config", lambda: _cfg(0)):
            assert dl.deadline_seconds() == 0
            assert dl.remaining_seconds() is None
            assert dl.passed() is False
            assert dl.would_exceed(10_000) is False

    def test_remaining_counts_down_and_never_goes_negative(self):
        with patch.object(dl, "get_config", lambda: _cfg(100)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 140.0):
            assert dl.remaining_seconds() == 0.0
            assert dl.passed() is True

    def test_not_passed_while_time_remains(self):
        with patch.object(dl, "get_config", lambda: _cfg(100)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 40.0):
            assert dl.remaining_seconds() == pytest.approx(60.0)
            assert dl.passed() is False

    def test_would_exceed_is_about_fitting_not_about_having_passed(self):
        """Admission control's whole point: 60s left is not passed, but it
        cannot host a five-minute snapshot."""
        with patch.object(dl, "get_config", lambda: _cfg(100)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 40.0):
            assert dl.passed() is False
            assert dl.would_exceed(300) is True
            assert dl.would_exceed(30) is False

    def test_an_unreadable_config_leaves_the_run_uncapped(self):
        """A config that cannot be read must not terminate a run that was
        going to succeed -- uncapped is the pre-existing behaviour."""
        def _boom():
            raise RuntimeError("no config")

        with patch.object(dl, "get_config", _boom):
            assert dl.deadline_seconds() == 0
            assert dl.passed() is False


class TestAdmissionControlOnDiscovery:
    """The five Bright Data fan-out nodes commit money at their trigger and
    then poll for minutes. Starting one past the deadline buys records the
    run will never use and still bills for them."""

    def _guarded_node(self):
        from src.graph import _guarded

        calls = []

        async def _node(state):
            calls.append(state)
            return {"keyword_search_done": True}

        return _guarded(_node, "keyword_search"), calls

    def test_a_discovery_node_does_not_start_past_the_deadline(self):
        node, calls = self._guarded_node()
        with patch.object(dl, "get_config", lambda: _cfg(100)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 200.0):
            out = asyncio.run(node({"thread_id": "t"}))

        assert calls == [], "the node body must not run"
        assert out["keyword_search_done"] is True, "but the branch must still complete"
        assert out["node_logs"][0]["input_summary"]["skipped"] == "run_deadline_seconds"

    def test_a_discovery_node_runs_normally_inside_the_deadline(self):
        node, calls = self._guarded_node()
        with patch.object(dl, "get_config", lambda: _cfg(100)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 10.0):
            out = asyncio.run(node({"thread_id": "t"}))

        assert len(calls) == 1
        assert out == {"keyword_search_done": True}

    def test_an_uncapped_run_is_never_skipped(self):
        node, calls = self._guarded_node()
        with patch.object(dl, "get_config", lambda: _cfg(0)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 10_000_000.0):
            asyncio.run(node({"thread_id": "t"}))
        assert len(calls) == 1


class TestClassifierYieldsAtTheDeadline:
    """One LLM call per channel, measured at ~21 minutes per batch of 50.
    Checked per channel so the run yields within ~25 seconds of its limit
    rather than at the batch's own natural end."""

    def test_the_loop_stops_and_says_why(self):
        from src.nodes import classify_channel as mod

        src = open(mod.__file__).read()
        # Asserted against the source rather than by running the node, which
        # needs Postgres and an LLM: what matters is that the check sits
        # INSIDE the per-channel loop, not before or after it.
        loop_start = src.index("for ch_id in eligible:")
        try_start = src.index("try:", loop_start)
        between = src[loop_start:try_start]
        assert "run_deadline.research_passed()" in between, (
            "the deadline check must be inside the per-channel loop"
        )
        assert "stopped_on" in src, "a truncated pass must say why in its log"


class TestBrightDataAbandonsAtTheDeadline:
    def test_the_poll_loop_checks_the_run_deadline(self):
        from src.tools import bright_data as mod

        src = open(mod.__file__).read()
        loop = src[src.index("while True:"):src.index("rows = await self._fetch")]
        assert "run_deadline.research_passed()" in loop, (
            "a snapshot can poll for minutes; without this the run overshoots "
            "by however long the collector takes"
        )

    def test_a_trigger_is_not_fired_past_the_deadline(self):
        """The POST is the moment money is committed."""
        from src.tools import bright_data as mod

        src = open(mod.__file__).read()
        head = src[src.index("dataset_id = self._dataset_ids[collector]"):src.index("async with self._semaphore")]
        assert "run_deadline.research_passed()" in head


class TestHydrationYieldsAtTheDeadline:
    """Measured at ~16 minutes for 285 channels, and it is not part of the
    _guarded fan-out, so without a check here a run that hits its deadline
    during discovery still spends that whole time hydrating before anything
    notices -- most of a short tier's entire window."""

    def test_the_loop_stops_and_says_why(self):
        # `from src.tools import hydrate_metadata` binds the FUNCTION of that
        # name, not the module -- go through sys.modules for the file.
        import sys

        import src.tools.hydrate_metadata  # noqa: F401

        src = open(sys.modules["src.tools.hydrate_metadata"].__file__).read()
        loop_start = src.index("for ch in channels:")
        body = src[loop_start:src.index("ch[\"discovery_method\"] =", loop_start)]
        assert "run_deadline.research_passed()" in body, (
            "the deadline check must be inside the per-channel loop"
        )
        assert '"stopped_on"' in src, "a truncated pass must say why in its log"


class TestEveryExpensiveNodeIsGuarded:
    """The first attempt guarded only the nodes measured slow on ONE run.
    The next run then spent 26 of its 33 overshoot minutes in two nodes
    nobody had guarded -- score_thumbnail_signals (22 min, since removed)
    and resolve_first_video_date (4 min). Guarding by anecdote does not
    converge; this pins the set.
    """

    def test_the_declared_skippable_set_is_actually_wired(self):
        import src.graph as mod

        src = open(mod.__file__).read()
        for name in mod.DEADLINE_SKIPPABLE_NODES:
            assert f'_deadline_aware({name}, "{name}")' in src, (
                f"{name} is declared skippable but not wrapped"
            )

    def test_the_terminal_path_is_never_skippable(self):
        """These are how a deadline-stopped run still produces a workbook."""
        import src.graph as mod

        for name in (
            "check_saturation", "cluster_branch", "compact_branch",
            "assign_cohorts", "finalize_dataset", "select_next_node",
        ):
            assert name not in mod.DEADLINE_SKIPPABLE_NODES

    def test_nodes_that_yield_internally_are_not_skipped_wholesale(self):
        """hydrate_metadata and classify_channel check the deadline inside
        their own loops, keeping the work already paid for. Skipping them
        outright would throw that away."""
        import src.graph as mod

        assert "hydrate_metadata" not in mod.DEADLINE_SKIPPABLE_NODES
        assert "classify_channel" not in mod.DEADLINE_SKIPPABLE_NODES

    def test_a_skippable_node_no_ops_past_the_deadline(self):
        from src.graph import _deadline_aware

        calls = []
        node = _deadline_aware(lambda state: calls.append(1) or {"ok": True}, "resolve_first_video_date")

        with patch.object(dl, "get_config", lambda: _cfg(100)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 200.0):
            out = asyncio.run(node({"thread_id": "t"}))

        assert calls == []
        assert out["node_logs"][0]["input_summary"]["skipped"] == "run_deadline_seconds"

    def test_a_skippable_node_runs_normally_inside_the_deadline(self):
        from src.graph import _deadline_aware

        calls = []
        node = _deadline_aware(lambda state: calls.append(1) or {"ok": True}, "resolve_first_video_date")

        with patch.object(dl, "get_config", lambda: _cfg(100)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 10.0):
            out = asyncio.run(node({"thread_id": "t"}))

        assert calls == [1]
        assert out == {"ok": True}


class TestChannelCapMatchesEnrichmentCapacity:
    """Discovery volume and enrichment capacity were unrelated numbers.

    classify_channel describes 50 channels per cycle and Glimpse gets one
    cycle, so 50 was its hard ceiling -- while its record budget found 273.
    The workbook that came out had 95% of its classification columns empty
    (category, sub_niche, primary_topic, geography_focus, and eight more),
    not because anything failed but because the run was asked to find four
    times what it could ever describe.
    """

    def test_every_tier_declares_a_cap(self):
        from src.api.depth import TIERS

        for tier in TIERS:
            cap = tier.governors["MAX_CHANNELS_PER_RUN"]
            assert cap == tier.max_channels
            assert cap >= 20, tier.id

    def test_the_cap_grows_with_the_window(self):
        from src.api.depth import TIERS

        caps = [t.governors["MAX_CHANNELS_PER_RUN"] for t in TIERS]
        assert caps == sorted(caps), caps

    def test_the_shortest_tier_is_not_asked_for_more_than_it_can_finish(self):
        """The original failure: a tier sized to DISCOVER several times what
        it could ever describe. The cap now comes from the measured
        end-to-end cost of a channel, so it is what the window can finish."""
        from src.api.depth import get_tier

        sample = get_tier("sample")
        assert sample.governors["MAX_CHANNELS_PER_RUN"] <= 50

    def test_discovery_stops_once_the_cap_is_reached(self):
        from src.graph import _guarded

        calls = []

        async def _node(state):
            calls.append(state)
            return {"keyword_search_done": True}

        node = _guarded(_node, "keyword_search")
        cfg = type("C", (), {"harness": HarnessConfig(max_channels_per_run=40)})()

        with patch.object(dl, "get_config", lambda: _cfg(0)), \
             patch("src.config.get_config", lambda: cfg):
            out = asyncio.run(node({
                "thread_id": "t",
                "discovered_channel_ids": [f"c{i}" for i in range(40)],
            }))

        assert calls == []
        assert out["node_logs"][0]["input_summary"]["skipped"] == "max_channels_per_run"

    def test_discovery_continues_below_the_cap(self):
        from src.graph import _guarded

        calls = []

        async def _node(state):
            calls.append(state)
            return {"keyword_search_done": True}

        node = _guarded(_node, "keyword_search")
        cfg = type("C", (), {"harness": HarnessConfig(max_channels_per_run=40)})()

        with patch.object(dl, "get_config", lambda: _cfg(0)), \
             patch("src.config.get_config", lambda: cfg):
            asyncio.run(node({
                "thread_id": "t",
                "discovered_channel_ids": [f"c{i}" for i in range(10)],
            }))

        assert len(calls) == 1

    def test_zero_means_uncapped_for_a_bare_cli_run(self):
        from src.graph import _guarded

        calls = []

        async def _node(state):
            calls.append(state)
            return {"keyword_search_done": True}

        node = _guarded(_node, "keyword_search")
        cfg = type("C", (), {"harness": HarnessConfig(max_channels_per_run=0)})()

        with patch.object(dl, "get_config", lambda: _cfg(0)), \
             patch("src.config.get_config", lambda: cfg):
            asyncio.run(node({
                "thread_id": "t",
                "discovered_channel_ids": [f"c{i}" for i in range(10_000)],
            }))

        assert len(calls) == 1


class TestAGovernorStopStillProducesAFinishedWorkbook:
    """A governor means "stop looking", not "skip the deliverable".

    Both routers used to send a terminal action straight to
    finalize_dataset, jumping extract_success_failure_factors,
    describe_video_titles, populate_taxonomy_dimensions,
    populate_crime_metadata, populate_shared_fields and assign_cohorts in
    one hop. Governors are the NORMAL way a tiered run ends, so every
    console run shipped a workbook with the Niches, Success Factors and
    Failure Factors sheets empty and the taxonomy and cohort columns blank
    -- after paying for the discovery that filled the rest of it.
    """

    def test_compaction_routes_a_governor_stop_into_the_write_up(self):
        from src.graph import route_after_compaction

        assert route_after_compaction({"next_action": "budget_exhausted"}) == [
            "extract_success_failure_factors"
        ]

    def test_selection_routes_every_terminal_action_into_the_write_up(self):
        from src.graph import route_after_select, _TERMINAL_ACTIONS

        for action in _TERMINAL_ACTIONS:
            assert route_after_select({"next_action": action}) == [
                "extract_success_failure_factors"
            ], action

    def test_neither_router_can_reach_finalize_directly(self):
        """finalize_dataset is the END of the write-up chain, never a
        shortcut around it."""
        from src.graph import route_after_compaction, route_after_select

        for action in ("budget_exhausted", "all_done"):
            assert "finalize_dataset" not in route_after_compaction({"next_action": action})
            assert "finalize_dataset" not in route_after_select({"next_action": action})

    def test_the_write_up_chain_is_never_deadline_skippable(self):
        """It was, briefly, which would have reintroduced the empty sheets
        by a different route -- the run reaches the chain and every node in
        it no-ops."""
        import src.graph as mod

        for node in (
            "extract_success_failure_factors", "describe_video_titles",
            "populate_taxonomy_dimensions", "populate_crime_metadata",
            "populate_shared_fields", "assign_cohorts",
        ):
            assert node not in mod.DEADLINE_SKIPPABLE_NODES, node

    def test_research_stops_early_enough_to_leave_the_write_up_room(self):
        """The write-up is paid for out of the same window, so research has
        to yield before the hard ceiling rather than at it."""
        assert 0 < dl.RESEARCH_SHARE < 1

        with patch.object(dl, "get_config", lambda: _cfg(1000)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 850.0):
            assert dl.research_passed() is True, "research should have yielded"
            assert dl.passed() is False, "but the run still has time to finish up"

    def test_research_share_does_not_apply_to_an_uncapped_run(self):
        with patch.object(dl, "get_config", lambda: _cfg(0)), \
             patch.object(dl, "run_elapsed_seconds", lambda: 10_000_000.0):
            assert dl.research_passed() is False


class TestTerminationStillExports:
    def test_the_terminal_path_is_not_subject_to_admission_control(self):
        """check_saturation, compact_branch and finalize_dataset must run
        even past the deadline -- they are how the run exports what it
        reached. Only the discovery fan-out is wrapped in _guarded."""
        from src import graph as mod

        src = open(mod.__file__).read()
        guarded = {
            line.split('"')[1]
            for line in src.splitlines()
            if "add_node(" in line and "_guarded(" in line
        }
        assert guarded == {
            "keyword_search", "graph_walk", "breakout_scanner",
            "underperformer_discovery", "new_channel_discovery",
        }
        for terminal in ("check_saturation", "compact_branch", "finalize_dataset"):
            assert terminal not in guarded


class TestTheWriteUpChainIsBoundedToo:
    """research_passed() bounded discovery; nothing bounded what followed
    it. Every enrichment and write-up node ran to completion however long
    that took, which is how a one-hour crime run took 2h34m -- research
    yielded on time at 48 minutes and the write-up then ran 1h45m."""

    def test_it_stops_the_write_up_once_the_window_is_spent(self, monkeypatch):
        from src.tools import deadline as d

        monkeypatch.setattr(d, "passed", lambda: True)
        assert d.writeup_passed({"run_id": "r"}) is True

    def test_it_does_not_stop_it_before(self, monkeypatch):
        from src.tools import deadline as d

        monkeypatch.setattr(d, "passed", lambda: False)
        assert d.writeup_passed({"run_id": "r"}) is False

    def test_healing_is_exempt(self, monkeypatch):
        """The export gate re-runs these very nodes AFTER the deadline, on
        purpose, to fill what the run ran out of time for. A node that just
        asked passed() would make the completeness gate a no-op and trade
        every missing column for the schedule."""
        from src.tools import deadline as d

        monkeypatch.setattr(d, "passed", lambda: True)
        assert d.writeup_passed({"healing": True}) is False

    def test_the_gate_actually_sets_that_flag(self):
        """Without it the two mechanisms cancel each other out silently."""
        src = open("src/tools/export_completeness.py", encoding="utf-8").read()
        assert '"healing": True' in src

    def test_every_write_up_node_checks_it(self):
        """One unguarded node is enough to reinstate the overrun."""
        import pathlib

        expected = [
            "populate_taxonomy_dimensions", "populate_shared_fields",
            "populate_crime_metadata", "describe_video_titles",
            "extract_success_failure_factors",
        ]
        for name in expected:
            code = pathlib.Path(f"src/nodes/{name}.py").read_text(encoding="utf-8")
            code = "\n".join(
                l for l in code.splitlines() if not l.strip().startswith("#")
            )
            assert "run_deadline.writeup_passed(state)" in code, name

    def test_it_is_checked_inside_the_loop_not_only_at_entry(self):
        """Admission control alone bounds overshoot to a whole node, which
        for these is the thing that took the hour."""
        import pathlib

        code = pathlib.Path(
            "src/nodes/populate_taxonomy_dimensions.py"
        ).read_text(encoding="utf-8")
        loop_at = code.index("for ch_id, title, desc, fmt, nid, nname, category in eligible:")
        assert code.index("run_deadline.writeup_passed(state)") > loop_at


class TestTheTierCapReachesEnrichment:
    """hydrate_metadata applied MAX_CHANNELS_PER_RUN where the API cost is.
    Nothing applied it to the enrichment scope, so a sample run that
    hydrated 21 channels under a cap of 21 went on to enrich 206."""

    def _scope(self, cap, discovered, hydrated=()):
        from unittest.mock import MagicMock, patch

        from src.tools import run_scope

        cfg = MagicMock()
        cfg.harness.max_channels_per_run = cap
        with patch("src.config.get_config", return_value=cfg):
            return run_scope.channel_scope({
                "discovered_channel_ids": list(discovered),
                "hydrated_channel_ids": set(hydrated),
            })

    def test_the_scope_is_trimmed_to_the_cap(self):
        got = self._scope(cap=3, discovered=[f"c{i}" for i in range(20)])
        assert len(got) == 3

    def test_hydrated_channels_are_kept_first(self):
        """They are the cap's own selection -- biggest first -- and the ones
        with data worth enriching."""
        got = self._scope(cap=2, discovered=[f"c{i}" for i in range(20)],
                          hydrated={"c17", "c18"})
        assert set(got) == {"c17", "c18"}

    def test_it_fills_the_remaining_room_deterministically(self):
        """Two processes given the same state must pick the same channels."""
        a = self._scope(cap=4, discovered=[f"c{i}" for i in range(20)], hydrated={"c9"})
        b = self._scope(cap=4, discovered=list(reversed([f"c{i}" for i in range(20)])),
                        hydrated={"c9"})
        assert a == b
        assert "c9" in a and len(a) == 4

    def test_an_uncapped_run_is_untouched(self):
        got = self._scope(cap=0, discovered=[f"c{i}" for i in range(20)])
        assert len(got) == 20

    def test_a_scope_under_the_cap_is_untouched(self):
        got = self._scope(cap=50, discovered=["c1", "c2"])
        assert sorted(got) == ["c1", "c2"]

    def test_an_unreadable_config_does_not_narrow_the_run(self):
        from unittest.mock import patch

        from src.tools import run_scope

        with patch("src.config.get_config", side_effect=RuntimeError("no config")):
            got = run_scope.channel_scope(
                {"discovered_channel_ids": [f"c{i}" for i in range(20)]}
            )
        assert len(got) == 20, "a config failure must not silently shrink a run"

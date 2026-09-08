"""A Standard or Deep run has to be able to finish.

run-bc5226fb2e06, a Standard education run on the "education" topic,
died after 2h24m with GraphRecursionError and produced no export at all.
The console had promised up to 100 channels, 5,400 videos and ~5h 48m.

Two faults, and either alone would have wasted the run.

  hydrate_metadata trims to max_channels_per_run, and the cap filled on the
  FIRST hydration: 100 hydrated, 565 trimmed. The next nineteen rounds each
  logged "no new channels to hydrate" while keyword search, graph walk and
  the breakout scanner kept buying channels that could never be hydrated --
  827 Bright Data records and 1,251 quota units for nothing. No stop
  condition recognised a full cap as done.

  The recursion limit was a flat 200 for every depth, and the deeper tiers
  are configured to need more: Sample is 1 branch x 1 round, Standard 4 x 4,
  Deep 8 x 8. The ceiling was reached before any stop condition could be.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.api import depth as depth_mod
from src.tools import saturation as sat


class TestAFullChannelCapEndsTheResearch:
    def _check(self, cap, hydrated, **overrides):
        cfg = MagicMock()
        cfg.harness.run_deadline_seconds = 0
        cfg.harness.budget_limit_usd = 0
        cfg.harness.brightdata_record_budget = 0
        cfg.harness.youtube_quota_budget_per_run = 0
        cfg.harness.max_channels_per_run = cap
        cfg.harness.saturation_novelty_threshold = 0.1
        cfg.harness.saturation_consecutive_window = 2
        # Set as real numbers so the checks BELOW the breakers can run --
        # a MagicMock compares as neither greater nor less than an int.
        cfg.harness.max_rounds_per_branch = 4
        cfg.harness.max_tree_depth = 2
        cfg.harness.branch_lineage_budget_enabled = False
        for k, v in overrides.items():
            setattr(cfg.harness, k, v)

        state = {
            "hydrated_channel_ids": {f"c{i}" for i in range(hydrated)},
            "tree": {"n1": {"status": "active"}},
            "active_node_id": "n1",
        }
        with patch.object(sat, "get_config", return_value=cfg), \
             patch.object(sat, "run_elapsed_seconds", return_value=0):
            return sat.check_saturation(state)

    def test_a_full_cap_stops_the_run(self):
        """Nineteen rounds of discovery ran after the cap was full. Not one
        of them could hydrate a channel."""
        out = self._check(cap=100, hydrated=100)
        node = out["node_logs"][0]["input_summary"]
        assert node.get("governor") == "max_channels_per_run", node

    def test_it_stops_when_the_cap_is_exceeded_too(self):
        out = self._check(cap=100, hydrated=140)
        assert out["node_logs"][0]["input_summary"]["governor"] == "max_channels_per_run"

    def test_a_run_with_room_left_carries_on(self):
        out = self._check(cap=100, hydrated=40)
        assert out.get("next_action") != "budget_exhausted"

    def test_an_uncapped_run_is_unaffected(self):
        """A bare CLI run sets no cap, and used to work exactly because of
        that -- it must not start stopping now."""
        out = self._check(cap=0, hydrated=5000)
        assert out.get("next_action") != "budget_exhausted"

    def test_it_exits_the_same_way_the_other_ceilings_do(self):
        """force-saturate, compact, finalize, export what the run reached --
        not an abort, which is what the recursion limit gave instead."""
        out = self._check(cap=10, hydrated=10)
        assert out.get("next_action") == "budget_exhausted"
        assert out["tree"]["n1"]["saturation_reason"] == "governor:max_channels_per_run"


class TestTheRecursionLimitFitsTheTierItGoverns:
    def test_every_tier_can_run_its_own_loop(self):
        """The arithmetic the flat 200 failed: a tier must be allowed at
        least as many supersteps as its branches and rounds will cost."""
        for tier in depth_mod.TIERS:
            rounds = (tier.governors["MAX_BRANCHES"]
                      * tier.governors["MAX_ROUNDS_PER_BRANCH"])
            needed = rounds * depth_mod._SUPERSTEPS_PER_ROUND
            assert tier.recursion_limit > needed, (
                f"{tier.id}: {rounds} rounds need more than "
                f"{tier.recursion_limit} supersteps"
            )

    def test_the_deeper_tiers_get_more_than_the_shallow_one(self):
        limits = [t.recursion_limit for t in depth_mod.TIERS]
        assert limits == sorted(limits)
        assert limits[-1] > limits[0], "Deep must not share Sample's ceiling"

    def test_standard_and_deep_both_exceed_the_old_flat_limit(self):
        """Both were configured to need more than 200 and given exactly
        200, which is why neither could finish."""
        by_id = {t.id: t for t in depth_mod.TIERS}
        assert by_id["standard"].recursion_limit > 200
        assert by_id["deep"].recursion_limit > 200

    def test_sample_keeps_the_limit_it_already_worked_at(self):
        by_id = {t.id: t for t in depth_mod.TIERS}
        assert by_id["sample"].recursion_limit == 200

    def test_the_run_is_actually_given_it(self):
        """A limit computed and not handed over would change nothing."""
        for tier in depth_mod.TIERS:
            assert tier.governors["GRAPH_RECURSION_LIMIT"] == tier.recursion_limit

    def test_the_graph_reads_that_governor(self):
        src = open("src/graph.py", encoding="utf-8").read()
        assert "graph_recursion_limit" in src

    def test_it_is_a_real_config_field(self):
        """Governors reach a run as environment variables, so a name
        HarnessConfig does not read is a governor that does nothing."""
        from src.config import HarnessConfig

        assert "graph_recursion_limit" in HarnessConfig.model_fields


class TestTheCapIsSpreadAcrossBranches:
    """The cap is what a tier can afford to enrich; the branches are what it
    is supposed to cover. Taking whatever the first discovery round found
    meant those two never met.

    run-bc5226fb2e06 filled all 100 slots on its FIRST hydration and then
    visited four more branches -- online-learning, test-prep, k12-tutorials,
    trivia-entertainment -- none of which could contribute a single channel.
    Standard promises "enough channels in each sub-niche for the comparisons
    the workbook is built for", and delivered one sub-niche.
    """

    def _room(self, cap, branches, already):
        """The room the trim allows this round, as hydrate_metadata computes
        it. Read from the source so the test cannot drift from the code."""
        share = cap if branches <= 1 else max(1, cap // branches)
        return max(0, min(cap - already, share))

    def test_one_round_cannot_take_the_whole_cap(self):
        assert self._room(cap=100, branches=4, already=0) == 25

    def test_four_branches_together_fill_it(self):
        got, already = [], 0
        for _ in range(4):
            room = self._room(100, 4, already)
            got.append(room)
            already += room
        assert sum(got) == 100
        assert got == [25, 25, 25, 25]

    def test_a_thin_branch_does_not_waste_the_capacity_it_left(self):
        """The share is only ever the smaller of itself and the room left,
        so a branch that found three channels does not cost the run 22."""
        already = 3           # a branch that only found three
        assert self._room(100, 4, already) == 25

    def test_a_single_branch_tier_is_unchanged(self):
        """Sample is one branch and worked; it must not start rationing."""
        assert self._room(cap=21, branches=1, already=0) == 21

    def test_it_never_exceeds_the_cap(self):
        assert self._room(cap=100, branches=4, already=90) == 10
        assert self._room(cap=100, branches=4, already=100) == 0

    def test_the_node_computes_it_the_same_way(self):
        import sys

        import src.tools.hydrate_metadata  # noqa: F401

        src = open(
            sys.modules["src.tools.hydrate_metadata"].__file__, encoding="utf-8"
        ).read()
        assert "share = cap if branches <= 1 else max(1, cap // branches)" in src
        assert "room = max(0, min(cap - len(hydrated), share))" in src

    def test_max_branches_is_a_real_config_field(self):
        from src.config import HarnessConfig

        assert "max_branches" in HarnessConfig.model_fields


class TestTheProgressBarTracksRoundsNotPhases:
    """Taking the furthest phase ever reached, one discovery round touches
    assess and puts the bar at 94%. Sample is one round, so that was near
    enough. Standard is sixteen: the education run sat at 98% two hours into
    a five-hour budget with fifteen rounds still to come.

    Replayed against that run's own log, the bar now reads 12% after round
    one, 53% at eight, 94% at sixteen.
    """

    def test_a_launched_run_records_its_round_budget(self, tmp_path, monkeypatch):
        """The console divides the research phases across these, so a run
        that does not carry the number cannot draw an honest bar."""
        from unittest.mock import patch

        from src.api import runs as runs_mod

        monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
        captured = {}

        class _P:
            pid = 1

        with patch("src.api.runs.subprocess.Popen", return_value=_P()), \
             patch("src.api.runs._append_registry", side_effect=captured.update):
            runs_mod.launch_run(["education"], depth="standard")

        assert captured["rounds_total"] == 16, captured.get("rounds_total")

    def test_each_depth_records_its_own(self):
        from src.api.depth import get_tier

        for depth, expected in (("sample", 1), ("standard", 16), ("deep", 64)):
            g = get_tier(depth).governors
            assert g["MAX_BRANCHES"] * g["MAX_ROUNDS_PER_BRANCH"] == expected, depth

    def test_the_console_divides_the_research_share_across_them(self):
        src = open("web/src/lib/progress.ts", encoding="utf-8").read()
        assert "roundsTotal" in src
        assert "RESEARCH_SHARE * Math.min(1, (roundsDone + within) / total)" in src

    def test_rounds_are_counted_from_the_node_that_closes_one(self):
        """check_saturation runs once per round, so counting it counts
        rounds without inferring them from the shape of the loop."""
        src = open("web/src/lib/progress.ts", encoding="utf-8").read()
        assert "e.node_name === 'check_saturation'" in src

    def test_reaching_the_write_up_means_research_is_over(self):
        """A run that saturates early must not sit at 40% while it writes
        the workbook -- an early finish is a finished search."""
        src = open("web/src/lib/progress.ts", encoding="utf-8").read()
        assert "if (inWriteUp)" in src
        assert "RESEARCH_SHARE + PHASE_WEIGHT.finish" in src

    def test_a_run_without_a_depth_keeps_the_old_behaviour(self):
        """A bare CLI run has no tier, so it has no round budget to divide
        by -- falling back to one round is what it did before."""
        src = open("web/src/lib/progress.ts", encoding="utf-8").read()
        assert "roundsTotal ?? 1" in src

    def test_the_page_passes_it_through(self):
        src = open("web/src/pages/LiveRunsPage.tsx", encoding="utf-8").read()
        assert "computeProgress(entries, run.status, run.rounds_total)" in src

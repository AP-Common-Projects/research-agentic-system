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
    """Two ceilings, because there are two populations.

    max_channels_per_run is the promise about the FILE -- channels that
    clear the subscriber floor and can be written into it. The hydration
    ceiling bounds what the run may spend reaching that promise. They were
    one number, and only about a fifth of hydrated channels clear the
    floor, so the Standard education run of 2026-09-08 stopped on "reached
    its channel limit (100 of 100)" and delivered a 17-row workbook.
    """

    def _check(self, cap, hydrated, delivered=0, ceiling=0, **overrides):
        cfg = MagicMock()
        cfg.harness.run_deadline_seconds = 0
        cfg.harness.budget_limit_usd = 0
        cfg.harness.brightdata_record_budget = 0
        cfg.harness.youtube_quota_budget_per_run = 0
        cfg.harness.max_channels_per_run = cap
        # Real ints, not the MagicMock default: run_ceilings reads this and
        # a mock attribute is truthy, which would pin every run to a
        # ceiling of 1.
        cfg.harness.max_hydrated_channels_per_run = ceiling
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
            "qualified_channel_ids": {f"c{i}" for i in range(delivered)},
            "tree": {"n1": {"status": "active"}},
            "active_node_id": "n1",
        }
        with patch.object(sat, "get_config", return_value=cfg), \
             patch.object(sat, "run_elapsed_seconds", return_value=0):
            return sat.check_saturation(state)

    def test_a_full_delivery_target_stops_the_run(self):
        """The promise is kept -- there is nothing left for a further round
        of discovery to contribute."""
        out = self._check(cap=100, hydrated=400, delivered=100)
        node = out["node_logs"][0]["input_summary"]
        assert node.get("governor") == "max_channels_per_run", node
        assert node.get("spent") == 100

    def test_hydrating_the_target_is_not_delivering_it(self):
        """The bug itself: 100 hydrated is not 100 in the workbook, and
        stopping there is what shipped 17 rows against a promise of 100."""
        out = self._check(cap=100, hydrated=100, delivered=24)
        assert out.get("next_action") != "budget_exhausted"

    def test_the_hydration_ceiling_still_stops_a_run_that_cannot_qualify(self):
        """A topic where nothing clears the floor must not hydrate forever
        chasing a target it will never reach."""
        out = self._check(cap=100, hydrated=1200, delivered=3)
        gov = out["node_logs"][0]["input_summary"]["governor"]
        assert gov == "max_hydrated_channels_per_run"

    def test_it_stops_when_the_target_is_exceeded_too(self):
        out = self._check(cap=100, hydrated=600, delivered=140)
        assert out["node_logs"][0]["input_summary"]["governor"] == "max_channels_per_run"

    def test_a_run_with_room_left_carries_on(self):
        out = self._check(cap=100, hydrated=40, delivered=8)
        assert out.get("next_action") != "budget_exhausted"

    def test_an_uncapped_run_is_unaffected(self):
        """A bare CLI run sets no cap, and used to work exactly because of
        that -- it must not start stopping now."""
        out = self._check(cap=0, hydrated=5000, delivered=5000)
        assert out.get("next_action") != "budget_exhausted"

    def test_it_exits_the_same_way_the_other_ceilings_do(self):
        """force-saturate, compact, finalize, export what the run reached --
        not an abort, which is what the recursion limit gave instead."""
        out = self._check(cap=10, hydrated=50, delivered=10)
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

    def _room(self, cap, branches, rounds_each, already):
        """The room the trim allows this round, as hydrate_metadata computes
        it. Mirrored here so the arithmetic can be exercised; the last test
        in this class asserts the node still computes it the same way."""
        rounds_total = max(1, branches * rounds_each)
        share = -(-cap // rounds_total)
        return max(0, min(cap - already, share))

    def test_one_round_cannot_take_the_whole_cap(self):
        assert self._room(cap=100, branches=4, rounds_each=4, already=0) == 7

    def test_a_single_branch_cannot_spend_the_whole_cap_either(self):
        """Dividing by branches alone was not enough. A branch gets four
        rounds, so at 25 a round the first one spent all 100 and the run
        ended having visited only `root` -- observed live on
        run-dd2dbdbc3080, four rounds of 25."""
        already = 0
        for _ in range(4):                      # one branch's whole allowance
            already += self._room(100, 4, 4, already)
        assert already < 100, "one branch must not consume the entire cap"
        assert already == 28

    def test_the_whole_run_can_still_fill_it(self):
        """Rationing that cannot reach the cap would trade one failure for
        another -- a Standard run delivering 40 channels of a promised 100."""
        already = 0
        for _ in range(16):                     # every round the tier budgets
            already += self._room(100, 4, 4, already)
        assert already == 100

    def test_each_branch_gets_roughly_its_share(self):
        per_branch = 4 * self._room(100, 4, 4, already=0)
        assert 20 <= per_branch <= 35, per_branch

    def test_a_thin_branch_does_not_waste_the_capacity_it_left(self):
        """The share is only ever the smaller of itself and the room left,
        so a branch that found three channels does not cost the run the
        rest of its allowance."""
        assert self._room(100, 4, 4, already=3) == 7

    def test_a_single_round_tier_is_unchanged(self):
        """Sample is one branch of one round and worked; it must not start
        rationing against itself."""
        assert self._room(cap=21, branches=1, rounds_each=1, already=0) == 21

    def test_it_never_exceeds_the_cap(self):
        assert self._room(cap=100, branches=4, rounds_each=4, already=96) == 4
        assert self._room(cap=100, branches=4, rounds_each=4, already=100) == 0

    def test_the_node_computes_it_the_same_way(self):
        import sys

        import src.tools.hydrate_metadata  # noqa: F401

        src = open(
            sys.modules["src.tools.hydrate_metadata"].__file__, encoding="utf-8"
        ).read()
        assert "rounds_total = max(1, branches * rounds_each)" in src
        assert "share = -(-cap // rounds_total)" in src
        assert "room = max(0, min(cap - len(hydrated), share))" in src

    def test_both_governors_it_divides_by_are_real_config_fields(self):
        from src.config import HarnessConfig

        assert "max_branches" in HarnessConfig.model_fields
        assert "max_rounds_per_branch" in HarnessConfig.model_fields


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


class TestTheConsoleNamesTheCeilingThatStopped:
    """Every run-level ceiling exits through the same next_action, which is
    called "budget_exhausted" -- the name of the graph edge, not a statement
    about money. The activity row rendered that raw, beside spent_usd.

    A Standard run that stopped because it had hydrated its hundredth
    channel read "budget_exhausted · spent usd: 1.1617" against a $6
    budget, which says the opposite of what happened. The log always
    carried `governor`; the row simply never showed it.
    """

    def test_the_row_reads_the_governor(self):
        src = open("web/src/pages/LiveRunsPage.tsx", encoding="utf-8").read()
        assert "GOVERNOR_REASON" in src
        assert "input.governor" in src

    def test_every_run_level_ceiling_has_a_phrase(self):
        """A governor with no entry falls back to naming itself, but the
        ones the tiers actually enforce should read as English."""
        import re

        src = open("web/src/pages/LiveRunsPage.tsx", encoding="utf-8").read()
        block = src[src.index("const GOVERNOR_REASON"):src.index("function summarise")]
        named = set(re.findall(r"^\s*([a-z_]+):", block, re.M))

        sat = open("src/tools/saturation.py", encoding="utf-8").read()
        enforced = set(re.findall(r'_budget_exhausted\(\s*\n?\s*state,\s*start,\s*"([a-z_]+)"', sat))
        assert enforced, "could not find the ceilings saturation enforces"
        assert enforced <= named, f"no phrase for: {sorted(enforced - named)}"

    def test_the_channel_cap_does_not_read_as_a_money_verdict(self):
        """It rendered as "spent usd: 1.1617" and read as "out of money" on
        a run that had spent $1.18 of a $9 allowance."""
        import re

        src = open("web/src/pages/LiveRunsPage.tsx", encoding="utf-8").read()
        block = src[src.index("const GOVERNOR_REASON"):src.index("function summarise")]
        phrase = re.search(r"max_channels_per_run: '([^']+)'", block)
        assert phrase, block
        said = phrase.group(1)
        assert "channel" in said, said
        for money in ("spend", "spent", "cost", "budget", "$"):
            assert money not in said.lower(), said

    def test_the_two_ceilings_do_not_read_the_same(self):
        """One is the promise kept, the other is a thin topic. A client
        reading "found all the channels this depth covers" on a run that
        delivered a quarter of them is the bug this pair exists to end."""
        import re

        src = open("web/src/pages/LiveRunsPage.tsx", encoding="utf-8").read()
        block = src[src.index("const GOVERNOR_REASON"):src.index("function summarise")]
        said = dict(re.findall(r"([a-z_]+): '([^']+)'", block))
        assert said["max_channels_per_run"] != said["max_hydrated_channels_per_run"]

    def test_it_says_the_research_finished_rather_than_failed(self):
        """Hitting a ceiling is the run completing its remit, not an error,
        and the row should not read like one."""
        src = open("web/src/pages/LiveRunsPage.tsx", encoding="utf-8").read()
        assert "research complete —" in src

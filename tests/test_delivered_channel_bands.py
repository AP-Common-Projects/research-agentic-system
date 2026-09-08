"""The tiers promise channels IN THE WORKBOOK, and the run is held to it.

A Standard education run on 2026-09-08 finished healthy and delivered 17
channels and 1,103 videos for $1.30. Its card had said 100 channels,
~5,400 videos and ~$6.75. Nothing failed; three things were wrong.

  1. MAX_CHANNELS_PER_RUN bounded HYDRATED channels while every surface
     quoted the workbook. About one hydrated channel in five clears the
     client's 50k subscriber floor, so the cap could never buy what it
     was quoting: 100 hydrated, 24 over the floor, 17 rows in the file.

  2. Seven enrichment nodes selected on `meets_subscriber_floor`, which is
     also true for sub-floor channels that tripped a breakout override.
     The export takes the real subscriber_count. 65 channels were enriched
     so that 24 could ship.

  3. Every per-channel stage was a serial loop around one LLM call. The
     run spent 6,584 of its 8,217 node-seconds waiting on a socket, and
     ended on the clock with 86% of its money unspent -- which is why the
     bill came to $1.30 rather than the $6.75 quoted.
"""

from __future__ import annotations

import pathlib
import re

from unittest.mock import patch

import pytest

from src.api import depth as depth_mod
from src.tools.deliverable import DISCOVERY_YIELD, hydration_ceiling


#: What the client asked for, verbatim: "Sample ... up to 50 channels (must
#: be at least 40 and max 50) ... Standard ... up to 250 (at least 200, max
#: 250) ... Deep ... up to 500 (at least 450, and maximum 500)."
BANDS = {"sample": (40, 50), "standard": (200, 250), "deep": (450, 500)}


class TestTheBandsAreWhatWasAskedFor:
    def test_each_tier_declares_its_band(self):
        for tier in depth_mod.TIERS:
            low, high = BANDS[tier.id]
            assert (tier.min_channels, tier.target_channels) == (low, high), tier.id

    def test_the_cap_handed_to_the_run_is_the_top_of_the_band(self):
        for tier in depth_mod.TIERS:
            assert tier.governors["MAX_CHANNELS_PER_RUN"] == BANDS[tier.id][1]

    def test_the_card_shows_both_ends(self):
        """"up to 250" alone was the number that went wrong quietly. A floor
        beside it is a claim a client can check against the file."""
        for tier in depth_mod.TIERS:
            low, high = BANDS[tier.id]
            assert tier.est_channels == f"{low:,}–{high:,}", tier.id

    def test_videos_scale_with_the_band(self):
        """"videos accordingly" -- at the measured 58 per floor-passing
        channel, from 145,694 videos across 2,516 such channels."""
        for tier in depth_mod.TIERS:
            low, high = BANDS[tier.id]
            assert tier.est_videos == (
                f"{low * depth_mod._VIDEOS_PER_CHANNEL:,}–"
                f"{high * depth_mod._VIDEOS_PER_CHANNEL:,}"
            ), tier.id

    def test_the_ladder_still_climbs(self):
        for attr in ("target_channels", "min_channels", "hours",
                     "est_openrouter_usd", "est_brightdata_usd"):
            values = [getattr(t, attr) for t in depth_mod.TIERS]
            assert values == sorted(values), attr
            assert len(set(values)) == 3, attr


class TestTheWindowCanActuallyReachTheBand:
    def test_the_top_of_the_band_fits_at_the_expected_yield(self):
        for tier in depth_mod.TIERS:
            work = depth_mod._research_seconds(tier.target_channels)
            budget = tier.hours * 3600 * depth_mod._SAFETY_MARGIN
            assert work <= budget, f"{tier.id}: {work:.0f}s in {budget:.0f}s"

    def test_the_bottom_of_the_band_fits_even_on_a_poor_topic(self):
        """The pooled organic yield is 15.5%, dragged down by two very wide
        augment runs that passed at 9-11%. A tier that only holds its
        minimum on a good topic does not hold its minimum."""
        for tier in depth_mod.TIERS:
            work = depth_mod._research_seconds(
                tier.min_channels,
                depth_mod._FLOOR_YIELD_PESSIMISTIC,
                depth_mod._SCOPE_YIELD_PESSIMISTIC,
            )
            budget = tier.hours * 3600 * depth_mod._SAFETY_MARGIN
            assert work <= budget, f"{tier.id}: {work:.0f}s in {budget:.0f}s"

    def test_discovery_can_pay_for_the_whole_hydration_ceiling(self):
        """Records buy candidates, and run-e4e794436210 stopped on this
        ceiling at 4,111 of 4,302 -- four hours into a 7.75-hour window,
        having spent $6.93 of $18.78. A record budget that binds before the
        delivery target makes the band unreachable whatever else is right."""
        for tier in depth_mod.TIERS:
            needed = (tier.hydration_ceiling
                      * depth_mod._RECORDS_PER_DISCOVERED_CHANNEL)
            assert tier.governors["BRIGHTDATA_RECORD_BUDGET"] >= needed, tier.id

    def test_the_record_budget_is_above_what_a_standard_run_measured(self):
        """4,111 records bought 2,186 discovered channels on the run that
        ran out. Standard now has to be able to buy its whole ceiling."""
        standard = depth_mod.TIERS_BY_ID["standard"]
        assert standard.governors["BRIGHTDATA_RECORD_BUDGET"] > 4302

    def test_the_quota_budget_covers_the_hydration_ceiling(self):
        for tier in depth_mod.TIERS:
            needed = tier.hydration_ceiling * depth_mod._QUOTA_PER_HYDRATED_CHANNEL
            assert tier.governors["YOUTUBE_QUOTA_BUDGET_PER_RUN"] >= needed, tier.id

    def test_the_quota_budget_stays_inside_one_day(self):
        """Five keys are configured, each with a 10,000-unit daily
        allowance. A tier that needs more than the pool cannot finish."""
        for tier in depth_mod.TIERS:
            assert tier.governors["YOUTUBE_QUOTA_BUDGET_PER_RUN"] <= 50_000, tier.id

    def test_the_spending_ceiling_sits_above_the_quote(self):
        """A circuit breaker under the quoted figure stops the run on money
        the client was told it had."""
        for tier in depth_mod.TIERS:
            assert tier.governors["BUDGET_LIMIT_USD"] > tier.est_openrouter_usd, tier.id


class TestTheCapCountsTheRightPopulation:
    def test_the_hydration_ceiling_is_several_times_the_target(self):
        """The bug in one assertion: these were the same number, and they
        are populations that differ by about five times."""
        for tier in depth_mod.TIERS:
            assert tier.hydration_ceiling >= tier.target_channels * 4, tier.id

    def test_both_ceilings_reach_the_run(self):
        for tier in depth_mod.TIERS:
            assert tier.governors["MAX_CHANNELS_PER_RUN"] == tier.target_channels
            assert (tier.governors["MAX_HYDRATED_CHANNELS_PER_RUN"]
                    == tier.hydration_ceiling)

    def test_the_ceiling_is_derived_from_the_measured_yield(self):
        for tier in depth_mod.TIERS:
            assert tier.hydration_ceiling == hydration_ceiling(tier.target_channels)

    def test_a_hundred_hydrated_would_not_have_satisfied_a_hundred_promised(self):
        """The education run, as arithmetic: at the rates the store
        actually shows, 100 hydrated channels cannot deliver 100 rows --
        they delivered 17, and the model says 15."""
        assert hydration_ceiling(100) > 100
        assert 12 <= int(100 * DISCOVERY_YIELD) <= 20


class TestTheEnrichmentGateAsksTheExportsQuestion:
    """The recurring bug class in this codebase: a marker that answers a
    different question than the workbook asks. This is the eighth."""

    NODES = [
        "classify_channel", "resolve_first_video_date",
        "extract_success_failure_factors", "populate_taxonomy_dimensions",
        "populate_shared_fields", "describe_video_titles",
        "populate_crime_metadata", "assign_cohorts", "finalize_dataset",
    ]

    def test_no_node_selects_work_on_the_floor_flag(self):
        for name in self.NODES:
            code = pathlib.Path(f"src/nodes/{name}.py").read_text(encoding="utf-8")
            assert "meets_subscriber_floor = TRUE" not in code, name

    def test_they_all_ask_the_shared_predicate_instead(self):
        for name in self.NODES:
            code = pathlib.Path(f"src/nodes/{name}.py").read_text(encoding="utf-8")
            assert "eligible_sql" in code, name

    def test_the_export_asks_the_same_function(self):
        from src.export import _floor
        from src.tools.deliverable import deliverable_floor, eligible_sql

        assert _floor() == deliverable_floor()
        assert str(deliverable_floor()) in eligible_sql()

    def test_the_predicate_reads_the_real_subscriber_count(self):
        from src.tools.deliverable import eligible_sql

        assert "subscriber_count >=" in eligible_sql()
        assert "meets_subscriber_floor" not in eligible_sql()

    def test_it_can_be_aliased_or_not(self):
        from src.tools.deliverable import eligible_sql

        assert eligible_sql("c").startswith("c.")
        assert eligible_sql(None).startswith("subscriber_count")


class TestCrimeKeepsItsWindowAndGivesUpChannels:
    def test_crime_delivers_fewer_in_the_same_hours(self):
        for tier in depth_mod.TIERS:
            crime = tier.for_topic("true crime")
            assert crime.hours == tier.hours
            assert crime.target_channels < tier.target_channels

    def test_crime_keeps_the_shape_of_its_band(self):
        """"at least" must go on meaning the same share of "up to"."""
        for tier in depth_mod.TIERS:
            crime = tier.for_topic("true crime")
            plain = tier.min_channels / tier.target_channels
            assert crime.min_channels / crime.target_channels == pytest.approx(
                plain, abs=0.05
            ), tier.id

    def test_a_crime_lookup_does_not_corrupt_the_shared_tier(self):
        """dataclasses.replace copies the governors REFERENCE, and
        __post_init__ writes into whatever dict it is handed."""
        before = [t.governors["MAX_CHANNELS_PER_RUN"] for t in depth_mod.TIERS]
        depth_mod.get_tier("standard", "crime")
        depth_mod.get_tier("deep", "true crime")
        after = [t.governors["MAX_CHANNELS_PER_RUN"] for t in depth_mod.TIERS]
        assert before == after == [50, 250, 500]


class TestTheTargetIsMeasuredAgainstTheFileItself:
    """The floor is not the last filter, and counting it as if it were is
    the same bug one layer down.

    The export also scopes to the run's dominant category, and falls back
    to own_only when no category resolves. Across the runs in the store
    those drop a further 26% of floor-passing channels on average -- 54%
    on run-019f20e21145, which held 192 over the floor and shipped 89.
    """

    def test_the_count_comes_from_the_functions_that_write_the_file(self):
        """Not a re-derived query beside them. "What was counted" and "what
        was written" cannot disagree if they are the same call."""
        src = pathlib.Path("src/export.py").read_text(encoding="utf-8")
        body = src[src.index("def workbook_channel_count"):
                   src.index("def workbook_rows")]
        assert "workbook_scope(" in body
        assert "fetch_run_channels(" in body

    def test_an_unreachable_store_is_none_not_zero(self):
        """Zero would read as "this run has delivered nothing" and stop
        nothing; None says the question could not be answered."""
        from src.export import workbook_channel_count

        with patch("src.export.workbook_scope", side_effect=RuntimeError("down")):
            assert workbook_channel_count("run-x") is None

    def test_saturation_stops_on_the_measured_figure(self):
        from src.tools import saturation as sat

        out = _saturation(cap=100, delivered=100)
        assert out["node_logs"][0]["input_summary"]["governor"] == "max_channels_per_run"
        assert out["node_logs"][0]["input_summary"]["spent"] == 100

    def test_holding_the_floor_passing_set_is_not_holding_the_file(self):
        """The gap this closes: 250 channels over the floor is about 185
        rows once the export has scoped them."""
        out = _saturation(cap=250, delivered=185)
        assert out.get("next_action") != "budget_exhausted"

    def test_the_figure_reaches_state_on_every_exit(self):
        """Eleven exit paths; a governor present on ten has a hole in it."""
        for delivered, cap in ((3, 100), (100, 100), (0, 0)):
            out = _saturation(cap=cap, delivered=delivered)
            assert out.get("delivered_channel_count") == delivered, (delivered, cap)

    def test_discovery_admission_reads_the_same_figure(self):
        code = pathlib.Path("src/graph.py").read_text(encoding="utf-8")
        assert 'state.get("delivered_channel_count")' in code

    def test_hydration_reads_it_too(self):
        code = pathlib.Path("src/tools/hydrate_metadata.py").read_text(encoding="utf-8")
        assert 'state.get("delivered_channel_count")' in code

    def test_the_education_runs_funnel_end_to_end(self):
        """The run the client reported, as arithmetic. 100 hydrated became
        24 over the floor became 17 rows; the old cap saw only the first
        number and called the promise kept."""
        from src.export import workbook_channel_count

        assert workbook_channel_count("run-dd2dbdbc3080") == 17


def _saturation(cap, delivered):
    """check_saturation with every other ceiling switched off."""
    from unittest.mock import MagicMock
    from src.tools import saturation as sat

    cfg = MagicMock()
    cfg.harness.run_deadline_seconds = 0
    cfg.harness.budget_limit_usd = 0
    cfg.harness.brightdata_record_budget = 0
    cfg.harness.youtube_quota_budget_per_run = 0
    cfg.harness.max_channels_per_run = cap
    cfg.harness.max_hydrated_channels_per_run = 0
    cfg.harness.saturation_novelty_threshold = 0.1
    cfg.harness.saturation_consecutive_window = 2
    cfg.harness.max_rounds_per_branch = 4
    cfg.harness.max_tree_depth = 2
    cfg.harness.branch_lineage_budget_enabled = False
    state = {
        "run_id": "run-under-test",
        "hydrated_channel_ids": set(),
        "tree": {"n1": {"status": "active"}},
        "active_node_id": "n1",
    }
    with patch.object(sat, "get_config", return_value=cfg), \
         patch.object(sat, "run_elapsed_seconds", return_value=0), \
         patch("src.export.workbook_channel_count", return_value=delivered):
        return sat.check_saturation(state)


class TestTheStreamShowsTheNumberThatMatters:
    """"channel count: 3003" was on the stream while the file it was
    heading for held seventeen rows."""

    def test_the_workbook_figure_is_logged_every_round(self):
        out = _saturation(cap=250, delivered=61)
        summary = out["node_logs"][0]["input_summary"]
        assert summary["channels_in_workbook"] == 61

    def test_it_is_logged_beside_its_target(self):
        out = _saturation(cap=250, delivered=61)
        assert out["node_logs"][0]["input_summary"]["workbook_target"] == 250

    def test_the_console_renders_both(self):
        src = pathlib.Path("web/src/pages/LiveRunsPage.tsx").read_text(encoding="utf-8")
        keys = src[src.index("const INTERESTING_KEYS"):src.index("/** Which run-level")]
        assert "'channels_in_workbook'" in keys
        assert "'workbook_target'" in keys

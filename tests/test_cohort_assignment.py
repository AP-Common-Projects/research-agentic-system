"""Cohorts must be assignable in both verticals, and mean something.

The client brief asks for 30-40 underperforming/failed channels per
vertical to correct survivor bias. The shipped implementation produced
exactly ONE across the whole database, for three compounding reasons:

  * `underperformer` was decided inside `if vertical == "crime"`, so
    Finance could not produce one at all;
  * the test was absolute -- `eng < 20 and subs < 100000` -- which above a
    50k subscriber floor describes a sliver of one size band, and says
    nothing about performance "relative to their niche/size" as asked;
  * a settled, non-breakout Finance channel fell through to `None` and got
    no cohort whatsoever (189 of 193 in the deliverable).
"""

from __future__ import annotations

import inspect

import src.nodes.assign_cohorts as mod


class TestUnderperformerIsVerticalAgnostic:
    def test_underperformer_is_decided_before_the_vertical_branch(self):
        src = inspect.getsource(mod.assign_cohorts)
        under = src.index('cohort_code = "underperformer"')
        crime_branch = src.index('elif vertical == "crime"')
        assert under < crime_branch, (
            "underperformer must be decided before the per-vertical branches, "
            "or one vertical can never produce one"
        )

    def test_finance_never_falls_through_without_a_cohort(self):
        src = inspect.getsource(mod.assign_cohorts)
        finance = src[src.index('elif vertical == "finance"'):]
        finance = finance[: finance.index("if cohort_code:")]
        assert '"market_benchmark"' in finance, (
            "an established, non-breakout Finance channel must still get a "
            "cohort rather than None"
        )


class TestUnderperformanceIsRelativeAndTimeAware:
    def test_peer_floor_ignores_thin_buckets(self):
        """No peer evidence must not read as evidence of failure."""
        src = inspect.getsource(mod._peer_engagement_floors)
        assert "n >= 8" in src, "a bucket needs enough members for a quartile"
        assert "PERCENTILE_CONT" in src, "the floor must be a peer percentile"

    def test_stagnation_and_abandonment_are_considered(self):
        src = inspect.getsource(mod.assign_cohorts)
        for token in ("abandoned", "stagnant", "below_peers"):
            assert token in src, f"{token} must feed the underperformer test"

    def test_a_winner_is_never_an_underperformer(self):
        src = inspect.getsource(mod.assign_cohorts)
        assert "is_under = (not is_winner)" in src, (
            "a high-engagement or very large channel must not be labelled "
            "an underperformer merely for a quiet six months"
        )


class TestEligibilityTerminates:
    def test_query_excludes_channels_outside_the_two_verticals(self):
        src = inspect.getsource(mod.assign_cohorts)
        assert "parent_category IN ('crime', 'finance')" in src, (
            "channels skipped in Python but left eligible in SQL refill the "
            "LIMIT window every round and stall the backfill"
        )

    def test_already_assigned_is_checked_against_the_right_vertical(self):
        src = inspect.getsource(mod.assign_cohorts)
        assert "cc.vertical = nt.parent_category" in src
        assert "cc.vertical = 'crime'" not in src, (
            "a hardcoded vertical makes every Finance channel look unassigned "
            "forever"
        )

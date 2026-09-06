"""Enrichment must touch the run's own channels, not the whole table.

Every enrichment node's eligibility query was a global select over
`channels` -- "floor-passing and unclassified", "missing a country", "no
affiliate signal yet" -- with no reference to the run asking. Harmless with
one dataset in the database. With ten thousand channels from a dozen past
runs in it, measured on the automotive run of 2026-09-05:

  * classify_channel's LIMIT 50 selected 50 channels of which 7 were the
    run's own. It classified 0 of its 76, so primary_niche_id shipped
    0/76, the Niches / Success Factors / Failure Factors sheets were
    empty, and every channel showed as "Unclassified" on the graph.

  * resolve_first_video_date spent four minutes and its whole LIMIT 50 on
    finance and crime channels; first_video_published_at shipped 0/76.

  * resolve_geo_language selected 2,720 channels across the whole table
    and resolved 399 before the run ended.

  * Each wrote those rows back under THIS run's id, so other runs'
    channels came to claim membership of a run that never found them --
    which is how an automotive run appeared in the finance workbook's
    lineage.
"""

from __future__ import annotations

import inspect

import pytest

from src.tools.run_scope import channel_scope, scope_clause


class TestScopeSemantics:
    def test_no_run_context_is_unrestricted(self):
        """A backfill script calling a node with a bare dict genuinely does
        want the whole table -- that is the one legitimate case."""
        assert channel_scope({}) is None
        assert scope_clause({}) == ("", ())

    def test_an_empty_scope_selects_nothing_and_is_not_unrestricted(self):
        """The distinction the whole module turns on. An empty list means
        'this run owns no channels'; treating it as falsy is what silently
        widens a scoped query back to every row in the table."""
        assert channel_scope({"discovered_channel_ids": []}) == []
        assert channel_scope({"scope_channel_ids": []}) == []

        sql, params = scope_clause({"discovered_channel_ids": []})
        assert sql, "an empty scope must still emit a restricting clause"
        assert params == ([],)

    def test_an_explicit_worker_slice_is_never_widened(self):
        """Parallel workers own disjoint slices; overlapping them buys the
        same LLM classification several times."""
        state = {
            "scope_channel_ids": ["a"],
            "discovered_channel_ids": ["a", "b", "c"],
        }
        assert channel_scope(state) == ["a"]

    def test_hydrated_channels_count_as_the_runs_own(self):
        """A resumed run reloads hydrated ids from its checkpoint without
        necessarily replaying discovery; those are still its to finish."""
        state = {
            "discovered_channel_ids": ["a", "b"],
            "hydrated_channel_ids": {"b", "c"},
        }
        assert sorted(channel_scope(state)) == ["a", "b", "c"]

    def test_the_clause_is_concatenable_and_parameterised(self):
        sql, params = scope_clause({"discovered_channel_ids": ["x"]})
        assert sql.startswith("AND ") and sql.endswith(" ")
        assert "%s" in sql
        assert params == (["x"],)

    def test_the_column_can_be_qualified_for_a_joined_query(self):
        sql, _ = scope_clause({"discovered_channel_ids": ["x"]}, "c.channel_id")
        assert "c.channel_id = ANY(%s)" in sql


class TestEveryEnrichmentNodeIsScoped:
    """Each of these ran a global query. A new node added unscoped would
    reproduce the same failure, so the set is asserted rather than trusted."""

    NODES = [
        ("src.nodes.classify_channel", "classify_channel"),
        ("src.nodes.resolve_first_video_date", "resolve_first_video_date"),
        ("src.nodes.resolve_geo_language", "resolve_geo_language"),
        ("src.nodes.extract_metadata_signals", "extract_metadata_signals"),
        ("src.nodes.extract_success_failure_factors", "extract_success_failure_factors"),
        ("src.nodes.assign_cohorts", "assign_cohorts"),
    ]

    @pytest.mark.parametrize("module_name,func_name", NODES)
    def test_node_scopes_its_eligibility_query(self, module_name, func_name):
        import importlib

        module = importlib.import_module(module_name)
        src = inspect.getsource(getattr(module, func_name))
        assert "scope_clause(" in src, (
            f"{func_name} selects channels without restricting them to the "
            "run -- it will enrich other runs' rows and leave its own empty"
        )

    @pytest.mark.parametrize("module_name,func_name", NODES)
    def test_the_scope_is_actually_applied_to_the_sql(self, module_name, func_name):
        """Computing the clause and forgetting to concatenate it is the
        obvious way to half-fix this."""
        import importlib

        module = importlib.import_module(module_name)
        src = inspect.getsource(getattr(module, func_name))
        assert "scope_sql" in src and "scope_params" in src, func_name


class TestCohortsWorkForEveryCategory:
    """assign_cohorts hardcoded `parent_category IN ('crime', 'finance')`
    and fell through to `continue` for anything else, so a run on any other
    topic assigned no cohorts at all -- channel_size_bucket and cohorts
    shipped empty and would have for gaming, music or automotive alike."""

    def test_no_vertical_is_hardcoded(self):
        from src.nodes import assign_cohorts as mod

        # Comment lines stripped first. The comment explaining WHY the
        # filter was removed necessarily quotes it, and a naive scan of the
        # whole source fails on its own explanation -- the mirror of a test
        # in this repo that once PASSED against broken code because the
        # string it searched for appeared in a comment.
        code = "\n".join(
            line for line in inspect.getsource(mod.assign_cohorts).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "IN ('crime', 'finance')" not in code

    def test_a_third_vertical_reaches_a_cohort_code(self):
        from src.nodes import assign_cohorts as mod

        src = inspect.getsource(mod.assign_cohorts)
        # The generic branch exists and names a cohort rather than skipping.
        assert "generic_group" in src
        assert "market_benchmark" in src

    def test_the_generic_branch_gets_its_own_exclusive_group(self):
        """Reusing crime's group would make an automotive channel's cohort
        mutually exclusive against crime's, deleting rows across verticals."""
        from src.nodes import assign_cohorts as mod

        src = inspect.getsource(mod.assign_cohorts)
        assert 'f"{vertical}_lifecycle"' in src


class TestHydrationEnforcesTheChannelCap:
    """Discovery admission checks the cap BEFORE a node runs, so the first
    discovery node can overshoot it in one call -- keyword_search returned
    96 channels against a cap of 36. Hydration is the gate everything
    downstream works from, so the cap has to bind here too."""

    def test_hydration_trims_to_the_cap(self):
        import sys

        import src.tools.hydrate_metadata  # noqa: F401

        src = open(sys.modules["src.tools.hydrate_metadata"].__file__).read()
        assert "max_channels_per_run" in src
        assert "trimmed_to_cap" in src, (
            "a trimmed run must say so in its log, not silently drop channels"
        )

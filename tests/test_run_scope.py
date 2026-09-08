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
        ("src.nodes.populate_taxonomy_dimensions", "populate_taxonomy_dimensions"),
        ("src.nodes.populate_shared_fields", "populate_shared_fields"),
        ("src.nodes.populate_crime_metadata", "populate_crime_metadata"),
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

    def test_no_node_still_reads_the_dead_scope_channel_ids_key_directly(self):
        """A real graph run never sets scope_channel_ids -- only
        discovered_channel_ids. Four nodes had their own ad-hoc
        `state.get("scope_channel_ids")` handling that predated run_scope.py
        and was never wired to anything a real run populates, so each one
        was silently unscoped in every real run regardless of what launched
        it: populate_taxonomy_dimensions, populate_shared_fields,
        describe_video_titles, populate_crime_metadata. Asserted against
        every node file at once so a fifth one written the same way is
        caught here rather than found live on a future run."""
        import glob

        offenders = []
        for path in glob.glob("src/nodes/*.py"):
            src = open(path).read()
            if 'state.get("scope_channel_ids")' in src:
                offenders.append(path)
        assert offenders == []

    def test_describe_video_titles_and_crime_metadata_are_also_scoped(self):
        """Not in NODES above because their scope plumbing does not run
        through scope_clause()'s (sql, params) shape -- describe_video_titles
        takes a channel_scope() list directly, and populate_crime_metadata
        uses scope_clause with a joined-table column qualifier."""
        import inspect as _inspect

        from src.nodes import describe_video_titles, populate_crime_metadata

        assert "channel_scope(state)" in _inspect.getsource(describe_video_titles.describe_video_titles)
        assert "scope_clause(state" in _inspect.getsource(populate_crime_metadata.populate_crime_metadata)


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


class TestSizeBucketDoesNotRequireVideos:
    """score_signals is keyed on channels that HAVE videos, because
    evergreen_score, engagement_score and is_likely_news are computed from
    them and are honestly unknown without them.

    channel_size_bucket is not: it is a function of subscriber_count alone,
    known the moment a channel is hydrated. Computing it inside the video
    loop meant 54 of the automotive run's 76 channels carried a bucket --
    the 22 without were the ones hydration never reached, so they had no
    video rows, and the bucket went missing with them despite their
    subscriber counts being on file the whole time.
    """

    def test_channels_without_videos_still_get_a_bucket(self):
        import inspect

        from src.tools.signal_scoring import score_signals

        src = inspect.getsource(score_signals)
        assert "missing_bucket" in src, (
            "channels absent from by_channel need a size bucket path of "
            "their own; the video loop will never reach them"
        )
        assert "channel_size_bucket(subs)" in src
        # And the floor flag alongside it. It defaults to FALSE, which is
        # indistinguishable from "below the floor", so a channel this branch
        # reached for its bucket alone was invisible to every floor-gated
        # node -- a 5.5M-subscriber channel among them.
        assert '"meets_subscriber_floor": meets' in src

    def test_it_is_reported_rather_than_silent(self):
        import inspect

        from src.tools.signal_scoring import score_signals

        assert "size_bucket_only" in inspect.getsource(score_signals)

    def test_the_bucket_is_a_pure_function_of_subscribers(self):
        """If this ever needed more than a subscriber count, the fix above
        would be unsound."""
        import inspect

        from src.tools.signal_scoring import channel_size_bucket

        params = inspect.signature(channel_size_bucket).parameters
        assert list(params) == ["subscriber_count"]
        assert channel_size_bucket(500) != channel_size_bucket(5_000_000)


class TestPrimaryTopicWorksForAnyVertical:
    """primary_topic's prompt hardcoded two closed lists -- "For Finance:
    ..." and "For Crime: ..." -- with no branch for anything else, and told
    the model to "select exactly one value from the allowed sets above".
    For any other vertical there was no allowed set, so the model correctly
    followed instructions and answered the one legal catch-all: "Other".

    Measured live: finance 0.6% Other, crime 3.3% (genuine edge cases,
    since those verticals HAD real lists) versus automotive at 89% (62 of
    69 in the raw table), and the same shape reproduced across every other
    catalog category once discovered -- 209 entertainment channels, 57
    lifestyle, 29 gaming, and so on, over 500 in total.
    """

    def test_the_prompt_is_no_longer_a_closed_finance_crime_list(self):
        from src.nodes.populate_taxonomy_dimensions import SYSTEM_PROMPT

        assert "For Finance:" not in SYSTEM_PROMPT
        assert "For Crime:" not in SYSTEM_PROMPT
        assert "select exactly one value from the allowed sets above" not in SYSTEM_PROMPT.lower()

    def test_the_prompt_still_shows_calibration_examples(self):
        """The old lists were doing double duty: closing off the field AND
        showing the model the desired granularity/style. Removing the
        closure must not also remove the calibration."""
        from src.nodes.populate_taxonomy_dimensions import SYSTEM_PROMPT

        for example in ("Retirement", "Cold Case", "Car Reviews"):
            assert example in SYSTEM_PROMPT

    def test_other_is_framed_as_rare_not_as_the_default_for_new_verticals(self):
        from src.nodes.populate_taxonomy_dimensions import SYSTEM_PROMPT

        assert "no category was provided for this vertical" in " ".join(SYSTEM_PROMPT.split())

    def test_the_channels_own_vertical_reaches_the_prompt(self):
        """The fix only works if the model is actually told which vertical
        it is looking at -- the prompt talks ABOUT verticals but the
        per-channel call has to supply this one's."""
        import inspect

        from src.nodes.populate_taxonomy_dimensions import populate_taxonomy_dimensions

        src = inspect.getsource(populate_taxonomy_dimensions)
        assert '"vertical": category' in src


class TestHydrationEnforcesTheChannelCap:
    """Discovery admission checks the cap BEFORE a node runs, so the first
    discovery node can overshoot it in one call -- keyword_search returned
    96 channels against a cap of 36. Hydration is the gate everything
    downstream works from, so the cap has to bind here too."""

    def test_hydration_trims_to_the_cap(self):
        import sys

        import src.tools.hydrate_metadata  # noqa: F401

        src = open(sys.modules["src.tools.hydrate_metadata"].__file__).read()
        # The hydration ceiling, not the delivery target: this node's cost
        # is per hydrated channel, and only about a fifth of them clear the
        # subscriber floor the workbook is scoped to.
        assert "run_ceilings" in src
        assert "trimmed_to_cap" in src, (
            "a trimmed run must say so in its log, not silently drop channels"
        )


class TestFinalizeDatasetWritesToTheWorkbooksChannels:
    """Every column finalize_dataset writes was scoped to channels the run
    was the FIRST to see, and a workbook is mostly channels it re-found.
    run-44e01aab65c2 shipped data_completeness_score at 4 of 5, and no
    re-run could fix it: the row was never in scope to begin with.

    The seventh time in this codebase that a write asked "who found this
    first" when the question was "what is in the workbook".
    """

    def test_it_uses_the_runs_own_channel_set(self):
        from src.nodes.finalize_dataset import _run_channel_clause

        sql, params = _run_channel_clause(
            {"discovered_channel_ids": ["mine", "re-found"]}, "run-x"
        )
        assert sql == "channel_id = ANY(%s)"
        assert sorted(params[0]) == ["mine", "re-found"]

    def test_a_re_found_channel_is_in_scope(self):
        """The exact row that could not be filled: this run tagged it, an
        earlier run discovered it."""
        from src.nodes.finalize_dataset import _run_channel_clause

        _, params = _run_channel_clause(
            {"discovered_channel_ids": ["found-by-an-earlier-run"]}, "run-x"
        )
        assert "found-by-an-earlier-run" in params[0]

    def test_without_a_run_context_it_falls_back_rather_than_widening(self):
        """A bare CLI invocation keeps the old behaviour. What it must never
        do is drop the clause: an unrestricted UPDATE here rewrites every
        channel in the database."""
        from src.nodes.finalize_dataset import _run_channel_clause

        sql, params = _run_channel_clause({}, "run-x")
        assert sql == "first_discovered_run_id = %s"
        assert params == ("run-x",)

    def test_a_run_owning_nothing_writes_nothing(self):
        from src.nodes.finalize_dataset import _run_channel_clause

        sql, params = _run_channel_clause({"discovered_channel_ids": []}, "run-x")
        assert sql == "channel_id = ANY(%s)"
        assert params[0] == []

    def test_the_workbook_columns_use_it(self):
        import inspect

        from src.nodes.finalize_dataset import finalize_dataset

        src = inspect.getsource(finalize_dataset)
        assert "data_completeness_score" in src and "WHERE {where_sql}" in src
        assert "missing_required_fields" in src
        # The growth columns keep the old clause on purpose: they are in
        # ALWAYS_DROPPED_COLUMNS and never reach a workbook.
        assert "first_discovered_run_id" in open(
            "src/nodes/finalize_dataset.py", encoding="utf-8"
        ).read()


class TestRawNicheLabelHasOneDefinition:
    """The last column with two copies of its rule -- the footage flags --
    ended up with none in the pipeline at all: the derivation lived only in
    a gapfill script, was run once by hand, and nothing wrote a flag
    afterwards. One definition, called by the node and by any backfill."""

    def test_the_node_calls_the_shared_backfill(self):
        import inspect

        from src.nodes.populate_taxonomy_dimensions import populate_taxonomy_dimensions

        src = inspect.getsource(populate_taxonomy_dimensions)
        assert "backfill_raw_niche_labels" in src

    def test_the_node_keeps_no_copy_of_the_sql(self):
        src = open("src/nodes/populate_taxonomy_dimensions.py", encoding="utf-8").read()
        assert "SET raw_niche_label = nt.niche_name" not in src, (
            "the rule lives in src/tools/niche_labels.py; a second copy is "
            "how the footage flags ended up with no producer at all"
        )

    def test_it_never_overwrites_a_label_already_there(self):
        from unittest.mock import MagicMock

        from src.tools.niche_labels import backfill_raw_niche_labels

        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 3
        assert backfill_raw_niche_labels(conn) == 3
        sql = cur.execute.call_args[0][0]
        assert "cn.raw_niche_label IS NULL" in sql

    def test_it_can_be_scoped(self):
        from unittest.mock import MagicMock

        from src.tools.niche_labels import backfill_raw_niche_labels

        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        backfill_raw_niche_labels(conn, "AND cn.channel_id = ANY(%s) ", (["c1"],))
        sql, params = cur.execute.call_args[0]
        assert "cn.channel_id = ANY(%s)" in sql
        assert params[0] == ["c1"]

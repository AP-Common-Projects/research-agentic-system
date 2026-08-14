"""Cost governors — proof that each ceiling actually fires.

These are the circuit breakers from docs/first-run-plan.md rung 02. Saturation
remains the intended stop condition; these exist so that a bug in the frontier
logic cannot spend a month's record allowance before anyone notices.

Each test here answers one question: does this cap bind, and does it announce
*which* cap bound? A run that stops because it ran out of allowance must never
be mistakable for a run that stopped because the niche was exhausted.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import httpx

from src.config import PROFILES, Config, profile_defaults
from src.graph import route_after_compaction, route_after_select
from src.tools.bright_data import (
    _is_retryable,
    _is_retryable_trigger,
    normalize_channel_ref,
)
from src.nodes.select_next_node import select_next_node
from src.nodes.taxonomy import SYSTEM_PROMPT, _enforce_branch_cap
from src.tools.graph_walk import build_frontier, graph_walk
from src.tools.keyword_search import keyword_search
from src.tools.budget import (
    clamp_frontier,
    clamp_keyword_plan,
    records_remaining,
    tier_b_affordable,
)
from src.tools.saturation import check_saturation
from tests.conftest import make_harness_config

YT = "https://www.youtube.com/"


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

class TestProfiles:
    def test_smoke_is_strictly_cheaper_than_bounded(self):
        smoke, bounded = PROFILES["smoke"], PROFILES["bounded"]
        for key in (
            "max_rounds_per_branch",
            "max_tree_depth",
            "max_branches",
            "keyword_queries_per_round",
            "keyword_results_per_query",
            "graph_walk_frontier_per_round",
            "brightdata_record_budget",
        ):
            assert smoke[key] <= bounded[key], f"{key} must not exceed bounded in smoke"

    def test_smoke_disables_comment_escalation(self):
        """Tier B costs 1 + V + V*C records per channel against Tier A's 1.
        The first live run should not be the one that discovers that."""
        assert PROFILES["smoke"]["graph_walk_escalate_to_comments"] is False

    def test_every_profile_caps_comments_at_ten(self):
        for name, preset in PROFILES.items():
            assert preset["graph_walk_comments_per_video"] == 10, name

    def test_explicit_env_var_outranks_profile(self, monkeypatch):
        """Precedence must be env > profile > field default. pydantic-settings
        ranks init kwargs above env vars, so the preset has to be filtered
        against os.environ or it would silently outrank a deliberate override."""
        monkeypatch.setenv("GRAPH_WALK_FRONTIER_PER_ROUND", "1")
        assert "graph_walk_frontier_per_round" not in profile_defaults("smoke")
        assert Config(profile="smoke").harness.graph_walk_frontier_per_round == 1

    def test_profile_applies_when_env_is_silent(self, monkeypatch):
        monkeypatch.delenv("GRAPH_WALK_FRONTIER_PER_ROUND", raising=False)
        assert Config(profile="smoke").harness.graph_walk_frontier_per_round == 5


# ---------------------------------------------------------------------------
# keyword_search
# ---------------------------------------------------------------------------

class TestKeywordGovernors:
    @staticmethod
    def _state():
        return {
            "tree": {"n1": {"id": "n1", "keywords": ["finance"], "queries_run": ["finance"]}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "novelty_rates": [],
            "rounds_by_node": {},
        }

    @pytest.mark.asyncio
    async def test_queries_truncated_to_cap(self):
        """broaden_or_pivot emits len(keywords) x 6 qualifiers per round. At
        ~469 records per uncapped discovery job that is ~14,000 records and $21
        in a single round."""
        with patch("src.tools.keyword_search.BrightDataClient") as mock_bd, \
             patch("src.tools.keyword_search.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(keyword_queries_per_round=2)
            client = MagicMock()
            mock_bd.return_value = client
            client.discover_channels_by_keyword = AsyncMock(return_value=([], 0))

            await keyword_search(self._state())

        queries = client.discover_channels_by_keyword.call_args[0][0]
        assert len(queries) == 2

    @pytest.mark.asyncio
    async def test_limit_per_input_is_always_passed(self):
        """The single most important cost lever: measured, one uncapped keyword
        returned 469 records where limit_per_input=3 returned exactly 3."""
        with patch("src.tools.keyword_search.BrightDataClient") as mock_bd, \
             patch("src.tools.keyword_search.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(keyword_results_per_query=7)
            client = MagicMock()
            mock_bd.return_value = client
            client.discover_channels_by_keyword = AsyncMock(return_value=([], 0))

            await keyword_search(self._state())

        assert client.discover_channels_by_keyword.call_args.kwargs["limit_per_input"] == 7

    @pytest.mark.asyncio
    async def test_records_and_cost_reach_state(self):
        with patch("src.tools.keyword_search.BrightDataClient") as mock_bd, \
             patch("src.tools.keyword_search.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                brightdata_cost_per_record_usd=0.0015
            )
            client = MagicMock()
            mock_bd.return_value = client
            client.discover_channels_by_keyword = AsyncMock(return_value=([], 40))

            result = await keyword_search(self._state())

        assert result["brightdata_records_used"] == 40
        assert result["budget_spent_usd"] == pytest.approx(0.06)


# ---------------------------------------------------------------------------
# graph_walk frontier
# ---------------------------------------------------------------------------

class TestFrontierGovernors:
    @staticmethod
    def _node(refs):
        return {"id": "n1", "seed_channel_ids": [], "_gw_refs": refs}

    def test_frontier_capped(self):
        refs = {f"{YT}@c{i}": 10_000 for i in range(20)}
        frontier = build_frontier(self._node(refs), set(), min_subscribers=0, limit=5)
        assert len(frontier) == 5

    def test_frontier_drops_channels_below_threshold(self):
        """Expanding the long tail spends a record and returns no edges —
        featured_channels appears on 1% of sub-100-subscriber channels."""
        refs = {f"{YT}@small": 10, f"{YT}@big": 50_000}
        frontier = build_frontier(self._node(refs), set(), min_subscribers=1000, limit=0)
        assert frontier == [f"{YT}@big"]

    def test_frontier_ranks_largest_first(self):
        refs = {f"{YT}@a": 100, f"{YT}@b": 900_000, f"{YT}@c": 5_000}
        frontier = build_frontier(self._node(refs), set(), min_subscribers=0, limit=0)
        assert frontier == [f"{YT}@b", f"{YT}@c", f"{YT}@a"]

    def test_seeds_survive_the_subscriber_filter(self):
        """Taxonomy seeds arrive as bare handles with no subscriber count. If
        the threshold dropped them the walk would have nothing to do on round
        one and would report itself saturated before doing any work."""
        node = {"id": "n1", "seed_channel_ids": ["@seed"], "_gw_refs": {}}
        frontier = build_frontier(node, set(), min_subscribers=100_000, limit=0)
        assert frontier == [f"{YT}@seed"]

    def test_expanded_refs_are_never_re_expanded(self):
        refs = {f"{YT}@a": 5000, f"{YT}@b": 5000}
        frontier = build_frontier(
            self._node(refs), {f"{YT}@a"}, min_subscribers=0, limit=0
        )
        assert frontier == [f"{YT}@b"]

    @pytest.mark.asyncio
    async def test_tier_b_stays_off_when_disabled(self, no_edge_store):
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                graph_walk_escalate_to_comments=False
            )
            client = MagicMock()
            mock_bd.return_value = client
            # A channel with no featured_channels is exactly the case that
            # would otherwise escalate to videos + comments.
            client.get_channels = AsyncMock(
                return_value=(
                    [{"channel_id": "UC_A", "channel_ref": f"{YT}@a",
                      "featured_channel_edges": []}],
                    1,
                )
            )
            client.get_channel_videos = AsyncMock(return_value=([], 0))
            client.get_comments = AsyncMock(return_value=([], 0))

            result = await graph_walk({
                "run_id": "r",
                "tree": {"n1": self._node({f"{YT}@a": 5000})},
                "active_node_id": "n1",
                "discovered_channel_ids": [],
                "expanded_channel_refs": set(),
                "novelty_rates": [],
                "rounds_by_node": {},
            })

        client.get_channel_videos.assert_not_called()
        client.get_comments.assert_not_called()
        assert result["brightdata_records_used"] == 1


# ---------------------------------------------------------------------------
# check_saturation — the run-level breakers
# ---------------------------------------------------------------------------

class TestSaturationGovernors:
    @staticmethod
    def _state(**overrides):
        state = {
            "tree": {"n1": {"id": "n1", "status": "active"}},
            "active_node_id": "n1",
            "budget_spent_usd": 0.0,
            "brightdata_records_used": 0,
            "youtube_quota_used": 0,
            "rounds_by_node": {},
            "saturated_branches": [],
        }
        state.update(overrides)
        return state

    def _run(self, state, **cfg_overrides):
        with patch("src.tools.saturation.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(**cfg_overrides)
            return check_saturation(state)

    def test_record_budget_halts_the_run(self):
        result = self._run(
            self._state(brightdata_records_used=150), brightdata_record_budget=150
        )
        assert result["next_action"] == "budget_exhausted"
        log = result["node_logs"][0]["input_summary"]
        assert log["governor"] == "brightdata_record_budget"

    def test_quota_budget_halts_the_run(self):
        result = self._run(
            self._state(youtube_quota_used=1000), youtube_quota_budget_per_run=1000
        )
        assert result["next_action"] == "budget_exhausted"
        assert result["node_logs"][0]["input_summary"]["governor"] == "youtube_quota_budget_per_run"

    def test_dollar_budget_halts_the_run(self):
        result = self._run(self._state(budget_spent_usd=1.0), budget_limit_usd=1.0)
        assert result["next_action"] == "budget_exhausted"
        assert result["node_logs"][0]["input_summary"]["governor"] == "budget_limit_usd"

    def test_each_governor_is_named_distinctly(self):
        """"Ran out of dollars", "ran out of records" and "ran out of quota"
        have different fixes; conflating them wastes an investigation."""
        names = []
        for state_kw, cfg_kw in (
            ({"budget_spent_usd": 5.0}, {"budget_limit_usd": 1.0}),
            ({"brightdata_records_used": 99}, {"brightdata_record_budget": 10}),
            ({"youtube_quota_used": 99}, {"youtube_quota_budget_per_run": 10}),
        ):
            result = self._run(self._state(**state_kw), **cfg_kw)
            names.append(result["node_logs"][0]["input_summary"]["governor"])
        assert len(set(names)) == 3

    def test_round_cap_force_saturates_the_branch(self):
        """Without this, a branch whose tracks both error every round records
        no novelty history and sets no exhaustion flag, so nothing below can
        ever fire and the graph loops to the recursion limit."""
        result = self._run(self._state(rounds_by_node={"n1": 1}), max_rounds_per_branch=2)
        assert result["next_action"] == "saturated"
        assert result["node_logs"][0]["input_summary"]["reason"] == "max_rounds_per_branch"

    def test_round_counter_advances_even_when_tracks_reported_nothing(self):
        """check_saturation owns the counter precisely because the discovery
        tracks cannot: a guarded failure returns no counter update at all."""
        result = self._run(self._state(), max_rounds_per_branch=5)
        assert result["rounds_by_node"] == {"n1": 1}

    def test_governor_stop_is_distinguishable_from_real_saturation(self):
        capped = self._run(self._state(rounds_by_node={"n1": 9}), max_rounds_per_branch=2)
        genuine = self._run(
            self._state(tree={"n1": {"id": "n1", "status": "active",
                                     "_kw_exhausted": True, "_gw_exhausted": True}}),
            max_rounds_per_branch=0,
        )
        assert capped["node_logs"][0]["input_summary"]["reason"] == "max_rounds_per_branch"
        assert genuine["node_logs"][0]["input_summary"]["reason"] == "both_tracks_exhausted"

    def test_zero_means_uncapped(self):
        result = self._run(
            self._state(brightdata_records_used=10**9, rounds_by_node={"n1": 10**6}),
            brightdata_record_budget=0,
            max_rounds_per_branch=0,
            budget_limit_usd=0,
        )
        assert result["next_action"] == "expand_deeper"

    def test_saturated_branches_returns_a_delta_not_the_whole_list(self):
        """An appending channel fed its own accumulated value doubles every
        round — [X] + [X] = [X, X], then 4, 8, 16. That OOM-killed the
        end-to-end test once the recursion limit was raised enough to bite."""
        result = self._run(
            self._state(saturated_branches=["n1"], rounds_by_node={"n1": 9}),
            max_rounds_per_branch=2,
        )
        assert result["saturated_branches"] == []

    def test_lineage_budget_force_saturates_only_its_subtree(self):
        # ADR-0006 rejected a global-only ceiling because one runaway branch
        # starves the run. Unbounded depth reintroduces that one level up: a
        # deep chain of splits under one root branch must not starve its
        # sibling. Only the offending lineage's subtree force-saturates.
        state = self._state(
            tree={
                "n1": {"id": "n1", "status": "active", "depth": 1, "lineage_root_id": "n1"},
                "n1_child": {"id": "n1_child", "status": "pending", "depth": 2, "lineage_root_id": "n1"},
                "other": {"id": "other", "status": "pending", "depth": 1, "lineage_root_id": "other"},
            },
            branch_lineage_spend={"n1": 10.0},
        )
        result = self._run(state, budget_limit_usd=10.0)
        assert result["next_action"] == "saturated"
        log = result["node_logs"][0]["input_summary"]
        assert log["reason"] == "governor:branch_lineage_budget"
        assert result["tree"]["n1"]["saturation_reason"] == "governor:branch_lineage_budget"
        assert result["tree"]["n1_child"]["saturation_reason"] == "governor:branch_lineage_budget"
        # The sibling lineage is untouched — it keeps its budget.
        assert "other" not in result["tree"]

    def test_lineage_under_share_does_not_fire(self):
        state = self._state(
            tree={
                "n1": {"id": "n1", "status": "active", "depth": 1, "lineage_root_id": "n1"},
                "other": {"id": "other", "status": "pending", "depth": 1, "lineage_root_id": "other"},
            },
            branch_lineage_spend={"n1": 1.0},
        )
        result = self._run(state, budget_limit_usd=10.0)
        assert result["next_action"] == "expand_deeper"

    def test_lineage_budget_disabled_does_not_fire(self):
        state = self._state(
            tree={
                "n1": {"id": "n1", "status": "active", "depth": 1, "lineage_root_id": "n1"},
            },
            branch_lineage_spend={"n1": 100.0},
        )
        result = self._run(state, budget_limit_usd=10.0, branch_lineage_budget_enabled=False)
        assert result["next_action"] == "expand_deeper"


# ---------------------------------------------------------------------------
# Tree growth
# ---------------------------------------------------------------------------

class TestTreeGovernors:
    @staticmethod
    def _tree_with_proposal(depth=0):
        return {
            "parent": {
                "id": "parent",
                "label": "parent",
                "depth": depth,
                "status": "compacted",
                "children_ids": [],
                "proposed_new_nodes": [
                    {"label": "child", "rationale": "r", "seed_channel_ids": ["@x"]}
                ],
            }
        }

    def _run(self, tree, **cfg_overrides):
        with patch("src.nodes.select_next_node.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(**cfg_overrides)
            return select_next_node({"tree": tree, "active_node_id": None, "next_action": ""})

    def test_depth_cap_blocks_the_proposal(self):
        result = self._run(self._tree_with_proposal(depth=1), max_tree_depth=1)
        assert "child" not in result.get("tree", {})
        assert result.get("next_action") == "all_done"

    def test_depth_cap_allows_within_limit(self):
        result = self._run(self._tree_with_proposal(depth=0), max_tree_depth=2)
        assert result["active_node_id"] == "child"

    def test_branch_cap_blocks_new_nodes(self):
        tree = self._tree_with_proposal(depth=0)
        tree["filler"] = {"id": "filler", "label": "f", "depth": 1, "status": "saturated"}
        result = self._run(tree, max_branches=2, max_tree_depth=0)
        assert "child" not in result.get("tree", {})

    def test_uncapped_when_zero(self):
        result = self._run(self._tree_with_proposal(depth=7), max_tree_depth=0, max_branches=0)
        assert result["active_node_id"] == "child"


class TestTaxonomyWidthGovernor:
    """The initial taxonomy was ungoverned — max_branches only ever applied to
    compaction-proposed nodes. Each branch runs its own discovery rounds, so
    branch count multiplies record spend directly."""

    @staticmethod
    def _tree(n):
        tree = {"root": {"id": "root", "depth": 0, "children_ids": [f"b{i}" for i in range(n)]}}
        for i in range(n):
            tree[f"b{i}"] = {"id": f"b{i}", "depth": 1, "parent_id": "root"}
        return tree

    def test_trims_to_the_budget_and_keeps_the_root(self):
        out = _enforce_branch_cap(self._tree(6), 2)
        assert "root" in out
        assert len([n for n in out.values() if n["depth"] != 0]) == 2

    def test_keeps_the_models_priority_order(self):
        """The prompt asks for best-first ordering, so the trim keeps the head
        of the list rather than an arbitrary subset."""
        out = _enforce_branch_cap(self._tree(6), 3)
        assert sorted(k for k in out if k != "root") == ["b0", "b1", "b2"]

    def test_dangling_child_references_are_pruned(self):
        out = _enforce_branch_cap(self._tree(6), 2)
        assert out["root"]["children_ids"] == ["b0", "b1"]

    def test_zero_means_uncapped(self):
        assert len(_enforce_branch_cap(self._tree(6), 0)) == 7

    def test_under_budget_is_untouched(self):
        tree = self._tree(2)
        assert _enforce_branch_cap(tree, 5) == tree

    def test_prompt_substitution_survives_the_embedded_json_schema(self):
        """The system prompt contains a literal JSON schema, so str.format
        would parse its braces as replacement fields."""
        rendered = SYSTEM_PROMPT.replace("{MAX_BRANCHES}", "2")
        assert "AT MOST 2" in rendered
        assert '"nodes"' in rendered, "the schema must survive substitution"
        assert "{MAX_BRANCHES}" not in rendered


# ---------------------------------------------------------------------------
# Findings from the six-point architecture review (2026-08-14)
# ---------------------------------------------------------------------------

class TestBudgetExhaustedIsTerminal:
    """The circuit breaker fires downstream of the spend, so routing has to
    treat it as terminal — otherwise a tripped ceiling still buys another
    discovery round via compaction -> proposed node -> select -> fan-out."""

    def test_select_routes_to_synthesis_when_budget_exhausted(self):
        assert route_after_select({"next_action": "budget_exhausted"}) == ["synthesize"]

    def test_select_still_fans_out_normally(self):
        assert route_after_select({"next_action": "expand_deeper"}) == [
            "keyword_search", "graph_walk",
        ]

    def test_compaction_does_not_re_enter_selection_after_a_ceiling(self):
        state = {
            "next_action": "budget_exhausted",
            "tree": {"n": {"status": "pending", "proposed_new_nodes": [{"label": "x"}]}},
        }
        assert route_after_compaction(state) == ["synthesize"]

    def test_compaction_still_continues_normally(self):
        state = {
            "next_action": "saturated",
            "tree": {"n": {"status": "pending", "proposed_new_nodes": []}},
        }
        assert route_after_compaction(state) == ["select_next_node"]


class TestTriggerRetryDoesNotDoubleBill:
    """trigger is the only request that spends money and the API has no
    idempotency key, so a lost response must not be re-issued."""

    def test_timeout_is_not_retried(self):
        assert _is_retryable_trigger(httpx.TimeoutException("lost")) is False

    def test_server_error_is_not_retried(self):
        req = httpx.Request("POST", "https://x")
        for code in (500, 502, 503, 504):
            exc = httpx.HTTPStatusError(
                "e", request=req, response=httpx.Response(code, request=req)
            )
            assert _is_retryable_trigger(exc) is False, code

    def test_rate_limit_is_retried(self):
        req = httpx.Request("POST", "https://x")
        exc = httpx.HTTPStatusError(
            "e", request=req, response=httpx.Response(429, request=req)
        )
        assert _is_retryable_trigger(exc) is True

    def test_read_path_still_retries_broadly(self):
        """Polling and fetching are free and idempotent — they keep the
        general policy."""
        assert _is_retryable(httpx.TimeoutException("x")) is True


class TestRefCanonicalisation:
    """expanded_channel_refs is the only thing preventing a re-walk from
    re-billing, so equivalent refs must collapse to one key."""

    @pytest.mark.parametrize("variant", [
        "@BenFelixCSI", "@benfelixcsi", "BenFelixCSI",
        "https://www.youtube.com/@BenFelixCSI",
        "https://www.youtube.com/@BenFelixCSI/videos",
        "https://www.youtube.com/@BenFelixCSI?sub_confirmation=1",
        "http://youtube.com/@BenFelixCSI/featured#frag",
    ])
    def test_handle_variants_collapse(self, variant):
        assert normalize_channel_ref(variant) == f"{YT}@benfelixcsi"

    @pytest.mark.parametrize("variant", [
        "UCDXTQ8nWmx_EhZ2v-kp7QxA",
        "https://www.youtube.com/channel/UCDXTQ8nWmx_EhZ2v-kp7QxA",
        "https://www.youtube.com/channel/UCDXTQ8nWmx_EhZ2v-kp7QxA/videos",
    ])
    def test_uc_id_variants_collapse(self, variant):
        assert normalize_channel_ref(variant) == f"{YT}channel/UCDXTQ8nWmx_EhZ2v-kp7QxA"

    def test_legacy_url_forms(self):
        assert normalize_channel_ref("https://www.youtube.com/c/BenFelix") == f"{YT}@benfelix"
        assert normalize_channel_ref("https://www.youtube.com/user/BenFelix") == f"{YT}@benfelix"

    def test_empty_stays_empty(self):
        assert normalize_channel_ref("") == ""
        assert normalize_channel_ref("   ") == ""


class TestNoveltyExcludesTheBacklog:
    """Refs found in earlier rounds that lost the frontier cut are not novel.
    Counting them again keeps the rate high forever, so saturation-on-novelty
    can never fire and every branch dies on a circuit breaker instead."""

    @pytest.mark.asyncio
    async def test_backlog_refs_are_not_counted_as_novel(self, no_edge_store):
        seen, fresh = f"{YT}@backlog", f"{YT}@brandnew"
        state = {
            "run_id": "r",
            "tree": {"n1": {
                "id": "n1",
                "seed_channel_ids": [f"{YT}@seed"],
                # backlog: discovered earlier, never expanded
                "_gw_refs": {seen: 10},
            }},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "expanded_channel_refs": set(),
            "novelty_rates": [],
            "rounds_by_node": {},
        }
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                graph_walk_frontier_per_round=1,
                graph_walk_escalate_to_comments=False,
            )
            client = MagicMock()
            mock_bd.return_value = client
            client.get_channels = AsyncMock(return_value=(
                [{"channel_id": "UC_S", "channel_ref": f"{YT}@seed",
                  "featured_channel_edges": [
                      {"ref": seen, "subscriber_count": 10},    # already known
                      {"ref": fresh, "subscriber_count": 20},   # genuinely new
                  ]}],
                1,
            ))
            client.get_channel_videos = AsyncMock(return_value=([], 0))
            client.get_comments = AsyncMock(return_value=([], 0))
            result = await graph_walk(state)

        # Two refs discovered, one already in the backlog -> half were new.
        # Under the old frontier-size denominator this scored 1.0 (one novel
        # ref over one frontier channel), which is a yield, not a proportion.
        assert result["novelty_rates"][0] == 0.5
        assert 0.0 <= result["novelty_rates"][0] <= 1.0, "novelty must be a proportion"

    @pytest.mark.asyncio
    async def test_novelty_decays_to_zero_when_only_backlog_returns(self, no_edge_store):
        seen = f"{YT}@backlog"
        state = {
            "run_id": "r",
            "tree": {"n1": {"id": "n1", "seed_channel_ids": [f"{YT}@seed"],
                            "_gw_refs": {seen: 10}}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "expanded_channel_refs": set(),
            "novelty_rates": [],
            "rounds_by_node": {},
        }
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                graph_walk_frontier_per_round=1,
                graph_walk_escalate_to_comments=False,
            )
            client = MagicMock()
            mock_bd.return_value = client
            client.get_channels = AsyncMock(return_value=(
                [{"channel_id": "UC_S", "channel_ref": f"{YT}@seed",
                  "featured_channel_edges": [{"ref": seen, "subscriber_count": 10}]}],
                1,
            ))
            client.get_channel_videos = AsyncMock(return_value=([], 0))
            client.get_comments = AsyncMock(return_value=([], 0))
            result = await graph_walk(state)

        assert result["novelty_rates"][0] == 0.0, "re-finding the backlog is not novelty"


class TestTierBFailureKeepsTierASpend:
    """Tier B triggers the long-running jobs, so it is the likely thing to
    raise. If it escaped, _guarded would discard the whole node return —
    losing records Tier A already paid for and the refs it expanded."""

    @pytest.mark.asyncio
    async def test_tier_a_accounting_survives_a_tier_b_failure(self, no_edge_store):
        state = {
            "run_id": "r",
            "tree": {"n1": {"id": "n1", "seed_channel_ids": [f"{YT}@a"], "_gw_refs": {}}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "expanded_channel_refs": set(),
            "novelty_rates": [],
            "rounds_by_node": {},
        }
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                graph_walk_escalate_to_comments=True,
                brightdata_cost_per_record_usd=0.0015,
            )
            client = MagicMock()
            mock_bd.return_value = client
            # Tier A succeeds and bills 3 records; the channel has no edges so
            # Tier B escalates, and Tier B blows up.
            client.get_channels = AsyncMock(return_value=(
                [{"channel_id": "UC_A", "channel_ref": f"{YT}@a",
                  "featured_channel_edges": []}],
                3,
            ))
            client.get_channel_videos = AsyncMock(
                side_effect=RuntimeError("snapshot still running after 900s")
            )
            client.get_comments = AsyncMock(return_value=([], 0))
            result = await graph_walk(state)

        assert result["brightdata_records_used"] == 3, "Tier A spend must be reported"
        assert result["budget_spent_usd"] == pytest.approx(3 * 0.0015)
        # Both identities of the expanded channel: the handle form we asked
        # for, and the /channel/UC… form a later round may meet it under.
        assert result["expanded_channel_refs"] == {
            f"{YT}@a", f"{YT}channel/UC_A",
        }, "must not re-expand next round, under either identifier"
        assert any(e["node_name"] == "graph_walk" for e in result["errors"])


class TestNoveltyIsAComparableProportion:
    """The graph-walk metric used to divide by frontier size, making its
    smallest non-zero value 1/len(frontier) — 0.20 under smoke, 0.067 under
    bounded, both above saturation_novelty_threshold (0.05). `gw_low` was
    therefore satisfiable only at exactly 0.0, so the graph-walk track had no
    graded saturation and branches could only stop on exhaustion or a governor.
    """

    @pytest.mark.asyncio
    async def test_novelty_never_exceeds_one(self, no_edge_store):
        """One frontier channel featuring four others used to score 4.0."""
        state = {
            "run_id": "r",
            "tree": {"n1": {"id": "n1", "seed_channel_ids": [f"{YT}@seed"], "_gw_refs": {}}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "expanded_channel_refs": set(),
            "novelty_rates": [],
            "rounds_by_node": {},
        }
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                graph_walk_frontier_per_round=1, graph_walk_escalate_to_comments=False,
            )
            client = MagicMock()
            mock_bd.return_value = client
            client.get_channels = AsyncMock(return_value=(
                [{"channel_id": "UC_S", "channel_ref": f"{YT}@seed",
                  "featured_channel_edges": [
                      {"ref": f"{YT}@n{i}", "subscriber_count": 1000} for i in range(4)
                  ]}],
                1,
            ))
            client.get_channel_videos = AsyncMock(return_value=([], 0))
            client.get_comments = AsyncMock(return_value=([], 0))
            result = await graph_walk(state)

        assert result["novelty_rates"][0] == 1.0, "all four were new -> 1.0, not 4.0"

    def test_threshold_is_reachable_under_every_profile(self):
        """A proportion can land below 0.05; a 1/frontier yield cannot."""
        for name, preset in PROFILES.items():
            frontier = preset["graph_walk_frontier_per_round"]
            if frontier:
                assert 1 / frontier > 0.05, (
                    f"{name}: this is why the old yield metric could not reach "
                    "the threshold — kept as a record of the bug"
                )
        # The proportion form has no such floor: 1 novel of 100 seen = 0.01.
        assert 1 / 100 < 0.05


class TestPreSpendBudget:
    """check_saturation's ceiling runs downstream of the spend, so it can only
    stop the NEXT round. Measured: every smoke run stopped at 176 records
    against a 150 ceiling. These gates ask before spending."""

    def test_uncapped_budget_returns_none(self):
        assert records_remaining({"brightdata_records_used": 10}, 0) is None

    def test_targets_ninety_percent_not_the_hard_ceiling(self):
        """Headroom for the round already in flight, so the hard ceiling stays
        a backstop instead of the thing that routinely fires."""
        assert records_remaining({"brightdata_records_used": 0}, 1000) == 900

    def test_never_negative(self):
        assert records_remaining({"brightdata_records_used": 5000}, 1000) == 0

    def test_keyword_plan_drops_whole_queries_first(self):
        queries = [f"q{i}" for i in range(6)]
        kept, limit = clamp_keyword_plan(queries, 20, remaining=60)
        assert kept == ["q0", "q1", "q2"] and limit == 20

    def test_keyword_plan_narrows_when_one_query_is_unaffordable(self):
        kept, limit = clamp_keyword_plan(["q0", "q1"], 20, remaining=7)
        assert kept == ["q0"] and limit == 7

    def test_keyword_plan_stops_at_zero(self):
        assert clamp_keyword_plan(["q"], 20, remaining=0)[0] == []

    def test_frontier_clamped_to_remaining(self):
        assert clamp_frontier([f"c{i}" for i in range(10)], remaining=3) == ["c0", "c1", "c2"]

    def test_tier_b_costs_are_priced_before_escalating(self):
        """3 videos x (1 + 10 comments) = 33 records per channel."""
        barren = [f"c{i}" for i in range(10)]
        assert tier_b_affordable(barren, 3, 10, remaining=100) == ["c0", "c1", "c2"]
        assert tier_b_affordable(barren, 3, 10, remaining=32) == []
        assert tier_b_affordable(barren, 3, 10, remaining=None) == barren

    @pytest.mark.asyncio
    async def test_keyword_refuses_to_spend_past_the_ceiling(self):
        state = {
            "tree": {"n1": {"id": "n1", "keywords": ["finance"], "queries_run": []}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "brightdata_records_used": 1000,
            "novelty_rates": [],
            "rounds_by_node": {},
        }
        with patch("src.tools.keyword_search.BrightDataClient") as mock_bd, \
             patch("src.tools.keyword_search.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(brightdata_record_budget=150)
            result = await keyword_search(state)

        mock_bd.assert_not_called()
        assert "budget" in result["node_logs"][0]["input_summary"]["reason"]


class TestPaidCallFailureIsCharged:
    """A trigger that succeeds server-side and then fails on poll is billing.
    Letting the exception escape hands the node to _guarded, which returns
    neither the spend nor the 'already done' marker — so the next round
    reissues the identical paid job."""

    @pytest.mark.asyncio
    async def test_keyword_failure_charges_and_marks_queries_consumed(self):
        state = {
            "tree": {"n1": {"id": "n1", "keywords": ["finance"], "queries_run": []}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "brightdata_records_used": 0,
            "novelty_rates": [],
            "rounds_by_node": {},
        }
        with patch("src.tools.keyword_search.BrightDataClient") as mock_bd, \
             patch("src.tools.keyword_search.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                keyword_queries_per_round=2, keyword_results_per_query=10,
                brightdata_cost_per_record_usd=0.0015,
            )
            client = MagicMock()
            mock_bd.return_value = client
            client.discover_channels_by_keyword = AsyncMock(
                side_effect=RuntimeError("snapshot still running after 900s")
            )
            result = await keyword_search(state)

        # Round one issues only the bare keyword (broaden_or_pivot adds
        # qualifiers from round two), so worst case is 1 query x limit 10.
        assert result["brightdata_records_used"] == 10, "worst case must be charged"
        assert result["budget_spent_usd"] == pytest.approx(10 * 0.0015)
        assert result["tree"]["n1"]["queries_run"], "queries must not be reissued"
        assert any(e["node_name"] == "keyword_search" for e in result["errors"])

    @pytest.mark.asyncio
    async def test_tier_a_failure_charges_and_marks_frontier_expanded(self, no_edge_store):
        state = {
            "run_id": "r",
            "tree": {"n1": {"id": "n1", "seed_channel_ids": [f"{YT}@a", f"{YT}@b"],
                            "_gw_refs": {}}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "expanded_channel_refs": set(),
            "brightdata_records_used": 0,
            "novelty_rates": [],
            "rounds_by_node": {},
        }
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                brightdata_cost_per_record_usd=0.0015
            )
            client = MagicMock()
            mock_bd.return_value = client
            client.get_channels = AsyncMock(side_effect=RuntimeError("poll timeout"))
            result = await graph_walk(state)

        assert result["brightdata_records_used"] == 2
        assert result["expanded_channel_refs"] == {f"{YT}@a", f"{YT}@b"}, (
            "must not rebuild and re-buy the identical frontier next round"
        )
        assert any(e["node_name"] == "graph_walk" for e in result["errors"])


class TestNoveltyPathIsReachable:
    """check_saturation tests the round cap BEFORE the novelty window, and
    history length equals round number — so novelty is unreachable unless
    max_rounds_per_branch exceeds saturation_consecutive_window. Before this
    was fixed, 100% of runs in every capped profile stopped on a governor."""

    def test_every_profile_can_reach_novelty_saturation(self):
        window = 3
        for name, preset in PROFILES.items():
            rounds = preset["max_rounds_per_branch"]
            assert rounds == 0 or rounds > window, (
                f"{name}: max_rounds_per_branch={rounds} <= window={window}, "
                "so novelty_below_threshold can never fire"
            )


class TestSeedsAreNotDiscoveries:
    """Taxonomy seeds enter round one's frontier directly from the LLM's
    guess. Crediting them to the graph-walk track inflates
    graph_walk_exclusive_count — the one number this project exists to
    produce. Measured on the rung-04 run: @GrahamStephan and @AndreiJikh were
    both seeds and both counted as walk-exclusive finds."""

    @staticmethod
    def _state(seeds, gw_refs=None):
        return {
            "run_id": "r",
            "tree": {"n1": {"id": "n1", "seed_channel_ids": list(seeds),
                            "_gw_refs": dict(gw_refs or {})}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "expanded_channel_refs": set(),
            "novelty_rates": [],
            "rounds_by_node": {},
        }

    @pytest.mark.asyncio
    async def test_seed_is_not_credited_to_the_walk(self, no_edge_store):
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                graph_walk_escalate_to_comments=False
            )
            client = MagicMock()
            mock_bd.return_value = client
            client.get_channels = AsyncMock(return_value=(
                [{"channel_id": "UC_SEED", "channel_ref": f"{YT}@grahamstephan",
                  "featured_channel_edges": []}],
                1,
            ))
            client.get_channel_videos = AsyncMock(return_value=([], 0))
            client.get_comments = AsyncMock(return_value=([], 0))
            result = await graph_walk(self._state(["@GrahamStephan"]))

        assert result["graph_walk_channel_ids"] == set(), (
            "an LLM-guessed seed is an entry point, not a discovery"
        )

    @pytest.mark.asyncio
    async def test_seed_is_still_hydrated_and_marked_expanded(self, no_edge_store):
        """Excluding seeds from attribution must not drop them from the run —
        they still belong in the store, the signals and the report."""
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                graph_walk_escalate_to_comments=False
            )
            client = MagicMock()
            mock_bd.return_value = client
            client.get_channels = AsyncMock(return_value=(
                [{"channel_id": "UC_SEED", "channel_ref": f"{YT}@grahamstephan",
                  "featured_channel_edges": []}],
                1,
            ))
            client.get_channel_videos = AsyncMock(return_value=([], 0))
            client.get_comments = AsyncMock(return_value=([], 0))
            result = await graph_walk(self._state(["@GrahamStephan"]))

        assert result["discovered_channel_ids"] == ["UC_SEED"], "must still hydrate"
        assert f"{YT}channel/UC_SEED" in result["expanded_channel_refs"]

    @pytest.mark.asyncio
    async def test_a_channel_reached_by_an_edge_is_credited(self, no_edge_store):
        """The genuine case must still count — otherwise the fix would zero
        out the metric rather than correct it."""
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config(
                graph_walk_escalate_to_comments=False
            )
            client = MagicMock()
            mock_bd.return_value = client
            client.get_channels = AsyncMock(return_value=(
                [{"channel_id": "UC_REACHED", "channel_ref": f"{YT}@reached",
                  "featured_channel_edges": []}],
                1,
            ))
            client.get_channel_videos = AsyncMock(return_value=([], 0))
            client.get_comments = AsyncMock(return_value=([], 0))
            # @reached came from a previous round's edges, not from seeds.
            state = self._state(["@somethingelse"], gw_refs={f"{YT}@reached": 5000})
            result = await graph_walk(state)

        assert result["graph_walk_channel_ids"] == {"UC_REACHED"}


class TestOneBadChannelCannotEndARun:
    """hydrate_metadata is the fan-in join and is NOT wrapped by graph.py's
    _guarded, so anything escaping it ends the run — after that round's Bright
    Data records are already paid for. A single channel whose uploads playlist
    404s ended a live run at 64 records."""

    def test_dead_uploads_playlist_returns_empty_not_raises(self):
        from src.tools.youtube_api import YouTubeAPIClient

        client = YouTubeAPIClient()
        request = httpx.Request("GET", "https://www.googleapis.com/youtube/v3/playlistItems")

        def fake_get(endpoint, params):
            if endpoint == "channels":
                return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUdead"}}}]}
            raise httpx.HTTPStatusError(
                "404", request=request, response=httpx.Response(404, request=request)
            )

        with patch.object(YouTubeAPIClient, "_get", side_effect=fake_get):
            assert client.get_channel_videos("UCdead") == []

    def test_a_real_error_still_propagates(self):
        """403-quota and 404-missing are facts about one channel; a 500 is not,
        and must not be silently swallowed into an empty video list."""
        from src.tools.youtube_api import YouTubeAPIClient

        client = YouTubeAPIClient()
        request = httpx.Request("GET", "https://x")

        def fake_get(endpoint, params):
            if endpoint == "channels":
                return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}}]}
            raise httpx.HTTPStatusError(
                "500", request=request, response=httpx.Response(500, request=request)
            )

        with patch.object(YouTubeAPIClient, "_get", side_effect=fake_get):
            with pytest.raises(httpx.HTTPStatusError):
                client.get_channel_videos("UCx")

    def test_hydration_survives_one_failing_channel(self):
        """The other channels' metadata must still land, and the failure must
        be recorded rather than swallowed."""
        from src.tools.hydrate_metadata import hydrate_metadata

        state = {
            "discovered_channel_ids": ["UC_ok", "UC_bad"],
            "hydrated_channel_ids": set(),
            "keyword_channel_ids": {"UC_ok", "UC_bad"},
            "graph_walk_channel_ids": set(),
            "youtube_quota_used": 0,
            "thread_id": "t",
        }
        yt = MagicMock()
        yt.get_channels.return_value = [
            {"channel_id": "UC_ok", "title": "fine"},
            {"channel_id": "UC_bad", "title": "dead"},
        ]
        yt.get_channel_videos.side_effect = [
            [{"video_id": "v1", "channel_id": "UC_ok", "view_count": 10}],
            RuntimeError("404 uploads playlist"),
        ]
        yt.get_quota_used.return_value = 4
        yt.quota_consumed_this_call.return_value = 4

        with patch("src.tools.hydrate_metadata.YouTubeAPIClient", return_value=yt), \
             patch("src.db.connection.get_connection"), \
             patch("src.db.connection.put_connection"), \
             patch("src.tools.dedup.persist_channel"), \
             patch("src.tools.dedup.persist_video"):
            result = hydrate_metadata(state)

        assert "v1" in result["discovered_video_ids"], "the good channel must survive"
        assert result["hydrated_channel_ids"] == {"UC_ok", "UC_bad"}
        assert any("UC_bad" in e["message"] for e in result["errors"]), (
            "the failure must be recorded, not swallowed"
        )


class TestSignalsActuallyGetComputed:
    """engagement_rate, cadence and velocity were never computed or stored in
    any run — 425 channels, zero signals — while score_signals still logged a
    channels_scored count and reported success. Three compounding causes:
    published_at is a str from the API but a datetime from Postgres,
    _parse_timestamp only caught ValueError (strptime raises TypeError on a
    datetime), and the channel loop sat inside one outer try/except."""

    def test_parse_accepts_the_shape_postgres_returns(self):
        from datetime import datetime, timezone
        from src.tools.signal_scoring import _parse_timestamp

        aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert _parse_timestamp(aware) == aware
        assert _parse_timestamp(datetime(2026, 1, 1)).tzinfo is timezone.utc
        assert _parse_timestamp("2026-01-01T00:00:00Z") is not None
        assert _parse_timestamp(None) is None
        assert _parse_timestamp(12345) is None

    def test_cadence_and_velocity_survive_undated_rows(self):
        """Sorting by a key that can return None raises TypeError the moment
        one row lacks a usable date."""
        from datetime import datetime, timezone
        from src.tools.signal_scoring import compute_cadence, compute_velocity

        vids = [
            {"published_at": datetime(2026, 1, i + 1, tzinfo=timezone.utc),
             "view_count": 100 * (i + 1)}
            for i in range(10)
        ]
        vids.append({"published_at": None, "view_count": 5})
        assert compute_cadence(vids) > 0
        assert compute_velocity(vids) > 0

    def test_signals_are_computed_from_datetime_rows(self):
        """End to end through score_signals with the store's real shape."""
        from datetime import datetime, timezone
        from src.tools.signal_scoring import score_signals

        rows = [
            {"video_id": f"v{i}", "channel_id": "UC_a", "view_count": 1000 * (i + 1),
             "like_count": 50, "comment_count": 5,
             "published_at": datetime(2026, 1, i + 1, tzinfo=timezone.utc)}
            for i in range(8)
        ]
        persisted = {}

        def fake_persist(conn, ch_id, signals):
            persisted[ch_id] = signals

        with patch("src.db.connection.get_connection"), \
             patch("src.db.connection.put_connection"), \
             patch("src.tools.dedup.fetch_videos_by_channels", return_value=rows), \
             patch("src.tools.dedup.persist_channel_signals", side_effect=fake_persist):
            result = score_signals({"discovered_channel_ids": ["UC_a"], "thread_id": "t"})

        assert persisted, "signals must actually reach the store"
        assert set(persisted["UC_a"]) == {"engagement_rate", "cadence", "velocity"}
        assert persisted["UC_a"]["cadence"] > 0
        assert result["node_logs"][0]["input_summary"]["channels_scored"] == 1

    def test_one_bad_channel_does_not_zero_the_round(self):
        from src.tools.signal_scoring import score_signals

        rows = [
            {"video_id": "v1", "channel_id": "UC_ok", "view_count": 100,
             "like_count": 1, "comment_count": 1, "published_at": "2026-01-01T00:00:00Z"},
            {"video_id": "v2", "channel_id": "UC_bad", "view_count": 100,
             "like_count": 1, "comment_count": 1, "published_at": "2026-01-02T00:00:00Z"},
        ]
        calls = []

        def flaky(conn, ch_id, signals):
            calls.append(ch_id)
            if ch_id == "UC_bad":
                raise RuntimeError("boom")

        with patch("src.db.connection.get_connection"), \
             patch("src.db.connection.put_connection"), \
             patch("src.tools.dedup.fetch_videos_by_channels", return_value=rows), \
             patch("src.tools.dedup.persist_channel_signals", side_effect=flaky):
            result = score_signals(
                {"discovered_channel_ids": ["UC_ok", "UC_bad"], "thread_id": "t"}
            )

        assert "UC_ok" in calls and "UC_bad" in calls
        summary = result["node_logs"][0]["input_summary"]
        assert summary["channels_scored"] == 1, "must count successes, not attempts"
        assert summary["scoring_errors"] == 1


class TestOutageIsNotSaturation:
    """Observed live: the Bright Data account was deactivated mid-run
    ("Customer is not active"), every call failed, and two branches reported
    `novelty_below_threshold` — because a failed round was scored 0.0, which
    satisfies the window. A dead vendor must never read as an exhausted niche.
    """

    @staticmethod
    def _state(kw, gw, **kw2):
        state = {
            "tree": {"n1": {"id": "n1", "status": "active",
                            "_kw_novelty_history": kw, "_gw_novelty_history": gw}},
            "active_node_id": "n1",
            "budget_spent_usd": 0.0,
            "brightdata_records_used": 0,
            "youtube_quota_used": 0,
            "rounds_by_node": {},
            "saturated_branches": [],
        }
        state.update(kw2)
        return state

    def _run(self, state, **cfg):
        with patch("src.tools.saturation.get_config") as c:
            c.return_value.harness = make_harness_config(**cfg)
            return check_saturation(state)

    def test_unmeasured_rounds_do_not_satisfy_the_threshold(self):
        result = self._run(self._state([None, None, None], [None, None, None]))
        assert result["node_logs"][0]["input_summary"]["reason"] != "novelty_below_threshold"

    def test_total_outage_is_named_explicitly(self):
        result = self._run(self._state([None, None, None], [None, None, None]))
        assert result["next_action"] == "saturated"
        assert result["node_logs"][0]["input_summary"]["reason"] == "discovery_unavailable"

    def test_genuine_low_novelty_still_saturates(self):
        """The fix must not disable the real stop condition."""
        result = self._run(self._state([0.0, 0.01, 0.0], [0.0, 0.0, 0.02]))
        assert result["node_logs"][0]["input_summary"]["reason"] == "novelty_below_threshold"

    def test_one_failed_round_blocks_saturation(self):
        """Two genuine zeros and one unmeasured round is not three rounds of
        evidence — it is two."""
        result = self._run(self._state([0.0, None, 0.0], [0.0, 0.0, 0.0]))
        assert result["next_action"] == "expand_deeper"

    def test_partial_outage_does_not_trip_the_outage_stop(self):
        """One track down, the other still measuring, is not an outage — the
        run can continue on the working track."""
        result = self._run(self._state([None, None, None], [0.5, 0.6, 0.7]))
        assert result["next_action"] == "expand_deeper"

    @pytest.mark.asyncio
    async def test_keyword_failure_records_no_measurement(self):
        state = {
            "tree": {"n1": {"id": "n1", "keywords": ["finance"], "queries_run": [],
                            "_kw_novelty_history": [0.4]}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "brightdata_records_used": 0,
            "novelty_rates": [],
            "rounds_by_node": {},
        }
        with patch("src.tools.keyword_search.BrightDataClient") as mock_bd, \
             patch("src.tools.keyword_search.get_config") as cfg:
            cfg.return_value.harness = make_harness_config()
            client = MagicMock()
            mock_bd.return_value = client
            client.discover_channels_by_keyword = AsyncMock(
                side_effect=RuntimeError("Customer is not active")
            )
            result = await keyword_search(state)

        assert result["tree"]["n1"]["_kw_novelty_history"] == [0.4, None]
        assert "novelty_rates" not in result, "a failed round contributes no rate"

    @pytest.mark.asyncio
    async def test_tier_a_failure_records_no_measurement(self, no_edge_store):
        state = {
            "run_id": "r",
            "tree": {"n1": {"id": "n1", "seed_channel_ids": [f"{YT}@a"],
                            "_gw_refs": {}, "_gw_novelty_history": [0.3]}},
            "active_node_id": "n1",
            "discovered_channel_ids": [],
            "expanded_channel_refs": set(),
            "brightdata_records_used": 0,
            "novelty_rates": [],
            "rounds_by_node": {},
        }
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd, \
             patch("src.tools.graph_walk.get_config") as cfg:
            cfg.return_value.harness = make_harness_config()
            client = MagicMock()
            mock_bd.return_value = client
            client.get_channels = AsyncMock(side_effect=RuntimeError("Customer is not active"))
            result = await graph_walk(state)

        assert result["tree"]["n1"]["_gw_novelty_history"] == [0.3, None]


class TestAccountLevelBudget:
    """brightdata_record_budget stops ONE run. Nothing stopped the Nth run
    from spending it again — which is how a 5,000-record allowance went with
    no individual run misbehaving. This is the wallet-level ceiling."""

    @staticmethod
    def _ledger(tmp_path, events):
        spend = tmp_path / "spend"
        spend.mkdir(parents=True, exist_ok=True)
        for i, evs in enumerate(events):
            with (spend / f"run-{i}.jsonl").open("w") as fh:
                for phase, n in evs:
                    fh.write(json.dumps({"phase": phase, "worst_case_records": n}) + "\n")
        return tmp_path

    def test_totals_collected_events_across_runs(self, tmp_path):
        from src.observability.logging_config import total_records_spent

        base = self._ledger(tmp_path, [[("collected", 100)], [("collected", 250)]])
        assert total_records_spent(base) == 350

    def test_trigger_intent_is_not_counted_as_spend(self, tmp_path):
        """Trigger lines are written before the POST as intent. Counting them
        alongside `collected` would double-bill every successful job."""
        from src.observability.logging_config import total_records_spent

        base = self._ledger(tmp_path, [[
            ("trigger", 100), ("triggered", 100), ("collected", 80),
        ]])
        assert total_records_spent(base) == 80

    def test_empty_ledger_is_zero_not_an_error(self, tmp_path):
        from src.observability.logging_config import total_records_spent

        assert total_records_spent(tmp_path) == 0

    def test_malformed_line_does_not_break_the_total(self, tmp_path):
        from src.observability.logging_config import total_records_spent

        spend = tmp_path / "spend"
        spend.mkdir(parents=True)
        (spend / "r.jsonl").write_text(
            json.dumps({"phase": "collected", "worst_case_records": 40})
            + "\nnot json at all\n"
            + json.dumps({"phase": "collected", "worst_case_records": 10}) + "\n"
        )
        # A corrupt line must not zero the wallet check — that would silently
        # remove the ceiling.
        assert total_records_spent(tmp_path) == 40

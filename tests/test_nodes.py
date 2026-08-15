"""Tests for src/nodes — all LLM calls mocked.

Coverage per the Track B spec:
1. build_taxonomy: valid/invalid JSON, retry, tree fields, node_log
2. compact_branch: store numbers in prompt, proposed nodes, branch_compactions
3. select_next_node: BFS ordering, proposed priority, all_done
4. synthesize + grade_findings: evidence grading axes, grade rules, cannot_determine
5. Node logs recorded with cost/latency

Async nodes are tested with `with patch(...)` context managers (decorator-based
@patch wraps the coroutine in a sync function, which pytest-asyncio cannot run).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.nodes.compact_branch import compact_branch
from src.nodes.select_next_node import select_next_node
from src.nodes.synthesize import synthesize, grade_findings
from src.nodes.taxonomy import build_taxonomy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_state():
    return {
        "run_id": "run-001",
        "thread_id": "thread-001",
        "selected_niche": "ai coding assistants",
        "niche_scanner_evidence": {
            "niche_name": "ai coding assistants",
            "opportunity_score": 0.78,
            "channel_count_estimate": 1500,
            "avg_views": 45000,
            "avg_subs": 120000,
            "growth_indicators": ["rising search volume", "new tools weekly"],
            "saturation_signal": "medium",
        },
        "tree": {},
        "active_node_id": None,
        "discovered_channel_ids": [],
        "discovered_video_ids": [],
        "visited_channel_ids": set(),
        "expanded_channel_ids": set(),
        "branch_compactions": [],
        "novelty_rates": [],
        "saturated_branches": [],
        "budget_spent_usd": 0.0,
        "next_action": "",
        "messages": [],
        "errors": [],
        "node_logs": [],
        "schema_version": 1,
        "final_report": None,
        "keyword_search_done": False,
        "graph_walk_done": False,
    }


def _fake_llm_response(content: str) -> dict:
    return {
        "content": content,
        "usage": {"prompt_tokens": 500, "completion_tokens": 300},
        "cost_usd": 0.0005,
        "model": "deepseek-v4-pro",
        "latency_ms": 1200.0,
    }


# ---------------------------------------------------------------------------
# build_taxonomy
# ---------------------------------------------------------------------------

TAXONOMY_VALID_JSON = json.dumps({
    "nodes": [
        {
            "id": "root",
            "label": "AI Coding Assistants",
            "depth": 0,
            "parent_id": None,
            "keywords": ["ai coding", "copilot", "code generation"],
            "seed_channels": ["@github", "@cursor_ai", "@vscode"],
        },
        {
            "id": "copilot_ecosystem",
            "label": "Copilot Ecosystem & Extensions",
            "depth": 1,
            "parent_id": "root",
            "keywords": ["github copilot", "copilot extensions", "copilot chat"],
            "seed_channels": ["@github", "@microsoft_dev"],
        },
        {
            "id": "open_source_alternatives",
            "label": "Open-Source Alternatives",
            "depth": 1,
            "parent_id": "root",
            "keywords": ["tabby", "codeium", "continue dev", "ollama"],
            "seed_channels": ["@tabbyml", "@codeium", "@continuedev"],
        },
        {
            "id": "cursor_tips",
            "label": "Cursor Tips & Workflows",
            "depth": 1,
            "parent_id": "root",
            "keywords": ["cursor ai", "cursor rules", "cursor workflows"],
            "seed_channels": ["@cursor_ai", "@aisdk"],
        },
    ]
})

TAXONOMY_INVALID_JSON = "not valid json at all {{{"


class TestBuildTaxonomy:

    @pytest.mark.asyncio
    async def test_valid_json_produces_correct_tree(self, base_state):
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_VALID_JSON)
            result = await build_taxonomy(base_state)

        tree = result["tree"]
        assert "root" in tree
        assert tree["root"]["depth"] == 0
        assert tree["root"]["status"] == "pending"
        assert tree["root"]["label"] == "AI Coding Assistants"
        assert result["active_node_id"] == "root"

    @pytest.mark.asyncio
    async def test_all_branches_have_correct_depth(self, base_state):
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_VALID_JSON)
            result = await build_taxonomy(base_state)

        tree = result["tree"]
        for nid, node in tree.items():
            if nid != "root":
                assert node["depth"] == 1, f"{nid} should be depth 1"
                assert node["parent_id"] == "root"

    @pytest.mark.asyncio
    async def test_keywords_present_on_all_nodes(self, base_state):
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_VALID_JSON)
            result = await build_taxonomy(base_state)

        for node in result["tree"].values():
            assert isinstance(node["keywords"], list)
            assert len(node["keywords"]) > 0

    @pytest.mark.asyncio
    async def test_seed_channel_ids_present_on_all_nodes(self, base_state):
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_VALID_JSON)
            result = await build_taxonomy(base_state)

        for node in result["tree"].values():
            assert isinstance(node["seed_channel_ids"], list)
            assert len(node["seed_channel_ids"]) > 0

    @pytest.mark.asyncio
    async def test_invalid_json_retries_then_errors(self, base_state):
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_INVALID_JSON)
            result = await build_taxonomy(base_state)

        assert mock_complete.call_count == 2
        assert len(result.get("errors", [])) > 0
        assert result["errors"][0]["error_type"] in ("JSONDecodeError", "ValueError")
        assert result["tree"] == {}
        assert result["active_node_id"] is None

    @pytest.mark.asyncio
    async def test_valid_on_second_attempt(self, base_state):
        calls = [
            _fake_llm_response(TAXONOMY_INVALID_JSON),
            _fake_llm_response(TAXONOMY_VALID_JSON),
        ]
        with patch("src.nodes.taxonomy.complete_tier", side_effect=calls) as mock_complete:
            result = await build_taxonomy(base_state)

        assert mock_complete.call_count == 2
        assert "root" in result["tree"]
        assert result["active_node_id"] == "root"

    @pytest.mark.asyncio
    async def test_records_node_log_with_cost_and_latency(self, base_state):
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_VALID_JSON)
            result = await build_taxonomy(base_state)

        logs = result.get("node_logs", [])
        assert len(logs) >= 1
        log = logs[0]
        assert log["node_name"] == "build_taxonomy"
        assert log["latency_ms"] is not None
        assert log["cost_usd"] is not None

    @pytest.mark.asyncio
    async def test_branches_count_between_3_and_6(self, base_state):
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_VALID_JSON)
            result = await build_taxonomy(base_state)

        branch_count = sum(1 for n in result["tree"].values() if n["depth"] > 0)
        assert 3 <= branch_count <= 6

    @pytest.mark.asyncio
    async def test_schema_version_set_on_nodes(self, base_state):
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_VALID_JSON)
            result = await build_taxonomy(base_state)

        for node in result["tree"].values():
            assert node.get("schema_version") == 1


# ---------------------------------------------------------------------------
# compact_branch
# ---------------------------------------------------------------------------

COMPACTION_VALID_JSON = json.dumps({
    "narrative_summary": "The Copilot ecosystem shows high engagement from developers sharing extension workflows."
    " Three channels consistently produce outlier videos when covering new Copilot features."
    " The open-source segment has lower average views but faster subscriber growth, suggesting an underserved niche.",
    "key_patterns": [
        {
            "pattern_name": "extension reviews",
            "description": "Videos reviewing Copilot extensions get 3x normal views",
            "evidence_summary": "Channels @github, @microsoft_dev show this pattern across 12 videos",
        }
    ],
    "proposed_nodes": [
        {
            "label": "VSCode Extension Dev",
            "rationale": "These channels focus on VSCode extension development broadly, not just AI coding",
            "seed_channel_ids": ["ch_vscode_ext_1", "ch_vscode_ext_2"],
        }
    ],
})


@pytest.fixture
def state_with_tree(base_state):
    base_state["tree"] = {
        "copilot_ecosystem": {
            "id": "copilot_ecosystem",
            "label": "Copilot Ecosystem",
            "depth": 1,
            "parent_id": "root",
            "keywords": ["copilot", "extensions"],
            "seed_channel_ids": ["ch_github", "ch_msdev", "ch_cursor"],
            "unexpanded_channel_ids": ["ch_github", "ch_msdev", "ch_cursor", "ch_fireship"],
            "children_ids": [],
            "status": "active",
            "compaction_summary": None,
            "proposed_new_nodes": [],
            "schema_version": 1,
        },
        "root": {
            "id": "root",
            "label": "AI Coding",
            "depth": 0,
            "parent_id": None,
            "keywords": ["ai coding"],
            "seed_channel_ids": [],
            "unexpanded_channel_ids": [],
            "children_ids": ["copilot_ecosystem"],
            "status": "saturated",
            "compaction_summary": None,
            "proposed_new_nodes": [],
            "schema_version": 1,
        },
    }
    base_state["active_node_id"] = "copilot_ecosystem"
    return base_state


@pytest.fixture
def mock_channels():
    return [
        {"channel_id": "ch_github", "title": "GitHub", "subscriber_count": 500000, "first_seen_at": "2026-01-15", "description": "", "discovery_method": "keyword"},
        {"channel_id": "ch_msdev", "title": "Microsoft Developer", "subscriber_count": 2000000, "first_seen_at": "2026-01-15", "description": "", "discovery_method": "keyword"},
        {"channel_id": "ch_cursor", "title": "Cursor AI", "subscriber_count": 150000, "first_seen_at": "2026-02-01", "description": "", "discovery_method": "graph_walk"},
        {"channel_id": "ch_fireship", "title": "Fireship", "subscriber_count": 3000000, "first_seen_at": "2026-01-10", "description": "", "discovery_method": "keyword"},
    ]


@pytest.fixture
def mock_videos():
    return [
        {"video_id": "v1", "channel_id": "ch_github", "title": "Copilot Extensions Guide", "view_count": 150000, "like_count": 5000, "comment_count": 300, "published_at": "2026-06-01", "outlier_score": 3.2},
        {"video_id": "v2", "channel_id": "ch_github", "title": "GitHub Models", "view_count": 80000, "like_count": 3000, "comment_count": 200, "published_at": "2026-05-15", "outlier_score": 1.5},
        {"video_id": "v3", "channel_id": "ch_msdev", "title": "VS Code AI Features", "view_count": 200000, "like_count": 8000, "comment_count": 500, "published_at": "2026-06-10", "outlier_score": 4.1},
        {"video_id": "v4", "channel_id": "ch_cursor", "title": "Cursor Rules Tutorial", "view_count": 50000, "like_count": 2000, "comment_count": 150, "published_at": "2026-07-01", "outlier_score": 2.8},
    ]


class TestCompactBranch:

    @pytest.mark.asyncio
    async def test_prompt_contains_real_store_numbers(self, state_with_tree, mock_channels, mock_videos):
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_complete.return_value = _fake_llm_response(COMPACTION_VALID_JSON)
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            await compact_branch(state_with_tree)

            call_prompt = mock_complete.call_args[0][1]
            assert "150000" in call_prompt, "Store view count 150000 not found in LLM prompt"
            assert "ch_github" in call_prompt
            assert "3.2" in call_prompt, "Store outlier_score 3.2 not found in LLM prompt"

    @pytest.mark.asyncio
    async def test_narrative_summary_stored(self, state_with_tree, mock_channels, mock_videos):
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_complete.return_value = _fake_llm_response(COMPACTION_VALID_JSON)
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            result = await compact_branch(state_with_tree)

        compactions = result.get("branch_compactions", [])
        assert len(compactions) == 1
        comp = compactions[0]
        assert comp["node_id"] == "copilot_ecosystem"
        assert "Copilot ecosystem" in comp["narrative_summary"]
        assert len(comp["key_patterns"]) >= 1
        assert comp["channel_count"] == len(mock_channels)
        assert comp["video_count"] == len(mock_videos)

    @pytest.mark.asyncio
    async def test_proposed_node_routed_correctly(self, state_with_tree, mock_channels, mock_videos):
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_complete.return_value = _fake_llm_response(COMPACTION_VALID_JSON)
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            result = await compact_branch(state_with_tree)

        updated_node = result.get("tree", {}).get("copilot_ecosystem", {})
        assert len(updated_node.get("proposed_new_nodes", [])) == 1
        proposed = updated_node["proposed_new_nodes"][0]
        assert proposed["label"] == "VSCode Extension Dev"
        assert proposed["parent_id"] == "copilot_ecosystem"
        assert len(proposed["seed_channel_ids"]) == 2

        comp = result["branch_compactions"][0]
        assert len(comp["proposed_nodes"]) == 1

    @pytest.mark.asyncio
    async def test_node_status_updated_to_compacted(self, state_with_tree, mock_channels, mock_videos):
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_complete.return_value = _fake_llm_response(COMPACTION_VALID_JSON)
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            result = await compact_branch(state_with_tree)

        updated_node = result["tree"]["copilot_ecosystem"]
        assert updated_node["status"] == "compacted"
        assert updated_node["compaction_summary"] is not None

    @pytest.mark.asyncio
    async def test_branch_compactions_appended(self, state_with_tree, mock_channels, mock_videos):
        state_with_tree["branch_compactions"] = [{"existing": "compaction"}]
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_complete.return_value = _fake_llm_response(COMPACTION_VALID_JSON)
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            result = await compact_branch(state_with_tree)

        assert len(result["branch_compactions"]) == 1

    @pytest.mark.asyncio
    async def test_records_node_log(self, state_with_tree, mock_channels, mock_videos):
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_complete.return_value = _fake_llm_response(COMPACTION_VALID_JSON)
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            result = await compact_branch(state_with_tree)

        logs = result.get("node_logs", [])
        assert len(logs) >= 1
        assert logs[0]["node_name"] == "compact_branch"
        assert "latency_ms" in logs[0]
        assert "cost_usd" in logs[0]

    @pytest.mark.asyncio
    async def test_invalid_active_node_id_returns_error(self, base_state):
        base_state["active_node_id"] = "nonexistent"
        base_state["tree"] = {}
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete:
            result = await compact_branch(base_state)

        assert len(result.get("errors", [])) > 0
        mock_complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_on_invalid_json(self, state_with_tree, mock_channels, mock_videos):
        calls = [
            _fake_llm_response("not json {{{"),
            _fake_llm_response(COMPACTION_VALID_JSON),
        ]
        with patch("src.nodes.compact_branch.complete_tier", side_effect=calls) as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            result = await compact_branch(state_with_tree)

        assert mock_complete.call_count == 2
        assert len(result.get("branch_compactions", [])) == 1

    def test_prompt_does_not_invent_numbers(self):
        node = {
            "id": "test_node",
            "label": "Test",
            "depth": 1,
            "keywords": ["test"],
            "schema_version": 1,
        }
        channels = [
            {"channel_id": "ch1", "title": "C1", "subscriber_count": 10000, "first_seen_at": "2026-01-01", "discovery_method": "keyword"},
        ]
        videos = [
            {"video_id": "v1", "channel_id": "ch1", "title": "V1", "view_count": 50000, "like_count": 2000, "comment_count": 100, "published_at": "2026-06-01", "outlier_score": 2.5},
        ]

        from src.nodes.compact_branch import _build_prompt
        prompt = _build_prompt(node, channels, videos)

        assert "10000" in prompt
        assert "50000" in prompt
        assert "2.5" in prompt

    def test_cluster_candidates_section_in_prompt(self):
        node = {"id": "n", "label": "N", "depth": 2, "keywords": [], "schema_version": 6}
        channels = [
            {"channel_id": "ch1", "title": "retro handheld", "subscriber_count": 5000,
             "first_seen_at": "2026-01-01", "discovery_method": "keyword"},
            {"channel_id": "ch2", "title": "retro handheld", "subscriber_count": 4000,
             "first_seen_at": "2026-01-01", "discovery_method": "keyword"},
        ]
        videos = []
        candidates = [
            {
                "member_refs": ["https://www.youtube.com/channel/ch1", "https://www.youtube.com/channel/ch2"],
                "member_channel_ids": ["ch1", "ch2"],
                "size": 2,
                "distinctness_score": 2.4,
            }
        ]
        from src.nodes.compact_branch import _build_prompt
        prompt = _build_prompt(node, channels, videos, cluster_candidates=candidates)
        assert "graph-detected channel clusters" in prompt
        assert "distinctness=2.4" in prompt
        assert "ch1" in prompt

    def test_leaf_prompt_forbids_split(self):
        node = {"id": "n", "label": "N", "depth": 2, "keywords": [], "schema_version": 6}
        channels = [{"channel_id": "ch1", "title": "C1", "subscriber_count": 1,
                     "first_seen_at": "2026-01-01", "discovery_method": "keyword"}]
        from src.nodes.compact_branch import _build_prompt
        prompt = _build_prompt(node, channels, [], cluster_candidates=None)
        assert "graph-detected channel clusters" not in prompt
        assert "Do NOT propose any new nodes" in prompt

    @pytest.mark.asyncio
    async def test_split_method_graph_cluster_when_confirmed(self, state_with_tree, mock_channels, mock_videos):
        state_with_tree["tree"]["copilot_ecosystem"]["cluster_candidates"] = [
            {
                "member_refs": ["r1", "r2"],
                "member_channel_ids": ["ch_github", "ch_msdev"],
                "size": 2,
                "distinctness_score": 2.4,
            }
        ]
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_complete.return_value = _fake_llm_response(COMPACTION_VALID_JSON)
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            result = await compact_branch(state_with_tree)

        updated = result["tree"]["copilot_ecosystem"]
        assert updated["split_method"] == "graph_cluster"

    @pytest.mark.asyncio
    async def test_split_method_llm_seed_when_no_candidates(self, state_with_tree, mock_channels, mock_videos):
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_complete.return_value = _fake_llm_response(COMPACTION_VALID_JSON)
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            result = await compact_branch(state_with_tree)

        updated = result["tree"]["copilot_ecosystem"]
        assert updated["split_method"] == "llm_seed"


# ---------------------------------------------------------------------------
# select_next_node
# ---------------------------------------------------------------------------

TWO_PENDING_TREE = {
    "root": {
        "id": "root", "label": "Root", "depth": 0, "parent_id": None,
        "children_ids": ["branch_a", "branch_b"], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "saturated", "compaction_summary": None,
        "proposed_new_nodes": [], "schema_version": 1,
    },
    "branch_a": {
        "id": "branch_a", "label": "Branch A", "depth": 1, "parent_id": "root",
        "children_ids": [], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "pending", "compaction_summary": None,
        "proposed_new_nodes": [], "schema_version": 1,
    },
    "branch_b": {
        "id": "branch_b", "label": "Branch B", "depth": 1, "parent_id": "root",
        "children_ids": [], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "pending", "compaction_summary": None,
        "proposed_new_nodes": [], "schema_version": 1,
    },
}

DEEPER_PENDING_TREE = {
    "root": {
        "id": "root", "label": "Root", "depth": 0, "parent_id": None,
        "children_ids": ["a", "b"], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "saturated", "compaction_summary": None,
        "proposed_new_nodes": [], "schema_version": 1,
    },
    "a": {
        "id": "a", "label": "A", "depth": 1, "parent_id": "root",
        "children_ids": ["a1"], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "saturated", "compaction_summary": None,
        "proposed_new_nodes": [], "schema_version": 1,
    },
    "a1": {
        "id": "a1", "label": "A1", "depth": 2, "parent_id": "a",
        "children_ids": [], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "pending", "compaction_summary": None,
        "proposed_new_nodes": [], "schema_version": 1,
    },
    "b": {
        "id": "b", "label": "B", "depth": 1, "parent_id": "root",
        "children_ids": [], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "pending", "compaction_summary": None,
        "proposed_new_nodes": [], "schema_version": 1,
    },
}

TREE_WITH_PROPOSED = {
    "root": {
        "id": "root", "label": "Root", "depth": 0, "parent_id": None,
        "children_ids": ["branch_a"], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "saturated", "compaction_summary": None,
        "proposed_new_nodes": [], "schema_version": 1,
    },
    "branch_a": {
        "id": "branch_a", "label": "Branch A", "depth": 1, "parent_id": "root",
        "children_ids": [], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "compacted", "compaction_summary": "done",
        "proposed_new_nodes": [
            {"label": "New Sub Niche", "rationale": "off-taxonomy cluster", "seed_channel_ids": ["ch_new1", "ch_new2"], "parent_id": "branch_a"},
        ],
        "schema_version": 1,
    },
    "branch_b": {
        "id": "branch_b", "label": "Branch B", "depth": 1, "parent_id": "root",
        "children_ids": [], "keywords": [], "seed_channel_ids": [],
        "unexpanded_channel_ids": [], "status": "pending", "compaction_summary": None,
        "proposed_new_nodes": [], "schema_version": 1,
    },
}


class TestSelectNextNode:
    def test_picks_pending_node_in_bfs_order(self):
        result = select_next_node({"tree": TWO_PENDING_TREE})
        assert result["active_node_id"] == "branch_a"

    def test_bfs_respects_depth_ascending(self):
        result = select_next_node({"tree": DEEPER_PENDING_TREE})
        assert result["active_node_id"] == "b"

    def test_activates_chosen_node(self):
        result = select_next_node({"tree": TWO_PENDING_TREE})
        updated = result["tree"]["branch_a"]
        assert updated["status"] == "active"

    def test_proposed_nodes_prioritized_over_pending(self):
        result = select_next_node({"tree": TREE_WITH_PROPOSED})
        assert "new_sub_niche" in result["tree"]
        assert result["active_node_id"] == "new_sub_niche"
        new_node = result["tree"]["new_sub_niche"]
        assert new_node["depth"] == 2
        assert new_node["parent_id"] == "branch_a"
        assert new_node["status"] == "active"

    def test_proposed_node_added_to_parent_children(self):
        result = select_next_node({"tree": TREE_WITH_PROPOSED})
        parent = result["tree"]["branch_a"]
        assert "new_sub_niche" in parent["children_ids"]

    def test_all_done_when_empty(self):
        result = select_next_node({"tree": {}})
        assert result.get("next_action") == "all_done"

    def test_all_done_when_all_saturated(self):
        tree = {
            "root": {
                "id": "root", "label": "R", "depth": 0, "parent_id": None,
                "children_ids": [], "keywords": [], "seed_channel_ids": [],
                "unexpanded_channel_ids": [], "status": "saturated",
                "proposed_new_nodes": [], "schema_version": 1,
            },
            "a": {
                "id": "a", "label": "A", "depth": 1, "parent_id": "root",
                "children_ids": [], "keywords": [], "seed_channel_ids": [],
                "unexpanded_channel_ids": [], "status": "compacted",
                "proposed_new_nodes": [], "schema_version": 1,
            },
        }
        result = select_next_node({"tree": tree})
        assert result.get("next_action") == "all_done"

    def test_no_llm_called(self):
        result = select_next_node({"tree": TWO_PENDING_TREE})
        assert result is not None

    def test_priority_sorts_rich_cluster_node_first(self):
        # A depth-2 node created from a distinctly-split cluster must be
        # explored before a shallow depth-1 sibling whose lineage showed no
        # structure — under a real budget, the rich branch deserves the depth
        # before the run spends budget completing mediocre siblings.
        tree = {
            "root": {
                "id": "root", "label": "R", "depth": 0, "parent_id": None,
                "children_ids": ["rich", "shallow"], "keywords": [],
                "seed_channel_ids": [], "unexpanded_channel_ids": [],
                "status": "saturated", "compaction_summary": None,
                "proposed_new_nodes": [], "schema_version": 6,
            },
            "rich": {
                "id": "rich", "label": "Rich", "depth": 1, "parent_id": "root",
                "children_ids": ["rich_child"], "keywords": [], "seed_channel_ids": [],
                "unexpanded_channel_ids": [], "status": "saturated",
                "compaction_summary": None, "proposed_new_nodes": [],
                "schema_version": 6, "cluster_distinctness_score": 3.5,
            },
            "rich_child": {
                "id": "rich_child", "label": "RichChild", "depth": 2, "parent_id": "rich",
                "children_ids": [], "keywords": [], "seed_channel_ids": [],
                "unexpanded_channel_ids": [], "status": "pending",
                "compaction_summary": None, "proposed_new_nodes": [],
                "schema_version": 6, "cluster_distinctness_score": 3.5,
            },
            "shallow": {
                "id": "shallow", "label": "Shallow", "depth": 1, "parent_id": "root",
                "children_ids": [], "keywords": [], "seed_channel_ids": [],
                "unexpanded_channel_ids": [], "status": "pending",
                "compaction_summary": None, "proposed_new_nodes": [],
                "schema_version": 6,
            },
        }
        result = select_next_node({"tree": tree})
        assert result["active_node_id"] == "rich_child"


# ---------------------------------------------------------------------------
# synthesize + grade_findings — THE CRITICAL TEST
# ---------------------------------------------------------------------------

SYNTHESIS_VALID_JSON = json.dumps({
    "summary": "AI coding tools are a rapidly growing niche with strong engagement.",
    "findings": [
        {
            "claim": "Copilot extension tutorial videos consistently outperform other content on the same channels, with 3-4x outlier scores.",
            "supporting_channel_ids": ["ch_github", "ch_msdev", "ch_cursor"],
            "pattern_type": "content_format",
            "evidence": {"outlier_scores": [3.2, 4.1, 2.8]},
        },
        {
            "claim": "Open-source AI coding tool channels show higher subscriber growth velocity despite lower absolute views.",
            "supporting_channel_ids": ["ch_oss_solo"],
            "pattern_type": "growth",
            "evidence": {"outlier_scores": [1.1]},
        },
        {
            "claim": "Fireship-style rapid-fire format correlates with high engagement in AI tooling content.",
            "supporting_channel_ids": ["ch_fireship"],
            "pattern_type": "content_format",
            "evidence": {"outlier_scores": [1.8]},
        },
    ],
    "cannot_determine": ["Whether high outlier scores are from content quality or algorithmic promotion"],
    "discovery_stats": {"total_channels": 15, "total_videos": 120, "branches_explored": 4, "patterns_found": 6},
})

SYNTHESIS_WEAK_ONLY = json.dumps({
    "summary": "The niche shows some activity but no strong patterns.",
    "findings": [
        {
            "claim": "This channel shows outlier performance — strong finding.",
            "supporting_channel_ids": ["ch_single"],
            "pattern_type": "growth",
            "evidence": {},
        },
        {
            "claim": "Another weak finding due to few channels — this should be moderate at best.",
            "supporting_channel_ids": ["ch_a", "ch_b"],
            "pattern_type": "engagement",
            "evidence": {},
        },
    ],
    "cannot_determine": [],
    "discovery_stats": {"total_channels": 5, "total_videos": 20, "branches_explored": 2, "patterns_found": 2},
})

SYNTHESIS_CAUSAL = json.dumps({
    "summary": "Patterns found.",
    "findings": [
        {
            "claim": "Because of the algorithm shift in June 2026, channels publishing daily tutorials saw 4x higher views. This caused the engagement boost.",
            "supporting_channel_ids": ["ch_algo1", "ch_algo2", "ch_algo3"],
            "pattern_type": "engagement",
            "evidence": {},
        },
    ],
    "cannot_determine": ["Algorithm shift impact not measurable"],
    "discovery_stats": {"total_channels": 10, "total_videos": 50, "branches_explored": 3, "patterns_found": 3},
})


class TestGradeFindings:
    def _store_with_videos(self, videos):
        return {"videos": videos, "channels": []}

    def test_strong_corroboration_with_strong_axes_becomes_strong(self):
        finding = {
            "claim": "Outlier pattern across multiple channels",
            "supporting_channel_ids": ["ch1", "ch2", "ch3"],
            "pattern_type": "content_format",
            "evidence": {},
        }
        recent = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        videos = [
            {"video_id": "v1", "channel_id": "ch1", "view_count": 100000, "published_at": recent, "outlier_score": 3.5},
            {"video_id": "v2", "channel_id": "ch2", "view_count": 80000, "published_at": recent, "outlier_score": 3.2},
            {"video_id": "v3", "channel_id": "ch3", "view_count": 120000, "published_at": recent, "outlier_score": 4.0},
        ]

        graded = grade_findings([finding], self._store_with_videos(videos))
        assert graded[0]["grade"] == "strong"

    def test_single_channel_never_strong(self):
        finding = {
            "claim": "This should not be strong despite LLM confidence",
            "supporting_channel_ids": ["ch_single"],
            "pattern_type": "content_format",
            "evidence": {},
        }
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        videos = [
            {"video_id": "v1", "channel_id": "ch_single", "view_count": 500000, "published_at": recent, "outlier_score": 10.0},
        ]

        graded = grade_findings([finding], self._store_with_videos(videos))
        assert graded[0]["grade"] != "strong"

    def test_deterministic_same_input_same_grades(self):
        finding = {
            "claim": "Consistent pattern",
            "supporting_channel_ids": ["ch_a", "ch_b", "ch_c"],
            "pattern_type": "growth",
            "evidence": {},
        }
        recent = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        videos = [
            {"video_id": "v1", "channel_id": "ch_a", "view_count": 90000, "published_at": recent, "outlier_score": 3.0},
            {"video_id": "v2", "channel_id": "ch_b", "view_count": 95000, "published_at": recent, "outlier_score": 3.1},
            {"video_id": "v3", "channel_id": "ch_c", "view_count": 88000, "published_at": recent, "outlier_score": 2.9},
        ]
        store = self._store_with_videos(videos)

        r1 = grade_findings([finding], store)
        r2 = grade_findings([finding], store)
        r3 = grade_findings([finding], store)

        assert r1 == r2 == r3

    def test_strong_claim_traces_to_3_plus_channels(self):
        finding = {
            "claim": "Strong claim with corroborating evidence",
            "supporting_channel_ids": ["ch_x", "ch_y", "ch_z", "ch_w"],
            "pattern_type": "engagement",
            "evidence": {},
        }
        recent = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        videos = [
            {"video_id": "v1", "channel_id": "ch_x", "view_count": 100000, "published_at": recent, "outlier_score": 3.5},
            {"video_id": "v2", "channel_id": "ch_y", "view_count": 110000, "published_at": recent, "outlier_score": 3.8},
            {"video_id": "v3", "channel_id": "ch_z", "view_count": 105000, "published_at": recent, "outlier_score": 4.0},
            {"video_id": "v4", "channel_id": "ch_w", "view_count": 95000, "published_at": recent, "outlier_score": 3.2},
        ]

        graded = grade_findings([finding], self._store_with_videos(videos))

        for g in graded:
            if g["grade"] == "strong":
                assert len(g["supporting_channel_ids"]) >= 3
                assert len(g["evidence"].get("supporting_channel_ids", [])) >= 3

    def test_corroboration_moderate_no_weak_others_becomes_moderate(self):
        finding = {
            "claim": "Two-channel pattern",
            "supporting_channel_ids": ["ch_p", "ch_q"],
            "pattern_type": "upload_cadence",
            "evidence": {},
        }
        recent = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        videos = [
            {"video_id": "v1", "channel_id": "ch_p", "view_count": 50000, "published_at": recent, "outlier_score": 3.5},
            {"video_id": "v2", "channel_id": "ch_q", "view_count": 55000, "published_at": recent, "outlier_score": 3.3},
        ]

        graded = grade_findings([finding], self._store_with_videos(videos))
        assert graded[0]["grade"] in ("moderate", "weak")

    def test_old_evidence_all_axes_weak_becomes_weak(self):
        finding = {
            "claim": "Old channels with wildly inconsistent, low scores should all be weak",
            "supporting_channel_ids": ["ch_old1", "ch_old2", "ch_old3"],
            "pattern_type": "growth",
            "evidence": {},
        }
        old_date = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        videos = [
            {"video_id": "v1", "channel_id": "ch_old1", "view_count": 50000, "published_at": old_date, "outlier_score": 0.01},
            {"video_id": "v2", "channel_id": "ch_old2", "view_count": 50000, "published_at": old_date, "outlier_score": 0.01},
            {"video_id": "v3", "channel_id": "ch_old3", "view_count": 50000, "published_at": old_date, "outlier_score": 1.9},
        ]

        graded = grade_findings([finding], self._store_with_videos(videos))
        assert graded[0]["grade"] == "weak"

    def test_effect_size_strong_with_3x(self):
        finding = {
            "claim": "High effect size",
            "supporting_channel_ids": ["ch_h1", "ch_h2", "ch_h3"],
            "pattern_type": "engagement",
            "evidence": {},
        }
        recent = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        videos = [
            {"video_id": "v1", "channel_id": "ch_h1", "view_count": 100000, "published_at": recent, "outlier_score": 5.0},
            {"video_id": "v2", "channel_id": "ch_h2", "view_count": 100000, "published_at": recent, "outlier_score": 4.5},
            {"video_id": "v3", "channel_id": "ch_h3", "view_count": 100000, "published_at": recent, "outlier_score": 3.5},
        ]

        graded = grade_findings([finding], self._store_with_videos(videos))
        evidence = graded[0]["evidence"]
        assert evidence["effect_size"] == "strong"

    def test_low_effect_but_recent_and_consistent_becomes_moderate(self):
        finding = {
            "claim": "Low effect size but recent and consistent",
            "supporting_channel_ids": ["ch_l1", "ch_l2", "ch_l3"],
            "pattern_type": "engagement",
            "evidence": {},
        }
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        videos = [
            {"video_id": "v1", "channel_id": "ch_l1", "view_count": 100000, "published_at": recent, "outlier_score": 1.5},
            {"video_id": "v2", "channel_id": "ch_l2", "view_count": 100000, "published_at": recent, "outlier_score": 1.2},
            {"video_id": "v3", "channel_id": "ch_l3", "view_count": 100000, "published_at": recent, "outlier_score": 1.8},
        ]

        graded = grade_findings([finding], self._store_with_videos(videos))
        assert graded[0]["grade"] == "moderate"


class TestSynthesize:

    @pytest.mark.asyncio
    async def test_cannot_determine_populated_on_causal_claims(self, base_state):
        base_state["branch_compactions"] = [
            {"node_id": "n1", "node_label": "N1", "narrative_summary": "summary 1", "key_patterns": []},
        ]
        with patch("src.nodes.synthesize.complete_tier") as mock_complete, \
             patch("src.export.fetch_run_videos") as mock_vids, \
             patch("src.export.fetch_run_channels") as mock_ch:
            mock_complete.return_value = _fake_llm_response(SYNTHESIS_CAUSAL)
            recent = datetime.now(timezone.utc) - timedelta(days=30)
            mock_vids.return_value = [
                {"video_id": "v1", "channel_id": "ch_algo1", "view_count": 100000, "published_at": recent.isoformat(), "outlier_score": 4.0},
                {"video_id": "v2", "channel_id": "ch_algo2", "view_count": 90000, "published_at": recent.isoformat(), "outlier_score": 3.5},
                {"video_id": "v3", "channel_id": "ch_algo3", "view_count": 110000, "published_at": recent.isoformat(), "outlier_score": 3.8},
            ]
            mock_ch.return_value = []
            result = await synthesize(base_state)

        report = result.get("final_report", {})
        cannot_deter = report.get("cannot_determine", [])
        causal_found = any("causal" in item.lower() or "L4" in item for item in cannot_deter)
        assert causal_found, f"Causal claim should add cannot_determine entry, got: {cannot_deter}"

    @pytest.mark.asyncio
    async def test_records_node_log(self, base_state):
        base_state["branch_compactions"] = [
            {"node_id": "n1", "node_label": "N1", "narrative_summary": "summary", "key_patterns": []},
        ]
        with patch("src.nodes.synthesize.complete_tier") as mock_complete, \
             patch("src.export.fetch_run_videos") as mock_vids, \
             patch("src.export.fetch_run_channels") as mock_ch:
            mock_complete.return_value = _fake_llm_response(SYNTHESIS_VALID_JSON)
            recent = datetime.now(timezone.utc) - timedelta(days=30)
            mock_vids.return_value = [
                {"video_id": "v1", "channel_id": "ch_github", "view_count": 100000, "published_at": recent.isoformat(), "outlier_score": 3.2},
                {"video_id": "v2", "channel_id": "ch_msdev", "view_count": 200000, "published_at": recent.isoformat(), "outlier_score": 4.1},
                {"video_id": "v3", "channel_id": "ch_cursor", "view_count": 50000, "published_at": recent.isoformat(), "outlier_score": 2.8},
                {"video_id": "v4", "channel_id": "ch_oss_solo", "view_count": 10000, "published_at": recent.isoformat(), "outlier_score": 1.1},
                {"video_id": "v5", "channel_id": "ch_fireship", "view_count": 30000, "published_at": recent.isoformat(), "outlier_score": 1.8},
            ]
            mock_ch.return_value = []
            result = await synthesize(base_state)

        logs = result.get("node_logs", [])
        assert len(logs) >= 1
        log = logs[0]
        assert log["node_name"] == "synthesize"
        assert log["latency_ms"] is not None
        assert log["cost_usd"] is not None

    @pytest.mark.asyncio
    async def test_final_report_has_all_fields(self, base_state):
        base_state["branch_compactions"] = [
            {"node_id": "n1", "node_label": "N1", "narrative_summary": "summary", "key_patterns": []},
        ]
        with patch("src.nodes.synthesize.complete_tier") as mock_complete, \
             patch("src.export.fetch_run_videos") as mock_vids, \
             patch("src.export.fetch_run_channels") as mock_ch:
            mock_complete.return_value = _fake_llm_response(SYNTHESIS_VALID_JSON)
            recent = datetime.now(timezone.utc) - timedelta(days=30)
            mock_vids.return_value = [
                {"video_id": "v1", "channel_id": "ch_github", "view_count": 100000, "published_at": recent.isoformat(), "outlier_score": 3.2},
                {"video_id": "v2", "channel_id": "ch_msdev", "view_count": 200000, "published_at": recent.isoformat(), "outlier_score": 4.1},
                {"video_id": "v3", "channel_id": "ch_cursor", "view_count": 50000, "published_at": recent.isoformat(), "outlier_score": 2.8},
            ]
            mock_ch.return_value = []
            result = await synthesize(base_state)

        report = result.get("final_report", {})
        assert report["niche"] == "ai coding assistants"
        assert report["run_id"] == "run-001"
        assert report.get("summary")
        assert isinstance(report.get("findings"), list)
        assert "cannot_determine" in report
        assert "discovery_stats" in report

    @pytest.mark.asyncio
    async def test_retry_on_invalid_json(self, base_state):
        calls = [
            _fake_llm_response("not json {{{"),
            _fake_llm_response(SYNTHESIS_VALID_JSON),
        ]
        base_state["branch_compactions"] = []
        with patch("src.nodes.synthesize.complete_tier", side_effect=calls) as mock_complete, \
             patch("src.export.fetch_run_videos") as mock_vids, \
             patch("src.export.fetch_run_channels") as mock_ch:
            mock_vids.return_value = []
            mock_ch.return_value = []
            result = await synthesize(base_state)

        assert mock_complete.call_count == 2
        assert result.get("final_report") is not None

    @pytest.mark.asyncio
    async def test_weak_claim_from_single_channel_remains_weak(self, base_state):
        base_state["branch_compactions"] = []
        with patch("src.nodes.synthesize.complete_tier") as mock_complete, \
             patch("src.export.fetch_run_videos") as mock_vids, \
             patch("src.export.fetch_run_channels") as mock_ch:
            mock_complete.return_value = _fake_llm_response(SYNTHESIS_WEAK_ONLY)
            recent = datetime.now(timezone.utc) - timedelta(days=10)
            mock_vids.return_value = [
                {"video_id": "v_single", "channel_id": "ch_single", "view_count": 100000, "published_at": recent.isoformat(), "outlier_score": 2.5},
                {"video_id": "v_a", "channel_id": "ch_a", "view_count": 50000, "published_at": recent.isoformat(), "outlier_score": 1.2},
                {"video_id": "v_b", "channel_id": "ch_b", "view_count": 60000, "published_at": recent.isoformat(), "outlier_score": 1.5},
            ]
            mock_ch.return_value = []
            result = await synthesize(base_state)

        findings = result.get("final_report", {}).get("findings", [])
        for f in findings:
            if len(f.get("supporting_channel_ids", [])) == 1:
                assert f["grade"] != "strong", f"Single-channel finding got grade={f['grade']}: {f['claim']}"


class TestSynthesizeIsRunScoped:
    """channels and videos are deliberately NOT run-scoped tables (see
    export.py's own docstring) — a channel found in a Finance run and a
    Legal run is one shared row. store.get_all_videos() has no channel_ids
    filter, so it returns every video ever discovered by every run sharing
    this database. Caught live: a Finance report cited "32,485 videos"
    against a run that had tagged under a thousand — synthesize was
    drafting findings from the whole cross-run pool, not this run's slice.
    fetch_run_channels/fetch_run_videos (category_tags-scoped, the same
    functions export.py uses) must be the data source whenever a run_id
    is available."""

    @pytest.mark.asyncio
    async def test_uses_run_scoped_fetchers_when_run_id_present(self, base_state):
        base_state["branch_compactions"] = []
        with patch("src.nodes.synthesize.complete_tier") as mock_complete, \
             patch("src.export.fetch_run_videos") as mock_vids, \
             patch("src.export.fetch_run_channels") as mock_ch, \
             patch("src.nodes.store.StoreAccess.get_all_videos", new_callable=AsyncMock) as mock_global_vids:
            mock_complete.return_value = _fake_llm_response(SYNTHESIS_VALID_JSON)
            mock_vids.return_value = []
            mock_ch.return_value = []
            await synthesize(base_state)

        mock_vids.assert_called_once_with("run-001", 1_000_000)
        mock_ch.assert_called_once_with("run-001")
        mock_global_vids.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_global_store_only_without_a_run_id(self, base_state):
        base_state["branch_compactions"] = []
        base_state["run_id"] = ""
        with patch("src.nodes.synthesize.complete_tier") as mock_complete, \
             patch("src.export.fetch_run_videos") as mock_vids, \
             patch("src.nodes.store.StoreAccess.get_all_videos", new_callable=AsyncMock) as mock_global_vids, \
             patch("src.nodes.store.StoreAccess.get_channels_by_ids", new_callable=AsyncMock) as mock_global_ch:
            mock_complete.return_value = _fake_llm_response(SYNTHESIS_VALID_JSON)
            mock_global_vids.return_value = []
            mock_global_ch.return_value = []
            await synthesize(base_state)

        mock_global_vids.assert_called_once()
        mock_vids.assert_not_called()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestSelectNextNodeEdgeCases:
    def test_empty_tree_all_done(self):
        result = select_next_node({"tree": {}})
        assert result.get("next_action") == "all_done"

    def test_proposed_node_no_label_uses_fallback(self):
        tree = {
            "root": {
                "id": "root", "label": "R", "depth": 0, "parent_id": None,
                "children_ids": [], "keywords": [], "seed_channel_ids": [],
                "unexpanded_channel_ids": [], "status": "compacted",
                "proposed_new_nodes": [
                    {"label": "", "rationale": "no label", "seed_channel_ids": ["ch1"], "parent_id": "root"},
                ],
                "schema_version": 1,
            },
        }
        result = select_next_node({"tree": tree})
        assert result["active_node_id"] is not None

    def test_proposed_node_duplicate_id_not_added_twice(self):
        tree = {
            "root": {
                "id": "root", "label": "R", "depth": 0, "parent_id": None,
                "children_ids": ["dup"], "keywords": [], "seed_channel_ids": [],
                "unexpanded_channel_ids": [], "status": "compacted",
                "proposed_new_nodes": [
                    {"label": "Dup", "rationale": "dup", "seed_channel_ids": ["ch1"], "parent_id": "root"},
                ],
                "schema_version": 1,
            },
            "dup": {
                "id": "dup", "label": "Dup", "depth": 1, "parent_id": "root",
                "children_ids": [], "keywords": [], "seed_channel_ids": [],
                "unexpanded_channel_ids": [], "status": "pending",
                "proposed_new_nodes": [], "schema_version": 1,
            },
        }
        result = select_next_node({"tree": tree})
        assert result["active_node_id"] == "dup"


class TestTaxonomyEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_niche_scanner_evidence(self, base_state):
        base_state["niche_scanner_evidence"] = {}
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_VALID_JSON)
            result = await build_taxonomy(base_state)

        assert "root" in result["tree"]

    @pytest.mark.asyncio
    async def test_missing_niche_name_defaults_to_unknown(self, base_state):
        base_state["selected_niche"] = ""
        with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
            mock_complete.return_value = _fake_llm_response(TAXONOMY_VALID_JSON)
            result = await build_taxonomy(base_state)

        assert result["active_node_id"] == "root"


class TestCompactBranchEdgeCases:
    @pytest.mark.asyncio
    async def test_no_off_taxonomy_clusters_means_no_proposed_nodes(self, state_with_tree, mock_channels, mock_videos):
        no_proposal_json = json.dumps({
            "narrative_summary": "No off-taxonomy clusters found.",
            "key_patterns": [],
            "proposed_nodes": [],
        })
        with patch("src.nodes.compact_branch.complete_tier") as mock_complete, \
             patch("src.nodes.store.StoreAccess.get_channels_for_node", new_callable=AsyncMock) as mock_get_ch, \
             patch("src.nodes.store.StoreAccess.get_videos_for_channels", new_callable=AsyncMock) as mock_get_vid:
            mock_complete.return_value = _fake_llm_response(no_proposal_json)
            mock_get_ch.return_value = mock_channels
            mock_get_vid.return_value = mock_videos
            result = await compact_branch(state_with_tree)

        updated = result["tree"]["copilot_ecosystem"]
        assert updated["proposed_new_nodes"] == []


# ---------------------------------------------------------------------------
# Integration: full grading flow through synthesize
# ---------------------------------------------------------------------------

class TestIntegrationSynthesizeGrading:
    @pytest.mark.asyncio
    async def test_findings_are_graded_after_llm_draft(self, base_state):
        base_state["branch_compactions"] = [
            {"node_id": "n1", "node_label": "N1", "narrative_summary": "summary", "key_patterns": []},
        ]
        with patch("src.nodes.synthesize.complete_tier") as mock_complete, \
             patch("src.export.fetch_run_videos") as mock_vids, \
             patch("src.export.fetch_run_channels") as mock_ch:
            mock_complete.return_value = _fake_llm_response(SYNTHESIS_VALID_JSON)
            recent = datetime.now(timezone.utc) - timedelta(days=30)
            mock_vids.return_value = [
                {"video_id": "v1", "channel_id": "ch_github", "view_count": 150000, "published_at": recent.isoformat(), "outlier_score": 3.2},
                {"video_id": "v2", "channel_id": "ch_msdev", "view_count": 200000, "published_at": recent.isoformat(), "outlier_score": 4.1},
                {"video_id": "v3", "channel_id": "ch_cursor", "view_count": 50000, "published_at": recent.isoformat(), "outlier_score": 2.8},
            ]
            mock_ch.return_value = []
            result = await synthesize(base_state)

        findings = result.get("final_report", {}).get("findings", [])
        assert len(findings) >= 1
        for f in findings:
            assert f["grade"] in ("strong", "moderate", "weak")
            assert "evidence" in f
            assert "corroboration" in f.get("evidence", {})
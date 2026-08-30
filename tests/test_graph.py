"""End-to-end graph integration tests.

Validates the full LangGraph wiring runs from START to END with all LLM and
external API calls mocked at their boundaries. Proves the fan-out/fan-in,
saturation routing, budget circuit breaker, and evidence-graded synthesis all
fire in a complete run.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from src.graph import build_graph, compile_graph, run_pipeline


TAXONOMY_JSON = json.dumps(
    {
        "nodes": [
            {
                "id": "root",
                "label": "test_niche",
                "depth": 0,
                "parent_id": None,
                "keywords": ["test keyword"],
                "seed_channels": ["seed_ch_a"],
            },
            {
                "id": "branch_a",
                "label": "branch a",
                "depth": 1,
                "parent_id": "root",
                "keywords": ["branch a keyword"],
                "seed_channels": ["seed_ch_b"],
            },
            {
                "id": "branch_b",
                "label": "branch b",
                "depth": 1,
                "parent_id": "root",
                "keywords": ["branch b keyword"],
                "seed_channels": ["seed_ch_c"],
            },
        ]
    }
)

COMPACTION_JSON = json.dumps(
    {
        "narrative_summary": "Channels show high engagement on tutorial content.",
        "key_patterns": [
            {
                "pattern_name": "tutorial engagement",
                "description": "tutorials outperform other formats",
                "evidence_summary": "outlier scores above 2x",
            }
        ],
        "proposed_nodes": [],
    }
)

SYNTHESIS_JSON = json.dumps(
    {
        "summary": "The niche is underexplored with several high-engagement channels.",
        "findings": [
            {
                "claim": "Tutorial-style channels show consistent outlier engagement",
                "supporting_channel_ids": ["ch_1", "ch_2", "ch_3"],
                "pattern_type": "engagement",
                "evidence": {},
            }
        ],
        "cannot_determine": ["Whether this correlates with subscriber retention"],
        "discovery_stats": {"total_channels": 3, "total_videos": 30, "branches_explored": 3, "patterns_found": 1},
    }
)


def _mock_llm_tier(complete_tier_mock):
    def side_effect(tier, prompt, system=None):
        if tier == "frontier" and "Build a taxonomy" in prompt:
            return {"content": TAXONOMY_JSON, "usage": {"prompt_tokens": 100, "completion_tokens": 100}, "cost_usd": 0.001}
        if tier == "mid":
            return {"content": COMPACTION_JSON, "usage": {"prompt_tokens": 100, "completion_tokens": 100}, "cost_usd": 0.001}
        return {"content": SYNTHESIS_JSON, "usage": {"prompt_tokens": 100, "completion_tokens": 100}, "cost_usd": 0.001}

    complete_tier_mock.side_effect = side_effect
    return complete_tier_mock


def _patch_llm_nodes():
    """Patch complete_tier at each node module's import site (they hold direct references)."""
    return (
        patch("src.nodes.taxonomy.complete_tier"),
        patch("src.nodes.compact_branch.complete_tier"),
        patch("src.nodes.synthesize.complete_tier"),
    )


def _mock_bright_data(bd_mock):
    """A client that succeeds and returns nothing.

    Both tracks must genuinely *complete* — a client whose methods aren't
    awaitable makes every round raise, which leaves no novelty history and no
    exhaustion flag, so the branch can never saturate and the graph runs to the
    recursion limit. Returning empty results is what drives the run down the
    real stop path: empty frontier and no new queries mark both tracks
    exhausted, and check_saturation ends the branch on `both_tracks_exhausted`.
    """
    client = MagicMock()
    bd_mock.return_value = client
    client.discover_channels_by_keyword = AsyncMock(return_value=([], 0))
    client.get_channels = AsyncMock(return_value=([], 0))
    client.get_channel_videos = AsyncMock(return_value=([], 0))
    client.get_comments = AsyncMock(return_value=([], 0))
    return client


def _mock_youtube(yt_mock):
    client = MagicMock()
    yt_mock.return_value = client
    client.get_channels.return_value = []
    client.get_channel_videos.return_value = []
    client.get_quota_used.return_value = 0
    return client


@pytest.mark.asyncio
async def test_end_to_end_run_completes():
    with (
        patch("src.nodes.taxonomy.complete_tier") as mock_tax,
        patch("src.nodes.compact_branch.complete_tier") as mock_comp,
        patch("src.nodes.synthesize.complete_tier") as mock_syn,
        # v3 added three more LLM-calling nodes to the graph
        # (classify_channel, score_thumbnail_signals,
        # extract_success_failure_factors) but this file's mocks were never
        # extended to cover them. Harmless against an empty test DB — but
        # their eligibility queries carry no run_id scoping at all, so
        # against a shared dev DB that already has real floor-qualifying
        # channels sitting in it (from any live run), an unmocked
        # complete_tier here means the test suite silently starts making
        # real, slow LLM calls and can hang for tens of minutes.
        patch("src.nodes.classify_channel.complete_tier") as mock_classify,
        patch("src.nodes.score_thumbnail_signals.complete_tier") as mock_thumb,
        patch("src.nodes.extract_success_failure_factors.complete_tier") as mock_factors,
        patch("src.nodes.describe_video_titles.complete_tier") as mock_describe,
        # complete_tier being mocked stops the LLM-cost/hang risk, but these
        # six nodes' own eligibility queries are unscoped SELECTs against
        # the whole `channels` table — against a shared dev DB, that means
        # a "harmless" test run reads AND WRITES real production rows.
        # Confirmed live: channel_success_factors/channel_failure_factors
        # for real Crime-run channels accumulated 235 stray rows tagged
        # "run-test"/"run-test-budget" from exactly this test, silently
        # corrupting a real deliverable's eligibility queries. Each of
        # these nodes already has its own `except: return early` around
        # get_connection() — raising here routes them through that
        # existing, designed-in path instead of touching the real DB.
        patch("src.nodes.resolve_geo_language.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.extract_metadata_signals.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.classify_channel.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.score_thumbnail_signals.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.extract_success_failure_factors.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.describe_video_titles.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.resolve_first_video_date.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.tools.graph_walk.BrightDataClient") as mock_bd,
        patch("src.tools.keyword_search.BrightDataClient") as mock_bd_kw,
        patch("src.tools.hydrate_metadata.YouTubeAPIClient") as mock_yt,
        patch("src.nodes.compact_branch.get_store") as mock_store,
        patch("src.nodes.synthesize.get_store") as mock_store_syn,
    ):
        _mock_llm_tier(mock_tax)
        _mock_llm_tier(mock_comp)
        _mock_llm_tier(mock_syn)
        _mock_llm_tier(mock_classify)
        _mock_llm_tier(mock_thumb)
        _mock_llm_tier(mock_factors)
        _mock_llm_tier(mock_describe)
        _mock_bright_data(mock_bd)
        _mock_bright_data(mock_bd_kw)
        _mock_youtube(mock_yt)

        store = MagicMock()
        store.get_all_videos = AsyncMock(return_value=[])
        store.get_channels_by_ids = AsyncMock(return_value=[])
        store.get_channels_for_node = AsyncMock(return_value=[])
        store.get_videos_for_channels = AsyncMock(return_value=[])
        mock_store.return_value = store
        mock_store_syn.return_value = store

        final = await run_pipeline(
            candidate_niches=["test_niche"],
            run_id="run-test",
            thread_id="thread-test",
        )

    assert final.get("selected_niche") == "test_niche"
    # v3 is dataset-first: synthesize()/final_report were retired by
    # ADR-0008 in favor of finalize_dataset writing deterministic rollups
    # straight to the store. A completed run is proven by finalize_dataset
    # actually firing at the end of the graph, not by a report payload.
    node_names = {log.get("node_name") for log in final.get("node_logs", [])}
    assert "finalize_dataset" in node_names


@pytest.mark.asyncio
async def test_graph_terminates_with_budget_breaker():
    with (
        patch("src.nodes.taxonomy.complete_tier") as mock_tax,
        patch("src.nodes.compact_branch.complete_tier") as mock_comp,
        patch("src.nodes.synthesize.complete_tier") as mock_syn,
        # v3 added three more LLM-calling nodes to the graph
        # (classify_channel, score_thumbnail_signals,
        # extract_success_failure_factors) but this file's mocks were never
        # extended to cover them. Harmless against an empty test DB — but
        # their eligibility queries carry no run_id scoping at all, so
        # against a shared dev DB that already has real floor-qualifying
        # channels sitting in it (from any live run), an unmocked
        # complete_tier here means the test suite silently starts making
        # real, slow LLM calls and can hang for tens of minutes.
        patch("src.nodes.classify_channel.complete_tier") as mock_classify,
        patch("src.nodes.score_thumbnail_signals.complete_tier") as mock_thumb,
        patch("src.nodes.extract_success_failure_factors.complete_tier") as mock_factors,
        patch("src.nodes.describe_video_titles.complete_tier") as mock_describe,
        # complete_tier being mocked stops the LLM-cost/hang risk, but these
        # six nodes' own eligibility queries are unscoped SELECTs against
        # the whole `channels` table — against a shared dev DB, that means
        # a "harmless" test run reads AND WRITES real production rows.
        # Confirmed live: channel_success_factors/channel_failure_factors
        # for real Crime-run channels accumulated 235 stray rows tagged
        # "run-test"/"run-test-budget" from exactly this test, silently
        # corrupting a real deliverable's eligibility queries. Each of
        # these nodes already has its own `except: return early` around
        # get_connection() — raising here routes them through that
        # existing, designed-in path instead of touching the real DB.
        patch("src.nodes.resolve_geo_language.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.extract_metadata_signals.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.classify_channel.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.score_thumbnail_signals.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.extract_success_failure_factors.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.describe_video_titles.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.nodes.resolve_first_video_date.get_connection", side_effect=Exception("test isolation: no real DB")),
        patch("src.tools.graph_walk.BrightDataClient") as mock_bd,
        patch("src.tools.keyword_search.BrightDataClient") as mock_bd_kw,
        patch("src.tools.hydrate_metadata.YouTubeAPIClient") as mock_yt,
        patch("src.nodes.compact_branch.get_store") as mock_store,
        patch("src.nodes.synthesize.get_store") as mock_store_syn,
    ):
        _mock_llm_tier(mock_tax)
        _mock_llm_tier(mock_comp)
        _mock_llm_tier(mock_syn)
        _mock_llm_tier(mock_classify)
        _mock_llm_tier(mock_thumb)
        _mock_llm_tier(mock_factors)
        _mock_llm_tier(mock_describe)
        _mock_bright_data(mock_bd)
        _mock_bright_data(mock_bd_kw)
        _mock_youtube(mock_yt)

        store = MagicMock()
        store.get_all_videos = AsyncMock(return_value=[])
        store.get_channels_by_ids = AsyncMock(return_value=[])
        store.get_channels_for_node = AsyncMock(return_value=[])
        store.get_videos_for_channels = AsyncMock(return_value=[])
        mock_store.return_value = store
        mock_store_syn.return_value = store

        final = await run_pipeline(
            candidate_niches=["test_niche"],
            run_id="run-test-budget",
            thread_id="thread-test-budget",
            state_overrides={"budget_spent_usd": 999999.0},
        )

    # The budget breaker trips downstream of check_saturation, but every
    # route out of it (route_after_select/route_after_compaction) still
    # funnels to finalize_dataset before END — the circuit breaker ends
    # the run cleanly, it doesn't just abandon it.
    node_names = {log.get("node_name") for log in final.get("node_logs", [])}
    assert "finalize_dataset" in node_names


@pytest.mark.asyncio
async def test_no_niches_terminates_gracefully():
    final = await run_pipeline(
        candidate_niches=[],
        run_id="run-empty",
        thread_id="thread-empty",
    )
    assert final.get("selected_niche") == ""
    # route_after_scan sends an empty candidate set straight to END —
    # finalize_dataset is never reached, unlike the budget-breaker case.
    node_names = {log.get("node_name") for log in final.get("node_logs", [])}
    assert "finalize_dataset" not in node_names


def test_graph_compiles_and_has_expected_nodes():
    graph = build_graph()
    nodes = graph.nodes
    expected = {
        "scan_niches",
        "expand_niche_adjacency",
        "build_taxonomy",
        "select_next_node",
        "keyword_search",
        "graph_walk",
        "breakout_scanner",
        "underperformer_discovery",
        "new_channel_discovery",
        "hydrate_metadata",
        "resolve_geo_language",
        "extract_metadata_signals",
        "score_signals",
        "score_thumbnail_signals",
        "classify_channel",
        "check_saturation",
        "cluster_branch",
        "compact_branch",
        "extract_success_failure_factors",
        "describe_video_titles",
        "populate_taxonomy_dimensions",
        "populate_crime_metadata",
        "populate_shared_fields",
        "assign_cohorts",
        "finalize_dataset",
    }
    assert expected <= set(nodes)
    assert "analyze_deep" not in nodes
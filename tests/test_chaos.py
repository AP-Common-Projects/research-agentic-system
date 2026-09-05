"""Chaos / failure-injection tests.

Simulates production failure modes and asserts GRACEFUL DEGRADATION — the run
must record the error and continue, never silently corrupt results, and never
crash. Follows the same mocking patterns as tests/test_graph.py:
- complete_tier patched at each node module's import site
- BrightDataClient / YouTubeAPIClient patched at their tool import sites
- get_store patched at the node import sites
- `with patch(...)` context managers for async tests (pytest-asyncio STRICT mode)
"""

from __future__ import annotations

import contextlib
import importlib
import json

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tenacity import RetryError

from src.graph import build_graph, run_pipeline, _guarded
from src.nodes.taxonomy import build_taxonomy
from src.tools.hydrate_metadata import hydrate_metadata
from src.config import get_config
from src.tools.saturation import check_saturation


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
        "narrative_summary": "Channels show high engagement.",
        "key_patterns": [],
        "proposed_nodes": [],
    }
)

SYNTHESIS_JSON = json.dumps(
    {
        "summary": "Niche summary.",
        "findings": [
            {
                "claim": "Pattern found.",
                "supporting_channel_ids": ["ch_1"],
                "pattern_type": "engagement",
                "evidence": {},
            }
        ],
        "cannot_determine": [],
        "discovery_stats": {"total_channels": 1, "total_videos": 1, "branches_explored": 1, "patterns_found": 1},
    }
)


def _mock_llm_tier(mock):
    def side_effect(tier, prompt, system=None):
        if tier == "frontier" and "Build a taxonomy" in prompt:
            return {"content": TAXONOMY_JSON, "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "cost_usd": 0.0}
        if tier == "mid":
            return {"content": COMPACTION_JSON, "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "cost_usd": 0.0}
        return {"content": SYNTHESIS_JSON, "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "cost_usd": 0.0}

    mock.side_effect = side_effect
    return mock


def _mock_store(mock):
    store = MagicMock()
    store.get_all_videos = AsyncMock(return_value=[])
    store.get_channels_by_ids = AsyncMock(return_value=[])
    store.get_channels_for_node = AsyncMock(return_value=[])
    store.get_videos_for_channels = AsyncMock(return_value=[])
    mock.return_value = store
    return store


def _mock_youtube(mock):
    client = MagicMock()
    client.get_channels.return_value = []
    client.get_channel_videos.return_value = []
    client.get_quota_used.return_value = 0
    mock.return_value = client
    return client


def _patch_graph_walk_success(mock):
    """Tier A returns one channel whose record resolves to found_ch_1."""
    client = MagicMock()
    client.get_channels = AsyncMock(
        return_value=(
            [{
                "channel_id": "found_ch_1",
                "channel_ref": "https://www.youtube.com/@seed_a",
                "featured_channel_edges": [],
            }],
            1,
        )
    )
    client.get_channel_videos = AsyncMock(return_value=([], 0))
    client.get_comments = AsyncMock(return_value=([], 0))
    mock.return_value = client
    return client


def _patch_keyword_success(mock):
    client = MagicMock()
    client.discover_channels_by_keyword = AsyncMock(
        return_value=(
            [{
                "channel_id": "kw_ch_1",
                "channel_ref": "https://www.youtube.com/@kw1",
                "title": "kw result",
                "subscriber_count": 1234,
            }],
            1,
        )
    )
    mock.return_value = client
    return client


#: The write-up chain: extract_success_failure_factors through
#: assign_cohorts. A governor stop routes THROUGH these now rather than
#: jumping to finalize_dataset -- skipping them is what left the Niches,
#: Success Factors and Failure Factors sheets empty -- so any pipeline test
#: reaches them, and unmocked they make real LLM calls and hang the suite.
#:
#: Entered as ONE context manager rather than a dozen `with` entries:
#: Python allows only 20 statically nested blocks, and these tests were
#: already close to the limit.
_WRITEUP_NODES = (
    "extract_success_failure_factors",
    "describe_video_titles",
    "populate_taxonomy_dimensions",
    "populate_crime_metadata",
    "populate_shared_fields",
    "assign_cohorts",
)


@contextlib.contextmanager
def _writeup_chain_stubbed():
    with contextlib.ExitStack() as stack:
        for node in _WRITEUP_NODES:
            module = importlib.import_module(f"src.nodes.{node}")
            if hasattr(module, "complete_tier"):
                stack.enter_context(patch(f"src.nodes.{node}.complete_tier"))
            if hasattr(module, "get_connection"):
                stack.enter_context(patch(
                    f"src.nodes.{node}.get_connection",
                    side_effect=Exception("test isolation: no real DB"),
                ))
        yield


def _patch_keyword_fail(mock):
    client = MagicMock()
    client.discover_channels_by_keyword = AsyncMock(
        side_effect=httpx.TimeoutException("keyword search timeout")
    )
    mock.return_value = client
    return client


def _patch_graph_walk_fail(mock):
    client = MagicMock()
    client.get_channels = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    client.get_channel_videos = AsyncMock(return_value=([], 0))
    client.get_comments = AsyncMock(return_value=([], 0))
    mock.return_value = client
    return client


@pytest.mark.asyncio
async def test_keyword_search_branch_fails_graph_walk_succeeds():
    with (
        patch("src.nodes.taxonomy.complete_tier") as mock_tax,
        patch("src.nodes.compact_branch.complete_tier") as mock_comp,
        patch("src.nodes.synthesize.complete_tier") as mock_syn,
        _writeup_chain_stubbed(),
        patch("src.tools.keyword_search.BrightDataClient") as mock_bd_kw,
        patch("src.tools.graph_walk.BrightDataClient") as mock_bd_gw,
        patch("src.tools.hydrate_metadata.YouTubeAPIClient") as mock_yt,
        patch("src.nodes.compact_branch.get_store") as mock_store,
        patch("src.nodes.synthesize.get_store") as mock_store_syn,
        patch("src.db.connection.get_connection") as mock_get_conn,
        patch("src.db.connection.put_connection") as mock_put_conn,
    ):
        _mock_llm_tier(mock_tax)
        _mock_llm_tier(mock_comp)
        _mock_llm_tier(mock_syn)
        _patch_keyword_fail(mock_bd_kw)
        _patch_graph_walk_success(mock_bd_gw)
        _mock_youtube(mock_yt)
        _mock_store(mock_store)
        _mock_store(mock_store_syn)
        mock_get_conn.return_value = MagicMock()

        final = await run_pipeline(
            candidate_niches=["test_niche"],
            run_id="run-kw-fail",
            thread_id="thread-kw-fail",
            state_overrides={"budget_spent_usd": 999999.0},
        )

    assert final is not None, "run must complete without crashing"
    assert "found_ch_1" in final.get("discovered_channel_ids", []), "graph_walk discoveries must survive"
    assert any(e.get("node_name") == "keyword_search" for e in final.get("errors", [])), (
        "keyword_search failure must be recorded in state['errors']"
    )


@pytest.mark.asyncio
async def test_graph_walk_branch_fails_keyword_succeeds():
    with (
        patch("src.nodes.taxonomy.complete_tier") as mock_tax,
        patch("src.nodes.compact_branch.complete_tier") as mock_comp,
        patch("src.nodes.synthesize.complete_tier") as mock_syn,
        _writeup_chain_stubbed(),
        patch("src.tools.keyword_search.BrightDataClient") as mock_bd_kw,
        patch("src.tools.graph_walk.BrightDataClient") as mock_bd_gw,
        patch("src.tools.hydrate_metadata.YouTubeAPIClient") as mock_yt,
        patch("src.nodes.compact_branch.get_store") as mock_store,
        patch("src.nodes.synthesize.get_store") as mock_store_syn,
        patch("src.db.connection.get_connection") as mock_get_conn,
        patch("src.db.connection.put_connection") as mock_put_conn,
    ):
        _mock_llm_tier(mock_tax)
        _mock_llm_tier(mock_comp)
        _mock_llm_tier(mock_syn)
        _patch_keyword_success(mock_bd_kw)
        _patch_graph_walk_fail(mock_bd_gw)
        _mock_youtube(mock_yt)
        _mock_store(mock_store)
        _mock_store(mock_store_syn)
        mock_get_conn.return_value = MagicMock()

        final = await run_pipeline(
            candidate_niches=["test_niche"],
            run_id="run-gw-fail",
            thread_id="thread-gw-fail",
            state_overrides={"budget_spent_usd": 999999.0},
        )

    assert final is not None, "run must complete without crashing"
    assert "kw_ch_1" in final.get("discovered_channel_ids", []), "keyword_search discoveries must survive"
    assert any(e.get("node_name") == "graph_walk" for e in final.get("errors", [])), (
        "graph_walk failure must be recorded in state['errors']"
    )


@pytest.mark.asyncio
async def test_llm_taxonomy_returns_invalid_json_then_recovers():
    state = {"selected_niche": "test", "niche_scanner_evidence": {}, "thread_id": "t"}
    calls = [
        {"content": "garbage not json {{{", "usage": {}, "cost_usd": 0.0},
        {"content": TAXONOMY_JSON, "usage": {}, "cost_usd": 0.0},
    ]
    with patch("src.nodes.taxonomy.complete_tier", side_effect=calls) as mock_complete:
        result = await build_taxonomy(state)

    assert mock_complete.call_count == 2, "must retry once after invalid JSON"
    assert "root" in result.get("tree", {}), "tree must be built on retry"
    assert result.get("active_node_id") == "root"


@pytest.mark.asyncio
async def test_llm_taxonomy_returns_invalid_twice():
    state = {"selected_niche": "test", "niche_scanner_evidence": {}, "thread_id": "t"}
    with patch("src.nodes.taxonomy.complete_tier") as mock_complete:
        mock_complete.return_value = {"content": "garbage not json {{{", "usage": {}, "cost_usd": 0.0}
        result = await build_taxonomy(state)

    assert mock_complete.call_count == 2
    assert len(result.get("errors", [])) > 0, "error must be recorded"
    assert result.get("tree", {}) == {}, "tree must remain empty"
    assert result.get("active_node_id") is None


def test_hydrate_metadata_db_unavailable():
    state = {"discovered_channel_ids": ["ch1"], "thread_id": "t", "run_id": "r"}
    with (
        patch("src.tools.hydrate_metadata.YouTubeAPIClient") as mock_yt,
        patch("src.db.connection.get_connection") as mock_get_conn,
    ):
        client = MagicMock()
        client.get_channels.return_value = [
            {"channel_id": "ch1", "title": "C1", "subscriber_count": 100}
        ]
        client.get_channel_videos.return_value = []
        client.get_quota_used.return_value = 0
        mock_yt.return_value = client

        mock_get_conn.side_effect = ConnectionError("postgres unavailable")

        result = hydrate_metadata(state)

    # Graceful: no exception propagates, node still emits its log.
    assert result is not None
    assert result.get("next_action") == "continue"
    assert any(l.get("node_name") == "hydrate_metadata" for l in result.get("node_logs", []))


def test_hydrate_metadata_uses_the_channels_real_creation_date():
    """_parse_channel already fetches YouTube's real snippet.publishedAt
    (the channel's actual creation date) into ch["published_at"] — a prior
    version read a "first_seen_at" key that dict never had, so it always
    fell through to "now" and silently discarded the real value."""
    state = {"discovered_channel_ids": ["ch1"], "thread_id": "t", "run_id": "r"}
    conn = MagicMock()
    conn.cursor.return_value = MagicMock()

    with (
        patch("src.tools.hydrate_metadata.YouTubeAPIClient") as mock_yt,
        patch("src.db.connection.get_connection", return_value=conn),
        patch("src.db.connection.put_connection"),
        patch("src.tools.hydrate_metadata.persist_channel") as mock_persist_channel,
        patch("src.tools.hydrate_metadata.persist_video"),
        patch("src.tools.hydrate_metadata.persist_channel_v3"),
        patch("src.tools.hydrate_metadata.persist_channel_snapshot"),
        patch("src.tools.hydrate_metadata.persist_video_v3"),
        patch("src.tools.dedup.persist_category_tags"),
    ):
        client = MagicMock()
        client.get_channels.return_value = [{
            "channel_id": "ch1", "title": "C1", "subscriber_count": 100,
            "published_at": "2019-03-14T00:00:00Z",  # real YouTube channel creation date
        }]
        client.get_channel_videos.return_value = []
        client.get_quota_used.return_value = 0
        mock_yt.return_value = client

        hydrate_metadata(state)

    assert mock_persist_channel.call_count == 1
    persisted_channel = mock_persist_channel.call_args.args[1]
    assert persisted_channel["first_seen_at"] == "2019-03-14T00:00:00Z"


def _brightdata_config():
    """Config double for the Bright Data client.

    Typed values, not bare MagicMocks: `mode` in particular is compared against
    "replay", and a MagicMock there would silently take the live path in a test
    that means to exercise transport.
    """
    cfg = MagicMock()
    cfg.brightdata.api_key = "test-key"
    cfg.brightdata.mode = "live"
    cfg.brightdata.channels_dataset_id = "gd_channels"
    cfg.brightdata.videos_dataset_id = "gd_videos"
    cfg.brightdata.comments_dataset_id = "gd_comments"
    cfg.brightdata.poll_interval_seconds = 0.0
    cfg.brightdata.poll_max_seconds = 1.0
    cfg.harness.brightdata_max_concurrency = 10
    return cfg


@pytest.mark.asyncio
async def test_rate_limit_on_bright_data():
    request = httpx.Request("POST", "https://api.brightdata.com/datasets/v3/trigger")
    response = httpx.Response(429, request=request)

    # (1) tenacity retry is invoked on a 429 (retryable) — 5 attempts, then hard failure.
    with patch("src.tools.bright_data.get_config") as mock_cfg, patch("time.sleep"):
        mock_cfg.return_value = _brightdata_config()

        from src.tools.bright_data import BrightDataClient, BrightDataError

        client = BrightDataClient()
        aclient = MagicMock()
        aclient.post = AsyncMock(return_value=response)

        with pytest.raises(RetryError):
            await client._trigger(aclient, "gd_test", [{"url": "x"}], {})

        assert aclient.post.await_count == 5, "tenacity retry must attempt 5 times on 429"

    # (2) a validation error is permanent — retrying it five times just burns
    # time on an input the API will never accept, so it must fail immediately.
    with patch("src.tools.bright_data.get_config") as mock_cfg, patch("time.sleep"):
        mock_cfg.return_value = _brightdata_config()

        from src.tools.bright_data import BrightDataClient, BrightDataError

        client = BrightDataClient()
        bad_request = httpx.Request("POST", "https://api.brightdata.com/datasets/v3/trigger")
        aclient = MagicMock()
        aclient.post = AsyncMock(
            return_value=httpx.Response(
                400,
                request=bad_request,
                json={"error": "Invalid input provided", "code": "validation_error"},
            )
        )

        with pytest.raises(BrightDataError, match="trigger rejected"):
            await client._trigger(aclient, "gd_test", [{"limit": 5}], {})

        assert aclient.post.await_count == 1, "validation errors must not be retried"

    # (2) the graph's guarded wrapper turns a hard failure into a recorded error, no crash.
    async def boom(state):
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    guarded = _guarded(boom, "keyword_search")
    result = await guarded({"thread_id": "t", "errors": []})
    assert result.get("keyword_search_done") is True
    assert any(e.get("node_name") == "keyword_search" for e in result.get("errors", []))


def test_budget_circuit_breaker_halts():
    """The ceiling is pinned, not inherited from .env.

    This read cfg.budget_limit_usd off the live config while asserting on a
    hardcoded spend of 10.0, so it passed only while the deployment's ceiling
    happened to be <= $10. Raising BUDGET_LIMIT_USD for a real run turned it
    red, reporting a circuit-breaker regression where the breaker was working
    exactly as designed.
    """
    state = {
        "budget_spent_usd": 10.0,
        "novelty_rates": [0.5, 0.5, 0.5],
        "active_node_id": "root",
        "saturated_branches": [],
        "tree": {
            "root": {"id": "root", "label": "root", "status": "active"},
            "branch_a": {"id": "branch_a", "label": "a", "status": "pending"},
            "branch_b": {"id": "branch_b", "label": "b", "status": "saturated"},
        },
    }

    with patch.object(get_config().harness, "budget_limit_usd", 5.0):
        result = check_saturation(state)

    assert result["next_action"] == "budget_exhausted"
    assert result["tree"]["root"]["status"] == "saturated", "active node must be saturated"
    assert result["tree"]["branch_a"]["status"] == "saturated", "pending node must be saturated"
    assert "branch_b" not in result["tree"], "already-saturated nodes must not be rewritten"
    assert set(result.get("saturated_branches", [])) == {"root", "branch_a"}


def test_graph_build_still_valid():
    # Regression guard: the graph wiring still contains the fan-out nodes the chaos
    # tests exercise, and no stale analyze_deep node.
    nodes = set(build_graph().nodes)
    assert {"keyword_search", "graph_walk", "hydrate_metadata", "check_saturation", "cluster_branch"} <= nodes
    assert "analyze_deep" not in nodes

"""Replay mode — the whole graph, end to end, on recorded Bright Data.

This is rung 03 of docs/first-run-plan.md and the highest-value test in the
suite. It runs the real LangGraph wiring, the real routing, the real frontier
and saturation logic, and the real Bright Data client against fixtures captured
from live collectors — at zero record spend.

Every structural bug that would otherwise be found by paying for it (routing,
reducer collisions, non-termination, doubling channels, report generation) is
found here for free. The two bugs that motivated this file — `saturated_branches`
doubling until OOM, and a branch that could never saturate because its round
counter lived in the nodes that were failing — both reproduce end to end and
neither is visible from a unit test of any single node.

Note that replay still reports record counts and accumulates budget. That is
deliberate: the governors must behave identically in replay, or a test that
claims a cap fires proves nothing about the live run.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.config as config_module
from src.graph import run_pipeline
from src.tools.bright_data import BrightDataClient

FIXTURES = Path("tests/fixtures/brightdata")

TAXONOMY_JSON = json.dumps({
    "nodes": [
        {
            "id": "root",
            "label": "Finance",
            "depth": 0,
            "parent_id": None,
            "keywords": ["personal finance"],
            "seed_channels": ["@BenFelixCSI"],
        }
    ]
})

COMPACTION_JSON = json.dumps({
    "narrative_summary": "Independent advisor-led channels dominate the sample.",
    "key_patterns": [
        {"pattern_name": "advisor-led", "description": "credentialed hosts",
         "evidence_summary": "several channels are run by CFA/CFP holders"}
    ],
    "proposed_nodes": [],
})

SYNTHESIS_JSON = json.dumps({
    "summary": "Finance research summary.",
    "findings": [
        {
            "claim": "Advisor-led channels correlate with higher subscriber counts",
            "supporting_channel_ids": ["UCDXTQ8nWmx_EhZ2v-kp7QxA"],
            "pattern_type": "audience_segment",
            "evidence": {},
        }
    ],
    "cannot_determine": ["whether credentials cause the audience size"],
    "discovery_stats": {"total_channels": 22},
})


def _llm(mock):
    def side_effect(tier, prompt, system=None):
        if "Build a taxonomy" in prompt:
            content = TAXONOMY_JSON
        elif tier == "mid":
            content = COMPACTION_JSON
        else:
            content = SYNTHESIS_JSON
        return {
            "content": content,
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            "cost_usd": 0.001,
        }

    mock.side_effect = side_effect
    return mock


@pytest.fixture
def replay_profile(monkeypatch):
    """Smoke profile in replay mode — the exact configuration rung 04 runs live."""
    monkeypatch.setenv("HARNESS_PROFILE", "smoke")
    monkeypatch.setenv("BRIGHTDATA_MODE", "replay")
    monkeypatch.setattr(config_module, "_config", None)
    yield
    monkeypatch.setattr(config_module, "_config", None)


class TestReplayClient:
    def test_fixtures_exist_for_every_collector(self, replay_profile):
        for name in ("channels", "videos", "comments"):
            assert (FIXTURES / f"{name}.json").exists(), f"missing {name} fixture"

    @pytest.mark.asyncio
    async def test_replay_never_calls_the_network(self, replay_profile):
        with patch("httpx.AsyncClient") as http:
            client = BrightDataClient()
            channels, records = await client.get_channels(["@anything"])

        http.assert_not_called()
        assert records == 22
        assert channels[0]["channel_id"].startswith("UC")

    @pytest.mark.asyncio
    async def test_missing_fixture_returns_empty_rather_than_raising(self, tmp_path, monkeypatch):
        """A replay run should exercise the graph's empty-result paths too,
        not crash on the first collector without a recording."""
        monkeypatch.setenv("BRIGHTDATA_MODE", "replay")
        monkeypatch.setenv("BRIGHTDATA_FIXTURES_DIR", str(tmp_path))
        monkeypatch.setattr(config_module, "_config", None)

        client = BrightDataClient()
        rows, records = await client.get_channels(["@x"])

        assert rows == []
        assert records == 0

    @pytest.mark.asyncio
    async def test_comments_fixture_is_capped_at_ten(self, replay_profile):
        """The recording itself must reflect the num_of_comments cap, or replay
        would understate what a live round returns."""
        client = BrightDataClient()
        comments, _ = await client.get_comments(["v"], num_of_comments=10)
        assert len(comments) <= 10
        assert all(c["author_channel_ref"] for c in comments)


class TestReplayEndToEnd:
    @pytest.mark.asyncio
    async def test_full_run_completes_and_produces_a_graded_report(self, replay_profile):
        store = MagicMock()
        store.get_all_videos = AsyncMock(return_value=[])
        store.get_channels_by_ids = AsyncMock(return_value=[])
        store.get_channels_for_node = AsyncMock(return_value=[])
        store.get_videos_for_channels = AsyncMock(return_value=[])

        with (
            patch("src.nodes.taxonomy.complete_tier") as tax,
            patch("src.nodes.compact_branch.complete_tier") as comp,
            patch("src.nodes.synthesize.complete_tier") as syn,
            patch("src.tools.hydrate_metadata.YouTubeAPIClient") as yt,
            patch("src.nodes.compact_branch.get_store", return_value=store),
            patch("src.nodes.synthesize.get_store", return_value=store),
            patch("src.tools.dedup.persist_edges", return_value=0),
            patch("src.db.connection.get_connection"),
            patch("src.db.connection.put_connection"),
        ):
            _llm(tax)
            _llm(comp)
            _llm(syn)
            yt_client = MagicMock()
            yt.return_value = yt_client
            yt_client.get_channels.return_value = []
            yt_client.get_channel_videos.return_value = []
            yt_client.get_quota_used.return_value = 0
            yt_client.quota_consumed_this_call.return_value = 0

            final = await run_pipeline(
                candidate_niches=["Finance"],
                run_id="run-replay",
                thread_id="thread-replay",
            )

        assert final.get("selected_niche") == "Finance"
        assert final.get("final_report") is not None, "a replay run must produce a report"
        assert final["final_report"]["findings"], "report must carry graded findings"

    @pytest.mark.asyncio
    async def test_run_terminates_on_a_stop_reason_not_the_recursion_limit(
        self, replay_profile
    ):
        """The distinction that matters: a run that ends by exception produced
        nothing, and every record it spent getting there is wasted."""
        store = MagicMock()
        store.get_all_videos = AsyncMock(return_value=[])
        store.get_channels_by_ids = AsyncMock(return_value=[])
        store.get_channels_for_node = AsyncMock(return_value=[])
        store.get_videos_for_channels = AsyncMock(return_value=[])

        with (
            patch("src.nodes.taxonomy.complete_tier") as tax,
            patch("src.nodes.compact_branch.complete_tier") as comp,
            patch("src.nodes.synthesize.complete_tier") as syn,
            patch("src.tools.hydrate_metadata.YouTubeAPIClient") as yt,
            patch("src.nodes.compact_branch.get_store", return_value=store),
            patch("src.nodes.synthesize.get_store", return_value=store),
            patch("src.tools.dedup.persist_edges", return_value=0),
            patch("src.db.connection.get_connection"),
            patch("src.db.connection.put_connection"),
        ):
            _llm(tax)
            _llm(comp)
            _llm(syn)
            yt_client = MagicMock()
            yt.return_value = yt_client
            yt_client.get_channels.return_value = []
            yt_client.get_channel_videos.return_value = []
            yt_client.get_quota_used.return_value = 0
            yt_client.quota_consumed_this_call.return_value = 0

            final = await run_pipeline(
                candidate_niches=["Finance"],
                run_id="run-replay-stop",
                thread_id="thread-replay-stop",
            )

        assert final.get("saturated_branches"), "at least one branch must saturate"
        reasons = {
            node.get("saturation_reason")
            for node in final.get("tree", {}).values()
            if node.get("saturation_reason")
        }
        assert reasons, "the stop reason must be recorded on the node"

    @pytest.mark.asyncio
    async def test_saturated_branches_does_not_grow_exponentially(self, replay_profile):
        """Regression: `saturated_branches` is an appending channel. A writer
        that returned the accumulated list instead of a delta doubled it every
        round — 2^n — and OOM-killed the run before it could finish."""
        store = MagicMock()
        store.get_all_videos = AsyncMock(return_value=[])
        store.get_channels_by_ids = AsyncMock(return_value=[])
        store.get_channels_for_node = AsyncMock(return_value=[])
        store.get_videos_for_channels = AsyncMock(return_value=[])

        with (
            patch("src.nodes.taxonomy.complete_tier") as tax,
            patch("src.nodes.compact_branch.complete_tier") as comp,
            patch("src.nodes.synthesize.complete_tier") as syn,
            patch("src.tools.hydrate_metadata.YouTubeAPIClient") as yt,
            patch("src.nodes.compact_branch.get_store", return_value=store),
            patch("src.nodes.synthesize.get_store", return_value=store),
            patch("src.tools.dedup.persist_edges", return_value=0),
            patch("src.db.connection.get_connection"),
            patch("src.db.connection.put_connection"),
        ):
            _llm(tax)
            _llm(comp)
            _llm(syn)
            yt_client = MagicMock()
            yt.return_value = yt_client
            yt_client.get_channels.return_value = []
            yt_client.get_channel_videos.return_value = []
            yt_client.get_quota_used.return_value = 0
            yt_client.quota_consumed_this_call.return_value = 0

            final = await run_pipeline(
                candidate_niches=["Finance"],
                run_id="run-replay-dedup",
                thread_id="thread-replay-dedup",
            )

        branches = final.get("saturated_branches", [])
        assert len(branches) == len(set(branches)), "no branch may appear twice"
        assert len(branches) <= len(final.get("tree", {}))

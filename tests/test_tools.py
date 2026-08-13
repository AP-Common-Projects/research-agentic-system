"""Tests for src/tools — all mock-based, zero real API calls.

Covers: outlier_score, graph_walk (3 fixture variants), keyword_search,
check_saturation, niche_scanner, dedup, signal_scoring.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.tools.dedup import (
    check_near_duplicate,
    is_known_channel,
    persist_channel,
    persist_edge,
    persist_video,
)
from src.tools.graph_walk import graph_walk
from src.tools.hydrate_metadata import _attribute
from src.tools.keyword_search import broaden_or_pivot, keyword_search
from src.tools.niche_scanner import compute_opportunity_score, scan_niches
from src.tools.outlier_score import (
    build_window,
    compute_outlier_score,
    score_channel_videos,
)
from src.tools.saturation import check_saturation
from src.tools.signal_scoring import (
    compute_cadence,
    compute_engagement_rate,
    compute_velocity,
    score_signals,
)


def _make_video(video_id, views, published_at):
    return {"video_id": video_id, "view_count": views, "published_at": published_at}


# ============================================================================
# Outlier score
# ============================================================================

class TestComputeOutlierScore:
    def test_clear_outlier(self):
        score = compute_outlier_score(
            video_views=10000,
            window_views=[1000, 1100, 900, 1050, 950, 1000, 1020, 980, 1010, 990],
        )
        assert score is not None
        assert score > 5.0

    def test_normal_video(self):
        score = compute_outlier_score(
            video_views=1000,
            window_views=[1000, 1100, 900, 1050, 950, 1000, 1020, 980, 1010, 990],
        )
        assert score is not None
        assert abs(score - 1.0) < 0.2

    def test_empty_window_returns_none(self):
        assert compute_outlier_score(100, []) is None

    def test_all_zero_window_returns_none(self):
        assert compute_outlier_score(100, [0, 0, 0]) is None

    def test_zero_views_video(self):
        score = compute_outlier_score(0, [100, 200, 300])
        assert score == 0.0

    def test_single_neighbor(self):
        score = compute_outlier_score(200, [100])
        assert score == 2.0


class TestBuildWindow:
    def test_middle_video_full_window(self):
        videos = [
            _make_video(f"v{i}", 1000 + i * 10, f"2026-01-{i+1:02d}T00:00:00Z")
            for i in range(20)
        ]
        window = build_window(videos, "v10")
        assert len(window) == 10
        assert videos[10]["view_count"] not in window

    def test_first_video_smaller_window(self):
        videos = [
            _make_video(f"v{i}", 1000 + i * 10, f"2026-01-{i+1:02d}T00:00:00Z")
            for i in range(10)
        ]
        window = build_window(videos, "v0")
        assert len(window) == 5

    def test_last_video_smaller_window(self):
        videos = [
            _make_video(f"v{i}", 1000 + i * 10, f"2026-01-{i+1:02d}T00:00:00Z")
            for i in range(10)
        ]
        window = build_window(videos, "v9")
        assert len(window) == 5

    def test_single_video(self):
        videos = [_make_video("v0", 100, "2026-01-01T00:00:00Z")]
        window = build_window(videos, "v0")
        assert window == []

    def test_unknown_video_id(self):
        videos = [_make_video("v0", 100, "2026-01-01T00:00:00Z")]
        window = build_window(videos, "v99")
        assert window == []

    def test_uses_alt_id_field(self):
        videos = [{"id": "abc", "views": 500, "published_at": "2026-01-01T00:00:00Z"}]
        window = build_window(videos, "abc")
        assert window == []


class TestScoreChannelVideos:
    def test_scores_all_videos(self):
        videos = [
            _make_video(f"v{i}", 1000 + i * 10, f"2026-01-{i+1:02d}T00:00:00Z")
            for i in range(10)
        ]
        result = score_channel_videos(videos)
        assert len(result) == 10
        for v in result:
            assert "outlier_score" in v
            assert v["outlier_score"] is not None

    def test_single_video_channel_returns_none_score(self):
        videos = [_make_video("v0", 100, "2026-01-01T00:00:00Z")]
        result = score_channel_videos(videos)
        assert len(result) == 1
        assert result[0]["outlier_score"] is None


# ============================================================================
# Graph walk — frontier regression tests
# ============================================================================

class TestGraphWalkFrontier:
    @pytest.mark.asyncio
    async def test_fast_saturating_graph(self):
        state = {
            "tree": {
                "node1": {
                    "id": "node1",
                    "label": "test",
                    "seed_channel_ids": ["ch_a", "ch_b"],
                    "unexpanded_channel_ids": [],
                },
            },
            "active_node_id": "node1",
            "discovered_channel_ids": [],
            "visited_channel_ids": set(),
            "expanded_channel_ids": set(),
            "novelty_rates": [],
        }

        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd:
            mock_client = MagicMock()
            mock_bd.return_value = mock_client
            mock_client.crawl_channel_relationships.return_value = [
                {"source_channel_id": "ch_a", "target_channel_id": "ch_c", "edge_type": "playlist"},
                {"source_channel_id": "ch_b", "target_channel_id": "ch_d", "edge_type": "playlist"},
            ]
            result = await graph_walk(state)

        assert "ch_c" in result["discovered_channel_ids"]
        assert "ch_d" in result["discovered_channel_ids"]
        assert "ch_a" in result["visited_channel_ids"]
        assert "ch_b" in result["visited_channel_ids"]
        assert "ch_a" in result["expanded_channel_ids"]
        assert "ch_b" in result["expanded_channel_ids"]
        assert len(result["novelty_rates"]) == 1

    @pytest.mark.asyncio
    async def test_slow_saturating_graph_multi_round(self):
        state = {
            "tree": {
                "node1": {
                    "id": "node1",
                    "label": "test",
                    "seed_channel_ids": ["ch_a"],
                    "unexpanded_channel_ids": [],
                },
            },
            "active_node_id": "node1",
            "discovered_channel_ids": [],
            "visited_channel_ids": set(),
            "expanded_channel_ids": set(),
            "novelty_rates": [],
        }

        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd:
            mock_client = MagicMock()
            mock_bd.return_value = mock_client

            mock_client.crawl_channel_relationships.return_value = [
                {"source_channel_id": "ch_a", "target_channel_id": "ch_b", "edge_type": "playlist"},
            ]
            r1 = await graph_walk(state)
            state["discovered_channel_ids"].extend(r1["discovered_channel_ids"])
            state["visited_channel_ids"] |= r1["visited_channel_ids"]
            state["expanded_channel_ids"] |= r1["expanded_channel_ids"]
            state["tree"].update(r1.get("tree", {}))

            mock_client.crawl_channel_relationships.return_value = [
                {"source_channel_id": "ch_b", "target_channel_id": "ch_c", "edge_type": "playlist"},
            ]
            r2 = await graph_walk(state)
            state["discovered_channel_ids"].extend(r2["discovered_channel_ids"])
            state["visited_channel_ids"] |= r2["visited_channel_ids"]
            state["tree"].update(r2.get("tree", {}))

            mock_client.crawl_channel_relationships.return_value = []
            r3 = await graph_walk(state)
            state["visited_channel_ids"] |= r3["visited_channel_ids"]

        assert "ch_b" in state["discovered_channel_ids"]
        assert "ch_c" in state["discovered_channel_ids"]
        assert "ch_a" in state["visited_channel_ids"]
        assert "ch_b" in state["visited_channel_ids"]
        assert "ch_c" in state["visited_channel_ids"]
        assert len(r3["discovered_channel_ids"]) == 0
        assert r3["novelty_rates"][0] == 0.0

    @pytest.mark.asyncio
    async def test_re_scan_regression_fixture(self):
        state = {
            "tree": {
                "node1": {
                    "id": "node1",
                    "label": "test",
                    "seed_channel_ids": ["ch_a"],
                    "unexpanded_channel_ids": [],
                },
            },
            "active_node_id": "node1",
            "discovered_channel_ids": [],
            "visited_channel_ids": set(),
            "expanded_channel_ids": set(),
            "novelty_rates": [],
        }

        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd:
            mock_client = MagicMock()
            mock_bd.return_value = mock_client

            mock_client.crawl_channel_relationships.return_value = [
                {"source_channel_id": "ch_a", "target_channel_id": "ch_b", "edge_type": "playlist"},
                {"source_channel_id": "ch_a", "target_channel_id": "ch_c", "edge_type": "playlist"},
            ]
            r1 = await graph_walk(state)
            assert r1["novelty_rates"][0] == 2.0

            state["discovered_channel_ids"].extend(r1["discovered_channel_ids"])
            state["visited_channel_ids"] |= r1["visited_channel_ids"]
            state["expanded_channel_ids"] |= r1["expanded_channel_ids"]
            state["tree"].update(r1.get("tree", {}))

            mock_client.crawl_channel_relationships.return_value = [
                {"source_channel_id": "ch_b", "target_channel_id": "ch_d", "edge_type": "playlist"},
                {"source_channel_id": "ch_c", "target_channel_id": "ch_e", "edge_type": "playlist"},
            ]
            r2 = await graph_walk(state)
            assert len(r2["discovered_channel_ids"]) == 2
            assert r2["novelty_rates"][0] == 1.0

            state["discovered_channel_ids"].extend(r2["discovered_channel_ids"])
            state["visited_channel_ids"] |= r2["visited_channel_ids"]
            state["tree"].update(r2.get("tree", {}))

            mock_client.crawl_channel_relationships.return_value = []
            r3 = await graph_walk(state)
            assert r3["novelty_rates"][0] == 0.0

        assert set(state["discovered_channel_ids"]) == {"ch_b", "ch_c", "ch_d", "ch_e"}

    @pytest.mark.asyncio
    async def test_graph_walk_respects_visited(self):
        state = {
            "tree": {
                "node1": {
                    "id": "node1",
                    "label": "test",
                    "seed_channel_ids": ["ch_a"],
                    "unexpanded_channel_ids": [],
                },
            },
            "active_node_id": "node1",
            "discovered_channel_ids": ["ch_b"],
            "visited_channel_ids": {"ch_a"},
            "expanded_channel_ids": {"ch_a"},
            "novelty_rates": [],
        }

        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd:
            mock_client = MagicMock()
            mock_bd.return_value = mock_client
            mock_client.crawl_channel_relationships.return_value = [
                {"source_channel_id": "ch_a", "target_channel_id": "ch_b", "edge_type": "playlist"},
            ]
            result = await graph_walk(state)

        assert "discovered_channel_ids" not in result
        assert result["graph_walk_done"] is True
        assert result["tree"]["node1"]["_gw_exhausted"] is True
        mock_client.crawl_channel_relationships.assert_not_called()

    @pytest.mark.asyncio
    async def test_graph_walk_expands_new_unexpanded_not_seed(self):
        state = {
            "tree": {
                "node1": {
                    "id": "node1",
                    "label": "test",
                    "seed_channel_ids": ["ch_a"],
                    "unexpanded_channel_ids": ["ch_b"],
                },
            },
            "active_node_id": "node1",
            "discovered_channel_ids": ["ch_b"],
            "visited_channel_ids": {"ch_a"},
            "expanded_channel_ids": {"ch_a"},
            "novelty_rates": [],
        }

        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd:
            mock_client = MagicMock()
            mock_bd.return_value = mock_client
            mock_client.crawl_channel_relationships.return_value = [
                {"source_channel_id": "ch_b", "target_channel_id": "ch_c", "edge_type": "playlist"},
            ]
            result = await graph_walk(state)

        assert result["discovered_channel_ids"] == ["ch_c"]

    @pytest.mark.asyncio
    async def test_no_active_node(self):
        state = {"tree": {}, "active_node_id": None}
        result = await graph_walk(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_frontier(self):
        state = {
            "tree": {
                "node1": {
                    "id": "node1",
                    "label": "test",
                    "seed_channel_ids": [],
                    "unexpanded_channel_ids": [],
                },
            },
            "active_node_id": "node1",
            "discovered_channel_ids": [],
            "visited_channel_ids": set(),
            "expanded_channel_ids": set(),
            "novelty_rates": [],
        }
        result = await graph_walk(state)
        assert result["graph_walk_done"] is True
        assert result["tree"]["node1"]["_gw_exhausted"] is True


# ============================================================================
# Keyword search — frontier-equivalent
# ============================================================================

class TestBroadenOrPivot:
    def test_first_call_returns_base_keywords(self):
        result = broaden_or_pivot(
            keywords=["cooking", "recipes"],
            queries_run=[],
            previous_results=[],
        )
        assert "cooking" in result
        assert "recipes" in result

    def test_broaden_adds_qualifiers(self):
        result = broaden_or_pivot(
            keywords=["gardening"],
            queries_run=["gardening"],
            previous_results=[{"title": "How to grow tomatoes"}],
        )
        assert "gardening" not in result
        assert any("gardening" in q for q in result)

    def test_does_not_resubmit_same_query(self):
        result = broaden_or_pivot(
            keywords=["gaming"],
            queries_run=["gaming", "gaming beginner"],
            previous_results=[],
        )
        assert "gaming" not in result
        assert "gaming beginner" not in result

    def test_pivots_when_few_results(self):
        result = broaden_or_pivot(
            keywords=["diy"],
            queries_run=["diy", "diy tutorial"],
            previous_results=[
                {"title": "Woodworking basics for beginners"},
                {"title": "Metal crafting ideas"},
            ],
        )
        flattened = " ".join(result)
        assert "woodworking" in flattened.lower()

    def test_pivot_filters_short_words(self):
        result = broaden_or_pivot(
            keywords=["cats"],
            queries_run=["cats"],
            previous_results=[{"title": "the and for you how with"}],
        )
        assert "the" not in result
        assert "and" not in result


class TestKeywordSearch:
    @pytest.mark.asyncio
    async def test_first_round_searches_keywords(self):
        state = {
            "tree": {
                "node1": {
                    "id": "node1",
                    "label": "test",
                    "keywords": ["cooking"],
                    "queries_run": [],
                },
            },
            "active_node_id": "node1",
            "discovered_channel_ids": [],
            "novelty_rates": [],
        }

        with patch("src.tools.keyword_search.BrightDataClient") as mock_bd:
            mock_client = MagicMock()
            mock_bd.return_value = mock_client
            mock_client.search_youtube.return_value = [
                {"channel_id": "ch_new", "title": "Cooking 101"},
            ]
            result = await keyword_search(state)

        assert "ch_new" in result["discovered_channel_ids"]
        assert result["keyword_search_done"] is True
        updated_node = result["tree"]["node1"]
        assert "cooking" in updated_node["queries_run"]

    @pytest.mark.asyncio
    async def test_skips_already_run_queries(self):
        state = {
            "tree": {
                "node1": {
                    "id": "node1",
                    "label": "test",
                    "keywords": ["cooking"],
                    "queries_run": [
                        "cooking", "cooking beginner", "cooking tutorial",
                        "cooking 2026", "cooking best of", "cooking how to",
                        "cooking guide",
                    ],
                },
            },
            "active_node_id": "node1",
            "discovered_channel_ids": [],
            "novelty_rates": [],
        }

        with patch("src.tools.keyword_search.BrightDataClient") as mock_bd:
            mock_client = MagicMock()
            mock_bd.return_value = mock_client
            result = await keyword_search(state)

        assert result.get("keyword_search_done") is True

    @pytest.mark.asyncio
    async def test_no_active_node(self):
        state = {"tree": {}, "active_node_id": None}
        result = await keyword_search(state)
        assert result == {}


# ============================================================================
# check_saturation — three-way routing
# ============================================================================

class TestCheckSaturation:
    def setup_method(self):
        self._cfg_patcher = patch("src.tools.saturation.get_config")
        mock_cfg = MagicMock()
        mock_cfg.harness.saturation_novelty_threshold = 0.05
        mock_cfg.harness.saturation_consecutive_window = 3
        mock_cfg.harness.budget_limit_usd = 10.0
        self.mock_cfg = mock_cfg
        self._cfg_patcher.start().return_value = mock_cfg

    def teardown_method(self):
        self._cfg_patcher.stop()

    def test_expand_deeper_when_high_novelty(self):
        state = {
            "tree": {"node1": {
                "id": "node1", "status": "active",
                "_kw_novelty_history": [0.5, 0.4, 0.3],
                "_gw_novelty_history": [0.5, 0.4, 0.3],
            }},
            "active_node_id": "node1",
            "budget_spent_usd": 1.0,
            "saturated_branches": [],
        }
        result = check_saturation(state)
        assert result["next_action"] == "expand_deeper"

    def test_saturated_after_consecutive_low_novelty(self):
        state = {
            "tree": {"node1": {
                "id": "node1", "status": "active",
                "_kw_novelty_history": [0.01, 0.02, 0.03],
                "_gw_novelty_history": [0.01, 0.02, 0.03],
            }},
            "active_node_id": "node1",
            "budget_spent_usd": 1.0,
            "saturated_branches": [],
        }
        result = check_saturation(state)
        assert result["next_action"] == "saturated"
        assert result["tree"]["node1"]["status"] == "saturated"
        assert "node1" in result["saturated_branches"]

    def test_not_saturated_with_insufficient_rounds(self):
        state = {
            "tree": {"node1": {
                "id": "node1", "status": "active",
                "_kw_novelty_history": [0.01, 0.02],
                "_gw_novelty_history": [0.01, 0.02],
            }},
            "active_node_id": "node1",
            "budget_spent_usd": 1.0,
            "saturated_branches": [],
        }
        result = check_saturation(state)
        assert result["next_action"] == "expand_deeper"

    def test_saturated_when_both_tracks_exhausted(self):
        state = {
            "tree": {"node1": {
                "id": "node1", "status": "active",
                "_kw_novelty_history": [0.5],
                "_gw_novelty_history": [0.5],
                "_kw_exhausted": True,
                "_gw_exhausted": True,
            }},
            "active_node_id": "node1",
            "budget_spent_usd": 1.0,
            "saturated_branches": [],
        }
        result = check_saturation(state)
        assert result["next_action"] == "saturated"

    def test_budget_exhausted_marks_all_active_saturated(self):
        state = {
            "tree": {
                "node1": {"id": "node1", "status": "active"},
                "node2": {"id": "node2", "status": "pending"},
                "node3": {"id": "node3", "status": "saturated"},
            },
            "active_node_id": "node1",
            "budget_spent_usd": 10.0,
            "saturated_branches": [],
        }
        result = check_saturation(state)
        assert result["next_action"] == "budget_exhausted"
        assert result["tree"]["node1"]["status"] == "saturated"
        assert result["tree"]["node2"]["status"] == "saturated"
        assert "node3" not in result["tree"]

    def test_budget_exhausted_below_limit(self):
        state = {
            "tree": {"node1": {"id": "node1", "status": "active"}},
            "active_node_id": "node1",
            "budget_spent_usd": 11.0,
            "saturated_branches": [],
        }
        result = check_saturation(state)
        assert result["next_action"] == "budget_exhausted"

    def test_one_low_round_breaks_consecutive(self):
        state = {
            "tree": {"node1": {
                "id": "node1", "status": "active",
                "_kw_novelty_history": [0.01, 0.4, 0.3, 0.03],
                "_gw_novelty_history": [0.01, 0.4, 0.3, 0.03],
            }},
            "active_node_id": "node1",
            "budget_spent_usd": 1.0,
            "saturated_branches": [],
        }
        result = check_saturation(state)
        assert result["next_action"] == "expand_deeper"


# ============================================================================
# Niche scanner
# ============================================================================

class TestNicheScanner:
    def test_deterministic_same_input(self):
        niches = ["gaming", "cooking", "diy_crafts"]
        r1 = scan_niches({"candidate_niches": niches, "selected_niche": ""})
        r2 = scan_niches({"candidate_niches": niches, "selected_niche": ""})
        assert r1["selected_niche"] == r2["selected_niche"]
        assert r1["niche_scanner_evidence"]["ranked"] == r2["niche_scanner_evidence"]["ranked"]

    def test_ranks_niches(self):
        niches = ["gaming", "cooking", "diy_crafts"]
        result = scan_niches({"candidate_niches": niches, "selected_niche": ""})
        assert result["selected_niche"] != ""
        ranked = result["niche_scanner_evidence"]["ranked"]
        assert len(ranked) == 3
        scores = [r["opportunity_score"] for r in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_opportunity_score_range(self):
        evidence = {
            "channel_count_estimate": 1000,
            "avg_views": 5000,
            "engagement": 5.0,
            "growth_signal": 0.5,
        }
        score = compute_opportunity_score(evidence)
        assert 0.0 <= score <= 1.0

    def test_opportunity_score_zero_competition(self):
        score = compute_opportunity_score({
            "channel_count_estimate": 0,
            "avg_views": 0,
            "engagement": 0,
            "growth_signal": 0,
        })
        assert score == pytest.approx(0.5)

    def test_empty_niches(self):
        result = scan_niches({"candidate_niches": [], "selected_niche": ""})
        assert result["next_action"] == "no_niches"
        assert result["selected_niche"] == ""


# ============================================================================
# Dedup
# ============================================================================

class TestIsKnownChannel:
    def test_known(self):
        assert is_known_channel("ch_a", {"ch_a", "ch_b"}) is True

    def test_unknown(self):
        assert is_known_channel("ch_c", {"ch_a", "ch_b"}) is False

    def test_empty_set(self):
        assert is_known_channel("ch_a", set()) is False


class TestCheckNearDuplicate:
    def test_exact_match(self):
        assert check_near_duplicate("Hello World", "Hello World") is True

    def test_case_insensitive(self):
        assert check_near_duplicate("Hello World", "hello world") is True

    def test_punctuation_stripped(self):
        assert check_near_duplicate("Hello, World!", "hello world") is True

    def test_high_similarity(self):
        assert check_near_duplicate(
            "How to bake a cake at home",
            "How to bake a cake at home easy",
            similarity_threshold=0.88,
        ) is True

    def test_low_similarity(self):
        assert check_near_duplicate(
            "How to bake a cake",
            "Top 10 gaming setups 2026",
        ) is False

    def test_below_threshold(self):
        assert check_near_duplicate(
            "How to bake",
            "Top 10 gaming",
            similarity_threshold=0.95,
        ) is False

    def test_empty_strings(self):
        assert check_near_duplicate("", "something") is False
        assert check_near_duplicate("something", "") is False

    def test_punctuation_only(self):
        assert check_near_duplicate("!!!", "###") is False


class TestPersistChannel:
    def test_upsert(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor

        channel = {
            "channel_id": "ch_test",
            "title": "Test Channel",
            "subscriber_count": 1000,
            "description": "A test",
            "first_seen_at": "2026-01-01",
            "discovery_method": "graph_walk",
            "extra": '{"key": "value"}',
        }

        persist_channel(conn, channel)

        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        cursor.close.assert_called_once()

    def test_rollback_on_error(self):
        cursor = MagicMock()
        cursor.execute.side_effect = RuntimeError("db error")
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with pytest.raises(RuntimeError):
            persist_channel(conn, {"channel_id": "ch_err", "title": "x", "subscriber_count": 0,
                                   "description": "", "first_seen_at": "", "discovery_method": "",
                                   "extra": "{}"})

        conn.rollback.assert_called_once()
        cursor.close.assert_called_once()


class TestPersistVideo:
    def test_upsert(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor

        video = {
            "video_id": "vid_1",
            "channel_id": "ch_1",
            "title": "Test Video",
            "view_count": 5000,
            "like_count": 200,
            "comment_count": 30,
            "published_at": "2026-06-01T00:00:00Z",
            "outlier_score": 1.5,
            "scraped_at": "now",
            "extra": "{}",
        }

        persist_video(conn, video)

        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        cursor.close.assert_called_once()


class TestPersistEdge:
    def test_upsert(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor

        edge = {
            "source_channel_id": "ch_a",
            "target_channel_id": "ch_b",
            "edge_type": "playlist",
            "discovered_at": "now",
            "run_id": "run_1",
        }

        persist_edge(conn, edge)

        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        cursor.close.assert_called_once()


# ============================================================================
# Signal scoring
# ============================================================================

class TestEngagementRate:
    def test_normal(self):
        rate = compute_engagement_rate({
            "view_count": 10000,
            "like_count": 500,
            "comment_count": 100,
        })
        assert rate == pytest.approx(0.06)

    def test_zero_views(self):
        assert compute_engagement_rate({"view_count": 0, "like_count": 100, "comment_count": 10}) == 0.0

    def test_alt_fields(self):
        rate = compute_engagement_rate({"views": 1000, "likes": 50, "comments": 10})
        assert rate == pytest.approx(0.06)


class TestCadence:
    def test_daily_uploads(self):
        videos = [
            {"published_at": "2026-07-01T00:00:00Z"},
            {"published_at": "2026-06-30T00:00:00Z"},
            {"published_at": "2026-06-29T00:00:00Z"},
            {"published_at": "2026-06-28T00:00:00Z"},
        ]
        cadence = compute_cadence(videos)
        assert cadence == pytest.approx(40.0, abs=5)

    def test_single_video(self):
        assert compute_cadence([{"published_at": "2026-01-01T00:00:00Z"}]) == 0.0

    def test_empty_list(self):
        assert compute_cadence([]) == 0.0


class TestVelocity:
    def test_stable(self):
        videos = []
        for i in range(20):
            videos.append({
                "published_at": f"2026-01-{i+1:02d}T00:00:00Z",
                "view_count": 1000,
            })
        assert compute_velocity(videos) == pytest.approx(1.0, rel=0.01)

    def test_growing(self):
        videos = []
        for i in range(20):
            videos.append({
                "published_at": f"2026-01-{i+1:02d}T00:00:00Z",
                "view_count": 1000 + i * 500,
            })
        vel = compute_velocity(videos)
        assert vel > 1.0

    def test_too_few_videos(self):
        videos = [{"published_at": "2026-01-01T00:00:00Z", "view_count": 100}]
        assert compute_velocity(videos) == 1.0

    def test_zero_older_views(self):
        videos = []
        for i in range(20):
            v = 0 if i >= 5 else 500
            videos.append({
                "published_at": f"2026-01-{i+1:02d}T00:00:00Z",
                "view_count": v,
            })
        vel = compute_velocity(videos)
        assert vel >= 1.0


class TestScoreSignals:
    def test_returns_expected_keys(self):
        state = {
            "discovered_channel_ids": ["ch_a"],
            "discovered_video_ids": ["v1"],
            "novelty_rates": [],
        }
        result = score_signals(state)
        assert "next_action" in result
        assert result["next_action"] == "continue"

class TestDiscoveryAttribution:
    """Per-track attribution — master plan §1 requires proving the graph-walk
    track surfaced channels the keyword track missed, which is only checkable
    if each channel records which track(s) actually found it."""

    def test_graph_walk_exclusive_channel_labeled_graph_walk(self):
        assert _attribute("UC_hidden", {"UC_pop"}, {"UC_hidden"}) == "graph_walk"

    def test_keyword_exclusive_channel_labeled_keyword(self):
        assert _attribute("UC_pop", {"UC_pop"}, {"UC_hidden"}) == "keyword"

    def test_channel_found_by_both_tracks_labeled_both(self):
        assert _attribute("UC_x", {"UC_x"}, {"UC_x"}) == "both"

    def test_channel_in_neither_set_is_unattributed_not_guessed(self):
        # Pre-v4 checkpoints carry no attribution; guessing a track here would
        # fabricate the very evidence the report grades on.
        assert _attribute("UC_legacy", set(), set()) == "unattributed"

    @pytest.mark.asyncio
    async def test_keyword_search_records_all_returned_channels(self):
        """Attribution records keyword-findability, not first-discovery: a
        channel graph_walk saw first is still keyword-findable if the keyword
        track also returns it."""
        state = {
            "tree": {"n1": {"id": "n1", "keywords": ["kw"], "queries_run": []}},
            "active_node_id": "n1",
            "discovered_channel_ids": ["ch_seen"],
            "novelty_rates": [],
        }
        with patch("src.tools.keyword_search.BrightDataClient") as mock_bd:
            mock_client = MagicMock()
            mock_bd.return_value = mock_client
            mock_client.search_youtube.return_value = [
                {"channel_id": "ch_seen", "title": "already discovered"},
                {"channel_id": "ch_new", "title": "brand new"},
            ]
            result = await keyword_search(state)

        assert result["keyword_channel_ids"] == {"ch_seen", "ch_new"}
        assert result["discovered_channel_ids"] == ["ch_new"]

    @pytest.mark.asyncio
    async def test_graph_walk_records_all_reachable_channels(self):
        state = {
            "tree": {"n1": {"id": "n1", "seed_channel_ids": ["ch_a"], "unexpanded_channel_ids": []}},
            "active_node_id": "n1",
            "discovered_channel_ids": ["ch_known"],
            "visited_channel_ids": set(),
            "expanded_channel_ids": set(),
            "novelty_rates": [],
        }
        with patch("src.tools.graph_walk.BrightDataClient") as mock_bd:
            mock_client = MagicMock()
            mock_bd.return_value = mock_client
            mock_client.crawl_channel_relationships.return_value = [
                {"source_channel_id": "ch_a", "target_channel_id": "ch_known", "edge_type": "playlist"},
                {"source_channel_id": "ch_a", "target_channel_id": "ch_fresh", "edge_type": "playlist"},
            ]
            result = await graph_walk(state)

        assert result["graph_walk_channel_ids"] == {"ch_known", "ch_fresh"}
        assert result["discovered_channel_ids"] == ["ch_fresh"]

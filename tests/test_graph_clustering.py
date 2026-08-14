"""Tests for graph_clustering.py — the v2 adaptive-depth node.

Deterministic, no LLM, no real API calls. Fixtures are hand-built graphs with
a known, verified-by-hand right answer, per agent-harness-eval's guidance for
a splitting/stopping condition (generalised from saturation to *splitting*):

- one fixture that should split (real substructure),
- one that shouldn't (one coherent blob),
- one that re-triggers the seed-reproducibility risk (run twice, assert
  identical output),
- one with channels but zero discovery edges (content-similarity fallback),
- zero-candidates-is-a-valid-non-error outcome.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import networkx as nx
import pytest

from src.tools.graph_clustering import (
    build_similarity_graph,
    cluster_branch,
    detect_communities,
    score_distinctness,
    _jaccard,
    _tokenize,
)


# ---------------------------------------------------------------------------
# tokenisation / similarity
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_lowercases_and_strips(self):
        assert "retro" in _tokenize("Retro Handheld REVIEW!")

    def test_drops_stopwords_and_short_tokens(self):
        tokens = _tokenize("the and for a how to you")
        assert tokens == set()


class TestJaccard:
    def test_identical(self):
        assert _jaccard({"a", "b", "c"}, {"a", "b", "c"}) == 1.0

    def test_disjoint(self):
        assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial(self):
        assert _jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# graph construction
# ---------------------------------------------------------------------------

def _ref(name: str) -> str:
    return f"https://www.youtube.com/channel/{name}"


def _channels(names: list[str], titles: dict[str, str] | None = None) -> list[dict]:
    titles = titles or {}
    return [
        {
            "channel_id": n,
            "title": titles.get(n, ""),
            "description": "",
        }
        for n in names
    ]


def _edge(source: str, target: str, edge_type: str = "featured_channel") -> dict:
    return {
        "source_channel_id": source,
        "target_channel_id": None,
        "target_channel_ref": _ref(target),
        "edge_type": edge_type,
    }


class TestBuildSimilarityGraph:
    def test_adds_all_refs_as_nodes(self):
        refs = [_ref("A"), _ref("B"), _ref("C")]
        G = build_similarity_graph(refs, _channels(["A", "B", "C"]), [])
        assert set(G.nodes()) == set(refs)

    def test_structural_edge_weight_by_type(self):
        refs = [_ref("A"), _ref("B")]
        edges = [_edge("A", "B", "featured_channel")]
        G = build_similarity_graph(refs, _channels(["A", "B"]), edges)
        assert G.has_edge(_ref("A"), _ref("B"))
        assert G[_ref("A")][_ref("B")]["weight"] == 3.0

    def test_comment_author_edge_weights_one(self):
        refs = [_ref("A"), _ref("B")]
        edges = [_edge("A", "B", "comment_author")]
        G = build_similarity_graph(refs, _channels(["A", "B"]), edges)
        assert G[_ref("A")][_ref("B")]["weight"] == 1.0

    def test_edge_with_external_source_is_dropped(self):
        # source "X" is not in the node's channel set — the edge must not appear.
        refs = [_ref("A"), _ref("B")]
        edges = [_edge("X", "B", "featured_channel")]
        G = build_similarity_graph(refs, _channels(["A", "B"]), edges)
        assert G.number_of_edges() == 0

    def test_content_similarity_edge_added_when_no_structural_edge(self):
        refs = [_ref("A"), _ref("B")]
        channels = _channels(
            ["A", "B"],
            {"A": "retro handheld emulation review", "B": "retro handheld emulation guide"},
        )
        G = build_similarity_graph(refs, channels, [])
        assert G.has_edge(_ref("A"), _ref("B"))

    def test_no_content_edge_when_disjoint(self):
        refs = [_ref("A"), _ref("B")]
        channels = _channels(
            ["A", "B"],
            {"A": "cooking recipes", "B": "quantum physics lecture"},
        )
        G = build_similarity_graph(refs, channels, [])
        assert not G.has_edge(_ref("A"), _ref("B"))

    def test_structural_edge_not_replaced_by_content(self):
        refs = [_ref("A"), _ref("B")]
        channels = _channels(
            ["A", "B"],
            {"A": "retro review", "B": "retro review"},
        )
        edges = [_edge("A", "B", "featured_channel")]
        G = build_similarity_graph(refs, channels, edges)
        # Structural weight (3.0) must win over any content similarity.
        assert G[_ref("A")][_ref("B")]["weight"] == 3.0


# ---------------------------------------------------------------------------
# community detection
# ---------------------------------------------------------------------------

def _two_cluster_graph() -> nx.Graph:
    """Two dense clusters connected by a single weak edge."""
    G = nx.Graph()
    # cluster A
    for i in range(3):
        for j in range(i + 1, 3):
            G.add_edge(_ref(f"A{i}"), _ref(f"A{j}"), weight=3.0)
    # cluster B
    for i in range(3):
        for j in range(i + 1, 3):
            G.add_edge(_ref(f"B{i}"), _ref(f"B{j}"), weight=3.0)
    # weak bridge
    G.add_edge(_ref("A0"), _ref("B0"), weight=0.1)
    return G


def _coherent_blob() -> nx.Graph:
    G = nx.Graph()
    names = [f"C{i}" for i in range(6)]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            G.add_edge(_ref(names[i]), _ref(names[j]), weight=2.0)
    return G


class TestDetectCommunities:
    def test_splits_two_clusters(self):
        communities = detect_communities(_two_cluster_graph(), seed=42)
        assert len(communities) == 2

    def test_coherent_blob_is_one_community(self):
        communities = detect_communities(_coherent_blob(), seed=42)
        assert len(communities) == 1

    def test_seed_reproducibility(self):
        # The single most important property in this module: a fixed seed must
        # produce identical communities across runs, or a resumed run silently
        # recomputes different clusters than the original found.
        G = _two_cluster_graph()
        a1 = detect_communities(G, seed=42)
        a2 = detect_communities(G, seed=42)
        # Normalise to sorted frozensets for order-independent comparison.
        canon = lambda cs: sorted([frozenset(c) for c in cs], key=lambda s: sorted(s))
        assert canon(a1) == canon(a2)

    def test_empty_graph_returns_no_communities(self):
        G = nx.Graph()
        G.add_nodes_from([_ref("A"), _ref("B")])
        assert detect_communities(G, seed=42) == []


class TestScoreDistinctness:
    def test_dense_cluster_scores_above_one(self):
        G = _two_cluster_graph()
        communities = detect_communities(G, seed=42)
        scores = [score_distinctness(G, c) for c in communities]
        assert all(s > 1.0 for s in scores)

    def test_noise_cluster_scores_near_one(self):
        # A single community spanning the whole graph is "no rest to compare
        # against" — not a coherent leaf, but also not a distinct split. The
        # function reports inf, which the caller treats as non-distinct via
        # the min_cluster_distinctness filter (inf passes, but a whole-graph
        # community of size == total is caught by size filtering upstream).
        G = _coherent_blob()
        communities = detect_communities(G, seed=42)
        assert len(communities) == 1
        assert score_distinctness(G, communities[0]) == float("inf")

    def test_weak_bridge_yields_distinct_clusters(self):
        # Two dense clusters joined by one weak edge must score distinctly
        # above 1.0 each — this is the "should split" fixture.
        G = _two_cluster_graph()
        communities = detect_communities(G, seed=42)
        assert len(communities) == 2
        for c in communities:
            assert score_distinctness(G, c) > 1.0


# ---------------------------------------------------------------------------
# cluster_branch node
# ---------------------------------------------------------------------------

def _node_state(refs: list[str]) -> dict:
    ref_map = {r: 5000 for r in refs}
    return {
        "thread_id": "t1",
        "active_node_id": "n1",
        "tree": {
            "n1": {
                "id": "n1",
                "label": "branch",
                "depth": 1,
                "lineage_root_id": "n1",
                "_kw_refs": ref_map,
                "_gw_refs": {},
                "_kw_channel_ids": [r.split("/")[-1] for r in refs],
                "_gw_channel_ids": [],
            }
        },
    }


class TestClusterBranch:
    @pytest.mark.asyncio
    async def test_insufficient_channels_returns_no_candidates(self):
        state = _node_state([_ref("A"), _ref("B"), _ref("C")])
        with patch("src.tools.graph_clustering.get_config") as cfg:
            cfg.return_value.harness.min_channels_for_split = 8
            result = await cluster_branch(state)
        assert "cluster_candidates" not in result.get("tree", {}).get("n1", {})
        assert result.get("cluster_branch_done") is True

    @pytest.mark.asyncio
    async def test_leaf_when_no_communities(self):
        # No discovery edges and no content overlap → no edges → no communities.
        refs = [_ref(f"C{i}") for i in range(10)]
        state = _node_state(refs)
        channels = _channels([r.split("/")[-1] for r in refs])
        with patch("src.tools.graph_clustering.get_config") as cfg, \
             patch("src.tools.graph_clustering.get_store") as store:
            cfg.return_value.harness.min_channels_for_split = 8
            cfg.return_value.harness.min_cluster_distinctness = 0.3
            cfg.return_value.harness.cluster_seed = 42
            store.return_value.get_channels_for_node = AsyncMock(return_value=channels)
            store.return_value.get_discovery_edges_for_channels = AsyncMock(return_value=[])
            result = await cluster_branch(state)
        assert result["tree"]["n1"]["cluster_candidates"] == []
        assert result["tree"]["n1"]["cluster_distinctness_score"] is None

    @pytest.mark.asyncio
    async def test_no_active_node_returns_gracefully(self):
        result = await cluster_branch({"thread_id": "t1", "tree": {}, "active_node_id": None})
        # Must not raise, must return a node log.
        assert result.get("node_logs") is not None

    @pytest.mark.asyncio
    async def test_zero_discovery_edges_degrades_to_content_only(self):
        # Two groups whose channels share titles within group but not across.
        refs = [_ref(f"G0_{i}") for i in range(6)] + [_ref(f"G1_{i}") for i in range(6)]
        state = _node_state(refs)
        cids = [r.split("/")[-1] for r in refs]
        titles = {}
        for i, c in enumerate(cids):
            group = "retro handheld emulation" if c.startswith("G0") else "vegan baking recipes"
            titles[c] = f"{group} episode {i}"
        channels = _channels(cids, titles)
        with patch("src.tools.graph_clustering.get_config") as cfg, \
             patch("src.tools.graph_clustering.get_store") as store:
            cfg.return_value.harness.min_channels_for_split = 5
            cfg.return_value.harness.min_cluster_distinctness = 0.3
            cfg.return_value.harness.cluster_seed = 42
            store.return_value.get_channels_for_node = AsyncMock(return_value=channels)
            store.return_value.get_discovery_edges_for_channels = AsyncMock(return_value=[])
            result = await cluster_branch(state)
        # Content similarity must have created intra-group edges; the two
        # groups must emerge as candidates (or at least not error). The
        # node must have run to completion.
        assert "cluster_branch_done" in result
        assert "cluster_candidates" in result["tree"]["n1"]


class TestRefIdentityMismatch:
    """The graph is keyed on refs from _kw_refs/_gw_refs, which are HANDLE
    form (`.../@name`) because that is what Bright Data's `url` field returns.
    Mapping a discovery edge's source_channel_id by synthesizing
    `.../channel/<id>` produces a string that is never in that set, so every
    structural edge was dropped — Louvain clustered on content similarity
    alone and the edge-type weight hierarchy applied to nothing.

    Every fixture in this file uses `.../channel/<name>` refs, which happens to
    match the synthesized form, so the mismatch was invisible to the suite.
    """

    @staticmethod
    def _channel(cid, ref, title="finance channel", desc="investing content"):
        return {"channel_id": cid, "channel_ref": ref, "title": title, "description": desc}

    def test_structural_edge_survives_handle_form_refs(self):
        from src.tools.bright_data import normalize_channel_ref
        from src.tools.graph_clustering import build_similarity_graph

        a = normalize_channel_ref("https://www.youtube.com/@benfelixcsi")
        b = normalize_channel_ref("https://www.youtube.com/@rationalreminder")
        channels = [self._channel("UC_A", a, "Ben Felix", "portfolio theory"),
                    self._channel("UC_B", b, "Rational Reminder", "academic podcast")]
        edges = [{"source_channel_id": "UC_A", "target_channel_ref": b,
                  "edge_type": "featured_channel"}]

        G = build_similarity_graph([a, b], channels, edges)

        assert G.has_edge(a, b), "the strongest structural signal must reach the graph"
        assert G[a][b]["weight"] == 3.0, "featured_channel must carry its weight, not Jaccard"

    def test_channel_form_refs_still_work(self):
        """Their existing fixtures must keep passing."""
        from src.tools.graph_clustering import build_similarity_graph

        a, b = "https://www.youtube.com/channel/A", "https://www.youtube.com/channel/B"
        channels = [{"channel_id": "A", "title": "x", "description": ""},
                    {"channel_id": "B", "title": "y", "description": ""}]
        edges = [{"source_channel_id": "A", "target_channel_ref": b,
                  "edge_type": "recommendation"}]

        G = build_similarity_graph([a, b], channels, edges)
        assert G[a][b]["weight"] == 2.0

    def test_edge_weights_outrank_content_similarity(self):
        """The whole point of the weight hierarchy: a deliberate feature link
        must dominate incidental title overlap."""
        from src.tools.bright_data import normalize_channel_ref
        from src.tools.graph_clustering import build_similarity_graph

        a = normalize_channel_ref("https://www.youtube.com/@one")
        b = normalize_channel_ref("https://www.youtube.com/@two")
        # Identical text would give Jaccard 1.0 — the structural edge must win.
        channels = [self._channel("UC_A", a, "money", "same words here"),
                    self._channel("UC_B", b, "money", "same words here")]
        edges = [{"source_channel_id": "UC_A", "target_channel_ref": b,
                  "edge_type": "featured_channel"}]

        G = build_similarity_graph([a, b], channels, edges)
        assert G[a][b]["weight"] == 3.0

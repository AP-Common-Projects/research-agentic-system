"""cluster_branch — deterministic community detection on a saturated node's
discovery graph.

Runs between check_saturation (saturated outcome) and compact_branch. Its job:
turn "does this branch have real internal structure" from an LLM guess into a
computed, reproducible number.

Algorithm per v2 plan §2:
1. Build weighted undirected graph from discovery_edges + content similarity.
2. Louvain community detection (seeded, for reproducibility).
3. Filter candidates by min size and distinctness.
4. Hands surviving candidates to compact_branch for LLM labelling and
   confirmation — not discovery.

This is a deterministic node. No LLM call. Cost = $0 per invocation.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import networkx as nx

from src.config import get_config
from src.nodes.store import get_store
from src.state import ErrorRecord, NodeLog
from src.tools.bright_data import normalize_channel_ref


# Edge-type weights: featured_channel is an owner's deliberate pick (strongest
# signal); recommendation is YouTube's algorithm; playlist is curated;
# comment_author is passive presence. Mirrors ADR-0006's signal-strength
# hierarchy.
_EDGE_WEIGHTS: dict[str, float] = {
    "featured_channel": 3.0,
    "recommendation": 2.0,
    "playlist": 2.0,
    "comment_author": 1.0,
    "collaboration": 1.0,
    "comment_mention": 1.0,
}


def _tokenize(text: str) -> set[str]:
    """Normalized token set for content-similarity comparison.

    Same lightweight approach dedup.py's check_near_duplicate uses for
    near-duplicate detection — zero new NLP dependency. Token-level Jaccard
    is more robust to word-order differences than character-level difflib.
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    stopwords = {
        "the", "and", "for", "you", "how", "with", "this", "that", "from",
        "your", "are", "was", "will", "has", "not", "its", "can", "but",
        "all", "just", "about", "also", "what", "when", "have", "been",
    }
    return {t for t in tokens if len(t) > 2 and t not in stopwords}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Token-set Jaccard similarity."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _id_to_ref(cid: str) -> str:
    """Convert a UC channel id to its canonical ref form."""
    if not cid:
        return ""
    return normalize_channel_ref(f"https://www.youtube.com/channel/{cid}")


def build_similarity_graph(
    refs: list[str],
    channels: list[dict[str, Any]],
    discovery_edges: list[dict[str, Any]],
    refs_by_id: dict[str, str] | None = None,
) -> nx.Graph:
    """Build weighted graph over the node's channel refs.

    Structural edges from discovery_edges carry type-based weights
    (featured_channel=3, recommendation=2, etc.). Content-similarity edges
    fill gaps for pairs with no discovery edge but overlapping video titles
    and descriptions — catches structure among small channels whose graph
    signal is sparsest (ADR-0006 measured featured_channels present on only
    1% of sub-100-subscriber channels).
    """
    G = nx.Graph()
    G.add_nodes_from(refs)

    # id -> normalized ref, for mapping source_channel_id to a graph node.
    #
    # Must use the channel's OWN ref, not a synthesized /channel/<id> URL.
    # `refs` comes from _kw_refs/_gw_refs, whose keys are handle-form
    # (`.../@benfelixcsi`) because that is what Bright Data's `url` field
    # returns. Synthesizing `.../channel/UC...` produces a string that is never
    # in `refs`, so every structural edge fails the membership test below and
    # is silently dropped — leaving Louvain to cluster on content similarity
    # alone and the edge-type weight hierarchy applying to nothing.
    #
    # `/channel/UC...` and `/@handle` are two identities for one channel and
    # string canonicalisation cannot collapse them; only the fetched record
    # links them, which is exactly what `channels` carries.
    # `refs_by_id` is the authoritative source: the discovery tracks record it
    # at the moment they resolve a channel. The `channels` rows cannot supply
    # it — the store has no ref column — so without the map every lookup below
    # falls back to the synthesized form and misses.
    id_to_nref: dict[str, str] = {
        cid: normalize_channel_ref(ref)
        for cid, ref in (refs_by_id or {}).items()
        if ref
    }
    for ch in channels:
        cid = ch.get("channel_id", "")
        if not cid or cid in id_to_nref:
            continue
        own_ref = ch.get("channel_ref") or ch.get("handle") or ""
        id_to_nref[cid] = normalize_channel_ref(own_ref) if own_ref else _id_to_ref(cid)

    # -- structural edges ---------------------------------------------------
    for edge in discovery_edges:
        target_ref = normalize_channel_ref(edge.get("target_channel_ref", ""))
        if not target_ref or target_ref not in refs:
            continue
        source_id = edge.get("source_channel_id", "")
        source_ref = id_to_nref.get(source_id, "")
        if not source_ref or source_ref not in refs:
            continue
        w = _EDGE_WEIGHTS.get(edge.get("edge_type", ""), 1.0)
        existing = G.get_edge_data(source_ref, target_ref, default={})
        G.add_edge(source_ref, target_ref, weight=max(w, existing.get("weight", 0.0)))

    # -- content-similarity edges (fallback) --------------------------------
    texts: dict[str, str] = {}
    for ch in channels:
        cid = ch.get("channel_id", "")
        nref = id_to_nref.get(cid, "")
        if nref and nref in refs:
            title = ch.get("title", "")
            desc = ch.get("description", "")
            texts[nref] = f"{title} {desc}"

    tokens: dict[str, set[str]] = {r: _tokenize(t) for r, t in texts.items()}
    ref_list = list(refs)
    for i in range(len(ref_list)):
        for j in range(i + 1, len(ref_list)):
            ri, rj = ref_list[i], ref_list[j]
            if G.has_edge(ri, rj):
                continue
            sim = _jaccard(tokens.get(ri, set()), tokens.get(rj, set()))
            if sim > 0.3:   # configurable, but threshold is simple enough to
                G.add_edge(ri, rj, weight=sim)
    return G


def detect_communities(
    G: nx.Graph, seed: int
) -> list[set[str]]:
    """Seeded Louvain community detection.

    The seed is non-negotiable: without it, a resumed run recomputes different
    clusters than the original run found — silent nondeterminism that breaks
    reproducibility and the eval harness's regression fixtures.
    """
    # Louvain does not handle isolates well (each isolate is a community of 1,
    # which is noise). Filter them; they'll be caught by min_channels_for_split
    # anyway.
    if G.number_of_edges() == 0:
        return []
    communities = nx.community.louvain_communities(
        G, seed=seed, weight="weight"
    )
    return [set(c) for c in communities if len(c) >= 1]


def score_distinctness(
    G: nx.Graph, community: set[str]
) -> float:
    """Average intra-community edge weight / average inter-community weight.

    A community meaningfully more connected internally than to the rest of
    the graph has a ratio > 1.0. Ratio near 1.0 means the community is
    algorithmic noise — no real structure to justify a split.
    """
    rest = set(G.nodes()) - community
    if not rest:
        return math.inf  # entire graph is one community

    intra_weights: list[float] = []
    inter_weights: list[float] = []

    for u in community:
        for v in community:
            if u == v:
                continue
            d = G.get_edge_data(u, v)
            if d:
                intra_weights.append(d.get("weight", 0.0))
        for w in rest:
            d = G.get_edge_data(u, w)
            if d:
                inter_weights.append(d.get("weight", 0.0))

    # No internal cohesion at all: this is a bag of unconnected nodes, not a
    # community, however isolated it looks from the outside.
    if not intra_weights:
        return 0.0

    avg_intra = sum(intra_weights) / len(intra_weights)

    # Genuinely disconnected from the rest of the graph. That IS maximal
    # distinctness and should be said so — the previous code divided by an
    # 0.01 sentinel and returned ~110, an arbitrary number that looks like a
    # measurement and passes any threshold. Measured live, EVERY retained
    # candidate scored exactly 110.0, which meant min_cluster_distinctness was
    # filtering nothing and ADR-0007's "evidence-gated" depth was ungated.
    #
    # Returning infinity keeps the gate honest: a disconnected component of
    # sufficient size genuinely earns a split, and the real filter for
    # everything else is the ratio below.
    if not inter_weights:
        return math.inf

    avg_inter = sum(inter_weights) / len(inter_weights)
    return avg_intra / avg_inter if avg_inter > 0 else 0.0


async def cluster_branch(state: dict) -> dict:
    """Deterministic community detection for the active saturated node.

    Returns cluster_candidates for compact_branch to judge, or empty list
    if the branch is coherent (true leaf).
    """
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    cfg = get_config().harness
    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")

    def _log(input_summary: dict) -> list[dict]:
        return [
            NodeLog(
                node_name="cluster_branch",
                thread_id=thread_id,
                input_summary=input_summary,
                latency_ms=(time.monotonic() - start) * 1000,
                cost_usd=0.0,
            ).model_dump()
        ]

    if not active_node_id or active_node_id not in tree:
        return {
            "node_logs": _log({"decision": "no_active_node", "node_id": active_node_id}),
        }

    node = tree[active_node_id]
    node_id = node.get("id", "")

    # Resolve and normalize the channel refs belonging to this node.
    kw_refs = dict(node.get("_kw_refs") or {})
    gw_refs = dict(node.get("_gw_refs") or {})
    raw_refs = set(kw_refs) | set(gw_refs)
    # Canonicalise: _kw_refs / _gw_refs may use Bright Data's raw URL form
    # while discovery_edges use normalize_channel_ref form. Both sides of the
    # graph must use the same canonical representation.
    normalized = {normalize_channel_ref(r) for r in raw_refs}
    normalized.discard("")
    ref_list = sorted(normalized)

    if len(ref_list) < cfg.min_channels_for_split:
        max_refs = max(len(kw_refs), len(gw_refs), 0)
        return {
            "node_logs": _log({
                "decision": "insufficient_channels",
                "node_id": node_id,
                "channel_count": len(ref_list),
                "min_needed": cfg.min_channels_for_split,
            }),
            "cluster_branch_done": True,
        }

    # Load channel data and discovery edges for this node's channels.
    store = get_store()
    kw_ch_ids = set(node.get("_kw_channel_ids") or [])
    gw_ch_ids = set(node.get("_gw_channel_ids") or [])
    all_ch_ids = sorted(kw_ch_ids | gw_ch_ids)

    refs_by_id = state.get("channel_refs_by_id", {})
    channels = await store.get_channels_for_node(node_id, all_ch_ids)
    discovery_edges = await store.get_discovery_edges_for_channels(ref_list)

    # Build the weighted graph.
    G = build_similarity_graph(
        ref_list, channels, discovery_edges,
        refs_by_id=state.get("channel_refs_by_id", {}),
    )

    # Community detection.
    communities = detect_communities(G, seed=cfg.cluster_seed)

    # Filter candidates.
    candidates: list[dict[str, Any]] = []
    best_distinctness: float | None = None
    for comm in communities:
        if len(comm) < cfg.min_channels_for_split:
            continue
        distinctness = score_distinctness(G, comm)
        if distinctness < cfg.min_cluster_distinctness:
            continue
        # Resolve member channel ids for this community.
        member_refs = set(comm)
        member_ids: list[str] = []
        for ch in channels:
            # Normalize before comparing. `member_refs` holds canonical refs
            # from the graph; `channel_ref` is the raw stored value. Comparing
            # them directly is the same identity mismatch that dropped the
            # structural edges above, and would silently yield empty member
            # lists — so a split would be proposed with no channels in it.
            cid_ = ch.get("channel_id", "")
            raw = refs_by_id.get(cid_) or ch.get("channel_ref") or ch.get("handle") or ""
            ref = normalize_channel_ref(raw) if raw else _id_to_ref(cid_)
            if ref and ref in member_refs:
                ch_id = ch.get("channel_id", "")
                if ch_id:
                    member_ids.append(ch_id)
        # math.inf does not survive JSON or JSONB. A disconnected community
        # is recorded as such rather than as a fabricated large number.
        disconnected = math.isinf(distinctness)
        candidates.append({
            "member_refs": sorted(member_refs),
            "member_channel_ids": member_ids,
            "size": len(comm),
            "distinctness_score": None if disconnected else round(distinctness, 4),
            "disconnected": disconnected,
        })
        if not disconnected and (
            best_distinctness is None or distinctness > best_distinctness
        ):
            best_distinctness = round(distinctness, 4)

    tree_update: dict[str, Any] = {
        "cluster_candidates": candidates,
        "cluster_distinctness_score": best_distinctness,
    }

    return {
        "tree": {active_node_id: tree_update},
        "cluster_branch_done": True,
        "node_logs": _log({
            "decision": "split" if candidates else "leaf",
            "node_id": node_id,
            "channel_count": len(ref_list),
            "graph_nodes": G.number_of_nodes(),
            "graph_edges": G.number_of_edges(),
            "communities_found": len(communities),
            "candidates_retained": len(candidates),
            "best_distinctness": best_distinctness,
        }),
    }
"""Frontier-based graph walk — the existential component.

Each round expands ONLY the frontier (unexpanded_channel_ids), never the
cumulative discovered_channel_ids set. This fixes Bug 2 — cumulative re-scan
collapsing novelty signal.

Algorithm from plan §8.5.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.tools.bright_data import BrightDataClient


async def graph_walk(state: dict) -> dict:
    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    if not active_node_id:
        return {}

    node = tree.get(active_node_id)
    if node is None:
        return {}

    frontier = list(node.get("unexpanded_channel_ids") or [])
    if not frontier:
        frontier = list(node.get("seed_channel_ids") or [])
    if not frontier:
        return {"graph_walk_done": True}

    client = BrightDataClient()
    edges = client.crawl_channel_relationships(frontier)

    frontier_set = set(frontier)
    found_ids: set[str] = set()
    for edge in edges:
        target = edge.get("target_channel_id", "")
        if target and target not in frontier_set:
            found_ids.add(target)

    discovered_set = set(state.get("discovered_channel_ids", []))
    truly_new = found_ids - discovered_set

    novelty = len(truly_new) / len(frontier) if frontier else 0.0

    new_visited = set(state.get("visited_channel_ids", set())) | frontier_set
    new_expanded = set(state.get("expanded_channel_ids", set())) | frontier_set

    updated_node = dict(node)
    updated_node["unexpanded_channel_ids"] = list(truly_new)
    updated_node["_last_novelty"] = novelty
    updated_node["_last_expanded_at"] = datetime.now(timezone.utc).isoformat()

    return {
        "discovered_channel_ids": list(truly_new),
        "visited_channel_ids": new_visited,
        "expanded_channel_ids": new_expanded,
        "novelty_rates": [round(novelty, 4)],
        "tree": {active_node_id: updated_node},
        "graph_walk_done": True,
    }
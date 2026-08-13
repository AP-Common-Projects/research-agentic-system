"""Frontier-based graph walk — the existential component.

Each round expands ONLY the frontier (unexpanded channels minus already-expanded),
never the cumulative discovered set. This fixes Bug 2 — cumulative re-scan
collapsing the novelty signal.

Writes only its OWN fields to the tree node (unexpanded_channel_ids, graph
novelty history, exhaustion flag) so the parallel keyword_search write isn't
clobbered — see the deep-merge reducer in state.py.

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

    expanded = set(state.get("expanded_channel_ids", set()))
    seeds = list(node.get("seed_channel_ids") or [])
    unexpanded = list(node.get("unexpanded_channel_ids") or [])

    frontier: list[str] = []
    for c in unexpanded + seeds:
        if c not in expanded and c not in frontier:
            frontier.append(c)

    gw_history = list(node.get("_gw_novelty_history", []))

    if not frontier:
        gw_history.append(0.0)
        return {
            "tree": {
                active_node_id: {
                    "_gw_novelty_history": gw_history,
                    "_gw_exhausted": True,
                }
            },
            "graph_walk_done": True,
        }

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

    gw_history.append(round(novelty, 4))

    return {
        "discovered_channel_ids": list(truly_new),
        "visited_channel_ids": new_visited,
        "expanded_channel_ids": new_expanded,
        "novelty_rates": [round(novelty, 4)],
        "tree": {
            active_node_id: {
                "unexpanded_channel_ids": list(truly_new),
                "_gw_novelty_history": gw_history,
                "_gw_exhausted": False,
                "_last_expanded_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        "graph_walk_done": True,
    }

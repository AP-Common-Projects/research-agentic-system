"""niche_queue.py — shared routing helpers for sequential multi-niche traversal.

Plan §7.2: used by route_after_select and route_after_compaction to check
whether there are more niches to process in the cluster, and to produce the
state delta that advances to the next one.
"""


def has_more_niches(state: dict) -> bool:
    niches = state.get("selected_niches") or []
    return state.get("niche_index", 0) + 1 < len(niches)


def advance_to_next_niche(state: dict) -> dict:
    """State delta for moving to the next niche in the cluster queue.

    Resets per-niche traversal state (tree, active_node_id) for a clean
    build_taxonomy pass on the next niche. Deliberately does NOT touch
    expanded_channel_refs, visited_channel_ids, hydrated_channel_ids,
    discovered_channel_ids, channel_refs_by_id, or any spend/budget field --
    those are the cross-niche dedup and cost-governor state this whole
    mechanism exists to keep shared across the cluster.
    """
    niches = state.get("selected_niches") or []
    next_index = state.get("niche_index", 0) + 1
    if next_index >= len(niches):
        return {"next_action": "all_done"}
    return {
        "niche_index": next_index,
        "selected_niche": niches[next_index],
        "tree": {},
        "active_node_id": None,
        "next_action": "next_niche",
    }
"""niche_queue — shared multi-niche routing logic (v4, plan §7.2).

Two exit points treat "this niche's tree is exhausted" as "the run is done."
Both need the identical check — are there more niches in the cluster queue —
so it's built once and shared, not duplicated into two functions.
"""

from __future__ import annotations


def has_more_niches(state: dict) -> bool:
    niches = state.get("selected_niches") or []
    return state.get("niche_index", 0) + 1 < len(niches)


def advance_to_next_niche(state: dict) -> dict:
    next_index = state.get("niche_index", 0) + 1
    niches = state.get("selected_niches") or []
    next_niche = niches[next_index] if next_index < len(niches) else ""
    return {
        "niche_index": next_index,
        "selected_niche": next_niche,
        "tree": {},
        "active_node_id": None,
    }
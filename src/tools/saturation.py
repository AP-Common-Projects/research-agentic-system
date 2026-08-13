"""check_saturation — the real stop condition.

Three-way routing:
- "expand_deeper" — novelty above threshold, continue
- "saturated" — node has exhausted both discovery tracks
- "budget_exhausted" — budget_spent >= budget_limit (circuit breaker)

Saturation is per-node, per-track. A node saturates when BOTH the keyword and
graph-walk tracks have been below the novelty threshold for the consecutive
window, or when both tracks report exhaustion (empty frontier / no new
queries). Budget is a circuit breaker ONLY — a run hitting the ceiling
regularly means frontier logic is broken.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.config import get_config


def check_saturation(state: dict) -> dict:
    cfg = get_config()
    threshold = cfg.harness.saturation_novelty_threshold
    window = cfg.harness.saturation_consecutive_window
    budget_limit = cfg.harness.budget_limit_usd
    budget_spent = state.get("budget_spent_usd", 0.0)

    if budget_spent >= budget_limit:
        return _budget_exhausted(state)

    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    node = tree.get(active_node_id) if active_node_id else None

    if node is None:
        return {"next_action": "saturated"}

    kw_history = list(node.get("_kw_novelty_history", []))
    gw_history = list(node.get("_gw_novelty_history", []))
    kw_exhausted = bool(node.get("_kw_exhausted", False))
    gw_exhausted = bool(node.get("_gw_exhausted", False))

    if kw_exhausted and gw_exhausted:
        return _mark_saturated(state)

    if len(kw_history) >= window and len(gw_history) >= window:
        kw_low = all(r < threshold for r in kw_history[-window:])
        gw_low = all(r < threshold for r in gw_history[-window:])
        if kw_low and gw_low:
            return _mark_saturated(state)

    return {"next_action": "expand_deeper"}


def _mark_saturated(state: dict) -> dict:
    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    now = datetime.now(timezone.utc).isoformat()

    tree_update: dict[str, dict] = {}
    if active_node_id and active_node_id in tree:
        tree_update[active_node_id] = {
            "status": "saturated",
            "saturated_at": now,
        }

    saturated_branches = list(state.get("saturated_branches", []))
    if active_node_id and active_node_id not in saturated_branches:
        saturated_branches.append(active_node_id)

    return {
        "next_action": "saturated",
        "tree": tree_update,
        "saturated_branches": saturated_branches,
    }


def _budget_exhausted(state: dict) -> dict:
    tree = state.get("tree", {})
    now = datetime.now(timezone.utc).isoformat()
    tree_update: dict[str, dict] = {}

    for node_id, node in tree.items():
        if node.get("status") in ("active", "pending"):
            tree_update[node_id] = {
                "status": "saturated",
                "saturated_at": now,
            }

    return {
        "next_action": "budget_exhausted",
        "tree": tree_update,
        "saturated_branches": [nid for nid in tree_update],
    }

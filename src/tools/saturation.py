"""check_saturation — the real stop condition.

Three-way routing:
- "expand_deeper" — novelty above threshold, continue
- "saturated" — novelty below threshold for consecutive_window rounds
- "budget_exhausted" — budget_spent >= budget_limit (circuit breaker)

Budget is a circuit breaker ONLY. A run hitting the ceiling regularly
means frontier logic is broken.
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

    novelty_rates = state.get("novelty_rates", [])
    if len(novelty_rates) < window:
        return {"next_action": "expand_deeper"}

    recent = novelty_rates[-window:]
    if all(r < threshold for r in recent):
        return _mark_saturated(state)

    return {"next_action": "expand_deeper"}


def _mark_saturated(state: dict) -> dict:
    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    now = datetime.now(timezone.utc).isoformat()

    tree_update: dict[str, dict] = {}
    if active_node_id and active_node_id in tree:
        node = dict(tree[active_node_id])
        node["status"] = "saturated"
        node["saturated_at"] = now
        tree_update[active_node_id] = node

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
            updated = dict(node)
            updated["status"] = "saturated"
            updated["saturated_at"] = now
            tree_update[node_id] = updated

    return {
        "next_action": "budget_exhausted",
        "tree": tree_update,
        "saturated_branches": [nid for nid in tree_update],
    }
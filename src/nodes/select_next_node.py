"""select_next_node — deterministic node selection.

Picks the next pending tree node. No LLM call — pure Python, fully testable.
Priority: (1) proposed_new_nodes from compaction, (2) BFS order by depth ascending.
"""

from __future__ import annotations

import time

from src.state import NodeLog


def select_next_node(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    tree: dict[str, dict] = state.get("tree", {})
    next_action = state.get("next_action", "")
    active_node_id = state.get("active_node_id")

    def _log(input_summary: dict) -> list[dict]:
        return [
            NodeLog(
                node_name="select_next_node",
                thread_id=thread_id,
                input_summary=input_summary,
                latency_ms=(time.monotonic() - start) * 1000,
                cost_usd=0.0,
            ).model_dump()
        ]

    if (
        next_action == "expand_deeper"
        and active_node_id
        and active_node_id in tree
        and tree[active_node_id].get("status") == "active"
    ):
        return {
            "active_node_id": active_node_id,
            "node_logs": _log({"decision": "continue_active", "node_id": active_node_id}),
        }

    for node_id, node in tree.items():
        proposed = node.get("proposed_new_nodes", [])
        if proposed:
            for p in proposed:
                proposed_id = p.get("label", proposed_id_hint(p)).lower().replace(" ", "_")
                if proposed_id not in tree:
                    new_node = {
                        "id": proposed_id,
                        "label": p.get("label", ""),
                        "depth": node.get("depth", 0) + 1,
                        "parent_id": node_id,
                        "children_ids": [],
                        "keywords": [],
                        "seed_channel_ids": p.get("seed_channel_ids", []),
                        "unexpanded_channel_ids": [],
                        "status": "active",
                        "compaction_summary": None,
                        "proposed_new_nodes": [],
                        "schema_version": node.get("schema_version", 1),
                    }
                    orig_parent = dict(tree.get(node_id, {}))
                    orig_parent.setdefault("children_ids", [])
                    if proposed_id not in orig_parent["children_ids"]:
                        orig_parent["children_ids"] = list(orig_parent.get("children_ids", [])) + [proposed_id]
                    tree_updates = {node_id: orig_parent, proposed_id: new_node}
                    return {
                        "tree": tree_updates,
                        "active_node_id": proposed_id,
                        "node_logs": _log({"decision": "proposed_node", "node_id": proposed_id, "parent_id": node_id}),
                    }

    pending = [
        (node_id, node)
        for node_id, node in tree.items()
        if node.get("status") == "pending"
    ]
    pending.sort(key=lambda item: (item[1].get("depth", 0), item[0]))

    if pending:
        next_id = pending[0][0]
        updated = dict(tree[next_id])
        updated["status"] = "active"
        return {
            "tree": {next_id: updated},
            "active_node_id": next_id,
            "node_logs": _log({"decision": "next_pending", "node_id": next_id, "pending_count": len(pending)}),
        }

    return {"next_action": "all_done", "node_logs": _log({"decision": "all_done"})}


def proposed_id_hint(proposed: dict) -> str:
    return proposed.get("label", "proposed")
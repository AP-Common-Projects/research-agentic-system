"""select_next_node — deterministic node selection.

Picks the next pending tree node. No LLM call — pure Python, fully testable.
Priority: (1) proposed_new_nodes from compaction, (2) BFS order by depth ascending.
"""

from __future__ import annotations

import time

from src.config import get_config
from src.state import NodeLog
from src.tools.budget import priority_score


def select_next_node(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    cfg = get_config().harness
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

    # Governors on tree growth. compact_branch can propose new nodes every
    # time it runs, and each new node starts a fresh discovery loop of its own —
    # so without a depth and a total-branch ceiling the tree, and the spend,
    # grow without bound. 0 means uncapped (the `full` profile).
    for node_id, node in tree.items():
        proposed = node.get("proposed_new_nodes", [])
        if proposed:
            for p in proposed:
                proposed_id = p.get("label", proposed_id_hint(p)).lower().replace(" ", "_")
                child_depth = node.get("depth", 0) + 1
                if cfg.max_tree_depth > 0 and child_depth > cfg.max_tree_depth:
                    continue
                # Count BRANCHES, not tree size. `_enforce_branch_cap` trims
                # the taxonomy to max_branches branches, which is 1 +
                # max_branches nodes once the root is included — so comparing
                # len(tree) against max_branches was always already true, and
                # every proposed split was rejected at every setting. v2's
                # adaptive depth could not create a single node.
                branch_count = sum(
                    1 for n in tree.values() if n.get("depth", 0) != 0
                )
                if cfg.max_branches > 0 and branch_count >= cfg.max_branches:
                    continue
                if proposed_id not in tree:
                    child_depth = node.get("depth", 0) + 1
                    new_node = {
                        "id": proposed_id,
                        "label": p.get("label", ""),
                        "depth": child_depth,
                        "parent_id": node_id,
                        "children_ids": [],
                        "keywords": [],
                        "seed_channel_ids": p.get("seed_channel_ids", []),
                        "unexpanded_channel_ids": [],
                        "status": "active",
                        "compaction_summary": None,
                        "proposed_new_nodes": [],
                        "schema_version": node.get("schema_version", 1),
                        # v2: a child created from a graph-confirmed split
                        # inherits the parent's split quality (feeds the
                        # priority sort). Depth-1 children root their own
                        # budget lineage; deeper ones inherit the parent's.
                        "split_method": node.get("split_method", "llm_seed"),
                        "cluster_distinctness_score": node.get("cluster_distinctness_score"),
                        "lineage_root_id": (
                            proposed_id
                            if child_depth == 1
                            else (node.get("lineage_root_id") or node_id)
                        ),
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
    # Priority, not pure BFS (v2): under a real budget ceiling, exhausting
    # every shallow branch before any rich branch can go deep spends the run
    # on mediocre siblings. Richer, more distinctly-split lineages get
    # explored first. Fully deterministic, no LLM, testable.
    pending.sort(key=lambda item: _priority_score(item[1], tree), reverse=True)

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


def _priority_score(node: dict, tree: dict[str, dict]) -> float:
    return priority_score(node, tree, get_config().harness)


def proposed_id_hint(proposed: dict) -> str:
    return proposed.get("label", "proposed")
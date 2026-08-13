"""build_taxonomy — L0 taxonomy designer node.

Frontier-tier LLM call scoped to niche name + scanner evidence only.
Produces the initial taxonomy tree with root + first-pass branches.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from src.config import get_config
from src.llm.cascade import complete_tier, estimate_cost
from src.state import NodeLog, TreeNode, ErrorRecord

SYSTEM_PROMPT = """You are a taxonomy designer for YouTube niche research. Your job is to produce a JSON taxonomy tree for a given niche.

Rules:
1. One root node representing the niche itself.
2. AT MOST {MAX_BRANCHES} first-pass branch nodes under the root, each representing a distinct sub-niche, content format, or audience segment. Order them most promising first — if the budget only allows a few, the earlier ones are the ones that will be researched.
3. Every node must have: id (string), label (string), keywords (list of 3-6 search keyword strings), seed_channels (list of 2-4 plausible YouTube channel handle or URL strings to seed graph walk).
4. The root node has depth 0; branches have depth 1.
5. Respond ONLY with the JSON object — no preamble, no markdown fences, no explanation.

Strict JSON schema:
{
  "nodes": [
    {
      "id": "root",
      "label": "<niche name>",
      "depth": 0,
      "parent_id": null,
      "keywords": ["keyword1", "keyword2", ...],
      "seed_channels": ["@handle1", "@handle2", ...]
    },
    {
      "id": "<branch-id>",
      "label": "<branch label>",
      "depth": 1,
      "parent_id": "root",
      "keywords": ["keyword1", "keyword2", ...],
      "seed_channels": ["@handle1", "@handle2", ...]
    }
  ]
}"""


def _enforce_branch_cap(tree: dict[str, dict], max_branches: int) -> dict[str, dict]:
    """Trim the taxonomy to the branch budget, keeping the root.

    `max_branches` in select_next_node only ever governed nodes *proposed by
    compaction* — the initial taxonomy was ungoverned, so a model asked for
    "3-6 branches" could hand back six independent discovery loops. Each branch
    runs its own rounds of both tracks, so branch count multiplies record spend
    directly: in a replay run the record budget tripped on branch 2 of 7, having
    spent it fanning out rather than searching any branch properly.

    The prompt asks for at most this many and to order them best-first, but the
    cap is enforced here regardless — an instruction the model can ignore is not
    a budget control. Order is the model's stated priority; ties break on id so
    the trim is deterministic across runs.
    """
    if max_branches <= 0:
        return tree

    roots = {nid: n for nid, n in tree.items() if n.get("depth", 0) == 0}
    branches = [(nid, n) for nid, n in tree.items() if n.get("depth", 0) != 0]
    if len(branches) <= max_branches:
        return tree

    kept = dict(roots)
    for nid, node in branches[:max_branches]:
        kept[nid] = node

    kept_ids = set(kept)
    for node in kept.values():
        children = node.get("children_ids") or []
        node["children_ids"] = [c for c in children if c in kept_ids]

    return kept


def _parse_tree_json(raw: str, niche_name: str) -> dict[str, dict]:
    cleaned = raw.strip()
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        cleaned = match.group(0)
    parsed = json.loads(cleaned)
    nodes_list = parsed.get("nodes", [])
    if not nodes_list:
        raise ValueError("No nodes in LLM response")
    tree: dict[str, dict] = {}
    for item in nodes_list:
        node_id = str(item.get("id", "")).strip()
        if not node_id:
            raise ValueError("Node missing id field")
        node = TreeNode(
            id=node_id,
            label=str(item.get("label", node_id)),
            depth=int(item.get("depth", 0)),
            parent_id=str(item.get("parent_id", "")) if item.get("parent_id") else None,
            keywords=[str(k) for k in item.get("keywords", [])],
            seed_channel_ids=[str(c) for c in item.get("seed_channels", [])],
            status="pending",
        )
        tree[node_id] = node.model_dump()
    return tree


async def build_taxonomy(state: dict) -> dict:
    niche_name = state.get("selected_niche", "unknown")
    scanner_evidence = state.get("niche_scanner_evidence", {})
    thread_id = state.get("thread_id", "")
    max_branches = get_config().harness.max_branches
    # str.replace, not str.format — the prompt embeds a literal JSON schema
    # and format() parses its braces as fields.
    system_prompt = SYSTEM_PROMPT.replace("{MAX_BRANCHES}", str(max_branches or 6))

    evidence_str = json.dumps(scanner_evidence, indent=2) if scanner_evidence else "No scanner evidence available."
    prompt = (
        f"Niche: {niche_name}\n\n"
        f"Scanner evidence: {evidence_str}\n\n"
        f"Build a taxonomy tree for this niche. Respond with ONLY the JSON object per the schema."
    )

    errors: list[dict] = []
    tree: dict[str, dict] = {}

    for attempt in range(2):
        try:
            start = time.monotonic()
            result = complete_tier("frontier", prompt, system_prompt)
            latency_ms = (time.monotonic() - start) * 1000
            content = result.get("content", "")

            tree = _enforce_branch_cap(
                _parse_tree_json(content, niche_name), max_branches
            )

            root_id = None
            for nid, node in tree.items():
                if node["depth"] == 0:
                    root_id = nid
                    break
            if root_id is None:
                raise ValueError("No root node (depth=0) found in tree")

            usage = result.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            cost = result.get("cost_usd", estimate_cost("frontier", prompt_tokens, completion_tokens))

            node_log = NodeLog(
                node_name="build_taxonomy",
                thread_id=thread_id,
                input_summary={"niche": niche_name, "attempt": attempt},
                llm_output=content[:500],
                latency_ms=latency_ms,
                cost_usd=cost,
            )

            return {
                "tree": tree,
                "active_node_id": root_id,
                "node_logs": [node_log.model_dump()],
                "errors": errors,
                "budget_spent_usd": cost,
            }

        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            if attempt == 0:
                prefix = "Your previous response was invalid JSON or missing required fields. "
                if "Expecting" in str(exc) or "JSON" in str(type(exc).__name__):
                    prefix += f"Parse error: {exc}. "
                prefix += "Respond with ONLY valid JSON per the schema. No markdown, no explanation."
                prompt = prefix + "\n\n" + prompt
                continue
            errors.append(
                ErrorRecord(
                    node_name="build_taxonomy",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                ).model_dump()
            )
            return {
                "errors": errors,
                "tree": {},
                "active_node_id": None,
            }

    return {"errors": errors, "tree": {}, "active_node_id": None}
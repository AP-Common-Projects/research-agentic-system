"""compact_branch — branch compaction + new-node proposal.

Mid-tier LLM call that produces a narrative summary for one branch's
discovered channels and computed signals. May propose new off-taxonomy nodes.
Numbers in the prompt come from the structured store, never invented.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from src.llm.cascade import complete_tier, estimate_cost
from src.nodes.store import get_store
from src.state import BranchCompaction, NodeLog, ErrorRecord, ProposedNode

SYSTEM_PROMPT = """You are a YouTube niche research analyst. Given a branch's channel and video data with computed signals, produce a structured compaction.

Rules:
1. Write a 2-4 paragraph narrative summary identifying patterns, content formats, engagement levels, and outlier patterns you observe in the data.
2. Extract 2-5 key patterns as structured items, each with: pattern_name, description, evidence_summary.
3. NEVER invent numbers not present in the provided data. If a number is not in the data, do not state it.
4. If you identify a coherent cluster of channels that does NOT fit under the current taxonomy node label, propose a new tree node with: label, rationale, and the specific channel_ids that belong to it.
5. If no off-taxonomy cluster exists, set proposed_nodes to empty list.

Respond with ONLY a JSON object with these exact keys:
{
  "narrative_summary": "<2-4 paragraph narrative>",
  "key_patterns": [
    {"pattern_name": "...", "description": "...", "evidence_summary": "..."}
  ],
  "proposed_nodes": [
    {
      "label": "<new node label>",
      "rationale": "<why this cluster doesn't fit current node>",
      "seed_channel_ids": ["<channel_id>", ...]
    }
  ]
}"""


def _build_prompt(node: dict, channels: list[dict], videos: list[dict]) -> str:
    lines = [
        f"Current taxonomy node: id={node['id']}, label={node['label']}, keywords={node.get('keywords', [])}",
        f"Depth: {node.get('depth')}, Schema version: {node.get('schema_version', 1)}",
        "",
        "--- CHANNELS ---",
    ]
    for ch in channels:
        lines.append(
            f"  {ch['channel_id']}: title={ch.get('title','')}, subs={ch.get('subscriber_count',0)}, "
            f"first_seen={ch.get('first_seen_at','')}, discovery={ch.get('discovery_method','')}"
        )
    lines.append("")
    lines.append("--- VIDEOS ---")
    for v in videos:
        lines.append(
            f"  {v['video_id']}: channel={v['channel_id']}, title={v.get('title','')}, "
            f"views={v.get('view_count',0)}, likes={v.get('like_count',0)}, "
            f"comments={v.get('comment_count',0)}, published={v.get('published_at','')}, "
            f"outlier_score={v.get('outlier_score',0)}"
        )
    lines.append("")
    lines.append(f"Total channels: {len(channels)}, Total videos: {len(videos)}")
    return "\n".join(lines)


def _parse_compaction_json(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


async def compact_branch(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    errors: list[dict] = []

    if not active_node_id or active_node_id not in tree:
        return {
            "errors": [
                ErrorRecord(
                    node_name="compact_branch",
                    error_type="ValueError",
                    message=f"Invalid active_node_id: {active_node_id}",
                ).model_dump()
            ]
        }

    node = tree[active_node_id]
    node_id = node["id"]
    node_label = node.get("label", "")
    channel_ids = list(
        set(node.get("seed_channel_ids", []) + node.get("unexpanded_channel_ids", []))
    )

    store = get_store()
    channels = await store.get_channels_for_node(node_id, channel_ids)
    channel_ids_found = [ch["channel_id"] for ch in channels]
    videos = await store.get_videos_for_channels(channel_ids_found)

    prompt = _build_prompt(node, channels, videos)

    for attempt in range(2):
        try:
            start = time.monotonic()
            result = complete_tier("mid", prompt, SYSTEM_PROMPT)
            latency_ms = (time.monotonic() - start) * 1000
            content = result.get("content", "")

            parsed = _parse_compaction_json(content)

            narrative_summary = str(parsed.get("narrative_summary", ""))
            key_patterns = parsed.get("key_patterns", [])
            if not isinstance(key_patterns, list):
                key_patterns = []

            proposed_raw = parsed.get("proposed_nodes", [])
            if not isinstance(proposed_raw, list):
                proposed_raw = []

            proposed_nodes: list[ProposedNode] = []
            for pn in proposed_raw:
                proposed = ProposedNode(
                    label=str(pn.get("label", "")),
                    rationale=str(pn.get("rationale", "")),
                    seed_channel_ids=[str(c) for c in pn.get("seed_channel_ids", [])],
                    parent_id=node_id,
                )
                if proposed.label and proposed.seed_channel_ids:
                    proposed_nodes.append(proposed)

            compaction = BranchCompaction(
                node_id=node_id,
                node_label=node_label,
                channel_count=len(channels),
                video_count=len(videos),
                top_channels=[
                    {"channel_id": ch["channel_id"], "title": ch.get("title", ""), "subscriber_count": ch.get("subscriber_count", 0)}
                    for ch in channels[:10]
                ],
                narrative_summary=narrative_summary,
                key_patterns=key_patterns,
                proposed_nodes=proposed_nodes,
            )

            usage = result.get("usage", {})
            cost = result.get("cost_usd", estimate_cost("mid", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)))

            node_log = NodeLog(
                node_name="compact_branch",
                thread_id=thread_id,
                input_summary={
                    "node_id": node_id,
                    "channel_count": len(channels),
                    "video_count": len(videos),
                    "attempt": attempt,
                },
                llm_output=content[:500],
                latency_ms=latency_ms,
                cost_usd=cost,
            )

            updated_node = dict(node)
            updated_node["status"] = "compacted"
            updated_node["compaction_summary"] = narrative_summary
            updated_node["proposed_new_nodes"] = [p.model_dump() for p in proposed_nodes]

            return {
                "tree": {node_id: updated_node},
                "branch_compactions": [compaction.model_dump()],
                "node_logs": [node_log.model_dump()],
                "errors": errors,
                "budget_spent_usd": cost,
            }

        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            if attempt == 0:
                prefix = f"Your previous response was invalid JSON. Parse error: {exc}. Respond with ONLY valid JSON per the schema."
                prompt = prefix + "\n\n" + prompt
                continue
            errors.append(
                ErrorRecord(
                    node_name="compact_branch",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                ).model_dump()
            )
            return {"errors": errors}

    return {"errors": errors}
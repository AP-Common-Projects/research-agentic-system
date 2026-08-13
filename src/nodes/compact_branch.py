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

from src.config import get_config
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


def _build_prompt(
    node: dict,
    channels: list[dict],
    videos: list[dict],
    max_channels: int = 0,
    max_videos: int = 0,
) -> str:
    """Serialise a branch for the compaction prompt.

    Capped, because this writes one line per channel and one per video straight
    into the request — an uncapped branch with a few hundred channels and their
    videos produces a six-figure-token prompt. Rows are taken highest-signal
    first (channels by subscribers, videos by outlier score) so the truncation
    keeps what the analysis is actually about, and the true totals are still
    stated at the end so the model is never misled about the sample size.
    """
    total_channels, total_videos = len(channels), len(videos)
    if max_channels > 0 and len(channels) > max_channels:
        channels = sorted(
            channels, key=lambda c: c.get("subscriber_count", 0) or 0, reverse=True
        )[:max_channels]
    if max_videos > 0 and len(videos) > max_videos:
        videos = sorted(
            videos, key=lambda v: v.get("outlier_score", 0) or 0, reverse=True
        )[:max_videos]

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
    if total_channels != len(channels) or total_videos != len(videos):
        lines.append(
            f"Showing {len(channels)} of {total_channels} channels and "
            f"{len(videos)} of {total_videos} videos "
            f"(highest subscribers / outlier scores first)."
        )
    lines.append(f"Total channels: {total_channels}, Total videos: {total_videos}")
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

    # The channels this branch actually resolved, recorded by each discovery
    # track as it ran. `seed_channel_ids` cannot be used for this: taxonomy
    # seeds are `@handle` strings, not UC ids, so looking the store up by them
    # matches nothing — which is how a run could hydrate 22 channels and 646
    # videos and still compact an empty branch.
    channel_ids = sorted(
        set(node.get("_kw_channel_ids") or []) | set(node.get("_gw_channel_ids") or [])
    )

    store = get_store()
    channels = await store.get_channels_for_node(node_id, channel_ids)
    channel_ids_found = [ch["channel_id"] for ch in channels]
    videos = await store.get_videos_for_channels(channel_ids_found)

    cfg = get_config().harness
    prompt = _build_prompt(
        node, channels, videos,
        max_channels=cfg.max_prompt_channels,
        max_videos=cfg.max_prompt_videos,
    )

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
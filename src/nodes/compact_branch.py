"""compact_branch — branch compaction + cluster confirmation.

Mid-tier LLM call that produces a narrative summary for one branch's
discovered channels and computed signals. In v2, cluster_branch runs first
and hands structured candidates to this node; the LLM's role is to judge
whether the graph-detected clusters represent genuinely distinct sub-niches
a report reader would care about, and to label them — not to discover them.
Numbers in the prompt come from the structured store, never invented.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.llm.json_parse import loads_forgiving
from src.config import get_config
from src.llm.cascade import complete_tier, estimate_cost
from src.nodes.store import get_store
from src.state import BranchCompaction, NodeLog, ErrorRecord, ProposedNode
from src.tools.budget import lineage_spend_delta

# -- prompt parts ------------------------------------------------------------

CLUSTER_JUDGE_HEADER = """
Below are graph-detected channel clusters that the data suggests may be distinct
sub-niches under this branch. For each cluster, state whether it IS a genuinely
distinct, actionable sub-niche or is algorithmic noise:

1. If CONFIRMED: give it a short label (5-12 characters, lowercase/underscore),
   3-5 keywords, and a one-line rationale for what makes it distinct from the
   parent branch. Keep the member channel IDs exactly as provided.
2. If REJECTED: give a one-line reason (e.g. "not meaningfully distinct from
   parent", "too few channels to corroborate", "members span unrelated content").

Only confirm clusters that would produce a distinct report section a researcher
would actually read. Over-splitting dilutes the synthesis. When in doubt, reject.
"""


LEAF_FOOTER = (
    "No graph clusters were detected for this branch — the channels are a "
    "coherent population. Do NOT propose any new nodes."
)

CLUSTER_FOOTER = (
    "For each confirmed cluster above, include a proposed_node entry. For "
    "rejected clusters, omit them entirely from proposed_nodes. If you rejected "
    "all clusters, set proposed_nodes to an empty list — do not invent new nodes."
)

SYSTEM_PROMPT = """You are a YouTube niche research analyst. Given a branch's channel and video data with computed signals, and optionally graph-detected cluster candidates, produce a structured compaction.

Rules:
1. Write a 2-4 paragraph narrative summary identifying patterns, content formats, engagement levels, and outlier patterns you observe in the data.
2. Extract 2-5 key patterns as structured items, each with: pattern_name, description, evidence_summary.
3. NEVER invent numbers not present in the provided data. If a number is not in the data, do not state it.
4. If cluster candidates are provided, judge each one per the instructions in the prompt. Only confirmed clusters become proposed_nodes; rejected clusters are omitted.
5. If no cluster candidates are provided, or you reject all candidates, set proposed_nodes to empty list. NEVER invent a split the graph did not already find.

Respond with ONLY a JSON object with these exact keys:
{
  "narrative_summary": "<2-4 paragraph narrative>",
  "key_patterns": [
    {"pattern_name": "...", "description": "...", "evidence_summary": "..."}
  ],
  "proposed_nodes": [
    {
      "label": "<short_node_label>",
      "rationale": "<one-line rationale>",
      "seed_channel_ids": ["<channel_id>", ...]
    }
  ],
  "cluster_judgements": [
    {"member_channel_ids": [...], "verdict": "confirmed|rejected", "label": "...", "keywords": [...], "reason": "..."}
  ]
}"""


def _build_prompt(
    node: dict,
    channels: list[dict],
    videos: list[dict],
    cluster_candidates: list[dict[str, Any]] | None = None,
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

    When cluster_candidates is non-empty, the prompt includes a cluster-judging
    section between the branch data and the output instruction. When empty
    (true leaf), the prompt explicitly forbids split proposals.
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

    # -- cluster candidates -------------------------------------------------
    if cluster_candidates:
        lines.append("")
        lines.append(CLUSTER_JUDGE_HEADER.strip())
        for i, cc in enumerate(cluster_candidates):
            member_ids = cc.get("member_channel_ids", [])
            # Show each candidate's member channels with their video outliers.
            member_lines: list[str] = []
            for mid in member_ids:
                ch_data = next((c for c in channels if c.get("channel_id") == mid), {})
                ch_title = ch_data.get("title", mid)
                vid_os = sorted(
                    [v.get("outlier_score", 0) or 0 for v in videos if v.get("channel_id") == mid],
                    reverse=True,
                )[:3]
                os_str = f", top_outlier_scores=[{', '.join(f'{s:.1f}' for s in vid_os)}]" if vid_os else ""
                member_lines.append(f"    {mid}: {ch_title}{os_str}")
            lines.append(f"\nCluster {i+1}: {len(member_ids)} channels, distinctness={cc.get('distinctness_score', '?')}")
            lines.extend(member_lines)
        lines.append("")
        lines.append(CLUSTER_FOOTER.strip())
    else:
        lines.append("")
        lines.append(LEAF_FOOTER)

    return "\n".join(lines)


def _parse_compaction_json(raw: str) -> dict[str, Any]:
    return loads_forgiving(raw, expect="object")


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

    channel_ids = sorted(
        set(node.get("_kw_channel_ids") or []) | set(node.get("_gw_channel_ids") or [])
    )

    store = get_store()
    channels = await store.get_channels_for_node(node_id, channel_ids)
    channel_ids_found = [ch["channel_id"] for ch in channels]
    videos = await store.get_videos_for_channels(channel_ids_found)

    # v2: cluster_branch may have populated this. When non-empty, the LLM
    # judges candidates rather than discovering them from raw data.
    cluster_candidates = node.get("cluster_candidates") or None

    cfg = get_config().harness
    prompt = _build_prompt(
        node, channels, videos,
        cluster_candidates=cluster_candidates,
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

            # Determine split method for the updated node record.
            split_method = "llm_seed"
            cluster_member_ids: list[str] = []
            if cluster_candidates and proposed_nodes:
                # Graph found structure and the LLM confirmed at least one
                # cluster — this is a graph-driven split.
                split_method = "graph_cluster"
                for cc in cluster_candidates:
                    cluster_member_ids.extend(cc.get("member_channel_ids", []))

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
                    "candidates_presented": len(cluster_candidates) if cluster_candidates else 0,
                    "candidates_confirmed": len(proposed_nodes),
                    "split_method": split_method,
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
            updated_node["split_method"] = split_method
            updated_node["cluster_member_channel_ids"] = list(set(cluster_member_ids))
            # Clear cluster_candidates — they're consumed.
            updated_node.pop("cluster_candidates", None)

            return {
                "tree": {node_id: updated_node},
                "branch_compactions": [compaction.model_dump()],
                "node_logs": [node_log.model_dump()],
                "errors": errors,
                "budget_spent_usd": cost,
                "branch_lineage_spend": lineage_spend_delta(
                    state, node.get("lineage_root_id"), cost
                ),
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
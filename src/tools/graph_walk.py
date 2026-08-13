"""Frontier-based graph walk — the existential component.

Each round expands ONLY the frontier (unexpanded channels minus already-expanded),
never the cumulative discovered set. This fixes Bug 2 — cumulative re-scan
collapsing the novelty signal.

Writes only its OWN fields to the tree node (`_gw_refs`, graph novelty history,
exhaustion flag) so the parallel keyword_search write isn't clobbered — see the
deep-merge reducer in state.py.

Two-tier expansion (docs/first-run-plan.md rung 02):

  Tier A — fetch the channel record and read `featured_channels`. One record
    per channel, and the edges ride along inside a record we already paid for.
  Tier B — only for channels Tier A yielded nothing new from, and only when
    enabled: recent videos (`next_recommended_videos`) plus capped comments
    (`user_id` of each commenter). Costs 1 + V + V*C records per channel.

Traversal is keyed on channel *refs* (handle/URL), not UC ids, because that is
what the data provides: `featured_channels` entries carry a url and handle but
no UC id, and taxonomy seeds are `@handle` strings. The UC id becomes known
when the channel record is fetched — which is the same call that expands it, so
resolving ids costs nothing extra.

Algorithm from plan §8.5.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from src.config import get_config
from src.tools.bright_data import BrightDataClient, normalize_channel_ref
from src.state import ErrorRecord, NodeLog


def build_frontier(
    node: dict,
    expanded_refs: set[str],
    min_subscribers: int,
    limit: int,
) -> list[str]:
    """Refs to expand this round: unexpanded, big enough, ranked, capped.

    Ranking by subscriber count is a cost decision, not an editorial one.
    Measured on 469 live discovery results, `featured_channels` appears on 1%
    of sub-100-subscriber channels versus 18% of 100k+ ones — so expanding the
    long tail spends records and returns no edges. Channels below the threshold
    are still hydrated and scored; they are just not traversed *from*.

    Refs whose subscriber count is unknown (taxonomy seeds, which arrive as
    bare handles) are kept regardless — they are the run's starting points and
    filtering them would leave the walk with nothing to do on round one.
    """
    known_subs: dict[str, int] = {}
    for source in ("_kw_refs", "_gw_refs"):
        for ref, subs in (node.get(source) or {}).items():
            known_subs[normalize_channel_ref(ref)] = max(
                known_subs.get(normalize_channel_ref(ref), 0), int(subs or 0)
            )

    candidates: list[str] = []
    for raw in list(node.get("seed_channel_ids") or []):
        ref = normalize_channel_ref(str(raw))
        if ref and ref not in candidates:
            candidates.append(ref)
    for ref in known_subs:
        if ref and ref not in candidates:
            candidates.append(ref)

    frontier = [r for r in candidates if r not in expanded_refs]

    if min_subscribers > 0:
        frontier = [
            r for r in frontier
            if r not in known_subs or known_subs[r] >= min_subscribers
        ]

    # Unknown-subscriber seeds sort first, then by size descending.
    frontier.sort(key=lambda r: -known_subs.get(r, 10**12))

    if limit > 0:
        frontier = frontier[:limit]
    return frontier


def _mine_recommended(videos: list[dict]) -> dict[str, int]:
    """Channel refs embedded in `next_recommended_videos` entries."""
    found: dict[str, int] = {}
    for video in videos:
        for entry in video.get("next_recommended", []):
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "")
            if "/channel/" in url or "/@" in url:
                found.setdefault(normalize_channel_ref(url), 0)
    return found


async def graph_walk(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    run_id = state.get("run_id", "")
    start = time.monotonic()
    cfg = get_config().harness
    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    if not active_node_id:
        return {}

    node = tree.get(active_node_id)
    if node is None:
        return {}

    # Reported for logging only; check_saturation owns the counter.
    round_no = state.get("rounds_by_node", {}).get(active_node_id, 0) + 1
    expanded_refs = set(state.get("expanded_channel_refs", set()))
    gw_history = list(node.get("_gw_novelty_history", []))

    frontier = build_frontier(
        node,
        expanded_refs=expanded_refs,
        min_subscribers=cfg.min_subscribers_for_expansion,
        limit=cfg.graph_walk_frontier_per_round,
    )

    if not frontier:
        gw_history.append(0.0)
        return {
            "tree": {
                active_node_id: {
                    "_gw_novelty_history": gw_history,
                    "_gw_exhausted": True,
                }
            },
            "graph_walk_done": True,
            "node_logs": [
                NodeLog(
                    node_name="graph_walk",
                    thread_id=thread_id,
                    input_summary={"node_id": active_node_id, "reason": "empty frontier"},
                    latency_ms=(time.monotonic() - start) * 1000,
                    cost_usd=0.0,
                ).model_dump()
            ],
        }

    client = BrightDataClient()
    records = 0

    # --- Tier A: channel records, edges included ---------------------------
    channels, used = await client.get_channels(frontier)
    records += used

    found_refs: dict[str, int] = {}
    resolved_ids: set[str] = set()
    barren: list[str] = []
    # The edges are the product. Until now they were computed in memory and
    # discarded, which left discovery_edges empty — and that table is the
    # evidence for the project's core claim that the walk reaches channels
    # keyword search never returns.
    discovered_edges: list[dict] = []
    ref_to_id: dict[str, str] = {}

    for ch in channels:
        source_id = ch.get("channel_id", "")
        if source_id:
            resolved_ids.add(source_id)
            if ch.get("channel_ref"):
                ref_to_id[normalize_channel_ref(ch["channel_ref"])] = source_id
        edges = ch.get("featured_channel_edges") or []
        yielded = False
        for edge in edges:
            ref = edge.get("ref", "")
            if not ref:
                continue
            discovered_edges.append(
                {
                    "source_channel_id": source_id,
                    "target_channel_ref": ref,
                    "edge_type": "featured_channel",
                    "run_id": run_id,
                }
            )
            if ref in expanded_refs:
                continue
            found_refs[ref] = max(found_refs.get(ref, 0), int(edge.get("subscriber_count") or 0))
            yielded = True
        if not yielded and ch.get("channel_ref"):
            barren.append(ch["channel_ref"])

    # --- Tier B: escalate only where Tier A found nothing ------------------
    # Isolated from Tier A: Tier B triggers long-running jobs (a Videos
    # discovery was measured still running at 12 minutes against a 900s poll
    # ceiling), so it is the likely thing to raise. If that exception escaped,
    # _guarded would discard this node's whole return — including the records
    # Tier A already paid for and the refs it already expanded — so the
    # governors would not see the spend and the next round would rebuild the
    # identical frontier and buy it again.
    tier_b_used = 0
    errors: list[dict] = []
    if cfg.graph_walk_escalate_to_comments and barren:
        try:
            videos, used = await client.get_channel_videos(
                barren, limit_per_input=cfg.graph_walk_videos_per_channel
            )
            tier_b_used += used
            for video in videos:
                source_id = video.get("channel_id") or ref_to_id.get(
                    normalize_channel_ref(video.get("channel_ref", "")), ""
                )
                for ref in _mine_recommended([video]):
                    discovered_edges.append(
                        {
                            "source_channel_id": source_id,
                            "target_channel_ref": ref,
                            "edge_type": "recommendation",
                            "run_id": run_id,
                        }
                    )
                    if ref not in expanded_refs:
                        found_refs.setdefault(ref, 0)

            video_owner = {
                v["video_id"]: (
                    v.get("channel_id")
                    or ref_to_id.get(normalize_channel_ref(v.get("channel_ref", "")), "")
                )
                for v in videos
                if v.get("video_id")
            }
            video_ids = list(video_owner)
            if video_ids:
                comments, used = await client.get_comments(
                    video_ids, num_of_comments=cfg.graph_walk_comments_per_video
                )
                tier_b_used += used
                for comment in comments:
                    raw_ref = comment.get("author_channel_ref", "")
                    if not raw_ref:
                        continue
                    ref = normalize_channel_ref(raw_ref)
                    discovered_edges.append(
                        {
                            "source_channel_id": video_owner.get(comment.get("video_id", ""), ""),
                            "target_channel_id": comment.get("author_channel_id") or None,
                            "target_channel_ref": ref,
                            "edge_type": "comment_author",
                            "run_id": run_id,
                        }
                    )
                    if ref not in expanded_refs:
                        found_refs.setdefault(ref, 0)
        except Exception as exc:
            # Tier B is the long-running, failure-prone half (a Videos
            # discovery was measured still running at 12 minutes against a
            # 900s poll ceiling). Letting it escape would hand the whole node
            # to _guarded, which discards the return dict — losing the records
            # Tier A already *paid for* and the refs it already expanded, so
            # the governors never see that spend and the next round rebuilds
            # the identical frontier and buys it a second time. Whatever Tier B
            # managed before failing is kept and counted.
            errors.append(
                ErrorRecord(
                    node_name="graph_walk",
                    error_type=type(exc).__name__,
                    message=f"tier B escalation failed: {exc}",
                    recoverable=True,
                ).model_dump()
            )
        records += tier_b_used

    cost = round(records * cfg.brightdata_cost_per_record_usd, 8)

    # Persist the edges. A store failure is recorded and the walk continues —
    # the discovery work is already paid for, so losing it to a transient
    # database problem would be the expensive failure mode.
    edges_written = 0
    if discovered_edges:
        try:
            from src.db.connection import get_connection, put_connection
            from src.tools.dedup import persist_edges

            conn = get_connection()
            try:
                edges_written = persist_edges(conn, discovered_edges)
            finally:
                put_connection(conn)
        except Exception as exc:
            errors.append(
                ErrorRecord(
                    node_name="graph_walk",
                    error_type=type(exc).__name__,
                    message=f"edge persistence failed: {exc}",
                    recoverable=True,
                ).model_dump()
            )

    discovered_set = set(state.get("discovered_channel_ids", []))
    truly_new_ids = [cid for cid in resolved_ids if cid not in discovered_set]

    # Novelty must count refs never seen before — not merely refs outside this
    # round's frontier. `_gw_refs` is the backlog: refs found in earlier rounds
    # that lost the frontier cut or the subscriber filter and are still waiting.
    # Omitting them re-counts the backlog as new every round, so in a niche
    # where channels feature each other the rate never decays (and can exceed
    # 1.0), `novelty_below_threshold` can never fire, and every branch is left
    # to terminate on a circuit breaker instead of on the project's actual stop
    # condition.
    frontier_set = set(frontier)
    already_known = set(node.get("_gw_refs") or {}) | frontier_set | expanded_refs
    novel_refs = {r for r in found_refs if r not in already_known}
    novelty = len(novel_refs) / len(frontier) if frontier else 0.0
    gw_history.append(round(novelty, 4))

    merged_refs = dict(node.get("_gw_refs") or {})
    for ref, subs in found_refs.items():
        merged_refs[ref] = max(merged_refs.get(ref, 0), subs)

    return {
        "discovered_channel_ids": truly_new_ids,
        "graph_walk_channel_ids": resolved_ids,
        "visited_channel_ids": resolved_ids,
        "expanded_channel_refs": frontier_set,
        "brightdata_records_used": records,
        "budget_spent_usd": cost,
        "novelty_rates": [round(novelty, 4)],
        "tree": {
            active_node_id: {
                "_gw_refs": merged_refs,
                "_gw_channel_ids": sorted(
                    set(node.get("_gw_channel_ids") or []) | resolved_ids
                ),
                "_gw_novelty_history": gw_history,
                "_gw_exhausted": False,
                "_last_expanded_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        "graph_walk_done": True,
        "errors": errors,
        "node_logs": [
            NodeLog(
                node_name="graph_walk",
                thread_id=thread_id,
                input_summary={
                    "node_id": active_node_id,
                    "round": round_no,
                    "frontier_size": len(frontier),
                    "tier_a_records": records - tier_b_used,
                    "tier_b_records": tier_b_used,
                    "records_consumed": records,
                    "channels_resolved": len(resolved_ids),
                    "edges_found": len(discovered_edges),
                    "edges_written": edges_written,
                    "refs_found": len(novel_refs),
                    "novelty": round(novelty, 4),
                },
                latency_ms=(time.monotonic() - start) * 1000,
                cost_usd=cost,
            ).model_dump()
        ],
    }

"""underperformer_discovery — deliberate inversion of size-biased graph walk (v4, plan §10).

Samples from the bottom of each node's discovered-but-excluded backlog
(channels min_subscribers_for_expansion correctly excluded from primary
traversal) up to a per-niche quota, tags them discovery_method='underperformer_discovery'.
"""

from __future__ import annotations

import time

from src.config import get_config
from src.state import NodeLog, ErrorRecord
from src.tools.bright_data import BrightDataClient


async def underperformer_discovery(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    cfg = get_config().harness
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="underperformer_discovery",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    if not active_node_id or active_node_id not in tree:
        return {"node_logs": _log({"reason": "no active node", "discovered": 0})}

    node = tree[active_node_id]

    # Pull the backlog: refs known but below min_subscribers, never traversed
    low_sub_refs: list[str] = []
    gw_refs = node.get("_gw_refs") or {}
    kw_refs = node.get("_kw_refs") or {}
    for ref, subs in {**kw_refs, **gw_refs}.items():
        if int(subs or 0) < cfg.min_subscribers_for_expansion and int(subs or 0) > 0:
            low_sub_refs.append(ref)

    if not low_sub_refs:
        return {"node_logs": _log({"reason": "no low-sub backlog", "discovered": 0})}

    # Sort ascending — smallest first
    def _subs(ref: str) -> int:
        return int(gw_refs.get(ref, kw_refs.get(ref, 0)) or 0)
    low_sub_refs.sort(key=_subs)

    # Sample up to quota (30-40 per the Crime brief)
    quota = 30
    frontier = low_sub_refs[:quota]

    client = BrightDataClient()
    discovered: set[str] = set()
    records = 0

    try:
        channels, used = await client.get_channels(frontier)
        records += used
        for ch in channels:
            cid = ch.get("channel_id", "")
            if cid:
                discovered.add(cid)
    except Exception as exc:
        errors.append(ErrorRecord(
            node_name="underperformer_discovery",
            error_type=type(exc).__name__,
            message=str(exc),
            recoverable=True,
        ).model_dump())

    discovered_set = set(state.get("discovered_channel_ids", []))
    truly_new = [c for c in discovered if c not in discovered_set]
    cost = round(records * cfg.brightdata_cost_per_record_usd, 8)

    return {
        "discovered_channel_ids": truly_new,
        "graph_walk_channel_ids": discovered,
        "brightdata_records_used": records,
        "budget_spent_usd": cost,
        "tree": {
            active_node_id: {
                "_discovery_method_underperformer": sorted(discovered),
            }
        },
        "node_logs": _log({
            "low_sub_backlog": len(low_sub_refs),
            "sampled": len(frontier),
            "discovered": len(discovered),
            "new": len(truly_new),
        }),
        "errors": errors,
    }
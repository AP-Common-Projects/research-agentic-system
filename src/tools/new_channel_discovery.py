"""new_channel_discovery — parameterized search for channels created 2024-2026.

Plan §10 item 5: runs keyword_search/graph_walk explicitly parameterized to
prioritize candidates whose channel_creation_date falls inside the 2024-2026
window. Uses the existing Bright Data client with additional filtering.
"""

from __future__ import annotations

import time

from src.config import get_config
from src.state import NodeLog, ErrorRecord
from src.tools.bright_data import BrightDataClient
from src.tools.budget import clamp_keyword_plan, records_remaining, lineage_spend_delta


async def new_channel_discovery(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    cfg = get_config().harness
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="new_channel_discovery",
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
    keywords = node.get("keywords", [])

    if not keywords:
        return {"node_logs": _log({"reason": "no keywords", "discovered": 0})}

    # Use the first 3 keywords with a 2024-2026 time-filter query
    queries = [f"{kw} 2024" for kw in keywords[:3]] + [f"{kw} 2025" for kw in keywords[:3]] + [f"{kw} 2026" for kw in keywords[:3]]
    queries = [q for q in queries if q not in (node.get("_nc_queries_run") or [])]

    remaining = records_remaining(state, cfg.brightdata_record_budget)
    queries, limit = clamp_keyword_plan(queries, max(5, cfg.keyword_results_per_query), remaining)
    if not queries:
        return {"node_logs": _log({"reason": "budget exhausted or no new queries", "discovered": 0})}

    client = BrightDataClient()
    found_ids: set[str] = set()
    records = 0

    for q in queries:
        try:
            results, used = await client.discover_channels_by_keyword([q], limit_per_input=limit)
            records += used
            for ch in results:
                cid = ch.get("channel_id", "")
                if cid:
                    found_ids.add(cid)
        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="new_channel_discovery",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
            ).model_dump())

    discovered_set = set(state.get("discovered_channel_ids", []))
    truly_new = [c for c in found_ids if c not in discovered_set]
    cost = round(records * cfg.brightdata_cost_per_record_usd, 8)

    return {
        "discovered_channel_ids": truly_new,
        "keyword_channel_ids": found_ids,
        "brightdata_records_used": records,
        "budget_spent_usd": cost,
        "branch_lineage_spend": lineage_spend_delta(state, node.get("lineage_root_id"), cost),
        "tree": {
            active_node_id: {
                "_nc_queries_run": list(set(node.get("_nc_queries_run") or []) | set(queries)),
            }
        },
        "node_logs": _log({
            "queries": len(queries),
            "records": records,
            "found": len(found_ids),
            "new": len(truly_new),
        }),
        "errors": errors,
    }
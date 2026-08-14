"""Keyword search with its own frontier-equivalent.

Frontier-equivalent: per-node `queries_run` tracks executed query terms.
Each round: broaden_or_pivot() generates new queries, excluding already-run
queries. Novelty rate = new_channels_found / total_channels_returned.

Writes only its OWN fields to the tree node (queries_run, keyword novelty
history, exhaustion flag, `_kw_refs`) so the parallel graph_walk write isn't
clobbered.

Cost discipline (docs/first-run-plan.md rung 02): the query list is truncated
to `keyword_queries_per_round` and every discovery job carries
`limit_per_input`. Measured live, an uncapped keyword returned 469 records
($0.70); the previous unbounded 6-qualifiers-per-keyword fan-out would have
issued ~30 of those per round.

Plan §0.2 item 3 required specifying this; the source material had it as
"not yet specified" — we specify it here.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from src.config import get_config
from src.tools.bright_data import BrightDataClient
from src.tools.budget import clamp_keyword_plan, records_remaining, lineage_spend_delta
from src.state import ErrorRecord, NodeLog


def broaden_or_pivot(
    keywords: list[str],
    queries_run: list[str],
    previous_results: list[dict],
) -> list[str]:
    new_queries: list[str] = []

    if not queries_run:
        new_queries.extend(keywords)
        return new_queries

    qualifiers = ["beginner", "tutorial", "2026", "best of", "how to", "guide"]
    for kw in keywords:
        for q in qualifiers:
            candidate = f"{kw} {q}"
            if candidate not in queries_run and candidate not in new_queries:
                new_queries.append(candidate)

    if previous_results and len(previous_results) < 3:
        pivot_terms: set[str] = set()
        for result in previous_results:
            title = result.get("title", "")
            for word in title.split():
                clean = word.strip(".,!?\"'():;#@").lower()
                if len(clean) > 3 and clean not in {"the", "and", "for", "you", "how", "with"}:
                    pivot_terms.add(clean)
        for term in pivot_terms:
            if term not in queries_run and term not in new_queries:
                new_queries.append(term)

    if not new_queries and keywords:
        for kw in keywords:
            if kw not in queries_run and kw not in new_queries:
                new_queries.append(kw)

    return new_queries


async def keyword_search(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
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
    keywords = node.get("keywords", [])
    queries_run = set(node.get("queries_run", []))
    kw_history = list(node.get("_kw_novelty_history", []))

    previous_results: list[dict] = []
    new_queries = broaden_or_pivot(list(keywords), list(queries_run), previous_results)
    new_queries = [q for q in new_queries if q not in queries_run]

    # The governor. Without it this list is len(keywords) x 6 every round.
    if cfg.keyword_queries_per_round > 0:
        new_queries = new_queries[: cfg.keyword_queries_per_round]

    if not new_queries:
        kw_history.append(0.0)
        return {
            "tree": {
                active_node_id: {
                    "_kw_novelty_history": kw_history,
                    "_kw_exhausted": True,
                }
            },
            "keyword_search_done": True,
            "node_logs": [
                NodeLog(
                    node_name="keyword_search",
                    thread_id=thread_id,
                    input_summary={"node_id": active_node_id, "reason": "no new queries"},
                    latency_ms=(time.monotonic() - start) * 1000,
                    cost_usd=0.0,
                ).model_dump()
            ],
        }

    # Ask before spending. check_saturation's ceiling only binds between
    # rounds, so without this the round already in flight can carry the run
    # past the budget — measured at 176 records against a 150 ceiling.
    remaining = records_remaining(state, cfg.brightdata_record_budget)
    new_queries, limit_per_input = clamp_keyword_plan(
        new_queries, cfg.keyword_results_per_query, remaining
    )
    if not new_queries:
        return {
            "keyword_search_done": True,
            "node_logs": [
                NodeLog(
                    node_name="keyword_search",
                    thread_id=thread_id,
                    input_summary={
                        "node_id": active_node_id,
                        "reason": "record budget exhausted",
                        "records_remaining": remaining,
                    },
                    latency_ms=(time.monotonic() - start) * 1000,
                    cost_usd=0.0,
                ).model_dump()
            ],
        }

    client = BrightDataClient()
    try:
        channels, records = await client.discover_channels_by_keyword(
            new_queries, limit_per_input=limit_per_input
        )
    except Exception as exc:
        # The job may well have been created and be billing right now — the
        # trigger succeeding and the poll failing is the common shape. Record
        # the worst-case spend and mark the queries consumed, because letting
        # this escape hands the node to _guarded, which returns neither: the
        # governors would not see the charge and the next round would reissue
        # the identical paid job, up to max_rounds_per_branch times.
        worst_case = len(new_queries) * max(1, limit_per_input)
        return {
            "brightdata_records_used": worst_case,
            "budget_spent_usd": round(worst_case * cfg.brightdata_cost_per_record_usd, 8),
            "branch_lineage_spend": lineage_spend_delta(
                state, node.get("lineage_root_id"),
                worst_case * cfg.brightdata_cost_per_record_usd,
            ),
            "tree": {
                active_node_id: {
                    "queries_run": list(queries_run) + new_queries,
                    "_kw_novelty_history": kw_history + [0.0],
                }
            },
            "errors": [
                ErrorRecord(
                    node_name="keyword_search",
                    error_type=type(exc).__name__,
                    message=f"discovery failed after trigger: {exc}",
                    recoverable=True,
                ).model_dump()
            ],
            "keyword_search_done": True,
            "node_logs": [
                NodeLog(
                    node_name="keyword_search",
                    thread_id=thread_id,
                    input_summary={
                        "node_id": active_node_id,
                        "reason": "discovery failed",
                        "queries_charged": len(new_queries),
                        "worst_case_records": worst_case,
                    },
                    latency_ms=(time.monotonic() - start) * 1000,
                ).model_dump()
            ],
        }
    cost = round(records * cfg.brightdata_cost_per_record_usd, 8)

    all_channels: dict[str, dict] = {}
    refs: dict[str, int] = {}
    for ch in channels:
        ch_id = ch.get("channel_id", "")
        if ch_id and ch_id not in all_channels:
            all_channels[ch_id] = ch
        ref = ch.get("channel_ref") or ch.get("handle") or ""
        if ref:
            refs[ref] = ch.get("subscriber_count", 0)

    discovered_set = set(state.get("discovered_channel_ids", []))
    new_channels = [ch_id for ch_id in all_channels if ch_id not in discovered_set]

    # Novelty is measured against records actually returned, not against the
    # capped query count — capping queries must not look like saturation.
    novelty = len(new_channels) / records if records > 0 else 0.0
    kw_history.append(round(novelty, 4))

    return {
        "discovered_channel_ids": new_channels,
        "keyword_channel_ids": set(all_channels),
        "brightdata_records_used": records,
        "budget_spent_usd": cost,
        "branch_lineage_spend": lineage_spend_delta(
            state, node.get("lineage_root_id"), cost
        ),
        "tree": {
            active_node_id: {
                "queries_run": list(queries_run) + new_queries,
                "_kw_novelty_history": kw_history,
                "_kw_exhausted": False,
                "_kw_refs": refs,
                # Per-branch attribution. compact_branch needs the channels
                # belonging to THIS node; discovered_channel_ids is run-wide
                # and seed_channel_ids are handles, not UC ids.
                "_kw_channel_ids": sorted(all_channels),
                "_last_keyword_search_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        "novelty_rates": [round(novelty, 4)],
        "keyword_search_done": True,
        "node_logs": [
            NodeLog(
                node_name="keyword_search",
                thread_id=thread_id,
                input_summary={
                    "node_id": active_node_id,
                    "round": round_no,
                    "queries_run": len(new_queries),
                    "records_consumed": records,
                    "channels_found": len(new_channels),
                    "novelty": round(novelty, 4),
                },
                latency_ms=(time.monotonic() - start) * 1000,
                cost_usd=cost,
            ).model_dump()
        ],
    }

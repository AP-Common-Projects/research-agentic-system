"""check_saturation — the real stop condition.

Three-way routing:
- "expand_deeper" — novelty above threshold, continue
- "saturated" — node has exhausted both discovery tracks (or hit its round cap)
- "budget_exhausted" — a run-level resource ceiling tripped (circuit breaker)

Saturation is per-node, per-track. A node saturates when BOTH the keyword and
graph-walk tracks have been below the novelty threshold for the consecutive
window, or when both tracks report exhaustion (empty frontier / no new
queries).

The governors below are circuit breakers, NOT the intended stop condition —
master plan first principle #2 is that saturation, not time or budget, ends a
run. A run that stops on a governor is a run to investigate. That is why every
stop reason is logged distinctly: "saturated because novelty collapsed" and
"stopped because we ran out of allowance" must never look the same in the logs.

Ceilings are checked before the novelty computation so that a run which has
already exhausted its allowance stops immediately rather than paying for one
more round to discover it.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from src.config import get_config
from src.state import NodeLog


def check_saturation(state: dict) -> dict:
    start = time.monotonic()
    cfg = get_config().harness
    threshold = cfg.saturation_novelty_threshold
    window = cfg.saturation_consecutive_window

    # --- run-level circuit breakers ---------------------------------------
    budget_spent = state.get("budget_spent_usd", 0.0)
    if cfg.budget_limit_usd > 0 and budget_spent >= cfg.budget_limit_usd:
        return _budget_exhausted(
            state, start, "budget_limit_usd",
            spent=round(budget_spent, 4), ceiling=cfg.budget_limit_usd,
        )

    records_used = state.get("brightdata_records_used", 0)
    if cfg.brightdata_record_budget > 0 and records_used >= cfg.brightdata_record_budget:
        return _budget_exhausted(
            state, start, "brightdata_record_budget",
            spent=records_used, ceiling=cfg.brightdata_record_budget,
        )

    quota_used = state.get("youtube_quota_used", 0)
    if cfg.youtube_quota_budget_per_run > 0 and quota_used >= cfg.youtube_quota_budget_per_run:
        return _budget_exhausted(
            state, start, "youtube_quota_budget_per_run",
            spent=quota_used, ceiling=cfg.youtube_quota_budget_per_run,
        )

    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    node = tree.get(active_node_id) if active_node_id else None

    if node is None:
        return {
            "next_action": "saturated",
            "node_logs": _log(state, start, {"decision": "saturated", "reason": "no active node"}),
        }

    # --- node-level round cap ---------------------------------------------
    # This node owns the round counter, deliberately. The discovery tracks
    # cannot: when one throws, the _guarded wrapper in graph.py returns an
    # error dict that contains no counter update, so a branch whose tracks both
    # fail every round would never advance and never trip the cap. Combined
    # with a failed round also producing no novelty history and no exhaustion
    # flag, the checks below can never fire either — and the graph loops until
    # the recursion limit kills the run with an exception instead of a report.
    # check_saturation runs exactly once per round regardless of what the
    # tracks did, so counting here is the only placement that always holds.
    rounds = state.get("rounds_by_node", {}).get(active_node_id, 0) + 1
    round_update = {active_node_id: rounds}

    if cfg.max_rounds_per_branch > 0 and rounds >= cfg.max_rounds_per_branch:
        return _mark_saturated(
            state, start,
            reason="max_rounds_per_branch",
            detail={"rounds": rounds, "ceiling": cfg.max_rounds_per_branch},
            round_update=round_update,
        )

    kw_history = list(node.get("_kw_novelty_history", []))
    gw_history = list(node.get("_gw_novelty_history", []))
    kw_exhausted = bool(node.get("_kw_exhausted", False))
    gw_exhausted = bool(node.get("_gw_exhausted", False))

    if kw_exhausted and gw_exhausted:
        return _mark_saturated(
            state, start, reason="both_tracks_exhausted", round_update=round_update
        )

    if len(kw_history) >= window and len(gw_history) >= window:
        kw_low = all(r < threshold for r in kw_history[-window:])
        gw_low = all(r < threshold for r in gw_history[-window:])
        if kw_low and gw_low:
            return _mark_saturated(
                state, start, reason="novelty_below_threshold", round_update=round_update
            )

    return {
        "next_action": "expand_deeper",
        "rounds_by_node": round_update,
        "node_logs": _log(
            state, start,
            {"decision": "expand_deeper", "node_id": active_node_id, "round": rounds},
        ),
    }


def _log(state: dict, start: float, input_summary: dict) -> list[dict]:
    enriched = {
        **input_summary,
        "records_used": state.get("brightdata_records_used", 0),
        "quota_used": state.get("youtube_quota_used", 0),
        "spent_usd": round(state.get("budget_spent_usd", 0.0), 4),
    }
    return [
        NodeLog(
            node_name="check_saturation",
            thread_id=state.get("thread_id", ""),
            input_summary=enriched,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()
    ]


def _mark_saturated(
    state: dict,
    start: float,
    reason: str = "",
    detail: dict | None = None,
    round_update: dict[str, int] | None = None,
) -> dict:
    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    now = datetime.now(timezone.utc).isoformat()

    tree_update: dict[str, dict] = {}
    if active_node_id and active_node_id in tree:
        tree_update[active_node_id] = {
            "status": "saturated",
            "saturated_at": now,
            "saturation_reason": reason,
        }

    # A delta, never the accumulated list — `saturated_branches` is an
    # appending channel, so returning what we read would make it double.
    already = set(state.get("saturated_branches", []))
    delta = [active_node_id] if active_node_id and active_node_id not in already else []

    return {
        "next_action": "saturated",
        "tree": tree_update,
        "rounds_by_node": round_update or {},
        "saturated_branches": delta,
        "node_logs": _log(
            state, start,
            {
                "decision": "saturated",
                "node_id": active_node_id,
                "reason": reason,
                **(detail or {}),
            },
        ),
    }


def _budget_exhausted(
    state: dict, start: float, governor: str, spent, ceiling
) -> dict:
    """A run-level ceiling tripped — force-saturate every live branch.

    `governor` names which ceiling, so the logs distinguish "ran out of dollars"
    from "ran out of records" from "ran out of quota". These have different
    fixes and conflating them wastes an investigation.
    """
    tree = state.get("tree", {})
    now = datetime.now(timezone.utc).isoformat()
    tree_update: dict[str, dict] = {}

    for node_id, node in tree.items():
        if node.get("status") in ("active", "pending"):
            tree_update[node_id] = {
                "status": "saturated",
                "saturated_at": now,
                "saturation_reason": f"governor:{governor}",
            }

    already = set(state.get("saturated_branches", []))
    return {
        "next_action": "budget_exhausted",
        "tree": tree_update,
        "saturated_branches": [nid for nid in tree_update if nid not in already],
        "node_logs": _log(
            state, start,
            {
                "decision": "budget_exhausted",
                "governor": governor,
                "spent": spent,
                "ceiling": ceiling,
                "nodes_force_saturated": len(tree_update),
            },
        ),
    }

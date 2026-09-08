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
from src.tools.budget import lineage_share


# The clock lives in src.tools.deadline, which every enforcement point
# shares -- re-deriving it here would let the node-level checks and the
# terminating check disagree about how long the run has been going.
from src.tools.deadline import run_elapsed_seconds  # noqa: E402


def _delivered_channel_count(state: dict) -> int | None:
    """Channel rows the workbook would carry right now, or None.

    Asked of the export rather than counted in state, because the export
    applies filters state knows nothing about: the run's dominant category,
    and own_only when no category resolves. Across the runs in the store
    those drop 26% of floor-passing channels on average and 54% at worst,
    so a run stopping on its own floor-passing count would ship well under
    the band its card quotes. This is the number the client counts.

    Falls back to the floor-passing set when there is no run_id or the
    store cannot answer. That over-counts, so the run stops slightly early
    rather than running past every other ceiling -- and the run deadline,
    record budget and hydration ceiling all still bind either way.
    """
    run_id = state.get("run_id") or ""
    if run_id:
        try:
            from src.export import workbook_channel_count

            measured = workbook_channel_count(run_id)
            if measured is not None:
                return measured
        except Exception:
            pass
    qualified = state.get("qualified_channel_ids")
    return len(qualified) if qualified is not None else None


def check_saturation(state: dict) -> dict:
    """The run's stop conditions, plus the one measurement they turn on.

    A thin wrapper over _check_saturation so the delivered-channel figure
    reaches state on EVERY exit path, not just the ones that were
    remembered. There are eleven of them, and a governor that is present on
    ten is a governor with a hole in it.
    """
    delivered = _delivered_channel_count(state)
    out = _check_saturation(state, delivered)
    if delivered is not None:
        out["delivered_channel_count"] = delivered
    return out


def _check_saturation(state: dict, delivered: int | None) -> dict:
    start = time.monotonic()
    cfg = get_config().harness
    threshold = cfg.saturation_novelty_threshold
    window = cfg.saturation_consecutive_window

    # --- run-level circuit breakers ---------------------------------------
    # Time first: every ceiling below bounds work, and a run can sit well
    # inside all of them while running for hours. The console names each
    # depth by duration, so duration has to be a real ceiling and not a
    # projection -- see run_deadline_seconds in config.py.
    if cfg.run_deadline_seconds > 0:
        elapsed = run_elapsed_seconds()
        if elapsed >= cfg.run_deadline_seconds:
            return _budget_exhausted(
                state, start, "run_deadline_seconds",
                spent=int(elapsed), ceiling=cfg.run_deadline_seconds,
            )

    budget_spent = state.get("budget_spent_usd", 0.0)
    if cfg.budget_limit_usd > 0 and budget_spent >= cfg.budget_limit_usd:
        return _budget_exhausted(
            state, start, "budget_limit_usd",
            spent=round(budget_spent, 4), ceiling=cfg.budget_limit_usd,
        )

    records_used = state.get("brightdata_records_used", 0)
    if cfg.brightdata_record_budget > 0:
        # The pre-spend gate in budget.py clamps every discovery node to 90%
        # of this ceiling (records_remaining targets CEILING_TARGET_RATIO),
        # so nothing can spend past that point. But this check only fired at
        # 100% — records_used >= brightdata_record_budget outright. Between
        # 90% and 100% both discovery nodes correctly return zero records
        # every round, hydrate_metadata finds nothing new, and the run sits in
        # an expand_deeper / continue_active loop burning rounds forever: no
        # node can spend, and nothing recognises that as "done".
        #
        # Observed live: a Finance run reached 3,610 of a 4,000 ceiling (90%
        # gate at 3,600), then looped at that exact number for the rest of its
        # allotted rounds and handed off to Legal with no report — 238 steps,
        # $5.42 spent, no synthesis. The two ceilings have to agree: if the
        # pre-spend gate says no more can be bought, the governor must say the
        # run is over, not wait for a total that spending is no longer able
        # to reach.
        from src.tools.budget import CEILING_TARGET_RATIO

        effective_ceiling = min(
            cfg.brightdata_record_budget,
            int(cfg.brightdata_record_budget * CEILING_TARGET_RATIO) + 1,
        )
        if records_used >= effective_ceiling:
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

    # The channel cap, for the same reason as the record ceiling above.
    #
    # Hydration is the gate everything downstream depends on, and it trims to
    # max_channels_per_run. Once that is full it returns "no new channels to
    # hydrate" for the rest of the run -- so every further round of keyword
    # search, graph walk and breakout scanning buys channels that can never
    # be hydrated, classified or exported. Nothing recognised that as done.
    #
    # Observed on run-bc5226fb2e06, a Standard education run: the cap filled
    # on the FIRST hydration (100 hydrated, 565 trimmed), then nineteen more
    # rounds found nothing to do while spending 827 Bright Data records and
    # 1,251 quota units, until LangGraph's recursion limit ended the run with
    # no export at all. Exactly the shape the record ceiling comment above
    # describes: no node can contribute, and nothing calls it finished.
    #
    # Two ceilings, because there are two populations. The target counts
    # channels that clear the subscriber floor -- the ones the workbook can
    # carry, and the number the depth card quotes. The hydration ceiling
    # bounds what the run may spend reaching it. Measuring the target
    # against hydrated channels is why a Standard run that had "reached its
    # channel limit (100 of 100)" delivered a 17-row file.
    from src.tools.deliverable import run_ceilings

    target, ceiling = run_ceilings(cfg)
    if target > 0 and delivered is not None:
        if delivered >= target:
            return _budget_exhausted(
                state, start, "max_channels_per_run",
                spent=delivered, ceiling=target,
            )
    if ceiling > 0:
        hydrated = len(state.get("hydrated_channel_ids") or ())
        if hydrated >= ceiling:
            return _budget_exhausted(
                state, start, "max_hydrated_channels_per_run",
                spent=hydrated, ceiling=ceiling,
            )

    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    node = tree.get(active_node_id) if active_node_id else None

    if node is None:
        return {
            "next_action": "saturated",
            "node_logs": _log(state, start, {"decision": "saturated", "reason": "no active node"}),
        }

    # --- branch-lineage budget -------------------------------------------
    # ADR-0006 rejected a global-only record ceiling because one runaway
    # branch would consume the whole run before other branches were touched.
    # Unbounded depth reintroduces that risk one level up: a deep chain of
    # splits under one root branch could starve its siblings. Each depth-1
    # lineage gets budget_limit_usd / num_depth1_branches; when a lineage's
    # cumulative spend crosses its share, only that subtree force-saturates.
    if cfg.branch_lineage_budget_enabled and cfg.budget_limit_usd > 0:
        lineage_id = node.get("lineage_root_id")
        if lineage_id:
            num_depth1 = sum(
                1 for n in tree.values() if n.get("depth") == 1
            )
            share = lineage_share(cfg.budget_limit_usd, num_depth1)
            if share is not None:
                spent = state.get("branch_lineage_spend", {}).get(lineage_id, 0.0)
                if spent >= share:
                    return _mark_lineage_exhausted(
                        state, start, lineage_id, spent, share
                    )

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

    # A round whose discovery calls failed records None, not a number: no
    # measurement was taken. If BOTH tracks have gone unmeasured for the whole
    # window, the vendor is down and continuing just burns rounds against a
    # dead API — stop, and say so, rather than letting it read as saturation.
    kw_recent = kw_history[-window:]
    gw_recent = gw_history[-window:]
    if len(kw_history) >= window and len(gw_history) >= window:
        if all(r is None for r in kw_recent) and all(r is None for r in gw_recent):
            return _mark_saturated(
                state, start,
                reason="discovery_unavailable",
                detail={"unmeasured_rounds": window},
                round_update=round_update,
            )

        # `r is not None` is the load-bearing half. Scoring a failed round as
        # 0.0 satisfied this check, so an outage produced
        # `novelty_below_threshold` — precisely the confusion this module's
        # docstring says must never happen. Observed live: two branches
        # reported saturation while every Bright Data call was returning
        # "Customer is not active".
        kw_low = all(r is not None and r < threshold for r in kw_recent)
        gw_low = all(r is not None and r < threshold for r in gw_recent)
        if kw_low and gw_low:
            return _mark_saturated(
                state, start, reason="novelty_below_threshold", round_update=round_update
            )

        # A track is "done" when it either exhausts (threshold) or stops
        # improving (plateau) — whichever it reaches. Measured on Finance the
        # two tracks behave completely differently: graph walk genuinely decays
        # to zero, while keyword search drops once and then holds ~0.65
        # forever, because broaden_or_pivot keeps generating NEW query variants
        # rather than draining a fixed pool. Requiring both to cross an
        # absolute threshold means the keyword track alone blocks saturation
        # indefinitely, which is why every live branch has died on a governor.
        if cfg.plateau_detection_enabled:
            kw_done = kw_low or _is_plateaued(kw_history, window, cfg.novelty_plateau_epsilon)
            gw_done = gw_low or _is_plateaued(gw_history, window, cfg.novelty_plateau_epsilon)
            if kw_done and gw_done:
                return _mark_saturated(
                    state, start,
                    reason="novelty_plateaued",
                    detail={
                        "keyword_novelty": kw_recent[-1] if kw_recent else None,
                        "graph_walk_novelty": gw_recent[-1] if gw_recent else None,
                        "epsilon": cfg.novelty_plateau_epsilon,
                    },
                    round_update=round_update,
                )

    return {
        "next_action": "expand_deeper",
        "rounds_by_node": round_update,
        "node_logs": _log(
            state, start,
            {"decision": "expand_deeper", "node_id": active_node_id, "round": rounds},
        ),
    }


def _is_plateaued(history: list, window: int, epsilon: float) -> bool:
    """True when a track's novelty has stopped improving.

    Scale-invariant, deliberately. An absolute threshold is a guess about how
    big the niche is, and the guess was wrong: measured on Finance, keyword
    novelty settles at ~0.65 across every branch and never approaches 0.05, so
    any threshold below the plateau never fires and any above it fires
    immediately and meaninglessly. What IS observable is that the series stops
    moving — deltas of +0.017, -0.05, 0.0 over three rounds.

    This detects diminishing returns, NOT exhaustion, and check_saturation
    reports it under its own name so the two are never conflated. A plateaued
    branch has not covered its niche; it has reached steady-state extraction,
    where further rounds add volume at constant cost without converging.

    Requires a real decline from the peak, so a track that returns 1.0 for its
    first several rounds — discovering as fast as it possibly can — is not
    mistaken for one that has levelled off.

    Unmeasured rounds (None, from a failed discovery call) are skipped rather
    than treated as values: an outage must not manufacture a flat line.
    """
    measured = [r for r in history if r is not None]
    if len(measured) < window:
        return False
    recent = measured[-window:]
    deltas = [recent[i + 1] - recent[i] for i in range(len(recent) - 1)]
    if any(abs(d) > epsilon for d in deltas):
        return False
    return recent[-1] < max(measured) - epsilon


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


def _mark_lineage_exhausted(
    state: dict, start: float, lineage_id: str, spent: float, share: float
) -> dict:
    """One depth-1 lineage crossed its share — force-saturate that subtree only.

    Distinct from _budget_exhausted (run-level) and _mark_saturated
    (novelty/rounds): other lineages keep their remaining budget. The
    governor name is logged so "stopped because this branch burned its share"
    is never mistaken for "ran out of records".
    """
    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    now = datetime.now(timezone.utc).isoformat()
    tree_update: dict[str, dict] = {}

    for node_id, node in tree.items():
        if node.get("lineage_root_id") == lineage_id and node.get("status") in (
            "active",
            "pending",
        ):
            tree_update[node_id] = {
                "status": "saturated",
                "saturated_at": now,
                "saturation_reason": "governor:branch_lineage_budget",
            }

    already = set(state.get("saturated_branches", []))
    return {
        "next_action": "saturated",
        "tree": tree_update,
        "saturated_branches": [nid for nid in tree_update if nid not in already],
        "node_logs": _log(
            state, start,
            {
                "decision": "saturated",
                "node_id": active_node_id,
                "reason": "governor:branch_lineage_budget",
                "lineage_id": lineage_id,
                "spent": round(spent, 4),
                "share": round(share, 4),
                "nodes_force_saturated": len(tree_update),
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

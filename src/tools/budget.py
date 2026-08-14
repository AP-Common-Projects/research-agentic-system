"""Pre-spend budget arithmetic for the paid discovery calls.

`check_saturation` enforces the run-level ceilings, but it runs *downstream* of
both spending nodes — so its verdict always applies to the next round, using
last round's totals. A run can therefore overshoot by one full round, which is
not theoretical: every smoke run in logs/ stopped at 176 records against a
150-record ceiling. Under `bounded` a round can be 630 records, so the same
mechanism permits ~3.7x the documented budget.

This module is the pre-spend half: the discovery nodes ask how much they may
spend *before* triggering, and clamp their request to fit. It mirrors
`YouTubeAPIClient.check_quota`, which already got this right — 90% of ceiling,
checked ahead of the call rather than after it.

The 90% target leaves headroom for the round that is already in flight, so the
hard ceiling in check_saturation stays a backstop rather than the thing that
routinely fires.
"""

from __future__ import annotations

CEILING_TARGET_RATIO = 0.90


def records_remaining(state: dict, record_budget: int) -> int | None:
    """Records this run may still spend. None means uncapped.

    Returns 0 rather than a negative number when already over — callers treat
    0 as "trigger nothing".
    """
    if record_budget <= 0:
        return None
    used = int(state.get("brightdata_records_used", 0) or 0)
    target = int(record_budget * CEILING_TARGET_RATIO)
    return max(0, target - used)


def clamp_keyword_plan(
    queries: list[str], limit_per_input: int, remaining: int | None
) -> tuple[list[str], int]:
    """Trim a keyword round to what the budget allows.

    Worst case for a discovery round is len(queries) * limit_per_input, since
    `limit_per_input` is a per-input cap. Drops whole queries first — a query
    run at a reduced limit still costs a job and returns a truncated view of
    that keyword, so fewer complete queries is the better trade.
    """
    if remaining is None:
        return queries, limit_per_input
    if remaining <= 0 or not queries or limit_per_input <= 0:
        return [], limit_per_input

    affordable = remaining // limit_per_input
    if affordable >= len(queries):
        return queries, limit_per_input
    if affordable >= 1:
        return queries[:affordable], limit_per_input
    # Not even one query at full width — run a single narrowed query rather
    # than stalling the branch entirely.
    return queries[:1], max(1, remaining)


def clamp_frontier(frontier: list[str], remaining: int | None) -> list[str]:
    """Trim a Tier A frontier to the budget. One record per channel."""
    if remaining is None:
        return frontier
    if remaining <= 0:
        return []
    return frontier[:remaining]


def tier_b_affordable(
    barren: list[str], videos_per_channel: int, comments_per_video: int,
    remaining: int | None,
) -> list[str]:
    """Channels that may be escalated, given what is left.

    Tier B costs `videos_per_channel * (1 + comments_per_video)` records per
    channel — 33 at the bounded settings, against Tier A's 1. This is where a
    round goes from tens of records to hundreds, so it is the call that most
    needs to ask permission first.
    """
    if remaining is None:
        return barren
    per_channel = max(1, videos_per_channel * (1 + comments_per_video))
    affordable = remaining // per_channel
    return barren[:affordable] if affordable > 0 else []


def lineage_share(budget_limit_usd: float, num_depth1_branches: int) -> float | None:
    """Per-depth-1-lineage USD share of the run budget. None means uncapped.

    Generalized from ADR-0006's own rejection of a global-only ceiling: an
    unbounded-depth tree reintroduces the "one runaway branch starves the run"
    failure one level up — a deep chain of splits under one root branch could
    consume the whole allowance before a sibling root branch gets a fair look.
    """
    if budget_limit_usd <= 0 or num_depth1_branches <= 0:
        return None
    return budget_limit_usd / num_depth1_branches


def lineage_spend_delta(state: dict, lineage_root_id: str | None, cost_usd: float) -> dict[str, float]:
    """The spend delta a node reports against its depth-1 lineage root.

    Returns an empty dict when the node has no lineage (root, pre-taxonomy
    failures) or spent nothing — so callers can always merge it into their
    return dict unconditionally.
    """
    if not lineage_root_id or not cost_usd or cost_usd <= 0:
        return {}
    return {lineage_root_id: round(float(cost_usd), 8)}

"""Research depth tiers -- the client-facing "how deep should this run go".

A tier is a named bundle of the same cost governors HarnessConfig already
exposes (see PROFILES in src/config.py). It is not a new execution mode:
launching a run with a tier sets those governors for that run and nothing
else changes.

Durations are the honest shape of these runs. A discovery pass on one
keyword takes minutes (Bright Data snapshots ran ~3.4 min/keyword measured
2026-09-01), hydration is ~4 channels/min, and every per-channel LLM node
is latency-bound at roughly one call per channel. Depth is therefore
bought in hours, not minutes, and the tier names say so plainly.

Cost estimates are split by provider because the two fail differently and
are topped up separately. They come from measured spend, not list prices:

  Bright Data  $0.0015/record. Five discovery passes on 2026-08-31/09-01
               pulled 39,208 records for ~$59.
  OpenRouter   measured per-call, e.g. crime case metadata at $0.0077 per
               8-video batch, classification at one mid-tier call per
               channel.

The channel/video yields are what those same runs actually produced, so a
client reading "≈350-450 channels" is reading history rather than a promise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class DepthTier:
    id: str
    label: str
    hours: int
    tagline: str
    description: str
    # What the client gets, from comparable historical runs.
    est_channels: str
    est_videos: str
    # Split so a single low provider locks the tier for the right reason.
    est_brightdata_usd: float
    est_openrouter_usd: float
    # Governor overrides handed to the run as env, mirroring PROFILES keys.
    governors: dict[str, Any] = field(default_factory=dict)

    @property
    def est_total_usd(self) -> float:
        return round(self.est_brightdata_usd + self.est_openrouter_usd, 2)


# Ordered shallowest to deepest; the UI renders them in this order.
TIERS: list[DepthTier] = [
    DepthTier(
        id="scout",
        label="Scout",
        hours=1,
        tagline="A first look at whether the topic is worth pursuing",
        description=(
            "One shallow discovery sweep on the strongest keywords. Enough to "
            "see whether a topic has channels above the floor at all, and what "
            "the obvious sub-niches are. Not a dataset you would hand a client."
        ),
        est_channels="40-60",
        est_videos="1,500-2,500",
        est_brightdata_usd=2.25,
        est_openrouter_usd=0.75,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 3,
            "MAX_TREE_DEPTH": 1,
            "MAX_BRANCHES": 2,
            "KEYWORD_QUERIES_PER_ROUND": 6,
            "KEYWORD_RESULTS_PER_QUERY": 25,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 10,
            "BRIGHTDATA_RECORD_BUDGET": 1500,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 3000,
            "BUDGET_LIMIT_USD": 3.0,
        },
    ),
    DepthTier(
        id="survey",
        label="Survey",
        hours=6,
        tagline="Enough breadth to compare sub-niches against each other",
        description=(
            "Covers the main sub-niches with enough channels in each to make "
            "comparisons meaningful. The usual starting point for a topic "
            "nobody has mapped yet."
        ),
        est_channels="120-180",
        est_videos="5,000-8,000",
        est_brightdata_usd=7.50,
        est_openrouter_usd=2.50,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 4,
            "MAX_TREE_DEPTH": 2,
            "MAX_BRANCHES": 4,
            "KEYWORD_QUERIES_PER_ROUND": 12,
            "KEYWORD_RESULTS_PER_QUERY": 50,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 20,
            "BRIGHTDATA_RECORD_BUDGET": 5000,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 8000,
            "BUDGET_LIMIT_USD": 10.0,
        },
    ),
    DepthTier(
        id="deep_dive",
        label="Deep Dive",
        hours=12,
        tagline="Full sub-niche coverage with size and cohort structure",
        description=(
            "Adds the long tail of sub-niches and enough channels per size "
            "band to support the stratified analysis the workbooks are built "
            "for -- including underperformers, not just winners."
        ),
        est_channels="220-300",
        est_videos="9,000-14,000",
        est_brightdata_usd=13.50,
        est_openrouter_usd=4.50,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 6,
            "MAX_TREE_DEPTH": 3,
            "MAX_BRANCHES": 6,
            "KEYWORD_QUERIES_PER_ROUND": 18,
            "KEYWORD_RESULTS_PER_QUERY": 100,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 30,
            "BRIGHTDATA_RECORD_BUDGET": 9000,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 15000,
            "BUDGET_LIMIT_USD": 18.0,
        },
    ),
    DepthTier(
        id="expedition",
        label="Expedition",
        hours=24,
        tagline="Client-deliverable depth for a single vertical",
        description=(
            "The depth the Finance and Crime workbooks were built at. Broad "
            "head-term discovery plus the long tail, full enrichment, and "
            "enough channels to hit a stratified size distribution."
        ),
        est_channels="350-450",
        est_videos="15,000-22,000",
        est_brightdata_usd=24.00,
        est_openrouter_usd=8.00,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 8,
            "MAX_TREE_DEPTH": 4,
            "MAX_BRANCHES": 8,
            "KEYWORD_QUERIES_PER_ROUND": 24,
            "KEYWORD_RESULTS_PER_QUERY": 150,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 50,
            "BRIGHTDATA_RECORD_BUDGET": 16000,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 25000,
            "BUDGET_LIMIT_USD": 32.0,
        },
    ),
    DepthTier(
        id="atlas",
        label="Atlas",
        hours=48,
        tagline="Exhaustive within the vertical, including adjacent niches",
        description=(
            "Pushes past the point where head keywords stop returning new "
            "channels and into adjacent sub-niches. Use when the goal is to "
            "be able to say the vertical has been mapped, not sampled."
        ),
        est_channels="550-700",
        est_videos="24,000-34,000",
        est_brightdata_usd=42.00,
        est_openrouter_usd=13.00,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 12,
            "MAX_TREE_DEPTH": 5,
            "MAX_BRANCHES": 12,
            "KEYWORD_QUERIES_PER_ROUND": 36,
            "KEYWORD_RESULTS_PER_QUERY": 200,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 80,
            "BRIGHTDATA_RECORD_BUDGET": 28000,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 40000,
            "BUDGET_LIMIT_USD": 55.0,
        },
    ),
    DepthTier(
        id="census",
        label="Census",
        hours=72,
        tagline="Everything the vertical has, to saturation",
        description=(
            "Runs until discovery genuinely saturates rather than until a "
            "governor trips. The most complete dataset the harness can "
            "produce for a topic, and the most expensive."
        ),
        est_channels="750-900",
        est_videos="35,000-48,000",
        est_brightdata_usd=60.00,
        est_openrouter_usd=18.00,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 0,   # 0 == uncapped, as in the `full` profile
            "MAX_TREE_DEPTH": 0,
            "MAX_BRANCHES": 0,
            "KEYWORD_QUERIES_PER_ROUND": 0,
            "KEYWORD_RESULTS_PER_QUERY": 250,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 0,
            "BRIGHTDATA_RECORD_BUDGET": 40000,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 0,
            "BUDGET_LIMIT_USD": 78.0,
        },
    ),
]

TIERS_BY_ID: dict[str, DepthTier] = {t.id: t for t in TIERS}


def get_tier(tier_id: str) -> DepthTier | None:
    return TIERS_BY_ID.get((tier_id or "").strip().lower())


def _provider(balances: dict[str, Any], name: str) -> dict[str, Any]:
    for row in balances.get("providers", []):
        if row.get("provider") == name:
            return row
    return {}


def tiers_with_availability(balances: dict[str, Any]) -> list[dict[str, Any]]:
    """Every tier, annotated with whether the wallet can currently fund it.

    A tier is locked only on evidence. An unknown balance -- Bright Data
    without billing permission and without a configured starting figure --
    does NOT lock anything: refusing to run on a number nobody has is worse
    than running and hitting the harness's own circuit breaker, which exists
    for exactly this. It is surfaced as a warning instead.
    """
    openrouter = _provider(balances, "openrouter")
    brightdata = _provider(balances, "brightdata")

    out: list[dict[str, Any]] = []
    for tier in TIERS:
        row = asdict(tier)
        row["est_total_usd"] = tier.est_total_usd

        blockers: list[str] = []
        warnings: list[str] = []

        or_avail = openrouter.get("available_usd")
        if or_avail is None:
            warnings.append("OpenRouter balance unavailable; cost not verified.")
        elif or_avail < tier.est_openrouter_usd:
            blockers.append(
                f"OpenRouter has ${or_avail:,.2f}; this depth needs about "
                f"${tier.est_openrouter_usd:,.2f}."
            )

        bd_avail = brightdata.get("available_usd")
        if bd_avail is None:
            warnings.append(
                "Bright Data balance is not readable, so this depth's "
                f"~${tier.est_brightdata_usd:,.2f} of discovery spend could not "
                "be checked against it."
            )
        elif bd_avail < tier.est_brightdata_usd:
            blockers.append(
                f"Bright Data has ${bd_avail:,.2f}; this depth needs about "
                f"${tier.est_brightdata_usd:,.2f}."
            )

        row["locked"] = bool(blockers)
        row["blockers"] = blockers
        row["warnings"] = warnings
        out.append(row)

    return out

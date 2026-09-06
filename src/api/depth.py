"""Research depth tiers -- the client-facing "how deep should this run go".

A tier is a named bundle of the same cost governors HarnessConfig already
exposes (see PROFILES in src/config.py). It is not a new execution mode:
launching a run with a tier sets those governors for that run and nothing
else changes.

Durations are the honest shape of these runs. A discovery pass on one
keyword takes minutes (Bright Data snapshots ran ~3.4 min/keyword measured
2026-09-01), hydration is ~4 channels/min, and every per-channel LLM node
is latency-bound at roughly one call per channel. Depth is therefore
bought in hours rather than minutes at every tier but the shortest,
which buys a single keyword pass and says so.

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


#: Measured across real runs: classify_channel takes ~21 minutes per batch
#: of 50 channels, one LLM call each.
_SECONDS_PER_CHANNEL = 25.0
#: Share of a run's window left for enrichment once discovery and hydration
#: have had their turn. From the automotive run: ~15 minutes of discovery
#: plus hydration inside a 30-minute window.
_ENRICHMENT_WINDOW_SHARE = 0.5
#: Below this a tier is not worth running at all.
_MIN_CHANNELS_PER_RUN = 20
#: Videos per channel, bracketing two complete measurements: the automotive
#: run at 21 (5,916 across 277) and the delivered finance workbook at 59
#: (20,550 across 350).
_VIDEOS_PER_CHANNEL_LOW = 21
_VIDEOS_PER_CHANNEL_HIGH = 59


@dataclass
class DepthTier:
    id: str
    label: str
    hours: float
    tagline: str
    description: str
    # Split so a single low provider locks the tier for the right reason.
    est_brightdata_usd: float
    est_openrouter_usd: float
    # Governor overrides handed to the run as env, mirroring PROFILES keys.
    governors: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Derived from `hours`, never written by hand: this is the ceiling
        # that makes the tier's own name true, and a tier whose deadline
        # disagreed with its label would be exactly the bug this fixes.
        # Every other governor here bounds work; this one bounds the clock,
        # and without it "30m" was a projection that ran 3-5 hours.
        self.governors["RUN_DEADLINE_SECONDS"] = int(self.hours * 3600)
        self.governors["MAX_CHANNELS_PER_RUN"] = self.max_channels

    @property
    def est_channels(self) -> str:
        """Channels a run of this length actually returns.

        The cap IS the estimate now. It is enforced -- discovery admission
        stops at it and hydration trims to it -- so a run fills to the cap
        wherever the topic has that many channels above the floor, and
        stops there. The hand-written ranges this replaced answered to
        nothing: Glimpse advertised "15-25" and the measured run discovered
        273, four times what it could describe.

        Phrased as a ceiling, not a range, because that is what it is.
        Discovery saturation binds long before the cap on any real topic --
        the two verticals measured end to end hold 350 (finance) and 240
        (crime) channels above the 50k floor, so a 72-hour run's 5,184 is a
        limit it will never approach on a topic that size. Promising a
        range would be inventing the lower end; a ceiling states what the
        run is permitted to do and lets the topic decide the rest.
        """
        return f"up to {self.max_channels:,}"

    @property
    def est_videos(self) -> str:
        """Videos those channels bring with them.

        Two complete measurements bracket this: the automotive run
        hydrated 5,916 videos across 277 channels (21/channel) and the
        delivered finance workbook holds 20,550 across 350 (59/channel).
        The spread is real -- it is how much back catalogue a vertical's
        channels carry -- so it is reported as a range rather than
        averaged into a single number that would be wrong for both. Like
        est_channels this is a ceiling: it follows the channel cap, which
        saturation reaches first on any real topic.
        """
        # The high rate, since this is a ceiling like est_channels. The low
        # rate is what a vertical of short-catalogue channels returns and is
        # kept in the constant for anyone sizing a run by hand.
        return f"up to {int(self.max_channels * _VIDEOS_PER_CHANNEL_HIGH):,}"

    @property
    def max_channels(self) -> int:
        """How many channels this tier can actually finish enriching.

        Derived, not chosen. Discovery volume and enrichment capacity used
        to be unrelated numbers, and the gap between them is what produced
        a workbook whose classification columns were 95% empty: Glimpse's
        record budget found 273 channels, and one cycle of classification
        can describe 50. The run had been asked to find four times what it
        could ever describe.

        The model is deliberately crude and its inputs are measured:
        classification runs at ~25s per channel (21 minutes per batch of
        50, across several real runs), and roughly half a run's window goes
        on discovery and hydration before enrichment starts. Both come from
        a small number of observations and should be re-measured -- but a
        cap derived from real timings beats an estimate that answers to
        nothing, which is what the channel counts here used to be.
        """
        return max(
            _MIN_CHANNELS_PER_RUN,
            int(self.hours * 3600 * _ENRICHMENT_WINDOW_SHARE / _SECONDS_PER_CHANNEL),
        )

    @property
    def est_total_usd(self) -> float:
        return round(self.est_brightdata_usd + self.est_openrouter_usd, 2)

    @property
    def duration_label(self) -> str:
        """How long the tier runs, in the largest unit that stays whole.

        The shortest tier is half an hour, which reads as "0.5h" if the UI
        formats it itself. Formatting here keeps every surface -- cards,
        confirmations, the balances table -- saying the same thing.
        """
        if self.hours < 1:
            return f"{round(self.hours * 60)}m"
        return f"{self.hours:g}h"


# Ordered shallowest to deepest; the UI renders them in this order.
TIERS: list[DepthTier] = [
    DepthTier(
        id="glimpse",
        label="Glimpse",
        hours=0.5,
        tagline="A quick read on whether a topic has anything in it",
        description=(
            "A single discovery pass on the strongest head keyword, hydrated "
            "and classified. Enough to see the shape of a topic and the "
            "biggest channels in it before committing to a longer run."
        ),
        est_brightdata_usd=0.90,
        est_openrouter_usd=0.30,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 1,
            "MAX_TREE_DEPTH": 1,
            "MAX_BRANCHES": 1,
            "KEYWORD_QUERIES_PER_ROUND": 4,
            "KEYWORD_RESULTS_PER_QUERY": 25,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 5,
            "BRIGHTDATA_RECORD_BUDGET": 600,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 1200,
            "BUDGET_LIMIT_USD": 1.2,
        },
    ),
    DepthTier(
        id="scout",
        label="Scout",
        hours=1,
        tagline="A first look at whether the topic is worth pursuing",
        description=(
            "One shallow discovery sweep on the strongest keywords. Enough to "
            "see whether a topic has channels above the floor at all, and what "
            "the obvious sub-niches are. A scouting pass rather than a finished "
            "deliverable."
        ),
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
            "Keeps going until discovery genuinely runs dry rather than until a "
            "limit is reached. The most complete picture we can build for a "
            "topic, and the most expensive."
        ),
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
        # asdict() sees dataclass FIELDS only, so every derived value has to
        # be added by hand. est_channels and est_videos became properties
        # (they follow max_channels now) and silently vanished from the API
        # payload until this line -- the depth cards rendered blank.
        row["est_total_usd"] = tier.est_total_usd
        row["duration_label"] = tier.duration_label
        row["est_channels"] = tier.est_channels
        row["est_videos"] = tier.est_videos
        row["max_channels"] = tier.max_channels

        blockers: list[str] = []
        warnings: list[str] = []

        or_avail = openrouter.get("available_usd")
        if or_avail is None:
            warnings.append(
                "We could not read the OpenRouter balance just now, so this "
                "cost has not been checked against it."
            )
        elif or_avail < tier.est_openrouter_usd:
            blockers.append(
                f"OpenRouter has ${or_avail:,.2f}; this depth needs about "
                f"${tier.est_openrouter_usd:,.2f}."
            )

        bd_avail = brightdata.get("available_usd")
        if bd_avail is None:
            warnings.append(
                "We could not read the Bright Data balance just now, so the "
                f"~${tier.est_brightdata_usd:,.2f} of discovery spend has not "
                "been checked against it."
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

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


#: Seconds of work per floor-passing channel DELIVERED, summed across every
#: per-channel stage, measured on run-c6c45a3e91b2 (86 floor-passing out of
#: 273 discovered, 4,614 videos):
#:
#:     discovery fan-out                10.0s
#:     hydrate_metadata                 15.1s
#:     classify_channel                 16.1s
#:     resolve_first_video_date          9.0s
#:     extract_success_failure_factors  11.6s
#:     populate_taxonomy_dimensions     14.2s
#:     populate_shared_fields           10.0s
#:     describe_video_titles            26.8s  (54 videos/channel, 30/batch)
#:                                     -------
#:                                     112.9s
#:
#: The previous model used 25s, counting classify_channel alone. That is
#: the whole reason a "30-minute" tier ran for hours: five of the eight
#: stages were not in the arithmetic at all. Every figure above is from one
#: real run and should be re-measured as more land -- but a number taken
#: from a clock beats one taken from an assumption.
_SECONDS_PER_CHANNEL = 113.0

#: Work that happens once regardless of channel count: taxonomy build (19s
#: measured), the latency floor of a single Bright Data snapshot cycle
#: (a run of one channel still waits for one), branch compaction (30s),
#: export and the completeness gate.
#:
#: Deliberately NOT the whole discovery phase -- that is already inside
#: _SECONDS_PER_CHANNEL at 10s per floor-passing channel, and counting it
#: twice shortened every tier.
_FIXED_OVERHEAD_SECONDS = 600.0

#: Channel counts are set to this fraction of what the window theoretically
#: allows. A tier that fits only if every stage hits its average is a tier
#: that overruns whenever one does not.
_SAFETY_MARGIN = 0.82

#: Measured LLM spend per channel: 4 channel-level calls plus ~1.8 video
#: description batches, at $0.0044/call across 1,328 observed calls.
_USD_PER_CHANNEL = 0.0255
#: build_taxonomy, compact_branch and the niche work, once per run.
_USD_FIXED = 0.35
#: Quoted cost carries this multiple. Under-quoting strands a run
#: mid-flight; over-quoting only makes a tier lock earlier than it must.
_COST_MARGIN = 1.5

#: Videos per channel, from the two complete measurements available: the
#: automotive run at 54 (4,614/86) and the delivered finance workbook at 59
#: (20,550/350).
_VIDEOS_PER_CHANNEL = 54


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
    def max_channels(self) -> int:
        """How many channels this tier can actually finish, end to end.

        Every per-channel stage is in this number, not just the one that
        was easiest to time. Enforced as MAX_CHANNELS_PER_RUN, so discovery
        stops here rather than finding several times what the window can
        describe -- which is what produced a workbook of mostly-empty
        columns and a run four times its stated length.
        """
        usable = self.hours * 3600 - _FIXED_OVERHEAD_SECONDS
        return max(10, int(usable * _SAFETY_MARGIN / _SECONDS_PER_CHANNEL))

    @property
    def est_channels(self) -> str:
        """Channels in the finished workbook.

        The cap IS the estimate, phrased as a ceiling: discovery saturation
        binds first on a thin topic, and a run that finds fewer stops
        early rather than padding.
        """
        return f"up to {self.max_channels:,}"

    @property
    def est_videos(self) -> str:
        return f"up to {self.max_channels * _VIDEOS_PER_CHANNEL:,}"

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
#
# Three, not seven. The old ladder was seven guesses at the same unknown:
# every tier shared one per-channel model, so when that model turned out to
# be four times optimistic, all seven were wrong together and the client had
# six ways to pick the wrong one. Three tiers, each sized from measured
# throughput and each enforced by a deadline and a channel cap, answer the
# only question a client actually has -- a look, a proper study, or
# everything -- and can each be checked against a real run.
#
# run-c6c45a3e91b2 (automotive: 273 discovered, 86 floor-passing, 4,614
# videos, ~3h of real pipeline work once its bugs are removed) is a
# Standard. It is the reference the three are scaled around.
TIERS: list[DepthTier] = [
    DepthTier(
        id="sample",
        label="Sample",
        hours=1,
        tagline="A first look at whether a topic is worth studying",
        description=(
            "One discovery pass on the strongest keywords, fully enriched. "
            "Enough to see the shape of a topic, its biggest channels and "
            "its obvious sub-niches before committing to a longer run. "
            "Small on purpose: every channel it returns is complete."
        ),
        est_brightdata_usd=0.60,
        est_openrouter_usd=1.50,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 1,
            "MAX_TREE_DEPTH": 1,
            "MAX_BRANCHES": 1,
            "KEYWORD_QUERIES_PER_ROUND": 4,
            "KEYWORD_RESULTS_PER_QUERY": 25,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 5,
            "BRIGHTDATA_RECORD_BUDGET": 400,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 1500,
            "BUDGET_LIMIT_USD": 3.0,
        },
    ),
    DepthTier(
        id="standard",
        label="Standard",
        hours=4,
        tagline="Enough coverage to compare sub-niches against each other",
        description=(
            "Covers a topic's main sub-niches with enough channels in each "
            "for the comparisons the workbook is built for -- size bands, "
            "cohorts, success and failure factors. The default, and the "
            "depth the reference automotive run was measured at."
        ),
        est_brightdata_usd=2.25,
        est_openrouter_usd=4.50,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 4,
            "MAX_TREE_DEPTH": 2,
            "MAX_BRANCHES": 4,
            "KEYWORD_QUERIES_PER_ROUND": 12,
            "KEYWORD_RESULTS_PER_QUERY": 50,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 20,
            "BRIGHTDATA_RECORD_BUDGET": 1500,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 8000,
            "BUDGET_LIMIT_USD": 9.0,
        },
    ),
    DepthTier(
        id="deep",
        label="Deep",
        hours=10,
        tagline="The long tail, not just the obvious channels",
        description=(
            "Pushes past the point where head keywords stop returning "
            "anything new, into adjacent sub-niches and the smaller "
            "channels underneath the well-known ones. For a vertical you "
            "intend to say you have mapped rather than sampled."
        ),
        est_brightdata_usd=6.00,
        est_openrouter_usd=10.50,
        governors={
            "MAX_ROUNDS_PER_BRANCH": 8,
            "MAX_TREE_DEPTH": 4,
            "MAX_BRANCHES": 8,
            "KEYWORD_QUERIES_PER_ROUND": 24,
            "KEYWORD_RESULTS_PER_QUERY": 100,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 50,
            "BRIGHTDATA_RECORD_BUDGET": 4000,
            "YOUTUBE_QUOTA_BUDGET_PER_RUN": 25000,
            "BUDGET_LIMIT_USD": 22.0,
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

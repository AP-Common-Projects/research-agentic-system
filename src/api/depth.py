"""Research depth tiers -- the client-facing "how deep should this run go".

A tier is a named bundle of the same cost governors HarnessConfig already
exposes (see PROFILES in src/config.py). It is not a new execution mode:
launching a run with a tier sets those governors for that run and nothing
else changes.

The arithmetic runs in one direction now: a tier states how many channels
it delivers, and its duration and cost are derived from that. It used to
run the other way -- hours in, channels out -- and the number that came
out was the wrong population. MAX_CHANNELS_PER_RUN bounded HYDRATED
channels while the card quoted the workbook, and only about a fifth of
hydrated channels clear the client's 50k subscriber floor. A Standard run
capped at 100 hydrated 100, cleared the floor with 24, and delivered 17
rows against a promise of 100.

Every figure below is measured, and the run it came from is named. The
reference is run-dd2dbdbc3080, a 4-hour Standard on "education"
(2026-09-08): 8,011 seconds wall clock, one discovery round, 100 hydrated
channels, 24 over the floor, 1,710 video titles described, $1.30 spent
against a $9 ceiling.

That last pair is the other half of the story. The run ended on the clock
with 86% of its money unspent, because every per-channel stage was a
serial loop around one LLM call -- 6,584 of its 8,217 node-seconds were a
socket wait. src/llm/concurrent.py runs them several at a time, which is
what makes 250 and 500 channel tiers arithmetically possible at all.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from src.tools.deliverable import (
    DISCOVERY_YIELD,
    _FLOOR_YIELD,
    _FLOOR_YIELD_PESSIMISTIC,
    _SCOPE_YIELD,
    _SCOPE_YIELD_PESSIMISTIC,
    hydration_ceiling,
)


# ---------------------------------------------------------------------------
# Measured throughput. Every constant here came off a clock, on a named run.
# ---------------------------------------------------------------------------

#: Seconds of LLM latency one DELIVERED channel costs, summed across every
#: per-channel stage, if those stages run one call at a time. Measured on
#: run-dd2dbdbc3080:
#:
#:     classify_channel                 18.3s  (1,192s / 65)
#:     extract_success_failure_factors  51.3s  (2,872s / 56)
#:     populate_taxonomy_dimensions     14.3s  (717s / 50)
#:     populate_shared_fields            9.5s  (474s / 50)
#:     resolve_first_video_date          1.4s  (93s / 65)
#:     describe_video_titles            45.0s  (0.78s a video x 58)
#:                                     ------
#:                                     139.8s
#:
#: For $0.0176 of tokens. This is latency, not work.
_SECONDS_PER_DELIVERED_CHANNEL_SERIAL = 140.0

#: Speed-up from running those calls concurrently, against
#: llm_concurrency = 8. Deliberately not 8: the prompt-building and the
#: database writes either side of each call stay serial, the last partial
#: batch of every stage runs at less than full width, and a 429 costs a
#: backoff.
#:
#: Measured against the live provider on 2026-09-08, and the SUSTAINED
#: figure is the one that matters. Short bursts look much better than the
#: pipeline will ever see:
#:
#:     8 calls,   8 workers, burst        5.0x
#:    16 calls,   8 workers, burst        5.7x
#:    32 calls,  16 workers, burst        8.7x
#:    48 calls,  24 workers, burst       13.8x
#:    48 calls,  16 workers, sustained    6.0x then 3.1x
#:
#: The provider throttles under load, so sixteen workers is not better
#: than eight once a stage runs for minutes rather than seconds -- the
#: second sustained trial was worse than eight workers had been. Four is
#: below the sustained floor observed, which is where a tier's arithmetic
#: has to sit. Raising llm_concurrency will not move this number; only a
#: new sustained measurement should.
#:
#: Two whole nodes ran against the live store at width eight:
#: describe_video_titles wrote 232 descriptions in 19.4s (0.08s a title
#: against 0.78s serial) and populate_taxonomy_dimensions filled 12
#: channels in 63.5s (5.3s each against 14.3s), both with zero errors.
_EFFECTIVE_CONCURRENCY = 4.0

#: Wall clock hydrate_metadata spends per channel: 325s for 100 channels
#: across four rounds on run-dd2dbdbc3080. This is the cost paid for every
#: channel discovery hands over, most of which will not clear the floor --
#: which is exactly why the two ceilings had to be separated.
_SECONDS_PER_HYDRATED_CHANNEL = 3.3

#: One discovery round -- keyword_search, graph_walk, breakout_scanner and
#: new_channel_discovery together. run-dd2dbdbc3080's single round took
#: 852s, spent 717 Bright Data records, and returned 593 channels
#: (199 + 2 + 15 + 409, with new_channel_discovery finding most of them).
#:
#: A round is the unit that matters here, not a keyword: the four nodes
#: poll one set of Bright Data snapshots together, so their latency is
#: shared rather than additive.
_SECONDS_PER_DISCOVERY_ROUND = 852.0
_CHANNELS_PER_DISCOVERY_ROUND = 593
_RECORDS_PER_DISCOVERY_ROUND = 717
#: keyword_search + graph_walk LLM spend on that round: $0.2985 + $0.0855.
_USD_PER_DISCOVERY_ROUND = 0.39

#: YouTube quota one hydrated channel costs: 333 units for 100 channels.
#: Five keys are configured, each with its own 10,000-unit daily
#: allowance, so the pool is 50,000 -- the ceilings below stay well inside
#: one day's worth.
_QUOTA_PER_HYDRATED_CHANNEL = 3.3

#: OpenRouter spend per delivered channel, from the same run: classify
#: $0.0017, factors $0.0027, taxonomy and shared fields ~$0.006 each,
#: descriptions $0.0012. The run's $1.30 total reconciles to this at 50
#: enriched channels plus one discovery round, which is what it did.
_USD_PER_DELIVERED_CHANNEL = 0.0176

#: Work that happens once regardless of channel count: taxonomy build (17s
#: measured), branch compaction (31s), export and the completeness gate's
#: own set-up. Deliberately NOT the discovery phase -- that is counted per
#: round above, and counting it twice shortened every tier.
_FIXED_OVERHEAD_SECONDS = 600.0

#: Videos held per floor-passing channel: 145,694 videos across the 2,516
#: channels in the store that clear 50k subscribers.
_VIDEOS_PER_CHANNEL = 58

#: The funnel's two narrowings, at the worst rates a tier is sized to
#: survive. DISCOVERY_YIELD is what a tier is sized to DELIVER at; these
#: are the rates its window still has to cover its stated MINIMUM at. A
#: tier that only holds its band on a good topic does not hold its band.
#:
#: Both live in src/tools/deliverable.py with the measurements behind
#: them, so the governor and the estimate cannot use different numbers.

#: Durations carry this margin: a tier that fits only if every stage hits
#: its average is a tier that overruns whenever one does not.
_SAFETY_MARGIN = 0.82

#: Quoted cost carries this multiple. Under-quoting strands a run
#: mid-flight; over-quoting only makes a tier lock earlier than it must.
_COST_MARGIN = 1.5

#: The completeness gate runs AFTER the graph, on its own budget, and the
#: cards never counted it. Measured on the two sample runs of 2026-09-07:
#: the graph stopped on time at 57 and 61 minutes against its 60, and the
#: gate then took a further 25 and 30 -- so a card saying "1h" described 82
#: and 91 minutes of waiting.
#:
#: Expressed as a share of ONE ENRICHMENT PASS, not of the research window.
#: A share of the window was defensible while the window was mostly
#: enrichment; it is not now that a Deep tier spends five hours hydrating,
#: because the gate does not discover and does not hydrate -- it heals
#: columns on channels the run already holds. At 45% of the window a Deep
#: run quoted 7h48m of checking, which describes nothing.
#:
#: Half a pass is what the measurement supports: those 21-channel sample
#: runs spent 25-30 minutes against a full serial pass of ~2,940s, so the
#: gate redid roughly 50-60% of one. Charged against the CONCURRENT
#: per-channel cost, since the gate drives the same nodes.
_GATE_SHARE_OF_ENRICHMENT = 0.5

#: Crime carries a per-VIDEO stage no other vertical runs:
#: populate_crime_metadata classifies each video into the fifteen case-file
#: columns -- crime_type, victim_type, case_status, the footage flags and
#: the rest -- at one mid-tier call per eight videos. Measured on
#: run-0ef0d6792b9b: 50 videos in 236s, so 4.7s each, serial.
#:
#: At 58 videos a channel that is 273 serial seconds on top of the 140
#: every channel costs, which is why a crime run of the same stated length
#: delivers roughly a third of the channels. Quoting the same numbers for
#: both was the reason a one-hour crime run took 2h34m.
_CRIME_SECONDS_PER_VIDEO = 4.7

#: $0.0077 per 8-video batch measured on the healthy path is $0.00096 a
#: video; an end-to-end backfill including retried and halved batches came
#: to $0.00265. This sits between them, and _COST_MARGIN carries the rest.
_CRIME_USD_PER_VIDEO = 0.0018

#: LangGraph supersteps one round costs, measured on run-bc5226fb2e06:
#: twenty rounds consumed exactly the 200-superstep ceiling, so ten each.
#: Twelve here for headroom.
_SUPERSTEPS_PER_ROUND = 12
#: Everything outside the discovery loop -- taxonomy, per-branch clustering
#: and compaction, the write-up chain, finalize.
_SUPERSTEPS_FIXED = 60
#: A tier that fits in the old flat ceiling keeps it, so nothing shrinks.
_MIN_RECURSION_LIMIT = 200


def is_crime_topic(topic: str | None) -> bool:
    """Whether a topic runs the crime case-file stage.

    Matched on the topic text because that is what the console has when it
    renders the cards -- the parent_category is not decided until
    classify_channel runs, long after the client has picked a depth.
    """
    return "crime" in (topic or "").strip().lower()


def _seconds_per_delivered_channel(crime: bool) -> float:
    """Concurrent latency one delivered channel costs, end to end."""
    serial = _SECONDS_PER_DELIVERED_CHANNEL_SERIAL
    if crime:
        serial += _CRIME_SECONDS_PER_VIDEO * _VIDEOS_PER_CHANNEL
    return serial / _EFFECTIVE_CONCURRENCY


def _discovery_rounds(hydrated: int) -> int:
    """Rounds needed to put `hydrated` channels in front of hydration."""
    return max(1, math.ceil(hydrated / _CHANNELS_PER_DISCOVERY_ROUND))


def _research_seconds(
    delivered: int,
    floor_yield: float = _FLOOR_YIELD,
    scope_yield: float = _SCOPE_YIELD,
    crime: bool = False,
) -> float:
    """Wall clock to put `delivered` rows in the workbook.

    THREE populations, three rates, which is the whole correction. The run
    DISCOVERS in rounds; HYDRATES everything a round hands over, at 3.3
    seconds each; ENRICHES the fraction that clears the subscriber floor,
    at 35 seconds each; and DELIVERS the fraction of those the export's
    category scope keeps.

    Enrichment is charged against the floor-passing count, not the
    delivered count, because that is what the nodes actually run on -- a
    channel dropped by the scope was still classified, described and
    factored first. Charging it at the delivered count is how a model
    under-quotes by a third.
    """
    enriched = math.ceil(delivered / max(1e-6, scope_yield))
    hydrated = math.ceil(enriched / max(1e-6, floor_yield))
    return (
        _FIXED_OVERHEAD_SECONDS
        + _discovery_rounds(hydrated) * _SECONDS_PER_DISCOVERY_ROUND
        + hydrated * _SECONDS_PER_HYDRATED_CHANNEL
        + enriched * _seconds_per_delivered_channel(crime)
    )


def _channels_in(seconds: float, crime: bool) -> int:
    """The inverse: how many channels fit in a window of `seconds`.

    Used only for crime, where the tier holds its stated duration and
    lets the channel count fall instead -- a small honest tier beats a
    larger one that cannot finish.

    Searched against _research_seconds rather than solved in closed form.
    The closed form has to guess the round count before it knows the
    channel count, and guessing one round left crime Standard 813 seconds
    over its own window: it needs two. Inverting the real function cannot
    disagree with it.
    """
    budget = seconds * _SAFETY_MARGIN
    fits = lambda n: _research_seconds(n, crime=crime) <= budget
    lo, hi = 5, 5
    while fits(hi * 2):
        hi *= 2
        if hi > 100_000:
            break
    hi *= 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return max(5, lo)


@dataclass
class DepthTier:
    id: str
    label: str
    tagline: str
    description: str
    #: Floor-passing channels this tier aims to put in the workbook. This
    #: IS max_channels_per_run, and it now means what the card says.
    target_channels: int
    #: The bottom of the stated band. Not decoration: the tier's window and
    #: budgets are sized to reach this many even at _YIELD_PESSIMISTIC.
    min_channels: int
    # Governor overrides handed to the run as env, mirroring PROFILES keys.
    governors: dict[str, Any] = field(default_factory=dict)
    #: Set by for_topic(). Crime runs a per-video stage no other vertical
    #: does, so the same hours buy materially fewer channels.
    crime: bool = False
    # Derived in __post_init__ and kept as FIELDS rather than properties so
    # dataclasses.asdict() still carries them into the API payload -- the
    # depth cards rendered blank the last time a derived value became a
    # property and silently left the response.
    hours: float = 0.0
    est_brightdata_usd: float = 0.0
    est_openrouter_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.crime:
            # Crime holds the tier's duration and gives up channels. The
            # ratio between the band's ends is preserved, so "at least"
            # keeps meaning the same share of "up to".
            ratio = self.min_channels / self.target_channels if self.target_channels else 0.8
            self.target_channels = _channels_in(self.hours * 3600, True)
            self.min_channels = max(1, int(self.target_channels * ratio))
        else:
            self.hours = self._hours()

        rounds = self.record_rounds
        # Derived from the target, never written by hand: this is the
        # ceiling that makes the tier's own name true, and a tier whose
        # deadline disagreed with its label would be exactly the bug this
        # fixes.
        self.governors["RUN_DEADLINE_SECONDS"] = int(self.hours * 3600)
        # The promise about the FILE.
        self.governors["MAX_CHANNELS_PER_RUN"] = self.target_channels
        # And the ceiling on what may be spent reaching it. These were one
        # number; they are two populations.
        self.governors["MAX_HYDRATED_CHANNELS_PER_RUN"] = self.hydration_ceiling
        self.governors["BRIGHTDATA_RECORD_BUDGET"] = rounds * _RECORDS_PER_DISCOVERY_ROUND
        self.governors["YOUTUBE_QUOTA_BUDGET_PER_RUN"] = self.quota_budget
        # Bounded and stated, rather than a fixed 30 minutes bolted onto
        # whatever the run took. The client is told this time up front now.
        self.governors["GATE_BUDGET_SECONDS"] = self.gate_budget_seconds
        self.governors["GRAPH_RECURSION_LIMIT"] = self.recursion_limit

        self.est_brightdata_usd = round(
            self.governors["BRIGHTDATA_RECORD_BUDGET"]
            * _brightdata_cost_per_record(),
            2,
        )
        # Charged on the ENRICHED count: a channel the export's scope later
        # drops was still classified, described and factored, and its
        # tokens were still bought.
        enriched = math.ceil(self.target_channels / _SCOPE_YIELD)
        self.est_openrouter_usd = round(
            (
                rounds * _USD_PER_DISCOVERY_ROUND
                + enriched * self._usd_per_delivered_channel()
            )
            * _COST_MARGIN,
            2,
        )
        self.governors["BUDGET_LIMIT_USD"] = _budget_ceiling(self.est_openrouter_usd)

    # -- sizing -------------------------------------------------------------

    def _hours(self) -> float:
        """Long enough for the top of the band, and for the bottom of it
        on a topic that yields badly. Rounded up to the quarter hour."""
        seconds = max(
            _research_seconds(self.target_channels, crime=self.crime),
            _research_seconds(
                self.min_channels,
                _FLOOR_YIELD_PESSIMISTIC,
                _SCOPE_YIELD_PESSIMISTIC,
                self.crime,
            ),
        )
        return math.ceil(seconds / _SAFETY_MARGIN / 900) * 900 / 3600

    def _usd_per_delivered_channel(self) -> float:
        usd = _USD_PER_DELIVERED_CHANNEL
        if self.crime:
            usd += _CRIME_USD_PER_VIDEO * _VIDEOS_PER_CHANNEL
        return usd

    @property
    def max_channels(self) -> int:
        """Floor-passing channels in the finished workbook.

        The name kept, the population corrected. This used to be the
        hydration cap, and the two differ by about five times.
        """
        return self.target_channels

    @property
    def hydration_ceiling(self) -> int:
        """Channels the run may hydrate reaching that target."""
        return hydration_ceiling(self.target_channels)

    @property
    def record_rounds(self) -> int:
        """Discovery rounds the record budget pays for.

        Sized on the PESSIMISTIC yield and the band's minimum, plus one:
        the run stops discovering as soon as it holds the target, so extra
        rounds here cost nothing on a good topic and are the difference
        between reaching the band and missing it on a poor one.
        """
        enriched = math.ceil(self.min_channels / _SCOPE_YIELD_PESSIMISTIC)
        hydrated = math.ceil(enriched / _FLOOR_YIELD_PESSIMISTIC)
        return _discovery_rounds(max(hydrated, self.hydration_ceiling)) + 1

    @property
    def quota_budget(self) -> int:
        """YouTube units, rounded up to a round number for legibility."""
        needed = self.hydration_ceiling * _QUOTA_PER_HYDRATED_CHANNEL * 1.2
        return int(math.ceil(needed / 500) * 500)

    def for_topic(self, topic: str | None) -> "DepthTier":
        """This tier as it applies to `topic`.

        Crime is the only vertical that changes the arithmetic today.
        """
        if self.crime or not is_crime_topic(topic):
            # Already adjusted: re-deriving from the reduced target would
            # shrink the tier a second time.
            return self
        # dict(governors), not the tier's own: dataclasses.replace copies
        # the REFERENCE, and __post_init__ writes into whatever dict it is
        # handed. Sharing it meant one crime lookup rewrote the
        # module-level tier's cap for every later caller, including
        # non-crime runs in the same process.
        return replace(self, crime=True, governors=dict(self.governors))

    # -- what the card says -------------------------------------------------

    @property
    def est_channels(self) -> str:
        """The band, both ends. "up to 250" alone was the number that went
        wrong quietly; a floor beside it is a claim that can be checked."""
        return f"{self.min_channels:,}–{self.target_channels:,}"

    @property
    def est_videos(self) -> str:
        return (
            f"{self.min_channels * _VIDEOS_PER_CHANNEL:,}–"
            f"{self.target_channels * _VIDEOS_PER_CHANNEL:,}"
        )

    @property
    def est_total_usd(self) -> float:
        return round(self.est_brightdata_usd + self.est_openrouter_usd, 2)

    @property
    def recursion_limit(self) -> int:
        """Supersteps the graph may run, derived from this tier's own loop.

        It was a flat 200 for every depth, and the deeper tiers are
        configured to need more: Standard is 4 branches x 4 rounds and Deep
        is 8 x 8, against Sample's 1 x 1. A Standard education run died on
        GraphRecursionError after 2h24m with no export at all -- the
        ceiling was reached before any stop condition could be.
        """
        rounds = (
            int(self.governors.get("MAX_BRANCHES", 1))
            * int(self.governors.get("MAX_ROUNDS_PER_BRANCH", 1))
        )
        needed = rounds * _SUPERSTEPS_PER_ROUND + _SUPERSTEPS_FIXED
        # Half again, because a branch that saturates early and one that
        # runs its full allowance cost different amounts, and the ceiling
        # has to fit the unlucky arrangement rather than the average.
        return max(_MIN_RECURSION_LIMIT, int(needed * 1.5))

    @property
    def gate_budget_seconds(self) -> int:
        """Wall clock the completeness gate may spend after the graph.

        A ceiling, not a plan: a run whose columns are already full leaves
        the gate nothing to do and it finishes in minutes. The card says
        "up to" for that reason.
        """
        enriched = math.ceil(self.target_channels / _SCOPE_YIELD)
        return int(
            enriched
            * _seconds_per_delivered_channel(self.crime)
            * _GATE_SHARE_OF_ENRICHMENT
        )

    @property
    def total_duration_label(self) -> str:
        """What the client actually waits: research plus the checks.

        The headline on the card. Quoting the research window alone was
        true of the graph and false of the wait, which is the only figure
        a client can act on.
        """
        total = self.hours * 3600 + self.gate_budget_seconds
        hours, rem = divmod(int(total), 3600)
        minutes = rem // 60
        if hours and minutes:
            return f"~{hours}h {minutes}m"
        if hours:
            return f"~{hours}h"
        return f"~{minutes}m"

    @property
    def gate_label(self) -> str:
        minutes = self.gate_budget_seconds // 60
        if minutes < 60:
            return f"up to {minutes}m"
        h, m = divmod(minutes, 60)
        return f"up to {h}h {m}m" if m else f"up to {h}h"

    @property
    def duration_label(self) -> str:
        """How long the tier runs, in the largest unit that stays whole."""
        if self.hours < 1:
            return f"{round(self.hours * 60)}m"
        return f"{self.hours:g}h"


def _brightdata_cost_per_record() -> float:
    try:
        from src.config import get_config

        return float(get_config().harness.brightdata_cost_per_record_usd)
    except Exception:
        return 0.0015


def _budget_ceiling(quoted: float) -> float:
    """The harness circuit breaker, above the quote and rounded up.

    It must never sit UNDER the figure the client was shown, or the run
    stops on money the client was told it had.
    """
    return float(math.ceil(quoted * 1.35))


# Ordered shallowest to deepest; the UI renders them in this order.
#
# Three, not seven. The old ladder was seven guesses at the same unknown:
# every tier shared one per-channel model, so when that model turned out to
# be four times optimistic, all seven were wrong together and the client had
# six ways to pick the wrong one. Three tiers, each sized from measured
# throughput and each enforced by a deadline, a delivery target and a
# hydration ceiling, answer the only question a client actually has -- a
# look, a proper study, or everything.
TIERS: list[DepthTier] = [
    DepthTier(
        id="sample",
        label="Sample",
        target_channels=50,
        min_channels=40,
        tagline="A first look at whether a topic is worth studying",
        description=(
            "One discovery pass on the strongest keywords, fully enriched. "
            "Enough to see the shape of a topic, its biggest channels and "
            "its obvious sub-niches before committing to a longer run. "
            "Small on purpose: every channel it returns is complete."
        ),
        governors={
            "MAX_ROUNDS_PER_BRANCH": 1,
            "MAX_TREE_DEPTH": 1,
            "MAX_BRANCHES": 1,
            "KEYWORD_QUERIES_PER_ROUND": 6,
            "KEYWORD_RESULTS_PER_QUERY": 50,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 8,
        },
    ),
    DepthTier(
        id="standard",
        label="Standard",
        target_channels=250,
        min_channels=200,
        tagline="Enough coverage to compare sub-niches against each other",
        description=(
            "Covers a topic's main sub-niches with enough channels in each "
            "for the comparisons the workbook is built for -- size bands, "
            "cohorts, success and failure factors. The default, and the "
            "depth the reference automotive run was measured at."
        ),
        governors={
            "MAX_ROUNDS_PER_BRANCH": 4,
            "MAX_TREE_DEPTH": 2,
            "MAX_BRANCHES": 4,
            "KEYWORD_QUERIES_PER_ROUND": 12,
            "KEYWORD_RESULTS_PER_QUERY": 50,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 20,
        },
    ),
    DepthTier(
        id="deep",
        label="Deep",
        target_channels=500,
        min_channels=450,
        tagline="The long tail, not just the obvious channels",
        description=(
            "Pushes past the point where head keywords stop returning "
            "anything new, into adjacent sub-niches and the smaller "
            "channels underneath the well-known ones. For a vertical you "
            "intend to say you have mapped rather than sampled."
        ),
        governors={
            "MAX_ROUNDS_PER_BRANCH": 8,
            "MAX_TREE_DEPTH": 4,
            "MAX_BRANCHES": 8,
            "KEYWORD_QUERIES_PER_ROUND": 24,
            "KEYWORD_RESULTS_PER_QUERY": 100,
            "GRAPH_WALK_FRONTIER_PER_ROUND": 50,
        },
    ),
]



TIERS_BY_ID: dict[str, DepthTier] = {t.id: t for t in TIERS}


def get_tier(tier_id: str, topic: str | None = None) -> DepthTier | None:
    """The tier, adjusted for the topic it will be run on.

    `topic` matters because crime runs a per-video stage no other vertical
    does. Passing it here is what makes the governors the run receives the
    same ones the client was shown on the card.
    """
    tier = TIERS_BY_ID.get((tier_id or "").strip().lower())
    return tier.for_topic(topic) if tier is not None else None


def _provider(balances: dict[str, Any], name: str) -> dict[str, Any]:
    for row in balances.get("providers", []):
        if row.get("provider") == name:
            return row
    return {}


def tiers_with_availability(
    balances: dict[str, Any], topic: str | None = None
) -> list[dict[str, Any]]:
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
    for base in TIERS:
        # Adjusted before affordability is judged: a crime run costs more
        # and delivers fewer channels, and locking a depth against the
        # wrong figure would be worse than not checking at all.
        tier = base.for_topic(topic)
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
        row["total_duration_label"] = tier.total_duration_label
        row["recursion_limit"] = tier.recursion_limit
        row["gate_label"] = tier.gate_label

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

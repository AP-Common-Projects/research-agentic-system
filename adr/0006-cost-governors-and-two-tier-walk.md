# ADR-0006: Cost governors and a two-tier graph walk

**Status:** Accepted
**Date:** 2026-08-14

## Context
Rebuilding `bright_data.py` against Bright Data's real API (trigger → poll →
snapshot, three per-record-billed collectors) made the pipeline's spend
observable for the first time, and it was unbounded in four places:

- `keyword_search` issued `len(keywords) × 6` discovery jobs per round with no
  `limit_per_input`. Measured live, one uncapped keyword returned **469
  records**; `limit_per_input=3` returned exactly 3. That fan-out is ~14,000
  records and ~$21 **per round**.
- `graph_walk` expanded the entire frontier every round, and the frontier grows.
- Nothing capped rounds, tree depth, or branch count.
- `budget_spent_usd` was incremented only by the three LLM nodes, so the
  circuit breaker could not see Bright Data records or YouTube quota — the two
  resources that can actually be exhausted. Both API clients are constructed
  fresh per round, so their instance counters reset and the 90%-of-ceiling
  guard only ever saw one round's usage.

Two measurements reshaped the design. Across 469 live discovery results the
median channel had **19 subscribers** and two thirds had under 100. And
`featured_channels` — the field that actually carries channel→channel edges —
appears on 1% of sub-100-subscriber channels versus 18% of 100k+ ones.
(`Links`, which we had assumed was the edge source, holds external websites.)

## Decision

**Two-tier graph walk.** Tier A fetches the channel record and reads
`featured_channels` off it: the edges ride along inside a record already paid
for, so expansion costs **1 record per channel**. Tier B — recent videos plus
comment-author mining — costs `1 + V + V·C` and fires only for channels Tier A
yielded nothing from. It is disabled entirely in the `smoke` profile.

**Rank and filter the frontier by subscriber count**, not just truncate it.
Expanding the long tail spends records and returns no edges. Channels below
`min_subscribers_for_expansion` are still hydrated and scored — they are simply
not traversed *from*. Taxonomy seeds, which arrive as bare handles with no
known subscriber count, are exempt; filtering them would leave round one with
nothing to do.

**Traversal keys on channel refs, not UC ids.** `featured_channels` entries
carry a URL and handle but no UC id, and taxonomy seeds are `@handle` strings.
The UC id only exists once the channel record has been fetched — which is the
same call that expands it, so resolving ids costs nothing extra.
`discovery_edges` therefore stores `target_channel_ref` as the always-present
identity and backfills `target_channel_id` on re-walk.

**Profiles (`smoke` / `bounded` / `full`) drive every governor**, with
precedence `explicit env var > profile preset > field default`. Presets are
filtered against `os.environ` because pydantic-settings ranks init kwargs
*above* env vars — an unfiltered preset would silently outrank a deliberate
override.

**`check_saturation` owns the round counter.** The discovery tracks cannot: when
one throws, the `_guarded` wrapper returns an error dict carrying no counter
update, and a failed round also produces no novelty history and no exhaustion
flag. A branch whose tracks both fail could therefore never satisfy any stop
condition. `check_saturation` runs exactly once per round regardless, so it is
the only placement that always holds.

## Consequences

Saturation remains the intended stop condition (master plan first principle
#2); these are circuit breakers. A run that stops on a governor is a run to
investigate, so each stop reason is logged distinctly — `max_rounds_per_branch`
and `governor:brightdata_record_budget` must never be mistakable for
`both_tracks_exhausted`. `0` means uncapped, which is how the `full` profile is
expressed.

Tier B being off in `smoke` means the first live run will not exercise
comment-author edges. Accepted: that path is the expensive one and it should
not debut on the run whose job is to prove the cheap path works.

Ranking by subscribers biases discovery toward established channels, which is
in tension with finding *emerging* niches. Mitigated by the threshold applying
only to traversal, never to hydration or scoring — small channels still reach
the store, the signals, and the report. Revisit if reports skew toward
incumbents.

The record budget binds per run, not per month. Nothing in the harness tracks
cumulative spend across runs against the 5,000-record free tier; that is
currently a human check.

## Alternatives considered

**Comment mining as the default edge source**, per the original design. Rejected
once `featured_channels` was found to be free — 34 records per channel versus 1
for the same class of edge.

**A global record ceiling only, with no per-node caps.** Rejected because a
single runaway branch would consume the whole run's allowance before any other
branch was touched, and the failure would look like budget exhaustion rather
than the frontier bug it actually was.

**Making the governors advisory (log-and-continue).** Rejected outright. The
entire point is that a bug cannot spend the allowance; a warning that a
runaway loop reads and ignores is not a control.

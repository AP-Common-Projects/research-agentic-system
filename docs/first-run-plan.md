# First End-to-End Run — Preparation and Cost-Controlled Test Plan

**Date:** 2026-08-13
**Goal:** get from "all credentials verified" to a real, completed research run on
**Finance**, then **Legal**, without burning credits on a bug we could have caught for free.

**Governing principle:** every rung of the ladder is strictly more expensive than the one
below it, and each rung has a gate you must pass before climbing. Nothing is capped by
discipline — every cap in this plan is enforced in code, because a runaway loop does not
read documentation.

---

## Status (2026-08-14)

| Rung | Gate | State |
|---|---|---|
| 00 Environment | suite green | **CHECKED** — 386 passing, 14 files |
| 01 Client rewrite | mocked tests on recorded fixtures | **CHECKED** — `production-auditor` ran; both HIGH findings fixed |
| 02 Governors | caps proven + six-point review passes | **CHECKED** — review returned PASS on all six; its one config blocker fixed |
| 03 Replay | full run → graded report, stops on a reason | **CHECKED** — exceeded: stops on `novelty_below_threshold` |
| 04 Live smoke | ≤150 records, report, a graph-walk-exclusive channel | **CHECKED** — 95 records, $0.14, machinery proven live |
| 05 Finance bounded | report reviewed, spend logged | **re-running** — first attempt's signals were dead |
| 06 Legal bounded | — | not started |

### Rung 04: what it proved, and what it did not

Its gate was met — the run completed under budget and produced a graded
report, which was its actual job: demonstrating that the pipeline works end to
end against live APIs. That stands.

**Its headline claim does not.** Rung 04 was reported as finding 7 channels
reachable only by the graph walk, averaging 12.7x the subscribers of the
keyword-found set. Two of the four named — @GrahamStephan and @AndreiJikh —
were taxonomy seeds the LLM guessed, credited to the walk because round one's
frontier *is* the seed list. The remaining two cannot now be checked: `NodeLog`
truncates `llm_output` at 500 characters, so the branch seeds are not in the
log, and the store was cleared before rung 05.

So rung 04's discovery claim is **unverifiable, not disproven**. The seed
attribution bug is fixed and rung 05 gives the first clean measurement. The
lesson worth keeping is that the number was quoted repeatedly as evidence for
the project's central thesis before anyone checked what fed it.

Rung 03 now clears its gate by more than it was written to require. The plan
asked only that a run terminate on *a* stop reason rather than an exception;
with the round cap lifted, both branches terminated on
`novelty_below_threshold` — the project's actual stated stop condition, working
for the first time:

```
root                        1.0 → 0.0 → 0.0 → 0.0  → saturated: novelty_below_threshold
personal-finance-budgeting  0.66 → 0.0 → 0.0 → 0.0 → saturated: novelty_below_threshold
```

### Honest residual on "checked"

Two subagent passes ran and their findings are fixed, but **neither reviewer has
seen the fixes made in response to its own final report**. Specifically:

- The pre-spend budget layer (`src/tools/budget.py`) and the paid-call failure
  guards were written after the production audit and are verified by their own
  tests and by me — not by an independent pass.
- `harness-eval-runner` never ran; its deliverable (the fixture/replay layer)
  was built directly.
- The reviewer asked for a *load-time* assertion on
  `max_rounds_per_branch > saturation_consecutive_window`; what exists is a
  test. That catches a regression in CI, not at runtime.

This is the same shape as the bug that survived a green suite for the whole
project (mocked cursors cannot type-check SQL), so it is recorded rather than
waved through. It is a judgement that the remaining risk is small and bounded,
not a claim that the risk is zero.

### Deliberately not done, from the production audit

- **structlog is never configured**, so `brightdata_triggered` /
  `brightdata_collected` — the only place `snapshot_id` appears — render to
  stdout and vanish. There is no durable handle for reconciling against the
  vendor's bill. The JSONL sink carries counts but not snapshot ids.
- `BRIGHTDATA_MODE` defaults to `live`. For a per-record-billed API on an
  already-overrun account, `replay` is the fail-safe default.
- The concurrency semaphore is per-instance and a fresh client is built every
  round, so no global limit exists. Harmless at current fan-out, but it does
  not do what it appears to.

---

Earlier status text, kept for the record: implemented and verified, 343 passing.

The replay rung paid for itself immediately. Six bugs were found by running the
whole graph on recorded data, and **five of them would have burned live records
before showing themselves**:

| # | Bug | Why it mattered |
|---|---|---|
| 1 | `saturated_branches` doubled every round — a writer returned the accumulated list into an appending reducer | `[X]+[X]=[X,X]`, then 4, 8, 16. OOM-killed the run. Latent at `recursion_limit=25`; fatal at 100 |
| 2 | A branch whose tracks both failed could never satisfy any stop condition | No novelty history, no exhaustion flag, no round counter → looped to the recursion limit and died by exception instead of producing a report |
| 3 | `PostgresSaver.setup()` runs `CREATE INDEX CONCURRENTLY` inside a transaction | Checkpoint tables had never been created. Every run died at startup |
| 4 | The pipeline runs `ainvoke` but used the **synchronous** checkpointer | `aget_tuple` raises `NotImplementedError` — the run died on the first superstep, before any node executed |
| 5 | `COALESCE(NULLIF($n,''), NOW())` — `text` vs `timestamptz` | **The store had never persisted a single channel or video.** One try block wrapped the whole loop, so the first failure discarded all 22 channels and 646 videos of a round, logged `hydration_errors: 1`, and let the run continue. Synthesis then queried an empty store and produced an empty report — a run that looked successful and contained nothing |
| 6 | `compact_branch` looked channels up by `seed_channel_ids` (`@handles`, not UC ids) | Compacted an empty branch even with a fully populated store |

Bug 5 is the one worth dwelling on: every persistence test drives a `MagicMock`
cursor, which accepts any string as SQL. Mocks cannot catch a type mismatch.
`tests/test_store_integration.py` now executes real SQL against real Postgres
and skips when none is reachable.

Also fixed: `discovery_edges` was never written at all (`persist_edge` existed
and nothing called it), and its CHECK constraint rejected the new edge types.
Both closed — a replay run now writes 38 real edges.

---

## 0. Where we actually are

Verified live today, not assumed:

| Thing | Status |
|---|---|
| Postgres | Reachable, `ping()` in 0.02s |
| OpenRouter — 4 model slugs | Verified against live catalog |
| YouTube Data API v3 key | Set, loaded by config |
| Bright Data account + token | Verified (Datasets + Unlocker share one token) |
| Python environment | **Was missing entirely** — rebuilt today (`.venv`, Python 3.12) |
| Test suite | **294 passed** in the rebuilt env |
| `src/tools/bright_data.py` | **Fiction.** Every endpoint it calls does not exist |
| Bright Data credentials in `.env` | **Still placeholders** |
| Iteration / depth / record caps | **None exist anywhere in the codebase** |

So: the infrastructure is real, and the discovery layer is not. That is the whole gap.

### New finding — the search gap is already solved by the vendor

`keyword_search` needs to turn a keyword into channels, and the three collectors on the
account (Channels, Videos posts, Comments) all take a *URL*, not a query. That looked like
a missing capability. It isn't — both scrapers support a discovery mode:

```
POST /datasets/v3/trigger?dataset_id=<id>&type=discover_new&discover_by=keyword
     &limit_per_input=<N>
Body: [{"keyword": "index fund investing"}]
→ {"snapshot_id": "sd_..."}
```

Confirmed working today on both `gd_lk538t2k2p1k3oos71` (Channels) and
`gd_lk56epmy2i5g7lzu0k` (Videos posts). `limit_per_input` is accepted, which is the single
most important cost lever in this whole document. No YouTube `search.list` fallback needed,
and no deviation from ADR-0001's tool stack.

**Caveat measured, not guessed:** keyword-discovery jobs run for *minutes*, not the ~5s that
by-URL collection takes. The client must be poll-with-backoff and must fan queries out
concurrently, or a single round takes hours.

**And the cap is not optional.** Measured today, same collector, same shape of query:

| Trigger | Records returned | Cost |
|---|---|---|
| `keyword=index fund investing`, `limit_per_input=3` | **exactly 3** | $0.005 |
| `keyword=personal finance`, no limit | **469** | $0.70 |

One uncapped keyword is 9% of the monthly free tier. `keyword_search`'s current
6-qualifiers-per-keyword fan-out would issue ~30 of those per round — **~14,000 records,
$21, per round.** The cap works, it is exact, and nothing may call this endpoint without it.

### Second finding — `graph_walk`'s edge source is free, and it isn't the field we assumed

The Channels record was inspected field by field rather than guessed at:

- **`Links` is external URLs** — `pwlcapital.com`, `x.com/benjaminfelix`,
  `rationalreminder.ca/podcast`. Not channel→channel edges. Useless for graph walk.
- **`featured_channels` is the real channel→channel edge**, structured and complete:
  `{name, subscribers, url, handle}`. This is exactly what `graph_walk` needs.
- **`collaborations`** is a list of video appearances (title, views, publish date) with no
  channel identifiers — a weak signal at best, not an edge source.

The consequence is large: `featured_channels` arrives **as a field on the channel record we
already paid one record for**. Channel→channel expansion therefore costs **1 record per
channel**, not the ~34 assumed when comment-mining was the presumed mechanism. Comment
mining becomes an *escalation path* used only when the cheap edges stop yielding novelty,
not the default.

---

## 1. The four things that will burn credits if we don't fix them first

These are not hypothetical. Each is a specific line of code.

**1. `graph_walk` has no frontier cap.** `crawl_channel_relationships(frontier)` expands
every channel in the frontier, every round, and the frontier grows each round. On the cheap
`featured_channels` path that is 1 record/channel and survivable; on the comment-mining
escalation path it is ~34 records/channel, so an uncapped frontier of 50 is 1,700 records —
a third of the free tier in a single round.

**2. `keyword_search` fans out 6 qualifiers per keyword, every round, with no
`limit_per_input`.** With 5 keywords that is 30 discovery jobs per round, and the measurement
above puts an uncapped job at ~469 records. That is **~14,000 records / $21 per round**, and
at minutes per job, many hours of wall clock. This is the single most dangerous line of code
in the repository right now.

**3. Nothing stops the loop.** If both tracks error, `_guarded` records the error and marks
each branch done; `hydrate_metadata` finds nothing; `check_saturation` sees no novelty
history and no exhaustion flag, so it returns `expand_deeper` — forever. The only thing that
currently stops it is LangGraph's default `recursion_limit=25`, which ends the run in a
`GraphRecursionError` instead of a report. There is no `max_rounds`, no `max_depth`, and
`compact_branch` can propose new nodes indefinitely.

**4. The budget circuit breaker is blind to everything except LLM tokens.**
`budget_spent_usd` is only incremented by `taxonomy.py`, `synthesize.py`, and
`compact_branch.py`. Bright Data records and YouTube quota — the actual dominant cost —
never reach it. Worse, `YouTubeAPIClient` and `BrightDataClient` are constructed *fresh
inside each node call*, so `_quota_used` resets to 0 every round: the 90%-of-ceiling guard
only ever sees a single round's usage.

**The cost model is inverted from what you'd assume.** LLM spend is genuinely small —
a full 5-branch run is roughly $0.47 on DeepSeek V4-Pro pricing. Bright Data records are
the real budget, and unbounded loops are the real risk.

---

## 2. Cost arithmetic (so the caps are chosen, not guessed)

**Bright Data:** $1.50 / 1,000 records = **$0.0015/record**. Free tier: 5,000 records/month.
**YouTube API:** free, 10,000 units/day. Hydration costs 3 units/channel → ~3,300
channels/day. Not a constraint at our scale.
**LLM (OpenRouter, DeepSeek V4-Pro $0.44/$0.87 per 1M):** ~$0.10 synthesis, ~$0.07/branch
compaction, ~$0.002 taxonomy.

Graph walk becomes **two-tier**, which is the main cost saving in this plan:

| Tier | Mechanism | Records / channel |
|---|---|---|
| **A — default** | Channel record → read `featured_channels` | **1** |
| **B — escalation** | + V videos, + V×C comments, mine comment authors | `1 + V + (V×C)` → 13 at V=2/C=5, 34 at V=3/C=10 |

Tier B fires only for channels where Tier A returned no new neighbours, so it is paid on a
shrinking minority of the frontier rather than all of it.

Per-run totals under the three profiles:

| Profile | Keyword records | Graph-walk records | Total | Cost | Free-tier % |
|---|---|---|---|---|---|
| `smoke` | 2 rnd × 4 q × 10 = 80 | 2 rnd × 5 ch × 1 = 10 | **90** | **$0.14** | 1.8% |
| `bounded` | 3 rnd × 6 q × 20 = 360 | 45 tier-A + ~170 tier-B = 215 | **575** | **$0.86** | 12% |
| `full` | uncapped | uncapped | — | — | — |

Budget for both categories: smoke (90) + Finance bounded (575) + Legal bounded (575)
= **~1,240 of 5,000 free records**, comfortably inside the free tier with ~3,700 in reserve.
The hard per-run ceiling is still set to 1,500 so that a bug cannot spend the reserve.

---

## 3. The ladder

### Phase 0 — Environment ✅ done today
Rebuilt `.venv` (Python 3.12), installed `[dev,console,mcp]`, 294 tests green, Postgres
verified. Also: delete the stale `src/omniframes_harness.egg-info` left over from the rename.

**Gate:** `.venv/bin/python -m pytest -q` green. **Cost: $0.**

---

### Phase 1 — Rewrite `bright_data.py` against the real API
**Subagent on duty:** none (pure client work) → `production-auditor` at the end.

Replace the fake synchronous client wholesale:

- `POST /datasets/v3/trigger?dataset_id=…` → `{"snapshot_id"}`, then poll
  `GET /datasets/v3/progress/{id}` until `ready`, then
  `GET /datasets/v3/snapshot/{id}?format=json`. Exponential backoff, hard poll ceiling.
- Three dataset IDs in config, not one: `channels`, `videos`, `comments`.
- `discover_new&discover_by=keyword&limit_per_input=N` for the keyword track.
- `num_of_comments=C` for the comments collector — the empirically confirmed cap
  (`limit` and `max_results` are rejected as unknown fields).
- Real field parsers: Channels (`id`, `identifier`, `handle`, `name`, `subscribers`,
  `videos_count`, `views`, `Description`, `featured_channels`), Videos (`video_id`, `title`,
  `views`, `likes`, `num_comments`, `next_recommended_videos`), Comments (`comment_id`,
  `comment_text`, `user_channel`).
- Edge sources for `graph_walk`, in cost order: **`featured_channels`** (Tier A, free with
  the channel record), then `next_recommended_videos` and comment-author mining (Tier B,
  escalation only). **Not `Links`** — that field is external websites, not channels.
- Every method returns `(payload, records_consumed)` so cost accounting is not optional.
- Concurrent query fan-out with a semaphore, since jobs take minutes.

**Gate:** unit tests against recorded fixtures, all mocked. **Cost: $0.**

---

### Phase 2 — Governors: caps and honest cost accounting
**Subagent on duty:** `langgraph-builder`, then **mandatory** `architecture-reviewer`
six-point review (this touches state fields written by parallel nodes — exactly its trigger
condition).

New config block, all env-overridable, presets selected by `HARNESS_PROFILE`:

```
HARNESS_PROFILE=smoke              # smoke | bounded | full
MAX_ROUNDS_PER_BRANCH=2            # smoke: 2   bounded: 3
MAX_TREE_DEPTH=1                   # smoke: 1   bounded: 2
MAX_BRANCHES=2                     # smoke: 2   bounded: 4
KEYWORD_QUERIES_PER_ROUND=4        # smoke: 4   bounded: 6
KEYWORD_RESULTS_PER_QUERY=10       # smoke: 10  bounded: 20
GRAPH_WALK_FRONTIER_PER_ROUND=5    # smoke: 5   bounded: 15
GRAPH_WALK_ESCALATE_TO_COMMENTS=0  # smoke: off bounded: on   — Tier B gate
GRAPH_WALK_VIDEOS_PER_CHANNEL=2    # smoke: 2   bounded: 3
GRAPH_WALK_COMMENTS_PER_VIDEO=5    # smoke: 5   bounded: 10
BRIGHTDATA_RECORD_BUDGET=150       # smoke: 150 bounded: 1500  — HARD ceiling
BRIGHTDATA_COST_PER_RECORD_USD=0.0015
YOUTUBE_QUOTA_BUDGET_PER_RUN=1000
BUDGET_LIMIT_USD=1.00              # smoke: 1.00  bounded: 5.00
```

State changes (`schema_version` 4 → 5, with a `migrate_state` block):

- `brightdata_records_used: Annotated[int, add]` — accumulating, survives the per-round
  client reconstruction that currently zeroes the counter.
- `youtube_quota_used: Annotated[int, add]` — same fix.
- `rounds_by_node: Annotated[dict[str,int], merge-max]` — per-branch round counter.

Enforcement points:

- `keyword_search` — truncate `broaden_or_pivot` output to `KEYWORD_QUERIES_PER_ROUND`;
  pass `limit_per_input`; add `records × $0.0015` to `budget_spent_usd`.
- `graph_walk` — truncate frontier to `GRAPH_WALK_FRONTIER_PER_ROUND` (keep the remainder
  in `unexpanded_channel_ids` so nothing is lost, just deferred); pass V and C caps; add
  record cost.
- `check_saturation` — three new force-saturate conditions **before** the novelty check:
  round cap hit, record budget hit, quota budget hit. Each logs a distinct reason so
  "stopped because saturated" is never confused with "stopped because capped".
- `select_next_node` — refuse to instantiate a proposed node beyond `MAX_TREE_DEPTH`, or
  beyond `MAX_BRANCHES` total.
- `run_pipeline` — set `recursion_limit` explicitly rather than inheriting 25.
- `compact_branch` / `synthesize` — cap the channel and video lines fed into the prompt;
  today `_build_prompt` serialises every row it is handed.

**Gate:** new unit tests prove each cap fires, `architecture-reviewer` returns pass on all
six points. **Cost: $0.**

---

### Phase 3 — Replay mode (the thing that makes Phase 4 safe)
**Subagent on duty:** `harness-eval-runner`.

Record the real JSON from one small live discovery job per collector into
`tests/fixtures/brightdata/*.json`, and add `BRIGHTDATA_MODE=replay` that serves those
fixtures instead of calling the API. Then run the **entire graph end to end** — real
LangGraph, real Postgres, real checkpointer, real routing, real saturation — with zero
Bright Data spend.

This is the highest-value step in the plan. Every structural bug (routing, reducer
collisions, the infinite loop, checkpoint resume, report generation) is found here for free.

**Gate:** a full replay run produces a `final_report` with graded findings and terminates on
a saturation reason, not a recursion error. **Cost: ~$0.05 LLM, $0 Bright Data.**

---

### Phase 4 — Live smoke test
**Subagent on duty:** `production-auditor` (nine-category audit) **before** running.

`HARNESS_PROFILE=smoke`, single category, launched from the console's Runs tab so the UI
path is exercised too. Hard ceiling 150 records, Tier B escalation disabled.

**Watch for:** does `graph_walk` find channels `keyword_search` did not? That is the entire
thesis of the project (master plan §1) and the `discovery_method` attribution added in
schema v4 is what proves it.

**Gate:** run completes, ≤150 records consumed, report generated, at least one channel
attributed `graph_walk` (not `both`). **Cost: ~$0.14 + ~$0.10 LLM.**

---

### Phase 5 — Finance, bounded
`HARNESS_PROFILE=bounded`, ceiling 1,500 records, Tier B escalation enabled. This is the
first run whose *output* we actually read as research rather than as a smoke signal.

**Gate:** report reviewed for quality; record spend logged against the free tier.
**Cost: ~$0.86 + ~$0.50 LLM.**

---

### Phase 6 — Legal, bounded + comparison
Same profile, second category. Compare the two reports and the two discovery-attribution
splits.

**Subagent on duty:** `roadmap-planner` afterwards — version the roadmap to v2 against
whatever Phases 4–6 actually taught us, rather than editing the master plan in place.

**Cost: ~$0.86 + ~$0.50 LLM.**

---

## 4. Running total

| Phase | Bright Data records | LLM | Cumulative free-tier use |
|---|---|---|---|
| already spent on API verification | ~950 | — | ~950 / 5,000 |
| 0–3 | 0 | ~$0.05 | ~950 |
| 4 smoke | ~90 | ~$0.10 | ~1,040 |
| 5 Finance | ~575 | ~$0.50 | ~1,615 |
| 6 Legal | ~575 | ~$0.50 | ~2,190 |

Leaves ~2,800 free records in reserve. **Total cash cost: ~$1.15 of LLM spend** — Bright
Data stays inside the free tier throughout, including the ~950 records already consumed
proving the API surface out.

---

## 5. Subagent assignment summary

| Phase | Subagent | Why it is the right one |
|---|---|---|
| 1 | `production-auditor` | New external client — retry discipline, idempotency, secrets |
| 2 | `langgraph-builder` → `architecture-reviewer` | New state fields written by parallel nodes; schema bump. This is precisely `architecture-reviewer`'s stated trigger |
| 3 | `harness-eval-runner` | Fixture-based regression layer is its defined job |
| 4 | `production-auditor` | "Is this ready to ship" gate, pre-live |
| 6 | `roadmap-planner` | Version the roadmap; do not silently edit |

---

## 6. Open items deliberately not in scope

- Real Bright Data credentials still need writing into `.env`
  (`BRIGHTDATA_API_KEY` + three dataset IDs). Blocking for Phase 4, not before.
- `MODEL_PRICING` reflects direct vendor prices, not OpenRouter's margin — cost tracking
  under-reports until a real invoice lands (ADR-0005 already records this).
- Valid `sort_by` enum values for the Comments collector remain unknown; `"top"` is
  rejected. Not blocking — `num_of_comments` alone gives the cost control we need.
- `next_recommended_videos`' exact shape is still unverified — the Videos keyword-discovery
  job used to probe it was still running after 12 minutes. Tier B is gated off in the
  `smoke` profile precisely so this stays off the critical path; verify it while recording
  Phase 3 fixtures. **Note the wall-clock number itself:** Videos discovery is far slower
  than Channels discovery, which is an argument for keeping `keyword_search` pointed at the
  Channels collector.
- Stale `MASTER_PLAN.md` §9 lines: "GitHub repo initialized with all guardrails. IN
  PROGRESS." and "Shared Postgres instance provisioned. PENDING."

# Production Readiness Audit — nine-category check against v1 code

Run by `production-auditor` (read-only) against the built v1 code, 13 Aug 2026.
Honest result — high-severity items were fixed, remaining items are Phase 2 hardening.

## Nine-category result

1. **Quota & rate-limit budgeting — PASS.** YouTube API has `check_quota` (ceiling
   × 90% target) before every call, halts via `break`/`return []` when breached.
   Bright Data concurrency respected via `asyncio.Semaphore` (config value).
   Cross-family LLM fallback (DeepSeek → Kimi) wired for frontier/mid/cheap on
   `RateLimitError`/`APITimeoutError`. Caveat: YouTube quota counter is in-memory
   (resets on process restart) — a resume after crash would re-burn quota with no
   memory of prior spend. Documented, acceptable for v1 local-invoked runs.

2. **Retry & backoff — PASS.** Tenacity-based retry in all three external clients
   (YouTube API, Bright Data, LLM): `wait_exponential_jitter(initial=1,max=60,jitter=2)`,
   `stop_after_attempt(5)`. Retryable (429/5xx/timeout) distinguished from non-retryable
   (4xx auth, quotaExceeded). Jitter prevents thundering herd.

3. **Checkpointing — PASS.** `PostgresSaver` configured at node granularity. `thread_id`
   one per run. Resume loads + migrates checkpoint before continuing. `hydrate_metadata`
   persists per-batch within a single node execution. Checkpoint state and store state
   are two non-atomic layers (acceptable for v1 batch job).

4. **Idempotency — PASS.** Postgres writes use `ON CONFLICT` upserts keyed on
   natural keys (`channel_id`, `video_id`, `(source,target,edge_type)`). Re-scanning
   visited channels is a no-op (frontier excludes expanded). Timestamps use
   `COALESCE(NULLIF(...,''), NOW())` — empty strings no longer break inserts.

5. **Secrets & config — PASS.** `.env` gitignored, `.env.example` committed with
   placeholders only. Gitleaks pre-commit hook + CI scan. Centralized config module
   (`src/config.py`). Magic numbers (quota ceiling, concurrency) moved to config.
   Caveat: no GitHub push-protection note (documented in plan §3.2, not yet configured).

6. **Observability — PARTIAL.** Structured `NodeLog` records present in taxonomy,
   compact_branch, synthesize, hydrate_metadata, and score_signals. Logs include
   cost/latency per node. No JSON log sink (logs stored in state, not written to
   disk). `keyword_search`, `graph_walk`, `niche_scanner`, `select_next_node`,
   `check_saturation` do not emit NodeLog. LangSmith tracing not wired (free tier
   available; documented as Phase 2).

7. **Cost guardrails — PASS.** Per-run budget ceiling halts the run via
   `check_saturation → budget_exhausted`. Spend is broken out per LLM call (cost
   tracked in the result dict). LLM fallback costs are tracked on the fallback call.
   Caveat: retry token costs from failed attempts are not counted (only the
   successful call's cost is recorded).

8. **Schema evolution — PASS.** `schema_version` field on `TreeNode` and in state.
   `migrate_state` covers v0→v3. Applied on resume. `TreeNodes` proposed by
   `compact_branch` inherit the parent's `schema_version`. Dynamic taxonomy growth
   (the only runtime schema change) is covered.

9. **Rollback — PASS.** Clean commit history with Conventional Commits prefixes.
   Versioned `pyproject.toml` (0.1.0). Git tag `v1` present. Eval harness regression
   gate (252 tests collected and passing per `pytest -q`, verified 13 Aug 2026 —
   golden dataset, chaos tests) on every commit.

## High-severity items (all fixed)

- **`hydrate_metadata` store-never-populated** — `first_seen_at=""` broke every
  channel insert (silent under `except: pass`). Fixed with `COALESCE(NULLIF(...))`
  in `dedup.py` and proper timestamps in `hydrate_metadata.py`.
- **Parallel tree write clobbering** — last-write-wins tree reducer silently dropped
  one branch's fields. Fixed with deep per-field merge + delta-only writes.
- **No LLM fallback** — all three production tiers used DeepSeek only; an outage
  would stall the entire run. Fixed with wired Kimi fallback.

## Additional findings

- ~~`quota_budget_check.py` is a standalone pre-flight tool; not invoked by
  `run_pipeline`/CLI/MCP.~~ **FIXED (13 Aug 2026, Phase 2):** wired as an opt-in
  `--quota-check CONFIG_JSON` pre-flight gate in `src/cli.py`; aborts before
  starting a run if any resource EXCEEDS its ceiling.
- `score_signals` was a stub — fixed (now computes engagement/cadence/velocity).
- `hydrate_metadata` re-fetched cumulative set every round — fixed (delta only).
- `MAX_CONCURRENCY` in `BrightDataClient` is used only by the async `_get_async`
  path, which is never called by the sync crawl path. Documented.
- ~~`MODEL_PRICING` in `client.py` duplicates `TIER_MAP` pricing in `cascade.py`~~
  **FIXED (13 Aug 2026, Phase 2):** `cascade.py` now derives tier pricing from
  `client.MODEL_PRICING` by model name — one source of truth.
- No git-push protection or scheduled audit configured — Phase 2 item, still open
  (GitHub org/repo setting, not code).

## Phase 2 observability follow-up (13 Aug 2026)

Category 6 (Observability) above was PARTIAL: `keyword_search`, `graph_walk`,
`niche_scanner`, `select_next_node`, and `check_saturation` emitted no `NodeLog`.
**Fixed:** all five now emit `NodeLog` on every return path. A new JSON log sink
(`src/observability/log_sink.py`) also writes every `NodeLog` to
`logs/<run_id>.jsonl` the moment a node produces it (wired via a `_logged`
wrapper around every graph node in `src/graph.py`), so a run's progress can be
tailed from disk instead of only being visible in the final checkpointed state.
LangSmith tracing remains unwired — still deferred, no change.

## Verdict

No remaining high-severity production issues. The three high-severity items found
were fixed. Of the originally-listed Phase 2 items, quota-check wiring, pricing
duplication, and the missing-NodeLog observability gap are now closed. Remaining
open: LangSmith tracing, GitHub push-protection (org setting).
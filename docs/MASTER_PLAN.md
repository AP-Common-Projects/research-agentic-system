# YouTube Niche-Research Harness
## Master Build Plan (v1)

**Status:** Active — single source of truth as of 12 Aug 2026
**Owners:** two engineers (Marzieh Ramezanian / CEO and Pouya Shaterzade / CTO)
**Supersedes:** the fragmented set of prior planning docs — see §0
**Scope of this document:** the v1 engineering build only. Not the client-sale plan, not the CrewAI port.

---

## Table of contents

0. Document status
1. The goal
2. The scope
3. Ground rules
4. First principles
5. System parts
6. Agents and subagents
7. Skills
8. Architecture
9. Roadmap
10. Production-readiness checklist
11. Input, output, interface, deployment
12. Summary
Appendix A — three ADRs

---

## 0. Document status

Five source files fed this plan. Three superseded, three authoritative, one override.

| Source file | Status |
|---|---|
| `youtube-research-architecture-roadmap.md` (Bright Data version) | **Authoritative** for architecture, tool stack, data model, risk register. |
| `youtube-research-harness-build-guide.md` (Bright Data + MCP version) | **Authoritative** for the harness's internal design, the niche scanner, evidence grading, MCP wrapping. |
| `youtube-harness-v1-gaps.md` | **Authoritative** for v1 scope, the two bug fixes, and the brain-LLM decision. |
| `omniframes-harness-engineering-plan(1).md` (v1.1) | **Authoritative** for the skills/subagents layer. |
| All other versions | **Superseded** — archived in `docs/archive/`. |

### 0.1 Conflicts resolved

1. Duplicate docs → Bright Data versions and v1.1 authoritative.
2. Postgres vs. DuckDB → Postgres, shared/managed (ADR-0002).
3. `analyze_deep` (L4) stub node → removed from v1 graph entirely.
4. `budget_exhausted` routing → force-saturates active nodes, routes to compact_branch → synthesize.
5. Five subagent templates for six skills → intentional; `adr-writer` is template-shaped, needs no subagent.
6. "Dual brain LLM" → two model families, three cascade tiers, DeepSeek as default (ADR-0003).
7. API access-path confirmation → open task, Phase 0.
8. `candidate_niches` source → human-supplied seed list (10-30 entries), config-driven.
9. Three overlapping roadmaps → consolidated into §9.
10. YouTube ToS exposure → flagged for pre-Phase 3.2 legal review; not a blocker for research-only use.

### 0.3 Amendments after the v1 build

- **Per-track discovery attribution (13 Aug 2026).** Building the console's
  discovery-graph view exposed that `hydrate_metadata` stamped every channel
  `discovery_method="keyword_and_graph_walk"`, so the store could not say which
  track found a channel — making §1's "the graph-walk track demonstrably
  surfaces at least one channel the keyword track missed" unverifiable from
  stored data. Fixed: `keyword_channel_ids` / `graph_walk_channel_ids` in
  HarnessState (union reducers), schema_version 4, and a real per-channel label
  of keyword / graph_walk / both / unattributed. Pre-v4 checkpoints report
  "unattributed" rather than being guessed.
- **Web console (13 Aug 2026).** Added as a third interface alongside CLI and
  MCP; see §11 and ADR-0004.

### 0.2 Architecture review (design-stage)

Run by `agentic-architecture-review` against the documented design (not code):
1. State & reducers — FIXED, VERIFY AT IMPLEMENTATION: audit every key in HarnessState.
2. Idempotency — PASS AS DESIGNED: upserts keyed on channel_id.
3. Frontier discipline — FIXED for graph_walk; NOT YET SPECIFIED for keyword_search.
4. Fan-out/fan-in — PASS AS DESIGNED; NOT SPECIFIED: failure/timeout behavior of one branch.
5. Checkpointing — PASS AS DESIGNED: Postgres-backed, node granularity.
6. Schema evolution — PARTIAL: capability specified, mechanics not yet applied to taxonomy-growth feature.

---

## 1. The goal

Build an autonomous research pipeline that takes a topic area and produces a graded, evidence-backed report identifying specific, underexplored YouTube channels and niches — plus the metadata-observable patterns that correlate with their outlier performance — that a human, or a plain keyword search, would not have found.

**What "done" looks like for v1:**
- The harness runs end-to-end on a small seed set with no human intervention mid-run.
- The two already-diagnosed bug classes (missing reducers, seed-based-not-frontier-based graph walk) have regression tests.
- The graph-walk track demonstrably surfaces at least one channel on a test niche that the keyword track alone missed.
- The final report identifies specific channels and specific patterns, with every claim labeled strong/moderate/weak by evidence backing.

---

## 2. The scope

### In scope for v1
L0 Taxonomy designer, L1 dual discovery (keyword + frontier-corrected graph walk), L2 metadata hydration, L3 deterministic signal computation, Niche-growth scanner, L5 branch compaction with new-node proposal, L6 cross-branch synthesis with four-axis evidence grading, Postgres structured store, tree-wide dedup/entity cache, Postgres-backed LangGraph checkpointer, Saturation-based stopping (frontier-corrected), Budget as circuit breaker only, Brain-LLM cascade (Kimi + DeepSeek), MCP-wrapped internal API, Full eval harness, Full production-readiness posture, Optional thumbnail-vision scoring (cut first under time pressure).

### Deferred to v2+
L4 deep analysis, Content-level causal explanation, Scheduled repeatable diffing runs, Hosted/scheduled production deployment, Plugin conversion (gated by real findings), Client sale packaging, CrewAI port.

### Explicitly out of scope
yt-dlp/custom-crawler path (superseded by ADR-0001), `pytrends` (dead), Retention/CTR/traffic source derivation (structurally unavailable to third parties).

---

## 3. Ground rules

3.1 Every step gets committed to GitHub with Conventional Commits prefixes.
3.2 `.env` never committed — five layers: `.gitignore`, pre-commit gitleaks hook, GitHub push protection, CI scan on every PR, periodic `production-readiness-audit` review.
3.3 No BS: no hardcoded stubs claimed as done, no swallowed failures, testable DoDs.
3.4 Two engineers, two tracks, one shared source of truth:
- **Track A (Discovery & Infrastructure):** state.py, Bright Data client, YouTube API client, outlier scoring, niche scanner, frontier/saturation logic, Postgres schema, checkpointer, dedup cache, quota budgeting.
- **Track B (Reasoning & Evaluation):** taxonomy/compaction/synthesis prompts and LLM client, brain-LLM cascade config, evidence-grading logic, MCP server, eval harness.
- **Shared:** graph.py (PR-reviewed jointly, gated by architecture-reviewer), HarnessState/Postgres contract, this plan, guardrail tooling.

---

## 4. First principles

1. Graph walk is the entire point.
2. Saturation, not time/budget, is the stop condition.
3. Structured numbers in Postgres; narrative in compaction — never the reverse.
4. Evidence grading makes the output honest.

### What gets cut, and why
- No L4 in v1, no stub `analyze_deep`, no custom MCP wrapper around Bright Data, no custom crawler, no Claude Code plugin yet, no sixth subagent for adr-writer, no hand-derived quota math, no local DuckDB as database, no hosted deployment for v1, thumbnail-vision after core loop works.

---

## 5. System parts — 19 components

See the plan for full table. Key components:
1. `state.py` — HarnessState, TreeNode, all reducers
2. Niche scanner — scores candidate niches
3. Taxonomy builder — LLM call, frontier tier
4. Node selector — deterministic next-node pick
5. Keyword search — Bright Data Scraper API
6. Graph walk — frontier-based channel-relationship crawl
7. Metadata hydration — YouTube Data API v3, batched 50 IDs/call
8. Signal scoring — outlier_score, engagement, cadence, velocity
9. Saturation check — novelty-rate routing + budget circuit breaker
10. Branch compaction — narrative summary + new-node proposal
11. Synthesis — final report with evidence grading
12. Structured store — Postgres (channels, videos, category_tags, discovery_edges, analysis_results)
13. Checkpointer — Postgres-backed LangGraph
14. Dedup/entity cache — pgvector similarity + exact channel_id match
15. Evidence grading — four-axis deterministic scoring
16. Eval harness — unit tests, golden dataset, calibrated judge, chaos tests
17. MCP server wrapper — run_niche_scan, run_deep_research, query_store
18. Secrets & config — .env + .env.example + centralized config module
19. Repo guardrails — .gitignore, pre-commit, CI, fast-suite CI

---

## 6. Agents and subagents

### 6.1 Product-internal reasoning nodes (runtime LLM calls)
- `build_taxonomy` — Frontier tier; sees niche name + scanner evidence only.
- `compact_branch` — Mid tier; sees raw channels + computed signals; may propose new node.
- `synthesize` — Frontier tier; sees all compactions + queries store directly.
- `analyze_deep` — v2 only, not built.

### 6.2 Claude Code subagents (engineering meta-layer)
- `architecture-reviewer` — Read-only, six-point review.
- `harness-eval-runner` — Builds and runs eval layer.
- `roadmap-planner` — Phased roadmap updates.
- `langgraph-builder` — Writes/modifies LangGraph code.
- `production-auditor` — Nine-category audit, flags issues.

(adr-writer deliberately has no paired subagent.)

---

## 7. Skills — the six-skill backbone

1. `agentic-architecture-review` — Six-point check on state, idempotency, frontier, fan-out, checkpointing, schema evolution.
2. `agent-harness-eval` — Four-layer eval strategy (unit → golden → judge → chaos).
3. `build-roadmap-planner` — Phased, dependency-ordered roadmaps with testable DoDs.
4. `langgraph-engineering-patterns` — Reducer patterns, frontier traversal, checkpointing, state bloat, schema versioning.
5. `production-readiness-audit` — Nine-category checklist for pre-deploy hardening.
6. `adr-writer` — Standard ADR template + index maintenance.

---

## 8. Architecture

### 8.1 Layer flow
Niche scanner → L0 Taxonomy → L1a Keyword search + L1b Graph walk (parallel) → L2 Metadata hydration → L3 Signal computation → (loop back if not saturated) → L5 Branch compaction → L6 Synthesis and evidence grading.

### 8.2 LangGraph node flow (v1, no dead analyze_deep)
START → scan_niches → build_taxonomy → select_next_node → keyword_search + graph_walk (parallel) → hydrate_metadata → score_signals → check_saturation → expand_deeper/saturated/budget_exhausted → compact_branch → route → synthesize → END.

### 8.3 Tool stack
| Need | Tool |
|---|---|
| Orchestration | LangGraph |
| Scraping, discovery | Bright Data YouTube Scraper API/Datasets |
| Bulk metadata | YouTube Data API v3 |
| Structured store | Postgres, managed/shared |
| Dedup | pgvector similarity |
| LLM | DeepSeek V4-Pro/V4-Flash, Kimi K3/K2.6, both via OpenRouter (ADR-0005) |

### 8.4 Data model
- `channels` — channel_id (PK), title, subscriber_count, description, first_seen_at, discovery_method
- `videos` — video_id (PK), channel_id (FK), title, view_count, like_count, comment_count, published_at, outlier_score, scraped_at
- `category_tags` — video_id/channel_id ↔ tree_node_id, many-to-many
- `discovery_edges` — source_channel_id, target_channel_id, edge_type, timestamp
- `analysis_results` — video_id, transcript, scene_cut_count, cuts_per_minute, multimodal_summary (populated in v2)

### 8.5 The two algorithms

**Outlier score:**
```
outlier_score(video) = video.views / mean(views of ~9-10 videos immediately before and after it)
```
Consistently above ~2-3x = repeatable pattern. Single 10x spike = possible fluke.

**Frontier-corrected saturation:**
```python
def graph_walk(state):
    node = state["tree"][state["active_node_id"]]
    frontier = node.get("unexpanded_channel_ids") or node["seed_channel_ids"]
    found = crawl_playlists_comments_collabs(frontier)
    truly_new = found - state["discovered_channel_ids"]
    return {
        "discovered_channel_ids": truly_new,
        "tree": {node["id"]: {**node, "unexpanded_channel_ids": truly_new}},
    }
```
Each round walks only newly discovered channels from last round. Novelty rate measures graph exhaustion, not re-scan dilution.

**Stop condition:** every leaf in the tree independently saturated. Duration is output, not input.

### 8.6 Brain-LLM cascade

| Tier | Nodes | Model | Context |
|---|---|---|---|
| Frontier | build_taxonomy, synthesize, judgment calls | DeepSeek V4-Pro (thinking on) | 1M |
| Mid | compact_branch | DeepSeek V4-Pro (thinking off) | 1M |
| Cheap | Query generation, lightweight touch-points | DeepSeek V4-Flash | 1M |
| Cross-judge | Eval harness judge | Kimi K3 | 1M |
| Thumbnail-vision | Thumbnail pattern scoring | Kimi K2.6 | 256K |

Default: DeepSeek across all three production tiers. Kimi reserved for eval cross-judge and optional thumbnail-vision.

---

## 9. Roadmap

### Phase 0 — Engineering backbone (in progress)
Goal: discipline layer exists so Phase 1 has guardrails from day one.
- All six skills built and installed. DONE.
- Five subagent templates written. DONE.
- GitHub repo initialized with all guardrails. IN PROGRESS.
- ADRs backfilled. DONE.
- DeepSeek/Kimi API access confirmed. DONE — via OpenRouter, single key (ADR-0005), resolves ADR-0003's open access-path item.
- Shared Postgres instance provisioned. PENDING.

### Phase 1 — Harness core build
Goal: harness runs end-to-end on a small seed set with no human intervention; both known bug classes have regression tests.
- `state.py` — schema + reducers, every field audited.
- `tools/youtube_api.py` — batched hydration.
- `tools/outlier_score.py`, `score_signals` — deterministic scoring.
- `tools/graph_walk.py` — frontier-based, Bright Data client.
- `tools/keyword_search.py` — its own frontier-equivalent.
- Niche scanner, opportunity_score.
- Postgres schema, dedup cache, checkpointer.
- `nodes/taxonomy.py` — build_taxonomy.
- `nodes/compact_branch.py` — incl. new-node proposal, schema_version.
- `nodes/synthesize.py` + evidence grading.
- Brain-LLM client + cascade config.
- `graph.py` — full wiring with budget_exhausted routing fix and fan-out/fan-in failure behavior.
- Eval harness — unit tests, golden set, calibrated judge, chaos tests.

### Phase 2 — Hardening
Goal: harness survives contact with real quotas, real failures, and a real multi-hour run.
- Full eval-harness pass.
- Full production-readiness audit (nine categories).
- Load/quota test.
- Failure injection: chaos run produces same shortlist as uninterrupted run.

### Phase 3 — Iteration and scale
Goal: validate against real usage, scope what comes after.
- Validate dynamic taxonomy growth.
- Scope external-client packaging (separate plan).
- Evaluate CrewAI port (separate plan).

### Standalone gate — plugin conversion
Convert to Claude Code plugin only after Phase 1 produces ≥1 real architecture-review finding and ≥1 real eval-harness catch.

---

## 10. Production-readiness checklist

1. Quota and rate-limit budgeting — circuit breaker at ~90% of ceiling.
2. Retry and backoff discipline — exponential with jitter, retryable/non-retryable distinguished.
3. Checkpointing and resumability — Postgres-backed, per-node, deliberate thread_id.
4. Idempotency and data integrity — upserts keyed on stable IDs.
5. Secrets and configuration management — five-layer defense, centralized config.
6. Observability — structured JSON logs per node with tracing.
7. Cost guardrails — per-run cost ceiling halts run; spend broken out per model family.
8. Schema evolution safety — schema_version field + migrate_state function.
9. Rollback readiness — known-good tag redeployable in minutes.

---

## 11. Input, output, interface, deployment

**Inputs:** candidate niche seed list (human-supplied, 10-30 strings), config (budget, thresholds, model assignments), optional direct mode (single niche, bypass scanner).

**Outputs:** final graded report (strong/moderate/weak), populated structured store (discovery_edges persists across runs), structured logs per node.

**Interface:** MCP server (run_niche_scan, run_deep_research, query_store) + minimal CLI wrapper + a local web console (FastAPI read layer at `src/api/`, React SPA at `web/`). The console launches runs by spawning the CLI entrypoint, so there remains exactly one execution path — see ADR-0004.

**Deployment:** local, human-invoked, from either engineer's laptop against shared Postgres. Not hosted/scheduled for v1.

---

## 12. Summary

Two engineers, one shared Postgres, six skills already built, and a roadmap that says — for every phase — what "done" actually means and how to check it.
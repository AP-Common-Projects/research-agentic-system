# YouTube Niche-Research Harness

Autonomous research pipeline that takes a topic area and produces a graded,
evidence-backed report identifying underexplored YouTube channels and niches —
plus the metadata-observable patterns that correlate with their outlier
performance — that a plain keyword search would not have found.

The core mechanism: two independent discovery tracks. Keyword search is
popularity-ranked by construction; a second track walks YouTube's
channel-to-channel relationship graph (playlists, collabs, comment
cross-mentions), which is ranked by community structure and surfaces channels
that never rank for any keyword.

## Architecture

```
scan_niches → build_taxonomy → select_next_node
   → { keyword_search + graph_walk } (parallel fan-out)
   → hydrate_metadata → score_signals → check_saturation
   → (expand_deeper | saturated | budget_exhausted)
   → compact_branch → synthesize (evidence-graded report)
```

- **Frontier-corrected saturation** is the real stop condition; budget is a
  circuit breaker only.
- **Evidence grading** (corroboration / consistency / recency / effect size)
  labels every claim strong / moderate / weak.
- **Postgres** structured store + LangGraph checkpointer (shared, resumable).
- **Brain-LLM cascade** — DeepSeek (V4-Pro / V4-Flash) default across tiers,
  Kimi (K3 / K2.6) for cross-judge + thumbnail-vision, with wired fallback.

See `docs/MASTER_PLAN.md` for the full build plan, `adr/` for decisions,
`docs/architecture-review.md` and `docs/production-audit.md` for review results.

## Setup

```bash
git clone git@github.com:AP-Common-Projects/research-agentic-system.git
cd research-agentic-system
pip install -e '.[dev]'
cp .env.example .env   # fill in real values
```

Required: a reachable Postgres instance (with `pgvector`), a YouTube Data API
v3 key, a Bright Data key, and an OpenRouter API key (routes both DeepSeek
V4-Pro/Flash and Kimi K3/K2.6 — see `adr/0005-openrouter-routing.md`).

## Run

```bash
python -m src.cli "3d printing" "pc building" "smart home diy"      # scan + research
python -m src.cli "3d printing" --resume <thread_id>                 # resume a run
python -m src.cli "3d printing" --json                               # JSON output
```

MCP server (for Claude Desktop / Claude Code):

```bash
python -m src.mcp.server   # exposes run_niche_scan, run_deep_research, query_store
```

## Console

A local web console for launching runs and reading their output without
hand-writing SQL or paging through raw JSON. A FastAPI read layer
(`src/api/`) over the same Postgres store, checkpointer, and JSONL log sink
the CLI already uses, with a React SPA (`web/`) on top. Runs are launched by
spawning `python -m src.cli`, so there is still exactly one way to execute the
pipeline — see `adr/0004-console-react-spa.md`.

```bash
pip install -e '.[console]'
cd web && npm install && npm run build && cd ..
uvicorn src.api.server:app --port 8000        # console at http://localhost:8000
```

For frontend work, run the two dev servers side by side instead:

```bash
uvicorn src.api.server:app --reload --port 8000
cd web && npm run dev                          # http://localhost:5173
```

Pages: **Runs** (launch, history, live status), **Run detail** (novelty-decay
chart, taxonomy tree, SSE-streamed node log, evidence-graded report),
**Discovery graph** (force-directed channel graph, marks filled where only the
graph-walk track reached a channel), **Channels** (store search, outlier
videos), **Spend** (cost and latency per node and run).

## Test

```bash
python -m pytest tests/ -q     # 252 tests, all mocked, zero real API calls
```

## Guardrails

- `.env` never committed (5-layer defense: gitignore, gitleaks pre-commit,
  GitHub push protection, CI scan, periodic audit).
- Conventional Commits; ADRs in `adr/` with running index.
- Eval harness: golden dataset, calibrated cross-model judge, chaos tests.

## Scope

v1 is a local, human-invoked, checkpointed/resumable batch job against a shared
Postgres. L4 deep analysis (transcript/scene-cut/multimodal), scheduled diffing
runs, and hosted deployment are deferred to v2 — see `docs/MASTER_PLAN.md` §2.

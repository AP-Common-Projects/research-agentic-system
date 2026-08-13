# ADR-0004: A React SPA over a FastAPI read layer as the harness console

**Status:** Accepted
**Date:** 2026-08-13

## Context
The harness had no interface beyond the CLI and the MCP server. Both block
until a run completes, which for a saturation-terminated crawl can be hours,
so an operator had no way to see what a run was doing, and the payoff data —
the taxonomy tree, the evidence-graded findings, the discovery edges — could
only be read as raw JSON or hand-written SQL. Master plan §11 scopes v1's
interface as "MCP server + minimal CLI wrapper"; this adds a third,
read-mostly surface without changing that contract.

A Streamlit dashboard was built first and rejected on review. It was the right
tool for confirming what the console needed to show, and the wrong one for
what it needed to be: Streamlit re-runs the entire script on every widget
interaction, which makes live telemetry a polling loop over the whole page;
its component vocabulary is fixed, so the one visual that carries the product's
thesis — which discovery track reached which channel — could not be drawn; and
it offers no route to a shareable artefact if the console is ever shown to a
client.

## Decision
Two pieces. A FastAPI read layer (`src/api/`) exposing runs, checkpoint state,
the NodeLog stream, store queries, and cost aggregation, with **server-sent
events** for live run telemetry. A Vite + React + TypeScript SPA (`web/`)
consuming it, built to a static bundle that FastAPI serves from the same
process in production.

Runs are launched by spawning the existing `python -m src.cli` entrypoint. The
API is an interface over the harness, never a second execution path — there is
one way to run the pipeline, and the console uses it.

React over Next.js: this is a local, single-operator tool with no SEO surface,
no public hosting, and all data arriving from a Python process. Next.js would
add a second runtime to supervise and an SSR layer with nothing to server-
render, against master plan §11's "local, human-invoked, not hosted for v1".

SSE over polling: a multi-hour run emits a node transition every few minutes.
Polling would spend hundreds of requests to deliver a handful of events; SSE
costs one idle connection and delivers each node as it lands.

## Consequences
A Node toolchain and a second language enter a previously Python-only repo,
and the console must be built (`npm run build`) before FastAPI can serve it —
a step that did not exist before. Two new optional dependency groups
(`console` for Python, `web/package.json` for the frontend) mean a contributor
who only touches the harness can skip both.

Building the discovery-graph view surfaced a real defect the Streamlit version
had hidden: `hydrate_metadata` wrote a single flat
`discovery_method="keyword_and_graph_walk"` for every channel, so the store
could not distinguish which track found a channel. That makes master plan §1's
definition of done — "the graph-walk track demonstrably surfaces at least one
channel the keyword track missed" — unverifiable from stored data. Fixed in
the same change with per-track attribution (`keyword_channel_ids` /
`graph_walk_channel_ids` in state, schema_version 4). Checkpoints written
before v4 cannot be retro-attributed and surface as "unattributed" rather than
being guessed.

## Alternatives considered
**Keep Streamlit.** Cheapest, and adequate for tables. Rejected because the
console's single most important view is a network graph with a custom
per-track encoding, which Streamlit cannot express without dropping to an
embedded HTML component — at which point the frontend is being written twice.

**Next.js.** Rejected on the grounds above; revisit if the console is ever
packaged for external clients (master plan §9, Phase 3), which is the scenario
where SSR and a hosting story start paying for themselves.

**Server-rendered Jinja templates from FastAPI.** No new toolchain, which is a
real advantage. Rejected because the live telemetry stream and the force-
directed graph are both genuinely client-side interactive, and hand-rolling
that against templates recreates a frontend framework badly.

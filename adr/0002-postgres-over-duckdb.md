# ADR-0002: Postgres over DuckDB as the structured store

**Status:** Accepted
**Date:** 2026-08-12 (backfilled — decision predates this record)

## Context
The architecture roadmap's "Open Decisions" section listed Postgres vs. DuckDB
as unresolved. Separately, the production-readiness checklist and the
`adr-writer` skill's own backfill instructions already assumed Postgres,
without the reasoning being written down in one place.

## Decision
Postgres, on a managed/shared instance reachable from both engineers'
machines.

## Consequences
Enables a genuinely shared tree-wide dedup cache and a checkpointer either
engineer can resume, without manual file-syncing. Requires provisioning and
maintaining a small hosted instance (cost, access management) instead of a
zero-infrastructure local file.

## Alternatives considered
DuckDB — lighter for a single-machine research run, and the original doc's
stated reason to prefer it in that scenario. Rejected here specifically
because of the two-engineer, two-machine requirement: DuckDB's single-file,
single-process embedded model doesn't support two people building against one
shared source of truth without one person becoming the de facto database
owner, manually shipping snapshots to the other — exactly the kind of
overhead this project's engineering rules exist to avoid. If this project ever
becomes genuinely single-machine again, this decision is worth revisiting
rather than assumed permanent.
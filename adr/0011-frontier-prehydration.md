# ADR-0011: Frontier pre-hydration as the augmentation-mode mechanism

**Status:** Accepted
**Date:** 2026-08-29

## Context
The most emphatic requirement is researching on top of an existing database
rather than rebuilding from scratch. Persistence is already idempotent
(upserts everywhere) — the gap is purely at the frontier layer:
create_initial_state starts every exclusion set empty.

## Decision
create_augmented_state pre-loads expanded_channel_refs, visited_channel_ids,
hydrated_channel_ids, and channel_refs_by_id from Postgres at run start.
discovered_channel_ids is deliberately NOT pre-seeded so novelty accounting
stays scoped to what's genuinely new. No change to any traversal logic —
it already uses "expand what's not yet expanded" correctly.

## Consequences
Four-state-field change delivers the stated requirement. A second augment
run against a fully-researched cluster completes at near-zero spend.
# ADR-0012: Extension tables over a wide-table strategy for vertical-specific fields

**Status:** Accepted
**Date:** 2026-08-29

## Context
Crime's brief asks for case-metadata fields with no Finance equivalent.
Finance's brief asks for fields with no direct Crime equivalent. The
channels/videos tables are explicitly vertical-agnostic shared entities.

## Decision
Vertical-specific fields go in 1:1 extension tables (crime_case_metadata,
video_reveal_mechanisms) keyed on the shared tables' PKs — following the
existing analysis_results precedent. Fields conceptually vertical-agnostic
(search_browse_estimate, creator_authority, commercial_intent, sponsorship)
are elevated into the shared schema, populated only when a run asks for them.

## Consequences
Prevents accumulation of always-NULL-for-one-vertical columns as more
verticals are added. Costs a join for queries wanting extension data alongside
shared fields — accepted, this codebase already uses this pattern.
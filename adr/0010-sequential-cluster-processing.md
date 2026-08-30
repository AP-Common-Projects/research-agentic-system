# ADR-0010: Sequential niche-cluster processing over parallel Send fan-out

**Status:** Accepted
**Date:** 2026-08-29

## Context
A niche cluster can contain several niches to research in one run. The
project's cost-governor accounting (branch_lineage_spend, rounds_by_node)
is scoped to one tree running at a time, with deliberate reasoning about
same-superstep concurrent-write hazards.

## Decision
Process niche cluster members sequentially — complete one niche's tree,
advance niche_index, start the next. Shared frontier exclusion sets
preserved across niches so niche 2's walk naturally excludes what niche 1
already discovered, at zero concurrency hazard.

## Consequences
Avoids re-architecting every governor field into a niche-partitioned form.
Costs wall-clock time for a hypothetical parallel approach — accepted for
this delta's actual goal (volume and quality, not runtime speed).
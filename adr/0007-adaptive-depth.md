# ADR-0007: Graph-driven, evidence-gated adaptive depth

**Status:** Accepted
**Date:** 2026-08-14

## Context

`max_tree_depth` was a fixed integer (2 under the default `bounded` profile),
and the mechanism that would decide to grow past it — `compact_branch`'s
new-node proposal — worked from an LLM reading a serialized text dump with no
deterministic grounding. The `full` profile already set `max_tree_depth: 0`
(uncapped), but running with it off would have exercised an ungrounded split
decision at full Bright Data cost per level.

## Decision

Add `cluster_branch`, a new deterministic node between `check_saturation` and
`compact_branch`, that runs seeded Louvain community detection plus lightweight
content-similarity (token-set Jaccard) clustering over each saturated node's
`discovery_edges` and channel set, filters candidates by minimum size and
distinctness, and hands only surviving candidates to `compact_branch` for LLM
labelling and confirmation — not discovery.

Depth becomes an emergent per-lineage property of real evidence, not a global
setting. `max_tree_depth` and `max_branches` are re-scoped as safety ceilings
only, set from a real calibration run rather than guessed (deferred to Phase 0
of the v2 roadmap; this build ships with them at their prior values plus the
new governors disabled-by-default where unmeasured).

Concrete mechanics shipped in this change:

- `src/tools/graph_clustering.py` — `build_similarity_graph`,
  `detect_communities`, `score_distinctness`, and the `cluster_branch` node.
  Edge-type weights mirror ADR-0006's signal-strength hierarchy
  (`featured_channel`=3, `recommendation`/`playlist`=2, `comment_author`=1).
  Content-similarity edges fire only where no discovery edge exists, at
  Jaccard > 0.3.
- `TreeNode` gains `lineage_root_id`, `cluster_distinctness_score`,
  `split_method`, `cluster_member_channel_ids` (schema v5 → v6).
- `select_next_node` traverses by priority (distinctness, evidence richness,
  depth, id) rather than pure BFS.
- `check_saturation` gains a fourth governor,
  `governor:branch_lineage_budget`, force-saturating a single depth-1 lineage
  that crosses its `budget_limit_usd / num_depth1_branches` share — generalising
  ADR-0006's rejection of a global-only ceiling one level up.

## Consequences

Some branches will run to depth 1; others deeper, decided independently by
their own evidence, matching the project's stated goal of depth that follows
the topic. New dependency: `networkx>=3.0`. New required schema migration
(v5 → v6): existing tree nodes backfill `split_method="llm_seed"` — the honest
label, since every pre-v2 split really was LLM-seeded — and `lineage_root_id`
from the tree's depth-1 ancestors.

New cost-governance surface (per-lineage budget share) that must be reasoned
about with the same rigor as the existing three governors. The Louvain `seed`
is non-negotiable: without it a resumed run recomputes different clusters than
the original, silent nondeterminism wearing an idempotency PASS.

## Alternatives considered

Simply raising `max_tree_depth` without `cluster_branch`. Rejected — this
exercises the untrustworthy part of the system (LLM-invented splits) at
increasing depth and cost, the opposite of the goal.

Embeddings-based content similarity instead of Jaccard/token-overlap. Deferred,
not rejected — the lightweight version reuses an already-proven pattern in this
codebase (`dedup.py`'s near-duplicate detection) with zero new NLP dependency;
revisit if the lightweight signal proves insufficient on real calibration data.

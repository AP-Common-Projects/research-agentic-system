# ADR-0009: Ontology-plus-empirical-validation for niche adjacency

**Status:** Accepted
**Date:** 2026-08-29

## Context
Both client briefs ask for higher channel volume without sacrificing quality.
The existing single-niche model has no mechanism for recognizing adjacent
niches worth researching alongside a target. A naive LLM-call approach would
generate "adjacent" labels without empirical grounding.

## Decision
Adjacency admission uses two independent, sequential gates: a curated ontology
(niche_adjacency table) generates candidate pairs from named domain
relationships; empirical validation reuses cluster_branch's Louvain machinery
across niche boundaries; a per-channel admission gate (§6.4) then decides
individual channel membership within an admitted cluster. Cold-start niche
pairs (no prior discovery_edges data) are admitted on ontology confidence for
one pass, then scored on every subsequent run.

## Consequences
Higher volume through wider frontier, not lower admission bar. Every admission
decision auditable via run_niche_cluster. Cold-start phase means early cluster
composition is provisional in a way later runs correct.
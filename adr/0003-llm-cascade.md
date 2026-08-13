# ADR-0003: Kimi and DeepSeek model families as the brain-LLM cascade

**Status:** Accepted
**Date:** 2026-08-12 (backfilled — decision predates this record; tier
defaults added by the master plan)

## Context
The original design assumed Claude/Gemini frontier models as the brain LLM.
Given this project's stated goal of long, thorough, deeply-saturating runs,
token cost is the primary lever on achievable depth, and DeepSeek's family is
roughly 25-30x cheaper per output token than Claude/GPT frontier pricing. The
harness's MCP-wrapped, model-agnostic design means swapping the brain LLM is a
config change at four LLM-touching nodes, not a re-architecture.

## Decision
Use a three-tier cascade (cheap/mid/frontier) drawing from two model
families — Kimi (K3, K2.6) and DeepSeek (V4-Pro, V4-Flash) — rather than one,
specifically because the eval harness's LLM-as-judge design needs a model
family that didn't produce a given output to credibly grade it. Default: the
DeepSeek lineage across all three production tiers, for operational
simplicity and lowest cost. Kimi is reserved for two targeted jobs: the
alternating cross-judge role in the eval harness, and the optional
thumbnail-vision feature, which needs Kimi specifically because DeepSeek V4
is text-only.

## Consequences
Two vendor integrations to maintain instead of one. Two separate
access/billing paths to resolve — DeepSeek's China-centric signup process
versus Kimi's more directly accessible API — flagged as an open task to close
before Phase 1's model-integration work begins, not yet resolved as of this
writing. Data residency: both vendors' traffic transits Chinese servers,
judged acceptable for this pipeline's low-sensitivity data (public YouTube
metadata, internal research reasoning) — worth re-review if this harness is
ever pointed at more sensitive data.

## Alternatives considered
Claude/Gemini frontier models (the original assumption) — not rejected on
capability grounds, superseded specifically on cost grounds given the "run
excessively long" design goal. A single-family-only cascade (DeepSeek or Kimi
alone) — rejected because it would remove the ability to cross-judge model
output without circularity, which the eval harness depends on.
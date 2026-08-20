# ADR-0008: Dataset-first — remove report generation, expand the structured store as the sole deliverable

**Status:** Accepted
**Date:** 2026-08-18

## Context
v1 proved the discovery mechanism. v2 made depth follow real graph structure.
The output was a narrative report — human-facing graded prose — plus the
populated structured store as a secondary artifact. First Principle #3 already
stated the underlying rule: "structured numbers live in Postgres; narrative
lives in compaction — never the reverse." The client's analytics team will do
their own synthesis; a fixed narrative competes for frontier-tier LLM budget
with enrichment work that makes the store more complete.

## Decision
`synthesize` is retired. `finalize_dataset` writes completeness rollups,
cluster-relative percentile ranks, and run-level stats directly to the store —
never as prose. The four-axis evidence-grading engine is preserved and reused
in `extract_success_failure_factors`, redirected from report copy to factor-
junction table columns.

Every channel gains geography (country_code, region, is_us_market with
country_source/country_confidence provenance), language, format (face/faceless,
dominant_format), a canonical niche (canonicalized against niche_taxonomy),
evergreen score, engagement composite, subscriber-floor gating, and
monetization signals. Five new enrichment nodes are inserted sequentially
after hydrate_metadata: `resolve_geo_language`, `extract_metadata_signals`,
`score_thumbnail_signals` (Kimi vision, gated), `classify_channel` (DeepSeek,
gated), and `extract_success_failure_factors` (gated).

The subscriber floor (§6.4) gates expensive LLM/vision calls — only channels
above 50k subs or with a breakout/thriving override get classified. Every
discovered channel still gets the cheap deterministic signals.

New tables: `harness_runs` (per-run metadata), `niche_taxonomy` + `success_factor_taxonomy` +
`failure_factor_taxonomy` (controlled vocabularies), `channel_snapshots` (longitudinal tracking),
`channel_success_factors` / `channel_failure_factors` (structured factor extraction),
`channel_niches` (canonical niche membership). ~50 new columns on `channels` and
~18 on `videos`. Schema version 6 → 7 with migrate_state v7.

## Consequences
The structured store is now the sole deliverable — directly queryable by the
data science team with no report to fall back on. New cost surface:
per-qualifying-channel LLM/vision calls from `classify_channel` and
`score_thumbnail_signals`, gated by the subscriber floor so they scale with
quality-filtered channel count, not raw discovery volume. The existing
`budget_limit_usd` circuit breaker still applies.

Every field is either populated with a real value, or explicitly marked
unavailable with a reason in `missing_required_fields` — never a silent NULL.
`data_completeness_score` is computed at run end so a data scientist can
filter on quality before analysis.

## Alternatives considered
Keep the report *and* add the dataset. Rejected — doubles frontier-tier LLM
spend on synthesis work the client's own tooling will do more flexibly.
Hard-exclude non-US channels at discovery. Rejected — breaks comparative
failure-factor analysis.
# ADR-0005: Route the DeepSeek/Kimi cascade through OpenRouter

**Status:** Accepted
**Date:** 2026-08-13

## Context
ADR-0003 adopted a two-family cascade (DeepSeek + Kimi) specifically so the
eval harness's cross-judge could grade a different family than production
used, and flagged as an open consequence: "Two separate access/billing paths
to resolve — DeepSeek's China-centric signup process versus Kimi's more
directly accessible API." That was never resolved before this change — direct
`DEEPSEEK_API_KEY`/`KIMI_API_KEY` credentials were configured but the signup
friction ADR-0003 anticipated meant neither vendor was actually onboarded.

## Decision
Access both families through OpenRouter instead of direct vendor APIs — one
key (`OPENROUTER_API_KEY`), one OpenAI-compatible endpoint
(`https://openrouter.ai/api/v1`), one signup/billing relationship. This
directly closes ADR-0003's open access-path item.

Internal model identifiers (`deepseek-v4-pro`, `deepseek-v4-flash`, `kimi-k3`,
`kimi-k2.6`) are unchanged everywhere they already appear — `TIER_MAP`,
`MODEL_PRICING`, cost tracking in `NodeLog`, tests, this project's docs. The
only new concept is `OPENROUTER_MODEL_SLUGS` in `src/llm/client.py`, which
translates an internal id to whatever OpenRouter's catalog calls that model
(`"deepseek/deepseek-v4-pro"`, etc.) at the point of the actual API call, and
nowhere else. `LLMClient` collapses from a per-family client dict to a single
client, since there is now only one upstream endpoint to hold a connection to.

`TierConfig.provider` in `cascade.py` keeps its `"deepseek"`/`"kimi"` values —
it was never used for client selection (that dispatch lived in
`LLMClient._get_client`, now removed); it's descriptive metadata for the
`llm_family_fallback` log line and stays meaningful as "which model family",
independent of which HTTP endpoint serves it.

## Consequences
Single point of failure: an OpenRouter outage now takes down both cascade
families at once, where direct integration would have left one family
reachable if only one vendor were down. Judged acceptable — the cross-judge
role ADR-0003 cared about is about *output* independence between families for
grading purposes, not *infrastructure* independence, and OpenRouter's own
uptime is generally better than either single vendor's direct API in
practice. Revisit if OpenRouter reliability becomes a real operational
problem; the fallback path in `cascade.py` would need a second, non-OpenRouter
route to help here, which is a larger change than this ADR covers.

Slightly higher per-token cost than direct vendor pricing (OpenRouter takes a
margin) — `MODEL_PRICING` in `client.py` still reflects the vendors' direct
prices, not OpenRouter's, so cost tracking will under-report actual spend by
whatever OpenRouter's margin is. Worth a follow-up once real invoices are
available to correct `MODEL_PRICING` to what's actually being paid.

~~`OPENROUTER_MODEL_SLUGS` is a best-guess mapping pending verification~~
**Verified 2026-08-13** against `GET /api/v1/models` on the live OpenRouter
catalog (409 models listed) — all four slugs (`deepseek/deepseek-v4-pro`,
`deepseek/deepseek-v4-flash`, `moonshotai/kimi-k3`, `moonshotai/kimi-k2.6`)
exist exactly as guessed.

## Alternatives considered
Keep direct DeepSeek/Kimi integrations and actually resolve the signup
friction — rejected as the harder path for no real benefit here; OpenRouter
exists precisely to remove that class of problem, and this project's LLM
calls are not so latency- or feature-sensitive that a proxy layer costs
anything meaningful.

Route only one family (e.g. DeepSeek) through OpenRouter and keep the other
direct — rejected for consistency: half-migrating would mean maintaining both
the old per-provider client dispatch and a new OpenRouter path simultaneously,
which is more code than picking one.

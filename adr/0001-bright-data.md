# ADR-0001: Bright Data as primary discovery and scraping infrastructure

**Status:** Accepted
**Date:** 2026-08-12 (backfilled — decision predates this record)

## Context
The original design used generic residential proxies plus a custom-built graph
crawler (playlist/comment/description parsing) and yt-dlp for search scraping.
This required building and maintaining a scraper against YouTube's own
anti-bot posture indefinitely.

## Decision
Adopt Bright Data as the primary tool for scraping, discovery, and media
extraction — YouTube Scraper API/Datasets plus the Media Extraction API —
replacing the custom crawler and yt-dlp-search-scraping paths. yt-dlp is
demoted to a rare fallback for transcript extraction only, when Bright Data's
native transcript field is unavailable for a given video.

## Consequences
`graph_walk` moves from "genuinely custom, no shortcut" to normal
API-integration work — this changed the build-order risk assessment directly.
Introduces per-record cost (~$1.5 per 1,000 records at list-rate pricing) and
a new vendor dependency. IP-blocking risk shifts from "our problem" to Bright
Data's managed-unlocker SLA.

## Alternatives considered
Kept generic residential proxies plus a custom crawler (the original design).
Rejected because the genuinely expensive part was never the per-record fee —
it was the ongoing engineering cost of maintaining a scraper against YouTube's
anti-bot measures. The one piece of the original design kept as-is: the
official YouTube Data API v3 for bulk metadata hydration, because 1 quota unit
per 50-ID batch is free at a scale where Bright Data's per-record pricing
would not be.
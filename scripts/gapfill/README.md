# Workbook gap-fill

Mirrors `.claude/skills/workbook-gap-fill/SKILL.md`, which is gitignored
along with the rest of `.claude/skills/`. Kept here so the procedure
survives in version control next to the scripts it describes.

# Filling the gaps in a client workbook

Written after the Finance delivery of 2026-09-01, where the client came back
with ~35 unpopulated columns across three sheets plus one class of *wrong*
values. Nearly every gap traced to the same root cause, and the fixes are
reusable verbatim for Crime.

## Where the scripts live

`scripts/gapfill/`. They were written ad hoc during the run and copied out
of a scratch directory, so they read their channel set from
`final_channel_sets.json` and take the vertical as argv[1].

## The one root cause behind most empty columns

`persist_channel_v3` / `persist_video_v3` filter every write through a
hardcoded allowlist. For a long period that allowlist was missing most v4
columns, so nodes computed values, logged success, and wrote **nothing**.
The allowlist is fixed now, but **the nodes have not re-run**, so the
columns stay empty and look like a collection failure.

The practical consequence: before assuming a column needs an API call or a
model, check whether it is a pure function of data already stored. Most are.

## Step 1 — audit, do not guess

`scripts/gapfill/gap_report.py <vertical>` prints per-sheet, per-column fill rates
straight from the export functions (not from the .xlsx), so it reflects what
the *next* rebuild will contain.

Read the output in three groups: 0.0% (never populated), partial, and
99%-ish (a genuine handful).

## Step 2 — fix in this order. The order matters.

### 2a. Refresh video statistics FIRST
`scripts/gapfill/refresh_video_stats.py <vertical>` — videos.list, 1 quota unit per 50
ids, so ~400 units for a 20k-video deliverable.

Why first: stats are a snapshot taken at hydration, and a channel's newest
uploads are hydrated within hours of publication. The workbook then reports
**0 views / 0 likes / 0 comments** for a video that has since done
thousands. A Codie Sanchez Short showing 0/0/0 against a real 4,872/268/4
is what surfaced this. ~1% of rows are affected, at every age — it is not
only fresh uploads.

The same call also fills `duration_seconds`, `language_code`, the
thumbnails blob behind `thumbnail_url`, and the real `description`.

Live streams legitimately report 0 views. Do not "fix" those.

### 2b. Deterministic recomputes — free, no API, no model
`scripts/gapfill/recompute_video_fields.py <vertical>`

- `title_word_count` / `has_number` / `is_question` / `capitalization` /
  `emoji_count` — reuse `extract_metadata_signals._title_signals`
- `is_likely_news` — reuse `_is_news_title`
- `views_per_day_since_publish` — views / days since publish
- `outlier_score` — **must run after 2a**: it is views divided by the mean
  of the ~10 videos either side, so a stale view count in the window
  corrupts scores for its neighbours too
- `sample_reason` — Shorts come from a different sampler and were never
  labelled; pre-2026-08-30 long-form rows came from
  `get_channel_videos(max_results=50)`, which *is* the `latest` sample

### 2c. Channel scores — free
`scripts/gapfill/backfill_scores.py <vertical>` — `evergreen_score` (per video, then
view-weighted to the channel), `engagement_score`, and
`data_completeness_score`. Reuses `signal_scoring.compute_*` so values
match what the node would write. Also after 2a: all three are functions of
view/like/comment counts.

Note `finalize_dataset` scopes completeness by `first_discovered_run_id`,
so a channel found by one run and shipped in another never gets a score.
The backfill scopes by the deliverable set instead.

### 2d. Cheap API fills
- `scripts/gapfill/backfill_channel_fields.py` — `channel_creation_date`,
  `description`, `country_code` (channels.list, 1 unit/50), plus
  `vertical_start_date` and its basis/confidence
- `scripts/gapfill/backfill_counts_and_first.py` — `first_video_published_at` (walks
  the uploads playlist to its LAST page; do **not** use
  `MIN(videos.published_at)`, which is the oldest video in a *recent*
  window and can be off by a decade) and the `total_long/shorts/live`
  counts from the UULF/UUSH/UULV auto-playlists

### 2e. Model-derived
- `scripts/gapfill/backfill_search_browse.py` — deterministic rule first
  (news → browse → search → mixed); no model needed at all
- `scripts/gapfill/describe_parallel.py <vertical> <slice>` — `video_description` on
  the cheap tier, 30 titles per call, ~$0.18 per 16k videos
- `scripts/gapfill/enrich_stragglers.py` — channels the parallel passes stalled past,
  still missing `primary_topic` / `creator_authority`

## Step 3 — re-audit, then rebuild

Re-run `gap_report.py`. Only then `finalize_and_rebuild.py`.

## Genuinely unobtainable — say so, do not fake

- `sub_growth_30d` / `sub_growth_90d` / `views_30d` / `views_90d` — YouTube
  exposes only *current* counts. No historical endpoint exists. These can
  only be built by accumulating snapshots forward, or bought from a third
  party (Social Blade / ViewStats).
- Per-video historical view series (24h/3d/7d/…) — same reason.
- Subscriber history by year — same reason.

## Cross-vertical columns

`crime_type`, `victim_type`, `suspect_relationship`, `investigation_type`,
`evidence_type_primary`, `case_status`, `case_fame_level`, `case_country`,
`case_year`, the five `*_available` footage flags and `reveal_mechanisms`
are Crime-only and are **correctly empty in Finance**. Do not report them
as gaps there. The reverse holds for nothing — Finance has no exclusive
columns.

## Pitfalls that cost real time

- **Commit granularity.** A backfill that paginates an API while holding a
  transaction blocks every other writer on that table. One held row locks
  on `channels` for ~40s per channel and stalled a concurrent backfill for
  four and a half minutes. Commit per row when the loop does I/O.
- **CHECK constraints.** `ADD COLUMN IF NOT EXISTS` is a no-op on an
  existing table and therefore never revises the CHECK it carries. Widening
  one (e.g. adding `shorts_sample` to `sample_reason`) requires DROP then
  ADD CONSTRAINT.
- **Empty scope lists.** `if scope:` is False for `[]`, so a worker with an
  empty slice silently widens to the whole table. Use `if scope is not
  None:`. This cost four hours of redundant LLM calls once.
- **Estimating instead of measuring.** Per-unit costs were wrong by 4x
  twice (quota per channel: 15 vs 68 actual; LLM output tokens: 65 vs 395
  per object). One real call takes three minutes to measure. Do that first.

"""extract_metadata_signals — deterministic title/hashtag/monetization extraction.

Runs on every channel, gated by nothing — purely regex and pattern matching.
Computes: title structure signals, hashtag extraction, monetization signals
(affiliate/sponsor/membership), upload cadence stats. Writes to the store
directly via persist_channel_v3 and persist_video_v3.
"""

from __future__ import annotations

import re
import time
from collections import Counter

from src.tools.run_scope import scope_clause
from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.state import NodeLog, ErrorRecord


_AFFILIATE_PATTERNS = re.compile(
    r"(affiliate\s*link|#ad\b|as\s+an\s+amazon\s+associate|commission\s*earned|"
    r"affiliate\s*disclosure|paid\s*link|may\s*earn\s*a\s*commission|"
    r"links?\s*(below|in\s*description).*affiliate|"
    r"support\s*the\s*channel.*(?:amazon|link)|"
    r"buy\s*on\s*amazon|amzn\.to|shop\.ltk)",
    re.IGNORECASE,
)

_SPONSOR_PATTERNS = re.compile(
    r"(sponsored\s*by|this\s*video\s*is\s*brought\s*to\s*you\s*by|"
    r"paid\s*promotion|in\s*collaboration\s*with|"
    r"thanks\s*to\s*(?:our\s*)?sponsor|big\s*thanks\s*to|"
    r"today'?s?\s*sponsor|#sponsored|#ad|#partner)",
    re.IGNORECASE,
)

_MEMBERSHIP_PATTERNS = re.compile(
    r"(patreon|ko-fi|buymeacoffee|channel\s*member|join\s*this\s*channel|"
    r"membership|subscribestar|liberapay|support\s*on\s*patreon|"
    r"become\s*a\s*member|exclusive\s*content\s*for\s*members)",
    re.IGNORECASE,
)

_HASHTAG_RE = re.compile(r"#(\w+)")


def _extract_hashtags(text: str) -> list[str]:
    return [tag.lower() for tag in _HASHTAG_RE.findall(text or "")]


#: Current-events markers in a video title. Deliberately literal — this is
#: the deterministic per-video complement to classify_channel's LLM-level
#: channel judgement, not a replacement for it.
_NEWS_TITLE_PATTERNS = re.compile(
    r"\b(breaking|just\s+in|live|update[sd]?|developing|announce[sd]?|"
    r"today|tonight|this\s+(week|morning|evening)|yesterday|"
    r"latest|new\s+details|press\s+conference|verdict|sentenced|"
    r"arrested|charged|indicted|testifies|hearing|trial\s+day)\b",
    re.IGNORECASE,
)


def _is_news_title(title: str) -> bool:
    """Per-video current-events signal.

    videos.is_likely_news existed as a column but nothing ever wrote it —
    every video row exported NULL. A video is news-ish when its title
    carries an explicit currency marker (breaking/update/verdict/...) or a
    bare year that reads as "this is about a dated event".
    """
    if not title:
        return False
    if _NEWS_TITLE_PATTERNS.search(title):
        return True
    return bool(re.search(r"\b20[12]\d\b", title))


def _title_signals(title: str) -> dict:
    if not title:
        return {}
    clean = title.strip()
    sig: dict = {
        "title_char_count": len(clean),
        "title_word_count": len(clean.split()),
        "title_has_number": bool(re.search(r"\d+", clean)),
        "title_is_question": clean.rstrip().endswith("?"),
        "title_emoji_count": len(re.findall(r"[\U0001F300-\U0001FAFF]|[\u2600-\u27BF]", clean)),
    }
    # Capitalization pattern
    words = clean.split()
    if not words:
        return sig
    caps = sum(1 for w in words if w and w[0].isupper())
    all_caps = sum(1 for w in words if w.isupper() and len(w) > 1)
    if all_caps / max(len(words), 1) > 0.5:
        sig["title_capitalization"] = "all_caps"
    elif all(w[0].isupper() for w in words if w):
        sig["title_capitalization"] = "title_case"
    elif caps / max(len(words), 1) > 0.3:
        sig["title_capitalization"] = "mixed_emphasis"
    else:
        sig["title_capitalization"] = "sentence_case"
    return sig


def _detect_monetization(title: str, description: str) -> dict:
    combined = f"{title or ''} {description or ''}"
    return {
        "has_affiliate_signal": bool(_AFFILIATE_PATTERNS.search(combined)),
        "has_sponsor_signal": bool(_SPONSOR_PATTERNS.search(combined)),
        "has_membership_signal": bool(_MEMBERSHIP_PATTERNS.search(combined)),
    }


# Shortest sampled window a per-week rate can honestly be derived from.
_MIN_SPAN_DAYS_FOR_RATE = 1.0
# channels.uploads_per_week_avg is NUMERIC(6,2): anything >= 10^4 is rejected
# by Postgres outright, so this is the column's ceiling, not a judgement about
# what upload rate is plausible.
_MAX_UPLOADS_PER_WEEK = 9999.99


def _compute_upload_stats(videos: list[dict]) -> dict:
    """Upload cadence: average per week and consistency (1 - normalized variance)."""
    from datetime import datetime, timezone

    if len(videos) < 2:
        return {"uploads_per_week_avg": 0, "upload_consistency_score": 0}
    dates = []
    for v in videos:
        pub = v.get("published_at") or ""
        if not pub:
            continue
        # published_at arrives as a real datetime when it comes from
        # Postgres (TIMESTAMPTZ via psycopg) and as a string when it comes
        # straight off the YouTube API. Only the string path existed, so
        # strptime raised TypeError on every DB-sourced row and the outer
        # except swallowed it — uploads_per_week_avg and
        # upload_consistency_score were 0 for every channel on every run,
        # which in turn broke the is_likely_news derivation downstream in
        # score_signals (it reads cadence).
        if isinstance(pub, datetime):
            dates.append(pub if pub.tzinfo else pub.replace(tzinfo=timezone.utc))
            continue
        try:
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                         "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.strptime(pub, fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dates.append(dt)
                    break
                except ValueError:
                    continue
        except Exception:
            continue
    if len(dates) < 2:
        return {"uploads_per_week_avg": 0, "upload_consistency_score": 0}
    dates.sort()
    span_days = (dates[-1] - dates[0]).total_seconds() / 86400
    # A weekly cadence cannot be inferred from a window shorter than a day:
    # dividing by a near-zero span extrapolates a burst into a rate that is
    # not just wrong but out of the column's range. Observed live on 2026-09-05
    # -- 10 videos sharing a 35-second span produced 183,272 uploads/week, and
    # 2 videos 26 seconds apart produced 40,320, against a NUMERIC(6,2) column
    # that stops at 9,999.99. Each overflow aborted persist_channel_v3 for that
    # channel, losing its whole signal row AND its video signals, 13 channels
    # in one run. `span_days <= 0` only caught an exactly-simultaneous batch.
    #
    # These are bulk uploads -- a back catalogue published in one sitting --
    # so the honest answer is that this sample says nothing about cadence,
    # which is what the other insufficient-data paths above already return.
    if span_days < _MIN_SPAN_DAYS_FOR_RATE:
        return {"uploads_per_week_avg": 0, "upload_consistency_score": 0}
    # Clamped to the column's own domain as a structural backstop: the guard
    # above is about meaning, this is about never handing Postgres a value
    # the column cannot hold, whatever future arithmetic lands here.
    uploads_per_week = min(round(len(dates) / (span_days / 7.0), 2), _MAX_UPLOADS_PER_WEEK)

    # Consistency used to be the coefficient of variation of raw gaps
    # BETWEEN individual uploads. That statistic is dominated by intraday
    # spacing: a channel posting 5 videos on Monday and none the rest of
    # the week produces gaps of ~0 (same-day) mixed with one gap of several
    # days, which spikes the CV to >1 and clamps consistency to 0 —
    # regardless of how steady the channel's actual weekly output is. That
    # is exactly backwards for what this column is meant to answer ("does
    # this channel upload on a predictable schedule?"): a channel doing
    # 4.9 uploads/week read as 0.32 for a bursty-but-steady upload pattern
    # that a human would call quite consistent.
    #
    # Bucket into fixed-size windows from the earliest date, and measure the
    # CV of PER-BUCKET COUNTS. Fixing the bucket at exactly 7 days broke
    # down for any channel averaging under ~1 upload/week: with a mean
    # count that low, a Poisson-ish process's own stdev (~sqrt(mean))
    # EXCEEDS the mean, so CV > 1 and consistency floors at 0 purely from
    # sampling noise — not because the channel is actually erratic. Verified
    # live: a channel posting an even ~1 video every other week (mean 0.44,
    # nearly all buckets are exactly 0 or 1) scored 0.00, the same score as
    # a channel that goes silent for months and then dumps 20 uploads in a
    # week — the metric couldn't tell "steady but infrequent" from "bursty".
    #
    # Widen the bucket for low-cadence channels so each one holds a few
    # expected uploads on average (never coarser than a channel's own
    # posting rate needs, never finer than a week, capped at ~6 months so
    # a very sparse channel still gets more than one bucket to compare).
    target_per_bucket = 3.0
    bucket_days = 7.0
    if uploads_per_week > 0:
        bucket_days = max(7.0, min(180.0, 7.0 * target_per_bucket / uploads_per_week))
    n_buckets = max(1, int(span_days // bucket_days) + 1)
    bucket_counts = [0] * n_buckets
    for d in dates:
        idx = min(int((d - dates[0]).total_seconds() / 86400 // bucket_days), n_buckets - 1)
        bucket_counts[idx] += 1

    if n_buckets < 2:
        # Everything landed in one bucket — nothing to compare against, but
        # the channel visibly uploaded during the sampled window.
        consistency = 1.0
    else:
        import statistics

        mean_count = sum(bucket_counts) / n_buckets
        cv = statistics.stdev(bucket_counts) / mean_count if mean_count > 0 else 1.0
        consistency = max(0.0, 1.0 - min(1.0, cv))
    return {
        "uploads_per_week_avg": uploads_per_week,
        "upload_consistency_score": round(consistency, 2),
    }


#: Videos enriched per call. These are pure string computations plus a
#: write each -- no model, no quota -- so the bound is about keeping one
#: invocation predictable, not about cost.
_VIDEO_BATCH = 2000


def extract_metadata_signals(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    run_id = state.get("run_id", "")

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="extract_metadata_signals",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "processed": 0})}

    try:
        cur = conn.cursor()
        # Scoped to this run's own channels -- see src/tools/run_scope.py.
        scope_sql, scope_params = scope_clause(state)
        cur.execute(
            "SELECT channel_id, title, description FROM channels "
            "WHERE has_affiliate_signal IS NULL " + scope_sql,
            scope_params,
        )
        channels = cur.fetchall()
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "processed": 0})}

    processed = 0
    errors: list[dict] = []
    from src.tools.dedup import persist_channel_v3, persist_video_v3

    for ch_id, title, desc in channels:
        ch_fields: dict = {}
        title_sig = _title_signals(title or "")
        monet = _detect_monetization(title or "", desc or "")
        ch_fields.update(monet)

        # Upload stats: read recent videos
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT published_at FROM videos WHERE channel_id = %s ORDER BY published_at DESC NULLS LAST LIMIT 50",
                (ch_id,),
            )
            videos = [{"published_at": r[0]} for r in cur.fetchall()]
            cur.close()
            upload = _compute_upload_stats(videos)
            ch_fields.update(upload)
        except Exception:
            pass

        try:
            persist_channel_v3(conn, ch_id, run_id, ch_fields)
            processed += 1
        except Exception as exc:
            # A failed statement leaves the connection's transaction
            # aborted, poisoning every remaining channel in this loop with
            # InFailedSqlTransaction unless rolled back here.
            conn.rollback()
            errors.append(ErrorRecord(
                node_name="extract_metadata_signals",
                error_type=type(exc).__name__,
                message=f"channel persist failed for {ch_id}: {exc}",
                recoverable=True,
            ).model_dump())
            continue

    # Video-level enrichment, deliberately NOT nested in the loop above.
    #
    # It used to be, which meant a channel's videos were only ever reached
    # while the channel itself was eligible -- and channel eligibility is
    # "has_affiliate_signal IS NULL", a one-time marker. So a channel any
    # earlier run had already processed skipped this entirely, including
    # videos hydrated long afterwards. 31,665 videos in this database had
    # no title signals for that reason, and run-3f649c9246a3 shipped six
    # Videos columns at 85% because one such channel came into its workbook
    # through the category filter.
    #
    # Videos carry their own marker, so they get their own pass.
    videos_enriched = 0
    try:
        cur = conn.cursor()
        video_scope_sql, video_scope_params = scope_clause(state, "channel_id")
        cur.execute(
            "SELECT video_id, title, description FROM videos "
            "WHERE hashtags IS NULL " + video_scope_sql
            # Bounded so one call cannot run away on a large backlog. The
            # caller re-invokes while progress is reported, which is how the
            # export gate drains a backlog without a fixed call ceiling.
            + "ORDER BY video_id LIMIT %s",
            video_scope_params + (_VIDEO_BATCH,),
        )
        videos_to_enrich = cur.fetchall()
        cur.close()

        for vid_id, v_title, v_desc in videos_to_enrich:
            v_fields: dict = {}
            v_fields["hashtags"] = _extract_hashtags(f"{v_title or ''} {v_desc or ''}")
            v_fields.update(_title_signals(v_title or ""))
            v_fields["is_likely_news"] = _is_news_title(v_title or "")
            try:
                persist_video_v3(conn, vid_id, v_fields)
                videos_enriched += 1
            except Exception as exc:
                conn.rollback()
                errors.append(ErrorRecord(
                    node_name="extract_metadata_signals",
                    error_type=type(exc).__name__,
                    message=f"video persist failed for {vid_id}: {exc}",
                    recoverable=True,
                ).model_dump())
                continue
    except Exception as exc:
        conn.rollback()
        errors.append(ErrorRecord(
            node_name="extract_metadata_signals",
            error_type=type(exc).__name__,
            message=f"video enrichment query failed: {exc}",
            recoverable=True,
        ).model_dump())

    put_connection(conn)
    return {
        "node_logs": _log({"processed": processed, "videos_enriched": videos_enriched}),
        "errors": errors,
    }
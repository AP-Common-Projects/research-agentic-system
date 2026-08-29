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

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.state import NodeLog


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
    intervals = [(dates[i + 1] - dates[i]).total_seconds() / 86400 for i in range(len(dates) - 1)]
    if not intervals:
        return {"uploads_per_week_avg": 0, "upload_consistency_score": 0}
    avg_days = sum(intervals) / len(intervals)
    uploads_per_week = 7.0 / avg_days if avg_days > 0 else 0
    if len(intervals) >= 2:
        import statistics
        variance = statistics.variance(intervals) if len(intervals) >= 2 and avg_days > 0 else 0
        consistency = max(0.0, 1.0 - min(1.0, variance / max(avg_days, 0.01)))
    else:
        consistency = 0.0
    return {
        "uploads_per_week_avg": round(uploads_per_week, 2),
        "upload_consistency_score": round(consistency, 2),
    }


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
        cur.execute(
            "SELECT channel_id, title, description FROM channels "
            "WHERE has_affiliate_signal IS NULL"
        )
        channels = cur.fetchall()
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "processed": 0})}

    processed = 0
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
        except Exception:
            continue

        # Video-level: hashtags and title signals
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT video_id, title, description FROM videos WHERE channel_id = %s AND hashtags IS NULL",
                (ch_id,),
            )
            videos_to_enrich = cur.fetchall()
            cur.close()
            for vid_id, v_title, v_desc in videos_to_enrich:
                v_fields: dict = {}
                v_fields["hashtags"] = _extract_hashtags(f"{v_title or ''} {v_desc or ''}")
                v_fields.update(_title_signals(v_title or ""))
                try:
                    persist_video_v3(conn, vid_id, v_fields)
                except Exception:
                    continue
        except Exception:
            continue

    put_connection(conn)
    return {"node_logs": _log({"processed": processed})}
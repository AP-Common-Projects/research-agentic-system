"""classify_channel — LLM classification of face/faceless, format, and niche.

Gated by meets_subscriber_floor (§6.4). Input is structured signals only
(thumbnail signals, title patterns, cadence, engagement) — never raw images
or text dumps. Output: face_status, dominant_format, and a proposed niche_name
that gets canonicalized against niche_taxonomy before insert.

The idempotency guard (§10 item 2): if classifier_model + classifier_version
already match, skip the LLM call — a resume must not re-bill a channel that
was already classified.
"""

from __future__ import annotations

import json
import re
import time
from difflib import SequenceMatcher

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
from src.state import NodeLog, ErrorRecord

SYSTEM_PROMPT = """You are a YouTube channel classifier. Given structured signals for a channel, produce a classification.

Input (all numbers, no raw text):
- face signals: how many thumbnails have faces
- engagement_score: 0-100 composite
- evergreen_score: 0-100
- is_likely_news: boolean
- uploads_per_week_avg: number
- upload_consistency_score: 0-1
- subscriber_count: number
- title patterns: avg chars, avg words, has_numbers, has_questions, case_style, emoji_count
- monetization: affiliate, sponsor, membership signals

Rules:
1. face_status: "face" if most thumbnails have faces, "faceless" if none do, "mixed" if it varies
2. dominant_format: pick from "animated_explainer", "talking_head", "documentary_narration", "compilation", "vlog", "clip_commentary", "tutorial", "list_roundup", "reaction_commentary", "gaming_playthrough", "music_video", "podcast". Use the signals, not the title.
3. niche_name: propose a short canonical niche name (e.g. "personal_finance_budgeting") — lowercase_underscore format. This will be matched against the taxonomy table.
4. classifier_model: "deepseek-v4-pro"
5. classifier_version: "v3.0"

Respond with ONLY a JSON object:
{
  "face_status": "face"|"faceless"|"mixed",
  "dominant_format": "...",
  "niche_name": "...",
  "classifier_model": "deepseek-v4-pro",
  "classifier_version": "v3.0"
}"""


def _match_niche(proposed: str, conn: Any) -> int | None:
    """Fuzzy-match a proposed niche name against niche_taxonomy. Returns niche_id or None."""
    if not proposed:
        return None
    cur = conn.cursor()
    try:
        cur.execute("SELECT niche_id, niche_name FROM niche_taxonomy")
        rows = cur.fetchall()
        best_id, best_score = None, 0.0
        normalized = proposed.lower().strip().replace(" ", "_")
        for nid, name in rows:
            if name == normalized:
                return nid
            score = SequenceMatcher(None, normalized, name).ratio()
            if score > best_score and score > 0.7:
                best_score = score
                best_id = nid
        return best_id
    finally:
        cur.close()


def classify_channel(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    cfg = get_config().harness
    run_id = state.get("run_id", "")
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="classify_channel",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "classified": 0})}

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT channel_id FROM channels WHERE meets_subscriber_floor = TRUE "
            "AND (classifier_model IS NULL OR classifier_model != 'deepseek-v4-pro' "
            "OR classifier_version != 'v3.0') LIMIT 50"
        )
        eligible = [r[0] for r in cur.fetchall()]
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "classified": 0})}

    classified = 0
    channels_enriched = 0
    from src.tools.dedup import persist_channel_v3, persist_channel_niche_membership

    for ch_id in eligible:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT subscriber_count, face_status, thumbnail_has_face, "
                "engagement_score, evergreen_score, is_likely_news, "
                "uploads_per_week_avg, upload_consistency_score, "
                "has_affiliate_signal, has_sponsor_signal, has_membership_signal "
                "FROM channels WHERE channel_id = %s",
                (ch_id,),
            )
            ch_row = cur.fetchone()
            cur.close()

            if not ch_row:
                continue

            # Build structured prompt
            prompt = json.dumps({
                "subscriber_count": ch_row[0] or 0,
                "face_signals": {"face_status": ch_row[1] or "unknown"},
                "engagement_score": ch_row[3] or 0,
                "evergreen_score": ch_row[4] or 0,
                "is_likely_news": bool(ch_row[5]),
                "uploads_per_week_avg": float(ch_row[6] or 0),
                "upload_consistency_score": float(ch_row[7] or 0),
                "monetization": {
                    "affiliate": bool(ch_row[8]),
                    "sponsor": bool(ch_row[9]),
                    "membership": bool(ch_row[10]),
                },
            }, indent=2)

            # LLM classification
            try:
                result = complete_tier("mid", prompt, SYSTEM_PROMPT)
                content = result.get("content", "")
                cleaned = content.strip()
                match = re.search(r"\{[\s\S]*\}", cleaned)
                if match:
                    cleaned = match.group(0)
                parsed = json.loads(cleaned)
            except Exception as exc:
                errors.append(ErrorRecord(
                    node_name="classify_channel",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=True,
                ).model_dump())
                continue

            face_status = str(parsed.get("face_status", "unknown"))
            dominant_format = str(parsed.get("dominant_format", ""))
            niche_name = str(parsed.get("niche_name", ""))
            classifier_model = str(parsed.get("classifier_model", "deepseek-v4-pro"))
            classifier_version = str(parsed.get("classifier_version", "v3.0"))

            # Canonicalize niche
            niche_id = _match_niche(niche_name, conn) if niche_name else None

            usage = result.get("usage", {})
            cost = estimate_cost("mid", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

            ch_fields = {
                "face_status": face_status,
                "dominant_format": dominant_format,
                "classifier_model": classifier_model,
                "classifier_version": classifier_version,
            }
            if niche_id:
                ch_fields["primary_niche_id"] = niche_id

            try:
                persist_channel_v3(conn, ch_id, run_id, ch_fields)
                if niche_id:
                    persist_channel_niche_membership(conn, ch_id, niche_id, is_primary=True, confidence=0.8)
                classified += 1
                channels_enriched += 1
            except Exception:
                continue

        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="classify_channel",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
            ).model_dump())
            continue

    put_connection(conn)
    return {
        "node_logs": _log({"classified": classified, "eligible": len(eligible)}),
        "channels_enriched_this_run": channels_enriched,
        "errors": errors,
        "budget_spent_usd": 0.0,  # cost tracked per-token, not per-node
    }
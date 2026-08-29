"""score_thumbnail_signals — Kimi vision call for thumbnail patterns.

Gated by meets_subscriber_floor (§6.4). Samples a fixed number of recent
thumbnails per channel, calls the vision model, and writes structured signals
to the videos table. Never sees raw channel dumps — just thumbnail URLs.

The floor gate is checked at the start of the node: only floor-qualifying
channels that haven't been scored yet are processed.
"""

from __future__ import annotations

import json
import time

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier
from src.state import NodeLog, ErrorRecord

SYSTEM_PROMPT = """You are a thumbnail analyst. For each thumbnail URL, answer two questions:

1. does_face: Does a human face appear in the thumbnail? (true/false)
2. text_density: How much text overlay is on the thumbnail?
   "none" — no text at all
   "low" — 1-3 words, small font
   "medium" — a short phrase or sentence
   "high" — dense text, multiple lines, or very large font

Respond with ONLY a JSON array of objects, one per thumbnail:
[{"does_face": true, "text_density": "medium"}, ...]"""


def score_thumbnail_signals(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    cfg = get_config().harness
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="score_thumbnail_signals",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "scored": 0})}

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT channel_id FROM channels WHERE meets_subscriber_floor = TRUE "
            "AND face_status = 'unknown' LIMIT 50"
        )
        eligible = [r[0] for r in cur.fetchall()]
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "scored": 0})}

    scored = 0
    from src.tools.dedup import persist_video_v3, persist_channel_v3

    for ch_id in eligible:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT video_id, thumbnails FROM videos WHERE channel_id = %s "
                "ORDER BY published_at DESC NULLS LAST LIMIT %s",
                (ch_id, cfg.thumbnail_sample_count),
            )
            video_rows = cur.fetchall()
            cur.close()

            # Collect thumbnail URLs
            thumb_urls: list[str] = []
            vid_ids: list[str] = []
            for vid_id, thumbs_json in video_rows:
                if not thumbs_json:
                    continue
                try:
                    thumbs = json.loads(thumbs_json) if isinstance(thumbs_json, str) else thumbs_json
                    url = None
                    if isinstance(thumbs, dict):
                        url = thumbs.get("high", {}).get("url") or thumbs.get("medium", {}).get("url") or thumbs.get("default", {}).get("url")
                    if url:
                        thumb_urls.append(url)
                        vid_ids.append(vid_id)
                except Exception:
                    continue

            if not thumb_urls:
                continue

            # Vision call
            try:
                prompt = "Analyze these thumbnails:\n" + "\n".join(thumb_urls)
                result = complete_tier("thumbnail_vision", prompt, SYSTEM_PROMPT)
                content = result.get("content", "")
                parsed = json.loads(content) if isinstance(content, str) else content
                if not isinstance(parsed, list):
                    continue
            except Exception as exc:
                errors.append(ErrorRecord(
                    node_name="score_thumbnail_signals",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=True,
                ).model_dump())
                continue

            # Write per-video signals
            for i, vid_id in enumerate(vid_ids):
                if i >= len(parsed):
                    break
                sig = parsed[i] if isinstance(parsed[i], dict) else {}
                v_fields = {
                    "thumbnail_has_face": bool(sig.get("does_face", False)),
                    "thumbnail_text_density": str(sig.get("text_density", "none")),
                }
                try:
                    persist_video_v3(conn, vid_id, v_fields)
                except Exception:
                    continue

            # Channel-level: face_status tally
            if vid_ids and parsed:
                faces = sum(1 for p in parsed if isinstance(p, dict) and p.get("does_face"))
                if len(parsed) > 0:
                    ratio = faces / len(parsed)
                    if ratio >= 0.8:
                        face_status = "face"
                    elif ratio <= 0.2:
                        face_status = "faceless"
                    else:
                        face_status = "mixed"
                    persist_channel_v3(conn, ch_id, state.get("run_id", ""), {"face_status": face_status})

            scored += 1

        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="score_thumbnail_signals",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
            ).model_dump())
            continue

    put_connection(conn)
    return {
        "node_logs": _log({"scored": scored, "eligible": len(eligible)}),
        "errors": errors,
    }
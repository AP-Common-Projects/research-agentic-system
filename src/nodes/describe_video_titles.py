"""describe_video_titles — one-sentence, title-only description per video.

The video-level analogue of classify_channel's niche_description: not a
summary of the video's content (nothing here ever fetches a transcript or
thumbnail), just what the title itself indicates the video is likely about,
in plain language. Meant to be read across hundreds of rows at once when
comparing what successful vs. failing channels are titling their videos —
raw titles alone are hard to pattern-match at that scale; a normalized
one-sentence gloss is not.

Gated by meets_subscriber_floor, same as classify_channel/
score_thumbnail_signals/extract_success_failure_factors — this reasons over
the same floor-qualifying channels' content, batched to keep the ratio of
LLM calls to videos small (titles are short; dozens fit in one prompt).
"""

from __future__ import annotations

import json
import re
import time

from src.tools.run_scope import channel_scope
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
from src.state import NodeLog, ErrorRecord

SYSTEM_PROMPT = """You are given a numbered list of YouTube video titles. For each one, write a ONE-sentence, plain-language description of what the video is likely about, based solely on the title.

Rules:
- One sentence per title, no more.
- Describe the likely CONTENT/SUBJECT, not the title's wording style.
- If a title is too generic/ambiguous to say anything specific, describe it as generically as it deserves — do not invent specifics the title doesn't support.
- Preserve the exact order and count of the input titles.

Respond with ONLY a JSON array of strings, one per title, in the same order:
["...", "...", ...]"""

_BATCH_SIZE = 30


def _fetch_batch(conn, scope: list[str] | None = None) -> list[tuple[str, str]]:
    """Titles still lacking a description.

    `scope is not None` restricts to a channel set -- absent means the
    original global behaviour, present-but-empty means this worker owns
    nothing. Truthiness would collapse those two, which is how parallel
    workers with an empty slice ended up each re-running the whole table.
    """
    cur = conn.cursor()
    try:
        scope_sql = "AND v.channel_id = ANY(%s) " if scope is not None else ""
        params: tuple = ((scope, _BATCH_SIZE) if scope is not None
                         else (_BATCH_SIZE,))
        cur.execute(
            "SELECT v.video_id, v.title FROM videos v "
            "JOIN channels c ON c.channel_id = v.channel_id "
            "WHERE c.meets_subscriber_floor = TRUE AND v.video_description IS NULL "
            "AND v.title IS NOT NULL AND v.title <> '' "
            + scope_sql +
            "ORDER BY v.video_id LIMIT %s",
            params,
        )
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        cur.close()


def describe_video_titles(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    # channel_scope(), not the raw scope_channel_ids key: that key is never
    # set by a real graph run (only discovered_channel_ids is), so this
    # fell back to "absent -> unscoped" in every real run -- every run
    # described video titles across the entire database's backlog, not
    # its own videos, competing with other runs' leftovers for the same
    # _BATCH_SIZE window.
    scope = channel_scope(state)
    start = time.monotonic()
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="describe_video_titles",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "described": 0})}

    described = 0
    total_cost = 0.0
    total_seen = 0
    # Titles are a few words each — even at thousands of eligible videos this
    # stays cheap, but a ceiling still bounds worst-case cost per invocation.
    max_videos = 3000

    try:
        batch = _fetch_batch(conn, scope)
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "described": 0})}

    while batch:
        total_seen += len(batch)
        video_ids = [v[0] for v in batch]
        titles = [v[1] for v in batch]
        prompt = json.dumps(
            {"titles": [f"{i + 1}. {t}" for i, t in enumerate(titles)]}, indent=2
        )
        try:
            result = complete_tier("cheap", prompt, SYSTEM_PROMPT)
            usage = result.get("usage", {})
            total_cost += result.get(
                "cost_usd",
                estimate_cost("cheap", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
            )
            content = result.get("content", "")
            cleaned = content.strip()
            match = re.search(r"\[[\s\S]*\]", cleaned)
            if match:
                cleaned = match.group(0)
            descriptions = json.loads(cleaned)
            if not isinstance(descriptions, list):
                raise ValueError("response was not a JSON array")
        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="describe_video_titles",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
            ).model_dump())
            if total_seen >= max_videos:
                break
            try:
                batch = _fetch_batch(conn, scope)
            except Exception:
                break
            continue

        from src.tools.dedup import persist_video_v3

        for vid_id, desc in zip(video_ids, descriptions):
            if not isinstance(desc, str) or not desc.strip():
                continue
            try:
                persist_video_v3(conn, vid_id, {"video_description": desc.strip()[:500]})
                described += 1
            except Exception as exc:
                conn.rollback()
                errors.append(ErrorRecord(
                    node_name="describe_video_titles",
                    error_type=type(exc).__name__,
                    message=f"persist failed for {vid_id}: {exc}",
                    recoverable=True,
                ).model_dump())

        if total_seen >= max_videos:
            break
        try:
            batch = _fetch_batch(conn, scope)
        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="describe_video_titles",
                error_type=type(exc).__name__,
                message=f"batch re-fetch failed after {total_seen} videos: {exc}",
                recoverable=True,
            ).model_dump())
            break

    put_connection(conn)
    return {
        "node_logs": _log({
            "described": described,
            "seen": total_seen,
            "cost_usd": round(total_cost, 6),
        }),
        "errors": errors,
        "budget_spent_usd": total_cost,
    }

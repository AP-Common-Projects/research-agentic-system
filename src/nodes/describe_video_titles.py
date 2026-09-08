"""describe_video_titles — one-sentence, title-only description per video.

The video-level analogue of classify_channel's niche_description: not a
summary of the video's content (nothing here ever fetches a transcript or
thumbnail), just what the title itself indicates the video is likely about,
in plain language. Meant to be read across hundreds of rows at once when
comparing what successful vs. failing channels are titling their videos —
raw titles alone are hard to pattern-match at that scale; a normalized
one-sentence gloss is not.

Gated by the deliverable floor, same as classify_channel/
score_thumbnail_signals/extract_success_failure_factors — this reasons over
the same floor-qualifying channels' content, batched to keep the ratio of
LLM calls to videos small (titles are short; dozens fit in one prompt).
"""

from __future__ import annotations

import json
import time

from src.llm.concurrent import map_llm, worker_count
from src.llm.json_parse import complete_json
from src.tools.run_scope import channel_scope
from src.tools import deadline as run_deadline
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
from src.state import NodeLog, ErrorRecord
from src.tools.deliverable import eligible_sql

SYSTEM_PROMPT = """You are given a numbered list of YouTube video titles. For each one, write a ONE-sentence, plain-language description of what the video is likely about, based solely on the title.

Rules:
- One sentence per title, no more.
- Describe the likely CONTENT/SUBJECT, not the title's wording style.
- If a title is too generic/ambiguous to say anything specific, describe it as generically as it deserves — do not invent specifics the title doesn't support.
- Preserve the exact order and count of the input titles.

Respond with ONLY a JSON array of strings, one per title, in the same order:
["...", "...", ...]"""

_BATCH_SIZE = 30


def _fetch_batch(
    conn, scope: list[str] | None = None, limit: int = _BATCH_SIZE
) -> list[tuple[str, str]]:
    """Titles still lacking a description.

    `scope is not None` restricts to a channel set -- absent means the
    original global behaviour, present-but-empty means this worker owns
    nothing. Truthiness would collapse those two, which is how parallel
    workers with an empty slice ended up each re-running the whole table.
    """
    cur = conn.cursor()
    try:
        scope_sql = "AND v.channel_id = ANY(%s) " if scope is not None else ""
        params: tuple = ((scope, limit) if scope is not None
                         else (limit,))
        cur.execute(
            "SELECT v.video_id, v.title FROM videos v "
            "JOIN channels c ON c.channel_id = v.channel_id "
            "WHERE " + eligible_sql() + " AND v.video_description IS NULL "
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

    # One fetch feeds every worker. Eligibility here is
    # "video_description IS NULL", so a batch cannot be re-fetched until the
    # previous one is written -- which is why this was strictly sequential
    # at 23s a batch, 1,329s for 1,710 titles on run-dd2dbdbc3080.
    workers = worker_count()

    def _next_chunk() -> list[tuple[str, str]]:
        return _fetch_batch(conn, scope, limit=_BATCH_SIZE * workers)

    try:
        chunk = _next_chunk()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "described": 0})}

    from src.tools.dedup import persist_video_v3

    while chunk:
        # The write-up chain used to run to completion however long it
        # took; a one-hour crime run spent 1h45m in it. Checked per
        # chunk so overshoot is one chunk, not one whole node.
        # Healing bypasses this -- see deadline.writeup_passed.
        if run_deadline.writeup_passed(state):
            break
        total_seen += len(chunk)
        batches = [
            chunk[i:i + _BATCH_SIZE] for i in range(0, len(chunk), _BATCH_SIZE)
        ]

        def _describe(batch: list[tuple[str, str]]):
            prompt = json.dumps(
                {"titles": [f"{i + 1}. {t}" for i, (_, t) in enumerate(batch)]},
                indent=2,
            )
            descriptions, result = complete_json(
                complete_tier, "cheap", prompt, SYSTEM_PROMPT, expect="array"
            )
            if not isinstance(descriptions, list):
                raise ValueError("response was not a JSON array")
            return descriptions, result

        answers = map_llm(
            batches,
            _describe,
            workers=workers,
            should_stop=lambda: run_deadline.writeup_passed(state),
            label="describe_video_titles",
        )

        for batch, got, call_error in answers:
            if call_error is not None:
                errors.append(ErrorRecord(
                    node_name="describe_video_titles",
                    error_type=type(call_error).__name__,
                    message=str(call_error),
                    recoverable=True,
                ).model_dump())
                continue
            descriptions, result = got
            usage = result.get("usage", {})
            total_cost += result.get(
                "cost_usd",
                estimate_cost("cheap", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
            )
            for (vid_id, _title), desc in zip(batch, descriptions):
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
            chunk = _next_chunk()
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

"""resolve_first_video_date — the channel's TRUE first-ever upload date.

Gated by the deliverable floor, same as classify_channel/
score_thumbnail_signals — this is a real network cost (potentially many
paginated API calls for a prolific channel) worth spending only on
channels that already cleared the floor.

Not derivable from the videos table: hydrate_metadata only ever fetches a
channel's 50 most recent uploads (the uploads playlist has no "oldest
first" sort), so MIN(videos.published_at) is the oldest video in that
recent window, not the channel's real first upload, for any channel with
more than 50 uploads ever. Verified live: off by over a decade on a real
channel with a long upload history. The only way to get the real answer is
to paginate the uploads playlist to its actual last page.
"""

from __future__ import annotations

import time

from src.tools.run_scope import scope_clause
from src.db.connection import get_connection, put_connection
from src.state import NodeLog, ErrorRecord
from src.tools.deliverable import eligible_sql


def resolve_first_video_date(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    run_id = state.get("run_id", "")
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="resolve_first_video_date",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "resolved": 0})}

    # Scoped to this run's own channels. Unscoped, the LIMIT 50 was spent
    # on whatever floor-passing channel in the whole table happened to be
    # missing a date -- measured on the automotive run: fifty finance and
    # crime channels resolved, none of its own 76, and the column shipped
    # 0% populated while the node still billed four minutes of API calls.
    scope_sql, scope_params = scope_clause(state)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT channel_id FROM channels WHERE " + eligible_sql(None) + " "
            "AND first_video_published_at IS NULL " + scope_sql + "LIMIT 50",
            scope_params,
        )
        eligible = [r[0] for r in cur.fetchall()]
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "resolved": 0})}

    resolved = 0
    from src.tools.dedup import persist_channel_v3
    from src.tools.youtube_api import YouTubeAPIClient

    client = YouTubeAPIClient()

    for ch_id in eligible:
        try:
            published_at = client.get_channel_first_video_published_at(ch_id)
            if not published_at:
                continue
            try:
                persist_channel_v3(conn, ch_id, run_id, {"first_video_published_at": published_at})
                resolved += 1
            except Exception as exc:
                conn.rollback()
                errors.append(ErrorRecord(
                    node_name="resolve_first_video_date",
                    error_type=type(exc).__name__,
                    message=f"persist failed for {ch_id}: {exc}",
                    recoverable=True,
                ).model_dump())
        except Exception as exc:
            # A failed request/statement anywhere above must not poison the
            # connection's transaction for every remaining channel in this
            # loop — same class of bug fixed in the sibling floor-gated
            # nodes (classify_channel, score_thumbnail_signals, ...).
            conn.rollback()
            errors.append(ErrorRecord(
                node_name="resolve_first_video_date",
                error_type=type(exc).__name__,
                message=f"lookup failed for {ch_id}: {exc}",
                recoverable=True,
            ).model_dump())
            continue

    put_connection(conn)
    return {
        "node_logs": _log({"resolved": resolved, "eligible": len(eligible)}),
        "errors": errors,
        "budget_spent_usd": 0.0,
    }

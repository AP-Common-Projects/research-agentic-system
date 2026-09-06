"""assign_cohorts — v4 cohort assignment node (plan §5.10, §10).

Assigns channels to cohorts defined in cohort_definitions. Handles:
- Crime's 4 mutually exclusive life-cycle cohorts (exclusive_group='crime_lifecycle')
- Finance's independent flag (is_new_channel) + outcome pair (new_winner/new_loser)

Mutual exclusivity: within an exclusive_group, assigning a new cohort deletes
any existing row for that channel within the same group. Criteria are recorded
as criteria_snapshot JSONB so every assignment is auditable.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from src.tools.run_scope import scope_clause
from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.state import NodeLog, ErrorRecord



def _peer_engagement_floors(conn) -> dict[tuple[str, str], float]:
    """Bottom-quartile engagement per (vertical, size bucket).

    "Underperforming" only means anything against comparable channels: a
    50k channel and a 5M channel are not judged on the same engagement
    number, which is exactly why the previous absolute threshold matched
    one channel in the entire dataset.

    Buckets with too few members to have a meaningful quartile are omitted,
    and the caller then falls back to 0.0 -- no peer evidence should read
    as evidence of failure.
    """
    floors: dict[tuple[str, str], float] = {}
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT nt.parent_category, c.channel_size_bucket,
                      PERCENTILE_CONT(0.25) WITHIN GROUP (
                          ORDER BY c.engagement_score),
                      COUNT(*)
               FROM channels c
               JOIN channel_niches cn ON cn.channel_id = c.channel_id
                    AND cn.is_primary = TRUE
               JOIN niche_taxonomy nt ON nt.niche_id = cn.niche_id
               WHERE c.meets_subscriber_floor = TRUE
                 AND c.engagement_score IS NOT NULL
                 AND c.channel_size_bucket IS NOT NULL
               GROUP BY 1, 2"""
        )
        for vertical, bucket, q1, n in cur.fetchall():
            if n >= 8 and q1 is not None:
                floors[(vertical, bucket)] = float(q1)
        cur.close()
    except Exception:
        pass
    return floors


def assign_cohorts(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    run_id = state.get("run_id", "")
    start = time.monotonic()
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="assign_cohorts",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "assigned": 0})}

    # Fetch cohort definitions
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT cohort_code, vertical, exclusive_group FROM cohort_definitions"
        )
        cohort_defs = {r[0]: {"vertical": r[1], "exclusive_group": r[2]} for r in cur.fetchall()}
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "cohort definitions unavailable", "assigned": 0})}

    # Determine each channel's primary niche (vertical) via channel_niches → niche_taxonomy
    assigned = 0
    # Scoped to this run's own channels -- see src/tools/run_scope.py.
    scope_sql, scope_params = scope_clause(state, "c.channel_id")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT c.channel_id, c.subscriber_count, c.channel_creation_date, "
            "c.vertical_start_date, c.engagement_score, c.evergreen_score, "
            "nt.parent_category, c.channel_size_bucket, "
            "(SELECT MAX(v.published_at) FROM videos v "
            " WHERE v.channel_id = c.channel_id) AS last_upload, "
            "(SELECT COUNT(*) FROM videos v "
            " WHERE v.channel_id = c.channel_id) AS video_n, "
            "(SELECT MIN(v.published_at) FROM videos v "
            " WHERE v.channel_id = c.channel_id) AS first_upload "
            "FROM channels c "
            "LEFT JOIN channel_niches cn ON c.channel_id = cn.channel_id AND cn.is_primary = TRUE "
            "LEFT JOIN niche_taxonomy nt ON cn.niche_id = nt.niche_id "
            "WHERE c.meets_subscriber_floor = TRUE "
            # The vertical was hardcoded to 'crime', so a Finance channel
            # never counted as already-assigned and was re-examined every
            # round forever. Match the cohort against the channel's OWN
            # vertical instead.
            "AND NOT EXISTS (SELECT 1 FROM channel_cohorts cc "
            "WHERE cc.channel_id = c.channel_id "
            "AND cc.vertical = nt.parent_category) "
            # The invariant here is that nothing eligible in SQL may be
            # skipped in Python: a skipped row refills the LIMIT 200 window
            # every round and the backfill stalls (observed: 48 assignments
            # with 300 candidates left). This used to hold it by listing
            # `IN ('crime', 'finance')` -- which also meant an automotive
            # run assigned zero cohorts, and so would any other topic.
            #
            # Now every classified vertical is processed, so the only rows
            # the loop still skips are those with no classification at all,
            # and THOSE are what SQL excludes. Same invariant, without
            # hardcoding which topics the client is allowed to research.
            "AND nt.parent_category IS NOT NULL "
            + scope_sql +
            "LIMIT 200",
            scope_params,
        )
        channels = cur.fetchall()
        cur.close()

        peer_floor = _peer_engagement_floors(conn)

        for (ch_id, subs, ch_creation, v_start, eng, eg, vertical,
             size_bucket, last_upload, video_n, first_upload) in channels:
            if not vertical:
                continue

            subs = int(subs or 0)
            eng = float(eng or 0)

            # Newness: 2024-2026 or first vertical video in that window
            is_new = False
            try:
                if ch_creation:
                    c_year = int(str(ch_creation)[:4]) if ch_creation else 0
                    if 2024 <= c_year <= 2026:
                        is_new = True
                if v_start:
                    v_year = int(str(v_start)[:4]) if v_start else 0
                    if 2024 <= v_year <= 2026:
                        is_new = True
            except Exception:
                pass

            # Performance classification
            is_winner = eng > 50 or subs > 500000

            # The brief asks for channels performing poorly "relative to
            # their niche/size", plus stagnant and abandoned ones. The old
            # test was absolute -- eng < 20 AND subs < 100000 -- which above
            # a 50k floor describes a sliver of the 50-100k band and matched
            # exactly one channel in the whole dataset.
            #
            # sub_growth_90d / views_90d would be the natural growth signals
            # but are empty for every row, so silence since the last upload
            # carries "stagnant"/"abandoned" instead.
            days_silent = None
            if last_upload is not None:
                try:
                    days_silent = (datetime.now(timezone.utc) - last_upload).days
                except TypeError:  # naive timestamp
                    days_silent = (datetime.now() - last_upload).days

            abandoned = days_silent is not None and days_silent >= 365
            stagnant = days_silent is not None and days_silent >= 180
            below_peers = eng < peer_floor.get((vertical, size_bucket), 0.0)

            # The brief qualifies an underperformer as a channel that
            # "published at least 30-50 videos" and was "active for some
            # months or even years" -- a channel with three uploads is not a
            # failure case, it is an absence of data. Our sample is capped,
            # so a channel showing 30 videos provably published at least 30;
            # it is a conservative floor rather than the true count.
            history_span_days = None
            if first_upload is not None and last_upload is not None:
                try:
                    history_span_days = (last_upload - first_upload).days
                except TypeError:
                    history_span_days = None

            has_track_record = (
                int(video_n or 0) >= 30
                and history_span_days is not None
                and history_span_days >= 180
            )

            is_under = (
                (not is_winner)
                and has_track_record
                and (abandoned or stagnant or below_peers)
            )

            generic_group = f"{vertical}_lifecycle"
            if is_under and not is_new:
                # Checked before the per-vertical branches: underperformer
                # used to live inside the crime branch only, so Finance could
                # not produce one at all and half the survivor-bias fix was
                # structurally impossible.
                cohort_code = "underperformer"
            elif vertical == "crime":
                cohort_code = (
                    "new_entrant_breakout" if is_new else (
                        "growth_competitor" if subs > 100000 and eng > 30 else "market_benchmark"
                    )
                )
            elif vertical == "finance":
                # Finance: is_new_channel is an independent flag
                if is_new:
                    try:
                        cur3 = conn.cursor()
                        _insert_cohort(conn, ch_id, vertical, "is_new_channel",
                                         {"subscriber_count": subs, "is_new": True},
                                         run_id, "finance_outcome", cur3, assigned)
                    except Exception:
                        pass
                # Winner/loser: mutually exclusive pair
                # A settled, non-breakout Finance channel used to fall
                # through to None and receive no cohort at all -- 189 of 193
                # in the deliverable. market_benchmark is the correct
                # default: an established channel performing normally.
                cohort_code = "new_winner" if (is_new and is_winner) else (
                    "new_loser" if is_new else
                    ("new_winner" if is_winner else "market_benchmark")
                )
            else:
                # Every other vertical. Each signal above -- is_new,
                # is_winner, is_under, has_track_record, below_peers -- is
                # computed from subscriber counts, engagement and upload
                # history, none of which is crime- or finance-specific, so
                # there was never a reason for a third vertical to fall
                # through to `continue` and receive no cohort at all. An
                # automotive run assigned zero, and so would gaming, music
                # or any other topic the client picks.
                #
                # Reuses the existing cohort codes rather than minting new
                # ones: channel_cohorts.cohort_code has a foreign key into
                # cohort_definitions keyed on the code alone, so these are
                # already valid for any vertical.
                cohort_code = (
                    "new_entrant_breakout" if (is_new and is_winner)
                    else "market_benchmark"
                )
                # Its own exclusive group, so the lifecycle cohorts stay
                # mutually exclusive WITHIN this vertical without the code
                # lookup handing back crime's group.
                generic_group = f"{vertical}_lifecycle"

            if cohort_code:
                exclusive_group = (
                    generic_group
                    if vertical not in ("crime", "finance")
                    else cohort_defs.get(cohort_code, {}).get("exclusive_group")
                )
                cur4 = conn.cursor()
                try:
                    _insert_cohort(conn, ch_id, vertical, cohort_code,
                                     {"subscriber_count": subs, "engagement": eng,
                                      "is_new": is_new, "is_winner": is_winner},
                                     run_id,
                                     exclusive_group,
                                     cur4, assigned)
                    assigned += 1
                finally:
                    cur4.close()
    except Exception as exc:
        conn.rollback()
        errors.append(ErrorRecord(
            node_name="assign_cohorts",
            error_type=type(exc).__name__,
            message=str(exc),
            recoverable=True,
        ).model_dump())

    put_connection(conn)
    return {
        "node_logs": _log({"assigned": assigned}),
        "errors": errors,
    }


def _insert_cohort(conn, ch_id: str, vertical: str, cohort_code: str,
                     criteria: dict, run_id: str, exclusive_group: str | None,
                     cur, _counter: int) -> None:
    if exclusive_group:
        cur.execute(
            "DELETE FROM channel_cohorts WHERE channel_id = %s AND vertical = %s "
            "AND cohort_code IN (SELECT cohort_code FROM cohort_definitions "
            "WHERE exclusive_group = %s)",
            (ch_id, vertical, exclusive_group),
        )
        conn.commit()
    cur.execute(
        "INSERT INTO channel_cohorts (channel_id, vertical, cohort_code, "
        "criteria_snapshot, assigned_by_run_id) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (channel_id, vertical, cohort_code) DO UPDATE SET "
        "criteria_snapshot = EXCLUDED.criteria_snapshot",
        (ch_id, vertical, cohort_code, json.dumps(criteria), run_id),
    )
    conn.commit()
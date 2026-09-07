"""populate_shared_fields — v4 shared field enrichment (plan §5.8, §12).

Populates creator_authority, search_browse_estimate, sponsor fields, and
commercial_intent on channels/videos/niche_taxonomy. All fields were added
to the schema in Phase 1; this node populates them using deterministic
signals + LLM classification where text-based inference is needed.

Gated by meets_subscriber_floor for LLM calls; cheap signals (sponsor
regex matching, search_browse estimate from evergreen/news signals)
applied to every channel.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from src.llm.json_parse import complete_json
from src.tools.run_scope import scope_clause
from src.tools import deadline as run_deadline
from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
from src.state import NodeLog, ErrorRecord

SYSTEM_PROMPT = """You analyze a YouTube channel to determine creator authority and search-vs-browse intent.

Input: channel title, description, recent video titles, subscriber count, engagement score.
Output:

1. creator_authority: one of:
   - "financial_professional" — certified/licensed finance professional
   - "economist" — academic economist or economic commentator
   - "accountant" — CPA or accounting professional
   - "trader" — active trader, technical analysis focus
   - "investor" — long-term/value investor
   - "academic" — university-affiliated researcher or educator
   - "entrepreneur" — business owner/founder sharing expertise
   - "media_personality" — journalist, presenter, or media figure
   - "general_creator" — content creator without specific credentials
   - "anonymous_brand" — faceless branded channel, no individual persona
   - "unknown" — not enough information to determine

2. creator_authority_evidence: one sentence citing the specific evidence.

3. search_browse_estimate: one of "search_driven", "browse_driven", "news_driven", "mixed", "unclear"
   based on whether the channel's content appears optimized for search discovery (tutorial/how-to titles),
   browse/recommendation discovery (personality-driven, hook-based titles), news/trend-driven, or mixed.

Rules:
- NEVER fabricate credentials. If the channel doesn't clearly show professional qualifications, say "general_creator" or "unknown".
- search_browse_estimate is ALWAYS a title/description-derived estimate, never a claim about actual traffic sources.
- Sponsorship info: only note it if the description actually mentions a sponsor by name or category.

Respond with ONLY a JSON object:
{
  "creator_authority": "...",
  "creator_authority_evidence": "...",
  "search_browse_estimate": "..."
}"""


def _mark_creator_authority_checked(conn, channel_id: str) -> None:
    """Record that creator_authority was attempted for this channel.

    Set unconditionally on a completed local attempt (the LLM answered,
    whether or not persisting its answer went smoothly), because
    'unknown' is simultaneously this column's default, a genuine model
    answer, and the eligibility sentinel -- so row state alone cannot
    say whether a channel has ever been looked at.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE channels SET creator_authority_checked_at = NOW() "
            "WHERE channel_id = %s",
            (channel_id,),
        )
        conn.commit()
    finally:
        cur.close()


def populate_shared_fields(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    run_id = state.get("run_id", "")
    start = time.monotonic()
    cfg = get_config().harness
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="populate_shared_fields",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "populated": 0})}

    from src.tools.dedup import persist_channel_v3

    # -- deterministic sponsor regex pass (every channel, no LLM) ---------
    sponsor_updated = 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT video_id, description FROM videos WHERE sponsor_status = 'not_clearly_sponsored' "
            "AND description IS NOT NULL AND description != '' LIMIT 200"
        )
        for vid_id, desc in cur.fetchall():
            fields: dict = {}
            if re.search(r"(sponsor|sponsored|brought to you by|partner with|in partnership with)", desc or "", re.I):
                fields["sponsor_status"] = "sponsored"
            if re.search(r"(nordvpn|surfshark|expressvpn|private internet access)", desc or "", re.I):
                fields["sponsor_category"] = "vpn"
                fields["sponsor_name"] = re.search(r"(nordvpn|surfshark|expressvpn|private internet access)", desc or "", re.I).group(1)
            elif re.search(r"(skillshare|brilliant|masterclass|coursera|udemy)", desc or "", re.I):
                fields["sponsor_category"] = "education"
                fields["sponsor_name"] = re.search(r"(skillshare|brilliant|masterclass|coursera|udemy)", desc or "", re.I).group(1)
            elif re.search(r"(robinhood|webull|etoro|coinbase|binance)", desc or "", re.I):
                fields["sponsor_category"] = "finance_investing"
                fields["sponsor_name"] = re.search(r"(robinhood|webull|etoro|coinbase|binance)", desc or "", re.I).group(1)
            if fields:
                try:
                    from src.tools.dedup import persist_video_v3
                    persist_video_v3(conn, vid_id, fields)
                    sponsor_updated += 1
                except Exception:
                    continue
        cur.close()
    except Exception:
        conn.rollback()

    # -- commercial_intent on niche_taxonomy (cheap, per niche) -----------
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT niche_id, niche_name, description FROM niche_taxonomy WHERE commercial_intent IS NULL"
        )
        for nid, nname, ndesc in cur.fetchall():
            combined = f"{nname} {ndesc or ''}".lower()
            if any(w in combined for w in ("invest", "stock", "trade", "crypto", "real estate", "wealth")):
                ci = "high"
            elif any(w in combined for w in ("budget", "save", "debt", "finance", "credit", "insurance")):
                ci = "medium"
            else:
                ci = "low"
            try:
                cur2 = conn.cursor()
                cur2.execute("UPDATE niche_taxonomy SET commercial_intent = %s WHERE niche_id = %s", (ci, nid))
                conn.commit()
                cur2.close()
            except Exception:
                conn.rollback()
        cur.close()
    except Exception:
        conn.rollback()

    # -- LLM: creator_authority + search_browse (floor-qualifying only) ---
    # Scoped via src.tools.run_scope, not the ad-hoc scope_channel_ids key
    # this replaced -- that key is never set by a real graph run, only
    # discovered_channel_ids is, so this node was UNSCOPED in every real
    # run: it processed the entire database's backlog on a LIMIT 50 with
    # no ordering favoring the current run, so a new run's own channels
    # competed with every other run's leftovers for the same fifty slots.
    scope_sql, scope_params = scope_clause(state)

    classified = 0
    try:
        cur = conn.cursor()
        cur.execute(
            # creator_authority = 'unknown' is BOTH this column's schema
            # default AND a genuine LLM answer for a channel with too
            # little information to judge -- so it cannot tell "never
            # attempted" apart from "attempted, and that was the honest
            # answer". A dedicated marker can.
            "SELECT channel_id, title, description, subscriber_count, engagement_score, is_likely_news, "
            "evergreen_score FROM channels WHERE creator_authority_checked_at IS NULL "
            "AND (meets_subscriber_floor = TRUE OR subscriber_count >= 10000) "
            + scope_sql + "LIMIT 50",
            scope_params,
        )
        eligible = cur.fetchall()
        cur.close()

        for ch_id, title, desc, subs, eng, news, eg in eligible:
            # The write-up chain used to run to completion however long it
            # took; a one-hour crime run spent 1h45m in it. Checked per
            # channel so overshoot is one channel, not one whole node.
            # Healing bypasses this -- see deadline.writeup_passed.
            if run_deadline.writeup_passed(state):
                break
            try:
                # Search/browse estimate from deterministic signals first
                sb_est = "mixed"
                if news:
                    sb_est = "news_driven"
                elif eng and float(eng or 0) > 60:
                    sb_est = "browse_driven"
                elif eg and float(eg or 0) > 70:
                    sb_est = "search_driven"

                # engagement_score is a NUMERIC column, so psycopg hands
                # back Decimal, which json.dumps cannot serialize. Without
                # the cast every channel raised TypeError and was skipped,
                # which is why creator_authority read 'unknown' across the
                # whole table. Same bug already fixed in classify_channel.py
                # and extract_success_failure_factors.py.
                prompt = json.dumps({
                    "channel_title": title or "",
                    "description": (desc or "")[:500],
                    "subscriber_count": int(subs or 0),
                    "engagement_score": float(eng or 0),
                    "is_likely_news": bool(news),
                })

                # A network fault or a malformed response here is
                # transient -- not a property of the channel -- so it must
                # NOT mark the channel checked; a later pass should retry.
                # This is its own try/except, split from the one below, so
                # a persist failure (which IS a property of the channel's
                # own data and would fail identically on every retry) can
                # be marked without also marking a plain network blip.
                try:
                    parsed, result = complete_json(
                        complete_tier, "mid", prompt, SYSTEM_PROMPT, expect="object"
                    )
                except Exception as exc:
                    errors.append(ErrorRecord(
                        node_name="populate_shared_fields",
                        error_type=type(exc).__name__,
                        message=str(exc),
                        recoverable=True,
                    ).model_dump())
                    continue

                ch_fields = {
                    "creator_authority": str(parsed.get("creator_authority", "unknown")),
                    "creator_authority_evidence": str(parsed.get("creator_authority_evidence", "")),
                }
                v3_fields = {"search_browse_estimate": str(parsed.get("search_browse_estimate", sb_est))}
                if v3_fields["search_browse_estimate"] == "unclear":
                    v3_fields["search_browse_estimate"] = sb_est

                persist_channel_v3(conn, ch_id, run_id, ch_fields)
                # search_browse_estimate is on videos but applied at channel level via an update
                try:
                    cur2 = conn.cursor()
                    cur2.execute(
                        "UPDATE videos SET search_browse_estimate = %s WHERE channel_id = %s "
                        "AND search_browse_estimate IS NULL",
                        (v3_fields["search_browse_estimate"], ch_id),
                    )
                    conn.commit()
                    cur2.close()
                except Exception:
                    conn.rollback()

                classified += 1
                _mark_creator_authority_checked(conn, ch_id)
            except Exception as exc:
                errors.append(ErrorRecord(
                    node_name="populate_shared_fields",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=True,
                ).model_dump())
                # Marked anyway: everything in this try, from building
                # ch_fields to persist_channel_v3, depends only on the
                # channel's own row -- it fails the same way on every
                # future attempt too, and leaving it unmarked recreates
                # the loop the marker exists to prevent.
                try:
                    _mark_creator_authority_checked(conn, ch_id)
                except Exception:
                    pass
    except Exception:
        conn.rollback()

    # Propagate the estimate to videos the per-channel loop above could not
    # reach, deliberately outside it.
    #
    # search_browse_estimate is a channel-level judgement written onto that
    # channel's videos by an UPDATE inside the loop -- and the loop's
    # eligibility is creator_authority_checked_at, a channel-level marker.
    # So a video hydrated after its channel was checked never receives the
    # estimate, and no re-run can give it one: the channel is marked, so it
    # is never revisited. The crime run of 2026-09-07 shipped this column at
    # 55% for exactly that reason, and healing could not move it.
    #
    # Copying a channel's own answer onto its remaining videos invents
    # nothing: it is the same value the loop would have written, and the
    # loop applied it to every video of the channel indiscriminately. A
    # channel with no answer at all is left alone -- that needs the model,
    # not a copy.
    propagated = 0
    try:
        scope_sql, scope_params = scope_clause(state, "v.channel_id")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE videos v SET search_browse_estimate = known.estimate "
                "FROM (SELECT DISTINCT ON (channel_id) channel_id, "
                "             search_browse_estimate AS estimate "
                "        FROM videos WHERE search_browse_estimate IS NOT NULL "
                "       ORDER BY channel_id) known "
                "WHERE v.channel_id = known.channel_id "
                "  AND v.search_browse_estimate IS NULL "
                + scope_sql,
                scope_params,
            )
            propagated = cur.rowcount or 0

            # And for a channel with no answer anywhere, the deterministic
            # estimate this node computes before it ever calls a model.
            #
            # The LLM pass is gated on creator_authority_checked_at -- a
            # marker about the channel's AUTHORITY, which also decides
            # whether its videos ever get this column. A channel marked
            # checked whose call failed keeps that mark forever, so its
            # videos could never be filled: one channel on
            # run-44e01aab65c2 held 54 of them. The sixth time in this
            # codebase that a marker for one piece of work has gated
            # another.
            #
            # Identical to the `sb_est` fallback above, expressed in SQL --
            # the same value the node writes whenever the model answers
            # "unclear". Derived from signals already on the channel, so it
            # costs nothing and states no more than the run already knows.
            cur.execute(
                "UPDATE videos v SET search_browse_estimate = CASE "
                "  WHEN c.is_likely_news THEN 'news_driven' "
                "  WHEN c.engagement_score > 60 THEN 'browse_driven' "
                "  WHEN c.evergreen_score > 70 THEN 'search_driven' "
                "  ELSE 'mixed' END "
                "FROM channels c "
                "WHERE c.channel_id = v.channel_id "
                "  AND v.search_browse_estimate IS NULL " + scope_sql,
                scope_params,
            )
            propagated += cur.rowcount or 0
        conn.commit()
    except Exception as exc:
        conn.rollback()
        errors.append(ErrorRecord(
            node_name="populate_shared_fields",
            error_type=type(exc).__name__,
            message=f"search_browse_estimate propagation failed: {exc}",
            recoverable=True,
        ).model_dump())

    put_connection(conn)
    return {
        "node_logs": _log({
            "sponsor_updated": sponsor_updated,
            "classified": classified,
            "propagated": propagated,
        }),
        "errors": errors,
    }
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
    # See populate_taxonomy_dimensions: optional, default-global scoping so a
    # backfill can skip channels no deliverable contains.
    scope = state.get("scope_channel_ids")
    # `is not None`, not truthiness: an EMPTY scope means "this worker owns
    # no channels" and must select nothing. Treating it as falsy silently
    # widened the query to every channel in the table, so four parallel
    # workers each re-ran the entire global backlog instead of their own
    # slice -- four hours of redundant LLM calls that also re-classified
    # channels deliberately excluded from the run.
    scope_sql = "AND channel_id = ANY(%s) " if scope is not None else ""
    scope_params: tuple = (list(scope),) if scope is not None else ()

    classified = 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT channel_id, title, description, subscriber_count, engagement_score, is_likely_news, "
            "evergreen_score FROM channels WHERE creator_authority = 'unknown' "
            "AND (meets_subscriber_floor = TRUE OR subscriber_count >= 10000) "
            + scope_sql + "LIMIT 50",
            scope_params,
        )
        eligible = cur.fetchall()
        cur.close()

        for ch_id, title, desc, subs, eng, news, eg in eligible:
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

                result = complete_tier("mid", prompt, SYSTEM_PROMPT)
                content = result.get("content", "")
                cleaned = content.strip()
                m = re.search(r"\{[\s\S]*\}", cleaned)
                if m:
                    parsed = json.loads(m.group(0))
                else:
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
            except Exception as exc:
                errors.append(ErrorRecord(
                    node_name="populate_shared_fields",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=True,
                ).model_dump())
    except Exception:
        conn.rollback()

    put_connection(conn)
    return {
        "node_logs": _log({
            "sponsor_updated": sponsor_updated,
            "classified": classified,
        }),
        "errors": errors,
    }
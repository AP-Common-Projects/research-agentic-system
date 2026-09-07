"""populate_taxonomy_dimensions — v4 taxonomy dimension extraction (brief §3).

Extends classify_channel's work: instead of reducing a channel to one niche_name,
extracts 5 independent dimensions plus records the raw pre-canonicalized label.

Gated by meets_subscriber_floor. Writes via persist_channel_v3.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.llm.json_parse import complete_json
from src.tools.run_scope import scope_clause
from src.tools import deadline as run_deadline
from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
from src.state import NodeLog, ErrorRecord

SYSTEM_PROMPT = """You classify a YouTube channel into independent taxonomy dimensions.

Input: channel title, description, existing niche classification, recent video titles.

Output: 5 independent fields, each one value from its allowed set:

1. primary_topic: the main subject the channel covers, as a SPECIFIC
   category within its own vertical (given to you below as "vertical") --
   not the vertical's name itself, a narrower category inside it. Title
   Case, 2-4 words, the kind of label a person in that space would
   recognise as its own content category.

   Calibration examples, showing the SPECIFICITY expected -- not a closed
   list, and not swappable across verticals:
     Finance:    "Retirement", "Investing", "Personal Finance", "Real Estate",
                 "Crypto", "Tax", "Business Finance", "Trading"
     Crime:      "Bodycam", "Cold Case", "Court Trial", "Forensic Science",
                 "True Crime Storytelling", "Missing Persons"
     Automotive: "Car Reviews", "DIY Repair", "Classic Cars", "Motorsport"

   For a vertical not shown above, invent an equally specific, real label
   for what this channel actually covers -- do not borrow a label from a
   different vertical's list, and do not answer with the vertical's own
   name.

   "Other" means "no specific category applies to this channel", not "no
   category was provided for this vertical" -- in a real vertical this
   should be rare. If you can name anything more specific than "Other",
   name it.

2. secondary_topic: the specific angle within the primary topic.
   Examples: "Retirement Planning", "Dividend Strategy", "Options Trading",
   "Suspect Interview Analysis", "CCTV Analysis", "Cold Case Reopening"

3. geography_focus: the geographic scope of the content.
   "Global", "US", "Canada", "UK", "Europe", "Asia", "Australia/NZ",
   "India", "Middle East", "Latin America", "Multi-Region", "None/Generic"

4. target_audience: who this content is primarily for.
   "Beginners", "Intermediate", "Advanced/Professional",
   "Retirees / 50+", "Young Adults / 20-35", "Gen Z / Under 25",
   "Women", "Immigrants / Expats", "Small Business Owners",
   "General Audience", "Niche Enthusiasts"

5. content_approach: the pedagogical or presentational style.
   "Educational", "News/Commentary", "Entertainment", "Investigative",
   "Storytelling/Documentary", "Interview-Based", "Tutorial/How-To",
   "Case Study", "Personal Experience/Vlog"

Rules:
- Select exactly one value per field from the allowed sets above.
- If genuinely ambiguous, select the closest match, not "Other"/"General".
- NEVER fabricate. If insufficient evidence, use the most conservative label.
- dominant_format (face/faceless/vlog/animated etc.) is NOT one of these fields — skip it.

Respond with ONLY a JSON object:
{
  "primary_topic": "...",
  "secondary_topic": "...",
  "geography_focus": "...",
  "target_audience": "...",
  "content_approach": "..."
}"""


def populate_taxonomy_dimensions(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    run_id = state.get("run_id", "")
    start = time.monotonic()
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="populate_taxonomy_dimensions",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "populated": 0})}

    # Optional restriction to the channels a deliverable actually contains.
    # The default stays global, so normal runs are unaffected; a backfill can
    # pass scope_channel_ids to avoid paying for channels no workbook will
    # ever show. Unscoped, one backfill had 3,415 channels queued at ~2.8/min
    # -- 20 hours and roughly $8 -- to populate ~780 that mattered.
    # Scoped to this run's own channels via src.tools.run_scope, not the
    # ad-hoc scope_channel_ids handling this replaced. That key is never
    # actually set by a real graph run -- only discovered_channel_ids is --
    # so this node was UNSCOPED in every real run, always, regardless of
    # what launched it: it processed the entire database's backlog on a
    # LIMIT 50 with no ordering favoring the current run, meaning a new
    # run's own channels competed with every other run's leftover backlog
    # for the same fifty slots and could lose every time.
    scope_sql, scope_params = scope_clause(state, "c.channel_id")

    # Find floor-qualifying channels without taxonomy dimensions yet
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT c.channel_id, c.title, c.description, c.dominant_format, "
            "cn.niche_id, nt.niche_name, nt.parent_category "
            "FROM channels c "
            "LEFT JOIN channel_niches cn ON c.channel_id = cn.channel_id AND cn.is_primary = TRUE "
            "LEFT JOIN niche_taxonomy nt ON cn.niche_id = nt.niche_id "
            "WHERE c.meets_subscriber_floor = TRUE "
            "AND c.primary_topic IS NULL "
            + scope_sql + "LIMIT 50",
            scope_params,
        )
        eligible = cur.fetchall()
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "populated": 0})}

    populated = 0
    from src.tools.dedup import persist_channel_v3, persist_channel_niche_membership

    for ch_id, title, desc, fmt, nid, nname, category in eligible:
        # The write-up chain used to run to completion however long it
        # took; a one-hour crime run spent 1h45m in it. Checked per
        # channel so overshoot is one channel, not one whole node.
        # Healing bypasses this -- see deadline.writeup_passed.
        if run_deadline.writeup_passed(state):
            break
        try:
            # Sample recent video titles for context
            cur2 = conn.cursor()
            cur2.execute(
                "SELECT title FROM videos WHERE channel_id = %s ORDER BY published_at DESC NULLS LAST LIMIT 5",
                (ch_id,),
            )
            sample_titles = [r[0] for r in cur2.fetchall()]
            cur2.close()

            prompt = json.dumps({
                "channel_title": title or "",
                "description": (desc or "")[:300],
                "niche": nname or "",
                "vertical": category or "",
                "dominant_format": fmt or "",
                "sample_video_titles": sample_titles,
            }, indent=2)

            parsed, result = complete_json(
                complete_tier, "mid", prompt, SYSTEM_PROMPT, expect="object"
            )

            fields = {
                "primary_topic": str(parsed.get("primary_topic", "")),
                "secondary_topic": str(parsed.get("secondary_topic", "")),
                "geography_focus": str(parsed.get("geography_focus", "")),
                "target_audience": str(parsed.get("target_audience", "")),
                "content_approach": str(parsed.get("content_approach", "")),
            }

            persist_channel_v3(conn, ch_id, run_id, fields)

            # Also capture raw_niche_label from the existing niche classification
            if nid:
                cur3 = conn.cursor()
                cur3.execute(
                    "UPDATE channel_niches SET raw_niche_label = COALESCE(raw_niche_label, %s) "
                    "WHERE channel_id = %s AND niche_id = %s",
                    (nname, ch_id, nid),
                )
                conn.commit()
                cur3.close()

            populated += 1
        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="populate_taxonomy_dimensions",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
            ).model_dump())

    # raw_niche_label, outside the loop above.
    #
    # It was written inside it, and that loop's eligibility is
    # "primary_topic IS NULL" -- a marker for entirely different work. Once
    # the topic was filled, by this node or by a heal, no channel was ever
    # eligible again and the label could never be set. The gaming workbook
    # of 2026-09-07 shipped raw_sub_niche at 0/10 with primary_topic at
    # 10/10, and re-running this node could not move it. The fifth time in
    # this codebase that a marker for one piece of work has gated another.
    #
    # It writes the canonical niche name, which is what the in-loop version
    # wrote too: the column comment describes the model's pre-canonical
    # proposal and nothing has ever captured that -- classify_channel does
    # not write this column at all. So this is the existing behaviour with
    # the wrong gate removed, not a new claim about the data. COALESCE via
    # the IS NULL guard keeps any genuine raw label already recorded.
    labelled = 0
    try:
        from src.tools.niche_labels import backfill_raw_niche_labels

        label_sql, label_params = scope_clause(state, "cn.channel_id")
        labelled = backfill_raw_niche_labels(conn, label_sql, label_params)
    except Exception as exc:
        conn.rollback()
        errors.append(ErrorRecord(
            node_name="populate_taxonomy_dimensions",
            error_type=type(exc).__name__,
            message=f"raw_niche_label backfill failed: {exc}",
            recoverable=True,
        ).model_dump())

    put_connection(conn)
    return {
        "node_logs": _log({
            "populated": populated,
            "eligible": len(eligible),
            "labelled": labelled,
        }),
        "errors": errors,
    }
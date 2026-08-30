"""populate_taxonomy_dimensions — v4 taxonomy dimension extraction (brief §3).

Extends classify_channel's work: instead of reducing a channel to one niche_name,
extracts 5 independent dimensions plus records the raw pre-canonicalized label.

Gated by meets_subscriber_floor. Writes via persist_channel_v3.
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

SYSTEM_PROMPT = """You classify a YouTube channel into independent taxonomy dimensions.

Input: channel title, description, existing niche classification, recent video titles.

Output: 5 independent fields, each one value from its allowed set:

1. primary_topic: the main subject the channel covers.
   For Finance: "Retirement", "Investing", "Personal Finance", "Economics", "Real Estate",
   "Crypto", "Tax", "Insurance", "Banking", "Business Finance", "Trading", "Other"
   For Crime: "Bodycam", "Police Investigation", "Interrogation", "Cold Case",
   "Digital Evidence", "Court Trial", "Forensic Science", "Criminal Psychology", "Other"

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
            "AND c.primary_topic IS NULL LIMIT 50"
        )
        eligible = cur.fetchall()
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "populated": 0})}

    populated = 0
    from src.tools.dedup import persist_channel_v3, persist_channel_niche_membership

    for ch_id, title, desc, fmt, nid, nname, category in eligible:
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

            result = complete_tier("mid", prompt, SYSTEM_PROMPT)
            content = result.get("content", "")
            cleaned = content.strip()
            m = re.search(r"\{[\s\S]*\}", cleaned)
            if not m:
                continue
            parsed = json.loads(m.group(0))

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

    put_connection(conn)
    return {
        "node_logs": _log({"populated": populated, "eligible": len(eligible)}),
        "errors": errors,
    }
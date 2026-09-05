"""Topic catalog and sub-niche discovery for the console's run launcher.

Two different questions, answered from two different sources, and the
console labels which is which:

  A topic the dataset already covers ("finance", "crime") has REAL
  sub-niches -- ones with channels behind them and counts to prove it.
  Those come from niche_taxonomy and are returned with `source: dataset`.

  A topic nobody has run yet has no evidence at all. Those sub-niches are
  proposed by the model and returned with `source: proposed`. Presenting a
  guess with the same authority as a measured count is how a client picks a
  sub-niche that turns out to have four channels in it.

The model call uses the cheap tier and is capped, because this runs while
someone is typing.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.db.connection import get_connection, put_connection

_MIN_CHANNELS_FOR_CATALOG = 20
_MAX_SUGGESTIONS = 14

SUGGEST_PROMPT = """You map a YouTube content topic into its distinct sub-niches.

Return ONLY a JSON array of 8-14 objects:
[{"name": "Bodycam / Police Incidents", "rationale": "one short clause"}]

Rules:
- A sub-niche is a distinct CONTENT FORMAT or SUBJECT within the topic that
  a viewer would recognise as its own kind of channel -- not an audience,
  not a country, not a company.
- Prefer sub-niches that plausibly have channels above 50,000 subscribers.
- Cover the topic's range, not just its most obvious corner.
- Name them the way a person would say them, in Title Case.
"""


def catalog() -> list[dict[str, Any]]:
    """Topics the dataset already has real coverage for."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT nt.parent_category,
                      COUNT(DISTINCT nt.niche_id)   AS niches,
                      COUNT(DISTINCT cn.channel_id) AS channels
               FROM niche_taxonomy nt
               LEFT JOIN channel_niches cn
                      ON cn.niche_id = nt.niche_id AND cn.is_primary
               WHERE nt.parent_category IS NOT NULL
               GROUP BY 1
               HAVING COUNT(DISTINCT cn.channel_id) >= %s
               ORDER BY 3 DESC""",
            (_MIN_CHANNELS_FOR_CATALOG,),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        put_connection(conn)

    return [
        {
            "id": r[0],
            "label": r[0].replace("_", " ").title(),
            "niche_count": r[1],
            "channel_count": r[2],
            "has_dataset": True,
        }
        for r in rows
    ]


def _dataset_subniches(topic: str) -> list[dict[str, Any]]:
    """Sub-niches with channels actually behind them, biggest first."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT nt.niche_name, COUNT(DISTINCT cn.channel_id) AS channels
               FROM niche_taxonomy nt
               JOIN channel_niches cn
                 ON cn.niche_id = nt.niche_id AND cn.is_primary
               WHERE lower(nt.parent_category) = lower(%s)
               GROUP BY 1
               HAVING COUNT(DISTINCT cn.channel_id) > 0
               ORDER BY 2 DESC
               LIMIT %s""",
            (topic, _MAX_SUGGESTIONS),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        put_connection(conn)

    return [
        {
            "name": r[0].replace("_", " ").title(),
            "slug": r[0],
            "channel_count": r[1],
            "source": "dataset",
            "rationale": f"{r[1]} channel(s) already in the dataset",
        }
        for r in rows
    ]


def _proposed_subniches(topic: str) -> list[dict[str, Any]]:
    """Model-proposed sub-niches for a topic with no coverage yet."""
    from src.llm.cascade import complete_tier

    result = complete_tier("cheap", f"Topic: {topic}", SUGGEST_PROMPT)
    content = (result.get("content") or "").strip()
    match = re.search(r"\[[\s\S]*\]", content)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    out: list[dict[str, Any]] = []
    for item in parsed[:_MAX_SUGGESTIONS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "slug": re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"),
                "channel_count": None,
                "source": "proposed",
                "rationale": str(item.get("rationale") or "").strip(),
            }
        )
    return out


def suggest(topic: str) -> dict[str, Any]:
    """Sub-niches for a topic, measured where possible and proposed otherwise."""
    topic = (topic or "").strip()
    if not topic:
        return {"topic": topic, "source": "none", "subniches": []}

    known = _dataset_subniches(topic)
    if known:
        return {
            "topic": topic,
            "source": "dataset",
            "note": (
                "These sub-niches already have channels in the dataset. "
                "Counts are real, not estimates."
            ),
            "subniches": known,
        }

    proposed = _proposed_subniches(topic)
    return {
        "topic": topic,
        "source": "proposed" if proposed else "none",
        "note": (
            "No dataset coverage for this topic yet, so these are proposed by "
            "the model and unverified. A run is what turns them into counts."
        ),
        "subniches": proposed,
    }

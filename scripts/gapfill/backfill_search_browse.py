"""search_browse_estimate for videos, from the same deterministic rule the
node uses before it ever calls a model.

populate_shared_fields derives this from signals it already has --
is_likely_news, engagement_score, evergreen_score -- and only reaches for
the LLM afterwards. Half the videos were left empty simply because that
node is channel-scoped and never walked the whole video table.
"""
import json
import sys

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config


def main() -> int:
    cat = sys.argv[1] if len(sys.argv) > 1 else "finance"
    sets = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/final_channel_sets.json"))
    ids = sorted(set(sets[cat]["have"]) | set(sets[cat].get("to_add") or []))
    conn = psycopg.connect(get_config().postgres.connection_string)
    cur = conn.cursor()
    # Mirrors populate_shared_fields: news first, then browse on high
    # engagement, then search on high evergreen, else mixed.
    cur.execute(
        """UPDATE videos v SET search_browse_estimate =
             CASE WHEN v.is_likely_news THEN 'news_driven'
                  WHEN c.engagement_score > 60 THEN 'browse_driven'
                  WHEN v.evergreen_score > 70 THEN 'search_driven'
                  ELSE 'mixed' END
           FROM channels c
           WHERE c.channel_id = v.channel_id
             AND v.channel_id = ANY(%s)
             AND v.search_browse_estimate IS NULL""",
        (ids,),
    )
    print(f"[{cat}] search_browse_estimate set on {cur.rowcount:,}", flush=True)
    conn.commit()
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

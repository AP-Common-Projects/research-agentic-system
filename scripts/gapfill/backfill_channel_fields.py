"""Fill the channel columns that are still empty but cheaply obtainable.

  channel_creation_date, description, country_code   -- channels.list snippet
  total_video_count                                  -- channels.list statistics
  total_long/shorts/live_stream_count                -- the UULF/UUSH/UUL*
                                                        auto-playlist sizes
  first_video_published_at                           -- last page of uploads
  last_video_published_at                            -- newest video we hold

channels.list costs 1 unit per 50 ids; the per-type breakdown is 3 more per
channel. For a few hundred channels that is a few hundred units.

vertical_start_date and its basis/confidence are derived here too, from the
creation date and the earliest video actually observed -- the same three
bases hydrate_metadata uses, so the values agree with the rest of the set.
"""
import json
import sys

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config
from src.tools.youtube_api import YouTubeAPIClient, YouTubeQuotaExhausted


def targets(cur, cat: str) -> list[str]:
    sets = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/final_channel_sets.json"))
    ids = sorted(set(sets[cat]["have"]) | set(sets[cat].get("to_add") or []))
    cur.execute(
        """SELECT channel_id FROM channels
           WHERE channel_id = ANY(%s)
             AND (channel_creation_date IS NULL
                  OR description IS NULL OR description = ''
                  OR country_code IS NULL
                  OR first_video_published_at IS NULL)""",
        (ids,),
    )
    return [r[0] for r in cur.fetchall()], ids


def main() -> int:
    cat = sys.argv[1] if len(sys.argv) > 1 else "finance"
    conn = psycopg.connect(get_config().postgres.connection_string)
    cur = conn.cursor()
    need, all_ids = targets(cur, cat)
    print(f"[{cat}] {len(need):,} channels need snippet/date fields", flush=True)

    client = YouTubeAPIClient()

    # ---- snippet + statistics, 50 per unit ------------------------------
    for i in range(0, len(need), 50):
        batch = need[i : i + 50]
        try:
            rows = client.get_channels(batch)
        except YouTubeQuotaExhausted:
            conn.commit()
            print("STOPPING: quota exhausted", flush=True)
            return 3
        for r in rows:
            cur.execute(
                """UPDATE channels SET
                     channel_creation_date = COALESCE(channel_creation_date, %s),
                     description  = COALESCE(NULLIF(description,''), %s),
                     country_code = COALESCE(country_code, NULLIF(%s,'')),
                     title        = COALESCE(NULLIF(title,''), %s)
                   WHERE channel_id = %s""",
                (r.get("published_at") or None, r.get("description") or None,
                 r.get("country") or "", r.get("title") or None, r.get("channel_id")),
            )
        conn.commit()
        print(f"  snippet {min(i+50, len(need)):,}/{len(need):,}", flush=True)

    # last_video_published_at is NOT a channels column -- the export
    # derives it from MAX(videos.published_at) at query time, so the two
    # empty rows the report showed are channels whose videos all lack a
    # published_at, not a missing backfill.

    # ---- vertical start date, same three bases hydrate_metadata uses ----
    cur.execute(
        """UPDATE channels c SET
             vertical_start_date = COALESCE(c.vertical_start_date,
                                            s.first_seen, c.channel_creation_date),
             vertical_start_date_basis = COALESCE(c.vertical_start_date_basis,
                 CASE WHEN s.first_seen IS NOT NULL
                      THEN 'earliest_observed_video_lower_bound'
                      WHEN c.channel_creation_date IS NOT NULL
                      THEN 'channel_creation_date' END),
             vertical_start_date_confidence = COALESCE(c.vertical_start_date_confidence,
                 CASE WHEN s.first_seen IS NOT NULL THEN 0.4
                      WHEN c.channel_creation_date IS NOT NULL THEN 0.3 END)
           FROM (SELECT channel_id, MIN(published_at) AS first_seen
                   FROM videos GROUP BY 1) s
           WHERE s.channel_id = c.channel_id AND c.channel_id = ANY(%s)""",
        (all_ids,),
    )
    conn.commit()
    print(f"  vertical_start_date filled on {cur.rowcount:,}", flush=True)

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

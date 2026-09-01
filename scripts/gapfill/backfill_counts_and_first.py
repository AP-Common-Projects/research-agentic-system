"""first_video_published_at and the per-upload-type counts.

first_video_published_at is resolved by walking the uploads playlist to its
LAST page -- not MIN(videos.published_at), which for any channel with more
than our sample would be the oldest video in a recent window and can be off
by a decade.

total_long/shorts/live_stream_count come from the sizes of YouTube's
auto-generated per-type playlists (UULF/UUSH/UULV), which sum exactly to the
uploads playlist. 3 quota units per channel.
"""
import json
import sys

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config
from src.tools.youtube_api import YouTubeAPIClient, YouTubeQuotaExhausted


def main() -> int:
    cat = sys.argv[1] if len(sys.argv) > 1 else "finance"
    sets = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/final_channel_sets.json"))
    ids = sorted(set(sets[cat]["have"]) | set(sets[cat].get("to_add") or []))

    conn = psycopg.connect(get_config().postgres.connection_string)
    cur = conn.cursor()
    client = YouTubeAPIClient()

    cur.execute(
        "SELECT channel_id FROM channels WHERE channel_id = ANY(%s) "
        "AND first_video_published_at IS NULL",
        (ids,),
    )
    need_first = [r[0] for r in cur.fetchall()]
    print(f"[{cat}] {len(need_first):,} need first_video_published_at", flush=True)
    done = 0
    for cid in need_first:
        try:
            first = client.get_channel_first_video_published_at(cid)
        except YouTubeQuotaExhausted:
            conn.commit(); print("STOPPING: quota", flush=True); return 3
        except Exception:
            first = None
        if first:
            cur.execute(
                "UPDATE channels SET first_video_published_at = %s WHERE channel_id = %s",
                (first, cid),
            )
            done += 1
            # Commit per channel. Batching every 25 held row locks on
            # `channels` across ~40s of playlist pagination, which blocked
            # any concurrent channel update -- the score backfill sat on a
            # Lock wait for four and a half minutes behind it.
            conn.commit()
        if done and done % 25 == 0:
            print(f"  first-date {done}/{len(need_first)}", flush=True)
    conn.commit()
    print(f"[{cat}] first_video_published_at set on {done:,}", flush=True)

    # ---- per-type counts into the newest channel_snapshots row -----------
    cur.execute(
        """SELECT c.channel_id FROM channels c
           WHERE c.channel_id = ANY(%s)
             AND NOT EXISTS (SELECT 1 FROM channel_snapshots s
                             WHERE s.channel_id = c.channel_id
                               AND s.long_video_count IS NOT NULL)""",
        (ids,),
    )
    need_counts = [r[0] for r in cur.fetchall()]
    print(f"[{cat}] {len(need_counts):,} need the upload breakdown", flush=True)
    got = 0
    for cid in need_counts:
        try:
            b = client.get_channel_upload_breakdown(cid)
        except YouTubeQuotaExhausted:
            conn.commit(); print("STOPPING: quota", flush=True); return 3
        except Exception:
            continue
        if not b:
            continue
        cur.execute(
            """INSERT INTO channel_snapshots
                 (channel_id, run_id, snapshot_at, long_video_count,
                  shorts_count, live_stream_count, total_video_count)
               VALUES (%s, %s, now(), %s, %s, %s, %s)""",
            (cid, "run-gapfill", b.get("long"), b.get("shorts"), b.get("live"),
             (b.get("long") or 0) + (b.get("shorts") or 0) + (b.get("live") or 0)),
        )
        got += 1
        if got % 25 == 0:
            conn.commit(); print(f"  breakdown {got}/{len(need_counts)}", flush=True)
    conn.commit()
    print(f"[{cat}] breakdown written for {got:,}", flush=True)
    cur.close(); conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""total_video_count / total_long/shorts/live_video_count, fixed properly.

The export joins channel_snapshots by `cs.run_id = t.run_id` -- the SPECIFIC
discovery run being read, not the channel generically. rebuild_workbooks'
collect() iterates every run_id in the deliverable's union and overwrites
`channels[id]` on each pass, last run wins. So a channel tagged to two runs,
with a breakdown snapshot under only one of them, surfaces the count only
if that happens to be the run collect() reads last -- otherwise the good
value is overwritten by a fetch with no snapshot at all.

51 of Finance's 350 channels are tagged to more than one run. A snapshot
written under a single made-up run_id (as an earlier version of this script
did) would never match any real run and would be invisible in every case.

Two passes:
  1. COPY. For a channel with an existing breakdown snapshot under any run,
     duplicate it under every run_id that channel is tagged to. Free -- no
     API call, and it is what recovers the ~30 channels that already had
     the data but under the wrong run_id.
  2. FETCH. Only channels with no breakdown anywhere left. One
     get_channel_upload_breakdown call each (3 quota units), written under
     every tagged run_id so the same problem cannot recur.
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
    union = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/run_ids_to_union.json"))
    ids = sorted(set(sets[cat]["have"]) | set(sets[cat].get("to_add") or []))
    NEW = {"finance": ["run-broad-fin", "run-broad-fin2"],
           "crime": ["run-broad-cri", "run-broad-cri2", "run-broad-cri3", "run-broad-cri4"]}
    rids = list(dict.fromkeys(union[cat] + NEW[cat]))

    conn = psycopg.connect(get_config().postgres.connection_string)
    cur = conn.cursor()

    cur.execute(
        """SELECT entity_id, array_agg(DISTINCT run_id) FROM category_tags
           WHERE entity_id = ANY(%s) AND entity_type = 'channel'
             AND run_id = ANY(%s)
           GROUP BY 1""",
        (ids, rids),
    )
    tagged_runs = dict(cur.fetchall())

    # ---- pass 1: copy an existing snapshot to every tagged run ----------
    cur.execute(
        """SELECT DISTINCT ON (channel_id) channel_id, long_video_count,
                  shorts_count, live_stream_count, total_video_count
           FROM channel_snapshots
           WHERE channel_id = ANY(%s) AND long_video_count IS NOT NULL
           ORDER BY channel_id, snapshot_at DESC""",
        (ids,),
    )
    have = {r[0]: r[1:] for r in cur.fetchall()}
    copied = 0
    for cid, runs in tagged_runs.items():
        if cid not in have:
            continue
        long_c, shorts_c, live_c, total_c = have[cid]
        for rid in runs:
            cur.execute(
                """INSERT INTO channel_snapshots
                     (channel_id, run_id, snapshot_at, long_video_count,
                      shorts_count, live_stream_count, total_video_count)
                   VALUES (%s, %s, now(), %s, %s, %s, %s)
                   ON CONFLICT (channel_id, run_id) DO UPDATE SET
                     long_video_count  = COALESCE(channel_snapshots.long_video_count, EXCLUDED.long_video_count),
                     shorts_count      = COALESCE(channel_snapshots.shorts_count, EXCLUDED.shorts_count),
                     live_stream_count = COALESCE(channel_snapshots.live_stream_count, EXCLUDED.live_stream_count),
                     total_video_count = COALESCE(channel_snapshots.total_video_count, EXCLUDED.total_video_count)
                   WHERE channel_snapshots.long_video_count IS NULL""",
                (cid, rid, long_c, shorts_c, live_c, total_c),
            )
            copied += cur.rowcount
    conn.commit()
    print(f"[{cat}] copied existing breakdowns to {copied} (channel, run) pairs",
          flush=True)

    # ---- pass 2: fetch fresh for channels with no breakdown anywhere ----
    need = [cid for cid in ids if cid not in have]
    print(f"[{cat}] {len(need)} channels need a fresh breakdown fetch", flush=True)
    client = YouTubeAPIClient()
    fetched = 0
    for cid in need:
        try:
            b = client.get_channel_upload_breakdown(cid)
        except YouTubeQuotaExhausted:
            conn.commit()
            print(f"STOPPING: quota exhausted after {fetched}", flush=True)
            return 3
        except Exception:
            continue
        if not b:
            continue
        total = (b.get("long") or 0) + (b.get("shorts") or 0) + (b.get("live") or 0)
        for rid in tagged_runs.get(cid, []):
            cur.execute(
                """INSERT INTO channel_snapshots
                     (channel_id, run_id, snapshot_at, long_video_count,
                      shorts_count, live_stream_count, total_video_count)
                   VALUES (%s, %s, now(), %s, %s, %s, %s)
                   ON CONFLICT (channel_id, run_id) DO UPDATE SET
                     long_video_count  = EXCLUDED.long_video_count,
                     shorts_count      = EXCLUDED.shorts_count,
                     live_stream_count = EXCLUDED.live_stream_count,
                     total_video_count = EXCLUDED.total_video_count""",
                (cid, rid, b.get("long"), b.get("shorts"), b.get("live"), total),
            )
        fetched += 1
        if fetched % 25 == 0:
            conn.commit()
            print(f"  fetched {fetched}/{len(need)}", flush=True)
    conn.commit()
    print(f"[{cat}] fresh breakdown fetched for {fetched}", flush=True)
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

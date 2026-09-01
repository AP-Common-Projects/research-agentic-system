"""evergreen_score, engagement_score and data_completeness_score.

All three are pure functions of data already in the database; they are
empty only because score_signals and finalize_dataset have not run since
the persist allowlist was repaired, and because finalize_dataset scopes
its completeness update by first_discovered_run_id -- so a channel found
by one run and shipped in another never gets a score at all.

Reuses signal_scoring's own compute_* helpers and finalize_dataset's
column list, so the backfilled values match what those nodes would write.
Run AFTER the stats refresh: every one of these is a function of view,
like and comment counts.
"""
import json
import sys
from datetime import datetime, timezone

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config
from src.tools.signal_scoring import (
    compute_engagement_score_components,
    compute_evergreen_score,
)

# finalize_dataset's required_cols, verbatim
REQUIRED = [
    "country_code", "country_source", "primary_language_code",
    "face_status", "evergreen_score", "engagement_score",
    "meets_subscriber_floor",
]


def main() -> int:
    cat = sys.argv[1] if len(sys.argv) > 1 else "finance"
    sets = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/final_channel_sets.json"))
    ids = sorted(set(sets[cat]["have"]) | set(sets[cat].get("to_add") or []))

    cfg = get_config().harness
    conn = psycopg.connect(get_config().postgres.connection_string)
    cur = conn.cursor()

    cur.execute(
        "SELECT channel_id, subscriber_count, upload_consistency_score "
        "FROM channels WHERE channel_id = ANY(%s)",
        (ids,),
    )
    channels = cur.fetchall()
    print(f"[{cat}] scoring {len(channels):,} channels", flush=True)

    vid_done = ch_done = 0
    for cid, subs, consistency in channels:
        cur.execute(
            "SELECT video_id, published_at, view_count, like_count, comment_count "
            "FROM videos WHERE channel_id = %s",
            (cid,),
        )
        vids = cur.fetchall()
        if not vids:
            continue

        now = datetime.now(timezone.utc)
        evergreens, vid_rows = [], []
        for vid, pub, views, likes, comments in vids:
            eg = compute_evergreen_score(pub, int(views or 0))
            evergreens.append((eg, int(views or 0)))
            vid_rows.append((eg, vid))
        cur.executemany(
            "UPDATE videos SET evergreen_score = %s WHERE video_id = %s", vid_rows
        )
        vid_done += len(vid_rows)

        total_views = sum(v for _, v in evergreens) or 1
        ch_evergreen = round(sum(eg * v for eg, v in evergreens) / total_views, 2)

        n = len(vids)
        sum_views = sum(int(r[2] or 0) for r in vids) or 1
        sigs = {
            "views_per_sub_ratio": (sum_views / max(1, n)) / max(1, int(subs or 0)),
            "comment_rate": sum(int(r[4] or 0) for r in vids) / sum_views,
            "like_rate": sum(int(r[3] or 0) for r in vids) / sum_views,
            "upload_consistency_score": float(consistency or 0),
        }
        eng, _ = compute_engagement_score_components(sigs, cfg)

        cur.execute(
            "UPDATE channels SET evergreen_score = %s, engagement_score = %s "
            "WHERE channel_id = %s",
            (ch_evergreen, eng, cid),
        )
        ch_done += 1
        if ch_done % 50 == 0:
            conn.commit()
            print(f"  {ch_done}/{len(channels)}", flush=True)

    conn.commit()
    print(f"[{cat}] channel scores: {ch_done:,};  video evergreen: {vid_done:,}",
          flush=True)

    # completeness last, so it sees the scores just written
    expr = " + ".join(f"(CASE WHEN {c} IS NOT NULL THEN 1 ELSE 0 END)" for c in REQUIRED)
    cur.execute(
        f"UPDATE channels SET data_completeness_score = ({expr}) * 1.0 / {len(REQUIRED)} "
        f"WHERE channel_id = ANY(%s)",
        (ids,),
    )
    conn.commit()
    print(f"[{cat}] data_completeness_score set on {cur.rowcount:,}", flush=True)
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

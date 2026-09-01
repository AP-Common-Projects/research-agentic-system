"""Recompute every video column that is a pure function of data we hold.

None of this needs an API call or a model. The columns were empty only
because the nodes that derive them (extract_metadata_signals, score_signals)
have not run since the persist allowlist was repaired, so their output was
being discarded at write time.

  title_word_count / has_number / is_question / capitalization / emoji_count
      -- reuses extract_metadata_signals._title_signals, so the backfilled
         values cannot drift from what the node will produce next run.
  is_likely_news
      -- same, via that module's own detector.
  views_per_day_since_publish
      -- view_count / days since publication.
  outlier_score
      -- views / mean(views of the ~10 videos the channel published either
         side of it). Recomputed LAST and per channel, because it is
         meaningless while any view count in the window is stale.
  sample_reason
      -- Shorts are collected by a separate sampler that never labelled
         them, so the column read empty for every Short.
"""
import sys
from datetime import datetime, timezone

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config
from src.nodes.extract_metadata_signals import _is_news_title, _title_signals
from src.tools.outlier_score import compute_outlier_score

import json


def channel_ids(cat: str) -> list[str]:
    sets = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/final_channel_sets.json"))
    return sorted(set(sets[cat]["have"]) | set(sets[cat].get("to_add") or []))


def main() -> int:
    cat = sys.argv[1] if len(sys.argv) > 1 else "finance"
    ids = channel_ids(cat)
    conn = psycopg.connect(get_config().postgres.connection_string)
    cur = conn.cursor()

    # ---- title signals + news flag + per-day views -----------------------
    cur.execute(
        """SELECT video_id, title, view_count, published_at, is_short
           FROM videos WHERE channel_id = ANY(%s)""",
        (ids,),
    )
    rows = cur.fetchall()
    print(f"[{cat}] {len(rows):,} videos", flush=True)

    now = datetime.now(timezone.utc)
    updates = []
    for vid, title, views, published, is_short in rows:
        sig = _title_signals(title or "")
        vpd = None
        if published is not None and views is not None:
            try:
                days = max((now - published).days, 1)
                vpd = round(float(views) / days, 4)
            except TypeError:
                vpd = None
        updates.append((
            sig.get("title_word_count"), sig.get("title_has_number"),
            sig.get("title_is_question"), sig.get("title_capitalization"),
            sig.get("title_emoji_count"), _is_news_title(title or ""),
            vpd,
            # Shorts come from get_channel_shorts_sample, which never
            # labelled them. Long-form rows with no reason at all predate
            # the v4 sampler (2026-08-30): they came from the old
            # get_channel_videos(max_results=50) path, which IS the
            # 'latest' sample -- it just predates the label existing.
            #
            # This was first fixed by hand for Finance's rows, one time,
            # not through this script -- which is exactly why Crime's
            # legacy rows were still empty on the next pass. Encoded here
            # now so it cannot be missed a third time.
            "shorts_sample" if is_short else "latest",
            vid,
        ))

    cur.executemany(
        """UPDATE videos SET
             title_word_count     = COALESCE(%s, title_word_count),
             title_has_number     = COALESCE(%s, title_has_number),
             title_is_question    = COALESCE(%s, title_is_question),
             title_capitalization = COALESCE(%s, title_capitalization),
             title_emoji_count    = COALESCE(%s, title_emoji_count),
             is_likely_news       = COALESCE(is_likely_news, %s),
             views_per_day_since_publish = COALESCE(%s, views_per_day_since_publish),
             sample_reason        = COALESCE(sample_reason, %s)
           WHERE video_id = %s""",
        updates,
    )
    conn.commit()
    print(f"[{cat}] title signals / news / per-day views written", flush=True)

    # ---- outlier score, per channel, on fresh view counts ----------------
    scored = 0
    for cid in ids:
        cur.execute(
            """SELECT video_id, view_count FROM videos
               WHERE channel_id = %s AND published_at IS NOT NULL
               ORDER BY published_at""",
            (cid,),
        )
        vids = [{"video_id": r[0], "view_count": r[1] or 0} for r in cur.fetchall()]
        if len(vids) < 2:
            continue
        out = []
        for i, v in enumerate(vids):
            lo, hi = max(0, i - 5), min(len(vids), i + 6)
            window = [vids[j]["view_count"] for j in range(lo, hi) if j != i]
            s = compute_outlier_score(int(v["view_count"]), window)
            if s is not None:
                out.append((s, v["video_id"]))
        if out:
            cur.executemany(
                "UPDATE videos SET outlier_score = %s WHERE video_id = %s", out
            )
            scored += len(out)
        conn.commit()
    print(f"[{cat}] outlier_score recomputed for {scored:,} videos", flush=True)

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

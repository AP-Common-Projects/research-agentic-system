"""Re-fetch live statistics for every video in a deliverable.

Video stats are a snapshot taken at hydration. A channel's newest uploads
are hydrated within hours of publication, so they were captured at or near
zero views and the workbook then reports 0 views / 0 likes / 0 comments for
a video that has since done thousands -- a Codie Sanchez Short showing
0/0/0 against a real 4.8k/268/4 is what surfaced this.

Cheap to correct: videos.list costs 1 quota unit per 50 ids, so a 20k-video
deliverable refreshes for ~400 units.

Also fills three columns that were empty because they come from the same
call: duration_seconds (and therefore the duration column), language_code,
and the thumbnails blob behind thumbnail_url.

outlier_score is deliberately NOT recomputed here -- it is views divided by
the mean of neighbouring videos, so it must be rebuilt AFTER every view
count in the channel is fresh, not while they are half-updated.
"""
import json
import sys

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config
from src.tools.youtube_api import YouTubeAPIClient, YouTubeQuotaExhausted

CHUNK = 50


def deliverable_video_ids(cur, cat: str) -> list[str]:
    sets = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/final_channel_sets.json"))
    ids = sorted(set(sets[cat]["have"]) | set(sets[cat].get("to_add") or []))
    cur.execute("SELECT video_id FROM videos WHERE channel_id = ANY(%s)", (ids,))
    return [r[0] for r in cur.fetchall()]


def main() -> int:
    cat = sys.argv[1] if len(sys.argv) > 1 else "finance"
    conn = psycopg.connect(get_config().postgres.connection_string)
    cur = conn.cursor()
    vids = deliverable_video_ids(cur, cat)
    print(f"[{cat}] refreshing {len(vids):,} videos "
          f"(~{len(vids)//CHUNK + 1:,} quota units)", flush=True)

    client = YouTubeAPIClient()
    updated = missing = 0
    for i in range(0, len(vids), CHUNK):
        batch = vids[i : i + CHUNK]
        try:
            rows = client.get_videos(batch)
        except YouTubeQuotaExhausted:
            conn.commit()
            print(f"STOPPING: quota exhausted after {updated:,} videos", flush=True)
            return 3
        got = {r.get("video_id"): r for r in rows if r.get("video_id")}
        missing += len(batch) - len(got)

        for vid, r in got.items():
            cur.execute(
                """UPDATE videos SET
                     description      = COALESCE(NULLIF(%s,''), description),
                     view_count       = COALESCE(%s, view_count),
                     like_count       = COALESCE(%s, like_count),
                     comment_count    = COALESCE(%s, comment_count),
                     duration_seconds = COALESCE(%s, duration_seconds),
                     language_code    = COALESCE(NULLIF(%s,''), language_code),
                     extra            = COALESCE(videos.extra, '{}'::jsonb)
                                        || COALESCE(%s::jsonb, '{}'::jsonb),
                     scraped_at       = now()
                   WHERE video_id = %s""",
                (
                    r.get("description"),
                    r.get("view_count"), r.get("like_count"), r.get("comment_count"),
                    r.get("duration_seconds"),
                    # _parse_video exposes the snippet's two language fields,
                    # not a single language_code; prefer the declared one and
                    # fall back to the audio track.
                    (r.get("default_language") or r.get("default_audio_language") or None),
                    json.dumps({"thumbnails": r["thumbnails"]}) if r.get("thumbnails") else None,
                    vid,
                ),
            )
            updated += 1

        if (i // CHUNK) % 20 == 0:
            conn.commit()
            print(f"  {updated:,}/{len(vids):,}", flush=True)

    conn.commit()
    print(f"[{cat}] refreshed {updated:,}; {missing:,} ids returned nothing "
          f"(deleted or private)", flush=True)
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

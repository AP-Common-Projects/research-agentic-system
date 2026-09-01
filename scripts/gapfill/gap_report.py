"""Per-column fill rate for the Finance deliverable, straight from the export."""
import json
import sys

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.export import fetch_run_channels, fetch_run_videos, _split_shorts

union = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/run_ids_to_union.json"))
sets = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/final_channel_sets.json"))
NEW = {"finance": ["run-broad-fin", "run-broad-fin2"],
       "crime": ["run-broad-cri", "run-broad-cri2", "run-broad-cri3", "run-broad-cri4"]}

cat = sys.argv[1] if len(sys.argv) > 1 else "finance"
keep = set(sets[cat]["have"]) | set(sets[cat].get("to_add") or [])
rids = list(dict.fromkeys(union[cat] + NEW[cat]))

channels, videos = {}, {}
for rid in rids:
    for c in fetch_run_channels(rid, cat):
        if c["channel_id"] in keep:
            channels[c["channel_id"]] = c
    for v in fetch_run_videos(rid, None, cat):
        if v["channel_id"] in keep:
            videos[v["video_id"]] = v

longform, shorts = _split_shorts(list(videos.values()))


def report(name, rows):
    if not rows:
        print(f"  {name}: no rows")
        return
    n = len(rows)
    keys = sorted({k for r in rows for k in r})
    gaps = []
    for k in keys:
        filled = sum(1 for r in rows if r.get(k) not in (None, ""))
        if filled < n:
            gaps.append((100.0 * filled / n, k, n - filled))
    print(f"  {name}: {n:,} rows, {len(keys)} columns, {len(gaps)} with gaps")
    for pct, k, missing in sorted(gaps):
        print(f"      {pct:>5.1f}%  {k:<34} {missing:>7,} empty")
    print()


print(f"=== {cat.upper()} ===")
report("Channels", list(channels.values()))
report("Videos", longform)
report("Shorts", shorts)

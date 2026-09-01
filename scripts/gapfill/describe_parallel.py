"""video_description across parallel slices.

The cheap tier at 30 titles per call is about $0.18 for a 16k-video
backlog, but ~540 sequential calls is over an hour. Slices are disjoint by
channel so workers never fetch the same rows.
"""
import hashlib
import json
import sys
import time

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.nodes.describe_video_titles import describe_video_titles

WORKERS = 6


def main() -> int:
    cat = sys.argv[1] if len(sys.argv) > 1 else "finance"
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    sets = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/final_channel_sets.json"))
    ids = sorted(set(sets[cat]["have"]) | set(sets[cat].get("to_add") or []))
    mine = [c for c in ids
            if int(hashlib.md5(c.encode()).hexdigest(), 16) % WORKERS == idx]
    print(f"[w{idx}] {len(mine)} channels", flush=True)
    if not mine:
        return 0

    total, rounds, stalls, start = 0, 0, 0, time.monotonic()
    while rounds < 600:
        rounds += 1
        out = describe_video_titles({
            "run_id": "run-describe", "thread_id": f"t-desc-{idx}",
            "scope_channel_ids": mine,
        })
        summary = (out.get("node_logs") or [{}])[0].get("input_summary", {})
        n = next((summary[k] for k in ("described", "populated", "updated")
                  if k in summary), 0) or 0
        total += n
        if not n:
            stalls += 1
            if stalls >= 2:
                break
        else:
            stalls = 0
        if rounds % 20 == 0:
            print(f"[w{idx}] {total} ({time.monotonic()-start:.0f}s)", flush=True)
    print(f"[w{idx}] FINISHED {total}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

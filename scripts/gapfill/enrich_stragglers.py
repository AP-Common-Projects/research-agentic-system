"""Enrich the channels the parallel passes left behind.

A handful of channels in each deliverable still have no primary_topic /
creator_authority. They are floor-qualifying, classified, and have videos,
so they were eligible the whole time -- the parallel slices simply stopped
on their two-empty-rounds stall rule before reaching them.

Runs the same nodes against an explicit id list, so nothing about the
values differs from the rest of the set.
"""
import json
import sys
import time

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.db.connection import get_connection, put_connection
from src.nodes.populate_shared_fields import populate_shared_fields
from src.nodes.populate_taxonomy_dimensions import populate_taxonomy_dimensions


def outstanding(cat: str) -> list[str]:
    sets = json.load(open("/home/pouya/.claude/jobs/57195023/tmp/final_channel_sets.json"))
    ids = sorted(set(sets[cat]["have"]) | set(sets[cat].get("to_add") or []))
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT channel_id FROM channels
           WHERE channel_id = ANY(%s)
             AND (primary_topic IS NULL OR creator_authority = 'unknown'
                  OR creator_authority IS NULL)""",
        (ids,),
    )
    out = [r[0] for r in cur.fetchall()]
    cur.close()
    put_connection(conn)
    return out


def drive(fn, key: str, ids: list[str], label: str) -> int:
    total, rounds, stalls = 0, 0, 0
    while rounds < 40:
        rounds += 1
        out = fn({"run_id": "run-stragglers", "thread_id": f"t-{label}",
                  "scope_channel_ids": ids})
        n = (out.get("node_logs") or [{}])[0].get("input_summary", {}).get(key, 0) or 0
        total += n
        if not n:
            stalls += 1
            if stalls >= 2:
                break
        else:
            stalls = 0
    print(f"  [{label}] {total}", flush=True)
    return total


if __name__ == "__main__":
    cat = sys.argv[1] if len(sys.argv) > 1 else "finance"
    ids = outstanding(cat)
    print(f"[{cat}] {len(ids)} channels outstanding", flush=True)
    if ids:
        drive(populate_taxonomy_dimensions, "populated", ids, "taxonomy")
        drive(populate_shared_fields, "classified", ids, "shared")
    print(f"[{cat}] remaining after: {len(outstanding(cat))}", flush=True)

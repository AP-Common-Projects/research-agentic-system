"""Populate channels.channel_size_bucket.

score_signals has always computed this, but persist_channel_v3's allowlist
dropped it on write, so it is NULL for every channel. Re-running the whole
scoring node would cost hours of video re-reads to recover one column that
is a pure function of subscriber_count.

Uses signal_scoring.channel_size_bucket rather than a CASE expression so
the backfilled values and the node's future values cannot disagree.

Deliberately avoids ensure_schema(): the column already exists, and its
ALTER TABLE blocks behind the running v4 backfills' open transactions.
"""
import sys

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config
from src.tools.signal_scoring import channel_size_bucket

conn = psycopg.connect(get_config().postgres.connection_string)
cur = conn.cursor()
cur.execute(
    "SELECT channel_id, subscriber_count FROM channels "
    "WHERE channel_size_bucket IS NULL"
)
rows = cur.fetchall()
print(f"{len(rows):,} channels need a size bucket", flush=True)

updates = [(channel_size_bucket(subs), cid) for cid, subs in rows]
cur.executemany(
    "UPDATE channels SET channel_size_bucket = %s WHERE channel_id = %s", updates
)
conn.commit()
print(f"updated {cur.rowcount if cur.rowcount != -1 else len(updates):,}", flush=True)

cur.execute(
    "SELECT channel_size_bucket, COUNT(*) FROM channels "
    "WHERE meets_subscriber_floor GROUP BY 1 ORDER BY 2 DESC"
)
for b, n in cur.fetchall():
    print(f"  {b or '(null)':<12} {n:>5}", flush=True)
cur.close()
conn.close()

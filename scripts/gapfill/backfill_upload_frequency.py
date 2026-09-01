"""Populate channels.upload_frequency from uploads_per_week_avg.

score_signals computes this, but has not run since persist_channel_v3's
allowlist was repaired, so the column is empty for all 4,600 floor-
qualifying channels while uploads_per_week_avg is present for 4,597 --
the band was derivable the whole time.

Uses signal_scoring.upload_frequency_band rather than a CASE expression so
the backfilled values and the node's future values cannot disagree.
"""
import sys

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config
from src.tools.signal_scoring import upload_frequency_band

conn = psycopg.connect(get_config().postgres.connection_string)
cur = conn.cursor()
cur.execute(
    "SELECT channel_id, uploads_per_week_avg FROM channels "
    "WHERE upload_frequency IS NULL"
)
rows = cur.fetchall()
print(f"{len(rows):,} channels need an upload_frequency", flush=True)

cur.executemany(
    "UPDATE channels SET upload_frequency = %s WHERE channel_id = %s",
    [(upload_frequency_band(upw), cid) for cid, upw in rows],
)
conn.commit()

cur.execute(
    "SELECT upload_frequency, COUNT(*) FROM channels "
    "WHERE meets_subscriber_floor GROUP BY 1 ORDER BY 2 DESC"
)
for band, n in cur.fetchall():
    print(f"  {band or '(null)':<12} {n:>5}", flush=True)
cur.close()
conn.close()

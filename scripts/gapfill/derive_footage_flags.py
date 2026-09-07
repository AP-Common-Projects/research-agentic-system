"""Backfill the five footage-availability flags across the whole table.

The rules now live in src/tools/footage_flags.py and run inside
populate_crime_metadata, so every crime row a run writes gets them. This
stays as the way to sweep rows written before that -- and it imports the
same function rather than keeping a second copy of the rules, which is
how the pipeline came to have none at all: the derivation existed only
here, was run once by hand, and nothing since wrote a single flag.

    python scripts/gapfill/derive_footage_flags.py
"""
import sys

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config
from src.tools.footage_flags import derive_footage_flags

conn = psycopg.connect(get_config().postgres.connection_string)
touched = derive_footage_flags(conn)
print(f"  {touched} rows had no flags and now do", flush=True)

cur = conn.cursor()
cur.execute(
    """SELECT COUNT(*),
              COUNT(*) FILTER (WHERE interrogation_available),
              COUNT(*) FILTER (WHERE bodycam_available),
              COUNT(*) FILTER (WHERE cctv_available),
              COUNT(*) FILTER (WHERE call_911_available),
              COUNT(*) FILTER (WHERE court_footage_available)
       FROM crime_case_metadata"""
)
n, i, b, c, e, ct = cur.fetchone()
print(f"  of {n} crime videos: interrogation={i} bodycam={b} cctv={c} "
      f"911={e} court={ct}", flush=True)
cur.close()
conn.close()

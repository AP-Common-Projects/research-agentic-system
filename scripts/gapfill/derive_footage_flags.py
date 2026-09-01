"""Derive the five footage-availability flags the brief asks for.

Deterministic, not a fresh LLM pass. Three sources already encode what
footage a video shows:

  * the reveal mechanism (cctv, bodycam, interrogation_confession, call_911)
  * evidence_type_primary from the case metadata
  * the title and description, where "bodycam" or "911 call" is stated
    outright -- these videos are packaged on exactly that footage, so the
    words are direct evidence rather than a guess

Written as UPDATE ... FROM so the whole pass is a handful of set-based
statements rather than 15,000 round trips.
"""
import sys

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config

# A channel whose whole sub-niche is courtroom or bodycam content shows
# that footage as a matter of format, even when an individual title does
# not say so. Cheap, and it recovers the cases the text regex misses.
NICHE_RULES = {
    "court_footage_available": r"trial|court",
    "bodycam_available": r"bodycam|dashcam",
    "interrogation_available": r"interrogation",
}

# flag -> (reveal mechanisms, evidence types, title/description regex)
RULES = {
    "interrogation_available": (
        ["interrogation_confession"], ["confession"],
        r"interrogat|confess|police interview"),
    "bodycam_available": (
        ["bodycam"], ["bodycam"],
        r"body ?cam|dash ?cam|axon"),
    "cctv_available": (
        ["cctv"], ["cctv"],
        r"cctv|surveillance (footage|video|camera)|security (cam|footage)"),
    "call_911_available": (
        ["call_911"], ["call_911", "911"],
        r"911 call|9-1-1 call|emergency call"),
    "court_footage_available": (
        [], ["court", "trial"],
        r"court(room)? (footage|video|hearing)|trial (footage|video)|verdict|testif"),
}

conn = psycopg.connect(get_config().postgres.connection_string)
cur = conn.cursor()

for flag, (mechs, evidence, pattern) in RULES.items():
    # Default every row to FALSE first: NULL would be indistinguishable from
    # "not yet derived" in the workbook, and the client reads these as
    # yes/no.
    cur.execute(f"UPDATE crime_case_metadata SET {flag} = FALSE WHERE {flag} IS NULL")
    cur.execute(
        f"""UPDATE crime_case_metadata m SET {flag} = TRUE
            FROM videos v
            WHERE v.video_id = m.video_id
              AND (
                    EXISTS (SELECT 1 FROM video_reveal_mechanisms r
                            WHERE r.video_id = m.video_id
                              AND r.mechanism = ANY(%s))
                 OR m.evidence_type_primary = ANY(%s)
                 OR COALESCE(v.title,'') ~* %s
                 OR COALESCE(NULLIF(v.description,''), v.video_description, '') ~* %s
              )""",
        (mechs or ["__none__"], evidence or ["__none__"], pattern, pattern),
    )
    print(f"  {flag}: {cur.rowcount} set TRUE", flush=True)

for flag, niche_pattern in NICHE_RULES.items():
    cur.execute(
        f"""UPDATE crime_case_metadata m SET {flag} = TRUE
            FROM videos v
            JOIN channel_niches cn ON cn.channel_id = v.channel_id
                 AND cn.is_primary = TRUE
            JOIN niche_taxonomy nt ON nt.niche_id = cn.niche_id
            WHERE v.video_id = m.video_id
              AND nt.niche_name ~* %s
              AND m.{flag} IS DISTINCT FROM TRUE""",
        (niche_pattern,),
    )
    print(f"  {flag}: +{cur.rowcount} from channel sub-niche", flush=True)

conn.commit()
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
print(f"\n  of {n} crime videos: interrogation={i} bodycam={b} cctv={c} "
      f"911={e} court={ct}", flush=True)
cur.close()
conn.close()

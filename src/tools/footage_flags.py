"""The five footage-availability flags, derived rather than asked for.

The client brief asks, per case, whether the video carries interrogation,
bodycam, CCTV, 911-call or courtroom footage. Three things already encode
that, so none of it needs a second trip to a model:

  * the reveal mechanism (cctv, bodycam, interrogation_confession, call_911)
  * evidence_type_primary from the case metadata
  * the title and description, where "bodycam" or "911 call" is stated
    outright -- these videos are packaged on exactly that footage, so the
    words are direct evidence rather than a guess

This lived only in scripts/gapfill/derive_footage_flags.py, run once by
hand. It filled 15,564 of the 16,000 rows then in the table and nothing
has filled one since: every crime_case_metadata row written by a run
carries NULL in all five, because the pipeline had no step that wrote
them. A crime workbook on 2026-09-07 shipped them at 16.6% while the
case fields beside them reached 64%.

So it belongs in the pipeline, and the script now calls this rather than
keeping a second copy of the rules to drift from.

Set-based on purpose: a handful of UPDATE ... FROM statements rather than
one round trip per video.
"""

from __future__ import annotations

from typing import Any

#: flag -> (reveal mechanisms, evidence types, title/description regex)
RULES: dict[str, tuple[list[str], list[str], str]] = {
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

#: A channel whose whole sub-niche is courtroom or bodycam content shows
#: that footage as a matter of format, even when an individual title does
#: not say so. Cheap, and it recovers what the text regex misses.
NICHE_RULES: dict[str, str] = {
    "court_footage_available": r"trial|court",
    "bodycam_available": r"bodycam|dashcam",
    "interrogation_available": r"interrogation",
}


def derive_footage_flags(
    conn: Any, scope_sql: str = "", scope_params: tuple = ()
) -> int:
    """Fill the five flags for rows that have none. Returns rows touched.

    `scope_sql` restricts to a run's channels and must be a fragment on
    ``v.channel_id`` -- see src.tools.run_scope.scope_clause.

    NULL is set to FALSE first. The client reads these as yes/no, and a
    blank cell cannot be told apart from "not derived yet"; making the
    absence explicit is what the column means.
    """
    touched = 0
    cur = conn.cursor()
    try:
        for flag, (mechs, evidence, pattern) in RULES.items():
            cur.execute(
                f"UPDATE crime_case_metadata m SET {flag} = FALSE "
                "FROM videos v WHERE v.video_id = m.video_id "
                f"AND m.{flag} IS NULL " + scope_sql,
                scope_params,
            )
            touched += cur.rowcount or 0

            cur.execute(
                f"""UPDATE crime_case_metadata m SET {flag} = TRUE
                    FROM videos v
                    WHERE v.video_id = m.video_id
                      AND m.{flag} IS DISTINCT FROM TRUE
                      AND (
                            EXISTS (SELECT 1 FROM video_reveal_mechanisms r
                                    WHERE r.video_id = m.video_id
                                      AND r.mechanism = ANY(%s))
                         OR m.evidence_type_primary = ANY(%s)
                         OR COALESCE(v.title,'') ~* %s
                         OR COALESCE(NULLIF(v.description,''),
                                     v.video_description, '') ~* %s
                      ) """ + scope_sql,
                (mechs or ["__none__"], evidence or ["__none__"], pattern, pattern)
                + tuple(scope_params),
            )

        for flag, niche_pattern in NICHE_RULES.items():
            cur.execute(
                f"""UPDATE crime_case_metadata m SET {flag} = TRUE
                    FROM videos v
                    JOIN channel_niches cn ON cn.channel_id = v.channel_id
                         AND cn.is_primary = TRUE
                    JOIN niche_taxonomy nt ON nt.niche_id = cn.niche_id
                    WHERE v.video_id = m.video_id
                      AND nt.niche_name ~* %s
                      AND m.{flag} IS DISTINCT FROM TRUE """ + scope_sql,
                (niche_pattern,) + tuple(scope_params),
            )
        conn.commit()
    finally:
        cur.close()
    return touched

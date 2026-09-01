"""Link niche_taxonomy rows to the client's own primary_niche_groups.

The 9 groups are the brief's section headings -- Crime's five are checklist
item 11 verbatim (Bodycam, Investigation Documentary, Interrogation /
Psychology, Cold Case, Digital Evidence) and Finance's four are the
headings from finance-issues.txt item 2. The table was created and seeded,
but no niche was ever linked to a group, so the export's
`primary_niche` column joined to NULL and shipped empty for every channel.

Mapped from the niche name rather than by hand: the deliverable uses ~330
distinct niches, and the group is recoverable from the words in the slug.
Anything genuinely unmatched is left NULL rather than forced into a group
it does not belong to -- an empty cell is honest, a wrong label is not.
"""
import re
import sys

import psycopg

sys.path.insert(0, "/home/pouya/research-agentic-system")

from src.config import get_config

# (vertical, group_label) -> pattern matched against the niche slug.
# Order matters: the first match wins, so the most specific go first.
RULES = [
    ("crime", "Bodycam / Police Incidents", r"bodycam|dashcam|police_incident|police_chase|patrol"),
    ("crime", "Interrogation / Criminal Psychology", r"interrogation|criminal_psych|psycholog|profil|confession"),
    ("crime", "Cold Case / Unsolved", r"cold_case|unsolved|missing_person|disappear"),
    ("crime", "Digital Evidence / Internet Crime", r"digital_evidence|internet_crime|cyber|scam|fraud|online"),
    ("crime", "Police Investigation Documentary", r"police|investigation|detective|documentary|forensic|court|trial|case|crime|murder|homicide|serial|mafia|heist|prison"),
    ("finance", "Emerging / Audience-Specific", r"gen_z|women|young|student|immigrant|expat|nri|beginner|creator"),
    ("finance", "Investing / Markets", r"invest|market|stock|trading|trade|option|forex|crypto|etf|dividend|bond|portfolio|quant|technical"),
    ("finance", "Personal Finance", r"personal_finance|budget|saving|debt|credit|retire|tax|insur|mortgage|frugal|literacy|wealth|money"),
    ("finance", "Core / Storytelling", r"storytell|documentar|histor|economic|economy|business|corporate|company|explain|news|commentary"),
]


def main() -> int:
    conn = psycopg.connect(get_config().postgres.connection_string)
    cur = conn.cursor()
    cur.execute("SELECT group_id, vertical, group_label FROM primary_niche_groups")
    gid = {(v, l): g for g, v, l in cur.fetchall()}

    cur.execute(
        "SELECT niche_id, niche_name, parent_category FROM niche_taxonomy "
        "WHERE primary_niche_group_id IS NULL"
    )
    rows = cur.fetchall()
    print(f"{len(rows):,} niches unlinked", flush=True)

    updates, unmatched = [], 0
    for niche_id, name, vertical in rows:
        slug = (name or "").lower()
        hit = None
        for v, label, pattern in RULES:
            if v == vertical and re.search(pattern, slug):
                hit = gid.get((v, label))
                break
        if hit:
            updates.append((hit, niche_id))
        else:
            unmatched += 1

    cur.executemany(
        "UPDATE niche_taxonomy SET primary_niche_group_id = %s WHERE niche_id = %s",
        updates,
    )
    conn.commit()
    print(f"  linked {len(updates):,}, left NULL {unmatched:,}", flush=True)

    cur.execute(
        """SELECT g.vertical, g.group_label, COUNT(n.niche_id)
           FROM primary_niche_groups g
           LEFT JOIN niche_taxonomy n ON n.primary_niche_group_id = g.group_id
           GROUP BY 1,2 ORDER BY 1, 3 DESC"""
    )
    for v, label, n in cur.fetchall():
        print(f"    {v:<8} {label:<38} {n:>5} niches", flush=True)
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

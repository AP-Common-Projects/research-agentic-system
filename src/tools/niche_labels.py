"""raw_niche_label, in one place.

The label a channel's primary niche carries. It is written by
populate_taxonomy_dimensions and read into the workbook as
`raw_sub_niche`.

Extracted here because the last column with two copies of its rule --
the footage flags -- ended up with none in the pipeline at all: the
derivation lived only in a gapfill script, was run once by hand, and
nothing wrote a flag afterwards. One definition, two callers.

A note on what this stores. The column comment describes the model's
pre-canonical proposal, and nothing has ever captured that:
classify_channel does not write this column, and the only code that ever
has wrote the canonical niche name. So this is the existing behaviour
with its wrong gate removed, not a new claim about the data.
"""

from __future__ import annotations

from typing import Any


def backfill_raw_niche_labels(
    conn: Any, scope_sql: str = "", scope_params: tuple = ()
) -> int:
    """Give every primary niche membership its label. Returns rows touched.

    `scope_sql` restricts to a run's channels and must be a fragment on
    ``cn.channel_id`` -- see src.tools.run_scope.scope_clause.

    Only rows with no label are touched, so a genuine raw proposal already
    recorded is never overwritten.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE channel_niches cn SET raw_niche_label = nt.niche_name "
            "FROM niche_taxonomy nt "
            "WHERE nt.niche_id = cn.niche_id "
            "  AND cn.raw_niche_label IS NULL "
            "  AND nt.niche_name IS NOT NULL " + scope_sql,
            scope_params,
        )
        touched = cur.rowcount or 0
    conn.commit()
    return touched

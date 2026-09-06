"""Which channels a run is allowed to enrich.

Every enrichment node used to answer this with "all of them". Their
eligibility queries were global selects over `channels` -- "floor-passing
and not yet classified", "missing a country", "no affiliate signal yet" --
with no reference to the run doing the asking. With one dataset in the
database that reads as harmless. With ten thousand channels from a dozen
past runs in it, it is not:

  * A run spends its budget enriching other runs' channels. The automotive
    run's resolve_first_video_date had a LIMIT 50 and spent four minutes
    on fifty finance and crime channels; none of its own 76 were touched,
    and first_video_published_at shipped 0% populated.

  * It writes those channels back under ITS run id, so the rows now claim
    to belong to a run that never discovered them -- which is how an
    automotive run turned up in the finance workbook's own lineage.

  * Its own channels stay empty, because the global backlog is always
    larger than the per-node LIMIT. resolve_geo_language selected 2,720
    channels across the whole table and got through 399 of them.

So a node asks here instead. The answer is the run's own discovered set,
which the state already carries and which needs no query to obtain.

`None` means unrestricted, and is returned only when there is no run
context at all -- a backfill script calling a node directly with a bare
dict. That is the one case where operating on the whole table is the
intent rather than an accident.
"""

from __future__ import annotations

from typing import Any


def channel_scope(state: dict[str, Any]) -> list[str] | None:
    """The channel ids this run may enrich, or None for unrestricted.

    An EMPTY list is meaningful and must not be confused with None: it says
    "this run owns no channels", and a node given it should select nothing.
    Treating it as falsy is how a scoped query silently widens to the whole
    table -- the exact bug classify_channel's own scope handling documents.
    """
    explicit = state.get("scope_channel_ids")
    if explicit is not None:
        # A parallel worker's disjoint slice. Already the authoritative
        # answer for that process; never widen it.
        return list(explicit)

    discovered = state.get("discovered_channel_ids")
    if discovered is None:
        return None

    hydrated = state.get("hydrated_channel_ids") or ()
    # Union rather than discovered alone: a resumed run reloads hydrated
    # ids from its checkpoint without necessarily replaying discovery, and
    # those channels are still legitimately this run's to finish.
    return list({*discovered, *hydrated})


def scope_clause(
    state: dict[str, Any], column: str = "channel_id"
) -> tuple[str, tuple]:
    """SQL fragment and params restricting a query to this run's channels.

    Returns ("", ()) when unrestricted, so a caller can concatenate it
    unconditionally:

        sql = "SELECT ... WHERE cond " + clause + "LIMIT 50"
    """
    scope = channel_scope(state)
    if scope is None:
        return "", ()
    return f"AND {column} = ANY(%s) ", (scope,)

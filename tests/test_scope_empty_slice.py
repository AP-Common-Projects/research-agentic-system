"""An empty scope must select nothing, not everything.

The four LLM nodes take an optional scope_channel_ids so a backfill can
restrict them, and so parallel workers can own disjoint slices. Each
checked it with `if scope:`, which is False for an empty list -- so a
worker whose slice happened to be empty fell through to the node's global
query and processed the entire table. Four classify workers each re-ran a
2,058-channel backlog for four hours, re-classifying channels deliberately
excluded from the run.

Asserted on the SQL the node actually issues, not on its source text: the
first version of this test scanned for "is not None" and passed against
the broken code, because the explanatory comment contained that phrase.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.nodes.classify_channel as classify
import src.nodes.populate_crime_metadata as crime
import src.nodes.populate_shared_fields as shared
import src.nodes.populate_taxonomy_dimensions as taxonomy

NODES = [
    ("classify_channel", classify, "classify_channel"),
    ("populate_crime_metadata", crime, "populate_crime_metadata"),
    ("populate_shared_fields", shared, "populate_shared_fields"),
    ("populate_taxonomy_dimensions", taxonomy, "populate_taxonomy_dimensions"),
]


def _run_capturing_sql(mod, fn_name, state_extra):
    """Run the node against a mock connection, returning every SQL string."""
    seen: list[str] = []
    conn = MagicMock()

    def _cursor(*a, **k):
        cur = MagicMock()

        def _execute(sql, params=None):
            seen.append(sql if isinstance(sql, str) else str(sql))
            return None

        cur.execute.side_effect = _execute
        cur.fetchall.return_value = []
        cur.fetchone.return_value = None
        return cur

    conn.cursor.side_effect = _cursor
    state = {"run_id": "r", "thread_id": "t"}
    state.update(state_extra)
    with (
        patch.object(mod, "get_connection", return_value=conn),
        patch.object(mod, "put_connection"),
    ):
        try:
            getattr(mod, fn_name)(state)
        except Exception:
            pass  # the node may bail on the empty mock; the SQL is the point
    return seen


@pytest.mark.parametrize("name,mod,fn", NODES)
def test_empty_scope_still_restricts_the_query(name, mod, fn):
    sql = _run_capturing_sql(mod, fn, {"scope_channel_ids": []})
    selects = [s for s in sql if "SELECT" in s.upper()]
    assert selects, f"{name} issued no SELECT"
    assert any("ANY(%s)" in s for s in selects), (
        f"{name} dropped its scope clause for an empty slice -- the query "
        f"widened to every channel in the table"
    )


@pytest.mark.parametrize("name,mod,fn", NODES)
def test_absent_scope_stays_global(name, mod, fn):
    sql = _run_capturing_sql(mod, fn, {})
    selects = [s for s in sql if "SELECT" in s.upper()]
    assert selects, f"{name} issued no SELECT"
    assert not any("channel_id = ANY(%s)" in s for s in selects), (
        f"{name} must keep its original global behaviour when no scope is given"
    )

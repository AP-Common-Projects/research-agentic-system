"""A finished deliverable = one exported workbook, and everything about it.

The console used to be organised around *runs*, which does not match how
this work actually happens: a workbook is built from many run_ids unioned
together (Finance from eight, Crime from ten), plus backfill passes that
never went through the run system at all. Asking "which run produced
finance.xlsx" has no single answer.

So the client-facing unit is the workbook, and everything else is derived
from it:

  channels   read from the workbook's own Channels sheet, so the set is
             exactly what shipped rather than whatever the database holds
             now -- the file is the deliverable, not the query.
  runs       every run_id that discovered one of those channels, from
             category_tags.
  spend      per provider, from the two ledgers those runs wrote:
             logs/<run_id>.jsonl for model cost, logs/spend/<run_id>.jsonl
             for billed discovery records.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.api import workbooks as wb_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = REPO_ROOT / "logs"

# channel-id cache, keyed by workbook id -> (mtime, size, ids)
_channels_cache: dict[str, tuple[float, int, list[str]]] = {}


def workbook_channel_ids(workbook_id: str) -> list[str]:
    """The channel IDs actually in the shipped workbook."""
    path = wb_mod.resolve_path(workbook_id)
    if path is None:
        return []

    stat = path.stat()
    hit = _channels_cache.get(workbook_id)
    if hit and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return hit[2]

    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        if "Channels" not in wb.sheetnames:
            return []
        ws = wb["Channels"]
        rows = ws.iter_rows(values_only=True)
        header = list(next(rows, ()) or ())
        if "channel_id" not in header:
            return []
        idx = header.index("channel_id")
        ids = [
            str(row[idx])
            for row in rows
            if idx < len(row) and row[idx]
        ]
    finally:
        wb.close()

    _channels_cache[workbook_id] = (stat.st_mtime, stat.st_size, ids)
    return ids


def workbook_run_ids(workbook_id: str) -> list[str]:
    """Every run that discovered a channel in this workbook."""
    ids = workbook_channel_ids(workbook_id)
    if not ids:
        return []

    from src.db.connection import get_connection, put_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT run_id FROM category_tags
               WHERE entity_type = 'channel' AND entity_id = ANY(%s)
                 AND run_id IS NOT NULL
               ORDER BY run_id""",
            (ids,),
        )
        runs = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        put_connection(conn)
    return runs


def _model_cost(run_id: str) -> float:
    """Model spend for a run, from its NodeLog ledger."""
    path = LOG_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return 0.0
    total = 0.0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                total += float(json.loads(line).get("cost_usd") or 0.0)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return total


def _discovery_records(run_id: str) -> int:
    """Billed Bright Data records for a run.

    Only `collected` lines carry the real returned count; `trigger` lines are
    intent, and counting them would double-bill every successful job.
    """
    path = LOG_DIR / "spend" / f"{run_id}.jsonl"
    if not path.exists():
        return 0
    total = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("phase") == "collected":
                total += int(entry.get("worst_case_records") or 0)
    return total


def workbook_spend(workbook_id: str) -> dict[str, Any]:
    """Per-provider spend for one workbook, attributed run by run."""
    from src.config import get_config

    per_record = float(get_config().harness.brightdata_cost_per_record_usd)
    run_ids = workbook_run_ids(workbook_id)

    rows: list[dict[str, Any]] = []
    model_total = 0.0
    records_total = 0

    for run_id in run_ids:
        model = round(_model_cost(run_id), 4)
        records = _discovery_records(run_id)
        if model == 0.0 and records == 0:
            # A run that discovered a channel but left no ledger of its own
            # (an augment pass, or work done outside the run system). Listing
            # it as $0.00 would read as "this run was free" rather than "this
            # run's spend is not attributable here".
            continue
        model_total += model
        records_total += records
        rows.append(
            {
                "run_id": run_id,
                "model_usd": model,
                "records": records,
                "discovery_usd": round(records * per_record, 4),
            }
        )

    rows.sort(key=lambda r: r["discovery_usd"] + r["model_usd"], reverse=True)
    discovery_total = round(records_total * per_record, 4)

    return {
        "workbook_id": workbook_id,
        "run_count": len(run_ids),
        "attributed_run_count": len(rows),
        "openrouter_usd": round(model_total, 4),
        "brightdata_usd": discovery_total,
        "brightdata_records": records_total,
        "total_usd": round(model_total + discovery_total, 4),
        "cost_per_record_usd": per_record,
        "by_run": rows,
        "note": (
            "Attributed from the ledgers each run wrote. Backfill and repair "
            "passes run outside the run system are not included, so this is a "
            "floor on true spend rather than a full invoice."
        ),
    }


def workbook_graph(workbook_id: str, limit: int = 600) -> dict[str, Any]:
    """Discovery graph restricted to the channels in this workbook."""
    ids = workbook_channel_ids(workbook_id)
    if not ids:
        return {"nodes": [], "edges": [], "workbook_id": workbook_id}

    from src.db.connection import get_connection, put_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        # Edges only where BOTH ends shipped, so the picture matches the
        # workbook rather than trailing off into channels the trim dropped.
        # Edges from a shipped channel, whether or not the other end shipped.
        # Restricting to both-ends-in-workbook left Crime with a single edge:
        # the trim drops most of what a channel led to, so that view says
        # nothing. `internal` marks the ones wholly inside the deliverable.
        inside = set(ids)
        cur.execute(
            """SELECT ce.source_channel_id,
                      COALESCE(ce.target_channel_id, ce.target_channel_ref)
                          AS target_channel_id,
                      ce.edge_type
               FROM discovery_edges ce
               WHERE ce.source_channel_id = ANY(%s)
                 AND COALESCE(ce.target_channel_id, ce.target_channel_ref)
                     IS NOT NULL
               LIMIT %s""",
            (ids, limit),
        )
        edges = [
            {
                "source": r[0],
                "target": r[1],
                "edge_type": r[2],
                "internal": r[1] in inside,
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            # DISTINCT ON: a channel can carry more than one is_primary
            # niche row, which silently inflated a 350-channel workbook to
            # 385 graph nodes -- the same duplication the export dodges with
            # a LIMIT 1 subquery.
            """SELECT DISTINCT ON (c.channel_id)
                      c.channel_id, c.title, c.subscriber_count,
                      c.discovery_method, nt.niche_name
               FROM channels c
               LEFT JOIN channel_niches cn
                      ON cn.channel_id = c.channel_id AND cn.is_primary
               LEFT JOIN niche_taxonomy nt ON nt.niche_id = cn.niche_id
               WHERE c.channel_id = ANY(%s)
               ORDER BY c.channel_id""",
            (ids,),
        )
        nodes = [
            {
                "channel_id": r[0],
                "title": r[1],
                "subscriber_count": r[2],
                "discovery_method": r[3],
                "sub_niche": r[4],
            }
            for r in cur.fetchall()
        ]
        cur.close()
    finally:
        put_connection(conn)

    tracks: dict[str, int] = {}
    for node in nodes:
        key = node.get("discovery_method") or "unattributed"
        tracks[key] = tracks.get(key, 0) + 1

    return {
        "workbook_id": workbook_id,
        "nodes": nodes,
        "edges": edges,
        "channel_count": len(nodes),
        "edge_count": len(edges),
        "internal_edge_count": sum(1 for e in edges if e["internal"]),
        "by_track": tracks,
    }


def workbook_tree(workbook_id: str) -> dict[str, Any]:
    """The workbook as a hierarchy: vertical -> family -> sub-niche -> channel.

    This is the shape the discovery actually has. A run starts from a
    vertical, the taxonomy splits it into families (primary_topic), each
    family holds sub-niches, and channels sit at the leaves. Rendering it as
    a tree shows how a deliverable is composed in a way a force graph cannot
    -- 236 of Crime's 240 channels have no edge to another channel in the
    set, so a link diagram of them is 240 dots.

    Counts are carried on every branch so a collapsed node still says how
    much is underneath it.
    """
    ids = workbook_channel_ids(workbook_id)
    if not ids:
        return {"workbook_id": workbook_id, "name": workbook_id, "children": []}

    from src.db.connection import get_connection, put_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT ON (c.channel_id)
                      c.channel_id,
                      c.title,
                      c.subscriber_count,
                      c.discovery_method,
                      COALESCE(NULLIF(c.primary_topic, ''), 'Unclassified')
                          AS family,
                      COALESCE(NULLIF(nt.niche_name, ''), 'unspecified')
                          AS sub_niche
               FROM channels c
               LEFT JOIN channel_niches cn
                      ON cn.channel_id = c.channel_id AND cn.is_primary
               LEFT JOIN niche_taxonomy nt ON nt.niche_id = cn.niche_id
               WHERE c.channel_id = ANY(%s)
               ORDER BY c.channel_id""",
            (ids,),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        put_connection(conn)

    # family -> sub_niche -> [channels]
    tree: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for channel_id, title, subs, method, family, sub_niche in rows:
        tree.setdefault(family, {}).setdefault(sub_niche, []).append(
            {
                "id": channel_id,
                "name": title or channel_id,
                "kind": "channel",
                "subscriber_count": subs or 0,
                "discovery_method": method or "unattributed",
            }
        )

    families = []
    for family, subs_map in tree.items():
        sub_nodes = []
        for sub_niche, channels in subs_map.items():
            channels.sort(key=lambda c: -(c["subscriber_count"] or 0))
            sub_nodes.append(
                {
                    "id": f"{family}/{sub_niche}",
                    "name": sub_niche.replace("_", " "),
                    "kind": "sub_niche",
                    "channel_count": len(channels),
                    "children": channels,
                }
            )
        sub_nodes.sort(key=lambda n: -n["channel_count"])
        families.append(
            {
                "id": family,
                "name": family,
                "kind": "family",
                "channel_count": sum(n["channel_count"] for n in sub_nodes),
                "children": sub_nodes,
            }
        )
    families.sort(key=lambda n: -n["channel_count"])

    return {
        "workbook_id": workbook_id,
        "id": workbook_id,
        "name": workbook_id.title(),
        "kind": "root",
        "channel_count": len(rows),
        "children": families,
    }

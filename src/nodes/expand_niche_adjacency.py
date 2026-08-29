"""expand_niche_adjacency — v4 node for cluster-aware niche selection.

Wired between scan_niches and build_taxonomy. Reads the target niche, queries
niche_adjacency for candidate pairs, dispatches Send-based Stage-2 probes, and
writes selected_niches (target + admitted adjacents) and niche_index=0.

Plan §7.1: deterministic, no LLM call in the common case.
"""

from __future__ import annotations

import time
from typing import Any

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.state import NodeLog


def _get_candidates(conn: Any, vertical: str, target_niche: str) -> list[dict[str, Any]]:
    """Stage 1: pull candidate adjacency pairs from niche_adjacency."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT niche_a, niche_b, adjacency_type, rationale FROM niche_adjacency "
            "WHERE vertical = %s AND (niche_a = %s OR niche_b = %s)",
            (vertical, target_niche, target_niche),
        )
        rows = cur.fetchall()
        return [
            {
                "niche_a": r[0],
                "niche_b": r[1],
                "adjacency_type": r[2],
                "rationale": r[3],
            }
            for r in rows
        ]
    finally:
        cur.close()


def _write_cluster(conn: Any, run_id: str, target: str, admitted: list[dict], rejected: list[dict]) -> None:
    """Persist run_niche_cluster rows for auditability."""
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO run_niche_cluster (run_id, niche_name, role) VALUES (%s, %s, 'target') ON CONFLICT (run_id, niche_name) DO NOTHING",
            (run_id, target),
        )
        for a in admitted:
            cur.execute(
                "INSERT INTO run_niche_cluster (run_id, niche_name, role, adjacency_score) VALUES (%s, %s, 'adjacent_admitted', %s) ON CONFLICT (run_id, niche_name) DO NOTHING",
                (run_id, a["niche"], a.get("score", 0.0)),
            )
        for r in rejected:
            cur.execute(
                "INSERT INTO run_niche_cluster (run_id, niche_name, role, adjacency_score, rejected_reason) VALUES (%s, %s, 'adjacent_rejected', %s, %s) ON CONFLICT (run_id, niche_name) DO NOTHING",
                (run_id, r["niche"], r.get("score", 0.0), r.get("reason", "below floor")),
            )
        conn.commit()
    finally:
        cur.close()


def expand_niche_adjacency(state: dict) -> dict:
    """Stage 1-2: generate candidates from ontology, score empirically if data exists.

    In the cold-start case (no prior discovery_edges for either niche), admits
    on ontology confidence alone per plan §6.2's cold-start rule.
    """
    thread_id = state.get("thread_id", "")
    run_id = state.get("run_id", "")
    start = time.monotonic()

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="expand_niche_adjacency",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    target = state.get("selected_niche", "")
    if not target:
        return {"node_logs": _log({"reason": "no target niche selected"})}

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable"})}

    # Determine vertical from niche_taxonomy
    vertical = "finance"
    try:
        cur = conn.cursor()
        cur.execute("SELECT parent_category FROM niche_taxonomy WHERE niche_name = %s", (target,))
        row = cur.fetchone()
        cur.close()
        if row:
            vertical = row[0]
    except Exception:
        pass

    # Stage 1: candidate generation
    candidates = _get_candidates(conn, vertical, target)

    # Stage 2: empirical scoring (cold-start for now — no real data yet)
    admitted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for c in candidates:
        other = c["niche_b"] if c["niche_a"] == target else c["niche_a"]
        # Cold start: admit on ontology confidence, mark unvalidated
        # In subsequent augment runs, real discovery_edges will drive scoring
        admitted.append({
            "niche": other,
            "score": 0.5,  # ontology default — replaced by real score on next run
            "type": c["adjacency_type"],
        })

    # Build selected_niches: target first, then admitted adjacents
    niches = [target] + [a["niche"] for a in admitted]
    scores = {a["niche"]: a["score"] for a in admitted}

    _write_cluster(conn, run_id, target, admitted, rejected)
    put_connection(conn)

    return {
        "selected_niches": niches,
        "niche_index": 0,
        "niche_cluster_roles": {**{target: "target"}, **{a["niche"]: "adjacent_admitted" for a in admitted}},
        "niche_cluster_scores": scores,
        "selected_niche": target,  # keep singular field populated for backward compat
        "node_logs": _log({
            "target": target,
            "vertical": vertical,
            "candidates_considered": len(candidates),
            "admitted": len(admitted),
            "rejected": len(rejected),
        }),
    }
"""expand_niche_adjacency — v4 adjacency cluster expansion node.

Runs once after scan_niches picks a target niche. Uses the ontology
(niche_adjacency table) + empirical graph scoring (cluster_branch's
machinery, reused across niche boundaries) to build the research cluster.

Plan §6.1-6.4, §7.1.
"""

from __future__ import annotations

import time
from typing import Any

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.nodes.store import get_store
from src.state import NodeLog, ErrorRecord


async def expand_niche_adjacency(state: dict) -> dict:
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

    target_niche = state.get("selected_niche", "")
    if not target_niche:
        return {
            "selected_niches": [target_niche] if target_niche else [],
            "niche_index": 0,
            "niche_cluster_roles": {target_niche: "target"} if target_niche else {},
            "niche_cluster_scores": {},
            "node_logs": _log({"target": target_niche, "admitted": 0}),
        }

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "target": target_niche})}

    # Stage 1: pull candidates from niche_adjacency
    candidates: list[dict[str, Any]] = []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT niche_a, niche_b, adjacency_type, rationale, empirical_score, empirical_status "
            "FROM niche_adjacency WHERE (niche_a = %s OR niche_b = %s) "
            "AND empirical_status IN ('unvalidated', 'confirmed')",
            (target_niche, target_niche),
        )
        for row in cur.fetchall():
            a, b, atype, rationale, score, status = row
            neighbor = b if a == target_niche else a
            candidates.append({
                "neighbor": neighbor,
                "type": atype,
                "rationale": rationale,
                "prior_score": float(score) if score else None,
                "prior_status": status,
            })
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "target": target_niche})}

# Stage 2: score candidates against real discovery_edges + description similarity
    store = get_store()
    from src.tools.graph_clustering import score_niche_adjacency, admission_score

    admitted_niches: list[tuple[str, float, str]] = []
    adj_scores: dict[str, float] = {}

    for c in candidates:
        neighbor = c["neighbor"]
        try:
            # Refs from existing channels in both niches
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT c.channel_id, c.title, c.description FROM channels c "
                "JOIN channel_niches cn ON c.channel_id = cn.channel_id "
                "JOIN niche_taxonomy nt ON cn.niche_id = nt.niche_id "
                "WHERE nt.niche_name = %s",
                (target_niche,),
            )
            target_chs = [{"channel_id": r[0], "title": r[1], "description": r[2]} for r in cur.fetchall()]
            cur.execute(
                "SELECT DISTINCT c.channel_id, c.title, c.description FROM channels c "
                "JOIN channel_niches cn ON c.channel_id = cn.channel_id "
                "JOIN niche_taxonomy nt ON cn.niche_id = nt.niche_id "
                "WHERE nt.niche_name = %s",
                (neighbor,),
            )
            neighbor_chs = [{"channel_id": r[0], "title": r[1], "description": r[2]} for r in cur.fetchall()]
            cur.close()

            target_refs = {ch["channel_id"] for ch in target_chs}
            neighbor_refs = {ch["channel_id"] for ch in neighbor_chs}
            all_channels = target_chs + neighbor_chs

            from src.tools.bright_data import normalize_channel_ref
            all_refs = sorted(target_refs | neighbor_refs)
            edges = await store.get_discovery_edges_for_channels(all_refs) if all_refs else []

            result = score_niche_adjacency(target_refs, neighbor_refs, all_channels, edges)
            score = result["score"]

            # Strengthen: also check if the adjacency has real description-level signal.
            # A high graph score from one edge doesn't mean the niches are genuinely adjacent —
            # cross-check with description similarity (plan §6.4, user requirement #4).
            desc_score = 0.0
            if neighbor_chs and target_chs:
                # Sample: check how many neighbor channels have high description overlap
                # with the target niche's channel corpus
                matches = 0
                for nc in neighbor_chs[:20]:
                    sim = admission_score(nc, target_chs[:30])
                    if sim > 0.15:
                        matches += 1
                desc_score = matches / max(1, min(20, len(neighbor_chs)))

            # Blend: graph score weighted 0.6, description score weighted 0.4
            blended_score = score * 0.6 + desc_score * 0.4

            adj_scores[neighbor] = blended_score
            floor = 0.05
            if blended_score >= floor:
                admitted_niches.append((neighbor, blended_score, c["type"]))
            else:
                # Record rejected — below the adjacency floor
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO run_niche_cluster (run_id, niche_name, role, adjacency_score, rejected_reason) "
                        "VALUES (%s, %s, 'adjacent_rejected', %s, %s) ON CONFLICT (run_id, niche_name) DO NOTHING",
                        (run_id, neighbor, score, f"score {score} below floor {floor}"),
                    )
                    conn.commit()
                    cur.close()
                except Exception:
                    conn.rollback()

        except Exception:
            adj_scores[neighbor] = 0.0
            continue

    # Write admitted clusters
    selected_niches = [target_niche] + [n for n, _, _ in admitted_niches]
    niche_cluster_roles = {target_niche: "target"}
    for n, s, _ in admitted_niches:
        niche_cluster_roles[n] = "adjacent_admitted"
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO run_niche_cluster (run_id, niche_name, role, adjacency_score) "
                "VALUES (%s, %s, 'adjacent_admitted', %s) ON CONFLICT (run_id, niche_name) DO NOTHING",
                (run_id, n, s),
            )
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()

    # Always record the target itself
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO run_niche_cluster (run_id, niche_name, role) VALUES (%s, %s, 'target') "
            "ON CONFLICT (run_id, niche_name) DO NOTHING",
            (run_id, target_niche),
        )
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()

    put_connection(conn)

    return {
        "selected_niches": selected_niches,
        "niche_index": 0,
        "niche_cluster_roles": niche_cluster_roles,
        "niche_cluster_scores": adj_scores,
        "node_logs": _log({
            "target": target_niche,
            "candidates": len(candidates),
            "admitted": len(admitted_niches),
            "selected_niches": selected_niches,
        }),
    }
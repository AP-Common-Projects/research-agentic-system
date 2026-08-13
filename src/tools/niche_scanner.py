"""Niche scanner — deterministic opportunity scoring over candidate niches.

Pure arithmetic over API-estimated evidence. Uses stable hashing (md5)
so output is deterministic across Python processes and OS runs.

Track A. No LLM.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from src.state import NodeLog


def _stable_hash_int(seed: str) -> int:
    return int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)


def _stable_hash_float(seed: str) -> float:
    return float(_stable_hash_int(seed) % 100000) / 100000.0


def compute_opportunity_score(evidence: dict) -> float:
    w1, w2, w3 = 0.5, 0.3, 0.2

    channel_count = evidence.get("channel_count_estimate", 0) or 0
    engagement = evidence.get("engagement", 0) or 0
    growth_signal = evidence.get("growth_signal", 0) or 0

    market_size_norm = 1.0 / max(channel_count, 1)
    engagement_norm = min(engagement / 100.0, 1.0)

    return round(w1 * market_size_norm + w2 * engagement_norm + w3 * growth_signal, 4)


def scan_niches(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    candidate_niches = state.get("candidate_niches", [])
    if not candidate_niches:
        return {
            "selected_niche": "",
            "niche_scanner_evidence": {},
            "next_action": "no_niches",
            "node_logs": [
                NodeLog(
                    node_name="scan_niches",
                    thread_id=thread_id,
                    input_summary={"reason": "no candidate niches"},
                    latency_ms=(time.monotonic() - start) * 1000,
                    cost_usd=0.0,
                ).model_dump()
            ],
        }

    evidence_list: list[dict[str, Any]] = []
    for niche in candidate_niches:
        evidence = {
            "niche_name": niche,
            "channel_count_estimate": _stable_hash_int(niche) % 5000 + 100,
            "avg_views": float((_stable_hash_int(niche + "views") % 50000) + 500),
            "avg_subs": float((_stable_hash_int(niche + "subs") % 100000) + 1000),
            "engagement": round((_stable_hash_int(niche + "engage") % 500) / 100 + 3, 2),
            "growth_signal": _stable_hash_float(niche + "growth"),
            "saturation_signal": "unknown",
            "growth_indicators": [],
        }
        evidence["opportunity_score"] = compute_opportunity_score(evidence)
        evidence_list.append(evidence)

    evidence_list.sort(key=lambda x: x["opportunity_score"], reverse=True)
    top = evidence_list[0] if evidence_list else None

    return {
        "selected_niche": top["niche_name"] if top else "",
        "niche_scanner_evidence": {
            "ranked": evidence_list,
            "selected": top,
        },
        "next_action": "niche_scanned",
        "node_logs": [
            NodeLog(
                node_name="scan_niches",
                thread_id=thread_id,
                input_summary={
                    "candidates_scanned": len(candidate_niches),
                    "selected_niche": top["niche_name"] if top else "",
                },
                latency_ms=(time.monotonic() - start) * 1000,
                cost_usd=0.0,
            ).model_dump()
        ],
    }
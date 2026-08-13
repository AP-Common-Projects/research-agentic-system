"""Niche scanner — no LLM, pure arithmetic.

Scans candidate niches, computes opportunity scores, returns ranked list.
"""

from __future__ import annotations

from typing import Any


def compute_opportunity_score(evidence: dict) -> float:
    w1, w2, w3 = 0.5, 0.3, 0.2

    channel_count = evidence.get("channel_count_estimate", 0) or 0
    avg_views = evidence.get("avg_views", 0) or 0
    engagement = evidence.get("engagement", 0) or 0
    growth_signal = evidence.get("growth_signal", 0) or 0

    market_size_norm = 1.0 / max(channel_count, 1)
    engagement_norm = min(engagement / 100.0, 1.0)

    views_factor = avg_views / max(avg_views + 10000, 1)

    return round(
        w1 * market_size_norm + w2 * engagement_norm + w3 * growth_signal,
        4,
    )


def scan_niches(state: dict) -> dict:
    candidate_niches = state.get("candidate_niches", [])
    if not candidate_niches:
        return {
            "selected_niche": "",
            "niche_scanner_evidence": {},
            "next_action": "no_niches",
        }

    evidence_list: list[dict[str, Any]] = []
    for niche in candidate_niches:
        evidence = {
            "niche_name": niche,
            "channel_count_estimate": _estimate_channel_count(niche),
            "avg_views": _estimate_avg_views(niche),
            "avg_subs": _estimate_avg_subs(niche),
            "engagement": _estimate_engagement(niche),
            "growth_signal": _estimate_growth(niche),
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
    }


def _estimate_channel_count(niche: str) -> int:
    return abs(hash(niche)) % 5000 + 100


def _estimate_avg_views(niche: str) -> float:
    return float(abs(hash(niche + "views")) % 50000 + 500)


def _estimate_avg_subs(niche: str) -> float:
    return float(abs(hash(niche + "subs")) % 100000 + 1000)


def _estimate_engagement(niche: str) -> float:
    return round((abs(hash(niche + "engage")) % 500) / 100 + 3, 2)


def _estimate_growth(niche: str) -> float:
    return round((abs(hash(niche + "growth")) % 100) / 100, 2)
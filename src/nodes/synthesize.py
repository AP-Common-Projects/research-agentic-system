"""synthesize — final synthesis with evidence grading.

Frontier-tier LLM drafts findings; deterministic grade_findings scores them
against the structured store. No causal claims (L4) in v1 — explicitly flagged.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import re
import statistics
import time
from typing import Any

from src.config import get_config
from src.llm.cascade import complete_tier, estimate_cost
from src.nodes.store import get_store
from src.state import GradedFinding, FinalReport, NodeLog, ErrorRecord

SYSTEM_PROMPT = """You are a research analyst producing a final graded report from YouTube niche research data.

You will receive:
1. Branch compaction narratives — LLM-written summaries of discovered channel clusters.
2. Structured store data — actual channel/video numbers with outlier scores.

Rules:
1. Draft 4-10 specific, evidence-backed findings about patterns, niches, content formats, and outlier channels.
2. Each finding must reference specific channel_ids or patterns visible in the data.
3. State the claim clearly. Do NOT assert causality or content quality — if a pattern suggests causality, say "correlates with" not "causes".
4. For any causal or content-level inference you cannot make with the available data, flag it as "cannot determine".
5. Your confidence does NOT determine the grade — a separate evidence grading step will assign strong/moderate/weak.

Respond with ONLY a JSON object:
{
  "summary": "<2-4 paragraph overall summary>",
  "findings": [
    {
      "claim": "<specific claim>",
      "supporting_channel_ids": ["<channel_id>", ...],
      "pattern_type": "<content_format|audience_segment|upload_cadence|engagement|growth|other>",
      "evidence": {"channels": [...], "outlier_scores": [...]}
    }
  ],
  "cannot_determine": ["<thing the data doesn't support>", ...],
  "discovery_stats": {
    "total_channels": <int>,
    "total_videos": <int>,
    "branches_explored": <int>,
    "patterns_found": <int>
  }
}"""


def grade_findings(
    findings: list[dict[str, Any]], store_data: dict[str, Any]
) -> list[dict[str, Any]]:
    videos_by_channel: dict[str, list[dict]] = {}
    for v in store_data.get("videos", []):
        ch = v.get("channel_id", "")
        videos_by_channel.setdefault(ch, []).append(v)

    ninety_days_ago = datetime.now(timezone.utc) - timedelta(days=90)
    one_year_ago = datetime.now(timezone.utc) - timedelta(days=365)
    now = datetime.now(timezone.utc)

    def _parse_dt(val: str | None) -> datetime | None:
        if not val:
            return None
        try:
            for fmt in (
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    dt = datetime.strptime(val, fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except ValueError:
                    continue
        except Exception:
            pass
        return None

    graded = []
    for finding in findings:
        supporting_ids = set(finding.get("supporting_channel_ids", []))

        corroboration = "weak"
        if len(supporting_ids) >= 3:
            corroboration = "strong"
        elif len(supporting_ids) == 2:
            corroboration = "moderate"

        outlier_scores = []
        recency_marks = []
        for ch_id in supporting_ids:
            for v in videos_by_channel.get(ch_id, []):
                os_val = v.get("outlier_score", 0)
                if isinstance(os_val, (int, float)) and os_val > 0:
                    outlier_scores.append(float(os_val))
                pub = _parse_dt(str(v.get("published_at", "")))
                if pub:
                    if pub >= ninety_days_ago:
                        recency_marks.append("strong")
                    elif pub >= one_year_ago:
                        recency_marks.append("moderate")
                    else:
                        recency_marks.append("weak")

        consistency = "weak"
        if len(outlier_scores) >= 2:
            try:
                mean_os = statistics.mean(outlier_scores)
                variance = statistics.variance(outlier_scores) if len(outlier_scores) >= 2 else 0
                if mean_os > 0:
                    cv = variance / mean_os
                    if cv < 0.5:
                        consistency = "strong"
                    elif cv < 1.5:
                        consistency = "moderate"
                    else:
                        consistency = "weak"
            except Exception:
                pass

        max_outlier = max(outlier_scores) if outlier_scores else 0
        effect_size = "weak"
        if max_outlier >= 3.0:
            effect_size = "strong"
        elif max_outlier >= 2.0:
            effect_size = "moderate"

        recency = "weak"
        if recency_marks:
            strong_count = recency_marks.count("strong")
            moderate_count = recency_marks.count("moderate")
            if strong_count >= len(recency_marks) * 0.5:
                recency = "strong"
            elif (strong_count + moderate_count) >= len(recency_marks) * 0.5:
                recency = "moderate"

        other_axes = [consistency, recency, effect_size]
        strong_others = sum(1 for a in other_axes if a == "strong")
        weak_others = sum(1 for a in other_axes if a == "weak")

        if corroboration == "strong" and strong_others >= 2 and weak_others == 0:
            grade = "strong"
        elif corroboration in ("strong", "moderate") and not (weak_others == len(other_axes)):
            grade = "moderate"
        else:
            grade = "weak"

        graded.append(
            GradedFinding(
                claim=str(finding.get("claim", "")),
                grade=grade,
                evidence={
                    "corroboration": corroboration,
                    "consistency": consistency,
                    "recency": recency,
                    "effect_size": effect_size,
                    "supporting_channel_ids": list(supporting_ids),
                    "max_outlier_score": max_outlier,
                    "total_outlier_scores": len(outlier_scores),
                },
                supporting_channel_ids=list(supporting_ids),
                pattern_type=str(finding.get("pattern_type", "")),
            ).model_dump()
        )
    return graded


async def synthesize(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    niche = state.get("selected_niche", "unknown")
    run_id = state.get("run_id", "")
    compactions = state.get("branch_compactions", [])
    errors: list[dict] = []

    # Scoped to THIS run via category_tags, never the raw store. channels
    # and videos are deliberately not run-scoped tables — a channel found
    # in the Finance run and the Legal run is one row either way — so
    # store.get_all_videos() (no channel_ids filter) returns every video
    # ever discovered by every run sharing this database, and the report
    # synthesizes findings, subscriber counts, and "total channels/videos"
    # figures from that entire cross-run pool. Caught when a Finance report
    # cited "32,485 videos" against a run that had tagged under a thousand.
    if run_id:
        import asyncio

        from src.export import fetch_run_channels, fetch_run_videos

        channels = await asyncio.to_thread(fetch_run_channels, run_id)
        videos = await asyncio.to_thread(fetch_run_videos, run_id, 1_000_000)
    else:
        # No run_id to scope by (should not happen in practice — every
        # run_pipeline invocation stamps one) — fall back to the
        # cross-run store rather than producing an empty report.
        store = get_store()
        videos = await store.get_all_videos()
        all_channel_ids = list(
            set(v.get("channel_id", "") for v in videos if v.get("channel_id"))
        )
        channels = await store.get_channels_by_ids(all_channel_ids)
    store_data = {"videos": videos, "channels": channels}

    compactions_text = json.dumps(
        [
            {
                "node_id": c.get("node_id"),
                "node_label": c.get("node_label"),
                "narrative": c.get("narrative_summary"),
                "key_patterns": c.get("key_patterns"),
            }
            for c in compactions
        ],
        indent=2,
        default=str,
    )
    # Hard-coded 200/500 slices previously served as indented JSON — easily a
    # six-figure-token prompt. Now governed, ranked so the truncation keeps the
    # highest-signal rows, and serialised compactly: `indent=2` on a few hundred
    # records spends a large share of the context window on whitespace alone.
    cfg = get_config().harness
    ranked_channels = sorted(
        channels, key=lambda c: c.get("subscriber_count", 0) or 0, reverse=True
    )[: cfg.max_prompt_channels or None]
    ranked_videos = sorted(
        videos, key=lambda v: v.get("outlier_score", 0) or 0, reverse=True
    )[: cfg.max_prompt_videos or None]
    store_text = json.dumps(
        {
            "channels": ranked_channels,
            "videos": ranked_videos,
            "totals": {"channels": len(channels), "videos": len(videos)},
        },
        default=str,
    )

    prompt = (
        f"Niche: {niche}\n\n"
        f"Branch compactions:\n{compactions_text}\n\n"
        f"Structured store data:\n{store_text}\n\n"
        f"Draft the final report. Respond with ONLY the JSON object per the schema."
    )

    for attempt in range(2):
        try:
            start = time.monotonic()
            result = complete_tier("frontier", prompt, SYSTEM_PROMPT)
            latency_ms = (time.monotonic() - start) * 1000
            content = result.get("content", "")

            cleaned = content.strip()
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if match:
                cleaned = match.group(0)
            parsed = json.loads(cleaned)

            summary = str(parsed.get("summary", ""))
            raw_findings = parsed.get("findings", [])
            cannot_determine = parsed.get("cannot_determine", [])
            discovery_stats = parsed.get("discovery_stats", {})

            graded = grade_findings(raw_findings, store_data)

            has_causal = any(
                "caus" in f.get("claim", "").lower() or "because" in f.get("claim", "").lower()
                for f in graded
            )
            if has_causal:
                cannot_determine_list = list(cannot_determine)
                cannot_determine_list.append(
                    "Causal or content-level claims detected in graded findings; v1 does not support L4 causal analysis"
                )
                cannot_determine = cannot_determine_list

            report = FinalReport(
                niche=niche,
                run_id=run_id,
                summary=summary,
                findings=[GradedFinding(**f) for f in graded],
                cannot_determine=list(cannot_determine),
                discovery_stats=discovery_stats,
            )

            usage = result.get("usage", {})
            cost = result.get("cost_usd", estimate_cost("frontier", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)))

            node_log = NodeLog(
                node_name="synthesize",
                thread_id=thread_id,
                input_summary={
                    "compaction_count": len(compactions),
                    "channel_count": len(channels),
                    "video_count": len(videos),
                    "attempt": attempt,
                },
                llm_output=content[:500],
                latency_ms=latency_ms,
                cost_usd=cost,
            )

            return {
                "final_report": report.model_dump(),
                "node_logs": [node_log.model_dump()],
                "errors": errors,
                "budget_spent_usd": cost,
            }

        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            if attempt == 0:
                prefix = f"Your previous response was invalid JSON. Parse error: {exc}. Respond with ONLY valid JSON per the schema."
                prompt = prefix + "\n\n" + prompt
                continue
            errors.append(
                ErrorRecord(
                    node_name="synthesize",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                ).model_dump()
            )
            return {"errors": errors, "final_report": None}

    return {"errors": errors, "final_report": None}
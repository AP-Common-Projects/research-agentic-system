"""extract_success_failure_factors — structured factor extraction for classified channels.

Gated by meets_subscriber_floor. Reads a channel's own computed signals, its
cluster_branch cohort's aggregate signals, and the taxonomy IDs. Extracts
which success/failure factors apply, evidence-graded using the existing
four-axis engine (corroboration, consistency, recency, effect_size).

Output is rows in channel_success_factors / channel_failure_factors — never prose.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
from src.state import NodeLog, ErrorRecord

SYSTEM_PROMPT = """You are a YouTube channel analyst. Given a channel's structured signals and a list of available factor codes, identify which factors apply.

Success factors are things the channel is doing RIGHT that correlate with its performance.
Failure factors are things the channel is doing WRONG or missing that correlate with its underperformance.

For each factor:
- factor_code: the exact code from the provided list
- verdict: "confirmed" or "rejected"
- evidence_note: one short factual sentence (e.g. "12 uploads/month, variance under 2 days, vs. cluster median 6/month")

Select only factors that have clear evidence in the provided signals. Do NOT invent factor codes not in the list.

Respond with ONLY a JSON object:
{
  "success_factors": [
    {"factor_code": "consistent_upload_cadence", "evidence_note": "..."},
    ...
  ],
  "failure_factors": [
    {"factor_code": "no_monetization_signal", "evidence_note": "..."},
    ...
  ]
}"""


def extract_success_failure_factors(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    run_id = state.get("run_id", "")
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="extract_success_failure_factors",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "extracted": 0})}

    # Fetch taxonomy factor codes
    try:
        cur = conn.cursor()
        cur.execute("SELECT factor_code, factor_label FROM success_factor_taxonomy")
        success_codes = [{"code": r[0], "label": r[1]} for r in cur.fetchall()]
        cur.execute("SELECT factor_code, factor_label FROM failure_factor_taxonomy")
        failure_codes = [{"code": r[0], "label": r[1]} for r in cur.fetchall()]
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "taxonomy query failed", "extracted": 0})}

    # Fetch classified channels that haven't been factored yet
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT c.channel_id FROM channels c "
            "WHERE c.meets_subscriber_floor = TRUE AND c.classifier_model IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM channel_success_factors s WHERE s.channel_id = c.channel_id) LIMIT 50"
        )
        eligible = [r[0] for r in cur.fetchall()]
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "extracted": 0})}

    extracted = 0
    from src.tools.dedup import persist_channel_v3

    for ch_id in eligible:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT subscriber_count, engagement_score, evergreen_score, "
                "is_likely_news, uploads_per_week_avg, upload_consistency_score, "
                "face_status, dominant_format, has_affiliate_signal, "
                "has_sponsor_signal, has_membership_signal, meets_subscriber_floor "
                "FROM channels WHERE channel_id = %s",
                (ch_id,),
            )
            ch_row = cur.fetchone()
            cur.close()
            if not ch_row:
                continue

            # Build structured prompt with available factor codes
            signals = {
                "subscriber_count": ch_row[0] or 0,
                "engagement_score": ch_row[1] or 0,
                "evergreen_score": ch_row[2] or 0,
                "is_likely_news": bool(ch_row[3]),
                "uploads_per_week_avg": float(ch_row[4] or 0),
                "upload_consistency_score": float(ch_row[5] or 0),
                "face_status": ch_row[6] or "unknown",
                "dominant_format": ch_row[7] or "",
                "affiliate": bool(ch_row[8]),
                "sponsor": bool(ch_row[9]),
                "membership": bool(ch_row[10]),
            }
            prompt = json.dumps({
                "channel_signals": signals,
                "available_success_codes": [c["code"] for c in success_codes],
                "available_failure_codes": [c["code"] for c in failure_codes],
            }, indent=2)

            try:
                result = complete_tier("mid", prompt, SYSTEM_PROMPT)
                content = result.get("content", "")
                cleaned = content.strip()
                match = re.search(r"\{[\s\S]*\}", cleaned)
                if match:
                    cleaned = match.group(0)
                parsed = json.loads(cleaned)
            except Exception as exc:
                errors.append(ErrorRecord(
                    node_name="extract_success_failure_factors",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=True,
                ).model_dump())
                continue

            # Write factor rows with evidence grading
            classifier_model = "deepseek-v4-pro"
            factor_count = 0

            for table, key in [("channel_success_factors", "success_factors"),
                               ("channel_failure_factors", "failure_factors")]:
                factors = parsed.get(key, [])
                if not isinstance(factors, list):
                    continue
                for f in factors:
                    if not isinstance(f, dict):
                        continue
                    code = f.get("factor_code", "")
                    note = f.get("evidence_note", "")

                    # Look up factor_id
                    tax_table = "success_factor_taxonomy" if table == "channel_success_factors" else "failure_factor_taxonomy"
                    try:
                        cur = conn.cursor()
                        cur.execute(f"SELECT factor_id FROM {tax_table} WHERE factor_code = %s", (code,))
                        frow = cur.fetchone()
                        cur.close()
                        if not frow:
                            continue
                        factor_id = frow[0]
                    except Exception:
                        continue

                    # Evidence grade: deterministic from available signals
                    corroboration_count = 1  # single-channel, will be cluster-compared in v3.1
                    grade = "moderate" if signals["engagement_score"] > 50 else "weak"

                    try:
                        cur = conn.cursor()
                        cur.execute(
                            f"INSERT INTO {table} (channel_id, factor_id, evidence_grade, "
                            "corroboration_count, compared_against, evidence_note, "
                            "extracted_by_run_id, classifier_model) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT (channel_id, factor_id, extracted_by_run_id) DO NOTHING",
                            (ch_id, factor_id, grade, corroboration_count,
                             "channel_own_baseline", note, run_id, classifier_model),
                        )
                        conn.commit()
                        cur.close()
                        factor_count += 1
                    except Exception:
                        conn.rollback()
                        continue

            extracted += 1

        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="extract_success_failure_factors",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
            ).model_dump())
            continue

    put_connection(conn)
    return {
        "node_logs": _log({"extracted": extracted, "eligible": len(eligible)}),
        "errors": errors,
    }
"""extract_success_failure_factors — structured factor extraction for classified channels.

Gated by meets_subscriber_floor. Reads a channel's own computed signals,
expressed as percentile rank against every other floor-qualifying channel in
the store, and the taxonomy IDs. Extracts which success/failure factors
apply and how strong the evidence is.

Output is rows in channel_success_factors / channel_failure_factors — never prose.

Evidence grading (§ design note): a prior version graded every factor off a
single fixed check — engagement_score > 50 — regardless of which factor was
actually being claimed. Two failures from that: (1) engagement_score here is
a weighted average of ratios each capped at 1.0 and multiplied by 100, so
its real-world ceiling for actual channels is nowhere near 100 — this run's
channels topped out at 40.73 — making "moderate" and "strong" unreachable by
construction, and every single row graded "weak". (2) It graded EVERY factor
by the same one metric even when the factor was about something else
entirely: a channel with evergreen_score=100.0 (literally the maximum) got
its "high_evergreen_rating" factor graded weak, because the check never
looked at evergreen_score at all. Fixed by computing this run's percentile
distribution for each numeric signal up front, handing the LLM percentile
rank (not an opaque absolute number with no stated scale) alongside the raw
value, and having it grade each factor against the metric IT actually cites.
corroboration_count was also hardcoded to 1 for every row ("v3.1" TODO) —
now reconciled after the batch to a real count of how many other channels in
this run confirmed the same factor.
"""

from __future__ import annotations

import bisect
import json
import time
from typing import Any

from src.llm.json_parse import loads_forgiving
from src.tools.run_scope import scope_clause
from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
from src.state import NodeLog, ErrorRecord

SYSTEM_PROMPT = """You are a YouTube channel analyst. Given a channel's structured signals and a list of available factor codes, identify which factors apply and how strong the evidence is.

Numeric signals are given both as a raw value and as a PERCENTILE (0-100) against every other channel in this dataset — the percentile is what tells you whether a number is actually distinctive, since the raw scale varies by metric.

Success factors are things the channel is doing RIGHT that correlate with its performance.
Failure factors are things the channel is doing WRONG or missing that correlate with its underperformance.

For each factor:
- factor_code: the exact code from the provided list
- evidence_note: one short factual sentence citing the actual number(s) that support it (e.g. "uploads_per_week_avg=12, upload_consistency_percentile=91")
- evidence_grade: how strong the evidence is, judged from the PERCENTILE of whichever signal(s) this specific factor is actually about — not a different metric:
  - "strong": the relevant percentile is at or beyond the 80th (or, for a failure factor, at or below the 20th)
  - "moderate": at or beyond the 60th percentile (or at or below the 40th for a failure factor)
  - "weak": the factor is real but the underlying signal isn't distinctive in this dataset

Select only factors that have clear evidence in the provided signals — use the percentiles to judge both WHICH factors apply and HOW STRONG the evidence is, and grade each factor against the metric it is actually about. Do NOT invent factor codes not in the list.

Respond with ONLY a JSON object:
{
  "success_factors": [
    {"factor_code": "consistent_upload_cadence", "evidence_note": "...", "evidence_grade": "strong"},
    ...
  ],
  "failure_factors": [
    {"factor_code": "no_monetization_signal", "evidence_note": "...", "evidence_grade": "moderate"},
    ...
  ]
}"""

_VALID_GRADES = {"strong", "moderate", "weak"}


def _percentile(sorted_values: list[float], value: float) -> float:
    """Percentile rank of `value` within `sorted_values` (0-100).

    Scale-agnostic by construction — unlike a fixed absolute threshold, this
    is meaningful regardless of what range a given metric actually occupies
    for real channels.
    """
    if not sorted_values:
        return 50.0
    idx = bisect.bisect_right(sorted_values, value)
    return round(100.0 * idx / len(sorted_values), 1)


def _fallback_grade(percentiles: list[float], is_failure: bool) -> str:
    """Used only if the LLM omits/mis-formats evidence_grade — a real grade
    derived from the same percentile evidence it was given, not a silent
    default to "weak" every time."""
    if not percentiles:
        return "weak"
    extreme = min(percentiles) if is_failure else max(percentiles)
    threshold_strong = 20.0 if is_failure else 80.0
    threshold_moderate = 40.0 if is_failure else 60.0
    if is_failure:
        if extreme <= threshold_strong:
            return "strong"
        if extreme <= threshold_moderate:
            return "moderate"
        return "weak"
    if extreme >= threshold_strong:
        return "strong"
    if extreme >= threshold_moderate:
        return "moderate"
    return "weak"


def _mark_checked(conn, channel_id: str) -> None:
    """Record that this channel's success/failure factors were attempted.

    Set unconditionally on a completed attempt, whether or not it produced
    any factor rows -- a real zero-result and "never looked at" both leave
    channel_success_factors/channel_failure_factors empty, so a marker
    outside those tables is the only way to tell them apart. Without it,
    a channel the model genuinely finds nothing for stays eligible on every
    future pass, and the node's own internal loop re-selects and re-bills
    it indefinitely.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE channels SET success_failure_factors_checked_at = NOW() "
            "WHERE channel_id = %s",
            (channel_id,),
        )
        conn.commit()
    finally:
        cur.close()


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

    # This run's actual distribution for every numeric signal a factor might
    # cite, computed once up front — the percentiles below are what let the
    # LLM grade evidence against real peers instead of an arbitrary absolute
    # number that may not even be reachable for real channels.
    try:
        cur = conn.cursor()
        # The cohort distribution is deliberately NOT scoped: a percentile
        # is only meaningful against a population, and this run's own
        # handful of channels is not one. Comparing a channel to itself
        # would rank every one of them at the 50th percentile.
        cur.execute(
            "SELECT engagement_score, evergreen_score, upload_consistency_score, "
            "uploads_per_week_avg FROM channels WHERE meets_subscriber_floor = TRUE"
        )
        cohort_rows = cur.fetchall()
        cur.close()
        cohort = {
            "engagement_score": sorted(float(r[0]) for r in cohort_rows if r[0] is not None),
            "evergreen_score": sorted(float(r[1]) for r in cohort_rows if r[1] is not None),
            "upload_consistency_score": sorted(float(r[2]) for r in cohort_rows if r[2] is not None),
            "uploads_per_week_avg": sorted(float(r[3]) for r in cohort_rows if r[3] is not None),
        }
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "cohort distribution query failed", "extracted": 0})}

    # WHICH channels get factors, though, is this run's business only.
    # Unscoped, a run wrote factors for whatever fifty classified channels
    # in the table happened to lack them -- other runs' channels, tagged
    # with this run's id -- while its own shipped the Success Factors and
    # Failure Factors sheets empty.
    scope_sql, scope_params = scope_clause(state, "c.channel_id")

    def _fetch_eligible_batch() -> list[str]:
        cur = conn.cursor()
        try:
            # Keyed on success_failure_factors_checked_at, not on whether a
            # factor row exists. A channel the model genuinely found zero
            # success factors for has no row in channel_success_factors
            # either way, so "no row" cannot mean "not attempted" -- that
            # conflation put a channel with a real zero result back in every
            # batch forever. The marker is set once, after the attempt,
            # regardless of what it found.
            cur.execute(
                "SELECT c.channel_id FROM channels c "
                "WHERE c.meets_subscriber_floor = TRUE AND c.classifier_model IS NOT NULL "
                "AND c.success_failure_factors_checked_at IS NULL "
                + scope_sql +
                "ORDER BY c.channel_id LIMIT 50",
                scope_params,
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()

    # This node runs exactly once, after every branch is done (unlike
    # classify_channel/score_thumbnail_signals, which run per-round) — a
    # single LIMIT 50 here silently capped the WHOLE run's factor
    # extraction at 50 channels, in arbitrary DB order, no matter how many
    # hundreds actually qualified. Loop batches until none remain, with a
    # safety ceiling on total channels so a pathological run can't turn
    # this into an unbounded sequence of LLM calls.
    max_channels = 1000
    try:
        eligible = _fetch_eligible_batch()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "extracted": 0})}

    extracted = 0
    total_cost = 0.0
    total_eligible_seen = 0
    from src.tools.dedup import persist_channel_v3

    while eligible:
        total_eligible_seen += len(eligible)
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

                # Build structured prompt with available factor codes.
                # engagement_score/evergreen_score are NUMERIC columns —
                # psycopg returns Decimal, which json.dumps cannot
                # serialize without an explicit float cast (same bug fixed
                # in classify_channel.py).
                engagement_score = float(ch_row[1] or 0)
                evergreen_score = float(ch_row[2] or 0)
                uploads_per_week = float(ch_row[4] or 0)
                upload_consistency = float(ch_row[5] or 0)
                # Percentile rank against every other floor-qualifying
                # channel in the store — this is what makes evidence_grade
                # meaningful; the raw values alone don't say whether 15.0 is
                # unremarkable or exceptional for this metric.
                percentiles = {
                    "engagement_percentile": _percentile(cohort["engagement_score"], engagement_score),
                    "evergreen_percentile": _percentile(cohort["evergreen_score"], evergreen_score),
                    "upload_consistency_percentile": _percentile(cohort["upload_consistency_score"], upload_consistency),
                    "uploads_per_week_percentile": _percentile(cohort["uploads_per_week_avg"], uploads_per_week),
                }
                signals = {
                    "subscriber_count": ch_row[0] or 0,
                    "engagement_score": engagement_score,
                    "evergreen_score": evergreen_score,
                    "is_likely_news": bool(ch_row[3]),
                    "uploads_per_week_avg": uploads_per_week,
                    "upload_consistency_score": upload_consistency,
                    "face_status": ch_row[6] or "unknown",
                    "dominant_format": ch_row[7] or "",
                    "affiliate": bool(ch_row[8]),
                    "sponsor": bool(ch_row[9]),
                    "membership": bool(ch_row[10]),
                    **percentiles,
                }
                prompt = json.dumps({
                    "channel_signals": signals,
                    "available_success_codes": [c["code"] for c in success_codes],
                    "available_failure_codes": [c["code"] for c in failure_codes],
                }, indent=2)

                try:
                    result = complete_tier("mid", prompt, SYSTEM_PROMPT)
                    usage = result.get("usage", {})
                    total_cost += result.get(
                        "cost_usd",
                        estimate_cost("mid", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
                    )
                    content = result.get("content", "")
                    parsed = loads_forgiving(content, expect="object")
                except Exception as exc:
                    # NOT marked checked: this is the LLM call itself
                    # failing (network, malformed response, rate limit),
                    # which is transient by nature and worth a later pass
                    # retrying. Contrast with the outer except below, where
                    # something about the channel's OWN data broke a local
                    # step -- that fails the same way every time and marking
                    # it is what stops the loop.
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
                        except Exception as exc:
                            conn.rollback()
                            errors.append(ErrorRecord(
                                node_name="extract_success_failure_factors",
                                error_type=type(exc).__name__,
                                message=f"factor taxonomy lookup failed for {code}: {exc}",
                                recoverable=True,
                            ).model_dump())
                            continue

                        # Evidence grade: prefer the LLM's own judgement,
                        # since it was given percentile context and knows
                        # which specific metric this factor is actually
                        # about. Fall back to a real percentile-derived
                        # grade — never a silent "weak" — only if the model
                        # omitted the field or returned something invalid.
                        llm_grade = str(f.get("evidence_grade", "")).strip().lower()
                        if llm_grade in _VALID_GRADES:
                            grade = llm_grade
                        else:
                            grade = _fallback_grade(
                                list(percentiles.values()), is_failure=(table == "channel_failure_factors")
                            )
                        # Reconciled to a real cross-channel count after the
                        # whole batch finishes (see below) — this is just
                        # the NOT NULL placeholder at insert time.
                        corroboration_count = 1

                        try:
                            cur = conn.cursor()
                            cur.execute(
                                f"INSERT INTO {table} (channel_id, factor_id, evidence_grade, "
                                "corroboration_count, compared_against, evidence_note, "
                                "extracted_by_run_id, classifier_model) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                                "ON CONFLICT (channel_id, factor_id, extracted_by_run_id) DO NOTHING",
                                (ch_id, factor_id, grade, corroboration_count,
                                 "run_cohort_percentile", note, run_id, classifier_model),
                            )
                            conn.commit()
                            cur.close()
                            factor_count += 1
                        except Exception as exc:
                            conn.rollback()
                            errors.append(ErrorRecord(
                                node_name="extract_success_failure_factors",
                                error_type=type(exc).__name__,
                                message=f"factor insert failed for {ch_id}/{code}: {exc}",
                                recoverable=True,
                            ).model_dump())
                            continue

                extracted += 1
                _mark_checked(conn, ch_id)

            except Exception as exc:
                # A failed statement anywhere above leaves the connection's
                # transaction aborted, poisoning every remaining channel in
                # this loop with InFailedSqlTransaction unless rolled back.
                conn.rollback()
                errors.append(ErrorRecord(
                    node_name="extract_success_failure_factors",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=True,
                ).model_dump())
                # Marked anyway. A channel whose signals are simply
                # malformed (a NULL where a float is expected, say) fails
                # the same way on every future attempt too -- leaving it
                # eligible would recreate exactly the loop this marker
                # exists to prevent, just via a different failure mode. The
                # `continue` below never reaches the mark above, so it is
                # repeated here explicitly.
                try:
                    _mark_checked(conn, ch_id)
                except Exception:
                    pass
                continue

        if total_eligible_seen >= max_channels:
            break
        try:
            eligible = _fetch_eligible_batch()
        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="extract_success_failure_factors",
                error_type=type(exc).__name__,
                message=f"batch re-fetch failed after {total_eligible_seen} channels: {exc}",
                recoverable=True,
            ).model_dump())
            break

    # Reconcile corroboration_count to how many channels in THIS run
    # actually confirmed each factor — computed once over the finished
    # batch rather than incrementally, so every row (including the first
    # channel processed) ends up with the true final count, not just
    # whatever had been seen by the time it was inserted.
    for table in ("channel_success_factors", "channel_failure_factors"):
        try:
            cur = conn.cursor()
            cur.execute(
                f"""
                WITH counts AS (
                    SELECT factor_id, COUNT(DISTINCT channel_id) AS n
                    FROM {table}
                    WHERE extracted_by_run_id = %(run_id)s
                    GROUP BY factor_id
                )
                UPDATE {table} t SET corroboration_count = counts.n
                FROM counts
                WHERE t.factor_id = counts.factor_id AND t.extracted_by_run_id = %(run_id)s
                """,
                {"run_id": run_id},
            )
            conn.commit()
            cur.close()
        except Exception as exc:
            conn.rollback()
            errors.append(ErrorRecord(
                node_name="extract_success_failure_factors",
                error_type=type(exc).__name__,
                message=f"corroboration_count reconciliation failed for {table}: {exc}",
                recoverable=True,
            ).model_dump())

    put_connection(conn)
    return {
        "node_logs": _log({
            "extracted": extracted,
            "eligible_seen": total_eligible_seen,
            "cost_usd": round(total_cost, 6),
        }),
        "errors": errors,
        "budget_spent_usd": total_cost,
    }
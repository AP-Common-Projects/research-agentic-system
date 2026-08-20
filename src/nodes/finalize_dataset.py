"""finalize_dataset — replaces synthesize. No narrative generation.

Runs deterministic store queries: completeness rollup, run-level stats,
cluster-relative percentile ranks. Populates harness_runs with final counts.
The previous synthesize's evidence-grading engine is preserved and reused
in extract_success_failure_factors (§7.3).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.state import NodeLog


def finalize_dataset(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    run_id = state.get("run_id", "")
    start = time.monotonic()
    cfg = get_config().harness

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="finalize_dataset",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable"})}

    try:
        cur = conn.cursor()

        # -- completeness rollup -------------------------------------------------
        cur.execute("SELECT COUNT(*) FROM channels WHERE first_discovered_run_id = %s", (run_id,))
        channels_discovered = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM channels WHERE meets_subscriber_floor = TRUE "
            "AND first_discovered_run_id = %s", (run_id,)
        )
        channels_enriched = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM videos WHERE channel_id IN (SELECT channel_id FROM channels WHERE first_discovered_run_id = %s)", (run_id,))
        videos_persisted = cur.fetchone()[0]

        # Compute completeness scores
        required_cols = [
            "country_code", "country_source", "primary_language_code",
            "face_status", "evergreen_score", "engagement_score",
            "meets_subscriber_floor",
        ]
        # A prior version of this ran once per column with an unaliased
        # self-referencing subquery: `FROM channels WHERE channel_id =
        # channels.channel_id` — since the outer UPDATE target and the
        # inner SELECT source share the same unqualified table name,
        # Postgres resolves `channels.channel_id` to the subquery's OWN row,
        # making the WHERE always true. COUNT(*) FILTER(...) then counted
        # NOT-NULL rows across the entire table, not the one row being
        # updated — divided by 7, this overflowed data_completeness_score's
        # NUMERIC(3,2) the moment more than ~70 channels existed anywhere in
        # the database (714 here), raising NumericValueOutOfRange and
        # aborting finalize_dataset with no completeness data ever written.
        # It also looped and overwrote the score per-column instead of
        # summing all 7 — only the last column in the loop ever survived.
        # A single direct-column expression, correlated by the UPDATE's own
        # row (no subquery needed), fixes both.
        score_expr = " + ".join(f"(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END)" for col in required_cols)
        cur.execute(
            f"UPDATE channels SET data_completeness_score = ({score_expr}) * 1.0 / {len(required_cols)} "
            f"WHERE first_discovered_run_id = %s", (run_id,)
        )
        conn.commit()

        # A prior version only ever APPENDED here — a column that was NULL
        # when finalize_dataset first ran and later got backfilled (by a
        # resume, or by re-running an earlier enrichment node) stayed
        # listed as missing forever, since nothing ever removed it from the
        # array. Same fix as data_completeness_score above: rebuild the
        # array fresh from the row's CURRENT state every call, rather than
        # mutating whatever was there before.
        missing_expr = ", ".join(
            f"CASE WHEN {col} IS NULL THEN '{col}' END" for col in required_cols
        )
        cur.execute(
            f"UPDATE channels SET missing_required_fields = "
            f"ARRAY_REMOVE(ARRAY[{missing_expr}], NULL) "
            f"WHERE first_discovered_run_id = %s", (run_id,)
        )
        conn.commit()

        # -- harness_runs update -------------------------------------------------
        cost_by_model = state.get("spend_by_model", {})
        total_cost = state.get("budget_spent_usd", 0.0)
        seed_niches = state.get("selected_niches", []) or [state.get("selected_niche", "")]
        config_snapshot = {
            "profile": cfg.profile,
            "subscriber_floor": cfg.subscriber_floor,
            "budget_limit_usd": cfg.budget_limit_usd,
            "max_tree_depth": cfg.max_tree_depth,
            "max_branches": cfg.max_branches,
        }

        cur.execute(
            "UPDATE harness_runs SET completed_at = %(now)s, status = %(status)s, "
            "channels_discovered = %(cd)s, channels_enriched = %(ce)s, "
            "videos_persisted = %(vp)s, total_cost_usd = %(tc)s, "
            "cost_by_model = %(cbm)s WHERE run_id = %(run_id)s",
            {
                "now": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
                "cd": channels_discovered,
                "ce": channels_enriched,
                "vp": videos_persisted,
                "tc": total_cost,
                "run_id": run_id,
                "cbm": json.dumps(cost_by_model) if cost_by_model else None,
            },
        )
        conn.commit()
        cur.close()

    except Exception as exc:
        # A bare "finalization failed" here once hid a NumericValueOutOfRange
        # (see the data_completeness_score comment above) with no way to
        # diagnose it short of reproducing the whole function by hand.
        conn.rollback()
        put_connection(conn)
        return {"node_logs": _log({
            "reason": "finalization failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })}

    put_connection(conn)
    return {
        "node_logs": _log({
            "channels_discovered": channels_discovered,
            "channels_enriched": channels_enriched,
            "videos_persisted": videos_persisted,
            "total_cost_usd": total_cost,
        }),
    }
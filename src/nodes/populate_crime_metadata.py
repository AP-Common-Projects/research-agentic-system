"""populate_crime_metadata — v4 Crime vertical enrichment (brief §8-10).

Extracts case_metadata, reveal_mechanisms, and comment samples for
qualifying Crime channels. Uses LLM for structured extraction with
controlled-vocabulary matching via match_or_create_controlled_term.

Gated by meets_subscriber_floor AND vertical='crime'.
Idempotent: skips channels where crime_case_metadata.classified_at is set.
"""

from __future__ import annotations

import json
import time
from difflib import SequenceMatcher
from typing import Any

from src.llm.json_parse import loads_forgiving
from src.tools.run_scope import scope_clause
from src.tools import deadline as run_deadline
from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
import structlog

from src.state import NodeLog, ErrorRecord

SYSTEM_PROMPT = """You analyze true-crime videos to extract structured case metadata.

Input: a JSON array of videos, each with a title, description and channel.

Return a JSON array of the SAME length, in the SAME order — one object per
input video. Analyse each video independently; do not let one video's case
influence another's.

For each video, output:
1. crime_type: the type of crime depicted.
   Propose a concise label (e.g. "homicide", "missing_person", "fraud", "robbery", "domestic_violence",
   "kidnapping", "terrorism", "cybercrime", "drug_crime", "other").

2. victim_type: the victim classification. Propose one.

3. suspect_relationship: the relationship between suspect and victim. Propose one.

4. investigation_type: the type of investigation. Propose one
   (e.g. "homicide_investigation", "missing_persons", "cold_case_review", "fraud_investigation", etc.).

5. evidence_type_primary: the primary type of evidence. Propose one
   (e.g. "dna", "cctv", "witness_testimony", "phone_records", "forensic", "confession", etc.).

6. case_status: exactly one of — "solved", "unsolved", "ongoing", "cold_case", "unknown"

7. case_fame_level: exactly one of —
   "nationally_known", "regionally_known", "locally_known", "obscure", "unknown"

8. case_country: ISO country name or "unknown"

9. case_year: approximate year of the crime, or null if unclear

10. reveal_mechanisms: list from this fixed set —
    "interrogation_confession", "suspect_mistake", "cctv", "phone_device_data",
    "dna", "call_911", "witness", "social_media", "financial_records",
    "location_data", "other"

Rules:
- For fields 1-5, propose specific labels — they will be matched against a controlled vocabulary.
  Be specific, not generic. "homicide" not "violent_crime".
- case_status and case_fame_level MUST be from the exact listed values.
- reveal_mechanisms MUST be from the exact listed values.
- Allow multiple reveal mechanisms per video.
- NEVER fabricate. If the video doesn't clearly indicate something, use "unknown".
- Preserve the exact order and count of the input videos.

Respond with ONLY a JSON array, one object per input video, in order:
[
  {
    "crime_type": "...",
    "victim_type": "...",
    "suspect_relationship": "...",
    "investigation_type": "...",
    "evidence_type_primary": "...",
    "case_status": "...",
    "case_fame_level": "...",
    "case_country": "...",
    "case_year": null,
    "reveal_mechanisms": ["dna", "witness"]
  }
]"""

# One mid-tier call per video cost ~$0.003 and, at ~94 videos across 240
# channels, would have run to roughly $67 for Crime alone — about seventeen
# times everything else in the pipeline combined, purely from the call
# pattern rather than the work. Batching matches what describe_video_titles
# and the factor extractor already do; 20 is deliberately below their 30
# because each item here carries a 600-char description and returns ten
# fields, so the response is far larger per item.
# 8, not 20. A 20-item response measured 7,906 completion tokens against
# the mid tier's 8,192 max_tokens -- 96.5% of the ceiling. Batches whose
# descriptions run slightly long truncate, the JSON fails to parse, and the
# halving path re-calls the same videos at 10, then 5, then 2... Throughput
# collapsed to 5.5 rows/min against an expected 60, and cost came in 4x
# over estimate. 8 items is ~3,200 output tokens: no truncation, no cascade.
logger = structlog.get_logger(__name__)

CRIME_METADATA_BATCH_SIZE = 8


def _safe_str(val: Any) -> str:
    return str(val or "").strip()


def _match_or_default(term: str, table: str, conn: Any) -> int | None:
    from src.tools.dedup import match_or_create_controlled_term
    return match_or_create_controlled_term(term, table, conn, "term_name", 0.7)


def _run_has_crime_channels(conn: Any, state: dict) -> bool:
    """Whether this run's own channels include any under the crime category.

    Deliberately asks about the run's channels rather than its videos: a
    crime run whose case rows are all already filled is still a crime run,
    and should still show the step having found nothing left to do. Only a
    run with no crime channels at all skips.

    A query failure answers True. Being wrong that way costs one no-op node;
    being wrong the other way would silently drop case metadata from a real
    crime run's workbook.
    """
    scope_sql, scope_params = scope_clause(state, "c.channel_id")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM channels c "
                "JOIN channel_niches cn ON c.channel_id = cn.channel_id "
                "AND cn.is_primary = TRUE "
                "JOIN niche_taxonomy nt ON cn.niche_id = nt.niche_id "
                "WHERE nt.parent_category = 'crime' "
                + scope_sql
                + "LIMIT 1",
                scope_params,
            )
            return cur.fetchone() is not None
    except Exception:
        conn.rollback()
        logger.warning("crime_scope_check_failed", run_id=state.get("run_id", ""))
        return True


def populate_crime_metadata(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    run_id = state.get("run_id", "")
    start = time.monotonic()
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="populate_crime_metadata",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "populated": 0})}

    # Every column this node writes is a crime case-file field, so on a
    # technology or finance run it has nothing to do. It used to run anyway,
    # find nothing, and report itself -- putting "Filling case details" in
    # the activity of a run about laptops. Returning no NodeLog at all is
    # what keeps the step out of the console: the activity list is built
    # from the logs a run emits, so a node that does not log does not appear.
    #
    # The category cannot be known at launch: classify_channel decides each
    # niche's parent_category during the run, so the only honest test is
    # whether this run's own channels turned out to be crime ones.
    if not _run_has_crime_channels(conn, state):
        put_connection(conn)
        return {}

    # Find Crime videos from channels meeting the floor, not yet classified

    # Scoped via src.tools.run_scope, not the ad-hoc scope_channel_ids key
    # this replaced -- that key is never set by a real graph run, only
    # discovered_channel_ids is, so a crime run's own videos competed with
    # every other crime run's leftover backlog for the same ORDER BY
    # outlier_score LIMIT 50 window, with no guarantee of winning it.
    scope_sql, scope_params = scope_clause(state, "v.channel_id")

    try:
        cur = conn.cursor()
        cur.execute(
            # COALESCE because raw descriptions were silently dropped on
            # write for the whole life of the dataset (see persist_video_v3):
            # every existing row has description NULL. video_description is
            # the LLM's one-line gloss of the title and is present for most
            # videos, so it keeps the classifier working on the data we
            # actually hold rather than blocking on a re-hydration.
            "SELECT v.video_id, v.title, "
            "COALESCE(NULLIF(v.description, ''), v.video_description) AS description, "
            "v.channel_id, "
            "c.title as channel_title FROM videos v "
            "JOIN channels c ON v.channel_id = c.channel_id "
            "LEFT JOIN crime_case_metadata ccm ON v.video_id = ccm.video_id "
            "JOIN channel_niches cn ON c.channel_id = cn.channel_id AND cn.is_primary = TRUE "
            "JOIN niche_taxonomy nt ON cn.niche_id = nt.niche_id "
            "WHERE nt.parent_category = 'crime' "
            "AND c.meets_subscriber_floor = TRUE "
            "AND ccm.video_id IS NULL "
            + scope_sql
            # Either source of text will do; a bare title is too thin to
            # classify a case from, so those are left for a later pass.
            + "AND COALESCE(NULLIF(v.description, ''), v.video_description) IS NOT NULL "
            "ORDER BY v.outlier_score DESC NULLS LAST LIMIT 50",
            scope_params,
        )
        eligible = cur.fetchall()
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "populated": 0})}

    _VALID_MECHANISMS = {
        "interrogation_confession", "suspect_mistake", "cctv",
        "phone_device_data", "dna", "call_911", "witness",
        "social_media", "financial_records", "location_data", "other",
    }

    def _persist(row, parsed) -> bool:
        """Write one video's case metadata and reveal mechanisms."""
        vid_id = row[0]
        cur2 = conn.cursor()
        try:
            cur2.execute(
                """INSERT INTO crime_case_metadata (video_id, crime_type, victim_type,
                   suspect_relationship, investigation_type, evidence_type_primary,
                   case_status, case_fame_level, case_country, case_year,
                   classifier_model, classifier_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (video_id) DO UPDATE SET
                   crime_type = EXCLUDED.crime_type,
                   case_status = EXCLUDED.case_status,
                   case_fame_level = EXCLUDED.case_fame_level,
                   classifier_model = EXCLUDED.classifier_model,
                   classified_at = now()""",
                (vid_id,
                 _safe_str(parsed.get("crime_type")),
                 _safe_str(parsed.get("victim_type")),
                 _safe_str(parsed.get("suspect_relationship")),
                 _safe_str(parsed.get("investigation_type")),
                 _safe_str(parsed.get("evidence_type_primary")),
                 _safe_str(parsed.get("case_status", "unknown")),
                 _safe_str(parsed.get("case_fame_level", "unknown")),
                 _safe_str(parsed.get("case_country")),
                 parsed.get("case_year"),
                 "deepseek-v4-pro", "v4.0"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            return False
        finally:
            cur2.close()

        mechanisms = parsed.get("reveal_mechanisms", [])
        if isinstance(mechanisms, list):
            for mech in mechanisms:
                mech_str = _safe_str(mech).lower().replace(" ", "_").replace("/", "_")
                if mech_str not in _VALID_MECHANISMS:
                    continue
                cur3 = conn.cursor()
                try:
                    cur3.execute(
                        "INSERT INTO video_reveal_mechanisms (video_id, mechanism) "
                        "VALUES (%s, %s) ON CONFLICT (video_id, mechanism) DO NOTHING",
                        (vid_id, mech_str),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                finally:
                    cur3.close()
        return True

    def _classify(rows) -> int:
        """One LLM call for a batch of videos, splitting on parse failure.

        A batch whose JSON comes back malformed is halved and retried, down
        to single items, so one bad response costs its own item rather than
        the other nineteen. Same recovery the video-description backfill
        needed: batching is what makes this node affordable, and without the
        split a single failure would silently drop a whole batch.
        """
        if not rows:
            return 0
        payload = [
            {
                "video_title": _safe_str(r[1]),
                "description": _safe_str(r[2])[:600],
                "channel": _safe_str(r[4]),
            }
            for r in rows
        ]
        try:
            result = complete_tier("mid", json.dumps(payload, indent=2), SYSTEM_PROMPT)
            content = result.get("content") or ""
            try:
                parsed = loads_forgiving(content, expect="array")
            except Exception:
                parsed = None
            if parsed is None or (isinstance(parsed, list) and len(parsed) != len(rows)):
                logger.warning(
                    "crime_metadata_batch_unusable",
                    size=len(rows),
                    got=(len(parsed) if isinstance(parsed, list) else None),
                    completion_tokens=(result.get("usage") or {}).get("completion_tokens"),
                )
            if not isinstance(parsed, list) or len(parsed) != len(rows):
                raise ValueError(
                    f"expected {len(rows)} objects, got "
                    f"{len(parsed) if isinstance(parsed, list) else type(parsed).__name__}"
                )
        except Exception as exc:
            if len(rows) == 1:
                errors.append(ErrorRecord(
                    node_name="populate_crime_metadata",
                    error_type=type(exc).__name__,
                    message=f"unrecoverable for {rows[0][0]}: {exc}",
                    recoverable=True,
                ).model_dump())
                return 0
            mid = len(rows) // 2
            return _classify(rows[:mid]) + _classify(rows[mid:])

        done = 0
        for row, obj in zip(rows, parsed):
            if isinstance(obj, dict) and _persist(row, obj):
                done += 1
        return done

    populated = 0
    for i in range(0, len(eligible), CRIME_METADATA_BATCH_SIZE):
        # The write-up chain used to run to completion however long it
        # took; a one-hour crime run spent 1h45m in it. Checked per
        # batch so overshoot is one batch, not one whole node.
        # Healing bypasses this -- see deadline.writeup_passed.
        if run_deadline.writeup_passed(state):
            break
        populated += _classify(eligible[i : i + CRIME_METADATA_BATCH_SIZE])

    put_connection(conn)
    return {
        "node_logs": _log({"populated": populated, "eligible": len(eligible)}),
        "errors": errors,
    }
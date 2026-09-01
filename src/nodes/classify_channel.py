"""classify_channel — LLM classification of face/faceless, format, and niche.

Gated by meets_subscriber_floor (§6.4). Input is structured signals plus the
channel's own title/description and a handful of recent video titles —
never raw images or full transcripts. Output: face_status, dominant_format,
and a proposed niche_name that gets canonicalized against niche_taxonomy
before insert.

An earlier version sent numeric signals ONLY (subscriber count, engagement
score, cadence — no text at all) on the theory that this was cheaper/safer.
In practice it meant the model had zero information about what a channel is
actually ABOUT, and produced plausible-sounding but disconnected niches —
observed live: "PoliceActivity" (6.9M subs, real bodycam/police content)
classified as "ambient_relaxation_music", "JCS - Criminal Psychology" as
"viral_compilation". Niche classification is exactly the one thing this
node cannot do without text — cadence/engagement numbers say nothing about
subject matter. Title/description text is cheap and already gets sent to
the LLM elsewhere in this codebase (taxonomy building); there was no real
cost or privacy reason to withhold it here specifically.

The idempotency guard (§10 item 2): if classifier_model + classifier_version
already match, skip the LLM call — a resume must not re-bill a channel that
was already classified.
"""

from __future__ import annotations

import json
import re
import time
from difflib import SequenceMatcher

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
from src.state import NodeLog, ErrorRecord

SYSTEM_PROMPT = """You are a YouTube channel classifier. Given a channel's own title/description, a sample of its recent video titles, and structured signals, produce a classification.

Input:
- channel_title, channel_description: the channel's own text — this is the primary evidence for what the channel is actually about
- recent_video_titles: a sample of real, current video titles from this channel
- face signals: how many thumbnails have faces
- engagement_score: 0-100 composite
- evergreen_score: 0-100
- is_likely_news: boolean
- uploads_per_week_avg: number
- upload_consistency_score: 0-1
- subscriber_count: number
- monetization: affiliate, sponsor, membership signals

Rules:
1. face_status: does the CREATOR appear on camera as an on-screen presenter? "face" = the creator presents on camera (talking head, vlog, reaction). "faceless" = the creator never appears; the channel is narration over footage, archival/bodycam/CCTV clips, compilations, animation, gameplay, or slideshows. "mixed" = genuinely both.
   This is about whether someone could REPLICATE this channel without ever being on camera — so a bodycam, interrogation-footage, or narrated-documentary channel is "faceless" even though human faces obviously appear IN the footage. Judge from the format and titles, not from whether people are visible in the video.
2. dominant_format: pick from "animated_explainer", "talking_head", "documentary_narration", "compilation", "vlog", "clip_commentary", "tutorial", "list_roundup", "reaction_commentary", "gaming_playthrough", "music_video", "podcast". Infer from the titles/description and the numeric signals together.
2b. is_likely_news: true if this channel's content is tied to CURRENT events — breaking news, ongoing trials/cases, recent incidents, daily/weekly news cycles, "this week" style recaps. false if the content is timeless (historical cases, resolved cold cases, evergreen explainers, documentary retrospectives). Judge primarily from the recent video titles: dates, "breaking", "update", "just in", ongoing-case language all point to news.
3. niche_name: propose a short canonical SUB-niche name (e.g. "personal_finance_budgeting", "bodycam_footage_analysis") — lowercase_underscore format, grounded in what channel_title/channel_description/recent_video_titles actually say the channel covers, as SPECIFIC as that evidence supports. Do not just repeat the parent category — a real sub-niche, not a restatement of it. This gets matched against the taxonomy table, and inserted as a new row if nothing close enough exists — your proposal is a real discovery, not a guess that gets discarded. Never propose a niche the title/description/video-titles give no evidence for.
4. parent_category: which top-level category this sub-niche belongs under. Pick the closest fit from: finance, crime, history, science_explainer, technology, gaming, lifestyle, automotive, music, entertainment. If truly none fit, propose a new short lowercase_underscore category name.
5. niche_description: one sentence describing what this sub-niche covers.
6. entertainment_score: 0-100 — how much this channel leans on entertainment value (personality, drama, humor, story, spectacle) versus pure information delivery. A documentary or true-crime channel can score high here if it's narratively engaging, not just factual. A dry tutorial or a static-slide explainer scores low even if well-made.
7. classifier_model: "deepseek-v4-pro"
8. classifier_version: "v3.0"

Respond with ONLY a JSON object:
{
  "face_status": "face"|"faceless"|"mixed",
  "dominant_format": "...",
  "is_likely_news": true|false,
  "niche_name": "...",
  "parent_category": "...",
  "niche_description": "...",
  "entertainment_score": <0-100>,
  "classifier_model": "deepseek-v4-pro",
  "classifier_version": "v3.0"
}"""


def _match_niche(
    proposed: str,
    conn: Any,
    parent_category: str = "",
    description: str = "",
    run_id: str = "",
    is_evergreen_prone: bool | None = None,
) -> int | None:
    """Fuzzy-match a proposed niche name against niche_taxonomy. Returns niche_id or None.

    No match above threshold means real sub-niche discovery, not a shrug —
    this is the client's "find sub-niches, REALLY, not mock" requirement.
    Insert it as a new canonical niche (proposed_by_run_id set) rather than
    silently dropping the LLM's proposal, which is what every classified
    channel with a genuinely new niche was doing before this fix: producing
    a real classification that then vanished at the taxonomy boundary.
    """
    if not proposed:
        return None
    normalized = proposed.lower().strip().replace(" ", "_")
    cur = conn.cursor()
    try:
        cur.execute("SELECT niche_id, niche_name FROM niche_taxonomy")
        rows = cur.fetchall()
        best_id, best_score = None, 0.0
        for nid, name in rows:
            if name == normalized:
                return nid
            score = SequenceMatcher(None, normalized, name).ratio()
            if score > best_score and score > 0.7:
                best_score = score
                best_id = nid
        if best_id is not None:
            return best_id

        cur.execute(
            "INSERT INTO niche_taxonomy (niche_name, parent_category, description, "
            "proposed_by_run_id, is_evergreen_prone) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (niche_name) DO NOTHING RETURNING niche_id",
            (
                normalized,
                (parent_category or "uncategorized").lower().strip().replace(" ", "_"),
                description or f"Auto-discovered sub-niche: {normalized}",
                run_id or None,
                # Only the hand-written seeds carried this before, so every
                # auto-discovered niche exported a blank column. The
                # proposing channel's own evergreen judgement is the best
                # evidence available at insert time.
                is_evergreen_prone,
            ),
        )
        row = cur.fetchone()
        if row:
            conn.commit()
            return row[0]
        # ON CONFLICT hit (a concurrent insert of the same name) — the row
        # exists now under a name our fuzzy pass didn't score above
        # threshold; look it up directly rather than losing the channel's
        # niche assignment.
        cur.execute("SELECT niche_id FROM niche_taxonomy WHERE niche_name = %s", (normalized,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def classify_channel(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    cfg = get_config().harness
    run_id = state.get("run_id", "")
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="classify_channel",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "classified": 0})}


    # Optional restriction to a channel set, defaulting to the node's normal
    # global behaviour. This node makes one mid-tier call per channel at
    # ~28s, so a 1,100-channel backlog is seven hours single-threaded. The
    # eligibility query is an unordered LIMIT 50, so unscoped parallel
    # workers would fetch overlapping rows and buy the same classification
    # several times; disjoint slices are what make parallelism safe.
    scope = state.get("scope_channel_ids")
    # `is not None`, not truthiness: an EMPTY scope means "this worker owns
    # no channels" and must select nothing. Treating it as falsy silently
    # widened the query to every channel in the table, so four parallel
    # workers each re-ran the entire global backlog instead of their own
    # slice -- four hours of redundant LLM calls that also re-classified
    # channels deliberately excluded from the run.
    if scope is not None:
        scope_sql = "AND channel_id = ANY(%s) "
        scope_params: tuple = (list(scope),)
    else:
        scope_sql = ""
        scope_params = ()

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT channel_id FROM channels WHERE meets_subscriber_floor = TRUE "
            "AND (classifier_model IS NULL OR classifier_model != 'deepseek-v4-pro' "
            "OR classifier_version != 'v3.0') "
            + scope_sql + "LIMIT 50",
            scope_params,
        )
        eligible = [r[0] for r in cur.fetchall()]
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "classified": 0})}

    classified = 0
    channels_enriched = 0
    total_cost = 0.0
    from src.tools.dedup import persist_channel_v3, persist_channel_niche_membership

    for ch_id in eligible:
        try:
            cur = conn.cursor()
            # thumbnail_has_face lives on `videos`, not `channels` — selecting
            # it here raised UndefinedColumn on every call, for the whole
            # life of this node, and the prompt never even referenced it
            # (dead column, skipped straight from ch_row[1] to ch_row[3]).
            cur.execute(
                "SELECT subscriber_count, face_status, "
                "engagement_score, evergreen_score, is_likely_news, "
                "uploads_per_week_avg, upload_consistency_score, "
                "has_affiliate_signal, has_sponsor_signal, has_membership_signal, "
                "title, description "
                "FROM channels WHERE channel_id = %s",
                (ch_id,),
            )
            ch_row = cur.fetchone()
            cur.close()

            if not ch_row:
                continue

            cur = conn.cursor()
            cur.execute(
                "SELECT title FROM videos WHERE channel_id = %s "
                "ORDER BY published_at DESC NULLS LAST LIMIT 10",
                (ch_id,),
            )
            recent_titles = [r[0] for r in cur.fetchall() if r[0]]
            cur.close()

            # Build structured prompt. engagement_score/evergreen_score are
            # NUMERIC columns — psycopg returns them as Decimal, which
            # json.dumps cannot serialize by default; every other numeric
            # field here was already cast to float except these two, which
            # raised TypeError on every real channel.
            prompt = json.dumps({
                "channel_title": ch_row[10] or "",
                "channel_description": (ch_row[11] or "")[:500],
                "recent_video_titles": recent_titles,
                "subscriber_count": ch_row[0] or 0,
                "face_signals": {"face_status": ch_row[1] or "unknown"},
                "engagement_score": float(ch_row[2] or 0),
                "evergreen_score": float(ch_row[3] or 0),
                "is_likely_news": bool(ch_row[4]),
                "uploads_per_week_avg": float(ch_row[5] or 0),
                "upload_consistency_score": float(ch_row[6] or 0),
                "monetization": {
                    "affiliate": bool(ch_row[7]),
                    "sponsor": bool(ch_row[8]),
                    "membership": bool(ch_row[9]),
                },
            }, indent=2)

            # LLM classification
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
                    node_name="classify_channel",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=True,
                ).model_dump())
                continue

            face_status = str(parsed.get("face_status", "unknown"))
            dominant_format = str(parsed.get("dominant_format", ""))
            niche_name = str(parsed.get("niche_name", ""))
            parent_category = str(parsed.get("parent_category", ""))
            niche_description = str(parsed.get("niche_description", ""))
            classifier_model = str(parsed.get("classifier_model", "deepseek-v4-pro"))
            classifier_version = str(parsed.get("classifier_version", "v3.0"))
            try:
                entertainment_score = max(0.0, min(100.0, float(parsed.get("entertainment_score", 0) or 0)))
            except (TypeError, ValueError):
                entertainment_score = None
            # Reading real titles beats score_signals' cadence/evergreen
            # heuristic for this one — that heuristic can only see "uploads
            # often + content ages fast", which misfires on high-volume
            # evergreen channels and misses slow-cadence news commentary.
            # Only override when the model actually answered.
            raw_news = parsed.get("is_likely_news")
            is_likely_news = bool(raw_news) if isinstance(raw_news, bool) else None

            # Canonicalize niche — inserts a new taxonomy row when nothing
            # close enough already exists (real sub-niche discovery).
            # A niche proposed by a channel whose own content ages well, and
            # which isn't news-driven, is evergreen-prone.
            channel_evergreen = float(ch_row[3] or 0)
            niche_evergreen_prone = (
                None if is_likely_news is None and not channel_evergreen
                else bool(channel_evergreen >= 50.0 and not is_likely_news)
            )
            niche_id = (
                _match_niche(
                    niche_name, conn, parent_category, niche_description, run_id,
                    niche_evergreen_prone,
                )
                if niche_name else None
            )

            usage = result.get("usage", {})
            cost = result.get("cost_usd", estimate_cost("mid", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)))
            total_cost += cost

            ch_fields = {
                "face_status": face_status,
                "dominant_format": dominant_format,
                "classifier_model": classifier_model,
                "classifier_version": classifier_version,
            }
            if entertainment_score is not None:
                ch_fields["entertainment_score"] = entertainment_score
            if is_likely_news is not None:
                ch_fields["is_likely_news"] = is_likely_news
            if niche_id:
                ch_fields["primary_niche_id"] = niche_id

            try:
                persist_channel_v3(conn, ch_id, run_id, ch_fields)
                if niche_id:
                    persist_channel_niche_membership(conn, ch_id, niche_id, is_primary=True, confidence=0.8)
                classified += 1
                channels_enriched += 1
            except Exception as exc:
                # A bare `except: continue` here once discarded a real
                # persistence failure with no record of it at all — worse
                # than silent, actively misleading, since `classified`
                # stayed 0 with zero errors logged. A failed statement also
                # leaves the connection's transaction aborted for every
                # remaining channel in this loop unless rolled back here.
                conn.rollback()
                errors.append(ErrorRecord(
                    node_name="classify_channel",
                    error_type=type(exc).__name__,
                    message=f"persist failed for {ch_id}: {exc}",
                    recoverable=True,
                ).model_dump())
                continue

        except Exception as exc:
            # Same reasoning: a failed SELECT/execute above leaves the
            # connection's transaction aborted, poisoning every subsequent
            # channel in this loop with InFailedSqlTransaction unless it's
            # rolled back before moving on.
            conn.rollback()
            errors.append(ErrorRecord(
                node_name="classify_channel",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
            ).model_dump())
            continue

    put_connection(conn)
    return {
        "node_logs": _log({"classified": classified, "eligible": len(eligible), "cost_usd": round(total_cost, 6)}),
        "channels_enriched_this_run": channels_enriched,
        "errors": errors,
        "budget_spent_usd": total_cost,
    }
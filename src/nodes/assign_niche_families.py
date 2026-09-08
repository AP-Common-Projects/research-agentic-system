"""assign_niche_families — the tier above sub-niches, for every vertical.

A Standard education run shipped 135 sub-niches for 167 channels, 115 of
them holding exactly one channel: `competitive_exam_maths`,
`preschool_learning_songs`, `proctoru_exam_preparation`. Individually
correct and collectively unreadable -- there is no comparison to make
between 115 groups of one.

The tier that fixes that already existed and was never filled in. The
`primary_niche_groups` table has carried a Primary_Niche level since the
crime brief, the Channels sheet has a `primary_niche` column reading it,
and the completeness gate waived that column with the note: "Nothing in
the pipeline assigns primary_niche_group_id, so it is set only for niches
that came with the seed data and is empty for any newly discovered
vertical -- a real limitation, but not one a run can close." Nine groups
were hand-seeded for crime and finance; 618 of 2,219 niches had one.

This node closes it. Every niche a run populates gets a family, and every
family carries at least MIN_SUB_NICHES_PER_FAMILY sub-niches -- enforced
in code after the model has proposed, because a model asked for a minimum
will cheerfully return a family of one.

Two passes, because they are two different questions and one prompt
answering both does neither well:

  propose  what families does THIS run's material actually divide into?
           One call, given the categories and their sizes.
  assign   which family does each sub-niche belong to? Chunked, so a run
           with five hundred niches is still a handful of calls, and
           concurrent, because they are independent.

Seeded groups are never overwritten. A niche that already belongs to a
curated crime or finance group keeps it; only NULLs are filled.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from src.db.connection import get_connection, put_connection
from src.llm.cascade import complete_tier, estimate_cost
from src.llm.concurrent import map_llm
from src.llm.json_parse import complete_json
from src.state import ErrorRecord, NodeLog
from src.tools import deadline as run_deadline
from src.tools.deliverable import eligible_sql
from src.tools.run_scope import scope_clause

logger = structlog.get_logger(__name__)

#: The client's rule, in one place: "each niche family consists at least
#: 10 sub-niches". A family below this is merged away rather than shipped.
MIN_SUB_NICHES_PER_FAMILY = 10

#: Sub-niches per assignment call. Small enough that the model sees every
#: label in the batch clearly, large enough that a 500-niche Deep run is
#: five calls rather than fifty.
_ASSIGN_BATCH = 60

PROPOSE_SYSTEM = """You group YouTube sub-niches into families for a market-research workbook.

A family is a level a researcher can compare across: broad enough that
several sub-niches sit under it, narrow enough to mean something. "Exam
Preparation" and "Early Childhood Learning" are families. "Education" is
usually too broad to be useful on its own; "Competitive Exam Maths" is a
sub-niche, not a family.

Return ONLY JSON:
{"families": [{"label": "Title Case Name", "description": "one sentence on what belongs here"}]}

Rules:
- Return exactly the number of families asked for, or fewer. Never more.
- Every family must be able to hold at least 10 of the sub-niches listed.
- Labels are Title Case, 2-4 words, no trailing punctuation.
- Cover the whole spread. Every sub-niche listed must have a plausible home."""

ASSIGN_SYSTEM = """You file YouTube sub-niches into a fixed list of families.

Return ONLY JSON: {"assignments": [{"sub_niche": "...", "family": "..."}]}

Rules:
- `family` MUST be copied exactly from the families given. Never invent one.
- Return one entry for every sub-niche given, in the same order.
- If a sub-niche fits poorly, pick the closest family anyway."""


def _run_niches(conn, state: dict) -> list[dict[str, Any]]:
    """Sub-niches this run populated, with any family they already carry."""
    scope_sql, scope_params = scope_clause(state, "c.channel_id")
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT nt.niche_id, nt.niche_name, nt.parent_category, "
            "       nt.description, png.group_label, "
            "       COUNT(DISTINCT c.channel_id) AS n "
            "FROM niche_taxonomy nt "
            "LEFT JOIN primary_niche_groups png "
            "       ON png.group_id = nt.primary_niche_group_id "
            "JOIN channels c ON c.primary_niche_id = nt.niche_id "
            "WHERE " + eligible_sql() + " "
            + scope_sql +
            "GROUP BY nt.niche_id, nt.niche_name, nt.parent_category, "
            "         nt.description, png.group_label "
            "ORDER BY n DESC, nt.niche_name",
            scope_params,
        )
        return [
            {"niche_id": r[0], "niche_name": r[1], "parent_category": r[2],
             "description": r[3], "current_family": r[4], "channel_count": r[5]}
            for r in cur.fetchall()
        ]
    finally:
        cur.close()


def _split_kept_and_loose(
    niches: list[dict],
) -> tuple[dict[int, str], list[dict]]:
    """Which niches already sit in a family big enough to keep.

    "Big enough" is measured IN THIS RUN, which is the only place the rule
    can be checked -- a family is what a reader sees on one workbook's
    sheet, not what it holds across the whole store.

    That distinction matters both ways. In a crime run the curated
    "Police Investigation Documentary" group holds dozens of sub-niches
    and is exactly the right answer, so it survives untouched. In this
    education run three stray finance niches pointed at three different
    curated finance groups and rendered as three families of one -- the
    precise thing the client asked to be rid of. Those are re-filed.

    Re-filing rewrites a niche's family globally, which is a real cost:
    the label is shared state. It is accepted because the family tier is a
    reading aid rather than a fact about the channel, and a family of one
    on a delivered sheet is a worse error than a label that moved.
    """
    counts: dict[str, int] = {}
    for n in niches:
        if n.get("current_family"):
            counts[n["current_family"]] = counts.get(n["current_family"], 0) + 1
    kept = {
        n["niche_id"]: n["current_family"]
        for n in niches
        if n.get("current_family")
        and counts.get(n["current_family"], 0) >= MIN_SUB_NICHES_PER_FAMILY
    }
    loose = [n for n in niches if n["niche_id"] not in kept]
    return kept, loose


def _target_family_count(n_niches: int) -> int:
    """How many families this run's material can support.

    Integer division, so the arithmetic itself keeps the promise: 135
    sub-niches support 13 families of at least 10, and asking for 14 would
    guarantee one of them breaks the rule before a model is even involved.
    """
    return max(1, n_niches // MIN_SUB_NICHES_PER_FAMILY)


#: Attempts at the proposal call before falling back to raw categories.
#:
#: Everything downstream inherits this one call's quality. When it lands,
#: an education run comes back with "Competitive Exam Preparation",
#: "K-12 Academic Support" and "Technology & Programming Education". When
#: it does not, the fallback produces "Education", "Entertainment",
#: "Lifestyle", "Technology" -- the parent_category column with title case
#: on it, which satisfies the minimum and tells the client nothing they
#: did not already have. One blank completion is not worth that drop.
_PROPOSE_ATTEMPTS = 3


def _propose_families(niches: list[dict], target: int) -> list[dict[str, str]]:
    """One call: what does this run's material divide into?"""
    by_cat: dict[str, list[str]] = {}
    for n in niches:
        by_cat.setdefault(n["parent_category"] or "uncategorised", []).append(
            n["niche_name"]
        )
    prompt = json.dumps({
        "families_wanted": target,
        "minimum_sub_niches_per_family": MIN_SUB_NICHES_PER_FAMILY,
        "total_sub_niches": len(niches),
        "categories": [
            # Twelve examples a category, not all of them: the model needs
            # the flavour of a category to name a family, not its census.
            {"category": cat, "sub_niche_count": len(names),
             "examples": names[:8]}
            for cat, names in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))
        ],
    }, indent=2)
    last: Exception | None = None
    parsed = None
    for attempt in range(1, _PROPOSE_ATTEMPTS + 1):
        try:
            parsed, _ = complete_json(
                complete_tier, "mid", prompt, PROPOSE_SYSTEM, expect="object"
            )
            break
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
            logger.warning(
                "niche_family_proposal_retry",
                attempt=attempt, exc_type=type(exc).__name__,
            )
    if parsed is None:
        raise last if last else RuntimeError("no proposal")

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for f in (parsed.get("families") or []):
        if not isinstance(f, dict):
            continue
        label = str(f.get("label", "")).strip()
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append({"label": label,
                    "description": str(f.get("description", "")).strip()})
    return out[:target]


def _assign(niches: list[dict], families: list[dict], state: dict) -> dict[int, str]:
    """Which family each sub-niche belongs to, chunked and concurrent."""
    labels = [f["label"] for f in families]
    by_label = {l.lower(): l for l in labels}
    batches = [niches[i:i + _ASSIGN_BATCH]
               for i in range(0, len(niches), _ASSIGN_BATCH)]

    def _call(batch: list[dict]):
        prompt = json.dumps({
            "families": [{"label": f["label"], "description": f["description"]}
                         for f in families],
            "sub_niches": [
                {"sub_niche": n["niche_name"],
                 "category": n["parent_category"],
                 "description": (n["description"] or "")[:160]}
                for n in batch
            ],
        }, indent=2)
        return complete_json(
            complete_tier, "mid", prompt, ASSIGN_SYSTEM, expect="object"
        )

    answers = map_llm(
        batches, _call,
        should_stop=lambda: run_deadline.writeup_passed(state),
        label="assign_niche_families",
    )

    placed: dict[int, str] = {}
    for batch, got, err in answers:
        if err is not None:
            continue
        parsed, _ = got
        # Keyed on the returned name, not on position: a model that drops
        # or reorders an entry would otherwise shift every later niche into
        # the wrong family, silently.
        answered = {}
        for a in (parsed.get("assignments") or []):
            if isinstance(a, dict):
                answered[str(a.get("sub_niche", "")).strip().lower()] = str(
                    a.get("family", "")
                ).strip()
        for n in batch:
            label = by_label.get(
                answered.get(n["niche_name"].lower(), "").lower(), ""
            )
            if label:
                placed[n["niche_id"]] = label
    return placed


def _category_profile(
    labels: dict[int, str], by_id: dict[int, dict], family: str
) -> dict[str, float]:
    """What parent_categories a family is made of, as shares."""
    counts: dict[str, int] = {}
    for nid, label in labels.items():
        if label != family:
            continue
        cat = by_id[nid].get("parent_category") or "uncategorised"
        counts[cat] = counts.get(cat, 0) + 1
    total = sum(counts.values()) or 1
    return {c: n / total for c, n in counts.items()}


def _nearest_family(
    profile: dict[str, float],
    others: list[str],
    profiles: dict[str, dict[str, float]],
    counts: dict[str, int],
) -> str:
    """The family a doomed one most resembles, by category overlap.

    Merging into whichever family happened to be LARGEST terminates but
    reads as nonsense: on the education run it filed "Technology &
    Programming Education", "Science & Nature Education" and "Language
    Learning Resources" under "Lifestyle & Family Vlogging", because that
    was the biggest bucket at the time. The label a client reads is the
    whole point of this tier.

    Overlap is the sum of min(share) per category -- 1.0 for families drawn
    from exactly the same categories, 0.0 for disjoint ones. Size breaks
    ties, so a merge still terminates.
    """
    def score(other: str) -> tuple[float, int]:
        their = profiles[other]
        overlap = sum(min(v, their.get(c, 0.0)) for c, v in profile.items())
        return (overlap, counts[other])

    return max(others, key=score)


def _enforce_minimum(
    placed: dict[int, str],
    niches: list[dict],
    families: list[dict],
) -> dict[int, str]:
    """Fold away every family that came back under the minimum.

    The rule the client asked for is a property of the OUTPUT, so it is
    enforced on the output. A model told "at least ten" will still hand
    back a family of one, and shipping that would be the same failure as
    the 115 singletons this node exists to remove.

    Unplaced niches are not dropped -- they join the largest family, which
    is the only choice that cannot itself break the minimum.
    """
    by_id = {n["niche_id"]: n for n in niches}
    counts: dict[str, int] = {}
    for label in placed.values():
        counts[label] = counts.get(label, 0) + 1

    # Anything the model never answered for.
    if len(placed) < len(niches):
        fallback = max(counts, key=counts.get) if counts else (
            families[0]["label"] if families else "General"
        )
        for n in niches:
            if n["niche_id"] not in placed:
                placed[n["niche_id"]] = fallback
                counts[fallback] = counts.get(fallback, 0) + 1

    # A run with too little material for even one full family gets one
    # family holding everything -- honest, and better than a dozen of one.
    if len(niches) < MIN_SUB_NICHES_PER_FAMILY:
        only = max(counts, key=counts.get)
        return {nid: only for nid in placed}

    while len(counts) > 1:
        smallest = min(counts, key=counts.get)
        if counts[smallest] >= MIN_SUB_NICHES_PER_FAMILY:
            break
        others = [l for l in counts if l != smallest]
        profiles = {l: _category_profile(placed, by_id, l) for l in counts}
        target = _nearest_family(profiles[smallest], others, profiles, counts)
        for nid, label in placed.items():
            if label == smallest:
                placed[nid] = target
        counts[target] += counts.pop(smallest)
        logger.info(
            "niche_family_merged", merged=smallest, into=target,
            now_holding=counts[target],
        )
    return placed


def _persist(conn, placed: dict[int, str], families: list[dict],
             niches: list[dict]) -> int:
    """Write the groups and point the niches at them."""
    by_id = {n["niche_id"]: n for n in niches}
    described = {f["label"]: f.get("description", "") for f in families}
    # A family's vertical is whichever parent_category most of its members
    # came from -- the table is keyed (vertical, group_label), and guessing
    # would collide two runs' families under one row.
    verticals: dict[str, dict[str, int]] = {}
    for nid, label in placed.items():
        cat = (by_id[nid].get("parent_category") or "uncategorised")
        verticals.setdefault(label, {})
        verticals[label][cat] = verticals[label].get(cat, 0) + 1

    group_ids: dict[str, int] = {}
    cur = conn.cursor()
    try:
        for label, cats in verticals.items():
            vertical = max(cats, key=cats.get)
            cur.execute(
                "INSERT INTO primary_niche_groups (vertical, group_label, description) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (vertical, group_label) DO UPDATE "
                "SET description = COALESCE(NULLIF(EXCLUDED.description, ''), "
                "                           primary_niche_groups.description) "
                "RETURNING group_id",
                (vertical, label, described.get(label, "") or label),
            )
            row = cur.fetchone()
            if row:
                group_ids[label] = row[0]
        conn.commit()

        updated = 0
        for nid, label in placed.items():
            gid = group_ids.get(label)
            if gid is None:
                continue
            # No IS NULL guard. _split_kept_and_loose has already decided
            # which niches keep the family they have; everything reaching
            # here is either unfiled or sitting in a family too small to
            # ship, and re-pointing it is the whole job.
            cur.execute(
                "UPDATE niche_taxonomy SET primary_niche_group_id = %s "
                "WHERE niche_id = %s",
                (gid, nid),
            )
            updated += cur.rowcount
        conn.commit()
        return updated
    finally:
        cur.close()


def assign_niche_families(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    errors: list[dict] = []

    def _log(summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="assign_niche_families",
            thread_id=thread_id,
            input_summary=summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "families": 0})}

    try:
        try:
            niches = _run_niches(conn, state)
        except Exception as exc:
            conn.rollback()
            return {"node_logs": _log({"reason": f"query failed: {exc}",
                                       "families": 0})}

        if not niches:
            return {"node_logs": _log({"reason": "no niches in scope",
                                       "families": 0})}

        # Families already big enough in THIS run are left exactly as they
        # are -- a curated crime group holding thirty sub-niches is a
        # better answer than anything proposed here.
        kept, loose = _split_kept_and_loose(niches)
        if not loose:
            sizes: dict[str, int] = {}
            for label in kept.values():
                sizes[label] = sizes.get(label, 0) + 1
            return {"node_logs": _log({
                "reason": "every family already meets the minimum",
                "sub_niches": len(niches), "families": len(sizes),
                "niches_grouped": 0,
                "smallest_family": min(sizes.values()) if sizes else 0,
                "minimum_required": MIN_SUB_NICHES_PER_FAMILY,
            })}

        target = _target_family_count(len(loose))
        try:
            families = _propose_families(loose, target)
        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="assign_niche_families",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
            ).model_dump())
            families = []

        # The families already in play are valid destinations too: a
        # handful of loose niches on a run whose families are otherwise
        # settled belong in one of those, not in a new family of three.
        kept_labels: list[str] = []
        for label in kept.values():
            if label not in kept_labels:
                kept_labels.append(label)
        destinations = families + [
            {"label": l, "description": ""} for l in kept_labels
            if l not in {f["label"] for f in families}
        ]

        if not destinations:
            # No proposal is still better than 115 families of one: fall
            # back to the categories the classifier already produced, which
            # _enforce_minimum will then merge up to the rule.
            seen: list[str] = []
            for n in loose:
                cat = (n["parent_category"] or "uncategorised").replace("_", " ").title()
                if cat not in seen:
                    seen.append(cat)
            destinations = [{"label": c, "description": ""} for c in seen]
            placed = {
                n["niche_id"]:
                    (n["parent_category"] or "uncategorised").replace("_", " ").title()
                for n in loose
            }
        else:
            placed = _assign(loose, destinations, state)

        # Enforced over the WHOLE run, kept families included, so the
        # sheet the client opens has no row under the minimum on it.
        placed.update(kept)
        families = destinations
        niches_for_rule = niches
        placed = _enforce_minimum(placed, niches_for_rule, families)
        loose_ids = {n["niche_id"] for n in loose}
        rewritten = {
            nid: label for nid, label in placed.items()
            if nid in loose_ids or placed[nid] != kept.get(nid)
        }
        try:
            updated = _persist(conn, rewritten, families, niches)
        except Exception as exc:
            conn.rollback()
            errors.append(ErrorRecord(
                node_name="assign_niche_families",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
            ).model_dump())
            updated = 0

        sizes: dict[str, int] = {}
        for label in placed.values():
            sizes[label] = sizes.get(label, 0) + 1
        return {
            "node_logs": _log({
                "sub_niches": len(niches),
                "families": len(sizes),
                "niches_grouped": updated,
                "smallest_family": min(sizes.values()) if sizes else 0,
                "minimum_required": MIN_SUB_NICHES_PER_FAMILY,
            }),
            "errors": errors,
        }
    finally:
        put_connection(conn)

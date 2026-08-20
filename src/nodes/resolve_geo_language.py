"""resolve_geo_language — deterministic geo/language assignment for every channel.

Reads the self-reported fields from hydrate_metadata (country_code from YouTube
snippet.country, primary_language_code from snippet.defaultLanguage). Falls back
through cheap, deterministic inference: title/description language detection,
then region derivation from country_code. Never uses an LLM.

Every channel ends with a country_source label and a confidence score so the
data science team knows exactly how each field was determined — never confusing
"self-reported country" with "inferred from English content."
"""

from __future__ import annotations

import re
import time
from typing import Any

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.state import NodeLog, ErrorRecord


# Common English words that strongly indicate English-language content.
_ENGLISH_MARKERS: set[str] = {
    "the", "and", "for", "you", "how", "with", "this", "that", "are",
    "your", "from", "have", "been", "what", "when", "all", "just",
    "about", "also", "will", "can", "not", "its", "but", "has", "was",
    "more", "new", "get", "one", "our", "make", "like", "out", "now",
    "some", "would", "could", "should", "other", "than", "then", "into",
    "over", "only", "way", "back", "after", "first", "through", "still",
}

# Non-Latin character ranges that indicate non-English content.
_NON_LATIN_RANGES = [
    (0x0600, 0x06FF),  # Arabic
    (0x4E00, 0x9FFF),  # CJK Unified
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x0400, 0x04FF),  # Cyrillic
    (0x0E00, 0x0E7F),  # Thai
    (0xAC00, 0xD7AF),  # Hangul
]

# ISO 3166-1 alpha-2 → region mapping
_COUNTRY_TO_REGION: dict[str, str] = {
    "US": "North America", "CA": "North America", "MX": "North America",
    "GB": "Europe", "DE": "Europe", "FR": "Europe", "ES": "Europe",
    "IT": "Europe", "NL": "Europe", "SE": "Europe", "PL": "Europe",
    "PT": "Europe", "NO": "Europe", "DK": "Europe", "FI": "Europe",
    "CH": "Europe", "AT": "Europe", "BE": "Europe", "IE": "Europe",
    "JP": "Asia Pacific", "KR": "Asia Pacific", "CN": "Asia Pacific",
    "IN": "Asia Pacific", "AU": "Asia Pacific", "NZ": "Asia Pacific",
    "SG": "Asia Pacific", "HK": "Asia Pacific", "TW": "Asia Pacific",
    "TH": "Asia Pacific", "VN": "Asia Pacific", "ID": "Asia Pacific",
    "PH": "Asia Pacific", "MY": "Asia Pacific", "PK": "Asia Pacific",
    "BD": "Asia Pacific", "LK": "Asia Pacific", "NP": "Asia Pacific",
    "BR": "Latin America", "AR": "Latin America", "CL": "Latin America",
    "CO": "Latin America", "PE": "Latin America",
    "ZA": "Africa", "NG": "Africa", "KE": "Africa", "EG": "Africa",
    "AE": "Middle East", "SA": "Middle East", "IL": "Middle East",
    "TR": "Middle East",
}


def _detect_text_language(text: str) -> tuple[str, float]:
    """Simple language detection from title/description tokens.

    Returns (language_code, confidence). Only distinguishes English vs
    non-English — this is a cheap signal, not a full language classifier.
    """
    if not text:
        return ("", 0.0)
    tokens = set(re.findall(r"[a-z]+", text.lower()))
    if not tokens:
        return ("", 0.0)
    en_count = len(tokens & _ENGLISH_MARKERS)
    if len(tokens) < 5:
        return ("en", 0.3) if en_count >= 2 else ("", 0.0)
    ratio = en_count / len(tokens)
    if ratio > 0.15:
        return ("en", min(ratio * 3.0, 0.9))
    return ("", 0.0)


def _has_non_latin(text: str) -> bool:
    return any(any(lo <= ord(ch) <= hi for lo, hi in _NON_LATIN_RANGES) for ch in text)


def _build_region(country_code: str | None) -> str:
    return _COUNTRY_TO_REGION.get((country_code or "").upper(), "")


def _modal_video_language(conn: Any, channel_id: str) -> str:
    """The language most of this channel's videos are actually in.

    YouTube reports defaultLanguage per video, and hydrate_metadata already
    stores it — so this is a real self-reported signal, not inference.
    Normalised to the base subtag ("en-US" -> "en") so it groups with the
    channel-level codes.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT LOWER(SPLIT_PART(language_code, '-', 1)) AS lang, COUNT(*) AS n "
            "FROM videos WHERE channel_id = %s AND language_code IS NOT NULL "
            "AND language_code <> '' "
            "GROUP BY 1 ORDER BY n DESC LIMIT 1",
            (channel_id,),
        )
        row = cur.fetchone()
        return row[0] if row else ""
    except Exception:
        conn.rollback()
        return ""
    finally:
        cur.close()


def resolve_geo_language(state: dict) -> dict:
    """Deterministic geo/language enrichment for all hydrated channels."""
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    run_id = state.get("run_id", "")

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="resolve_geo_language",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    try:
        conn = get_connection()
    except Exception:
        return {"node_logs": _log({"reason": "store unreachable", "resolved": 0})}

    try:
        cur = conn.cursor()
        # Scoped to country_source='unknown' before, which meant a channel
        # whose country YouTube self-reports was skipped entirely — so it
        # never got `region` derived (a pure deterministic function of
        # country_code that has nothing to do with how the country was
        # learned) and never got language detection either. 67 of this
        # run's 81 channels had a country and no region for exactly that
        # reason. Select anything still MISSING one of the three outputs;
        # the country-inference step below is still gated on its own
        # condition (no country_code), so a self-reported country is never
        # overwritten.
        cur.execute(
            "SELECT channel_id, title, description, country_code, country_source, primary_language_code "
            "FROM channels WHERE country_source = 'unknown' "
            "   OR region IS NULL OR primary_language_code IS NULL"
        )
        unresolved = cur.fetchall()
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "resolved": 0})}

    resolved = 0
    errors: list[dict] = []
    for row in unresolved:
        ch_id, title, desc, cc, cs, lang = row
        fields: dict = {}

        # Language: YouTube's own per-video defaultLanguage first, then
        # text detection on the channel's own copy.
        #
        # Text detection alone left 52 of 72 channels with no language at
        # all — a marker-counting heuristic simply fails on a short or
        # link-heavy channel description. Meanwhile every hydrated video
        # carries a real language_code straight from the API, so the modal
        # video language is both far more available and better evidence
        # than guessing at the description.
        if not lang:
            modal = _modal_video_language(conn, ch_id)
            if modal:
                fields["primary_language_code"] = modal
                fields["language_confidence"] = 0.9
            else:
                combined = f"{title or ''} {desc or ''}"
                detected_lang, confidence = _detect_text_language(combined)
                if detected_lang:
                    fields["primary_language_code"] = detected_lang
                    fields["language_confidence"] = round(confidence, 2)
                elif _has_non_latin(combined):
                    fields["primary_language_code"] = "non-latin"
                    fields["language_confidence"] = 0.3

        # Country: if still unknown, infer from language
        if not cc:
            lang_code = fields.get("primary_language_code") or lang
            if lang_code == "en":
                fields["country_code"] = "US"
                fields["country_source"] = "inferred_language"
                fields["country_confidence"] = 0.3
            elif lang_code == "non-latin":
                fields["country_source"] = "unknown"
                fields["country_confidence"] = 0.0

        # Region
        effective_cc = fields.get("country_code") or cc
        if effective_cc:
            region = _build_region(effective_cc)
            if region:
                fields["region"] = region

        # US market: US country OR English language with unknown country
        if effective_cc == "US" or (not cc and (fields.get("primary_language_code") or lang) == "en"):
            fields["is_us_market"] = True

        if fields:
            try:
                from src.tools.dedup import persist_channel_v3
                persist_channel_v3(conn, ch_id, run_id, fields)
                resolved += 1
            except Exception as exc:
                # A failed statement leaves the connection's transaction
                # aborted, poisoning every remaining channel in this loop
                # with InFailedSqlTransaction unless rolled back — and a
                # bare `except: continue` here previously discarded the
                # failure with no record of it at all.
                conn.rollback()
                errors.append(ErrorRecord(
                    node_name="resolve_geo_language",
                    error_type=type(exc).__name__,
                    message=f"persist failed for {ch_id}: {exc}",
                    recoverable=True,
                ).model_dump())
                continue

    put_connection(conn)
    return {
        "node_logs": _log({"resolved": resolved, "total": len(unresolved)}),
        "errors": errors,
    }
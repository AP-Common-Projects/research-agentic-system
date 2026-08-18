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

from src.config import get_config
from src.db.connection import get_connection, put_connection
from src.state import NodeLog


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
    "PH": "Asia Pacific", "MY": "Asia Pacific",
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
        cur.execute(
            "SELECT channel_id, title, description, country_code, country_source, primary_language_code "
            "FROM channels WHERE country_source = 'unknown'"
        )
        unresolved = cur.fetchall()
        cur.close()
    except Exception:
        put_connection(conn)
        return {"node_logs": _log({"reason": "query failed", "resolved": 0})}

    resolved = 0
    for row in unresolved:
        ch_id, title, desc, cc, cs, lang = row
        fields: dict = {}

        # Language: prefer YouTube self-report, then text detection
        if not lang:
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
            except Exception:
                continue

    put_connection(conn)
    return {"node_logs": _log({"resolved": resolved, "total": len(unresolved)})}
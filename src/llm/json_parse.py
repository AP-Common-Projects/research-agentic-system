"""Parsing JSON that a language model wrote.

Every node that calls a model does the same three lines: strip the reply,
regex out the first ``{...}`` or ``[...]``, and hand it to ``json.loads``.
That works until the model emits something a person would read as fine and
a strict parser will not accept -- and over enough calls it always does.

Two real failures from one Cinema run, both of which cost a channel or a
batch of video descriptions:

    Expecting property name enclosed in double quotes: line 4 column 2
        -- a trailing comma before the closing brace

    Expecting ',' delimiter: line 31 column 123
        -- an unescaped quote inside a string, e.g. a description
           containing "Star Wars" with the quotes left in

Neither is ambiguous about what was meant. Both are mechanical to repair,
and repairing them is strictly better than discarding a channel the run
already paid to classify.

What this will NOT do is guess. Every repair below is information
preserving: it changes punctuation the model got wrong, never content. If
the payload is genuinely unreadable it raises, because inventing a plausible
object would put fabricated research in a client's workbook -- the one
outcome worse than a missing row.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

__all__ = ["loads_forgiving", "JSONResponseError"]


class JSONResponseError(ValueError):
    """The reply could not be read as JSON, even after repair."""

    def __init__(self, message: str, payload: str = "") -> None:
        super().__init__(message)
        self.payload = payload


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
#: Curly quotes a model reaches for when it is "being helpful".
_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def _strip_fences(text: str) -> str:
    out = _FENCE.sub("", text.strip())
    return out.strip()


def _extract(text: str, expect: str) -> str:
    """The outermost JSON value of the wanted shape, prose either side dropped."""
    if expect == "array":
        opener, closer = "[", "]"
    elif expect == "object":
        opener, closer = "{", "}"
    else:
        first = min(
            (i for i in (text.find("{"), text.find("[")) if i != -1),
            default=-1,
        )
        if first == -1:
            return text
        opener = text[first]
        closer = "}" if opener == "{" else "]"

    start = text.find(opener)
    end = text.rfind(closer)
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start:end + 1]


def _escape_inner_quotes(text: str) -> str:
    """Escape a double quote that sits inside a JSON string value.

    A model writing a description containing a quoted title emits

        {"note": "a review of "Star Wars" and its sequels"}

    which is unreadable to a strict parser at the second quote. Walking the
    text once and asking, at every quote inside a string, "is this actually
    the end of the string?" -- by looking at what follows it -- distinguishes
    a terminator from a quote the model meant to be content.

    A real terminator is followed by whitespace and then one of ``, } ] :``
    or the end of the payload. Anything else is content, and gets escaped.
    """
    out: list[str] = []
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue

        if ch == '"':
            if not in_string:
                in_string = True
                out.append(ch)
                continue
            # Inside a string: is this the close, or content?
            rest = text[i + 1:]
            stripped = rest.lstrip()
            if stripped == "" or stripped[0] in ",}]:":
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
            continue

        out.append(ch)

    return "".join(out)


#: Repairs, cheapest and safest first. Each takes the payload and returns a
#: candidate; the caller re-parses after every step and stops at the first
#: that works, so a payload needing only a trailing-comma fix is never put
#: through quote-escaping.
_REPAIRS: list[tuple[str, Any]] = [
    ("trailing comma", lambda s: _TRAILING_COMMA.sub(r"\1", s)),
    ("smart quotes", lambda s: s.translate(_SMART_QUOTES)),
    ("smart quotes + trailing comma",
     lambda s: _TRAILING_COMMA.sub(r"\1", s.translate(_SMART_QUOTES))),
    ("unescaped inner quotes", _escape_inner_quotes),
    ("unescaped inner quotes + trailing comma",
     lambda s: _TRAILING_COMMA.sub(r"\1", _escape_inner_quotes(s))),
]


def loads_forgiving(
    content: str,
    expect: Literal["object", "array", "any"] = "any",
) -> Any:
    """Parse a model's reply, repairing what is mechanically repairable.

    Raises JSONResponseError if it cannot be read without guessing at
    content. Callers already treat that as "skip this item" -- the point of
    this function is that the case arises far less often.
    """
    if not content or not content.strip():
        raise JSONResponseError("empty response", content or "")

    candidate = _extract(_strip_fences(content), expect)

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as first_error:
        for _label, repair in _REPAIRS:
            try:
                fixed = repair(candidate)
            except Exception:
                continue
            if fixed == candidate:
                continue
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                continue

        raise JSONResponseError(
            f"unreadable JSON ({first_error})", candidate
        ) from first_error

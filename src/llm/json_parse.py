"""Parsing JSON that a language model wrote.

Every node that calls a model does the same three lines: strip the reply,
regex out the first ``{...}`` or ``[...]``, and hand it to ``json.loads``.
That works until the model emits something a person would read as fine and
a strict parser will not accept -- and over enough calls it always does.

Three real failures, from the Cinema and technology runs, each of which
cost a channel or a batch of video descriptions:

    Expecting property name enclosed in double quotes: line 4 column 2
        -- a trailing comma before the closing brace

    Expecting ',' delimiter: line 31 column 123
        -- an unescaped quote inside a string, e.g. a description
           containing "Star Wars" with the quotes left in

    Expecting ',' delimiter: line 31 column 63
        -- the closing bracket simply missing. The technology run's
           payload was captured and checked: all 30 descriptions were
           complete and well-formed, and the reply ended after the last
           one with no "]". Appending it recovered every one of them.

None is ambiguous about what was meant. All are mechanical to repair, and
repairing them is strictly better than discarding a channel the run
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

import structlog

__all__ = ["loads_forgiving", "JSONResponseError"]

logger = structlog.get_logger(__name__)

#: How much of a bad payload to put in the log. Enough to see the defect
#: without pasting a whole batch of descriptions into every run's stdout.
_LOG_EXCERPT = 600


class JSONResponseError(ValueError):
    """The reply could not be read as JSON, even after repair."""

    def __init__(self, message: str, payload: str = "") -> None:
        super().__init__(message)
        self.payload = payload


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_DANGLING_COMMA = re.compile(r",\s*$")
#: Curly DOUBLE quotes only. A curly apostrophe is never structural -- JSON
#: has no single-quoted strings -- so translating one could only ever damage
#: a description that legitimately wrote "Apple's" with a typographic quote.
_SMART_QUOTES = str.maketrans({"“": '"', "”": '"'})


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


def _close_unbalanced(text: str) -> str:
    """Close brackets the model opened and never closed.

    The technology run's failing batch was thirty complete descriptions and
    no ``]``. Adding the bracket invents nothing -- it asserts the structure
    the model was already halfway through writing, and the content it
    recovers is exactly what the model sent.

    Truncation mid-value is the harder case, and the answer there is to
    drop the incomplete tail rather than close the string around a half
    sentence. Cutting back to the last completed element loses one item;
    inventing the end of it would put a severed description in a workbook
    and look like a real one.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    #: Where the last complete element ended, and how deep we were there.
    safe: tuple[int, int] | None = None
    #: Whether the model wrote anything at all inside what it opened.
    saw_value = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                saw_value = True
            continue

        if ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append("]" if ch == "[" else "}")
        elif ch in "]}":
            if stack:
                stack.pop()
            saw_value = True
        elif ch == ",":
            safe = (i, len(stack))
        elif ch in "0123456789tfn":
            saw_value = True

    if not stack and not in_string:
        return text

    if not saw_value:
        # A bare "{" is not a truncated object, it is an empty reply with a
        # brace on it. Closing it would hand the caller {} -- which reads as
        # "the model answered, with nothing" and gets persisted as blanks.
        return text

    if in_string:
        if safe is None:
            # Nothing completed before the truncation; there is nothing to
            # salvage that would not be guesswork.
            return text
        cut, depth = safe
        return text[:cut] + "".join(reversed(stack[:depth]))

    return _DANGLING_COMMA.sub("", text) + "".join(reversed(stack))


#: Each repair is a no-op on a payload that does not have its defect, so
#: they are applied cumulatively and the result re-parsed after every step.
#: Order matters in one direction only: an unescaped inner quote desynchronises
#: the string tracking the other repairs rely on, so the pipeline is also
#: tried with that step first. Whichever order parses, wins.
_STRUCTURAL: list[tuple[str, Any]] = [
    ("closed an unbalanced bracket", _close_unbalanced),
    ("dropped a trailing comma", lambda s: _TRAILING_COMMA.sub(r"\1", s)),
    ("straightened curly quotes", lambda s: s.translate(_SMART_QUOTES)),
]
_QUOTES: tuple[str, Any] = ("escaped a quote inside a string", _escape_inner_quotes)

_PIPELINES: list[list[tuple[str, Any]]] = [
    _STRUCTURAL + [_QUOTES],
    [_QUOTES] + _STRUCTURAL,
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
    except json.JSONDecodeError as exc:
        # Rebound: Python unbinds an `as` name when the block exits, and the
        # decoder's own complaint is what every log line and message below
        # reports.
        first_error = exc

    for pipeline in _PIPELINES:
        fixed = candidate
        applied: list[str] = []
        for label, repair in pipeline:
            try:
                stepped = repair(fixed)
            except Exception:  # a repair must never be the thing that fails
                continue
            if stepped == fixed:
                continue
            fixed = stepped
            applied.append(label)
            try:
                parsed = json.loads(fixed)
            except json.JSONDecodeError:
                continue
            logger.info(
                "llm_json_repaired",
                repairs=applied,
                error=str(first_error),
                excerpt=candidate[:_LOG_EXCERPT],
            )
            return parsed

    # Logged rather than only raised: the message a node records names the
    # decoder's complaint but not the text that caused it, which is how the
    # technology run's six failures had to be reproduced to be diagnosed.
    logger.warning(
        "llm_json_unreadable",
        error=str(first_error),
        length=len(candidate),
        excerpt=candidate[:_LOG_EXCERPT],
        tail=candidate[-200:],
    )
    raise JSONResponseError(
        f"unreadable JSON ({first_error})", candidate
    ) from first_error

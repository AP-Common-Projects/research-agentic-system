"""Golden-dataset regression test for the deterministic `grade_findings` function.

Loads src/eval/fixtures/golden_synthesis_cases.jsonl and runs every case through
`grade_findings`, asserting the grade (and, when specified, individual evidence
axes) matches the expected value.

Zero LLM calls: `grade_findings` is pure Python over the structured store.

Dates in the golden file are stored as relative day offsets (`published_at_days_ago`)
plus a format tag (`published_at_format`) so the file stays diffable and time-stable
— the 90/365-day recency windows are evaluated against the test's own "now".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.nodes.synthesize import grade_findings


GOLDEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "eval"
    / "fixtures"
    / "golden_synthesis_cases.jsonl"
)


def _format_date(dt: datetime, fmt: str) -> str:
    if fmt == "iso_z":
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if fmt == "iso_no_tz":
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    if fmt == "date_only":
        return dt.strftime("%Y-%m-%d")
    if fmt == "iso_offset":
        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    raise ValueError(f"unknown published_at_format: {fmt}")


def _resolve_videos(raw_videos: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    resolved: list[dict] = []
    for raw in raw_videos:
        vid = {"channel_id": raw.get("channel_id", "")}
        if "outlier_score" in raw:
            vid["outlier_score"] = raw["outlier_score"]
        if "published_at_literal" in raw:
            vid["published_at"] = raw["published_at_literal"]
        elif "published_at_days_ago" in raw:
            fmt = raw.get("published_at_format", "iso_offset")
            days = int(raw["published_at_days_ago"])
            vid["published_at"] = _format_date(now - timedelta(days=days), fmt)
        resolved.append(vid)
    return resolved


def _load_cases() -> list[dict]:
    cases: list[dict] = []
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


CASES = _load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(case: dict):
    finding = case["finding"]
    store_data = case.get("store_data", {})
    store = {
        "videos": _resolve_videos(store_data.get("videos", [])),
        "channels": store_data.get("channels", []),
    }

    graded = grade_findings([finding], store)

    assert len(graded) == 1, f"{case['id']}: expected one graded finding"
    result = graded[0]
    expected = case["expected_grade"]
    assert result["grade"] == expected, (
        f"{case['id']}: got grade={result['grade']} "
        f"(evidence={result['evidence']}) expected {expected}"
    )

    for axis, expected_val in (case.get("expected_evidence") or {}).items():
        actual = result["evidence"].get(axis)
        assert actual == expected_val, (
            f"{case['id']}: evidence[{axis}]={actual!r} != {expected_val!r}"
        )

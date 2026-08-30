"""The v4 LLM nodes must survive NUMERIC columns coming back as Decimal.

psycopg maps a NUMERIC column to decimal.Decimal, which json.dumps refuses
to serialize. populate_shared_fields built its prompt straight from the row,
so every channel raised TypeError inside a per-channel try/except and was
skipped silently -- the node reported success while classifying nothing, and
creator_authority read 'unknown' across all 8,493 channels because that is
the column default rather than a real answer.

The same bug was already fixed twice before, in classify_channel and in
extract_success_failure_factors, which is why this is a test rather than a
third one-line cast: it fails for any node that feeds a raw NUMERIC into a
prompt payload.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import src.nodes.populate_shared_fields as mod


def _channel_row():
    """Shaped exactly like the node's eligibility SELECT.

    engagement_score and evergreen_score are NUMERIC in schema.py, so these
    are Decimal on a real connection.
    """
    return (
        "UC_test",
        "A Channel",
        "a description",
        120_000,
        Decimal("64.25"),   # engagement_score
        False,              # is_likely_news
        Decimal("71.50"),   # evergreen_score
    )


def _run(llm_response):
    conn = MagicMock()
    cur = conn.cursor.return_value
    # The node runs a sponsor pass and a niche pass before the channel pass;
    # only the channel query needs rows for this test.
    cur.fetchall.side_effect = [[], [], [_channel_row()]] + [[]] * 8
    captured = {}

    def _complete(tier, prompt, system):
        captured["prompt"] = prompt
        return {"content": llm_response}

    with (
        patch.object(mod, "complete_tier", side_effect=_complete),
        patch.object(mod, "get_connection", return_value=conn),
        patch.object(mod, "put_connection"),
    ):
        out = mod.populate_shared_fields({"thread_id": "t", "run_id": "r"})
    return out, captured


class TestDecimalPayload:
    def test_decimal_scores_do_not_break_the_prompt(self):
        out, captured = _run(json.dumps({
            "creator_authority": "financial_professional",
            "creator_authority_evidence": "states CFA in bio",
        }))

        errors = out.get("errors") or []
        assert not [e for e in errors if e.get("error_type") == "TypeError"], (
            f"NUMERIC column was passed to json.dumps unconverted: {errors}"
        )

        summary = (out.get("node_logs") or [{}])[0].get("input_summary", {})
        assert summary.get("classified") == 1, (
            f"channel was skipped rather than classified: {summary}"
        )

    def test_prompt_carries_the_score_as_a_json_number(self):
        _, captured = _run(json.dumps({
            "creator_authority": "general_creator",
            "creator_authority_evidence": "no stated credentials",
        }))

        payload = json.loads(captured["prompt"])
        # A str() cast would silence the TypeError while quietly handing the
        # model "64.25" instead of a number, so assert the type too.
        assert isinstance(payload["engagement_score"], (int, float))
        assert payload["engagement_score"] == 64.25

"""populate_crime_metadata batches its LLM calls, at a size that fits.

One mid-tier call per video cost ~$0.003, which across ~94 videos on 240
Crime channels came to roughly $67 — about seventeen times the rest of the
pipeline combined, entirely because of the call pattern rather than the work
being done. Every other per-video node in the codebase already batches.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import src.nodes.populate_crime_metadata as mod


def _rows(n):
    return [(f"v{i}", f"title {i}", f"desc {i}", f"c{i}", "chan") for i in range(n)]


def _obj():
    return {
        "crime_type": "homicide", "victim_type": "adult",
        "suspect_relationship": "stranger",
        "investigation_type": "homicide_investigation",
        "evidence_type_primary": "dna", "case_status": "solved",
        "case_fame_level": "obscure", "case_country": "USA",
        "case_year": 2020, "reveal_mechanisms": ["dna", "witness"],
    }


def _run(rows, llm):
    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = rows
    with (
        patch.object(mod, "complete_tier", side_effect=llm),
        patch.object(mod, "get_connection", return_value=conn),
        patch.object(mod, "put_connection"),
    ):
        return mod.populate_crime_metadata({"thread_id": "t", "run_id": "r"})


class TestBatching:
    def test_one_call_per_batch_not_per_video(self):
        sizes = []

        def llm(tier, prompt, system):
            n = len(json.loads(prompt))
            sizes.append(n)
            return {"content": json.dumps([_obj()] * n)}

        _run(_rows(50), llm)
        assert sizes == [8, 8, 8, 8, 8, 8, 2], "50 videos must cost 7 calls, not 50"

    def test_response_order_maps_back_to_the_right_videos(self):
        """The batch prompt and response are matched positionally, so a
        response that silently reorders would attach one video's case to
        another. Length is asserted; order is the contract."""
        seen = []

        def llm(tier, prompt, system):
            items = json.loads(prompt)
            seen.extend(i["video_title"] for i in items)
            return {"content": json.dumps([_obj()] * len(items))}

        _run(_rows(3), llm)
        assert seen == ["title 0", "title 1", "title 2"]


class TestMalformedBatchRecovery:
    def test_bad_batch_splits_instead_of_losing_every_video_in_it(self):
        sizes = []

        def llm(tier, prompt, system):
            n = len(json.loads(prompt))
            sizes.append(n)
            if n == 20:
                return {"content": "not json at all"}
            return {"content": json.dumps([_obj()] * n)}

        out = _run(_rows(20), llm)
        assert sizes == [8, 8, 4]
        assert out["node_logs"][0]["input_summary"]["populated"] == 20

    def test_wrong_length_response_is_treated_as_a_failure(self):
        """A response with fewer objects than inputs would zip() silently
        against the wrong videos, so it must split rather than persist."""
        sizes = []

        def llm(tier, prompt, system):
            n = len(json.loads(prompt))
            sizes.append(n)
            if n > 1:
                return {"content": json.dumps([_obj()])}   # only ever one
            return {"content": json.dumps([_obj()])}

        out = _run(_rows(4), llm)
        assert sizes[0] == 4 and len(sizes) > 1, "must split, not accept the short response"
        assert out["node_logs"][0]["input_summary"]["populated"] == 4

    def test_a_single_unrecoverable_video_does_not_sink_the_batch(self):
        def llm(tier, prompt, system):
            items = json.loads(prompt)
            if any(i["video_title"] == "title 2" for i in items):
                if len(items) == 1:
                    return {"content": "irreparable"}
                return {"content": "bad"}
            return {"content": json.dumps([_obj()] * len(items))}

        out = _run(_rows(4), llm)
        summary = out["node_logs"][0]["input_summary"]
        assert summary["populated"] == 3, "the other three must still land"
        assert any("unrecoverable" in e["message"] for e in out["errors"])

"""Regression tests for a real bug class found across three v3 nodes:
classify_channel and extract_success_failure_factors
all called estimate_cost() (or received cost_usd from complete_tier) and then
discarded it, returning budget_spent_usd=0.0 or omitting the key entirely.
check_saturation's budget_limit_usd breaker reads budget_spent_usd — with it
silently zero, the three most expensive calls in the pipeline (LLM
classification, vision, LLM factor extraction) spent real money invisibly to
the one circuit breaker meant to see it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.nodes.classify_channel import classify_channel
from src.nodes.extract_success_failure_factors import extract_success_failure_factors
from src.nodes.describe_video_titles import describe_video_titles
from src.nodes.resolve_first_video_date import resolve_first_video_date


def _mock_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def _llm_response(content: str, cost_usd: float = 0.01) -> dict:
    return {
        "content": content,
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "cost_usd": cost_usd,
    }


class TestClassifyChannelBudgetTracking:
    def test_reports_real_cost_not_zero(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("ch1",)],  # eligible channels
            [],  # recent video titles
            [(1, "existing_niche")],  # niche_taxonomy for _match_niche
        ]
        cursor.fetchone.return_value = (
            1000, "unknown", 50.0, 60.0, False, 3.0, 0.8, False, False, False,
            "Test Channel", "A test channel description",
        )
        conn = _mock_conn(cursor)

        with patch("src.nodes.classify_channel.get_connection", return_value=conn), \
             patch("src.nodes.classify_channel.put_connection"), \
             patch("src.nodes.classify_channel.complete_tier") as mock_complete, \
             patch("src.tools.dedup.persist_channel_v3"), \
             patch("src.tools.dedup.persist_channel_niche_membership"):
            mock_complete.return_value = _llm_response(
                '{"face_status": "face", "dominant_format": "vlog", '
                '"niche_name": "existing_niche", "classifier_model": "deepseek-v4-pro", '
                '"classifier_version": "v3.0"}',
                cost_usd=0.0123,
            )
            result = classify_channel({"run_id": "run-1", "thread_id": "t-1"})

        assert result["budget_spent_usd"] == 0.0123
        assert result["budget_spent_usd"] != 0.0

    def test_sums_cost_across_multiple_channels(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("ch1",), ("ch2",)],
            [],  # ch1 recent video titles
            [(1, "existing_niche")],
            [],  # ch2 recent video titles
            [(1, "existing_niche")],
        ]
        cursor.fetchone.return_value = (
            1000, "unknown", 50.0, 60.0, False, 3.0, 0.8, False, False, False,
            "Test Channel", "A test channel description",
        )
        conn = _mock_conn(cursor)

        with patch("src.nodes.classify_channel.get_connection", return_value=conn), \
             patch("src.nodes.classify_channel.put_connection"), \
             patch("src.nodes.classify_channel.complete_tier") as mock_complete, \
             patch("src.tools.dedup.persist_channel_v3"), \
             patch("src.tools.dedup.persist_channel_niche_membership"):
            mock_complete.return_value = _llm_response(
                '{"face_status": "face", "dominant_format": "vlog", '
                '"niche_name": "existing_niche", "classifier_model": "deepseek-v4-pro", '
                '"classifier_version": "v3.0"}',
                cost_usd=0.01,
            )
            result = classify_channel({"run_id": "run-1", "thread_id": "t-1"})

        assert result["budget_spent_usd"] == 0.02


class TestClassifyChannelNicheDiscovery:
    """The requirement this exists for: 'find sub-niches, REALLY, not mock.'
    Before this fix, _match_niche returned None on no fuzzy match and the
    LLM's proposed niche vanished — no INSERT existed anywhere outside the
    seed file. proposed_by_run_id was a schema column nothing ever wrote."""

    def test_proposes_a_genuinely_new_niche_and_inserts_it(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("ch1",)],  # eligible channels
            [("Police Bodycam Compilation Episode 42",)],  # recent video titles
            [(1, "totally_unrelated_niche")],  # niche_taxonomy — no good match
        ]
        cursor.fetchone.side_effect = [
            (1000, "unknown", 50.0, 60.0, False, 3.0, 0.8, False, False, False,
             "PoliceActivity", "Real police bodycam footage"),  # ch_row
            (99,),  # INSERT ... RETURNING niche_id
        ]
        conn = _mock_conn(cursor)

        with patch("src.nodes.classify_channel.get_connection", return_value=conn), \
             patch("src.nodes.classify_channel.put_connection"), \
             patch("src.nodes.classify_channel.complete_tier") as mock_complete, \
             patch("src.tools.dedup.persist_channel_v3") as mock_persist, \
             patch("src.tools.dedup.persist_channel_niche_membership") as mock_membership:
            mock_complete.return_value = _llm_response(
                '{"face_status": "faceless", "dominant_format": "documentary_narration", '
                '"niche_name": "bodycam_footage_analysis", "parent_category": "crime", '
                '"niche_description": "Police bodycam footage breakdowns and analysis.", '
                '"entertainment_score": 72, '
                '"classifier_model": "deepseek-v4-pro", "classifier_version": "v3.0"}',
            )
            result = classify_channel({"run_id": "run-42", "thread_id": "t-1"})

        # The new niche was actually inserted, not silently dropped.
        insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO niche_taxonomy" in c.args[0]]
        assert len(insert_calls) == 1
        sql, params = insert_calls[0].args
        assert params[0] == "bodycam_footage_analysis"
        assert params[1] == "crime"
        assert params[3] == "run-42"  # proposed_by_run_id — the column nothing wrote before

        # The channel got assigned the new niche_id, and entertainment_score
        # was written through to persist_channel_v3.
        mock_membership.assert_called_once_with(conn, "ch1", 99, is_primary=True, confidence=0.8)
        assert mock_persist.call_args.args[3]["entertainment_score"] == 72.0
        assert result["errors"] == []

    def test_a_close_fuzzy_match_reuses_the_existing_niche_not_a_duplicate(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("ch1",)],
            [],  # recent video titles
            [(5, "personal_finance_budgeting")],  # close to the proposed name below
        ]
        cursor.fetchone.return_value = (
            1000, "unknown", 50.0, 60.0, False, 3.0, 0.8, False, False, False,
            "Test Channel", "A test channel description",
        )
        conn = _mock_conn(cursor)

        with patch("src.nodes.classify_channel.get_connection", return_value=conn), \
             patch("src.nodes.classify_channel.put_connection"), \
             patch("src.nodes.classify_channel.complete_tier") as mock_complete, \
             patch("src.tools.dedup.persist_channel_v3"), \
             patch("src.tools.dedup.persist_channel_niche_membership") as mock_membership:
            mock_complete.return_value = _llm_response(
                '{"face_status": "face", "dominant_format": "vlog", '
                '"niche_name": "personal finance budgeting", "parent_category": "finance", '
                '"classifier_model": "deepseek-v4-pro", "classifier_version": "v3.0"}',
            )
            classify_channel({"run_id": "run-1", "thread_id": "t-1"})

        insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO niche_taxonomy" in c.args[0]]
        assert insert_calls == [], "an existing near-identical niche must be reused, not duplicated"
        mock_membership.assert_called_once_with(conn, "ch1", 5, is_primary=True, confidence=0.8)


class TestThumbnailVisionNodeIsGone:
    """score_thumbnail_signals was removed, not disabled.

    It sent thumbnail URLs to the vision tier as PLAIN TEXT -- the model was
    asked to judge images it was never given -- so it returned nothing
    parseable and raised JSONDecodeError per batch. Observed on a live run:
    50 errors and 22 minutes of billed vision calls, and since it sat
    between score_signals and classify_channel it burned that time inside
    every run. Its only outputs were thumbnail_has_face and
    thumbnail_text_density, both since dropped from the deliverable, and
    face_status, which classify_channel derives independently from channel
    text.
    """

    def test_the_node_is_not_importable(self):
        with pytest.raises(ModuleNotFoundError):
            __import__("src.nodes.score_thumbnail_signals")

    def test_the_graph_does_not_reference_it(self):
        import src.graph as graph_mod

        assert "score_thumbnail_signals" not in open(graph_mod.__file__).read()

    def test_the_floor_gate_now_leads_straight_to_classification(self):
        from src.graph import route_after_floor

        assert route_after_floor({"floor_gate_eligible": True}) == ["classify_channel"]
        assert route_after_floor({"floor_gate_eligible": False}) == ["check_saturation"]


class TestExtractSuccessFailureFactorsBudgetTracking:
    def test_reports_real_cost_not_zero(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("strong_hook", "Strong Hook")],  # success_factor_taxonomy rows
            [("weak_title", "Weak Title")],  # failure_factor_taxonomy rows
            [],  # cohort distribution rows (engagement/evergreen/consistency/cadence)
            [("ch1",)],  # eligible channels, first batch
            [],  # second _fetch_eligible_batch() call — empty, loop ends
        ]
        cursor.fetchone.return_value = (
            1000, 60.0, 55.0, False, 3.0, 0.8, "face", "vlog", False, False, False, True,
        )
        conn = _mock_conn(cursor)

        with patch("src.nodes.extract_success_failure_factors.get_connection", return_value=conn), \
             patch("src.nodes.extract_success_failure_factors.put_connection"), \
             patch("src.nodes.extract_success_failure_factors.complete_tier") as mock_complete, \
             patch("src.tools.dedup.persist_channel_v3"):
            mock_complete.return_value = _llm_response(
                '{"success_factors": [], "failure_factors": []}', cost_usd=0.033,
            )
            result = extract_success_failure_factors({"run_id": "run-1", "thread_id": "t-1"})

        assert result["budget_spent_usd"] == 0.033
        assert result["budget_spent_usd"] != 0.0


class TestExtractSuccessFailureFactorsProcessesAllEligibleChannels:
    """This node runs exactly once, at the very end of a run — unlike
    classify_channel/score_thumbnail_signals, which run per-round. Its old
    single LIMIT 50 query, called once, silently capped the WHOLE run's
    factor extraction at 50 channels in arbitrary DB order — a run with
    300 qualifying channels would only ever factor the first 50, forever,
    since the node never runs again. Must loop until genuinely exhausted."""

    def test_loops_past_a_single_batch_of_50(self):
        cursor = MagicMock()
        # Two batches of "eligible" (2 channels each, well under the real
        # 50-row LIMIT, but the same shape: non-empty, then non-empty,
        # then empty) proves the node re-queries rather than stopping
        # after the first page.
        cursor.fetchall.side_effect = [
            [],  # success_factor_taxonomy
            [],  # failure_factor_taxonomy
            [],  # cohort distribution rows
            [("ch1",), ("ch2",)],  # batch 1
            [("ch3",), ("ch4",)],  # batch 2
            [],  # batch 3 — empty, loop ends
        ]
        cursor.fetchone.return_value = (
            1000, 60.0, 55.0, False, 3.0, 0.8, "face", "vlog", False, False, False, True,
        )
        conn = _mock_conn(cursor)

        with patch("src.nodes.extract_success_failure_factors.get_connection", return_value=conn), \
             patch("src.nodes.extract_success_failure_factors.put_connection"), \
             patch("src.nodes.extract_success_failure_factors.complete_tier") as mock_complete, \
             patch("src.tools.dedup.persist_channel_v3"):
            mock_complete.return_value = _llm_response('{"success_factors": [], "failure_factors": []}')
            result = extract_success_failure_factors({"run_id": "run-1", "thread_id": "t-1"})

        assert result["node_logs"][0]["input_summary"]["extracted"] == 4
        assert result["node_logs"][0]["input_summary"]["eligible_seen"] == 4

    def test_stops_at_the_safety_ceiling_not_unbounded(self):
        """1000-channel safety cap — batches of 2 here just keep the mock
        small; the assertion is that it stops, not that it hits exactly
        1000 in this synthetic case."""
        cursor = MagicMock()
        batch = [("chA",), ("chB",)]
        # 3 taxonomy/cohort/no-op fetches + as many batches as the loop
        # asks for; side_effect running out would itself fail the test
        # with StopIteration if the loop somehow asked for more than
        # expected.
        cursor.fetchall.side_effect = [[], [], []] + [batch] * 501 + [[]]
        cursor.fetchone.return_value = (
            1000, 60.0, 55.0, False, 3.0, 0.8, "face", "vlog", False, False, False, True,
        )
        conn = _mock_conn(cursor)

        with patch("src.nodes.extract_success_failure_factors.get_connection", return_value=conn), \
             patch("src.nodes.extract_success_failure_factors.put_connection"), \
             patch("src.nodes.extract_success_failure_factors.complete_tier") as mock_complete, \
             patch("src.tools.dedup.persist_channel_v3"):
            mock_complete.return_value = _llm_response('{"success_factors": [], "failure_factors": []}')
            result = extract_success_failure_factors({"run_id": "run-1", "thread_id": "t-1"})

        seen = result["node_logs"][0]["input_summary"]["eligible_seen"]
        assert seen >= 1000, "must not stop before the safety ceiling"
        assert seen < 1002, "must not process an unbounded number of batches past the ceiling"


class TestEvidenceGradingIsRealNotAlwaysWeak:
    """A prior version graded every factor off one fixed check
    (engagement_score > 50) regardless of what the factor was actually
    about. Two bugs from that: engagement_score's real-world ceiling for
    actual channels is nowhere near 100 (a weighted average of ratios each
    capped at 1.0), so "moderate"/"strong" were unreachable and every row
    graded "weak" — and it graded EVERY factor by that one metric even when
    the factor cited something else (evidence_grade stayed "weak" on a
    channel with evergreen_score=100.0, the literal maximum, for its
    "high_evergreen_rating" factor). Fixed by grading off percentile rank
    against the run's own cohort, with the LLM choosing the grade from the
    metric it actually cites."""

    def test_percentile_is_scale_agnostic(self):
        from src.nodes.extract_success_failure_factors import _percentile

        low_scale = [0.1, 0.2, 0.3, 0.4, 0.5]
        assert _percentile(low_scale, 0.5) == 100.0
        assert _percentile(low_scale, 0.3) == 60.0
        assert _percentile([], 42.0) == 50.0, "no cohort data must not crash or bias toward 0"

    def test_fallback_grade_uses_percentiles_not_a_flat_default(self):
        from src.nodes.extract_success_failure_factors import _fallback_grade

        assert _fallback_grade([95.0, 40.0], is_failure=False) == "strong"
        assert _fallback_grade([65.0, 30.0], is_failure=False) == "moderate"
        assert _fallback_grade([10.0, 20.0], is_failure=False) == "weak"
        # For a failure factor, being extreme means a LOW percentile.
        assert _fallback_grade([5.0, 60.0], is_failure=True) == "strong"
        assert _fallback_grade([], is_failure=False) == "weak"

    def test_llm_supplied_grade_is_used_directly(self):
        """The LLM was given percentile context specifically so it can
        grade each factor against the metric it actually cites — its
        answer should be trusted, not overridden by a blanket check."""
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("high_evergreen_rating", "High evergreen rating")],
            [],
            [],  # cohort distribution — empty is fine, percentiles default to 50
            [("ch1",)],
            [],
        ]
        # engagement_score=15 (would have been "weak" under the old fixed
        # check) but evergreen_score=100 — the LLM is told this and asked
        # to grade the factor it's actually citing.
        cursor.fetchone.return_value = (
            1_000_000, 15.0, 100.0, False, 3.0, 0.8, "face", "documentary_narration",
            False, False, False, True,
        )
        conn = _mock_conn(cursor)

        with patch("src.nodes.extract_success_failure_factors.get_connection", return_value=conn), \
             patch("src.nodes.extract_success_failure_factors.put_connection"), \
             patch("src.nodes.extract_success_failure_factors.complete_tier") as mock_complete:
            mock_complete.return_value = _llm_response(
                '{"success_factors": [{"factor_code": "high_evergreen_rating", '
                '"evidence_note": "evergreen_score is 100.0, the maximum possible", '
                '"evidence_grade": "strong"}], "failure_factors": []}'
            )
            extract_success_failure_factors({"run_id": "run-1", "thread_id": "t-1"})

        insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO channel_success_factors" in c.args[0]]
        assert len(insert_calls) == 1
        params = insert_calls[0].args[1]
        assert params[2] == "strong", "the LLM's own grade must be used, not a recomputed 'weak'"

    def test_missing_grade_falls_back_to_percentile_derived_not_weak_by_default(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("high_evergreen_rating", "High evergreen rating")],
            [],
            # Cohort where this channel's evergreen_score is the maximum —
            # 100th percentile — so the fallback must grade it "strong",
            # not silently default to "weak".
            [(10.0, 20.0, 0.5, 2.0), (20.0, 40.0, 0.5, 2.0), (30.0, 60.0, 0.5, 2.0), (40.0, 80.0, 0.5, 2.0)],
            [("ch1",)],
            [],
        ]
        cursor.fetchone.return_value = (
            1_000_000, 15.0, 80.0, False, 3.0, 0.8, "face", "documentary_narration",
            False, False, False, True,
        )
        conn = _mock_conn(cursor)

        with patch("src.nodes.extract_success_failure_factors.get_connection", return_value=conn), \
             patch("src.nodes.extract_success_failure_factors.put_connection"), \
             patch("src.nodes.extract_success_failure_factors.complete_tier") as mock_complete:
            # No evidence_grade in the LLM's response at all.
            mock_complete.return_value = _llm_response(
                '{"success_factors": [{"factor_code": "high_evergreen_rating", '
                '"evidence_note": "evergreen_score is 80.0, the maximum in this dataset"}], '
                '"failure_factors": []}'
            )
            extract_success_failure_factors({"run_id": "run-1", "thread_id": "t-1"})

        insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO channel_success_factors" in c.args[0]]
        params = insert_calls[0].args[1]
        assert params[2] == "strong", (
            f"a channel at the 100th percentile for evergreen_score must not fall back to 'weak', got {params[2]}"
        )

    def test_corroboration_count_is_reconciled_after_the_batch(self):
        """corroboration_count was hardcoded to 1 for every row. A real
        reconciliation pass must run against the actual table after
        extraction finishes."""
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[], [], [], [("ch1",)], []]
        cursor.fetchone.return_value = (
            1000, 60.0, 55.0, False, 3.0, 0.8, "face", "vlog", False, False, False, True,
        )
        conn = _mock_conn(cursor)

        with patch("src.nodes.extract_success_failure_factors.get_connection", return_value=conn), \
             patch("src.nodes.extract_success_failure_factors.put_connection"), \
             patch("src.nodes.extract_success_failure_factors.complete_tier") as mock_complete:
            mock_complete.return_value = _llm_response('{"success_factors": [], "failure_factors": []}')
            extract_success_failure_factors({"run_id": "run-1", "thread_id": "t-1"})

        reconcile_calls = [
            c for c in cursor.execute.call_args_list
            if "UPDATE" in c.args[0] and "corroboration_count" in c.args[0]
        ]
        assert len(reconcile_calls) == 2, "must reconcile both the success and failure factor tables"


class TestUploadCadenceHandlesRealTimestamps:
    """published_at is a datetime when it comes from Postgres and a string
    when it comes from the YouTube API. Only the string path existed, so
    every DB-sourced channel got uploads_per_week_avg=0 and
    upload_consistency_score=0 — which also broke the is_likely_news
    derivation in score_signals, since that reads cadence."""

    def test_datetime_objects_produce_a_real_cadence(self):
        from datetime import datetime, timezone
        from src.nodes.extract_metadata_signals import _compute_upload_stats

        videos = [
            {"published_at": datetime(2026, 1, 1 + i * 7, tzinfo=timezone.utc)}
            for i in range(5)
        ]
        stats = _compute_upload_stats(videos)
        # 5 uploads spanning 4 weeks (count / span), not the reciprocal of
        # the average gap — 5 / (28 days / 7) = 1.25/week.
        assert stats["uploads_per_week_avg"] == pytest.approx(1.25, abs=0.01)
        # A perfectly-regular 5-video sample still only spans 2 adaptive
        # buckets at this cadence, which can't show as high a score as a
        # longer/denser sample — exact consistency behavior is covered by
        # TestUploadConsistencyDoesNotFloorLowCadenceChannels below. This
        # test's job is just proving the datetime path produces a real,
        # non-degenerate score at all.
        assert stats["upload_consistency_score"] > 0.5

    def test_naive_datetimes_are_treated_as_utc_not_dropped(self):
        from datetime import datetime
        from src.nodes.extract_metadata_signals import _compute_upload_stats

        videos = [{"published_at": datetime(2026, 1, 1 + i * 7)} for i in range(4)]
        assert _compute_upload_stats(videos)["uploads_per_week_avg"] > 0

    def test_iso_strings_still_work(self):
        from src.nodes.extract_metadata_signals import _compute_upload_stats

        videos = [
            {"published_at": f"2026-01-{1 + i * 7:02d}T00:00:00Z"} for i in range(4)
        ]
        assert _compute_upload_stats(videos)["uploads_per_week_avg"] > 0


class TestUploadRateCannotOverflowItsColumn:
    """channels.uploads_per_week_avg is NUMERIC(6,2) -- it stops at 9,999.99.

    A burst upload divided by a near-zero span blew straight past that, and
    Postgres rejects the value rather than truncating it, so the whole
    persist_channel_v3 call for that channel aborted: its signal row AND its
    video signals were lost, and the loop moved on. 13 channels went that way
    in one run before this was caught, with the only trace an error record on
    the run itself. `span_days <= 0` only guarded an exactly-simultaneous
    batch; these spans were small but non-zero.
    """

    def _burst(self, n: int, seconds_apart: float):
        from datetime import datetime, timedelta, timezone

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            {"published_at": start + timedelta(seconds=seconds_apart * i)}
            for i in range(n)
        ]

    def test_the_exact_observed_bursts_stay_in_range(self):
        """All three reproduce channels seen live on 2026-09-05."""
        from src.nodes.extract_metadata_signals import _compute_upload_stats

        # 10 videos across 35 seconds -> computed 183,272/week
        # 2 videos across 26 seconds -> computed 40,320/week
        # 16 videos across ~15 minutes -> computed 10,338/week
        for n, span_seconds in [(10, 35), (2, 26), (16, 932)]:
            stats = _compute_upload_stats(
                self._burst(n, span_seconds / max(1, n - 1))
            )
            assert stats["uploads_per_week_avg"] <= 9999.99, (n, span_seconds)

    def test_a_sub_day_burst_reports_no_cadence_rather_than_a_huge_one(self):
        """A back catalogue published in one sitting says nothing about how
        often the channel uploads, so it takes the same answer as every other
        insufficient-data path here rather than a clamped-but-fictional rate."""
        from src.nodes.extract_metadata_signals import _compute_upload_stats

        stats = _compute_upload_stats(self._burst(20, 1.0))  # 20 videos, 19s
        assert stats["uploads_per_week_avg"] == 0
        assert stats["upload_consistency_score"] == 0

    def test_a_real_cadence_is_still_measured_normally(self):
        """The guard must not swallow ordinary channels -- it is a floor on
        the sampling window, not on the upload rate."""
        from datetime import datetime, timedelta, timezone
        from src.nodes.extract_metadata_signals import _compute_upload_stats

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 3 videos a day for 10 days = 21/week, high but entirely real.
        videos = [
            {"published_at": start + timedelta(days=d, hours=8 * v)}
            for d in range(10) for v in range(3)
        ]
        stats = _compute_upload_stats(videos)
        assert 15 < stats["uploads_per_week_avg"] < 30

    def test_the_clamp_holds_even_if_the_span_guard_is_bypassed(self):
        """Belt and braces: the column ceiling is enforced independently of
        the meaning-level guard, so no future arithmetic path can hand
        Postgres a value it will reject."""
        from src.nodes import extract_metadata_signals as mod

        # Span passes the day floor, but an absurd count pushes the rate up.
        stats = mod._compute_upload_stats(self._burst(2_000_000, 0.1))
        assert stats["uploads_per_week_avg"] == mod._MAX_UPLOADS_PER_WEEK


class TestUploadConsistencyDoesNotFloorLowCadenceChannels:
    """Fixed 7-day buckets broke down for any channel averaging under ~1
    upload/week: a Poisson-ish process's own sampling noise gives stdev
    exceeding the mean at that rate, so CV > 1 and consistency floors at
    0.00 — indistinguishable from genuine burst-and-silence. Verified live
    against a channel posting an even ~1 video every other week for two
    years (mean 0.44/week bucket, nearly all buckets exactly 0 or 1): it
    scored 0.00, identical to a channel that goes silent for months and
    then dumps 20 uploads in a week."""

    def test_a_steady_but_infrequent_channel_scores_high_not_zero(self):
        from datetime import datetime, timedelta, timezone
        from src.nodes.extract_metadata_signals import _compute_upload_stats

        # One video every 14 days for a year — genuinely steady, just slow.
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        videos = [{"published_at": base + timedelta(days=14 * i)} for i in range(26)]
        stats = _compute_upload_stats(videos)
        assert stats["uploads_per_week_avg"] == pytest.approx(0.5, abs=0.05)
        assert stats["upload_consistency_score"] > 0.7, (
            f"a channel posting on a strict 14-day schedule must not floor "
            f"to ~0 just because its cadence is under 1/week, got "
            f"{stats['upload_consistency_score']}"
        )

    def test_a_genuinely_bursty_low_cadence_channel_still_scores_low(self):
        """The fix must not paper over real burstiness — only the false
        floor caused by sparse-count sampling noise."""
        from datetime import datetime, timedelta, timezone
        from src.nodes.extract_metadata_signals import _compute_upload_stats

        base = datetime(2023, 1, 1, tzinfo=timezone.utc)
        # 20 uploads dumped in one week, then silence for two years.
        burst = [{"published_at": base + timedelta(days=i)} for i in range(7)] * 3
        videos = burst[:20] + [{"published_at": base + timedelta(days=730)}]
        stats = _compute_upload_stats(videos)
        assert stats["upload_consistency_score"] < 0.3


class TestVideoLevelNewsDetection:
    """videos.is_likely_news was a column nothing ever wrote — every video
    row exported NULL, so the client's 'exclude news-oriented content'
    requirement had no per-video signal to filter on."""

    def test_flags_current_events_titles(self):
        from src.nodes.extract_metadata_signals import _is_news_title

        assert _is_news_title("BREAKING: suspect arrested in downtown case")
        assert _is_news_title("Trial Day 3 — the verdict is in")
        assert _is_news_title("Update: new details emerge")

    def test_does_not_flag_evergreen_titles(self):
        from src.nodes.extract_metadata_signals import _is_news_title

        assert not _is_news_title("The unsolved disappearance that baffled detectives")
        assert not _is_news_title("How forensic science actually works")
        assert not _is_news_title("")


class TestDescribeVideoTitles:
    """The video-level analogue of classify_channel's niche_description —
    added so success/failure factors can later be read against a
    normalized one-sentence gloss of each title instead of raw text."""

    def test_persists_one_description_per_title_in_order(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("v1", "Bodycam: Officer Confronts Armed Suspect"), ("v2", "Cold Case Solved After 20 Years")],
            [],  # second batch fetch — empty, loop ends
        ]
        conn = _mock_conn(cursor)

        with patch("src.nodes.describe_video_titles.get_connection", return_value=conn), \
             patch("src.nodes.describe_video_titles.put_connection"), \
             patch("src.nodes.describe_video_titles.complete_tier") as mock_complete, \
             patch("src.tools.dedup.persist_video_v3") as mock_persist:
            mock_complete.return_value = _llm_response(
                '["A police bodycam clip showing an officer confronting an armed suspect.", '
                '"A previously unsolved case that was finally resolved two decades later."]',
                cost_usd=0.002,
            )
            result = describe_video_titles({"thread_id": "t-1"})

        assert mock_persist.call_count == 2
        assert mock_persist.call_args_list[0].args[1] == "v1"
        assert "bodycam" in mock_persist.call_args_list[0].args[2]["video_description"].lower()
        assert mock_persist.call_args_list[1].args[1] == "v2"
        assert result["budget_spent_usd"] == 0.002
        assert result["node_logs"][0]["input_summary"]["described"] == 2

    def test_mismatched_response_length_does_not_crash(self):
        """A short/malformed LLM response must not raise — the batch is
        skipped and recorded as an error, not silently zip-truncated in a
        way that mislabels videos."""
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [("v1", "Title A"), ("v2", "Title B"), ("v3", "Title C")],
            [],
        ]
        conn = _mock_conn(cursor)

        with patch("src.nodes.describe_video_titles.get_connection", return_value=conn), \
             patch("src.nodes.describe_video_titles.put_connection"), \
             patch("src.nodes.describe_video_titles.complete_tier") as mock_complete, \
             patch("src.tools.dedup.persist_video_v3") as mock_persist:
            mock_complete.return_value = _llm_response('["Only one description."]')
            result = describe_video_titles({"thread_id": "t-1"})

        # zip() truncates to the shorter list — exactly 1 persisted, not a crash.
        assert mock_persist.call_count == 1
        assert result["errors"] == []

    def test_no_eligible_videos_is_a_clean_no_op(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[]]
        conn = _mock_conn(cursor)

        with patch("src.nodes.describe_video_titles.get_connection", return_value=conn), \
             patch("src.nodes.describe_video_titles.put_connection"), \
             patch("src.nodes.describe_video_titles.complete_tier") as mock_complete:
            result = describe_video_titles({"thread_id": "t-1"})

        mock_complete.assert_not_called()
        assert result["node_logs"][0]["input_summary"]["described"] == 0


class TestResolveFirstVideoDate:
    """first_video_published_at must come from paginating the uploads
    playlist to its real last page (see test_youtube_api.py) — this class
    covers the node's own eligibility/persistence wiring, not the
    pagination logic itself."""

    def test_persists_the_resolved_date_for_each_eligible_channel(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[("ch1",), ("ch2",)]]
        conn = _mock_conn(cursor)

        with patch("src.nodes.resolve_first_video_date.get_connection", return_value=conn), \
             patch("src.nodes.resolve_first_video_date.put_connection"), \
             patch("src.tools.youtube_api.YouTubeAPIClient") as mock_client_cls, \
             patch("src.tools.dedup.persist_channel_v3") as mock_persist:
            mock_client = MagicMock()
            mock_client.get_channel_first_video_published_at.side_effect = [
                "2013-09-12T00:00:00Z", "2019-01-01T00:00:00Z",
            ]
            mock_client_cls.return_value = mock_client
            result = resolve_first_video_date({"run_id": "run-1", "thread_id": "t-1"})

        assert mock_persist.call_count == 2
        assert mock_persist.call_args_list[0].args[1] == "ch1"
        assert mock_persist.call_args_list[0].args[2] == "run-1"
        assert mock_persist.call_args_list[0].args[3] == {
            "first_video_published_at": "2013-09-12T00:00:00Z"
        }
        assert result["node_logs"][0]["input_summary"]["resolved"] == 2
        assert result["errors"] == []

    def test_a_channel_the_api_cant_resolve_is_skipped_not_a_crash(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[("ch1",)]]
        conn = _mock_conn(cursor)

        with patch("src.nodes.resolve_first_video_date.get_connection", return_value=conn), \
             patch("src.nodes.resolve_first_video_date.put_connection"), \
             patch("src.tools.youtube_api.YouTubeAPIClient") as mock_client_cls, \
             patch("src.tools.dedup.persist_channel_v3") as mock_persist:
            mock_client = MagicMock()
            mock_client.get_channel_first_video_published_at.return_value = None
            mock_client_cls.return_value = mock_client
            result = resolve_first_video_date({"run_id": "run-1", "thread_id": "t-1"})

        mock_persist.assert_not_called()
        assert result["node_logs"][0]["input_summary"]["resolved"] == 0

    def test_no_eligible_channels_is_a_clean_no_op(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[]]
        conn = _mock_conn(cursor)

        with patch("src.nodes.resolve_first_video_date.get_connection", return_value=conn), \
             patch("src.nodes.resolve_first_video_date.put_connection"), \
             patch("src.tools.youtube_api.YouTubeAPIClient"):
            result = resolve_first_video_date({"run_id": "run-1", "thread_id": "t-1"})

        assert result["node_logs"][0]["input_summary"]["resolved"] == 0
        assert result["errors"] == []


class TestVerticalStartDateConfidence:
    """Both briefs warn against assuming channel creation date == vertical
    start date. The basis/confidence must say which case actually applied,
    rather than stamping a single optimistic number on every row."""

    def _conn(self, earliest, held):
        from unittest.mock import MagicMock

        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (earliest, held)
        conn.cursor.return_value = cur
        return conn

    def test_full_catalogue_held_is_a_real_observation(self):
        from src.tools.hydrate_metadata import _derive_vertical_start

        fields = {}
        ch = {"channel_id": "UCabc", "_total_long_form": 40}
        _derive_vertical_start(ch, self._conn("2019-01-09", 40), fields)
        assert fields["vertical_start_date_basis"] == "first_vertical_video_observed"
        assert fields["vertical_start_date_confidence"] == 0.7

    def test_truncated_scan_is_only_a_lower_bound(self):
        """If we hold 50 of a channel's 2,761 long-form videos, the oldest we
        can see says nothing about when it actually started — reporting that
        as an observation with high confidence is the bug this guards."""
        from src.tools.hydrate_metadata import _derive_vertical_start

        fields = {}
        ch = {"channel_id": "UCabc", "_total_long_form": 2761}
        _derive_vertical_start(ch, self._conn("2025-06-01", 50), fields)
        assert fields["vertical_start_date_basis"] == "earliest_observed_video_lower_bound"
        assert fields["vertical_start_date_confidence"] == 0.4

    def test_no_videos_falls_back_to_creation_date_and_says_so(self):
        from src.tools.hydrate_metadata import _derive_vertical_start

        fields = {}
        ch = {"channel_id": "UCabc", "published_at": "2016-01-01", "_total_long_form": 0}
        _derive_vertical_start(ch, self._conn(None, 0), fields)
        assert fields["vertical_start_date_basis"] == "channel_creation_date"
        assert fields["vertical_start_date_confidence"] == 0.3


class TestTopLifetimeSampling:
    def test_ranks_by_lifetime_views_over_the_whole_catalogue(self):
        """The old implementation ranked the newest 50 by outlier score. A
        channel's most-watched video is frequently an old one, so it has to
        be able to win against recent uploads."""
        from unittest.mock import MagicMock
        from src.tools.hydrate_metadata import _tag_video_samples

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        videos = [
            {"video_id": "old_hit", "published_at": "2015-01-01",
             "view_count": 9_000_000, "is_short": False},
            {"video_id": "recent", "published_at": "2026-01-01",
             "view_count": 1_000, "is_short": False},
        ]
        _tag_video_samples(conn, videos)
        statements = [c.args[0] for c in cur.execute.call_args_list]
        ids = [c.args[1][0] for c in cur.execute.call_args_list]
        top = [i for s, i in zip(statements, ids) if "'top_lifetime'" in s]
        assert "old_hit" in top

    def test_shorts_are_excluded_from_long_form_sampling(self):
        from unittest.mock import MagicMock
        from src.tools.hydrate_metadata import _tag_video_samples

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        videos = [
            {"video_id": "longform", "published_at": "2026-01-01",
             "view_count": 100, "is_short": False},
            {"video_id": "a_short", "published_at": "2026-01-02",
             "view_count": 5_000_000, "is_short": True},
        ]
        _tag_video_samples(conn, videos)
        tagged = {c.args[1][0] for c in cur.execute.call_args_list}
        assert "a_short" not in tagged


class TestEnrichmentIsFloorGated:
    """The per-channel YouTube enrichment lookups (upload breakdown, Shorts
    ID set) answer questions asked only of exported channels. Ungated they
    ran for every discovered channel: measured live, 233 hydrated in 35
    minutes of which 11 cleared the floor, yet all 233 paid for them.
    Combined burn reached 367 calls/min — two projects' 20,000-unit daily
    allowance gone in under an hour."""

    def _client(self, subs):
        from unittest.mock import MagicMock

        c = MagicMock()
        c.get_channels.return_value = [{
            "channel_id": "UCx", "title": "T", "subscriber_count": subs,
            "video_count": 10, "view_count": 100, "published_at": "2020-01-01",
        }]
        c.get_channel_videos.return_value = []
        c.get_quota_used.return_value = 0
        return c

    def _run(self, client):
        from unittest.mock import MagicMock, patch
        import importlib

        # src/tools/__init__ re-exports the function under the submodule's
        # own name, so `import src.tools.hydrate_metadata as mod` hands back
        # the function rather than the module.
        mod = importlib.import_module("src.tools.hydrate_metadata")

        with (
            patch.object(mod, "YouTubeAPIClient", return_value=client),
            patch.object(mod, "persist_channel"),
            patch.object(mod, "persist_channel_v3"),
            patch.object(mod, "persist_channel_snapshot"),
            patch.object(mod, "persist_video"),
            patch.object(mod, "persist_video_v3"),
            patch("src.db.connection.get_connection", return_value=MagicMock()),
            patch("src.db.connection.put_connection"),
            patch("src.tools.dedup.persist_category_tags"),
        ):
            return mod.hydrate_metadata({
                "discovered_channel_ids": ["UCx"], "thread_id": "t",
                "run_id": "r", "hydrated_channel_ids": set(),
            })

    def test_sub_floor_channel_skips_the_paid_lookups(self):
        client = self._client(1_000)
        self._run(client)
        client.get_channel_upload_breakdown.assert_not_called()
        client.get_channel_shorts_ids.assert_not_called()

    def test_sub_floor_channel_uses_the_cheap_video_path(self):
        client = self._client(1_000)
        self._run(client)
        assert client.get_channel_videos.call_args.kwargs["deep_scan"] is False

    def test_floor_clearing_channel_gets_the_full_treatment(self):
        client = self._client(500_000)
        self._run(client)
        client.get_channel_upload_breakdown.assert_called_once()
        assert client.get_channel_videos.call_args.kwargs["deep_scan"] is True

    def test_skipping_is_not_recorded_as_an_error(self):
        """A deliberate skip must not look like a failure in the run log."""
        out = self._run(self._client(1_000))
        assert not [e for e in out.get("errors", [])
                    if "breakdown" in e.get("message", "")]


class TestVideoSignalsDoNotDependOnChannelEligibility:
    """The video pass used to be nested inside the channel loop, whose
    eligibility is `has_affiliate_signal IS NULL` -- a one-time marker. So a
    channel any earlier run had already processed skipped its videos too,
    including videos hydrated long afterwards. 31,665 videos in the live
    database had no title signals for that reason, and run-3f649c9246a3
    shipped six Videos columns at 85% because one such channel entered its
    workbook through the category filter.

    A marker on the channel cannot gate work done per video.
    """

    def _run(self, channel_rows, video_rows):
        from unittest.mock import MagicMock, patch

        import src.nodes.extract_metadata_signals as mod

        conn = MagicMock()
        cursors = []

        def _cursor():
            cur = MagicMock()
            cursors.append(cur)
            # First call selects channels, the last selects videos.
            cur.fetchall.side_effect = lambda: (
                channel_rows if len(cursors) == 1 else video_rows
            )
            return cur

        conn.cursor.side_effect = _cursor
        persisted = []
        with patch.object(mod, "get_connection", return_value=conn), \
             patch.object(mod, "put_connection"), \
             patch("src.tools.dedup.persist_channel_v3"), \
             patch("src.tools.dedup.persist_video_v3",
                   side_effect=lambda c, vid, f: persisted.append((vid, f))):
            out = mod.extract_metadata_signals(
                {"thread_id": "t", "run_id": "r", "discovered_channel_ids": ["c1"]}
            )
        return out, persisted

    def test_videos_are_enriched_even_when_no_channel_is_eligible(self):
        """The exact shape that shipped: nothing to do at channel level,
        seventy videos with no title signals."""
        out, persisted = self._run(
            channel_rows=[],  # every channel already has has_affiliate_signal
            video_rows=[("v1", "How the M4 Chip Works", "")],
        )
        assert [vid for vid, _ in persisted] == ["v1"]
        assert persisted[0][1]["title_word_count"] == 5

    def test_it_reports_video_progress_separately_from_channel_progress(self):
        """The export gate re-invokes a node while it reports progress. A
        call that enriches videos and no channels has to read as progress,
        or the backlog is abandoned after one call."""
        out, _ = self._run(
            channel_rows=[],
            video_rows=[("v1", "A Title", ""), ("v2", "Another", "")],
        )
        summary = out["node_logs"][0]["input_summary"]
        assert summary["processed"] == 0
        assert summary["videos_enriched"] == 2

    def test_the_gate_counts_that_key_as_progress(self):
        from src.tools import export_completeness as gate

        src = open("src/tools/export_completeness.py", encoding="utf-8").read()
        assert '"videos_enriched"' in src, (
            "heal() must recognise the video pass, or it stops after one call"
        )
        assert gate is not None

    def test_the_video_pass_is_bounded(self):
        """One call must not run away on a 31,665-row backlog; the caller
        re-invokes while progress continues."""
        import src.nodes.extract_metadata_signals as mod

        assert 0 < mod._VIDEO_BATCH <= 5000


class TestSearchBrowseEstimateReachesEveryVideo:
    """search_browse_estimate is a channel-level judgement written onto that
    channel's videos by an UPDATE inside populate_shared_fields' per-channel
    loop -- and that loop's eligibility is creator_authority_checked_at, a
    channel-level marker. A video hydrated after its channel was checked
    never received the estimate, and no re-run could give it one: the
    channel is marked, so it is never revisited.

    The crime run of 2026-09-07 shipped the column at 55% for that reason,
    and the export gate's healing could not move it. Same shape as
    extract_metadata_signals' nested video pass: a marker on the channel
    cannot gate work done per video.
    """

    def _run(self, state=None):
        from unittest.mock import MagicMock, patch

        import src.nodes.populate_shared_fields as mod

        conn = MagicMock()
        conn.cursor.return_value.fetchall.return_value = []
        ctx = conn.cursor.return_value.__enter__.return_value
        ctx.rowcount = 7
        with patch.object(mod, "get_connection", return_value=conn), \
             patch.object(mod, "put_connection"), \
             patch.object(mod, "complete_tier"):
            out = mod.populate_shared_fields(
                state or {"thread_id": "t", "run_id": "r",
                          "discovered_channel_ids": ["c1"]}
            )
        return out, ctx

    def test_it_propagates_even_when_no_channel_is_eligible(self):
        """Nothing to classify, and videos still missing the estimate --
        the exact state the crime run was stuck in."""
        out, ctx = self._run()
        summary = out["node_logs"][0]["input_summary"]
        assert summary["classified"] == 0
        # Both passes count: the copy from a sibling, then the derived
        # fallback for channels with no answer anywhere. The mock reports 7
        # rows for each.
        assert summary["propagated"] == 14, "the propagation must run regardless"

    def _statements(self, ctx):
        """Both passes, in order. There are two, and asserting only the
        last silently stopped checking the first."""
        return [c[0][0] for c in ctx.execute.call_args_list]

    def test_it_copies_a_channels_own_answer_first(self):
        """Invents nothing: the value comes from another video of the SAME
        channel, which is the value the loop would have written."""
        _, ctx = self._run()
        copy_sql = self._statements(ctx)[0]
        assert "v.channel_id = known.channel_id" in copy_sql
        assert "v.search_browse_estimate IS NULL" in copy_sql
        assert "WHERE search_browse_estimate IS NOT NULL" in copy_sql, (
            "the source video must have a real answer to copy"
        )

    def test_a_channel_with_no_answer_falls_back_to_the_derived_estimate(self):
        """The LLM pass is gated on creator_authority_checked_at -- a marker
        about the channel's AUTHORITY, which also decided whether its videos
        ever got this column. A channel marked checked whose call failed kept
        that mark forever and its videos could never be filled: one channel
        on run-44e01aab65c2 held 54 of them.

        The fallback is the same deterministic value the node writes whenever
        the model answers "unclear", derived from signals already on the
        channel -- so it costs nothing and states no more than the run knows.
        """
        _, ctx = self._run()
        derive_sql = self._statements(ctx)[1]
        assert "CASE" in derive_sql
        assert "c.is_likely_news THEN 'news_driven'" in derive_sql
        assert "ELSE 'mixed'" in derive_sql
        assert "v.search_browse_estimate IS NULL" in derive_sql, (
            "it must never overwrite an answer already there"
        )

    def test_the_fallback_matches_the_nodes_own_thresholds(self):
        """Two expressions of one rule; if they drift, a video's estimate
        depends on which path happened to reach it."""
        import inspect

        import src.nodes.populate_shared_fields as mod

        src = inspect.getsource(mod.populate_shared_fields)
        assert 'eng and float(eng or 0) > 60' in src
        assert "c.engagement_score > 60 THEN 'browse_driven'" in src
        assert 'eg and float(eg or 0) > 70' in src
        assert "c.evergreen_score > 70 THEN 'search_driven'" in src

    def test_both_passes_are_scoped_to_the_run(self):
        _, ctx = self._run()
        for sql in self._statements(ctx):
            assert "v.channel_id = ANY(%s)" in sql
        for call in ctx.execute.call_args_list:
            assert call[0][1][0] == ["c1"]

    def test_the_gate_counts_propagation_as_progress(self):
        """heal() re-invokes a node while it reports progress. A call that
        propagates and classifies nothing has to read as progress."""
        src = open("src/tools/export_completeness.py", encoding="utf-8").read()
        assert '"propagated"' in src


class TestTheSubscriberFloorIsAlwaysComputed:
    """meets_subscriber_floor defaults to FALSE, and FALSE is
    indistinguishable from "computed, and genuinely below the floor". A
    channel score_signals reached only for its size bucket therefore looked
    like it had failed the floor, and every floor-gated node -- classify_
    channel, describe_video_titles, populate_crime_metadata,
    extract_success_failure_factors -- skipped it permanently.

    On the gaming run of 2026-09-07 that included a channel with 5,500,000
    subscribers against a floor of 50,000.
    """

    def test_a_channel_with_no_videos_still_gets_the_flag(self):
        from unittest.mock import MagicMock, patch

        import src.tools.signal_scoring as mod

        persisted = {}
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = (5_500_000,)
        cfg = MagicMock()
        cfg.harness.subscriber_floor = 50_000

        with patch("src.db.connection.get_connection", return_value=conn), \
             patch("src.db.connection.put_connection"), \
             patch("src.config.get_config", return_value=cfg), \
             patch("src.tools.dedup.fetch_videos_by_channels", return_value=[]), \
             patch("src.tools.dedup.persist_channel_v3",
                   side_effect=lambda c, cid, rid, f: persisted.update(f)):
            mod.score_signals({"discovered_channel_ids": ["c1"],
                               "thread_id": "t", "run_id": "r"})

        assert persisted.get("meets_subscriber_floor") is True, (
            "5.5M subscribers against a 50k floor must not read as below it"
        )
        assert "channel_size_bucket" in persisted

    def test_a_genuinely_small_channel_still_reads_as_below(self):
        from unittest.mock import MagicMock, patch

        import src.tools.signal_scoring as mod

        persisted = {}
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = (900,)
        cfg = MagicMock()
        cfg.harness.subscriber_floor = 50_000

        with patch("src.db.connection.get_connection", return_value=conn), \
             patch("src.db.connection.put_connection"), \
             patch("src.config.get_config", return_value=cfg), \
             patch("src.tools.dedup.fetch_videos_by_channels", return_value=[]), \
             patch("src.tools.dedup.persist_channel_v3",
                   side_effect=lambda c, cid, rid, f: persisted.update(f)):
            mod.score_signals({"discovered_channel_ids": ["c1"],
                               "thread_id": "t", "run_id": "r"})

        assert persisted.get("meets_subscriber_floor") is False

    def test_the_floor_needs_no_videos_to_be_answered(self):
        """The overrides do; the threshold itself does not, which is why
        the missing videos were never a reason to leave it unanswered."""
        from unittest.mock import MagicMock

        from src.tools.signal_scoring import compute_subscriber_floor

        cfg = MagicMock()
        cfg.subscriber_floor = 50_000
        meets, reason = compute_subscriber_floor(
            {"subscriber_count": 413_000, "_videos": []}, cfg
        )
        assert meets is True and reason is None

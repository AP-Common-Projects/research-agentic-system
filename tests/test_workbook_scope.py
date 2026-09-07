"""Which rows a workbook contains, and the guarantee that it has some.

A crime run on 2026-09-07 ran 2h34m and delivered a workbook with zero
rows. Nothing failed: the CSV export beside it wrote 14 channels and 500
videos, and the completeness gate printed EMPTY: channels and let the
export proceed.

Two guards, each defensible alone, composed into it:

  dominant_run_category counted only channels the run was the FIRST to
  see. A run that mostly re-finds known channels has almost none, so the
  category came back None.

  own_only = category is None. With no category to keep other verticals
  out, the workbook narrowed to this run's brand-new channels -- 7 of
  them, all under the 50k floor.

Neither guard was wrong about the case it was written for. The failure
was that no one asked what happens when both fire, and nothing anywhere
asserted the obvious: a run that found something must not produce a file
containing nothing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src import export as ex


class TestTheCategoryIsReadFromWhatTheRunTagged:
    def test_it_counts_tagged_channels_not_only_first_discovered(self):
        """The crime run had 0 classified first-discovered channels and 12
        classified crime channels in its tagged set."""
        captured = {}

        def _fetch(sql, params):
            captured["sql"] = sql
            return [{"parent_category": "crime", "n": 12}]

        with patch.object(ex, "_fetch", _fetch), patch.object(ex, "_floor", lambda: 50000):
            assert ex.dominant_run_category("run-x") == "crime"

        assert "category_tags" in captured["sql"], (
            "must read the run's tagged channels, not first_discovered_run_id"
        )
        assert "first_discovered_run_id" not in captured["sql"]

    def test_a_thin_signal_is_still_refused(self):
        """The guard that stopped an automotive run being labelled crime."""
        with patch.object(ex, "_fetch", lambda *a: [{"parent_category": "crime", "n": 1}]), \
             patch.object(ex, "_floor", lambda: 50000):
            assert ex.dominant_run_category("run-x") is None

    def test_a_tie_is_still_refused(self):
        """Four strangers, one each, is not a category -- it is noise."""
        rows = [
            {"parent_category": "crime", "n": 7},
            {"parent_category": "finance", "n": 7},
        ]
        with patch.object(ex, "_fetch", lambda *a: rows), \
             patch.object(ex, "_floor", lambda: 50000):
            assert ex.dominant_run_category("run-x") is None

    def test_a_clear_winner_over_stragglers_is_accepted(self):
        """The automotive run reads automotive=44 against finance=4 on its
        tagged set -- the reading the old query could not see."""
        rows = [
            {"parent_category": "automotive", "n": 44},
            {"parent_category": "finance", "n": 4},
        ]
        with patch.object(ex, "_fetch", lambda *a: rows), \
             patch.object(ex, "_floor", lambda: 50000):
            assert ex.dominant_run_category("run-x") == "automotive"


class TestAScopeNeverSelectsNothing:
    def _scope(self, category, own_rows, wide_rows):
        """own_only returns own_rows, own_only=False returns wide_rows."""
        def _channels(run_id, cat=None, min_subs=None, own_only=False):
            return own_rows if own_only else wide_rows

        with patch.object(ex, "dominant_run_category", return_value=category), \
             patch.object(ex, "fetch_run_channels", side_effect=_channels):
            return ex.workbook_scope("run-x")

    def test_own_only_widens_when_it_would_empty_the_workbook(self):
        """The exact shape that shipped: no category, and no first-discovered
        channel over the floor, while the run's real rows sit in its tagged
        set."""
        scope = self._scope(category=None, own_rows=[], wide_rows=[{"channel_id": "c1"}])
        assert scope.own_only is False, "an empty workbook is never the right answer"

    def test_it_does_not_widen_when_own_only_has_rows(self):
        """The guard exists for a reason -- it must keep working whenever it
        is not about to empty the file."""
        scope = self._scope(
            category=None,
            own_rows=[{"channel_id": "mine"}],
            wide_rows=[{"channel_id": "mine"}, {"channel_id": "stranger"}],
        )
        assert scope.own_only is True

    def test_a_run_that_genuinely_found_nothing_stays_empty(self):
        """Widening must not invent rows from other runs for a run that has
        none of its own -- an honest empty is not the bug."""
        scope = self._scope(category=None, own_rows=[], wide_rows=[])
        assert scope.own_only is True

    def test_a_resolved_category_does_the_job_without_own_only(self):
        scope = self._scope(category="crime", own_rows=[], wide_rows=[{"channel_id": "c"}])
        assert scope.category == "crime"
        assert scope.own_only is False


class TestAnEmptyWorkbookIsRefused:
    def test_it_raises_rather_than_writing_a_hollow_file(self):
        """The one output worse than no workbook: a file that looks
        delivered. The CSVs, graph and research bundle are already written
        by then, so the run keeps everything except the empty .xlsx."""
        scope = ex.WorkbookScope(category="crime", min_subscribers=None,
                                 own_only=True, video_limit=None)
        with patch.object(ex, "workbook_scope", return_value=scope), \
             patch.object(ex, "workbook_rows", return_value=([], [])), \
             patch.object(ex, "_fetch", return_value=[{"n": 14}]):
            with pytest.raises(ex.EmptyWorkbookError, match="Refusing"):
                ex.export_excel("run-x", out_path=None)

    def test_a_run_with_no_tagged_channels_is_allowed_to_be_empty(self):
        """Nothing found is a real result, and refusing to write it would
        turn an honest answer into a crash."""
        scope = ex.WorkbookScope(category=None, min_subscribers=None,
                                 own_only=True, video_limit=None)
        with patch.object(ex, "workbook_scope", return_value=scope), \
             patch.object(ex, "workbook_rows", return_value=([], [])), \
             patch.object(ex, "_fetch", return_value=[{"n": 0}]):
            try:
                ex.export_excel("run-x", out_path=None)
            except ex.EmptyWorkbookError:
                pytest.fail("an honestly empty run must not raise")
            except Exception:
                pass  # it fails later for unrelated reasons; the guard passed

    def test_the_error_names_what_it_found_so_the_cause_is_diagnosable(self):
        scope = ex.WorkbookScope(category="crime", min_subscribers=None,
                                 own_only=True, video_limit=None)
        with patch.object(ex, "workbook_scope", return_value=scope), \
             patch.object(ex, "workbook_rows", return_value=([], [])), \
             patch.object(ex, "_fetch", return_value=[{"n": 14}]):
            with pytest.raises(ex.EmptyWorkbookError) as exc:
                ex.export_excel("run-x", out_path=None)
        assert "14" in str(exc.value)
        assert "own_only=True" in str(exc.value)

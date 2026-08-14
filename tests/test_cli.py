"""The account-budget preflight — specifically, that it is resume-aware.

Observed live: Legal's resume aborted projecting 6510 (ledger) + 4000 (the
FULL per-run budget) = 10510 against a 9100 ceiling, when the thread had
already spent 1513 of that 4000 and its true remaining exposure was only
2487 — 6510 + 2487 = 8997, safely under. The preflight was double-counting a
resume's own past spend, which is already inside the ledger total.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.cli import _already_spent_by_thread, _preflight_account_budget
from src.config import HarnessConfig


def _cfg(**overrides):
    base = {
        "brightdata_record_budget": 4000,
        "brightdata_account_record_budget": 9100,
        "brightdata_cost_per_record_usd": 0.0015,
    }
    base.update(overrides)
    return HarnessConfig(**base)


class TestAlreadySpentByThread:
    def test_reads_the_checkpointed_counter(self):
        with patch("src.api.server._load_checkpoint") as load:
            load.return_value = {"brightdata_records_used": 1513}
            assert _already_spent_by_thread("thread-x") == 1513

    def test_missing_checkpoint_is_zero_not_an_error(self):
        with patch("src.api.server._load_checkpoint", return_value=None):
            assert _already_spent_by_thread("thread-x") == 0

    def test_a_read_failure_falls_back_to_zero(self):
        """Must not block a resume just because the peek failed — falling
        back to 0 reproduces a fresh run's (conservative) behaviour, never a
        permissive one."""
        with patch("src.api.server._load_checkpoint", side_effect=RuntimeError("db down")):
            assert _already_spent_by_thread("thread-x") == 0


class TestPreflightIsResumeAware:
    def test_the_exact_observed_failure_now_passes(self):
        """6510 ledger + (4000 - 1513 already spent) = 8997 <= 9100."""
        with patch("src.config.get_config") as gc, \
             patch("src.observability.logging_config.total_records_spent", return_value=6510), \
             patch("src.cli._already_spent_by_thread", return_value=1513):
            gc.return_value.harness = _cfg()
            assert _preflight_account_budget("thread-6ca2f17710d8") is True

    def test_a_fresh_run_stays_conservative(self):
        """No resume thread: the full per-run budget is still the worst case,
        exactly as it must be for a run that has spent nothing yet."""
        with patch("src.config.get_config") as gc, \
             patch("src.observability.logging_config.total_records_spent", return_value=6510):
            gc.return_value.harness = _cfg()
            assert _preflight_account_budget(None) is False

    def test_a_resume_that_would_still_breach_is_blocked(self):
        """Being resume-aware must not become permissive — a resume that
        genuinely would cross the ceiling still aborts."""
        with patch("src.config.get_config") as gc, \
             patch("src.observability.logging_config.total_records_spent", return_value=8900), \
             patch("src.cli._already_spent_by_thread", return_value=100):
            gc.return_value.harness = _cfg()
            # 8900 + (4000 - 100) = 12800, well over 9100.
            assert _preflight_account_budget("thread-x") is False

    def test_disabled_ceiling_always_proceeds(self):
        with patch("src.config.get_config") as gc:
            gc.return_value.harness = _cfg(brightdata_account_record_budget=0)
            assert _preflight_account_budget("thread-x") is True

    def test_remaining_this_run_never_goes_negative(self):
        """A thread that somehow already spent more than its own per-run
        budget (a resume against a lowered ceiling) must not credit it a
        negative allowance that understates the projection."""
        with patch("src.config.get_config") as gc, \
             patch("src.observability.logging_config.total_records_spent", return_value=1000), \
             patch("src.cli._already_spent_by_thread", return_value=5000):
            gc.return_value.harness = _cfg(brightdata_record_budget=4000)
            # remaining_this_run clamps to 0, so projected == spent == 1000.
            assert _preflight_account_budget("thread-x") is True

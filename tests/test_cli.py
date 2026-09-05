"""The account-budget preflight — specifically, that it is resume-aware.

Observed live: Legal's resume aborted projecting 6510 (ledger) + 4000 (the
FULL per-run budget) = 10510 against a 9100 ceiling, when the thread had
already spent 1513 of that 4000 and its true remaining exposure was only
2487 — 6510 + 2487 = 8997, safely under. The preflight was double-counting a
resume's own past spend, which is already inside the ledger total.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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


class TestExportUsesTheActualRunId:
    """_run() mints a fresh run_id on every invocation, including --resume.
    On a fresh run that matches state["run_id"] (create_initial_state stamps
    it with exactly this value). On resume it does not: the checkpointed
    state — and everything hydrate_metadata tagged in category_tags under it
    — keeps the ORIGINAL run_id, since the resume-delta logic in run_pipeline
    never overwrites an existing key. Exporting under the freshly-generated id
    queried category_tags for a run that tagged nothing: real report prose
    sitting above channels: 0, videos: 0, edges: 0."""

    @pytest.mark.asyncio
    async def test_fresh_run_exports_under_its_own_id(self):
        from src.cli import _run

        with patch("src.cli.ensure_schema"), \
             patch("src.cli.get_async_checkpointer", new=AsyncMock(return_value=None)), \
             patch("src.cli.run_pipeline", new=AsyncMock(
                 return_value={"run_id": "run-fresh123", "final_report": {}}
             )), \
             patch("src.export.export_run") as export_run, \
             patch("src.cli.close_async_pool", new=AsyncMock()), \
             patch("src.cli.close_pools"), \
             patch("uuid.uuid4") as u:
            u.return_value.hex = "fresh123456789"
            await _run(["Finance"], resume_thread_id=None)

        called_run_id = export_run.call_args[0][0]
        assert called_run_id == "run-fresh123", (
            "for a fresh run the minted id and the state id are the same value"
        )

    @pytest.mark.asyncio
    async def test_resumed_run_exports_under_the_ORIGINAL_id_not_the_minted_one(self):
        from src.cli import _run

        with patch("src.cli.ensure_schema"), \
             patch("src.cli.get_async_checkpointer", new=AsyncMock(return_value=None)), \
             patch("src.cli.run_pipeline", new=AsyncMock(
                 # The pipeline returns the ORIGINAL checkpointed run_id —
                 # never the fresh one _run() generated for this invocation.
                 return_value={"run_id": "run-ORIGINAL", "final_report": {}}
             )), \
             patch("src.export.export_run") as export_run, \
             patch("src.cli.close_async_pool", new=AsyncMock()), \
             patch("src.cli.close_pools"):
            await _run(["Finance"], resume_thread_id="thread-existing")

        called_run_id = export_run.call_args[0][0]
        assert called_run_id == "run-ORIGINAL", (
            "must export under the id the data was actually tagged with"
        )

    @pytest.mark.asyncio
    async def test_missing_run_id_in_final_state_falls_back_to_the_minted_one(self):
        """Never crash on export because the state was somehow thin — fall
        back to the id this invocation generated rather than exporting under
        an empty string."""
        from src.cli import _run

        with patch("src.cli.ensure_schema"), \
             patch("src.cli.get_async_checkpointer", new=AsyncMock(return_value=None)), \
             patch("src.cli.run_pipeline", new=AsyncMock(return_value={"final_report": {}})), \
             patch("src.export.export_run") as export_run, \
             patch("src.cli.close_async_pool", new=AsyncMock()), \
             patch("src.cli.close_pools"), \
             patch("uuid.uuid4") as u:
            u.return_value.hex = "fallback123456"
            await _run(["Finance"], resume_thread_id="thread-x")

        called_run_id = export_run.call_args[0][0]
        assert called_run_id == "run-fallback1234"  # run_id truncates hex to [:12]


class TestFinishedRunsStopBeingReportedAsRunning:
    """launch_run starts each run with Popen and discards the handle, so
    nothing ever wait()s on it. A finished run therefore stays in the process
    table as a zombie -- and os.kill(pid, 0) SUCCEEDS on a zombie, so the
    console reported every completed run as "running", permanently.

    Caught live: a run was stopped, `ps` showed it gone, and the Live runs
    page still showed it running with its pid resolving to a <defunct> entry.
    """

    def test_a_zombie_is_not_running(self, monkeypatch):
        from src.api import runs as runs_mod

        monkeypatch.setattr(runs_mod.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(runs_mod, "_is_zombie", lambda pid: True)
        reaped = []
        monkeypatch.setattr(
            runs_mod.os, "waitpid",
            lambda pid, flags: reaped.append(pid) or (pid, 0),
        )

        assert runs_mod.is_process_running(4242) is False
        assert reaped == [4242], "a zombie should also be reaped, not just reported"

    def test_a_live_process_is_still_running(self, monkeypatch):
        from src.api import runs as runs_mod

        monkeypatch.setattr(runs_mod.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(runs_mod, "_is_zombie", lambda pid: False)
        assert runs_mod.is_process_running(4242) is True

    def test_a_dead_pid_is_not_running(self, monkeypatch):
        from src.api import runs as runs_mod

        def _boom(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(runs_mod.os, "kill", _boom)
        assert runs_mod.is_process_running(4242) is False

    def test_no_pid_is_not_running(self):
        from src.api import runs as runs_mod

        assert runs_mod.is_process_running(None) is False
        assert runs_mod.is_process_running(0) is False

    def test_zombie_detection_parses_a_comm_containing_spaces_and_parens(self, tmp_path, monkeypatch):
        """/proc/<pid>/stat puts the state after `comm`, which is
        parenthesised and may itself contain ')' -- splitting on whitespace
        or the FIRST ')' misreads the state for such a process."""
        from src.api import runs as runs_mod

        class _FakePath:
            def __init__(self, _p): pass
            def read_text(self): return "4242 (we (ird) proc) Z 1 4242 0"

        monkeypatch.setattr(runs_mod, "Path", _FakePath)
        assert runs_mod._is_zombie(4242) is True

    def test_a_missing_proc_entry_is_not_treated_as_a_zombie(self, monkeypatch):
        """On a non-Linux host there is no /proc; the os.kill result has to
        stand rather than every run being declared finished."""
        from src.api import runs as runs_mod

        class _FakePath:
            def __init__(self, _p): pass
            def read_text(self): raise FileNotFoundError

        monkeypatch.setattr(runs_mod, "Path", _FakePath)
        assert runs_mod._is_zombie(4242) is False


class TestRunIdOverride:
    """The console's launch/monitor link.

    launch_run() mints a run_id/thread_id, registers them, then starts this
    process as a subprocess -- but _run() used to mint its OWN ids every
    time regardless, so the registry's ids and the ids the child actually
    logged, checkpointed and exported under were never the same pair. The
    registry entry the console polled stayed at 0 log lines forever, while
    the real run proceeded invisibly under a run_id nothing pointed at.
    Caught live: a Glimpse test run's console entry never left "running"
    while its actual NodeLog, found only by scanning logs/*.jsonl, showed
    it finish minutes earlier under a different id entirely.
    """

    @pytest.mark.asyncio
    async def test_run_id_override_is_used_verbatim(self):
        from src.cli import _run

        with patch("src.cli.ensure_schema"), \
             patch("src.cli.get_async_checkpointer", new=AsyncMock(return_value=None)), \
             patch("src.cli.run_pipeline", new=AsyncMock(return_value={})) as run_pipeline, \
             patch("src.export.export_run") as export_run, \
             patch("src.export.export_excel"), \
             patch("src.cli.close_async_pool", new=AsyncMock()), \
             patch("src.cli.close_pools"):
            await _run(
                ["Finance"], resume_thread_id=None,
                run_id_override="run-pinned000", thread_id_override="thread-pinned000",
            )

        assert run_pipeline.call_args.kwargs["run_id"] == "run-pinned000"
        assert run_pipeline.call_args.kwargs["thread_id"] == "thread-pinned000"
        assert export_run.call_args[0][0] == "run-pinned000"

    @pytest.mark.asyncio
    async def test_resume_thread_id_wins_over_thread_id_override(self):
        """--resume and --thread-id are mutually exclusive at the argparse
        level (main() rejects both), but _run() itself must still resolve
        deterministically if ever called with both: resuming a specific
        checkpoint has to win, or a resume silently starts a fresh thread."""
        from src.cli import _run

        with patch("src.cli.ensure_schema"), \
             patch("src.cli.get_async_checkpointer", new=AsyncMock(return_value=None)), \
             patch("src.cli.run_pipeline", new=AsyncMock(return_value={})) as run_pipeline, \
             patch("src.export.export_run"), \
             patch("src.export.export_excel"), \
             patch("src.cli.close_async_pool", new=AsyncMock()), \
             patch("src.cli.close_pools"):
            await _run(
                ["Finance"], resume_thread_id="thread-resume-me",
                thread_id_override="thread-should-be-ignored",
            )

        assert run_pipeline.call_args.kwargs["thread_id"] == "thread-resume-me"

    def test_launch_run_pins_the_subprocess_to_the_registered_ids(self, tmp_path, monkeypatch):
        """The other half of the link: launch_run() must actually pass
        --run-id/--thread-id, not just be capable of it."""
        from src.api import runs as runs_mod

        monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
        captured = {}

        class _FakeProc:
            pid = 4242

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return _FakeProc()

        with patch("src.api.runs.subprocess.Popen", side_effect=_fake_popen), \
             patch("src.api.runs._append_registry"):
            entry = runs_mod.launch_run(["finance"])

        argv = captured["argv"]
        assert "--run-id" in argv
        assert argv[argv.index("--run-id") + 1] == entry["run_id"]
        assert "--thread-id" in argv
        assert argv[argv.index("--thread-id") + 1] == entry["thread_id"]

"""Threshold overrides, stopping a run, and deleting a run or workbook.

These are the console's destructive and configuring edges, so each guard is
asserted rather than assumed: a delete that removes the wrong thing, or a
threshold that silently does nothing because its env name is a typo, both
fail quietly and are discovered much later.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.api import runs as runs_mod
from src.api import thresholds as th


class TestThresholdCatalog:
    def test_every_env_name_is_a_real_config_field(self):
        """The override reaches the run as an environment variable, so a
        name HarnessConfig does not read is an override that silently does
        nothing -- the failure mode is a client setting a floor and getting
        a run that ignored it."""
        from src.config import HarnessConfig

        fields = {f.upper() for f in HarnessConfig.model_fields}
        for spec in th.THRESHOLDS:
            assert spec.env in fields, f"{spec.id}: {spec.env} is not a config field"

    def test_env_name_matches_the_id(self):
        for spec in th.THRESHOLDS:
            assert spec.env == spec.id.upper(), spec.id

    def test_every_threshold_explains_itself(self):
        for spec in th.THRESHOLDS:
            assert spec.label and spec.help, spec.id
            assert spec.minimum < spec.maximum, spec.id

    def test_the_channel_cap_defaults_to_the_chosen_tier(self):
        """HarnessConfig's own default is 0 (uncapped), but a run launched
        at a depth gets the tier's cap -- showing the config default would
        display a number the run was never going to use."""
        from src.api.depth import get_tier

        rows = {r["id"]: r for r in th.catalog("sample")}
        assert rows["max_channels_per_run"]["default"] == get_tier("sample").max_channels

    def test_without_a_depth_it_falls_back_to_config(self):
        rows = {r["id"]: r for r in th.catalog(None)}
        assert rows["subscriber_floor"]["default"] == 50000


class TestThresholdValidation:
    def test_nothing_in_nothing_out(self):
        assert th.validate(None) == {}
        assert th.validate({}) == {}

    def test_a_valid_override_becomes_an_env_var(self):
        assert th.validate({"subscriber_floor": 25000}) == {"SUBSCRIBER_FLOOR": "25000"}

    def test_an_unknown_id_is_refused(self):
        with pytest.raises(ValueError, match="Unknown threshold"):
            th.validate({"not_a_threshold": 1})

    def test_out_of_range_is_refused_not_clamped(self):
        """Substituting a different number than the client set, silently, is
        worse than telling them it was not accepted."""
        with pytest.raises(ValueError, match="between"):
            th.validate({"subscriber_floor": 10})
        with pytest.raises(ValueError, match="between"):
            th.validate({"saturation_novelty_threshold": 0.9})

    def test_a_non_number_is_refused(self):
        with pytest.raises(ValueError, match="must be a number"):
            th.validate({"subscriber_floor": "lots"})

    def test_blank_means_leave_it_alone(self):
        assert th.validate({"subscriber_floor": ""}) == {}
        assert th.validate({"subscriber_floor": None}) == {}

    def test_ints_serialise_without_a_decimal_point(self):
        """SUBSCRIBER_FLOOR=25000.0 would not parse as an int field."""
        out = th.validate({"subscriber_floor": 25000.0})
        assert out["SUBSCRIBER_FLOOR"] == "25000"


class TestThresholdsReachTheRun:
    def test_overrides_are_applied_after_the_tier(self, tmp_path, monkeypatch):
        """An explicit choice has to win over the tier's own governor, or
        raising the channel cap would appear to work and do nothing."""
        monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
        captured = {}

        class _P:
            pid = 1

        def _popen(argv, **kwargs):
            captured["env"] = kwargs["env"]
            return _P()

        with patch("src.api.runs.subprocess.Popen", side_effect=_popen), \
             patch("src.api.runs._append_registry"):
            runs_mod.launch_run(
                ["finance"], depth="standard",
                thresholds={"max_channels_per_run": 200, "subscriber_floor": 20000},
            )

        env = captured["env"]
        assert env["MAX_CHANNELS_PER_RUN"] == "200", "the override must beat the tier"
        assert env["SUBSCRIBER_FLOOR"] == "20000"
        # Untouched governors still come from the tier.
        assert env["RUN_DEADLINE_SECONDS"] == "14400"

    def test_an_invalid_threshold_stops_the_launch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
        with patch("src.api.runs.subprocess.Popen") as popen:
            with pytest.raises(ValueError):
                runs_mod.launch_run(["finance"], thresholds={"subscriber_floor": 1})
        popen.assert_not_called()


class TestStopRun:
    def test_unknown_run_is_a_lookup_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
        with pytest.raises(LookupError):
            runs_mod.stop_run("run-nope")

    def test_stopping_a_finished_run_is_not_an_error(self, tmp_path, monkeypatch):
        """The console cannot know the process died between rendering the
        button and the click landing."""
        monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
        (tmp_path / "registry.jsonl").write_text(
            json.dumps({"run_id": "run-a", "pid": 4242}) + "\n"
        )
        with patch.object(runs_mod, "is_process_running", return_value=False):
            out = runs_mod.stop_run("run-a")
        assert out["stopped"] is False
        assert out["reason"] == "not running"

    def test_a_live_run_gets_sigterm_not_sigkill(self, tmp_path, monkeypatch):
        """The harness writes its log and checkpoint as it goes, so a
        terminated run keeps what it finished. SIGKILL would not let it."""
        import signal

        monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
        (tmp_path / "registry.jsonl").write_text(
            json.dumps({"run_id": "run-a", "pid": 4242}) + "\n"
        )
        sent = {}
        with patch.object(runs_mod, "is_process_running", return_value=True), \
             patch.object(runs_mod.os, "kill", side_effect=lambda p, s: sent.update(pid=p, sig=s)):
            out = runs_mod.stop_run("run-a")

        assert out["stopped"] is True
        assert sent == {"pid": 4242, "sig": signal.SIGTERM}


class TestDeleteRun:
    def _seed(self, tmp_path, monkeypatch, pid=None):
        monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
        (tmp_path / "registry.jsonl").write_text(
            json.dumps({"run_id": "run-a", "pid": pid}) + "\n"
            + json.dumps({"run_id": "run-b", "pid": None}) + "\n"
        )
        (tmp_path / "run-a.jsonl").write_text("{}\n")
        (tmp_path / "run-a.stdout.log").write_text("x")
        (tmp_path / "spend").mkdir(exist_ok=True)
        (tmp_path / "spend" / "run-a.jsonl").write_text("{}\n")

    def test_a_running_run_is_refused(self, tmp_path, monkeypatch):
        """Deleting the log of a process still writing to it leaves the
        console showing a run whose file reappears a second later."""
        self._seed(tmp_path, monkeypatch, pid=4242)
        with patch.object(runs_mod, "is_process_running", return_value=True):
            with pytest.raises(ValueError, match="Stop it before deleting"):
                runs_mod.delete_run("run-a")
        assert (tmp_path / "run-a.jsonl").exists()

    def test_deleting_removes_every_artefact_and_only_that_run(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch, pid=None)
        with patch.object(runs_mod, "is_process_running", return_value=False):
            out = runs_mod.delete_run("run-a")

        assert out["deleted"] is True
        assert not (tmp_path / "run-a.jsonl").exists()
        assert not (tmp_path / "run-a.stdout.log").exists()
        assert not (tmp_path / "spend" / "run-a.jsonl").exists()

        # The other run's registry entry survives.
        kept = [json.loads(l) for l in (tmp_path / "registry.jsonl").read_text().splitlines() if l.strip()]
        assert [e["run_id"] for e in kept] == ["run-b"]

    def test_a_log_only_run_can_be_deleted(self, tmp_path, monkeypatch):
        """Not every run has a registry entry; one discovered from its log
        file must still be removable."""
        monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
        (tmp_path / "run-x.jsonl").write_text("{}\n")
        out = runs_mod.delete_run("run-x")
        assert out["deleted"] is True
        assert not (tmp_path / "run-x.jsonl").exists()


class TestDeleteWorkbook:
    def test_the_delivered_workbooks_are_refused(self):
        """They are the shipped client work, they sit outside the per-run
        export layout, and a re-export would not reproduce them."""
        from src.api import workbooks as wb

        for wid in ("finance", "crime"):
            with pytest.raises(ValueError, match="cannot be deleted"):
                wb.delete_workbook(wid)

    def test_an_unknown_workbook_is_a_lookup_error(self):
        from src.api import workbooks as wb

        with pytest.raises(LookupError):
            wb.delete_workbook("run-does-not-exist")

    def test_a_run_export_removes_the_whole_directory(self, tmp_path, monkeypatch):
        """The .xlsx is not the only artefact: channels.csv, the graph and
        the manifest sit beside it, and leaving them makes the directory
        still look like an export with a missing workbook."""
        from src.api import workbooks as wb

        export = tmp_path / "exports" / "run-z"
        export.mkdir(parents=True)
        (export / "run-z.xlsx").write_text("x")
        (export / "channels.csv").write_text("y")
        (export / "manifest.json").write_text("{}")

        monkeypatch.setattr(wb, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(
            wb, "resolve_path", lambda wid: export / "run-z.xlsx" if wid == "run-z" else None
        )
        out = wb.delete_workbook("run-z")

        assert sorted(out["removed"]) == ["channels.csv", "manifest.json", "run-z.xlsx"]
        assert not export.exists()

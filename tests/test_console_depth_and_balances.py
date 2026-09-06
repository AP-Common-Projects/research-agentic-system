"""The console's wallet gate: depth tiers, balances, and launch refusal.

This is the surface where a bug spends money. A tier that unlocks on a
balance that cannot fund it starts a run that dies half-finished, and a
tier that locks on an unknown balance stops work for no reason. Both
directions are asserted here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.api import depth as depth_mod


def _balances(openrouter=None, brightdata=None):
    """A balances payload shaped exactly like balances.all_balances()."""
    return {
        "providers": [
            {"provider": "openrouter", "available_usd": openrouter,
             "source": "live" if openrouter is not None else "unavailable"},
            {"provider": "brightdata", "available_usd": brightdata,
             "source": "live" if brightdata is not None else "derived"},
        ]
    }


class TestTierShape:
    def test_there_are_three_tiers_spanning_a_look_to_a_full_map(self):
        """Seven tiers were seven guesses at one unknown -- they shared a
        per-channel model, so when it proved four times optimistic they were
        all wrong together. Three, each sized from measured throughput."""
        assert [t.id for t in depth_mod.TIERS] == ["sample", "standard", "deep"]
        assert [t.hours for t in depth_mod.TIERS] == [1, 4, 10]

    def test_duration_label_never_renders_a_fraction_of_an_hour(self):
        """The UI prints this string verbatim, so "0.5h" must not reach it."""
        labels = [t.duration_label for t in depth_mod.TIERS]
        assert labels == ["1h", "4h", "10h"]

    def test_availability_rows_carry_every_derived_field(self):
        """asdict() serialises dataclass FIELDS only. est_channels and
        est_videos became properties and vanished from the payload until
        they were added by hand -- the depth cards rendered blank."""
        rows = depth_mod.tiers_with_availability(
            _balances(openrouter=500.0, brightdata=500.0)
        )
        row = next(r for r in rows if r["id"] == "sample")
        for key in ("est_channels", "est_videos", "est_total_usd",
                    "duration_label", "max_channels"):
            assert key in row, f"{key} is missing from the API payload"
            assert row[key] not in (None, ""), key

    def test_availability_rows_carry_the_duration_label(self):
        rows = depth_mod.tiers_with_availability(
            _balances(openrouter=500.0, brightdata=500.0)
        )
        assert next(r for r in rows if r["id"] == "sample")["duration_label"] == "1h"

    def test_shortest_tier_is_the_cheapest_and_is_affordable_on_pocket_change(self):
        sample = depth_mod.TIERS[0]
        assert sample.est_total_usd == min(t.est_total_usd for t in depth_mod.TIERS)
        rows = depth_mod.tiers_with_availability(
            _balances(openrouter=sample.est_openrouter_usd,
                      brightdata=sample.est_brightdata_usd)
        )
        assert not next(r for r in rows if r["id"] == "sample")["locked"]

    def test_brightdata_estimate_matches_the_record_budget_it_buys(self):
        """$0.0015/record is the measured rate; an estimate that disagrees with
        the governor it ships with would quote the client the wrong figure."""
        for tier in depth_mod.TIERS:
            budget = tier.governors["BRIGHTDATA_RECORD_BUDGET"]
            assert tier.est_brightdata_usd == round(budget * 0.0015, 2), tier.id

    def test_every_tier_is_named_not_just_numbered(self):
        for tier in depth_mod.TIERS:
            assert tier.label and not tier.label[0].isdigit(), tier.id
            assert tier.tagline, tier.id

    def test_cost_rises_monotonically_with_depth(self):
        totals = [t.est_total_usd for t in depth_mod.TIERS]
        assert totals == sorted(totals), totals

    def test_governors_use_config_env_names(self):
        """Governors are passed as env to the child, so the names must be the
        ones HarnessConfig actually reads -- a typo here silently does
        nothing rather than failing loudly."""
        from src.config import HarnessConfig

        fields = {f.upper() for f in HarnessConfig.model_fields}
        for tier in depth_mod.TIERS:
            for key in tier.governors:
                assert key in fields, f"{tier.id}: {key} is not a HarnessConfig field"


class TestAffordability:
    def test_a_tier_locks_when_a_provider_cannot_fund_it(self):
        rows = depth_mod.tiers_with_availability(_balances(openrouter=1.0, brightdata=500.0))
        deep = next(r for r in rows if r["id"] == "deep")
        assert deep["locked"]
        assert any("OpenRouter" in b for b in deep["blockers"])

    def test_a_tier_unlocks_when_both_providers_can_fund_it(self):
        rows = depth_mod.tiers_with_availability(_balances(openrouter=500.0, brightdata=500.0))
        assert not any(r["locked"] for r in rows)

    def test_either_provider_alone_can_lock_a_tier(self):
        """Discovery and model spend are topped up separately; a full
        OpenRouter balance must not paper over an empty Bright Data one."""
        rows = depth_mod.tiers_with_availability(_balances(openrouter=500.0, brightdata=1.0))
        deep = next(r for r in rows if r["id"] == "deep")
        assert deep["locked"]
        assert any("Bright Data" in b for b in deep["blockers"])

    def test_unknown_balance_warns_but_does_not_lock(self):
        """Bright Data without billing permission reports nothing. Refusing to
        run on a number nobody has is worse than running into the harness's
        own circuit breaker, which exists for this."""
        rows = depth_mod.tiers_with_availability(_balances(openrouter=500.0, brightdata=None))
        assert not any(r["locked"] for r in rows)
        assert all(r["warnings"] for r in rows)

    def test_a_tier_survives_a_thin_but_exactly_sufficient_wallet(self):
        # Bound by id, not position -- a new shallowest tier must not silently
        # re-point this at something cheaper.
        standard = depth_mod.get_tier("standard")
        rows = depth_mod.tiers_with_availability(
            _balances(openrouter=standard.est_openrouter_usd,
                      brightdata=standard.est_brightdata_usd)
        )
        assert not next(r for r in rows if r["id"] == "standard")["locked"]


class TestBalanceHonesty:
    def test_derived_brightdata_is_labelled_and_explains_itself(self):
        from src.api.balances import _derived_brightdata

        row = _derived_brightdata("HTTP 403")
        assert row["source"] == "derived"
        assert "403" in row["remediation"]
        assert row["spent_usd"] is not None

    def test_a_failed_lookup_never_reports_a_number(self):
        """An unreachable provider must yield None, not 0 -- zero reads as
        'no money' and would lock every tier on a network blip."""
        from src.api import balances as bal

        bal._cache.clear()
        with patch.object(bal.httpx, "get", side_effect=OSError("network down")):
            row = bal.openrouter_balance(force=True)
        assert row["available_usd"] is None
        assert row["source"] == "unavailable"


class TestBrightDataConnect:
    """The Balances page's only path to a live Bright Data reading: paste a
    token, and it is validated live before it is trusted or persisted."""

    def _fake_response(self, status_code, payload=None, text=""):
        class _Resp:
            def __init__(self):
                self.status_code = status_code
                self._payload = payload or {}
                self.text = text
            def json(self):
                return self._payload
        return _Resp()

    def test_rejects_a_bad_token_without_touching_config_or_env(self, tmp_path, monkeypatch):
        from src.api import balances as bal

        env_file = tmp_path / ".env"
        env_file.write_text("BRIGHTDATA_API_KEY=collector-key\n")
        monkeypatch.setattr(bal, "ENV_PATH", env_file)

        collector_before = bal.get_config().brightdata.api_key
        billing_before = bal.get_config().brightdata.billing_api_key
        with patch.object(
            bal.httpx, "get",
            return_value=self._fake_response(403, text="Invalid credentials"),
        ):
            with pytest.raises(ValueError, match="rejected"):
                bal.connect_brightdata("bad-token")

        assert bal.get_config().brightdata.api_key == collector_before
        assert bal.get_config().brightdata.billing_api_key == billing_before
        assert env_file.read_text() == "BRIGHTDATA_API_KEY=collector-key\n"

    def test_accepts_a_good_token_as_a_separate_billing_key(self, tmp_path, monkeypatch):
        """The one regression this whole endpoint exists to prevent: a
        balance-capable token must never become the collector key. It did,
        once -- a live test run's every discovery call started failing 401
        the moment a Connect call landed, because both lived in `api_key`."""
        from src.api import balances as bal

        env_file = tmp_path / ".env"
        env_file.write_text("BRIGHTDATA_MODE=live\nBRIGHTDATA_API_KEY=collector-key\nOTHER=1\n")
        monkeypatch.setattr(bal, "ENV_PATH", env_file)
        # Pin the in-memory collector key too, so this asserts against a known
        # baseline rather than whatever the real process environment happens
        # to hold -- ambient state is exactly what let this regression through
        # live, undetected, the first time.
        monkeypatch.setattr(bal.get_config().brightdata, "api_key", "collector-key")
        bal._cache.clear()

        with patch.object(
            bal.httpx, "get",
            return_value=self._fake_response(200, {"balance": 100.0, "pending_costs": 12.5}),
        ):
            row = bal.connect_brightdata("good-token")

        assert row["source"] == "live"
        assert row["available_usd"] == 87.5
        assert bal.get_config().brightdata.billing_api_key == "good-token"
        assert bal.get_config().brightdata.api_key == "collector-key", (
            "connect_brightdata must never touch the collector key"
        )
        lines = env_file.read_text().splitlines()
        assert "BRIGHTDATA_API_KEY=collector-key" in lines, "collector key untouched in .env too"
        assert "BRIGHTDATA_BILLING_API_KEY=good-token" in lines
        assert "BRIGHTDATA_MODE=live" in lines
        assert "OTHER=1" in lines
        assert len(lines) == 4, "adds one new line for the billing key, edits nothing else"

    def test_the_accepted_key_reaches_child_processes(self, tmp_path, monkeypatch):
        """os.environ, not just the config object and .env.

        launch_run() hands each run os.environ.copy(), and load_dotenv() does
        not override a name already present there -- so updating only the
        config object and the file leaves every child inheriting whatever
        this process started with. That is exactly how a stale key survived
        both a Connect call and a .env rewrite and still reached a live run,
        which then failed 401 on every single discovery call while the run
        itself carried on reporting no error.
        """
        import os

        from src.api import balances as bal

        env_file = tmp_path / ".env"
        env_file.write_text("BRIGHTDATA_API_KEY=collector-key\n")
        monkeypatch.setattr(bal, "ENV_PATH", env_file)
        monkeypatch.delenv("BRIGHTDATA_BILLING_API_KEY", raising=False)
        bal._cache.clear()

        with patch.object(
            bal.httpx, "get",
            return_value=self._fake_response(200, {"balance": 1.0, "pending_costs": 0.0}),
        ):
            bal.connect_brightdata("good-token")

        assert os.environ["BRIGHTDATA_BILLING_API_KEY"] == "good-token"

    def test_billing_key_takes_priority_over_the_collector_key_when_reading_balance(self, monkeypatch):
        from src.api import balances as bal

        bal._cache.clear()
        cfg = bal.get_config().brightdata
        monkeypatch.setattr(cfg, "api_key", "collector-key", raising=False)
        monkeypatch.setattr(cfg, "billing_api_key", "billing-key", raising=False)

        seen_keys = []
        def _fake_get(url, headers=None, timeout=None):
            seen_keys.append(headers["Authorization"])
            return self._fake_response(200, {"balance": 5.0, "pending_costs": 0.0})

        with patch.object(bal.httpx, "get", side_effect=_fake_get):
            row = bal.brightdata_balance(force=True)

        assert row["source"] == "live"
        assert seen_keys == ["Bearer billing-key"]

    def test_empty_token_is_rejected_before_any_network_call(self):
        from src.api import balances as bal

        with patch.object(bal.httpx, "get") as mock_get:
            with pytest.raises(ValueError, match="empty"):
                bal.connect_brightdata("   ")
        mock_get.assert_not_called()


class TestLaunchGate:
    def test_launch_run_rejects_an_unknown_depth(self):
        from src.api.runs import launch_run

        with pytest.raises(ValueError, match="Unknown depth"):
            launch_run(["finance"], depth="not-a-tier")


class TestRunDeadline:
    """The ceiling that makes a tier's name true.

    Every other governor bounds work, and none of them bounds the clock. On
    the first real console launch a "30m" Glimpse was tracking 3-5 hours:
    its 600-record budget bought 285 channels, and classification alone runs
    ~21 minutes per 50 channels, so the tier's own record budget mandated
    ~2 hours of classification before discovery was counted. The label was a
    projection of mine, enforced by nothing.
    """

    def test_every_tier_deadline_matches_its_own_label(self):
        for tier in depth_mod.TIERS:
            assert tier.governors["RUN_DEADLINE_SECONDS"] == int(tier.hours * 3600), tier.id

    def test_the_shortest_tier_really_is_one_hour(self):
        assert depth_mod.get_tier("sample").governors["RUN_DEADLINE_SECONDS"] == 3600

    def test_deadline_is_derived_not_hand_written(self):
        """A tier constructed with a contradictory deadline must be corrected
        rather than trusted -- hand-written values are how a label and its
        ceiling drift apart."""
        tier = depth_mod.DepthTier(
            id="t", label="T", hours=2, tagline="", description="",
            est_brightdata_usd=0.0, est_openrouter_usd=0.0,
            governors={"RUN_DEADLINE_SECONDS": 99},
        )
        assert tier.governors["RUN_DEADLINE_SECONDS"] == 7200

    def test_saturation_stops_the_run_once_the_deadline_passes(self, monkeypatch):
        from src.config import HarnessConfig
        from src.tools import saturation as sat

        monkeypatch.setattr(sat, "run_elapsed_seconds", lambda: 1801.0)
        monkeypatch.setattr(
            sat, "get_config",
            lambda: type("C", (), {"harness": HarnessConfig(run_deadline_seconds=1800)})(),
        )
        out = sat.check_saturation({
            "tree": {"root": {"status": "active"}},
            "active_node_id": "root",
            "budget_spent_usd": 0.0,
        })
        assert out["next_action"] == "budget_exhausted"
        assert out["tree"]["root"]["saturation_reason"] == "governor:run_deadline_seconds"

    def test_a_run_inside_its_deadline_is_not_stopped_by_it(self, monkeypatch):
        """The deadline must not be the reason a run ends early -- every
        other ceiling still has to be what decides."""
        from src.config import HarnessConfig
        from src.tools import saturation as sat

        monkeypatch.setattr(sat, "run_elapsed_seconds", lambda: 60.0)
        monkeypatch.setattr(
            sat, "get_config",
            lambda: type("C", (), {"harness": HarnessConfig(run_deadline_seconds=1800)})(),
        )
        out = sat.check_saturation({
            "tree": {"root": {"status": "active"}},
            "active_node_id": "root",
            "budget_spent_usd": 0.0,
        })
        reason = (out.get("tree", {}).get("root") or {}).get("saturation_reason", "")
        assert reason != "governor:run_deadline_seconds"

    def test_zero_means_uncapped_so_a_bare_cli_run_is_unaffected(self, monkeypatch):
        from src.config import HarnessConfig
        from src.tools import saturation as sat

        assert HarnessConfig().run_deadline_seconds == 0
        monkeypatch.setattr(sat, "run_elapsed_seconds", lambda: 10_000_000.0)
        monkeypatch.setattr(
            sat, "get_config",
            lambda: type("C", (), {"harness": HarnessConfig(run_deadline_seconds=0)})(),
        )
        out = sat.check_saturation({
            "tree": {"root": {"status": "active"}},
            "active_node_id": "root",
            "budget_spent_usd": 0.0,
        })
        reason = (out.get("tree", {}).get("root") or {}).get("saturation_reason", "")
        assert reason != "governor:run_deadline_seconds"


class TestGovernorsReachTheRun:
    def test_a_tier_actually_changes_the_child_process_config(self):
        """The end-to-end link the whole depth feature rests on.

        Governors are handed to the run as environment variables and resolved
        by HarnessConfig in the child. If a name were wrong the run would
        start happily on the .env profile and quietly ignore the depth the
        client paid for -- silent, and invisible in any log. So this asserts
        against a real subprocess, not a mock.
        """
        import json
        import os
        import subprocess
        import sys

        tier = depth_mod.get_tier("standard")
        assert tier is not None

        env = os.environ.copy()
        for key, value in tier.governors.items():
            env[key] = str(value)

        code = (
            "import sys; sys.path.insert(0, '.')\n"
            "from src.config import get_config\n"
            "import json\n"
            "h = get_config().harness\n"
            "print(json.dumps({"
            "'budget_limit_usd': h.budget_limit_usd,"
            "'max_rounds_per_branch': h.max_rounds_per_branch,"
            "'max_branches': h.max_branches,"
            "'run_deadline_seconds': h.run_deadline_seconds,"
            "'brightdata_record_budget': h.brightdata_record_budget}))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        resolved = json.loads(proc.stdout.strip().splitlines()[-1])

        assert resolved["budget_limit_usd"] == tier.governors["BUDGET_LIMIT_USD"]
        assert resolved["max_rounds_per_branch"] == tier.governors["MAX_ROUNDS_PER_BRANCH"]
        assert resolved["max_branches"] == tier.governors["MAX_BRANCHES"]
        assert resolved["brightdata_record_budget"] == tier.governors["BRIGHTDATA_RECORD_BUDGET"]
        # The clock ceiling has to survive the env round-trip like the rest:
        # a tier whose deadline never reached the child would run as long as
        # its work budget allowed, which is the bug this governor exists for.
        assert resolved["run_deadline_seconds"] == tier.governors["RUN_DEADLINE_SECONDS"]
        assert resolved["run_deadline_seconds"] == int(tier.hours * 3600)


class TestTopicSuggestions:
    """The launcher's sub-niche preview: what it shows and where it comes from."""

    def test_only_the_delivered_verticals_read_from_the_dataset(self):
        from src.api import topics as topics_mod

        assert topics_mod.DELIVERED_TOPICS == {"finance", "crime"}

    def test_every_other_catalog_topic_has_a_curated_map(self, monkeypatch):
        """A catalog topic with no curated map falls through to a live model
        call, which is a 30-second wait in the launcher."""
        from src.api import topics as topics_mod

        for topic, items in topics_mod.CURATED_SUBNICHES.items():
            assert topic not in topics_mod.DELIVERED_TOPICS, topic
            assert len(items) >= 8, f"{topic}: only {len(items)} sub-niches"
            for name, why in items:
                assert name and name[0].isupper(), (topic, name)
                assert why, (topic, name)

    def test_curated_topics_never_reach_the_model(self, monkeypatch):
        from src.api import topics as topics_mod

        def _boom(_topic):
            raise AssertionError("a curated topic must not call the model")

        monkeypatch.setattr(topics_mod, "_proposed_subniches", _boom)
        out = topics_mod.suggest("Gaming")
        assert out["source"] == "proposed"
        assert len(out["subniches"]) >= 8
        assert all(s["channel_count"] is None for s in out["subniches"])

    def test_curated_lookup_is_insensitive_to_how_the_topic_is_written(self):
        from src.api import topics as topics_mod

        names = {
            s["name"]
            for s in topics_mod.suggest("science explainer")["subniches"]
        }
        assert names == {
            s["name"] for s in topics_mod.suggest("science_explainer")["subniches"]
        }
        assert names

    def test_dataset_rows_carry_no_channel_count_rationale(self, monkeypatch):
        """The rationale was the channel count in prose; the console stopped
        showing counts, and a tooltip is still showing."""
        from src.api import topics as topics_mod

        monkeypatch.setattr(
            topics_mod,
            "_dataset_subniches",
            lambda _t: [
                {
                    "name": "True Crime Documentary",
                    "slug": "true_crime_documentary",
                    "channel_count": 300,
                    "source": "dataset",
                    "rationale": "",
                }
            ],
        )
        out = topics_mod.suggest("crime")
        assert out["source"] == "dataset"
        assert all(not s["rationale"] for s in out["subniches"])

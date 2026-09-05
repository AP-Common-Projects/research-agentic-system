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
    def test_tiers_cover_the_durations_the_client_asked_for(self):
        assert [t.hours for t in depth_mod.TIERS] == [0.5, 1, 6, 12, 24, 48, 72]

    def test_duration_label_never_renders_a_fraction_of_an_hour(self):
        """The UI prints this string verbatim, so "0.5h" must not reach it."""
        labels = [t.duration_label for t in depth_mod.TIERS]
        assert labels == ["30m", "1h", "6h", "12h", "24h", "48h", "72h"]

    def test_availability_rows_carry_the_duration_label(self):
        rows = depth_mod.tiers_with_availability(
            _balances(openrouter=500.0, brightdata=500.0)
        )
        assert next(r for r in rows if r["id"] == "glimpse")["duration_label"] == "30m"

    def test_shortest_tier_is_the_cheapest_and_is_affordable_on_pocket_change(self):
        glimpse = depth_mod.TIERS[0]
        assert glimpse.est_total_usd == min(t.est_total_usd for t in depth_mod.TIERS)
        rows = depth_mod.tiers_with_availability(
            _balances(openrouter=glimpse.est_openrouter_usd,
                      brightdata=glimpse.est_brightdata_usd)
        )
        assert not next(r for r in rows if r["id"] == "glimpse")["locked"]

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
        census = next(r for r in rows if r["id"] == "census")
        assert census["locked"]
        assert any("OpenRouter" in b for b in census["blockers"])

    def test_a_tier_unlocks_when_both_providers_can_fund_it(self):
        rows = depth_mod.tiers_with_availability(_balances(openrouter=500.0, brightdata=500.0))
        assert not any(r["locked"] for r in rows)

    def test_either_provider_alone_can_lock_a_tier(self):
        """Discovery and model spend are topped up separately; a full
        OpenRouter balance must not paper over an empty Bright Data one."""
        rows = depth_mod.tiers_with_availability(_balances(openrouter=500.0, brightdata=1.0))
        census = next(r for r in rows if r["id"] == "census")
        assert census["locked"]
        assert any("Bright Data" in b for b in census["blockers"])

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
        scout = depth_mod.get_tier("scout")
        rows = depth_mod.tiers_with_availability(
            _balances(openrouter=scout.est_openrouter_usd,
                      brightdata=scout.est_brightdata_usd)
        )
        assert not next(r for r in rows if r["id"] == "scout")["locked"]


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
        env_file.write_text("BRIGHTDATA_API_KEY=old-key\n")
        monkeypatch.setattr(bal, "ENV_PATH", env_file)

        before = bal.get_config().brightdata.api_key
        with patch.object(
            bal.httpx, "get",
            return_value=self._fake_response(403, text="Invalid credentials"),
        ):
            with pytest.raises(ValueError, match="rejected"):
                bal.connect_brightdata("bad-token")

        assert bal.get_config().brightdata.api_key == before
        assert env_file.read_text() == "BRIGHTDATA_API_KEY=old-key\n"

    def test_accepts_a_good_token_and_persists_it(self, tmp_path, monkeypatch):
        from src.api import balances as bal

        env_file = tmp_path / ".env"
        env_file.write_text("BRIGHTDATA_MODE=live\nBRIGHTDATA_API_KEY=old-key\nOTHER=1\n")
        monkeypatch.setattr(bal, "ENV_PATH", env_file)
        bal._cache.clear()

        with patch.object(
            bal.httpx, "get",
            return_value=self._fake_response(200, {"balance": 100.0, "pending_costs": 12.5}),
        ):
            row = bal.connect_brightdata("good-token")

        assert row["source"] == "live"
        assert row["available_usd"] == 87.5
        assert bal.get_config().brightdata.api_key == "good-token"
        lines = env_file.read_text().splitlines()
        assert "BRIGHTDATA_API_KEY=good-token" in lines
        assert "BRIGHTDATA_MODE=live" in lines
        assert "OTHER=1" in lines
        assert len(lines) == 3, "must edit the one line, not append a duplicate"

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

        tier = depth_mod.get_tier("scout")
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

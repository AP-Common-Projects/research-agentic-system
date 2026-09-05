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
        assert [t.hours for t in depth_mod.TIERS] == [1, 6, 12, 24, 48, 72]

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

    def test_cheapest_tier_survives_a_thin_but_sufficient_wallet(self):
        scout = depth_mod.TIERS[0]
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

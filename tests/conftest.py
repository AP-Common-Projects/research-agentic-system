"""Shared test fixtures.

The JSON log sink writes every NodeLog to ``<log_dir>/<run_id>.jsonl``. Tests
that drive `run_pipeline` would otherwise scatter fixture runs through the
repo's real ``logs/`` directory, where they are indistinguishable from actual
runs — and the console would list them as real history. Redirect the sink to a
temp directory for the whole suite.
"""

from __future__ import annotations

import pytest

import src.config as config_module
from src.config import HarnessConfig


@pytest.fixture(autouse=True)
def isolate_log_sink(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.observability.log_sink._log_path",
        lambda run_id: tmp_path / f"{run_id}.jsonl",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def deterministic_profile(monkeypatch):
    """Pin the cost profile so tests never inherit the developer's .env.

    The repo's own .env carries HARNESS_PROFILE=smoke, which caps tree depth at
    1 and rounds at 2. Left alone, that silently changes what the tests are
    testing — select_next_node stops creating depth-2 nodes, check_saturation
    stops on the round cap before reaching the novelty logic — and the suite's
    result depends on a file that is not in version control.

    `full` is the right pin: every governor is uncapped, so tests exercise the
    logic under test rather than the circuit breakers. Tests that are *about* a
    governor set that governor explicitly.
    """
    monkeypatch.setenv("HARNESS_PROFILE", "full")
    monkeypatch.setattr(config_module, "_config", None)
    yield
    monkeypatch.setattr(config_module, "_config", None)


@pytest.fixture
def no_edge_store(mocker):
    """Neutralise graph_walk's edge persistence.

    graph_walk writes discovery_edges as a side effect. Unit tests are about
    traversal, not storage, and must not depend on a reachable Postgres — nor
    silently pass because the write failed and got swallowed into state errors.
    """
    return mocker.patch("src.tools.dedup.persist_edges", return_value=0)


def make_harness_config(**overrides) -> HarnessConfig:
    """A real HarnessConfig for tests that patch `get_config`.

    Returning a bare MagicMock leaves every field a MagicMock, so any new
    numeric governor blows up on comparison (`MagicMock > 0`) in a way that
    looks like a product bug rather than a stale test double.
    """
    base: dict = {
        "saturation_novelty_threshold": 0.05,
        "saturation_consecutive_window": 3,
        "budget_limit_usd": 10.0,
        "max_rounds_per_branch": 0,
        "max_tree_depth": 0,
        "max_branches": 0,
        "keyword_queries_per_round": 0,
        "graph_walk_frontier_per_round": 0,
        "min_subscribers_for_expansion": 0,
        "brightdata_record_budget": 0,
        "youtube_quota_budget_per_run": 0,
    }
    base.update(overrides)
    return HarnessConfig(**base)

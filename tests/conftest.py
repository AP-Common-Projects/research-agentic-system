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
        "plateau_detection_enabled": True,
        "novelty_plateau_epsilon": 0.05,
    }
    base.update(overrides)
    return HarnessConfig(**base)


# Every node module that reaches the network or the database on its own.
# Kept as data rather than a pile of decorators so that adding a node to the
# graph and forgetting to mock it cannot silently turn the suite into a live,
# paid run — which is exactly what happened when v4 added five nodes: a plain
# `pytest` made real LLM calls, spent real Bright Data records, and read and
# wrote the developer's real Postgres. Two suites hung for minutes apiece,
# holding connections to 127.0.0.1:5432 and two HTTPS peers.
_LLM_NODE_MODULES = (
    "src.nodes.populate_crime_metadata",
    "src.nodes.populate_shared_fields",
    "src.nodes.populate_taxonomy_dimensions",
)
_DB_NODE_MODULES = _LLM_NODE_MODULES + (
    "src.nodes.expand_niche_adjacency",
    "src.nodes.assign_cohorts",
)
# Guarded at each node's own call site, never at src.tools.bright_data
# itself: one test constructs BrightDataClient directly to exercise its
# retry/rate-limit behaviour, and patching the source class would break that
# test rather than isolate it. Patching per call site leaves the class itself
# importable and real.
_BRIGHTDATA_MODULES = (
    "src.tools.graph_walk",
    "src.tools.keyword_search",
    "src.tools.breakout_scanner",
    "src.tools.new_channel_discovery",
    "src.tools.underperformer_discovery",
)


@pytest.fixture(autouse=True)
def no_live_calls_from_v4_nodes(monkeypatch):
    """Fail closed: v4 nodes never reach a real LLM, API, or database.

    Autouse and unconditional. A test that genuinely wants one of these
    nodes to talk to something can still patch it itself — patches applied
    inside the test win over this one — but the default is safe, so the
    failure mode is a mocked call rather than a surprise invoice.

    Each of these nodes already wraps get_connection() in `except: return
    early`, so raising is the designed-in "no store available" path rather
    than new behaviour invented for the tests.
    """

    def _no_db(*_args, **_kwargs):
        raise RuntimeError("test isolation: no real database")

    def _no_llm(*_args, **_kwargs):
        return {"content": "{}", "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "cost_usd": 0.0, "model": "test", "tier": "test"}

    import importlib

    for module_name in _DB_NODE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if hasattr(module, "get_connection"):
            monkeypatch.setattr(module, "get_connection", _no_db, raising=False)
        if module_name in _LLM_NODE_MODULES and hasattr(module, "complete_tier"):
            monkeypatch.setattr(module, "complete_tier", _no_llm, raising=False)

    for module_name in _BRIGHTDATA_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if hasattr(module, "BrightDataClient"):
            monkeypatch.setattr(
                module, "BrightDataClient",
                lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("test isolation: no live Bright Data")
                ),
                raising=False,
            )

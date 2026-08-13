"""Tests for state.py — reducers, migration, and initial state factory.

These are pure Python tests with zero LLM calls — the fastest, cheapest layer
of the eval harness. Every reducer is tested for its merge behavior, including
the edge cases that the architecture review flagged.

Coverage targets:
- merge_discovered_channels: dedup, empty existing, empty new, interleaved duplicates
- merge_set_union: basic union, empty sets, disjoint sets
- merge_tree_dict: new keys, overwrite existing, empty state
- accumulate_float: basic sum, negative (refund), zero
- add_messages: standard behavior
- migrate_state: v0→v1→v2 migration, already-current state
- create_initial_state: all defaults present
"""

from __future__ import annotations

import pytest
from src.state import (
    _merge_discovered_channels,
    _merge_discovered_videos,
    _merge_set_union,
    _merge_tree_dict,
    _accumulate_float,
    create_initial_state,
    migrate_state,
)


# ---------------------------------------------------------------------------
# Reducer tests
# ---------------------------------------------------------------------------

class TestMergeDiscoveredChannels:
    def test_basic_dedup(self):
        existing = ["ch1", "ch2", "ch3"]
        new = ["ch2", "ch3", "ch4", "ch5"]
        result = _merge_discovered_channels(existing, new)
        assert result == ["ch1", "ch2", "ch3", "ch4", "ch5"]

    def test_empty_existing(self):
        result = _merge_discovered_channels([], ["ch1", "ch2"])
        assert result == ["ch1", "ch2"]

    def test_empty_new(self):
        result = _merge_discovered_channels(["ch1", "ch2"], [])
        assert result == ["ch1", "ch2"]

    def test_all_duplicates(self):
        result = _merge_discovered_channels(["ch1", "ch2"], ["ch1", "ch2"])
        assert result == ["ch1", "ch2"]

    def test_interleaved_duplicates(self):
        result = _merge_discovered_channels(["ch1", "ch2"], ["ch3", "ch1", "ch4", "ch2"])
        assert result == ["ch1", "ch2", "ch3", "ch4"]

    def test_preserves_order(self):
        result = _merge_discovered_channels(["a", "b"], ["c", "d", "e"])
        assert result == ["a", "b", "c", "d", "e"]

    def test_same_channel_from_both_parallel_branches(self):
        """Simulates Bug 1 pattern: both keyword_search and graph_walk find the same channel."""
        existing = ["ch_a", "ch_b"]
        new_from_keyword = ["ch_c", "ch_a"]
        new_from_graph = ["ch_d", "ch_c"]
        after_keyword = _merge_discovered_channels(existing, new_from_keyword)
        after_graph = _merge_discovered_channels(after_keyword, new_from_graph)
        assert after_graph == ["ch_a", "ch_b", "ch_c", "ch_d"]


class TestMergeSetUnion:
    def test_basic_union(self):
        result = _merge_set_union({"a", "b"}, {"b", "c"})
        assert result == {"a", "b", "c"}

    def test_empty_existing(self):
        result = _merge_set_union(set(), {"a", "b"})
        assert result == {"a", "b"}

    def test_disjoint_sets(self):
        result = _merge_set_union({"a", "b"}, {"c", "d"})
        assert result == {"a", "b", "c", "d"}


class TestMergeTreeDict:
    def test_new_key_added(self):
        node_a = {"id": "a", "label": "Node A", "status": "pending"}
        node_b = {"id": "b", "label": "Node B", "status": "pending"}
        existing = {"a": node_a}
        new = {"b": node_b}
        result = _merge_tree_dict(existing, new)
        assert "a" in result and "b" in result
        assert result["a"] == node_a
        assert result["b"] == node_b

    def test_overwrite_existing(self):
        node_a_v1 = {"id": "a", "label": "Node A", "status": "pending"}
        node_a_v2 = {"id": "a", "label": "Node A", "status": "saturated"}
        result = _merge_tree_dict({"a": node_a_v1}, {"a": node_a_v2})
        assert result["a"] == node_a_v2

    def test_empty_state(self):
        node_a = {"id": "a", "label": "Node A", "status": "pending"}
        result = _merge_tree_dict({}, {"a": node_a})
        assert result == {"a": node_a}


class TestAccumulateFloat:
    def test_basic_sum(self):
        assert _accumulate_float(1.5, 2.3) == pytest.approx(3.8)

    def test_negative(self):
        assert _accumulate_float(5.0, -2.0) == pytest.approx(3.0)

    def test_zero(self):
        assert _accumulate_float(3.14, 0.0) == pytest.approx(3.14)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestCreateInitialState:
    def test_all_defaults_present(self):
        state = create_initial_state(
            run_id="run-001",
            thread_id="thread-001",
            candidate_niches=["niche1", "niche2"],
        )
        assert state["run_id"] == "run-001"
        assert state["thread_id"] == "thread-001"
        assert state["candidate_niches"] == ["niche1", "niche2"]
        assert state["selected_niche"] == ""
        assert state["tree"] == {}
        assert state["active_node_id"] is None
        assert state["discovered_channel_ids"] == []
        assert state["discovered_video_ids"] == []
        assert state["visited_channel_ids"] == set()
        assert state["expanded_channel_ids"] == set()
        assert state["keyword_channel_ids"] == set()
        assert state["graph_walk_channel_ids"] == set()
        assert state["branch_compactions"] == []
        assert state["novelty_rates"] == []
        assert state["budget_spent_usd"] == 0.0
        assert state["next_action"] == "start"
        assert state["messages"] == []
        assert state["errors"] == []
        assert state["node_logs"] == []
        assert state["schema_version"] == 4
        assert state["final_report"] is None
        assert state["keyword_search_done"] is False
        assert state["graph_walk_done"] is False

    def test_create_is_idempotent(self):
        s1 = create_initial_state("r1", "t1", ["n1"])
        s2 = create_initial_state("r1", "t1", ["n1"])
        assert s1 == s2


# ---------------------------------------------------------------------------
# State migration
# ---------------------------------------------------------------------------

class TestMigrateState:
    def test_migrates_v0_to_current(self):
        state = {"schema_version": 0}
        result = migrate_state(state)
        assert result["schema_version"] == 4
        assert result["run_id"] == ""
        assert result["thread_id"] == ""
        assert result["errors"] == []
        assert result["node_logs"] == []
        assert result["budget_spent_usd"] == 0.0
        assert result["final_report"] is None
        assert result["keyword_search_done"] is False
        assert result["graph_walk_done"] is False
        assert result["saturated_branches"] == []
        assert result["niche_scanner_evidence"] == {}
        assert result["hydrated_channel_ids"] == set()

    def test_migrates_v1_to_current(self):
        state = {"schema_version": 1, "run_id": "r1", "thread_id": "t1"}
        result = migrate_state(state)
        assert result["schema_version"] == 4
        assert result["run_id"] == "r1"
        assert result["saturated_branches"] == []
        assert result["niche_scanner_evidence"] == {}
        assert result["hydrated_channel_ids"] == set()

    def test_migrates_v2_to_current(self):
        state = {"schema_version": 2, "saturated_branches": ["n1"]}
        result = migrate_state(state)
        assert result["schema_version"] == 4
        assert result["saturated_branches"] == ["n1"]
        assert result["hydrated_channel_ids"] == set()

    def test_migrates_v3_adds_discovery_attribution(self):
        state = {"schema_version": 3, "hydrated_channel_ids": {"UC1"}}
        result = migrate_state(state)
        assert result["schema_version"] == 4
        assert result["hydrated_channel_ids"] == {"UC1"}
        # Pre-v4 checkpoints cannot be retro-attributed — they must come back
        # empty rather than guessing which track found a channel.
        assert result["keyword_channel_ids"] == set()
        assert result["graph_walk_channel_ids"] == set()

    def test_migration_does_not_clobber_existing_attribution(self):
        state = {
            "schema_version": 4,
            "keyword_channel_ids": {"UC_kw"},
            "graph_walk_channel_ids": {"UC_gw"},
        }
        result = migrate_state(state)
        assert result["keyword_channel_ids"] == {"UC_kw"}
        assert result["graph_walk_channel_ids"] == {"UC_gw"}

    def test_already_current_no_change(self):
        state = {
            "schema_version": 4,
            "run_id": "r2",
            "saturated_branches": ["n1"],
            "niche_scanner_evidence": {"x": 1},
        }
        result = migrate_state(state)
        assert result["schema_version"] == 4
        assert result["saturated_branches"] == ["n1"]
        assert result["niche_scanner_evidence"] == {"x": 1}

    def test_preserves_existing_values_on_migration(self):
        state = {"schema_version": 0, "run_id": "my-run", "thread_id": "my-thread"}
        result = migrate_state(state)
        assert result["run_id"] == "my-run"
        assert result["thread_id"] == "my-thread"
"""build_graph_payload and its helpers — the data feeding the interactive
discovery-graph export. The interesting cases are the ref-identity problem
(edges whose target was never hydrated) and the trim policy when a run's
graph exceeds export_max_graph_nodes.
"""

from __future__ import annotations

from src.export import _category, _ref_label, build_graph_payload, build_taxonomy_payload


class TestCategory:
    def test_known_methods_pass_through(self):
        assert _category("keyword") == "keyword"
        assert _category("graph_walk") == "graph_walk"
        assert _category("both") == "both"

    def test_unattributed_or_unknown_folds_to_unresolved(self):
        assert _category(None) == "unresolved"
        assert _category("") == "unresolved"
        assert _category("something_else") == "unresolved"


class TestRefLabel:
    def test_handle_form_shows_the_handle(self):
        assert _ref_label("https://youtube.com/@SomeHandle") == "@SomeHandle"

    def test_channel_id_form_is_truncated(self):
        label = _ref_label("https://youtube.com/channel/UCabcdefghijklmno123")
        assert label == "UCabcdefgh…"

    def test_empty_or_missing_ref_is_labelled_unknown(self):
        assert _ref_label("") == "(unknown)"
        assert _ref_label(None) == "(unknown)"


class TestBuildGraphPayload:
    def test_every_hydrated_channel_becomes_a_node(self):
        channels = [
            {"channel_id": "c1", "title": "Channel One", "subscriber_count": 1000, "discovery_method": "keyword"},
            {"channel_id": "c2", "title": "Channel Two", "subscriber_count": 2000, "discovery_method": "graph_walk"},
        ]
        payload = build_graph_payload(channels, edges=[])
        assert {n["id"] for n in payload["nodes"]} == {"c1", "c2"}
        assert all(n["resolved"] for n in payload["nodes"])
        assert payload["category_counts"] == {"keyword": 1, "graph_walk": 1}

    def test_an_edges_unhydrated_target_ref_becomes_an_unresolved_node(self):
        """The ref-identity problem: featured_channels/recommendations point at
        a handle-form ref that was never itself expanded into a channels row,
        so target_channel_id is NULL and only target_channel_ref is known."""
        channels = [
            {"channel_id": "c1", "title": "Channel One", "subscriber_count": 1000, "discovery_method": "keyword"},
        ]
        edges = [
            {
                "source_channel_id": "c1",
                "target_channel_id": None,
                "target_channel_ref": "https://youtube.com/@NeverHydrated",
                "edge_type": "featured_channel",
            }
        ]
        payload = build_graph_payload(channels, edges)
        ids = {n["id"] for n in payload["nodes"]}
        assert "c1" in ids
        unresolved = [n for n in payload["nodes"] if not n["resolved"]]
        assert len(unresolved) == 1
        assert unresolved[0]["label"] == "@NeverHydrated"
        assert unresolved[0]["category"] == "unresolved"
        assert len(payload["edges"]) == 1
        assert payload["edges"][0]["source"] == "c1"
        assert payload["edges"][0]["target"] == "https://youtube.com/@NeverHydrated"

    def test_an_edge_whose_target_did_hydrate_uses_the_channel_id_not_the_ref(self):
        channels = [
            {"channel_id": "c1", "title": "One", "subscriber_count": 10, "discovery_method": "keyword"},
            {"channel_id": "c2", "title": "Two", "subscriber_count": 20, "discovery_method": "graph_walk"},
        ]
        edges = [
            {
                "source_channel_id": "c1",
                "target_channel_id": "c2",
                "target_channel_ref": "https://youtube.com/@Two",
                "edge_type": "featured_channel",
            }
        ]
        payload = build_graph_payload(channels, edges)
        assert len(payload["nodes"]) == 2
        assert payload["edges"][0]["target"] == "c2"

    def test_an_edge_with_no_usable_target_is_dropped_not_crashed_on(self):
        channels = [{"channel_id": "c1", "title": "One", "subscriber_count": 10, "discovery_method": "keyword"}]
        edges = [{"source_channel_id": "c1", "target_channel_id": None, "target_channel_ref": "", "edge_type": "x"}]
        payload = build_graph_payload(channels, edges)
        assert payload["edges"] == []

    def test_a_dangling_edge_source_that_never_hydrated_is_still_represented(self):
        edges = [
            {
                "source_channel_id": "orphan",
                "target_channel_id": None,
                "target_channel_ref": "https://youtube.com/@Target",
                "edge_type": "featured_channel",
            }
        ]
        payload = build_graph_payload(channels=[], edges=edges)
        ids = {n["id"] for n in payload["nodes"]}
        assert "orphan" in ids
        assert len(payload["edges"]) == 1

    def test_under_the_cap_nothing_is_trimmed(self):
        channels = [{"channel_id": "c1", "title": "One", "subscriber_count": 1, "discovery_method": "keyword"}]
        payload = build_graph_payload(channels, edges=[], max_nodes=2000)
        assert payload["truncated"] is False

    def test_over_the_cap_hydrated_channels_are_never_dropped(self):
        channels = [
            {"channel_id": f"c{i}", "title": f"Ch {i}", "subscriber_count": i, "discovery_method": "keyword"}
            for i in range(5)
        ]
        edges = [
            {
                "source_channel_id": "c0",
                "target_channel_id": None,
                "target_channel_ref": f"https://youtube.com/@frontier{i}",
                "edge_type": "featured_channel",
            }
            for i in range(20)
        ]
        payload = build_graph_payload(channels, edges, max_nodes=10)
        assert payload["truncated"] is True
        resolved_ids = {n["id"] for n in payload["nodes"] if n["resolved"]}
        assert resolved_ids == {"c0", "c1", "c2", "c3", "c4"}
        assert len(payload["nodes"]) == 10

    def test_trimming_drops_edges_touching_removed_nodes(self):
        channels = [{"channel_id": "c0", "title": "Hub", "subscriber_count": 1, "discovery_method": "keyword"}]
        edges = [
            {
                "source_channel_id": "c0",
                "target_channel_id": None,
                "target_channel_ref": f"https://youtube.com/@frontier{i}",
                "edge_type": "featured_channel",
            }
            for i in range(10)
        ]
        payload = build_graph_payload(channels, edges, max_nodes=3)
        ids = {n["id"] for n in payload["nodes"]}
        for e in payload["edges"]:
            assert e["source"] in ids and e["target"] in ids

    def test_channels_missing_an_id_are_skipped(self):
        channels = [{"channel_id": "", "title": "No id", "subscriber_count": 1, "discovery_method": "keyword"}]
        payload = build_graph_payload(channels, edges=[])
        assert payload["nodes"] == []


def _tree_node(id_, label, depth, parent_id, **overrides):
    node = {
        "id": id_, "label": label, "depth": depth, "parent_id": parent_id,
        "children_ids": [],  # deliberately never populated, matching real data
        "status": "compacted", "split_method": "llm_seed",
        "cluster_distinctness_score": None, "seed_channel_ids": [],
        "keywords": [], "compaction_summary": "",
    }
    node.update(overrides)
    return node


class TestBuildTaxonomyPayload:
    def test_topology_is_derived_from_parent_id_not_children_ids(self):
        """The real children_ids field is written at split time and never
        kept in sync — every compacted node observed in production carries
        children_ids: [] despite having children pointing at it via
        parent_id. Topology must come from parent_id alone."""
        tree = {
            "root": _tree_node("root", "Finance", 0, None),
            "a": _tree_node("a", "Investing", 1, "root"),
            "b": _tree_node("b", "Budgeting", 1, "root"),
        }
        payload = build_taxonomy_payload(tree)
        assert payload["roots"] == ["root"]
        assert set(payload["children"]["root"]) == {"a", "b"}

    def test_all_nodes_are_included_no_trim_policy(self):
        tree = {f"n{i}": _tree_node(f"n{i}", f"Node {i}", 1, "root") for i in range(50)}
        tree["root"] = _tree_node("root", "Root", 0, None)
        payload = build_taxonomy_payload(tree)
        assert len(payload["nodes"]) == 51

    def test_a_node_with_a_dangling_parent_id_becomes_a_root(self):
        """Defensive: a partial/interrupted run should still render rather
        than crash on a parent_id that points at nothing in this tree."""
        tree = {"orphan": _tree_node("orphan", "Orphan", 1, "missing_parent")}
        payload = build_taxonomy_payload(tree)
        assert payload["roots"] == ["orphan"]

    def test_coverage_counts_prefer_category_tags_over_seed_count(self):
        tree = {"root": _tree_node("root", "Finance", 0, None, seed_channel_ids=["@a", "@b"])}
        counts = {"root": {"channels": 285, "videos": 9598}}
        payload = build_taxonomy_payload(tree, counts)
        node = payload["nodes"][0]
        assert node["channel_count"] == 285
        assert node["video_count"] == 9598

    def test_coverage_falls_back_to_seed_count_when_untagged(self):
        tree = {"root": _tree_node("root", "Finance", 0, None, seed_channel_ids=["@a", "@b", "@c"])}
        payload = build_taxonomy_payload(tree, counts=None)
        assert payload["nodes"][0]["channel_count"] == 3
        assert payload["nodes"][0]["video_count"] == 0

    def test_status_counts_tally_every_node(self):
        tree = {
            "root": _tree_node("root", "Finance", 0, None, status="compacted"),
            "a": _tree_node("a", "A", 1, "root", status="active"),
            "b": _tree_node("b", "B", 1, "root", status="pending"),
            "c": _tree_node("c", "C", 1, "root", status="saturated"),
        }
        payload = build_taxonomy_payload(tree)
        assert payload["status_counts"] == {"compacted": 1, "active": 1, "pending": 1, "saturated": 1}

    def test_governor_saturation_reason_is_preserved_verbatim(self):
        tree = {
            "root": _tree_node("root", "Finance", 0, None,
                                saturation_reason="governor:brightdata_record_budget"),
        }
        payload = build_taxonomy_payload(tree)
        assert payload["nodes"][0]["saturation_reason"] == "governor:brightdata_record_budget"

    def test_empty_tree_produces_an_empty_payload_not_a_crash(self):
        payload = build_taxonomy_payload({})
        assert payload["nodes"] == []
        assert payload["roots"] == []

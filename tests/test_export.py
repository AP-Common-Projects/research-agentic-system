"""build_graph_payload and its helpers — the data feeding the interactive
discovery-graph export. The interesting cases are the ref-identity problem
(edges whose target was never hydrated) and the trim policy when a run's
graph exceeds export_max_graph_nodes.
"""

from __future__ import annotations

from src.export import _category, _ref_label, build_graph_payload, flag_low_confidence_channels


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


class TestFlagLowConfidenceChannels:
    """Two independent contamination sources found in the Finance export:
    60 zero-subscriber keyword-search hits with no other discovery edge
    (likely dead/spam/placeholder channels), and 65 real channels whose
    only discovery edge is a comment_author edge (someone commented on a
    video — no evidence they're a finance channel). Zero overlap between
    the two in that data, but the flag must handle both independently."""

    def test_zero_subscriber_channel_is_flagged(self):
        channels = [{"channel_id": "c1", "subscriber_count": 0}]
        flagged = flag_low_confidence_channels(channels, comment_author_only_ids=set())
        assert flagged[0]["low_confidence"] is True
        assert flagged[0]["low_confidence_reasons"] == ["zero_subscribers"]

    def test_null_subscriber_count_is_also_flagged(self):
        channels = [{"channel_id": "c1", "subscriber_count": None}]
        flagged = flag_low_confidence_channels(channels, comment_author_only_ids=set())
        assert flagged[0]["low_confidence"] is True
        assert flagged[0]["low_confidence_reasons"] == ["zero_subscribers"]

    def test_comment_author_only_channel_is_flagged_even_with_real_subscribers(self):
        channels = [{"channel_id": "c1", "subscriber_count": 1650000}]
        flagged = flag_low_confidence_channels(channels, comment_author_only_ids={"c1"})
        assert flagged[0]["low_confidence"] is True
        assert flagged[0]["low_confidence_reasons"] == ["comment_author_only"]

    def test_a_real_channel_with_subscribers_and_no_comment_flag_is_clean(self):
        channels = [{"channel_id": "c1", "subscriber_count": 500000}]
        flagged = flag_low_confidence_channels(channels, comment_author_only_ids=set())
        assert flagged[0]["low_confidence"] is False
        assert flagged[0]["low_confidence_reasons"] == []

    def test_both_reasons_can_apply_at_once(self):
        channels = [{"channel_id": "c1", "subscriber_count": 0}]
        flagged = flag_low_confidence_channels(channels, comment_author_only_ids={"c1"})
        assert flagged[0]["low_confidence"] is True
        assert set(flagged[0]["low_confidence_reasons"]) == {"zero_subscribers", "comment_author_only"}

    def test_does_not_mutate_the_input_dicts(self):
        original = {"channel_id": "c1", "subscriber_count": 0}
        channels = [original]
        flag_low_confidence_channels(channels, comment_author_only_ids=set())
        assert "low_confidence" not in original

    def test_preserves_all_other_fields(self):
        channels = [{"channel_id": "c1", "subscriber_count": 100, "title": "Some Channel"}]
        flagged = flag_low_confidence_channels(channels, comment_author_only_ids=set())
        assert flagged[0]["title"] == "Some Channel"
        assert flagged[0]["channel_id"] == "c1"

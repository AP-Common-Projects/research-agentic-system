"""build_graph_payload and its helpers — the data feeding the interactive
discovery-graph export. The interesting cases are the ref-identity problem
(edges whose target was never hydrated) and the trim policy when a run's
graph exceeds export_max_graph_nodes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.export import (
    _category,
    _excel_safe,
    _ref_label,
    _report_markdown,
    build_excel_workbook,
    build_graph_payload,
    flag_low_confidence_channels,
)


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


def _report(**overrides):
    r = {
        "niche": "Finance",
        "summary": "A summary of the niche.",
        "findings": [
            {"grade": "strong", "claim": "A strong claim.", "supporting_channel_ids": ["c1"]},
            {"grade": "weak", "claim": "A weak claim.", "supporting_channel_ids": []},
        ],
        "cannot_determine": ["Whether content quality drives growth."],
        "recommendations": {"do": ["Make audit-format videos."], "avoid": ["Generic tutorials."]},
    }
    r.update(overrides)
    return r


def _manifest(**overrides):
    m = {
        "run_id": "run-x", "niche": "Finance", "channels": 2, "videos": 2,
        "exported_at": "2026-08-15T00:00:00+00:00",
        "stop_reasons": ["governor:brightdata_record_budget"],
        "low_confidence_channels": {"zero_subscribers": 1, "comment_author_only": 0, "total_flagged": 1},
        "sub_niches_covered": 2,
    }
    m.update(overrides)
    return m


class TestReportMarkdown:
    """The report is a brief for a content team, not a pipeline status
    page — no mention of governors, saturation, or which channels the
    export is less confident about. That provenance lives in manifest.json
    and channels.csv's low_confidence_reasons column instead."""

    def test_no_saturation_or_governor_language(self):
        md = _report_markdown(_report(), _manifest(), channels=[], branch_counts={}, tree={})
        assert "saturation" not in md.lower()
        assert "governor" not in md.lower()

    def test_no_low_confidence_caveat(self):
        md = _report_markdown(_report(), _manifest(), channels=[], branch_counts={}, tree={})
        assert "low_confidence" not in md
        assert "flagged" not in md.lower()

    def test_no_warning_marker_on_channels(self):
        channels = [{
            "channel_id": "c1", "title": "Coin Bureau", "subscriber_count": 2720000,
            "discovery_method": "unattributed", "low_confidence_reasons": ["comment_author_only"],
        }]
        md = _report_markdown(_report(), _manifest(), channels, branch_counts={}, tree={})
        assert "⚠" not in md

    def test_overview_table_lists_sub_niches_by_depth_then_size(self):
        tree = {
            "root": {"label": "Finance", "depth": 0},
            "a": {"label": "Crypto", "depth": 1},
            "b": {"label": "Investing", "depth": 1},
        }
        branch_counts = {
            "root": {"channels": 300, "videos": 9000},
            "a": {"channels": 100, "videos": 3000},
            "b": {"channels": 200, "videos": 4000},
        }
        md = _report_markdown(_report(), _manifest(), [], branch_counts, tree)
        overview = md.split("## Overview")[1].split("## Summary")[0]
        # root (depth 0) first, then depth-1 branches sorted by channel count desc
        assert overview.index("Finance") < overview.index("Investing") < overview.index("Crypto")

    def test_branch_with_no_data_is_omitted_from_overview(self):
        tree = {
            "root": {"label": "Finance", "depth": 0},
            "empty": {"label": "Empty Branch", "depth": 1},
        }
        branch_counts = {"root": {"channels": 10, "videos": 100}}
        md = _report_markdown(_report(), _manifest(), [], branch_counts, tree)
        assert "Empty Branch" not in md

    def test_what_to_do_and_avoid_sections_present_when_recommendations_exist(self):
        md = _report_markdown(_report(), _manifest(), [], {}, {})
        assert "## What to do" in md
        assert "Make audit-format videos." in md
        assert "## What to avoid" in md
        assert "Generic tutorials." in md

    def test_sections_omitted_when_no_recommendations(self):
        md = _report_markdown(_report(recommendations={}), _manifest(), [], {}, {})
        assert "## What to do" not in md
        assert "## What to avoid" not in md

    def test_findings_and_top_channels_still_present(self):
        channels = [{
            "channel_id": "c1", "title": "Graham Stephan", "subscriber_count": 5000000,
            "discovery_method": "keyword",
        }]
        md = _report_markdown(_report(), _manifest(), channels, {}, {})
        assert "## Findings" in md
        assert "A strong claim." in md
        assert "## Top channels by audience" in md
        assert "Graham Stephan" in md

    def test_channel_titles_with_embedded_newlines_dont_break_markdown(self):
        """A real YouTube channel title can contain a literal newline —
        observed live, it split a "Channels: ..." list item into a stray
        blank line and a dangling continuation ("Blockchain basics\\n\\n,
        Tax.Crypto")."""
        report = _report(findings=[{
            "grade": "moderate", "claim": "A claim.", "supporting_channel_ids": ["c1"],
        }])
        channels = [{"channel_id": "c1", "title": "Blockchain basics\n\n", "subscriber_count": 100}]
        md = _report_markdown(report, _manifest(), channels, {}, {})
        assert "Channels: Blockchain basics" in md
        assert "\n\n," not in md


class TestBuildExcelWorkbook:
    def test_has_the_three_expected_sheets(self):
        wb = build_excel_workbook(_manifest(), [], [], {}, {})
        assert wb.sheetnames == ["Overview", "Channels", "Outlier Videos"]

    def test_overview_sheet_has_totals(self):
        manifest = _manifest(channels=706, videos=500, sub_niches_covered=3)
        wb = build_excel_workbook(manifest, [], [], {}, {})
        ws = wb["Overview"]
        values = [cell.value for row in ws.iter_rows() for cell in row]
        assert 706 in values
        assert 500 in values
        assert 3 in values

    def test_overview_sheet_lists_sub_niche_breakdown(self):
        tree = {
            "root": {"label": "Finance", "depth": 0},
            "a": {"label": "Crypto", "depth": 1},
        }
        branch_counts = {"root": {"channels": 300, "videos": 9000}, "a": {"channels": 100, "videos": 3000}}
        wb = build_excel_workbook(_manifest(), [], [], branch_counts, tree)
        ws = wb["Overview"]
        values = [cell.value for row in ws.iter_rows() for cell in row]
        assert "Finance" in values
        assert "Crypto" in values

    def test_channels_sheet_has_header_and_data_rows(self):
        channels = [
            {"channel_id": "c1", "title": "Channel One", "subscriber_count": 1000},
            {"channel_id": "c2", "title": "Channel Two", "subscriber_count": 2000},
        ]
        wb = build_excel_workbook(_manifest(), channels, [], {}, {})
        ws = wb["Channels"]
        assert [c.value for c in ws[1]] == ["channel_id", "title", "subscriber_count"]
        assert [c.value for c in ws[2]] == ["c1", "Channel One", 1000]
        assert [c.value for c in ws[3]] == ["c2", "Channel Two", 2000]

    def test_list_valued_cells_are_joined_not_left_as_python_lists(self):
        channels = [{"channel_id": "c1", "low_confidence_reasons": ["zero_subscribers", "comment_author_only"]}]
        wb = build_excel_workbook(_manifest(), channels, [], {}, {})
        ws = wb["Channels"]
        assert ws[2][1].value == "zero_subscribers; comment_author_only"

    def test_outlier_videos_sheet_has_header_and_data_rows(self):
        videos = [{"video_id": "v1", "title": "A Video", "view_count": 5000}]
        wb = build_excel_workbook(_manifest(), [], videos, {}, {})
        ws = wb["Outlier Videos"]
        assert [c.value for c in ws[1]] == ["video_id", "title", "view_count"]
        assert [c.value for c in ws[2]] == ["v1", "A Video", 5000]

    def test_empty_sheets_dont_crash(self):
        wb = build_excel_workbook(_manifest(), [], [], {}, {})
        assert wb["Channels"]["A1"].value == "(no rows)"
        assert wb["Outlier Videos"]["A1"].value == "(no rows)"

    def test_tz_aware_datetimes_dont_crash_the_workbook_save(self, tmp_path):
        """Postgres TIMESTAMPTZ columns come back as tz-aware datetimes via
        psycopg — openpyxl raises TypeError on those outright (Excel has no
        timezone type). Caught live: exporting Finance's real
        first_seen_at/published_at columns crashed the save() call."""
        published = datetime(2026, 1, 1, tzinfo=timezone.utc)
        channels = [{"channel_id": "c1", "first_seen_at": published}]
        videos = [{"video_id": "v1", "published_at": published}]
        wb = build_excel_workbook(_manifest(), channels, videos, {}, {})
        wb.save(tmp_path / "test.xlsx")  # must not raise
        assert (tmp_path / "test.xlsx").exists()

    def test_control_characters_in_descriptions_dont_crash_the_workbook_save(self, tmp_path):
        """Real channel descriptions can carry raw control characters —
        openpyxl raises IllegalCharacterError outright rather than
        stripping them. Caught live exporting Legal's real data."""
        channels = [{"channel_id": "c1", "description": "Helping Attorneys\x0bBuild Their Practice"}]
        wb = build_excel_workbook(_manifest(), channels, [], {}, {})
        wb.save(tmp_path / "test.xlsx")  # must not raise
        assert (tmp_path / "test.xlsx").exists()


class TestExcelSafe:
    def test_strips_timezone_from_datetimes(self):
        aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = _excel_safe(aware)
        assert result.tzinfo is None
        assert result == datetime(2026, 1, 1)

    def test_naive_datetimes_pass_through_unchanged(self):
        naive = datetime(2026, 1, 1)
        assert _excel_safe(naive) == naive

    def test_joins_lists(self):
        assert _excel_safe(["a", "b"]) == "a; b"

    def test_other_types_pass_through(self):
        assert _excel_safe("plain string") == "plain string"
        assert _excel_safe(42) == 42
        assert _excel_safe(None) is None

    def test_strips_illegal_control_characters_from_strings(self):
        """openpyxl raises IllegalCharacterError outright on these — caught
        live exporting a real Legal channel description containing one."""
        assert _excel_safe("before\x0bafter") == "beforeafter"
        assert _excel_safe("clean text") == "clean text"

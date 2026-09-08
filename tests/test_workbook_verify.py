"""Reading back the workbook that was written.

Every completeness failure this project has shipped had one shape: the
check and the artifact were two different things that agreed only while
somebody kept them in step.

  the gate audited the channels the run DISCOVERED (15) while the workbook
  was written from the ones it TAGGED (9)

  it declared three of the Videos sheet's forty columns and reported
  COMPLETE over the other thirty-seven

  it counted factor rows in the TABLE for the workbook's channels while
  the sheet came from a query filtered on who extracted them, so it said
  "not empty" over two blank sheets

  it skipped a sheet with no rows without a word

Each was fixed where it happened; the class was not. These assert the one
check that cannot drift: it opens the file and reads the cells.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools import workbook_verify as wv


def _write(tmp_path: Path, sheets: dict[str, tuple[list, list]]) -> Path:
    """sheets: {name: (header, rows)}."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, (header, rows) in sheets.items():
        ws = wb.create_sheet(name)
        ws.append(header)
        for r in rows:
            ws.append(r)
    path = tmp_path / "wb.xlsx"
    wb.save(path)
    return path


def _full(tmp_path: Path, **overrides) -> Path:
    """A workbook that verifies, so each test changes exactly one thing."""
    sheets = {
        "Channels": (["channel_id", "primary_topic"], [["c1", "gaming"]]),
        "Videos": (["video_id", "video_description"], [["v1", "about it"]]),
        "Niche Families": (["niche_family", "distinct_sub_niches"],
                           [["Exam Prep", 12]]),
        "Niches": (["niche_id"], [["n1"]]),
        "Success Factors": (["factor_code"], [["f1"]]),
        "Failure Factors": (["factor_code"], [["f2"]]),
    }
    sheets.update(overrides)
    return _write(tmp_path, sheets)


class TestItReadsTheFileNotTheDatabase:
    def test_a_complete_workbook_verifies(self, tmp_path):
        report = wv.verify_workbook(_full(tmp_path))
        assert report.ok, report.render()
        assert report.findings, "it must actually have read some columns"

    def test_an_empty_sheet_fails_however_full_the_table_is(self):
        """The crime run of 2026-09-07: factors on all four channels in the
        table, both sheets blank in the file, gate COMPLETE. Nothing about
        the database can rescue a sheet with no rows in it."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = _full(Path(d), **{"Success Factors": (["factor_code"], [])})
            report = wv.verify_workbook(path)
        assert "Success Factors" in report.empty_sheets
        assert not report.ok

    def test_a_missing_sheet_fails(self, tmp_path):
        sheets = {
            "Channels": (["channel_id"], [["c1"]]),
            "Videos": (["video_id"], [["v1"]]),
            "Niches": (["niche_id"], [["n1"]]),
            "Success Factors": (["factor_code"], [["f1"]]),
        }
        report = wv.verify_workbook(_write(tmp_path, sheets))
        assert "Failure Factors" in report.missing_sheets
        assert not report.ok

    def test_shorts_may_be_absent(self, tmp_path):
        """A run that found no shorts has no Shorts sheet, and that is an
        answer rather than a gap."""
        report = wv.verify_workbook(_full(tmp_path))
        assert "Shorts" not in report.missing_sheets

    def test_a_workbook_with_niches_and_no_families_is_a_gap(self, tmp_path):
        """The family tier never ran. Absent, not blank -- which no fill
        rate can see."""
        sheets = {
            "Channels": (["channel_id"], [["c1"]]),
            "Videos": (["video_id"], [["v1"]]),
            "Niches": (["niche_id"], [["n1"]]),
            "Success Factors": (["factor_code"], [["f1"]]),
            "Failure Factors": (["factor_code"], [["f2"]]),
        }
        report = wv.verify_workbook(_write(tmp_path, sheets))
        assert "Niche Families" in report.missing_sheets
        assert not report.ok

    def test_a_workbook_with_no_niches_needs_no_families(self, tmp_path):
        """Nothing to group. The sheet is correctly absent."""
        sheets = {
            "Channels": (["channel_id"], [["c1"]]),
            "Videos": (["video_id"], [["v1"]]),
            "Niches": (["niche_id"], []),
            "Success Factors": (["factor_code"], [["f1"]]),
            "Failure Factors": (["factor_code"], [["f2"]]),
        }
        report = wv.verify_workbook(_write(tmp_path, sheets))
        assert "Niche Families" not in report.missing_sheets

    def test_a_short_family_in_the_written_file_fails(self, tmp_path):
        report = wv.verify_workbook(_full(tmp_path, **{
            "Niche Families": (["niche_family", "distinct_sub_niches"],
                               [["Big", 30], ["Tiny", 2]]),
        }))
        assert report.short_families == [("Tiny", 2)]
        assert not report.ok

    def test_an_unpopulated_column_fails(self, tmp_path):
        path = _full(tmp_path, Channels=(
            ["channel_id", "primary_topic"],
            [["c1", "gaming"], ["c2", None], ["c3", None]],
        ))
        report = wv.verify_workbook(path)
        short = [f for f in report.failures if f.column == "primary_topic"]
        assert short and short[0].filled == 1

    def test_a_blank_string_counts_as_unpopulated(self, tmp_path):
        path = _full(tmp_path, Niches=(["niche_id"], [["n1"], [""]]))
        report = wv.verify_workbook(path)
        assert [f for f in report.failures if f.column == "niche_id"]


class TestItHonoursTheSameContractAsTheGate:
    def test_a_waived_column_is_not_flagged(self, tmp_path):
        """description is waived -- a channel need not write one -- and the
        verifier must not disagree with the gate about that."""
        path = _full(tmp_path, Channels=(
            ["channel_id", "description"], [["c1", None], ["c2", None]],
        ))
        report = wv.verify_workbook(path)
        assert not [f for f in report.failures if f.column == "description"]

    def test_a_declared_threshold_wins_over_the_default(self, tmp_path):
        """country_code sits at 0.70 because a channel with no country
        signal has none."""
        path = _full(tmp_path, Channels=(
            ["channel_id", "country_code"],
            [["c1", "US"], ["c2", "GB"], ["c3", "FR"], ["c4", None]],
        ))
        report = wv.verify_workbook(path)
        found = next(f for f in report.findings if f.column == "country_code")
        assert found.min_fill == 0.70
        # 3 of 4 clears its own bar and would fail the strict default,
        # which is the whole point of declaring one.
        assert found.ok
        assert found.rate < wv._DEFAULT_MIN_FILL

    def test_shorts_answers_to_the_videos_contract(self, tmp_path):
        path = _full(tmp_path, Shorts=(
            ["video_id", "video_description"], [["v1", "x"]],
        ))
        report = wv.verify_workbook(path)
        assert any(f.sheet == "Shorts" for f in report.findings)

    def test_an_undeclared_column_is_still_read(self, tmp_path):
        """Columns renamed at write time -- "Channel Creation Date",
        "duration(in minute -by default)" -- match nothing declared, and
        must be measured rather than skipped."""
        path = _full(tmp_path, Channels=(
            ["channel_id", "Channel Creation Date"],
            [["c1", None], ["c2", None]],
        ))
        report = wv.verify_workbook(path)
        assert [f for f in report.failures if f.column == "Channel Creation Date"]


class TestACrimeWorkbookMustCarryItsCaseFile:
    """A crime run whose category failed to resolve dropped all fifteen
    case columns and shipped without any of them. Absent, not blank -- and
    no fill rate can see a column that is not there."""

    def test_missing_case_columns_are_caught(self, tmp_path):
        path = _full(tmp_path)
        report = wv.verify_workbook(path, expect_crime=True)
        assert report.missing_columns, "the case file must be present"
        assert any("crime_type" in c for c in report.missing_columns)
        assert not report.ok

    def test_a_non_crime_workbook_is_not_asked_for_them(self, tmp_path):
        report = wv.verify_workbook(_full(tmp_path), expect_crime=False)
        assert report.missing_columns == []

    def test_present_case_columns_are_then_checked_for_fill(self, tmp_path):
        header = ["video_id"] + [c.column for c in wv.CRIME_VIDEO_CHECKS]
        row = ["v1"] + [None] * len(wv.CRIME_VIDEO_CHECKS)
        path = _full(tmp_path, Videos=(header, [row, ["v2"] + ["x"] * len(wv.CRIME_VIDEO_CHECKS)]))
        report = wv.verify_workbook(path, expect_crime=True)
        assert report.missing_columns == []
        assert [f for f in report.failures if f.column == "crime_type"]


class TestItNeverCostsTheClientTheFile:
    def test_an_unreadable_workbook_is_reported_not_raised(self, tmp_path):
        bad = tmp_path / "not-a-workbook.xlsx"
        bad.write_text("this is not a spreadsheet")
        report = wv.verify_workbook(bad)
        assert report.unreadable
        assert not report.ok
        assert "UNREADABLE" in report.render()

    def test_the_report_says_which_file_it_read(self, tmp_path):
        report = wv.verify_workbook(_full(tmp_path))
        assert "wb.xlsx" in report.render()

    def test_a_passing_report_says_how_much_it_read(self, tmp_path):
        report = wv.verify_workbook(_full(tmp_path))
        assert "VERIFIED" in report.render()
        assert "columns read from the file" in report.render()


class TestItRunsAfterEveryExport:
    def test_the_cli_verifies_what_it_wrote(self):
        src = open("src/cli.py", encoding="utf-8").read()
        export_at = src.index("xlsx_path = export_excel(")
        verify_at = src.index("verify_run_workbook(")
        assert verify_at > export_at, (
            "the file has to exist before it can be read back"
        )

    def test_a_failed_verification_is_said_loudly(self):
        src = open("src/cli.py", encoding="utf-8").read()
        assert "did NOT verify" in src

    def test_the_verdict_is_left_beside_the_workbook(self):
        """So it survives the console scrolling away."""
        src = open("src/cli.py", encoding="utf-8").read()
        assert "verification.txt" in src


class TestWhetherItIsACrimeWorkbookIsAskedSafely:
    """The resolved category alone is not a safe answer, because the failure
    it is meant to catch is the one that destroys it.

    A crime run whose classification comes out thin or tied resolves to no
    category; the export then treats it as a non-crime workbook and drops
    all fifteen case columns -- and a verifier asking that same resolved
    category would agree they were not wanted, and pass a crime deliverable
    with no case file in it. That is what shipped on 2026-09-07.
    """

    def test_the_seed_topic_decides_even_when_the_category_did_not_resolve(
        self, tmp_path, monkeypatch
    ):
        from unittest.mock import patch

        from src.export import WorkbookScope

        path = _full(tmp_path)
        unresolved = WorkbookScope(category=None, min_subscribers=None,
                                   own_only=False, video_limit=None)
        with patch.object(wv, "_run_seed_niches", return_value=["crime"]), \
             patch("src.export.workbook_scope", return_value=unresolved):
            report = wv.verify_run_workbook("run-x", path)

        assert report.missing_columns, (
            "a run the client asked for as crime must be held to its case "
            "file however the classification turned out"
        )

    def test_the_resolved_category_still_counts(self, tmp_path):
        from unittest.mock import patch

        from src.export import WorkbookScope

        crime = WorkbookScope(category="crime", min_subscribers=None,
                              own_only=False, video_limit=None)
        with patch.object(wv, "_run_seed_niches", return_value=[]), \
             patch("src.export.workbook_scope", return_value=crime):
            report = wv.verify_run_workbook("run-x", _full(tmp_path))
        assert report.missing_columns

    def test_a_non_crime_run_is_not_asked_for_them(self, tmp_path):
        from unittest.mock import patch

        from src.export import WorkbookScope

        other = WorkbookScope(category="gaming", min_subscribers=None,
                              own_only=False, video_limit=None)
        with patch.object(wv, "_run_seed_niches", return_value=["gaming"]), \
             patch("src.export.workbook_scope", return_value=other):
            report = wv.verify_run_workbook("run-x", _full(tmp_path))
        assert report.missing_columns == []

    def test_the_seed_is_read_from_the_launch_registry(self):
        """What the client typed, recorded before any classification could
        fail."""
        import inspect

        src = inspect.getsource(wv._run_seed_niches)
        assert "registry_path" in src and "niches" in src

    def test_an_unreadable_registry_does_not_crash_the_verification(self):
        from unittest.mock import patch

        with patch("src.api.runs.registry_path", side_effect=RuntimeError("gone")):
            assert wv._run_seed_niches("run-x") == []

"""The last word: read the workbook that was written, and say what is in it.

Every completeness failure this project has shipped has one shape. The
gate checked something, the export wrote something else, and the two
questions were not the same question:

  * it audited the channels the run DISCOVERED while the workbook was
    written from the channels the run TAGGED -- 15 against 9
  * it declared three of the Videos sheet's forty columns and reported
    COMPLETE over the other thirty-seven
  * it counted factor rows in the TABLE for the workbook's channels while
    the sheet was written by a query filtered on who extracted them, so it
    said "not empty" over two blank sheets
  * it skipped a sheet with no rows without a word

Each was fixed where it happened. The class was not, because every fix
still leaves the check and the artifact as two separate things that agree
only as long as somebody keeps them in step.

So this asks the file. It opens the .xlsx that is about to be handed over
and reads the cells. There is no query to drift, no scope to re-derive,
no second opinion about which rows count: whatever a person will see when
they open the workbook is what this measures.

It is a verifier, not a healer. By the time it runs the file exists, and
the honest response to a gap here is to say so loudly against the
delivered artifact -- the gate is where filling happens, and if that did
not work, saying "COMPLETE" a second time would be the whole problem
repeating itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.tools.export_completeness import (
    _DECLARED,
    _DEFAULT_MIN_FILL,
    CRIME_VIDEO_CHECKS,
    CRIME_WAIVERS,
    SHEET_WAIVERS,
)

#: Sheets that carry rows of data. Overview is a label/value summary, so it
#: is checked for existence and not for column fill.
_DATA_SHEETS = frozenset({
    "Channels", "Videos", "Shorts", "Niches",
    "Success Factors", "Failure Factors",
})

#: Shorts is Videos split at write time; it answers to the same contract.
_CONTRACT = {"shorts": "videos"}


@dataclass
class SheetFinding:
    sheet: str
    column: str
    filled: int
    total: int
    min_fill: float

    @property
    def rate(self) -> float:
        return (self.filled / self.total) if self.total else 1.0

    @property
    def ok(self) -> bool:
        return self.rate >= self.min_fill


@dataclass
class VerifyReport:
    path: str
    findings: list[SheetFinding] = field(default_factory=list)
    empty_sheets: list[str] = field(default_factory=list)
    missing_sheets: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    unreadable: str = ""

    @property
    def failures(self) -> list[SheetFinding]:
        return [f for f in self.findings if not f.ok]

    @property
    def ok(self) -> bool:
        return not (self.failures or self.empty_sheets or self.missing_sheets
                    or self.missing_columns or self.unreadable)

    def render(self) -> str:
        name = Path(self.path).name
        lines = [f"  workbook verification for {name}"]
        if self.unreadable:
            lines.append(f"  UNREADABLE: {self.unreadable}")
            return "\n".join(lines)
        for sheet in self.missing_sheets:
            lines.append(f"  MISSING SHEET: {sheet}")
        for sheet in self.empty_sheets:
            lines.append(f"  EMPTY SHEET: {sheet}")
        for col in self.missing_columns:
            lines.append(f"  MISSING COLUMN: {col}")
        for f in sorted(self.failures, key=lambda x: x.rate):
            lines.append(
                f"  SHORT  {f.sheet}.{f.column:<28} {f.filled}/{f.total} "
                f"{f.rate * 100:5.1f}%  needs {f.min_fill * 100:.0f}%"
            )
        checked = len(self.findings)
        lines.append(
            f"  VERIFIED — {checked} columns read from the file, all populated"
            if self.ok else
            f"  NOT VERIFIED — {len(self.failures)} short of "
            f"{checked} columns read from the file"
        )
        return "\n".join(lines)


def _waivers_for(sheet: str, is_crime: bool) -> dict[str, str]:
    from src.export import ALWAYS_DROPPED_COLUMNS

    key = "Videos" if sheet == "Shorts" else sheet
    out = dict(SHEET_WAIVERS.get(key, {}))
    out.update({c: "removed from the deliverable" for c in ALWAYS_DROPPED_COLUMNS})
    out.update(CRIME_WAIVERS)
    if not is_crime:
        out.update({c.column: "crime only" for c in CRIME_VIDEO_CHECKS})
    return out


def verify_workbook(
    path: str | Path, expect_crime: bool = False
) -> VerifyReport:
    """Read the written workbook and report what is actually in it.

    `expect_crime` says the workbook is a crime deliverable, so its case
    columns have to be present as well as populated. A crime run whose
    category failed to resolve dropped all fifteen of them and shipped
    without any -- absent, not blank, which no fill rate can see.
    """
    report = VerifyReport(path=str(path))
    try:
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        report.unreadable = f"{type(exc).__name__}: {exc}"
        return report

    try:
        present = set(wb.sheetnames)
        for sheet in sorted(_DATA_SHEETS):
            if sheet not in present:
                # Shorts is legitimately absent when a run found none.
                if sheet != "Shorts":
                    report.missing_sheets.append(sheet)
                continue

            rows = list(wb[sheet].iter_rows(values_only=True))
            header = list(rows[0]) if rows else []
            body = rows[1:]
            if not body:
                report.empty_sheets.append(sheet)
                continue

            if expect_crime and sheet in ("Videos", "Shorts"):
                for check in CRIME_VIDEO_CHECKS:
                    if check.column not in header:
                        report.missing_columns.append(f"{sheet}.{check.column}")

            waived = _waivers_for(sheet, expect_crime)
            contract = _CONTRACT.get(sheet.lower(), sheet.lower())
            for index, column in enumerate(header):
                if not column or column in waived:
                    continue
                filled = sum(
                    1 for r in body
                    if index < len(r) and r[index] not in (None, "")
                )
                declared = _DECLARED.get((contract, column))
                report.findings.append(SheetFinding(
                    sheet=sheet,
                    column=str(column),
                    filled=filled,
                    total=len(body),
                    min_fill=declared.min_fill if declared else _DEFAULT_MIN_FILL,
                ))
    finally:
        wb.close()

    return report


def verify_run_workbook(run_id: str, path: str | Path) -> VerifyReport:
    """verify_workbook, with `expect_crime` answered from the run itself."""
    try:
        from src.export import workbook_scope

        is_crime = workbook_scope(run_id).category == "crime"
    except Exception:
        is_crime = False
    return verify_workbook(path, expect_crime=is_crime)

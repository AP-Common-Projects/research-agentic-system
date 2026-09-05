"""The exported Excel deliverables, for the console's reports page.

Distinct from /api/reports, which lists in-run report objects held in graph
state. These are the finished client artefacts on disk -- the files someone
actually sends to a client.

Metadata is read straight from the workbook (sheet names and row counts)
rather than kept in a side manifest, so the page cannot drift from the file.
That read is cached against the file's mtime and size, because opening a
5 MB workbook on every page load would make the console feel broken.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Curated rather than globbed: `exports/` also holds per-run scratch output
# that is not a client deliverable, and the client asked for these two.
CATALOG: list[dict[str, str]] = [
    {
        "id": "finance",
        "title": "Finance",
        "vertical": "finance",
        "path": "exports/now big runs-Aug 22/Finance/finance.xlsx",
        "description": "Finance channel research workbook",
    },
    {
        "id": "crime",
        "title": "Crime",
        "vertical": "crime",
        "path": "exports/now big runs-Aug 22/Crime/crime.xlsx",
        "description": "Crime channel research workbook",
    },
]

# key -> (mtime, size, payload)
_meta_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _read_sheet_summary(path: Path) -> dict[str, Any]:
    """Sheet names and row counts, via a read-only streaming open."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        sheets = []
        for ws in wb.worksheets:
            # In read-only mode max_row is None until the sheet has been
            # sized -- it silently reports 0 rows for a full workbook, which
            # is how this page first rendered "0 channels" against a 4 MB
            # file. Counting the streamed rows is the reliable read, and the
            # mtime cache keeps it off the hot path.
            total = sum(1 for _ in ws.iter_rows(values_only=True))
            sheets.append({"name": ws.title, "rows": max(total - 1, 0)})
        return {"sheets": sheets}
    finally:
        wb.close()


def _entry(spec: dict[str, str]) -> dict[str, Any]:
    path = REPO_ROOT / spec["path"]
    row: dict[str, Any] = {
        "id": spec["id"],
        "title": spec["title"],
        "vertical": spec["vertical"],
        "description": spec["description"],
        "filename": path.name,
        "available": path.exists(),
        "download_url": f"/api/workbooks/{spec['id']}/download",
    }
    if not path.exists():
        row["error"] = "File not found on disk."
        return row

    stat = path.stat()
    row["size_bytes"] = stat.st_size
    row["modified_at"] = stat.st_mtime

    cached = _meta_cache.get(spec["id"])
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        row.update(cached[2])
        return row

    try:
        summary = _read_sheet_summary(path)
    except Exception as exc:
        row["error"] = f"Could not read workbook: {type(exc).__name__}: {exc}"
        return row

    # Headline numbers the client actually asks about, pulled from the
    # sheets rather than recomputed from the database -- what is IN the file
    # is what the page should claim.
    by_name = {s["name"]: s["rows"] for s in summary["sheets"]}
    summary["channel_count"] = by_name.get("Channels", 0)
    summary["video_count"] = by_name.get("Videos", 0) + by_name.get("Shorts", 0)

    _meta_cache[spec["id"]] = (stat.st_mtime, stat.st_size, summary)
    row.update(summary)
    return row


def list_workbooks() -> list[dict[str, Any]]:
    return [_entry(spec) for spec in CATALOG]


def resolve_path(workbook_id: str) -> Path | None:
    for spec in CATALOG:
        if spec["id"] == workbook_id:
            path = REPO_ROOT / spec["path"]
            return path if path.exists() else None
    return None

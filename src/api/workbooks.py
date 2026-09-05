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


def discovered_specs() -> list[dict[str, str]]:
    """Workbooks produced by runs, newest first.

    CATALOG holds the two curated deliverables, which live outside the
    per-run export layout and are pinned by hand. Everything a run exports
    lands at exports/<run_id>/<run_id>.xlsx, and before this those files
    existed on disk but could never appear in the console -- a client
    launched a run, it finished, and the Workbooks page still showed only
    the two originals.
    """
    root = REPO_ROOT / "exports"
    if not root.is_dir():
        return []

    pinned = {spec["path"] for spec in CATALOG}
    # Only runs this console launched. exports/ still holds directories from
    # CLI-era runs whose history was deliberately cleared from the app, and
    # listing their workbooks would put back exactly what that removed.
    try:
        from src.api.runs import load_registry

        known = {e.get("run_id") for e in load_registry()}
    except Exception:
        known = set()

    found: list[tuple[float, dict[str, str]]] = []
    for xlsx in root.glob("*/*.xlsx"):
        rel = xlsx.relative_to(REPO_ROOT).as_posix()
        if rel in pinned:
            continue
        run_id = xlsx.parent.name
        if run_id not in known:
            continue
        try:
            mtime = xlsx.stat().st_mtime
        except OSError:
            continue
        found.append((mtime, {
            "id": run_id,
            "title": _title_for_run(run_id),
            "vertical": "",
            "path": rel,
            "description": f"Exported by {run_id}",
        }))

    found.sort(key=lambda pair: pair[0], reverse=True)
    return [spec for _, spec in found]


def _title_for_run(run_id: str) -> str:
    """The topic the run was asked to research, falling back to its id.

    A page listing "run-c6c45a3e91b2" tells the reader nothing; the seed
    niche is what they typed to start it.
    """
    try:
        from src.export import run_seed_niches

        seeds = run_seed_niches(run_id)
    except Exception:
        seeds = []
    if not seeds:
        return run_id
    return ", ".join(s.replace("_", " ").title() for s in seeds)


def _all_specs() -> list[dict[str, str]]:
    return [*CATALOG, *discovered_specs()]


def list_workbooks() -> list[dict[str, Any]]:
    return [_entry(spec) for spec in _all_specs()]


def resolve_path(workbook_id: str) -> Path | None:
    for spec in _all_specs():
        if spec["id"] == workbook_id:
            path = REPO_ROOT / spec["path"]
            return path if path.exists() else None
    return None

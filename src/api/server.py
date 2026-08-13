"""FastAPI backend for the niche-research console.

Adds an interface over the harness; it is not a second execution path. Runs
are launched through the same ``python -m src.cli`` entrypoint, state is read
from the same Postgres checkpointer, and telemetry is read from the JSONL
sink that ``src/observability`` already writes.

Run it:
    uvicorn src.api.server:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.api import runs as runs_mod
from src.api import store_queries

app = FastAPI(title="Niche Research Console API", version="1.0.0")

# The Vite dev server runs on a different origin during development. The
# production path serves the built frontend from this same process, where
# CORS is irrelevant.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class LaunchRequest(BaseModel):
    niches: list[str] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness plus a store-reachability probe the UI surfaces as a banner.

    Uses a short, dedicated timeout (store_queries.ping) rather than a real
    query — polled every 30s by the shell, this must fail fast when the
    store is down instead of tying up that whole interval on one probe.
    """
    store_ok, store_error = True, None
    try:
        store_queries.ping()
    except Exception as exc:
        store_ok, store_error = False, f"{type(exc).__name__}: {exc}"
    return {"status": "ok", "store_reachable": store_ok, "store_error": store_error}


@app.get("/api/runs")
def api_list_runs() -> list[dict[str, Any]]:
    return runs_mod.list_runs()


@app.post("/api/runs", status_code=201)
def api_launch_run(req: LaunchRequest) -> dict[str, Any]:
    niches = [n.strip() for n in req.niches if n.strip()]
    if not niches:
        raise HTTPException(status_code=422, detail="At least one non-empty niche is required.")
    try:
        return runs_mod.launch_run(niches)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not launch run: {exc}") from exc


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str) -> dict[str, Any]:
    run = runs_mod.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")

    state: dict[str, Any] | None = None
    state_error: str | None = None
    thread_id = run.get("thread_id")

    if thread_id:
        try:
            state = _load_checkpoint(thread_id)
        except Exception as exc:
            # A missing checkpoint is normal early in a run; report it rather
            # than returning a shape that implies the run has no state.
            state_error = f"{type(exc).__name__}: {exc}"

    return {"run": run, "state": state, "state_error": state_error}


def _load_checkpoint(thread_id: str) -> dict[str, Any] | None:
    from src.db.checkpointer import get_checkpointer
    from src.graph import compile_graph

    graph_app = compile_graph(checkpointer=get_checkpointer())
    snapshot = graph_app.get_state({"configurable": {"thread_id": thread_id}})
    if not snapshot or not snapshot.values:
        return None

    values = dict(snapshot.values)
    return {
        "selected_niche": values.get("selected_niche", ""),
        "tree": values.get("tree", {}),
        "active_node_id": values.get("active_node_id"),
        "budget_spent_usd": values.get("budget_spent_usd", 0.0),
        "novelty_rates": values.get("novelty_rates", []),
        "saturated_branches": values.get("saturated_branches", []),
        "errors": values.get("errors", []),
        "final_report": values.get("final_report"),
        "schema_version": values.get("schema_version"),
        "discovered_channel_count": len(values.get("discovered_channel_ids", [])),
        "discovered_video_count": len(values.get("discovered_video_ids", [])),
        "keyword_channel_count": len(values.get("keyword_channel_ids", set()) or set()),
        "graph_walk_channel_count": len(values.get("graph_walk_channel_ids", set()) or set()),
        "graph_walk_exclusive_count": len(
            (values.get("graph_walk_channel_ids", set()) or set())
            - (values.get("keyword_channel_ids", set()) or set())
        ),
    }


@app.get("/api/reports")
def api_list_reports() -> list[dict[str, Any]]:
    """Every finished report across all runs — the console's report library.

    A report only exists once a run reaches synthesize, so this is strictly a
    subset of /api/runs; runs still in progress or that never produced one
    (errored out, saturated with nothing found) are simply absent here rather
    than showing up as an empty/broken card.
    """
    reports: list[dict[str, Any]] = []
    for run in runs_mod.list_runs():
        thread_id = run.get("thread_id")
        if not thread_id:
            continue
        try:
            state = _load_checkpoint(thread_id)
        except Exception:
            continue
        report = state.get("final_report") if state else None
        if not report:
            continue

        findings = report.get("findings", [])
        grade_counts = {"strong": 0, "moderate": 0, "weak": 0}
        for finding in findings:
            grade = finding.get("grade")
            if grade in grade_counts:
                grade_counts[grade] += 1

        reports.append(
            {
                "run_id": run["run_id"],
                "niche": report.get("niche", ""),
                "generated_at": report.get("generated_at"),
                "summary": report.get("summary", ""),
                "finding_count": len(findings),
                "grade_counts": grade_counts,
                "cannot_determine_count": len(report.get("cannot_determine", [])),
                "discovery_stats": report.get("discovery_stats", {}),
            }
        )

    reports.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    return reports


@app.get("/api/runs/{run_id}/logs")
def api_run_logs(run_id: str, since: int = Query(0, ge=0)) -> dict[str, Any]:
    entries = runs_mod.read_run_log(run_id, since=since)
    return {"entries": entries, "cursor": since + len(entries)}


@app.get("/api/runs/{run_id}/events")
async def api_run_events(run_id: str, since: int = Query(0, ge=0)) -> StreamingResponse:
    """Server-sent events: new NodeLog lines as the run writes them.

    Polling a multi-hour run for a handful of node transitions wastes both
    request volume and the operator's attention; SSE pushes each node as it
    lands and costs one idle connection.
    """

    async def event_stream() -> AsyncIterator[str]:
        cursor = since
        idle_ticks = 0
        try:
            while True:
                entries = await asyncio.to_thread(runs_mod.read_run_log, run_id, cursor)
                if entries:
                    idle_ticks = 0
                    for entry in entries:
                        cursor += 1
                        payload = json.dumps({"cursor": cursor, "entry": entry}, default=str)
                        yield f"event: node\ndata: {payload}\n\n"
                else:
                    idle_ticks += 1
                    # Comment frames keep proxies and browsers from dropping an
                    # otherwise-silent connection during long crawl phases.
                    yield ": keepalive\n\n"

                run = await asyncio.to_thread(runs_mod.get_run, run_id)
                if run and run.get("status") != "running" and idle_ticks >= 2:
                    yield f"event: end\ndata: {json.dumps({'cursor': cursor})}\n\n"
                    return

                await asyncio.sleep(1.0)
        except asyncio.CancelledError:  # client navigated away
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _store_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Store unreachable: {type(exc).__name__}: {exc}",
        ) from exc


@app.get("/api/store/counts")
def api_store_counts() -> dict[str, Any]:
    counts = _store_call(store_queries.store_counts)
    return {
        **counts,
        "by_discovery_method": _store_call(store_queries.discovery_method_breakdown),
    }


@app.get("/api/store/channels")
def api_channels(q: str = "", limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    return _store_call(store_queries.search_channels, q, limit)


@app.get("/api/store/videos/outliers")
def api_outliers(limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    return _store_call(store_queries.top_outlier_videos, limit)


@app.get("/api/store/graph")
def api_graph(
    channel_id: str = "", limit: int = Query(400, ge=1, le=2000)
) -> dict[str, Any]:
    return _store_call(store_queries.discovery_graph, channel_id, limit)


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------


@app.get("/api/costs")
def api_costs() -> dict[str, Any]:
    by_node: dict[str, dict[str, Any]] = {}
    by_run: dict[str, dict[str, Any]] = {}
    total = 0.0

    for run in runs_mod.list_runs():
        run_id = run["run_id"]
        for entry in runs_mod.read_run_log(run_id):
            cost = float(entry.get("cost_usd") or 0.0)
            latency = entry.get("latency_ms")
            node = entry.get("node_name", "unknown")
            total += cost

            node_row = by_node.setdefault(
                node, {"node_name": node, "calls": 0, "cost_usd": 0.0, "_latencies": []}
            )
            node_row["calls"] += 1
            node_row["cost_usd"] += cost
            if isinstance(latency, (int, float)):
                node_row["_latencies"].append(float(latency))

            run_row = by_run.setdefault(
                run_id,
                {"run_id": run_id, "niches": run.get("niches", []), "calls": 0, "cost_usd": 0.0},
            )
            run_row["calls"] += 1
            run_row["cost_usd"] += cost

    node_rows = []
    for row in by_node.values():
        latencies = row.pop("_latencies")
        row["cost_usd"] = round(row["cost_usd"], 6)
        row["avg_latency_ms"] = round(sum(latencies) / len(latencies), 1) if latencies else None
        node_rows.append(row)
    node_rows.sort(key=lambda r: r["cost_usd"], reverse=True)

    run_rows = sorted(by_run.values(), key=lambda r: r["cost_usd"], reverse=True)
    for row in run_rows:
        row["cost_usd"] = round(row["cost_usd"], 6)

    return {"total_usd": round(total, 6), "by_node": node_rows, "by_run": run_rows}


# ---------------------------------------------------------------------------
# Static frontend (production build). Registered last so /api/* always wins.
# ---------------------------------------------------------------------------

_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def mount_console(application: FastAPI = app) -> bool:
    """Serve the built SPA, if it has been built.

    A bare StaticFiles mount 404s on client-side routes like /graph, because
    no such file exists on disk. The catch-all hands those back index.html so
    a deep link or a refresh lands on the right view; real asset paths are
    still served from disk, and unknown /api/* paths still 404 as they should.
    """
    if not _DIST.is_dir():
        return False

    application.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @application.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Unknown API route")
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")

    return True


CONSOLE_MOUNTED = mount_console()
